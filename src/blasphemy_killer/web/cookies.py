"""The cookies.txt the web UI uploads, and where downloads look for it.

yt-dlp needs a logged-in session for age-gated, members-only, private or
region-locked videos. The CLI takes a path to a file; the browser cannot, since
an unauthenticated page that opened server-side paths would be a way to read
any file on the host. So the browser sends the file's *text* and it is stored
here, in the config directory next to marker.key -- which is also where the
Docker instructions have always told people to put it, because yt-dlp may
rewrite the file with refreshed cookies after a download.
"""

from __future__ import annotations

from pathlib import Path

from .. import config as config_module
from ..config import Config
from .durable import write_atomic

UPLOAD_NAME = "cookies.txt"

# A whole browser profile's cookies run to tens of kilobytes. Past this it is
# not a cookies.txt, and it is not going into the config volume.
MAX_BYTES = 2 * 1024 * 1024


class CookieError(ValueError):
    """The uploaded text is not a usable Netscape cookies.txt."""


def upload_path() -> Path:
    """Where an uploaded file lives. Read through the module rather than
    imported, so a test (or BK_CONFIG_DIR) can redirect the config dir."""
    return config_module.CONFIG_DIR / UPLOAD_NAME


def usable(path: Path) -> bool:
    """Whether a stored cookies file is still readable as Netscape format.

    Existence is not enough. A file that is there but unusable is worse than no
    file at all: yt-dlp refuses it outright, so *every* download fails until
    somebody notices. An unclean shutdown can leave exactly that -- a zero-byte
    cookies.txt -- so the content is checked rather than trusted because the
    path exists.
    """
    try:
        validate(path.read_bytes())
    except (CookieError, OSError):
        return False
    return True


def effective(cfg: Config) -> Path | None:
    """The cookies file a download should use.

    An uploaded file wins over the config.toml setting: it is the more recent
    and more explicit choice, and it survives a restart, so what the UI shows
    is what the next download gets either way. It only wins while it is still
    usable, though -- a corrupt upload falls back to config.toml instead of
    breaking downloads that would otherwise have worked.
    """
    uploaded = upload_path()
    return uploaded if uploaded.is_file() and usable(uploaded) else cfg.cookies


def _is_cookie_line(line: str) -> bool:
    # "#HttpOnly_" is a real cookie in this format, not a comment -- a file of
    # nothing but HttpOnly cookies is still a valid one.
    if not line.strip():
        return False
    if line.startswith("#") and not line.startswith("#HttpOnly_"):
        return False
    return len(line.split("\t")) >= 7


def count_cookies(text: str) -> int:
    return sum(1 for line in text.splitlines() if _is_cookie_line(line))


def validate(raw: bytes) -> str:
    """Check the upload really is a Netscape cookies.txt, and say what is
    wrong when it is not -- the usual mistake is a JSON export, and "yt-dlp
    failed" three minutes into a download is a miserable way to find out."""
    if len(raw) > MAX_BYTES:
        raise CookieError(f"larger than {MAX_BYTES // 1024} KB — that is not a cookies.txt")
    if not raw.strip():
        raise CookieError("the file is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CookieError("not a text file — cookies.txt is plain text") from None
    if text.lstrip()[:1] in ("[", "{"):
        raise CookieError(
            "this looks like a JSON cookie export; yt-dlp needs the Netscape "
            'format (the "Get cookies.txt LOCALLY" extension exports it)'
        )
    if not count_cookies(text):
        raise CookieError(
            "no cookie lines found — a Netscape cookies.txt has one cookie per "
            "line, in tab-separated fields"
        )
    return text


def save(raw: bytes) -> Path:
    """Validate and store the upload, replacing any previous one."""
    # 0600 from the moment it exists, and swapped in atomically: the file
    # grants access to the uploader's logged-in accounts, and a download may be
    # reading the previous one right now. write_atomic also flushes it to the
    # disk before the rename -- a kernel panic in that gap is how this ends up
    # as a zero-byte cookies.txt that refuses every later download.
    return write_atomic(upload_path(), validate(raw), mode=0o600)


def remove() -> bool:
    """Delete the uploaded file. Returns whether there was one."""
    try:
        upload_path().unlink()
        return True
    except FileNotFoundError:
        return False


def status(cfg: Config) -> dict:
    """What the UI shows about the current cookies -- never the cookies
    themselves, only that they are there and roughly how many."""
    path = effective(cfg)
    uploaded = upload_path()
    if path is None or not path.is_file():
        return {"present": False, "uploaded": False, "path": None,
                "count": 0, "size": 0, "mtime": None}
    try:
        st = path.stat()
        count = count_cookies(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {"present": False, "uploaded": False, "path": None,
                "count": 0, "size": 0, "mtime": None}
    return {
        "present": True,
        "uploaded": path == uploaded,   # False = named by config.toml, not ours to delete
        "path": str(path),
        "count": count,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }
