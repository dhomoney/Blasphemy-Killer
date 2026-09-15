"""Writes to the config directory that survive a crash, not just a shutdown.

An atomic replace only promises that a name flips from one complete file to
another. It promises nothing about the contents having reached the disk first,
and a machine that dies in that gap leaves the new name pointing at nothing --
which is exactly how a zero-byte cookies.txt broke every download in 2.2.1.

So everything the config volume holds is written the same way: into a temp file
in the same directory, flushed all the way down, renamed over the target, and
the rename itself persisted.
"""

from __future__ import annotations

import os
from pathlib import Path


def sync_dir(directory: Path) -> None:
    """Persist a rename itself, best effort.

    Without this the new name can reach the disk before the contents it points
    at, so a crash in between leaves the file there and empty. Not every
    filesystem allows fsync on a directory, and by this point the replace has
    already happened, so a refusal is not worth failing the write over.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str, *, mode: int = 0o644) -> Path:
    """Replace path with text, durably. mode applies from the moment the temp
    file exists, so a file that should never be world-readable never is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    sync_dir(path.parent)
    return path
