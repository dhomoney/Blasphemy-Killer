import errno
import os
from pathlib import Path

import pytest

from blasphemy_killer.media import atomic_replace


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    tmp = tmp_path / ".movie.bk-tmp.abc.mp4"
    original = tmp_path / "movie.mp4"
    tmp.write_bytes(b"new")
    original.write_bytes(b"old")
    os.chmod(tmp, 0o600)
    os.chmod(original, 0o644)
    return tmp, original


def test_replaces_and_carries_permissions(pair):
    tmp, original = pair
    atomic_replace(tmp, original)
    assert original.read_bytes() == b"new"
    assert not tmp.exists()
    assert (original.stat().st_mode & 0o7777) == 0o644


def test_keep_backup(pair):
    tmp, original = pair
    atomic_replace(tmp, original, keep_backup=True)
    assert original.read_bytes() == b"new"
    assert original.with_name("movie.mp4.bak").read_bytes() == b"old"


def test_chmod_eperm_tolerated(pair, monkeypatch):
    # CIFS/SMB without POSIX extensions rejects chmod with EPERM; the
    # replace must still go through.
    tmp, original = pair

    def deny(*args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", deny)
    atomic_replace(tmp, original)
    assert original.read_bytes() == b"new"
    assert not tmp.exists()


def test_chmod_other_oserror_propagates(pair, monkeypatch):
    tmp, original = pair

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "chmod", fail)
    with pytest.raises(OSError):
        atomic_replace(tmp, original)
    assert original.read_bytes() == b"old"
