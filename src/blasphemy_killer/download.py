"""yt-dlp wrapper: download a URL, return the path of the resulting video file."""

from __future__ import annotations

from pathlib import Path


class DownloadError(Exception):
    pass


def is_url(arg: str) -> bool:
    return arg.startswith(("http://", "https://"))


def _js_runtimes() -> dict[str, dict]:
    """YouTube's "n challenge" needs a JavaScript runtime to solve. yt-dlp only
    enables deno by default; enable every runtime it knows so an already
    installed node/bun/quickjs works too. yt-dlp picks the best one present and
    ignores the rest, so listing an uninstalled runtime costs nothing."""
    try:
        from yt_dlp.globals import supported_js_runtimes
    except ImportError:  # yt-dlp too old to have the option at all
        return {}
    return {name: {} for name in supported_js_runtimes.value}


def download(url: str, output: Path | None = None,
             cookies: Path | None = None) -> Path:
    """Download url with yt-dlp. If output is given it names the final file
    (extension may be adjusted to match the merged container). cookies names a
    Netscape-format cookies.txt used for age-gated or members-only videos."""
    import yt_dlp

    if output is not None:
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Fixed name: strip any extension yt-dlp will re-add for the merge format.
        outtmpl = str(output.with_suffix("")) + ".%(ext)s"
    else:
        outtmpl = "%(title)s [%(id)s].%(ext)s"

    opts = {
        "outtmpl": outtmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": False,
    }
    if runtimes := _js_runtimes():
        opts["js_runtimes"] = runtimes
    if cookies is not None:
        opts["cookiefile"] = str(cookies.expanduser())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(str(exc)) from exc

    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    # Fallback for older yt-dlp info dicts.
    with yt_dlp.YoutubeDL(opts) as ydl:
        return Path(ydl.prepare_filename(info))
