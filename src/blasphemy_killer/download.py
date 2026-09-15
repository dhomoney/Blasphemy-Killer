"""yt-dlp wrapper: download a URL, return the path of the resulting video file."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

# (stage, fraction) -- fraction is None while the size is still unknown.
OnProgress = Callable[[str, float | None], None]


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


class _Reporter:
    """Adapts yt-dlp's hooks to an (stage, fraction) callback.

    An exception raised by the callback -- which is how the web UI cancels a
    running download -- cannot simply travel out through yt-dlp: only
    DownloadCancelled unwinds it cleanly. So it is stashed here and re-raised
    by download() once yt-dlp has stopped.

    The .part file yt-dlp is writing is tracked for the same reason: an
    abandoned download would otherwise leave gigabytes in the media directory
    under an extension the library never lists, so nobody would ever see it.
    """

    def __init__(self, on_progress: OnProgress):
        self._on_progress = on_progress
        self.raised: BaseException | None = None
        self.partial: Path | None = None

    def downloading(self, status: dict) -> None:
        state = status.get("status")
        tmp = status.get("tmpfilename")
        if tmp and str(tmp).endswith(".part"):
            self.partial = Path(tmp)
        if state == "downloading":
            # total_bytes_estimate is a guess, and a low one often enough that
            # done/total sails past 1.0 -- a 334% progress bar was how this was
            # found. Clamp here rather than in each consumer.
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            done = status.get("downloaded_bytes") or 0
            self._report("downloading", min(done / total, 1.0) if total else None)
        elif state == "finished":
            self._report("downloading", 1.0)

    def postprocessing(self, status: dict) -> None:
        if status.get("status") == "started":
            # Merging the separate video and audio streams into one container.
            self._report("merging", None)

    def _report(self, stage: str, fraction: float | None) -> None:
        if self.raised is not None:
            return
        try:
            self._on_progress(stage, fraction)
        except BaseException as exc:      # noqa: BLE001 -- re-raised in download()
            from yt_dlp.utils import DownloadCancelled
            self.raised = exc
            raise DownloadCancelled(str(exc)) from exc


def _discard_partial(partial: Path | None) -> None:
    """Remove the half-written .part of an aborted download. Only ever the
    path yt-dlp reported writing, never a glob over the destination."""
    if partial is None:
        return
    try:
        partial.unlink(missing_ok=True)
    except OSError:
        pass          # a leftover .part is untidy, not a failure worth raising


def download(url: str, output: Path | None = None, cookies: Path | None = None,
             *, dest_dir: Path | None = None,
             on_progress: OnProgress | None = None) -> Path:
    """Download url with yt-dlp. If output is given it names the final file
    (extension may be adjusted to match the merged container). cookies names a
    Netscape-format cookies.txt used for age-gated or members-only videos.
    dest_dir places a title-named download in that directory; on_progress is
    called as the download advances and may raise to abort it."""
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
    if dest_dir is not None:
        # "home" is what a bare outtmpl resolves against. yt-dlp strips path
        # separators out of the title itself, so the file lands in dest_dir and
        # nowhere else.
        opts["paths"] = {"home": str(dest_dir)}
    if runtimes := _js_runtimes():
        opts["js_runtimes"] = runtimes
    if cookies is not None:
        opts["cookiefile"] = str(cookies.expanduser())

    reporter: _Reporter | None = None
    if on_progress is not None:
        reporter = _Reporter(on_progress)
        opts["progress_hooks"] = [reporter.downloading]
        opts["postprocessor_hooks"] = [reporter.postprocessing]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.DownloadCancelled) as exc:
        # DownloadCancelled is not a DownloadError; both can carry an aborted
        # callback, depending on where yt-dlp was when the hook raised.
        if reporter is not None and reporter.raised is not None:
            _discard_partial(reporter.partial)
            raise reporter.raised from None
        raise DownloadError(str(exc)) from exc

    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    # Fallback for older yt-dlp info dicts.
    with yt_dlp.YoutubeDL(opts) as ydl:
        return Path(ydl.prepare_filename(info))
