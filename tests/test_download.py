from pathlib import Path

import pytest

from blasphemy_killer import download as download_mod


class _FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL that records the opts it was built with."""

    seen: list[dict] = []

    def __init__(self, opts):
        type(self).seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        return {"requested_downloads": [{"filepath": "/tmp/video.mp4"}]}


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
