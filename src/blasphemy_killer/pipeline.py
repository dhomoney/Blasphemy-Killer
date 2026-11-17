"""The per-file pipeline, decoupled from how progress is reported.

`process()` runs probe -> extract -> transcribe -> match -> render -> replace
and emits events as it goes. The CLI turns those events into terminal output;
the web UI turns them into SSE messages. Neither formatting concern lives here.
"""

from __future__ import annotations

import errno
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
    # probing|extracting|transcribing|loading-model|matching|rendering|verifying|reporting
    # loading-model comes inside transcribing, when the whisper model is not
    # loaded yet, and is followed by transcribing again.
    stage: str


@dataclass(frozen=True)
class Progress:
    """How far the run has got.

    fraction is the current stage's own share done, 0.0-1.0. overall is the
    whole file's, from STAGE_WEIGHTS: it never goes down and ends at 1.0. One
    is emitted at every stage change too, at the stage's fraction 0.0.
    """
    fraction: float
    stage: str = "transcribing"
    overall: float | None = None


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


# Relative cost of each stage, for Progress.overall. Rough shares of a typical
# run's wall time; tune them from the elapsed_seconds in .bk.json reports.
# Loading (or downloading) the model has no measurable length, so it weighs
# nothing and is shown as text rather than as a made-up percentage.
STAGE_WEIGHTS: dict[str, float] = {
    "probing": 1,
    "extracting": 9,
    "loading-model": 0,
    "transcribing": 70,
    "matching": 1,
    "rendering": 18,
    "verifying": 0.5,
    "reporting": 0.5,
}


class _Tracker:
    """Emits StageChanged and Progress, keeping the overall figure.

    The planned stages are normalised to 1.0, so a dry run (no render) and a
    run without a report still end at exactly 100%.
    """

    def __init__(self, emit: OnEvent, stages: list[str]):
        self._emit = emit
        total = sum(STAGE_WEIGHTS[s] for s in stages)
        self._start: dict[str, float] = {}
        self._share: dict[str, float] = {}
        done = 0.0
        for s in stages:
            self._start[s] = done / total
            self._share[s] = STAGE_WEIGHTS[s] / total
            done += STAGE_WEIGHTS[s]
        self.stage = stages[0]
        self.overall = 0.0

    def enter(self, stage: str) -> None:
        self.stage = stage
        self._emit(StageChanged(stage))
        self.progress(0.0)

    def progress(self, fraction: float) -> None:
        fraction = min(1.0, max(0.0, fraction))
        value = self._start.get(self.stage, self.overall) + self._share.get(self.stage, 0.0) * fraction
        self.overall = max(self.overall, min(1.0, value))
        self._emit(Progress(fraction, self.stage, self.overall))

    def finish(self) -> None:
        self.overall = 1.0
        self._emit(Progress(1.0, self.stage, 1.0))


@dataclass
class Result:
    status: str  # "skipped" | "dry-run" | "processed"
    reason: str | None = None
    marker: str | None = None
    matches: list[Match] = field(default_factory=list)
    intervals: list[tuple[float, float]] = field(default_factory=list)
    elapsed: float = 0.0
    duration: float = 0.0


# POSIX only: Windows has no O_NOFOLLOW, and touching it there raised
# AttributeError after a clean had already replaced the file.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def write_report(report_path: Path, report: dict) -> None:
    """Write the JSON sidecar, refusing to follow a pre-placed symlink."""
    if not _O_NOFOLLOW and report_path.is_symlink():
        # Without the flag, check first. There is a window between the check
        # and the open, which O_NOFOLLOW closes where it exists.
        raise OSError(errno.ELOOP, "refusing to write through a symlink", str(report_path))
    fd = os.open(
        report_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW,
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

    on_event receives progress events; check_cancelled is called at each
    stage start, once per transcribed segment, and just before the original
    is replaced, and should raise Cancelled to abort. Both optional.
    """
    emit: OnEvent = on_event or (lambda _event: None)
    check: CheckCancelled = check_cancelled or (lambda: None)
    started = time.monotonic()
    stages = ["probing", "extracting", "loading-model", "transcribing", "matching"]
    if not dry_run:
        stages += ["rendering", "verifying"] + (["reporting"] if cfg.write_report else [])
    track = _Tracker(emit, stages)

    track.enter("probing")
    check()
    info = probe(path)

    if info.marker and not force:
        if marker_valid(info):
            result = Result(status="skipped", reason="already-processed", marker=info.marker)
            emit(Skipped("already-processed", info.marker))
            track.finish()
            emit(Finished(result))
            return result
        emit(MarkerUnsigned())

    stream = info.transcription_stream
    if stream is None:
        result = Result(status="skipped", reason="no-audio")
        emit(Skipped("no-audio"))
        track.finish()
        emit(Finished(result))
        return result

    track.enter("extracting")
    check()
    wav = tmp_dir / "audio.wav"
    extract_wav(path, stream, wav, center_channel=cfg.center_channel,
                duration=info.duration, on_progress=track.progress)

    # No track.enter("transcribing") here: transcribe() announces it itself,
    # after "loading-model" when the model has to load first. Entering it here
    # too showed "transcribing" twice around the model load.
    check()
    from .transcribe import transcribe  # deferred: heavy import, not needed for --help etc.
    words = transcribe(
        wav, model=cfg.model, language=cfg.language or None,
        cpu_threads=cfg.cpu_threads, beam_size=cfg.beam_size,
        vad_filter=cfg.vad_filter, vad_threshold=cfg.vad_threshold,
        on_progress=track.progress,
        on_stage=track.enter,
        check_cancelled=check_cancelled,
    )
    wav.unlink(missing_ok=True)

    track.enter("matching")
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
        track.finish()
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
    replaced = False
    try:
        track.enter("rendering")
        check()
        if intervals:
            render(info, intervals, out_tmp, tmp_dir / "filter.txt",
                   on_progress=track.progress)
        else:
            stamp_only(info, out_tmp, on_progress=track.progress)
        track.enter("verifying")
        verify_output(info, out_tmp)
        # The last point a cancel can still be honoured. Rendering is the long
        # pass and has no checkpoints of its own, so a request made during it
        # arrives here -- before the original is touched, which is the only
        # thing that makes stopping worth anything.
        check()
        atomic_replace(out_tmp, path, keep_backup=cfg.keep_backup and bool(intervals))
        replaced = True
    finally:
        if not replaced:
            # Whatever went wrong -- a failure, or a cancel, which is not an
            # error and so never reached the old except clause. The temp file
            # is a dot-file, and Library.list_dir filters those out, so one
            # left behind here is invisible in the media directory forever.
            out_tmp.unlink(missing_ok=True)

    if cfg.write_report:
        track.enter("reporting")
        write_report(
            path.with_name(path.name + ".bk.json"),
            _build_report(path, cfg, info, matches, intervals, time.monotonic() - started),
        )

    result = Result(
        status="processed", matches=matches, intervals=intervals,
        elapsed=time.monotonic() - started, duration=info.duration,
    )
    track.finish()
    emit(Finished(result))
    return result
