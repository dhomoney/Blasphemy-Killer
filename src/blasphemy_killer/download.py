"""yt-dlp wrapper: download a URL, return the path of the resulting video file."""

from __future__ import annotations

from pathlib import Path


class DownloadError(Exception):
    pass


def is_url(arg: str) -> bool:
    return arg.startswith(("http://", "https://"))


def download(url: str, output: Path | None = None) -> Path:
    """Download url with yt-dlp. If output is given it names the final file
    (extension may be adjusted to match the merged container)."""
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
