"""Browsing the media root: safe path resolution and cached done-marker status.

Reading a file's "already cleaned" state means an ffprobe subprocess, which is
far too slow to do inline for a directory of any size. Listings return
immediately with names and sizes; status is filled in by a small background
pool and pushed to the browser as it arrives.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..media import MediaError, probe
from ..mute import marker_valid


class PathDenied(Exception):
    """A requested path resolved outside the media root."""


def resolve_within(root: Path, candidate: str | os.PathLike | None) -> Path:
    """Resolve candidate relative to root, refusing anything that escapes it.

    Symlinks are resolved before the check, so a link inside the root pointing
    outside it is rejected too. This is the only real access control the server
    has, so it is deliberately strict: relative paths only, no '..' survivors.
    """
    root = root.resolve()
    target = root if not candidate else (root / str(candidate)).resolve()
    if target != root and root not in target.parents:
        raise PathDenied(f"path outside media root: {candidate}")
    return target


@dataclass
class Entry:
    name: str
    path: str          # relative to the media root, the form the API speaks
    is_dir: bool
    size: int
    mtime: float
    cleaned: bool | None = None   # None = not probed yet


class Library:
    def __init__(self, root: Path, extensions: list[str], workers: int = 2):
        self.root = root.resolve()
        self.extensions = [e.lower() for e in extensions]
        self._probes = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bk-probe")
        # (path, mtime, size) -> cleaned. Keyed on the stat so a file that gets
        # replaced is re-probed rather than reported from a stale entry.
        self._cache: dict[tuple[str, float, int], bool] = {}

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if path != self.root else ""

    def list_dir(self, path: str | None) -> tuple[str, list[Entry]]:
        """List one directory. Returns (relative path, entries) with `cleaned`
        populated only where it is already cached."""
        target = resolve_within(self.root, path)
        if not target.is_dir():
            raise PathDenied(f"not a directory: {path}")

        entries: list[Entry] = []
        for child in sorted(
            target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        ):
            if child.name.startswith("."):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            if not is_dir and child.suffix.lower() not in self.extensions:
                continue
            entries.append(Entry(
                name=child.name,
                path=self.rel(child),
                is_dir=is_dir,
                size=st.st_size,
                mtime=st.st_mtime,
                cleaned=None if is_dir else self._cache.get(
                    (str(child), st.st_mtime, st.st_size)
                ),
            ))
        return self.rel(target), entries

    def scan(self, entries: list[Entry], on_result: Callable[[str, bool], None]) -> None:
        """Probe any entries whose status isn't cached, calling on_result(path,
        cleaned) as each finishes. Returns immediately."""
        for entry in entries:
            if entry.is_dir or entry.cleaned is not None:
                continue
            self._probes.submit(self._probe_one, entry, on_result)

    def _probe_one(self, entry: Entry, on_result: Callable[[str, bool], None]) -> None:
        full = self.root / entry.path
        try:
            info = probe(full)
            cleaned = bool(info.marker) and marker_valid(info)
        except (MediaError, OSError, ValueError):
            cleaned = False
        self._cache[(str(full), entry.mtime, entry.size)] = cleaned
        on_result(entry.path, cleaned)

    def invalidate(self, path: Path) -> None:
        """Forget a file's cached status after it has been rewritten."""
        key = str(path)
        for cached in [k for k in self._cache if k[0] == key]:
            self._cache.pop(cached, None)

    def shutdown(self) -> None:
        self._probes.shutdown(wait=False, cancel_futures=True)
