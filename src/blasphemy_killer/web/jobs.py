"""Single-worker job queue driving pipeline.process and URL downloads.

One worker, deliberately: transcription saturates the CPU, so a second
concurrent job would only make both slower. The worker is a plain thread rather
than anything on the event loop -- ctranslate2 releases the GIL while it
computes, so SSE keeps flowing while a job runs.

Downloads share that worker rather than running alongside it. A download is
network-bound and would happily overlap a transcription, but one queue means
one obvious order of events, and a file cannot start being cleaned halfway
through arriving.

A download that lands queues its own clean behind it. Pasting a URL is a
request for a cleaned file, not for a file plus a second decision, so the
follow-up job is created here -- by the worker that knows the name yt-dlp
finally gave it -- rather than waiting for a browser to notice and ask.
"""

from __future__ import annotations

import itertools
import queue
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Config
from ..download import DownloadError, download
from ..media import MediaError
from ..pipeline import (
    Cancelled, Finished, MarkerUnsigned, MatchFound, MatchingComplete,
    Progress, Skipped, StageChanged, process,
)

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
PROCESS, DOWNLOAD = "process", "download"


@dataclass
class Job:
    id: str
    path: str            # relative to the media root; "" until a download names its file
    dry_run: bool
    force: bool
    kind: str = PROCESS  # PROCESS | DOWNLOAD
    url: str | None = None
    dest: str = ""       # download only: directory, relative to the media root
    clean: bool = False  # download only: clean the file once it lands
    source: str | None = None  # process only: the download job that produced it
    state: str = QUEUED
    stage: str | None = None
    progress: float = 0.0
    matches: list[dict] = field(default_factory=list)
    muted: int = 0
    skipped_reason: str | None = None
    unsigned_marker: bool = False
    cancel_requested: bool = False
    error: str | None = None
    elapsed: float = 0.0
    created: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        return asdict(self)


class JobQueue:
    def __init__(self, root: Path, cfg: Config, broadcast: Callable[[dict], None],
                 on_file_changed: Callable[[Path], None] | None = None,
                 resolve_cookies: Callable[[], Path | None] | None = None):
        self.root = root
        self.cfg = cfg
        self._broadcast = broadcast
        self._on_file_changed = on_file_changed
        # Resolved per download, not once at startup: the cookies file can be
        # uploaded or removed while the server is running.
        self._resolve_cookies = resolve_cookies
        self._ids = itertools.count(1)
        self._pending: queue.Queue[Job] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._current: Job | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bk-worker", daemon=True)
        self._thread.start()

    # --- public API ---------------------------------------------------------

    def submit(self, rel_path: str, *, dry_run: bool, force: bool,
               source: str | None = None) -> Job:
        """Enqueue a file. A path already queued or running is not queued twice."""
        with self._lock:
            for existing in self._jobs.values():
                if existing.path == rel_path and existing.state in (QUEUED, RUNNING):
                    return existing
            job = Job(id=str(next(self._ids)), path=rel_path, dry_run=dry_run,
                      force=force, source=source)
            self._jobs[job.id] = job
        self._pending.put(job)
        self._publish(job)
        return job

    def submit_download(self, url: str, dest: str, *, clean: bool = True) -> Job:
        """Enqueue a URL download into dest (a directory relative to the media
        root). Unless clean is false, the downloaded file is queued for
        cleaning as soon as it lands. A URL already queued or running is not
        queued twice."""
        with self._lock:
            for existing in self._jobs.values():
                if existing.url == url and existing.state in (QUEUED, RUNNING):
                    return existing
            job = Job(
                id=str(next(self._ids)), path="", dry_run=True, force=False,
                kind=DOWNLOAD, url=url, dest=dest, clean=clean,
            )
            self._jobs[job.id] = job
        self._pending.put(job)
        self._publish(job)
        return job

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in (DONE, FAILED, CANCELLED):
                return job
            self._cancelled.add(job_id)
            if job.state == QUEUED:
                # Never started; mark it now. The worker skips it when it pops.
                job.state = CANCELLED
            else:
                # Already running. It only stops at the next checkpoint (a
                # whisper segment boundary, which can be seconds away), so flag
                # the request now rather than leaving the UI looking stuck.
                job.cancel_requested = True
        self._publish(job)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            return [j.snapshot() for j in sorted(self._jobs.values(), key=lambda j: int(j.id))]

    def shutdown(self) -> None:
        self._stop.set()
        self._pending.put(None)  # type: ignore[arg-type]

    # --- worker -------------------------------------------------------------

    def _publish(self, job: Job) -> None:
        self._broadcast({"type": "job", "job": job.snapshot()})

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._pending.get()
            if job is None:
                return
            if job.state == CANCELLED:
                continue
            if job.kind == DOWNLOAD:
                self._execute_download(job)
            else:
                self._execute(job)

    def _execute(self, job: Job) -> None:
        job.state = RUNNING
        job.stage = "probing"
        self._current = job
        self._publish(job)
        started = time.monotonic()

        def check_cancelled() -> None:
            if job.id in self._cancelled:
                raise Cancelled()

        def on_event(event) -> None:
            match event:
                case StageChanged(stage):
                    job.stage = stage
                case Progress(fraction):
                    job.progress = fraction
                case MatchFound(m):
                    job.matches.append({
                        "start": m.start, "end": m.end, "text": m.text, "phrase": m.phrase,
                    })
                case MatchingComplete(_):
                    job.progress = 1.0
                case MarkerUnsigned():
                    job.unsigned_marker = True
                case Skipped(reason, _):
                    job.skipped_reason = reason
                case Finished(result):
                    job.muted = len(result.intervals)
                    job.elapsed = result.elapsed
                case _:
                    return
            self._publish(job)

        target = self.root / job.path
        try:
            with tempfile.TemporaryDirectory(prefix="blasphemy-killer-") as tmp:
                process(
                    target, self.cfg,
                    dry_run=job.dry_run, force=job.force, tmp_dir=Path(tmp),
                    on_event=on_event, check_cancelled=check_cancelled,
                )
            job.state = DONE
        except Cancelled:
            job.state = CANCELLED
        except (MediaError, OSError, ValueError) as exc:
            job.state = FAILED
            job.error = str(exc)
        finally:
            job.stage = None
            job.elapsed = job.elapsed or (time.monotonic() - started)
            self._current = None
            self._cancelled.discard(job.id)
            if job.state == DONE and not job.dry_run and self._on_file_changed:
                self._on_file_changed(target)
            self._publish(job)

    def _execute_download(self, job: Job) -> None:
        job.state = RUNNING
        job.stage = "downloading"
        self._current = job
        self._publish(job)
        started = time.monotonic()
        last_published = 0.0

        def on_progress(stage: str, fraction: float | None) -> None:
            """Called from inside yt-dlp. Raising is how a cancel gets out."""
            nonlocal last_published
            if job.id in self._cancelled:
                raise Cancelled()
            changed = stage != job.stage
            job.stage = stage
            if fraction is not None:
                job.progress = fraction
            # yt-dlp fires this per chunk; publishing every one would flood SSE.
            now = time.monotonic()
            if changed or now - last_published >= 0.25:
                last_published = now
                self._publish(job)

        root = self.root.resolve()
        dest = (root / job.dest) if job.dest else root
        try:
            cookies = (
                self._resolve_cookies() if self._resolve_cookies else self.cfg.cookies
            )
            landed = download(
                job.url, cookies=cookies,
                dest_dir=dest, on_progress=on_progress,
            ).resolve()
            try:
                job.path = str(landed.relative_to(root))
            except ValueError:
                raise ValueError(f"download landed outside the media root: {landed}") from None
            job.state = DONE
        except Cancelled:
            job.state = CANCELLED
        except (DownloadError, OSError, ValueError) as exc:
            job.state = FAILED
            job.error = str(exc)
        finally:
            job.stage = None
            job.progress = 1.0 if job.state == DONE else job.progress
            job.elapsed = time.monotonic() - started
            self._current = None
            self._cancelled.discard(job.id)
            self._publish(job)

        if job.state == DONE and job.clean and job.path:
            # Queued, not run inline: it belongs behind anything already
            # waiting, and it is a job of its own in the UI -- cancellable,
            # with its own progress and its own list of matches.
            self.submit(job.path, dry_run=False, force=False, source=job.id)
