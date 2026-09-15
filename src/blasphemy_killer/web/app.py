"""FastAPI application: browse the media root, queue jobs, stream progress."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..config import Config, load_config
from ..download import is_url
from . import cookies as cookies_store
from .jobs import JobQueue
from .library import Library, PathDenied, resolve_within

STATIC_DIR = Path(__file__).parent / "static"


class Hub:
    """Fan-out of server events to connected browsers.

    The worker runs on a plain thread, so everything it publishes has to cross
    into the event loop via call_soon_threadsafe.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict) -> None:
        """Safe to call from any thread."""
        payload = json.dumps(message)
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._fanout, payload)

    def _fanout(self, payload: str) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A browser that cannot keep up loses events rather than
                # stalling the worker; it re-syncs via GET /api/jobs.
                pass


class JobRequest(BaseModel):
    path: str
    dry_run: bool = True
    force: bool = False


class DownloadRequest(BaseModel):
    url: str
    dest: str = ""       # directory to download into, relative to the media root


def media_type(path: Path) -> str:
    """Content-Type for playback. A container the browser cannot decode will
    still be served -- it just will not play -- so a guess is good enough."""
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def create_app(root: Path, cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    hub = Hub()
    library = Library(root, cfg.extensions)
    jobs = JobQueue(
        root, cfg, broadcast=hub.publish,
        on_file_changed=library.invalidate,
        resolve_cookies=lambda: cookies_store.effective(cfg),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind(asyncio.get_running_loop())
        yield
        jobs.shutdown()
        library.shutdown()

    app = FastAPI(title="blasphemy-killer", version=__version__, lifespan=lifespan)
    app.state.root = root
    app.state.jobs = jobs
    app.state.library = library

    @app.get("/api/config")
    def get_config() -> dict:
        return {
            "version": __version__,
            "root": str(root),
            "model": cfg.model,
            "phrases": len(cfg.phrases),
            "keep_backup": cfg.keep_backup,
        }

    @app.get("/api/browse")
    def browse(path: str | None = None) -> dict:
        try:
            here, entries = library.list_dir(path)
        except PathDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Fill in "already cleaned" out of band; the browser gets each result
        # over SSE as it lands.
        library.scan(
            entries,
            lambda rel, cleaned: hub.publish(
                {"type": "scan", "path": rel, "cleaned": cleaned}
            ),
        )
        parent = None if not here else str(Path(here).parent) if str(Path(here).parent) != "." else ""
        return {"path": here, "parent": parent, "entries": [asdict(e) for e in entries]}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": jobs.list()}

    @app.post("/api/jobs")
    def create_job(req: JobRequest) -> dict:
        try:
            target = resolve_within(library.root, req.path)
        except PathDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            raise HTTPException(status_code=400, detail="not a file")
        job = jobs.submit(req.path, dry_run=req.dry_run, force=req.force)
        return {"job": job.snapshot()}

    @app.post("/api/downloads")
    def create_download(req: DownloadRequest) -> dict:
        """Queue a yt-dlp download into the media root.

        Only the URL comes from the browser; the cookies file is whatever the
        config names, never a path the caller can choose -- that would turn an
        unauthenticated UI into a file-read primitive.
        """
        url = req.url.strip()
        if not is_url(url):
            raise HTTPException(status_code=400, detail="only http(s) URLs can be downloaded")
        try:
            dest = resolve_within(library.root, req.dest)
        except PathDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not dest.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")
        job = jobs.submit_download(url, library.rel(dest))
        return {"job": job.snapshot()}

    @app.get("/api/cookies")
    def cookies_status() -> dict:
        return cookies_store.status(cfg)

    @app.put("/api/cookies")
    async def upload_cookies(request: Request) -> dict:
        """Store a cookies.txt sent as the raw request body.

        The browser reads the file and posts its text, so no filename or path
        from the caller is ever involved: it lands on one fixed path in the
        config directory or it does not land at all.
        """
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cookies_store.MAX_BYTES:
            raise HTTPException(status_code=413, detail="that file is far too large")
        try:
            cookies_store.save(await request.body())
        except cookies_store.CookieError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not write the cookies file: {exc}"
            ) from exc
        return cookies_store.status(cfg)

    @app.delete("/api/cookies")
    def delete_cookies() -> dict:
        cookies_store.remove()
        return cookies_store.status(cfg)

    @app.get("/api/media")
    def media(path: str) -> FileResponse:
        """Stream one media file for the player. FileResponse honours Range
        requests, which is what lets the browser seek instead of downloading
        the whole file before it can scrub."""
        try:
            target = resolve_within(library.root, path)
        except PathDenied as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        if target.suffix.lower() not in cfg.extensions:
            raise HTTPException(status_code=400, detail="not a media file")
        return FileResponse(target, media_type=media_type(target))

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict:
        job = jobs.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return {"job": job.snapshot()}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        q = hub.subscribe()

        async def stream():
            try:
                # Anything already queued, so a late browser is not blind.
                yield f"data: {json.dumps({'type': 'sync', 'jobs': jobs.list()})}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"   # keeps proxies from closing it
                        continue
                    yield f"data: {payload}\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
