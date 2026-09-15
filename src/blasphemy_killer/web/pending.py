"""Cleans that were promised but have not run yet, remembered across restarts.

A clean queued from the listing is a live decision: if the server goes down
before it runs, the person who made it is sitting right there and can make it
again. A clean queued by a finished download is not -- it is a promise made on
their behalf, often while they are somewhere else entirely, and a restart in
the gap would leave the file sitting in the library looking merely "not
scanned". Nothing would ever say a promise had been dropped.

So only the automatic ones are written down, in the config volume next to
cookies.txt and marker.key, and re-queued at startup. Re-queueing a file that
did get cleaned costs nothing: process() skips on a valid marker.

The file is validated when it is read rather than trusted because it exists --
the same lesson as the cookies store. A truncated or hand-mangled file must not
be able to stop the server from starting.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .. import config as config_module
from .durable import write_atomic

PENDING_NAME = "pending-cleans.json"
VERSION = 1

# Read-modify-write from the worker thread, and read once at startup.
_lock = threading.Lock()


def path() -> Path:
    """Read through the module, so BK_CONFIG_DIR (or a test) can redirect it."""
    return config_module.CONFIG_DIR / PENDING_NAME


def _read() -> list[dict]:
    """Every entry that survives validation. A file that is missing, corrupt,
    or the wrong shape reads as no pending cleans at all."""
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return []
    entries = raw.get("cleans")
    if not isinstance(entries, list):
        return []
    clean: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        clean.append({"path": rel, "force": bool(entry.get("force"))})
    return clean


def _write(entries: list[dict]) -> None:
    write_atomic(path(), json.dumps({"version": VERSION, "cleans": entries}, indent=2))


def load() -> list[dict]:
    with _lock:
        return _read()


def record(rel_path: str, *, force: bool = False) -> None:
    """Note that rel_path is owed a clean. Recorded before the job is queued,
    so the crash window is the queue itself and not the writing down of it."""
    with _lock:
        entries = [e for e in _read() if e["path"] != rel_path]
        entries.append({"path": rel_path, "force": force})
        _write(entries)


def clear(rel_path: str) -> None:
    """The promise is settled -- cleaned, skipped, failed or cancelled alike.

    A failure is deliberately not retried: re-queueing a file that ffmpeg
    cannot process would mean doing it again on every restart forever, and the
    failed job says so in the UI at the time.
    """
    with _lock:
        entries = _read()
        remaining = [e for e in entries if e["path"] != rel_path]
        if len(remaining) != len(entries):
            _write(remaining)
