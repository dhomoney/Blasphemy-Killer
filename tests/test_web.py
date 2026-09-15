from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blasphemy_killer.config import load_config
from blasphemy_killer.web.app import create_app
from blasphemy_killer.web.library import Library, PathDenied, resolve_within


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    (root / "shows").mkdir(parents=True)
    (root / "movie.mp4").write_bytes(b"not really a movie")
    (root / "shows" / "ep1.mkv").write_bytes(b"nor this")
    (root / "notes.txt").write_text("ignored: not a media extension")
    (root / ".hidden.mp4").write_bytes(b"hidden")
    return root


@pytest.fixture(autouse=True)
def isolated_config_dir(monkeypatch, tmp_path: Path) -> Path:
    """Keep uploaded cookies away from the real ~/.config, in both directions:
    tests must not write there, nor read a cookies.txt that is already there."""
    from blasphemy_killer import config as config_module
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    return config_dir


@pytest.fixture
def client(media_root: Path):
    app = create_app(media_root, load_config())
    with TestClient(app) as c:
        yield c


# --- path containment -------------------------------------------------------
#
# The server is unauthenticated by design, so refusing to step outside the
# media root is the only access control it has.


def test_resolve_within_allows_root_and_children(media_root: Path):
    assert resolve_within(media_root, None) == media_root.resolve()
    assert resolve_within(media_root, "") == media_root.resolve()
    assert resolve_within(media_root, "shows") == (media_root / "shows").resolve()
    assert resolve_within(media_root, "shows/ep1.mkv") == (media_root / "shows/ep1.mkv").resolve()


@pytest.mark.parametrize("candidate", [
    "..",
    "../..",
    "../secret.txt",
    "shows/../../secret.txt",
    "/etc/passwd",
    "/",
])
def test_resolve_within_rejects_escapes(media_root: Path, candidate: str):
    with pytest.raises(PathDenied):
        resolve_within(media_root, candidate)


def test_resolve_within_rejects_symlink_out_of_root(media_root: Path, tmp_path: Path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"should stay unreachable")
    (media_root / "sneaky.mp4").symlink_to(outside)
    with pytest.raises(PathDenied):
        resolve_within(media_root, "sneaky.mp4")


def test_browse_rejects_traversal(client: TestClient):
    assert client.get("/api/browse", params={"path": "../.."}).status_code == 400
    assert client.get("/api/browse", params={"path": "/etc"}).status_code == 400


def test_create_job_rejects_traversal(client: TestClient):
    res = client.post("/api/jobs", json={"path": "../../etc/passwd", "dry_run": True})
    assert res.status_code == 400


# --- browsing ---------------------------------------------------------------


def test_browse_root_lists_media_and_dirs(client: TestClient):
    body = client.get("/api/browse").json()
    assert body["path"] == ""
    assert body["parent"] is None
    names = {e["name"] for e in body["entries"]}
    assert names == {"shows", "movie.mp4"}          # .txt and dotfiles filtered out


def test_browse_subdirectory_reports_parent(client: TestClient):
    body = client.get("/api/browse", params={"path": "shows"}).json()
    assert body["path"] == "shows"
    assert body["parent"] == ""
    assert [e["name"] for e in body["entries"]] == ["ep1.mkv"]
    assert body["entries"][0]["path"] == "shows/ep1.mkv"


def test_browse_missing_directory_is_denied(client: TestClient):
    assert client.get("/api/browse", params={"path": "nope"}).status_code == 400


def test_directories_sort_before_files(media_root: Path):
    (media_root / "aaa.mp4").write_bytes(b"x")
    library = Library(media_root, load_config().extensions)
    _here, entries = library.list_dir("")
    assert entries[0].is_dir and entries[0].name == "shows"
    library.shutdown()


# --- jobs -------------------------------------------------------------------


def test_create_job_requires_a_file_not_a_directory(client: TestClient):
    res = client.post("/api/jobs", json={"path": "shows", "dry_run": True})
    assert res.status_code == 400
    assert "not a file" in res.json()["detail"]


def test_job_is_queued_and_listed(client: TestClient, monkeypatch):
    # Keep the worker from actually running ffmpeg on our fake media.
    from blasphemy_killer.web import jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "process", lambda *a, **k: None)

    created = client.post("/api/jobs", json={"path": "movie.mp4", "dry_run": True}).json()
    assert created["job"]["path"] == "movie.mp4"
    assert created["job"]["dry_run"] is True

    listed = client.get("/api/jobs").json()["jobs"]
    assert [j["path"] for j in listed] == ["movie.mp4"]


def test_same_path_is_not_queued_twice(media_root: Path):
    """Dedupe happens on submit, before the worker can pick anything up."""
    from blasphemy_killer.web.jobs import JobQueue

    events: list[dict] = []
    queue = JobQueue(media_root, load_config(), broadcast=events.append)
    queue.shutdown()          # stop the worker so jobs stay queued
    first = queue.submit("movie.mp4", dry_run=True, force=False)
    second = queue.submit("movie.mp4", dry_run=True, force=False)
    assert first.id == second.id
    assert len(queue.list()) == 1


def test_cancelling_a_queued_job_marks_it_cancelled(media_root: Path):
    from blasphemy_killer.web.jobs import CANCELLED, JobQueue

    queue = JobQueue(media_root, load_config(), broadcast=lambda _m: None)
    queue.shutdown()
    job = queue.submit("movie.mp4", dry_run=True, force=False)
    cancelled = queue.cancel(job.id)
    assert cancelled.state == CANCELLED


def test_cancel_unknown_job_is_404(client: TestClient):
    assert client.delete("/api/jobs/does-not-exist").status_code == 404


# --- streaming media for the player -----------------------------------------


def test_media_is_served_with_range_support(client: TestClient, media_root: Path):
    """Scrubbing is the browser seeking, which means Range requests. Without
    these headers the player can only play from the start."""
    res = client.get("/api/media", params={"path": "movie.mp4"})
    assert res.status_code == 200
    assert res.headers["accept-ranges"] == "bytes"
    assert res.headers["content-type"] == "video/mp4"
    assert res.content == (media_root / "movie.mp4").read_bytes()


def test_media_serves_a_partial_range(client: TestClient):
    res = client.get("/api/media", params={"path": "movie.mp4"}, headers={"Range": "bytes=4-9"})
    assert res.status_code == 206
    assert res.content == b"really"
    assert res.headers["content-range"] == "bytes 4-9/18"


def test_media_rejects_traversal(client: TestClient):
    assert client.get("/api/media", params={"path": "../../etc/passwd"}).status_code == 400


def test_media_refuses_non_media_files(client: TestClient):
    """The media root can hold anything; only what the library lists is served."""
    assert client.get("/api/media", params={"path": "notes.txt"}).status_code == 400


def test_media_missing_file_is_404(client: TestClient):
    assert client.get("/api/media", params={"path": "gone.mp4"}).status_code == 404


# --- downloads --------------------------------------------------------------


@pytest.fixture
def fake_download(monkeypatch, media_root: Path):
    """Stand in for yt-dlp: drop a file in the destination and report it."""
    from blasphemy_killer.web import jobs as jobs_mod

    calls: list[dict] = []

    def _download(url, *, cookies=None, dest_dir=None, on_progress=None):
        calls.append({"url": url, "cookies": cookies, "dest_dir": dest_dir})
        if on_progress:
            on_progress("downloading", 0.5)
        landed = Path(dest_dir) / "Clip [abc123].mp4"
        landed.write_bytes(b"downloaded")
        return landed

    monkeypatch.setattr(jobs_mod, "download", _download)
    return calls


def _await_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    """Poll until the worker has finished with a job."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for job in client.get("/api/jobs").json()["jobs"]:
            if job["id"] == job_id and job["state"] in ("done", "failed", "cancelled"):
                return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def test_download_lands_in_the_browsed_directory(client: TestClient, fake_download,
                                                 media_root: Path):
    created = client.post("/api/downloads", json={"url": "https://example.com/v", "dest": "shows"})
    assert created.status_code == 200
    job = _await_job(client, created.json()["job"]["id"])

    assert job["state"] == "done"
    assert job["kind"] == "download"
    assert job["path"] == "shows/Clip [abc123].mp4"      # relative, for the player
    assert fake_download[0]["dest_dir"] == media_root / "shows"


def test_download_rejects_non_http_urls(client: TestClient):
    for url in ("file:///etc/passwd", "ftp://example.com/v", "/etc/passwd", ""):
        res = client.post("/api/downloads", json={"url": url})
        assert res.status_code == 400, url


def test_download_rejects_destination_outside_root(client: TestClient):
    res = client.post("/api/downloads", json={"url": "https://example.com/v", "dest": "../.."})
    assert res.status_code == 400


def test_download_rejects_a_file_as_destination(client: TestClient):
    res = client.post("/api/downloads", json={"url": "https://example.com/v", "dest": "movie.mp4"})
    assert res.status_code == 400


def test_download_landing_outside_the_root_fails_the_job(client: TestClient, monkeypatch,
                                                         tmp_path: Path):
    """A path traversal in the title would be a yt-dlp bug, not ours -- but the
    job still refuses to hand the player something outside the root."""
    from blasphemy_killer.web import jobs as jobs_mod

    escapee = tmp_path / "escaped.mp4"
    escapee.write_bytes(b"x")
    monkeypatch.setattr(jobs_mod, "download", lambda *a, **k: escapee)

    created = client.post("/api/downloads", json={"url": "https://example.com/v"})
    job = _await_job(client, created.json()["job"]["id"])
    assert job["state"] == "failed"
    assert "outside the media root" in job["error"]


def test_download_failure_is_reported_on_the_job(client: TestClient, monkeypatch):
    from blasphemy_killer.download import DownloadError
    from blasphemy_killer.web import jobs as jobs_mod

    def _boom(*_a, **_k):
        raise DownloadError("Video unavailable")

    monkeypatch.setattr(jobs_mod, "download", _boom)
    created = client.post("/api/downloads", json={"url": "https://example.com/v"})
    job = _await_job(client, created.json()["job"]["id"])
    assert job["state"] == "failed"
    assert job["error"] == "Video unavailable"


def test_same_url_is_not_downloaded_twice(media_root: Path):
    from blasphemy_killer.web.jobs import JobQueue

    queue = JobQueue(media_root, load_config(), broadcast=lambda _m: None)
    queue.shutdown()          # stop the worker so jobs stay queued
    first = queue.submit_download("https://example.com/v", "")
    second = queue.submit_download("https://example.com/v", "")
    assert first.id == second.id
    assert len(queue.list()) == 1


def test_download_body_cannot_name_a_cookies_path(client: TestClient, fake_download,
                                                  media_root: Path):
    """Cookies are uploaded, never named: a path in the request body would let
    an unauthenticated page read any file on the host, so it is ignored."""
    created = client.post(
        "/api/downloads",
        json={"url": "https://example.com/v", "cookies": "/etc/passwd"},
    )
    _await_job(client, created.json()["job"]["id"])
    assert fake_download[0]["cookies"] == load_config().cookies


# --- misc -------------------------------------------------------------------


def test_config_endpoint_reports_root_and_model(client: TestClient, media_root: Path):
    body = client.get("/api/config").json()
    assert body["root"] == str(media_root)
    assert body["model"] == load_config().model
    assert body["phrases"] > 0


def test_index_and_static_assets_are_served(client: TestClient):
    assert "<title>blasphemy-killer</title>" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_no_external_asset_references(client: TestClient):
    """The container may run with --network none; nothing may come from a CDN."""
    for path in ("/", "/static/app.js", "/static/style.css"):
        body = client.get(path).text
        assert "http://" not in body.replace("http://{host}", "")
        assert "https://" not in body


def test_cancelling_a_running_job_flags_the_request_immediately(media_root: Path, monkeypatch):
    """Cancel only lands at the next checkpoint, which can be seconds into a
    whisper segment. The flag is what keeps the UI from looking stuck."""
    import threading

    from blasphemy_killer.web import jobs as jobs_mod
    from blasphemy_killer.web.jobs import RUNNING, JobQueue

    running = threading.Event()
    release = threading.Event()

    def slow_process(*_a, **kwargs):
        running.set()
        release.wait(timeout=5)
        kwargs["check_cancelled"]()      # the checkpoint

    monkeypatch.setattr(jobs_mod, "process", slow_process)

    queue = JobQueue(media_root, load_config(), broadcast=lambda _m: None)
    job = queue.submit("movie.mp4", dry_run=True, force=False)
    assert running.wait(timeout=5)

    queue.cancel(job.id)
    assert job.state == RUNNING          # not stopped yet...
    assert job.cancel_requested is True  # ...but the UI can say "cancelling"

    release.set()
    for _ in range(50):
        if job.state != RUNNING:
            break
        threading.Event().wait(0.1)
    assert job.state == "cancelled"
    queue.shutdown()


# --- cookies ----------------------------------------------------------------
#
# The file grants access to whatever accounts the uploader is logged in to, so
# it is written 0600, never read back out, and only ever lands on one path.

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\ttop-secret-value\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1790000000\tHSID\tanother-secret\n"
)


def _put_cookies(client: TestClient, body, **kwargs):
    return client.put("/api/cookies", content=body,
                      headers={"Content-Type": "text/plain"}, **kwargs)


def test_uploaded_cookies_are_stored_private_to_the_user(client: TestClient,
                                                         isolated_config_dir: Path):
    body = _put_cookies(client, NETSCAPE).json()
    assert body == {
        "present": True, "uploaded": True,
        "path": str(isolated_config_dir / "cookies.txt"),
        "count": 2, "size": len(NETSCAPE), "mtime": body["mtime"],
    }
    stored = isolated_config_dir / "cookies.txt"
    assert stored.read_text() == NETSCAPE
    assert stored.stat().st_mode & 0o777 == 0o600      # not group- or world-readable


def test_cookies_are_never_served_back(client: TestClient):
    """Status says whether cookies are there, never what they are."""
    _put_cookies(client, NETSCAPE)
    for res in (client.get("/api/cookies"), client.get("/api/config")):
        assert "top-secret-value" not in res.text
        assert "another-secret" not in res.text


def test_a_json_export_is_rejected_with_the_reason(client: TestClient):
    """The common mistake. Catching it here beats a yt-dlp failure three
    minutes into a download."""
    res = _put_cookies(client, '[{"name": "SID", "value": "x"}]')
    assert res.status_code == 400
    assert "Netscape" in res.json()["detail"]


@pytest.mark.parametrize("body, reason", [
    (b"", "empty"),
    (b"   \n\n", "empty"),
    (b"\x00\x01\x02\xff\xfe", "not a text file"),
    ("# Netscape HTTP Cookie File\n# nothing but comments\n", "no cookie lines"),
    ("just some prose about cookies\n", "no cookie lines"),
])
def test_files_that_are_not_cookies_are_rejected(client: TestClient, body, reason: str):
    res = _put_cookies(client, body)
    assert res.status_code == 400
    assert reason in res.json()["detail"]


def test_an_absurdly_large_upload_is_refused_before_it_is_read(client: TestClient):
    from blasphemy_killer.web.cookies import MAX_BYTES
    res = client.put(
        "/api/cookies", content=b"x",
        headers={"Content-Type": "text/plain", "Content-Length": str(MAX_BYTES + 1)},
    )
    assert res.status_code == 413


def test_httponly_cookies_are_real_cookies_not_comments(client: TestClient):
    """#HttpOnly_ lines start with '#' but are cookies; a file of nothing else
    is still valid."""
    body = "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tabc\n"
    res = _put_cookies(client, body)
    assert res.status_code == 200
    assert res.json()["count"] == 1


def test_uploading_again_replaces_the_previous_file(client: TestClient,
                                                    isolated_config_dir: Path):
    _put_cookies(client, NETSCAPE)
    shorter = "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSID\tabc\n"
    assert _put_cookies(client, shorter).json()["count"] == 1
    assert (isolated_config_dir / "cookies.txt").read_text() == shorter
    assert not list(isolated_config_dir.glob(".cookies.txt.new"))   # no temp left behind


def test_a_rejected_upload_leaves_the_working_file_alone(client: TestClient,
                                                         isolated_config_dir: Path):
    _put_cookies(client, NETSCAPE)
    assert _put_cookies(client, "rubbish\n").status_code == 400
    assert (isolated_config_dir / "cookies.txt").read_text() == NETSCAPE


def test_removing_cookies_deletes_the_file(client: TestClient, isolated_config_dir: Path):
    _put_cookies(client, NETSCAPE)
    body = client.delete("/api/cookies").json()
    assert body["present"] is False
    assert not (isolated_config_dir / "cookies.txt").exists()


def test_removing_when_there_are_none_is_not_an_error(client: TestClient):
    assert client.delete("/api/cookies").status_code == 200


def test_cookies_from_config_are_reported_but_not_ours_to_delete(media_root: Path,
                                                                 tmp_path: Path):
    """A path set in config.toml still shows up, flagged so the UI does not
    offer to remove a file it did not put there."""
    from blasphemy_killer.web.cookies import status

    configured = tmp_path / "their-cookies.txt"
    configured.write_text(NETSCAPE)
    cfg = load_config()
    cfg.cookies = configured

    reported = status(cfg)
    assert reported["present"] is True
    assert reported["uploaded"] is False
    assert reported["path"] == str(configured)


def test_an_upload_wins_over_the_config_setting(client: TestClient, tmp_path: Path,
                                                isolated_config_dir: Path):
    from blasphemy_killer.web.cookies import effective

    configured = tmp_path / "their-cookies.txt"
    configured.write_text(NETSCAPE)
    cfg = load_config()
    cfg.cookies = configured
    assert effective(cfg) == configured

    _put_cookies(client, NETSCAPE)
    assert effective(cfg) == isolated_config_dir / "cookies.txt"


def test_downloads_pick_up_the_uploaded_cookies(client: TestClient, fake_download,
                                                isolated_config_dir: Path):
    """Resolved per download, not once at startup -- uploading cookies has to
    affect the next download without a restart."""
    _put_cookies(client, NETSCAPE)
    created = client.post("/api/downloads", json={"url": "https://example.com/v"})
    _await_job(client, created.json()["job"]["id"])
    assert fake_download[0]["cookies"] == isolated_config_dir / "cookies.txt"


def test_removing_cookies_takes_effect_for_the_next_download(client: TestClient,
                                                             fake_download):
    _put_cookies(client, NETSCAPE)
    client.delete("/api/cookies")
    created = client.post("/api/downloads", json={"url": "https://example.com/v"})
    _await_job(client, created.json()["job"]["id"])
    assert fake_download[0]["cookies"] == load_config().cookies
