"""Thin faster-whisper wrapper producing word-level timestamps."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Callable

from .match import Word

_model_cache: dict[tuple[str, int], object] = {}

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")


def available_cpus() -> int:
    """CPUs this process may actually use. os.cpu_count() reports the host's
    total inside a container and ignores --cpus, so check the cgroup v2 quota
    first; without one, fall back to the affinity-aware count."""
    try:
        quota, period = CGROUP_CPU_MAX.read_text().split()
        if quota != "max":
            return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    if hasattr(os, "process_cpu_count"):  # 3.13+, honors CPU affinity
        return os.process_cpu_count() or 4
    return os.cpu_count() or 4


def _get_model(name: str, cpu_threads: int):
    from faster_whisper import WhisperModel

    key = (name, cpu_threads)
    if key not in _model_cache:
        _model_cache[key] = WhisperModel(
            name, device="cpu", compute_type="int8", cpu_threads=cpu_threads
        )
    return _model_cache[key]


def transcribe(wav_path: Path, *, model: str, language: str | None,
               cpu_threads: int = 0, beam_size: int = 5,
               on_progress: Callable[[float], None] | None = None,
               check_cancelled: Callable[[], None] | None = None) -> list[Word]:
    """Transcribe a mono 16 kHz WAV and return the word stream with timestamps.

    on_progress, if given, receives the fraction of the media transcribed so
    far. check_cancelled is called once per segment and may raise to abort;
    this is the only stage long enough to be worth interrupting.
    """
    if cpu_threads <= 0:
        cpu_threads = min(8, available_cpus())
    whisper = _get_model(model, cpu_threads)

    # faster-whisper returns a generator: segments are produced as the audio is
    # consumed, which is what makes progress reporting possible at all.
    segments, info = whisper.transcribe(
        str(wav_path),
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
        language=language or None,
        beam_size=beam_size,
    )
    total = getattr(info, "duration", 0.0) or 0.0
    words: list[Word] = []
    for segment in segments:
        if check_cancelled is not None:
            check_cancelled()
        for w in segment.words or []:
            words.append(Word(text=w.word, start=w.start, end=w.end))
        if on_progress is not None and total > 0:
            on_progress(min(1.0, segment.end / total))
    if on_progress is not None:
        on_progress(1.0)
    return words
