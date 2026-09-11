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
