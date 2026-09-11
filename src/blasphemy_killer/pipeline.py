"""The per-file pipeline, decoupled from how progress is reported.

`process()` runs probe -> extract -> transcribe -> match -> render -> replace
and emits events as it goes. The CLI turns those events into terminal output;
the web UI turns them into SSE messages. Neither formatting concern lives here.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Config
from .match import Match, build_intervals, find_matches
from .media import MediaError, MediaInfo, atomic_replace, extract_wav, probe, verify_output
from .mute import marker_valid, render, stamp_only


class Cancelled(Exception):
    """Raised through the check_cancelled callback to abort a run in progress."""


# --- events -----------------------------------------------------------------
#
# Emitted in order. A consumer that only cares about some of them can ignore
# the rest; new event types may be added, so match on type rather than assuming
# the set is closed.


@dataclass(frozen=True)
class StageChanged:
    """Entered a new pipeline stage."""
    stage: str  # probing|extracting|transcribing|matching|rendering|verifying|reporting


@dataclass(frozen=True)
class Progress:
    """Fraction of the media transcribed so far, 0.0-1.0."""
    fraction: float


@dataclass(frozen=True)
class MatchFound:
    match: Match


@dataclass(frozen=True)
class MatchingComplete:
    count: int


@dataclass(frozen=True)
class MarkerUnsigned:
    """A done-marker was present but not signed by this machine's key."""


@dataclass(frozen=True)
class Skipped:
    reason: str  # "already-processed" | "no-audio"
    marker: str | None = None


@dataclass(frozen=True)
class Finished:
    result: "Result"


Event = (
    StageChanged | Progress | MatchFound | MatchingComplete
    | MarkerUnsigned | Skipped | Finished
)

OnEvent = Callable[[Event], None]
CheckCancelled = Callable[[], None]


@dataclass
class Result:
    status: str  # "skipped" | "dry-run" | "processed"
    reason: str | None = None
    marker: str | None = None
    matches: list[Match] = field(default_factory=list)
    intervals: list[tuple[float, float]] = field(default_factory=list)
    elapsed: float = 0.0
    duration: float = 0.0


def write_report(report_path: Path, report: dict) -> None:
    """Write the JSON sidecar, refusing to follow a pre-placed symlink."""
    fd = os.open(
        report_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o644,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(report, indent=2))


def _build_report(path: Path, cfg: Config, info: MediaInfo, matches: list[Match],
                  intervals: list[tuple[float, float]], elapsed: float) -> dict:
    return {
        "tool": "blasphemy-killer",
        "version": __version__,
        "file": str(path),
        "model": cfg.model,
        "matches": [asdict(m) for m in matches],
        "muted_intervals": intervals,
        "duration": info.duration,
        "elapsed_seconds": round(elapsed, 1),
    }


def process(path: Path, cfg: Config, *, dry_run: bool, force: bool, tmp_dir: Path,
            on_event: OnEvent | None = None, check_cancelled: CheckCancelled | None = None) -> Result:
    """Run the full pipeline on one file.

    on_event receives progress events; check_cancelled is called periodically
    during transcription and should raise Cancelled to abort. Both optional.
    """
    emit: OnEvent = on_event or (lambda _event: None)
    check: CheckCancelled = check_cancelled or (lambda: None)
    started = time.monotonic()

    emit(StageChanged("probing"))
    check()
    info = probe(path)

    if info.marker and not force:
        if marker_valid(info):
            result = Result(status="skipped", reason="already-processed", marker=info.marker)
            emit(Skipped("already-processed", info.marker))
            emit(Finished(result))
            return result
        emit(MarkerUnsigned())

    stream = info.transcription_stream
    if stream is None:
        result = Result(status="skipped", reason="no-audio")
        emit(Skipped("no-audio"))
        emit(Finished(result))
        return result

    emit(StageChanged("extracting"))
    check()
    wav = tmp_dir / "audio.wav"
    extract_wav(path, stream, wav)

    emit(StageChanged("transcribing"))
    check()
    from .transcribe import transcribe  # deferred: heavy import, not needed for --help etc.
    words = transcribe(
        wav, model=cfg.model, language=cfg.language or None,
        cpu_threads=cfg.cpu_threads, beam_size=cfg.beam_size,
        on_progress=lambda f: emit(Progress(f)),
        check_cancelled=check_cancelled,
    )
    wav.unlink(missing_ok=True)

    emit(StageChanged("matching"))
    matches = find_matches(words, cfg.phrases)
    intervals = build_intervals(
        matches, pad_before=cfg.pad_before, pad_after=cfg.pad_after,
        clamp_end=info.duration or None,
    )
    for m in matches:
        emit(MatchFound(m))
    emit(MatchingComplete(len(matches)))

    if dry_run:
        result = Result(
            status="dry-run", matches=matches, intervals=intervals,
            elapsed=time.monotonic() - started, duration=info.duration,
        )
        emit(Finished(result))
        return result

    # Random name, created 0600 with O_EXCL: not guessable or symlink-plantable
    # by another writer in a shared directory. Still matches .gitignore's
    # `.*.bk-tmp.*` pattern.
    fd, out_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.bk-tmp.", suffix=path.suffix
    )
    os.close(fd)
    out_tmp = Path(out_name)
    try:
        emit(StageChanged("rendering"))
        check()
        if intervals:
            render(info, intervals, out_tmp, tmp_dir / "filter.txt")
        else:
            stamp_only(info, out_tmp)
        emit(StageChanged("verifying"))
        verify_output(info, out_tmp)
        atomic_replace(out_tmp, path, keep_backup=cfg.keep_backup and bool(intervals))
    except (MediaError, OSError):
        out_tmp.unlink(missing_ok=True)
        raise

    if cfg.write_report:
        emit(StageChanged("reporting"))
        write_report(
            path.with_name(path.name + ".bk.json"),
            _build_report(path, cfg, info, matches, intervals, time.monotonic() - started),
        )

    result = Result(
        status="processed", matches=matches, intervals=intervals,
        elapsed=time.monotonic() - started, duration=info.duration,
    )
    emit(Finished(result))
    return result
