from pathlib import Path

import pytest

from blasphemy_killer import download as download_mod


class _FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL that records the opts it was built with.

    Downloading fires whatever hooks were registered, the way yt-dlp does, so
    progress reporting and cancellation can be tested without a network.
    """

    seen: list[dict] = []
    filepath = "/tmp/video.mp4"

    def __init__(self, opts):
        type(self).seen.append(opts)
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        for hook in self.opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": 200})
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200})
            hook({"status": "finished", "downloaded_bytes": 200, "total_bytes": 200})
        for hook in self.opts.get("postprocessor_hooks", []):
            hook({"status": "started", "postprocessor": "Merger"})
        return {"requested_downloads": [{"filepath": type(self).filepath}]}


@pytest.fixture
def fake_yt_dlp(monkeypatch):
    yt_dlp = pytest.importorskip("yt_dlp")
    _FakeYDL.seen = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    return _FakeYDL


def test_no_cookies_leaves_option_out(fake_yt_dlp):
    download_mod.download("https://example.com/v")
    assert "cookiefile" not in fake_yt_dlp.seen[0]


def test_cookies_passed_to_yt_dlp(fake_yt_dlp, tmp_path: Path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    download_mod.download("https://example.com/v", cookies=cookies)
    assert fake_yt_dlp.seen[0]["cookiefile"] == str(cookies)


def test_cookies_path_is_expanded(fake_yt_dlp):
    download_mod.download("https://example.com/v", cookies=Path("~/cookies.txt"))
    assert fake_yt_dlp.seen[0]["cookiefile"] == str(Path.home() / "cookies.txt")


# --- destination ------------------------------------------------------------


def test_dest_dir_becomes_the_download_home(fake_yt_dlp, tmp_path: Path):
    download_mod.download("https://example.com/v", dest_dir=tmp_path)
    opts = fake_yt_dlp.seen[0]
    assert opts["paths"] == {"home": str(tmp_path)}
    # The template stays a bare filename, so nothing can climb out of dest_dir.
    assert "/" not in opts["outtmpl"]


def test_output_still_wins_over_dest_dir(fake_yt_dlp, tmp_path: Path):
    """-o names the file outright; the CLI has always worked that way."""
    download_mod.download("https://example.com/v", tmp_path / "clean.mp4")
    assert fake_yt_dlp.seen[0]["outtmpl"] == str(tmp_path / "clean") + ".%(ext)s"


# --- progress and cancellation ---------------------------------------------


def test_progress_is_reported_as_stages_and_fractions(fake_yt_dlp):
    seen: list[tuple[str, float | None]] = []
    download_mod.download("https://example.com/v", on_progress=lambda s, f: seen.append((s, f)))
    assert seen == [
        ("downloading", 0.0),
        ("downloading", 0.25),
        ("downloading", 1.0),
        ("merging", None),
    ]


def test_unknown_total_reports_no_fraction(fake_yt_dlp, monkeypatch):
    """A stream with no Content-Length still reports the stage, just no number."""
    import yt_dlp

    class _NoSize(_FakeYDL):
        def extract_info(self, url, download):
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 17})
            return {"requested_downloads": [{"filepath": "/tmp/video.mp4"}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _NoSize)
    seen: list[tuple[str, float | None]] = []
    download_mod.download("https://example.com/v", on_progress=lambda s, f: seen.append((s, f)))
    assert seen == [("downloading", None)]


def test_an_underestimated_size_never_reports_past_100_percent(fake_yt_dlp, monkeypatch):
    """total_bytes_estimate is a guess. When it guesses low, downloaded/total
    goes over 1.0 and the UI renders a 334% progress bar."""
    import yt_dlp

    class _BadEstimate(_FakeYDL):
        def extract_info(self, url, download):
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "downloaded_bytes": 30,
                      "total_bytes_estimate": 100})
                hook({"status": "downloading", "downloaded_bytes": 334,
                      "total_bytes_estimate": 100})
            return {"requested_downloads": [{"filepath": "/tmp/video.mp4"}]}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _BadEstimate)
    seen: list[tuple[str, float | None]] = []
    download_mod.download("https://example.com/v", on_progress=lambda s, f: seen.append((s, f)))
    assert seen == [("downloading", 0.3), ("downloading", 1.0)]


def test_progress_callback_exception_aborts_and_propagates(fake_yt_dlp):
    """Raising from the callback is how the web UI cancels a running download.
    It has to come back out as itself, not as a yt-dlp error."""

    class Stop(Exception):
        pass

    def on_progress(_stage, _fraction):
        raise Stop("cancelled")

    with pytest.raises(Stop):
        download_mod.download("https://example.com/v", on_progress=on_progress)


def test_aborting_discards_the_half_written_part_file(fake_yt_dlp, monkeypatch,
                                                      tmp_path: Path):
    """A cancelled download must not leave bytes behind: .part is not a media
    extension, so the library would never list what it left in the folder."""
    import yt_dlp

    partial = tmp_path / "video.mp4.part"
    partial.write_bytes(b"half a video")
    keep = tmp_path / "unrelated.mp4"
    keep.write_bytes(b"someone else's file")

    class _Partial(_FakeYDL):
        def extract_info(self, url, download):
            for hook in self.opts.get("progress_hooks", []):
                hook({
                    "status": "downloading", "downloaded_bytes": 1,
                    "total_bytes": 100, "tmpfilename": str(partial),
                })
            raise AssertionError("the hook should have aborted the download")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _Partial)

    class Stop(Exception):
        pass

    def on_progress(_stage, _fraction):
        raise Stop()

    with pytest.raises(Stop):
        download_mod.download("https://example.com/v", dest_dir=tmp_path,
                              on_progress=on_progress)
    assert not partial.exists()
    assert keep.exists()          # nothing else in the folder is touched


def test_a_completed_download_keeps_its_file(fake_yt_dlp, tmp_path: Path):
    """Only an abort discards anything; a normal finish must not."""
    landed = tmp_path / "video.mp4"
    landed.write_bytes(b"a whole video")
    fake_yt_dlp.filepath = str(landed)
    try:
        assert download_mod.download("https://example.com/v",
                                     on_progress=lambda *_a: None) == landed
        assert landed.exists()
    finally:
        fake_yt_dlp.filepath = "/tmp/video.mp4"


def test_no_progress_callback_registers_no_hooks(fake_yt_dlp):
    download_mod.download("https://example.com/v")
    assert "progress_hooks" not in fake_yt_dlp.seen[0]
    assert "postprocessor_hooks" not in fake_yt_dlp.seen[0]
