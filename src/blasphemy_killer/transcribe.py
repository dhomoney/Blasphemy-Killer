"""Thin faster-whisper wrapper producing word-level timestamps."""

from __future__ import annotations

import os
from pathlib import Path

from .match import Word

_model_cache: dict[tuple[str, int], object] = {}


def _get_model(name: str, cpu_threads: int):
    from faster_whisper import WhisperModel

    key = (name, cpu_threads)
    if key not in _model_cache:
        _model_cache[key] = WhisperModel(
            name, device="cpu", compute_type="int8", cpu_threads=cpu_threads
        )
    return _model_cache[key]


def transcribe(wav_path: Path, *, model: str, language: str | None,
               cpu_threads: int = 0, beam_size: int = 5) -> list[Word]:
    """Transcribe a mono 16 kHz WAV and return the word stream with timestamps."""
    if cpu_threads <= 0:
        cpu_threads = min(8, os.cpu_count() or 4)
    whisper = _get_model(model, cpu_threads)

    segments, _info = whisper.transcribe(
        str(wav_path),
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
        language=language or None,
        beam_size=beam_size,
    )
    words: list[Word] = []
    for segment in segments:
        for w in segment.words or []:
            words.append(Word(text=w.word, start=w.start, end=w.end))
    return words
