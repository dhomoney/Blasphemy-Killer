"""FastAPI application: browse the media root, queue jobs, stream progress."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import WHISPER_MODELS, Config, load_config
from ..download import is_url
from . import cookies as cookies_store
from .jobs import JobQueue
from .library import Library, PathDenied, resolve_within

STATIC_DIR = Path(__file__).parent / "static"


# Everything the page loads comes from this origin: one script, one stylesheet,
# fetch()/EventSource to /api, and <video>/<audio> on /api/media. No inline
# script or style, so neither needs 'unsafe-inline'. frame-ancestors is what
# stops clickjacking (X-Frame-Options is sent too for old browsers).
CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "media-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def cross_site(request: Request) -> bool:
    """True when a browser says this request was triggered by another site.

    The UI has no login, so any page the user visits could otherwise make their
    browser POST to the server (queue a clean, replace the cookies file). A
    browser always labels such a request: Sec-Fetch-Site says where it came
    from, and Origin names the page. Requests carrying neither (curl, scripts)
    are not browser-driven and pass.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is not None:
        # "null" (sandboxed iframe, file://) has no netloc and never matches.
        return urlsplit(origin).netloc != request.headers.get("host", "")
    return False


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
        try:
            loop.call_soon_threadsafe(self._fanout, payload)
        except RuntimeError:
            # The loop has already closed: the server is shutting down while
            # the worker is still finishing a job. Nobody is listening, and an
            # exception here would take the worker thread down with it.
            pass

    def _fanout(self, payload: str) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A browser that cannot keep up loses events rather than
                # stalling the worker; it re-syncs via GET /api/jobs.
                pass


class Tuning(BaseModel):
    """Per-job transcription overrides; an unset field keeps the configured value."""
    center_channel: bool | None = None
    vad_filter: bool | None = None
    vad_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    def overrides(self) -> dict | None:
        return self.model_dump(exclude_none=True) or None


class JobRequest(BaseModel):
    path: str
    dry_run: bool = True
    force: bool = False
    model: str | None = None  # whisper model override; None = configured default
    tuning: Tuning | None = None


class DownloadRequest(BaseModel):
    url: str
    dest: str = ""       # directory to download into, relative to the media root
    clean: bool = True   # queue the clean as soon as the file lands
    model: str | None = None  # whisper model for that clean; None = default
    tuning: Tuning | None = None


def check_model(model: str | None) -> str | None:
    """The model name is handed straight to faster-whisper, which treats an
    unknown name as a Hugging Face repo id -- so only the offered list is
    accepted, never an arbitrary caller-supplied string."""
    if model is not None and model not in WHISPER_MODELS:
        raise HTTPException(status_code=400, detail="unknown whisper model")
    return model


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
        # After the hub is bound, so a restored job's first event reaches any
        # browser that is already watching.
        jobs.restore_pending()
        yield
        jobs.shutdown()
        library.shutdown()

    app = FastAPI(title="blasphemy-killer", version=__version__, lifespan=lifespan)
    app.state.root = root
    app.state.jobs = jobs
    app.state.library = library

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        # The HTML and its assets change together on every release but keep the
        # same URLs; heuristic browser caching can otherwise pair the new page
        # with a stale app.js (leaving e.g. the model dropdown empty). no-cache
        # still allows 304s off the etag, so unchanged assets cost one round
        # trip, not a re-download.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def block_cross_site_writes(request: Request, call_next):
        if request.method not in SAFE_METHODS and cross_site(request):
            return JSONResponse(
                {"detail": "cross-site request refused"}, status_code=403,
                headers=SECURITY_HEADERS,
            )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/api/config")
    def get_config() -> dict:
        return {
            "version": __version__,
            "root": str(root),
            "model": cfg.model,
            "models": WHISPER_MODELS,
            "center_channel": cfg.center_channel,
            "vad_filter": cfg.vad_filter,
            "vad_threshold": cfg.vad_threshold,
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
        up = PurePosixPath(here).parent.as_posix()
        parent = None if not here else up if up != "." else ""
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
        job = jobs.submit(req.path, dry_run=req.dry_run, force=req.force,
                          model=check_model(req.model),
                          tuning=req.tuning.overrides() if req.tuning else None)
        return {"job": job.snapshot()}

    @app.post("/api/downloads")
    def create_download(req: DownloadRequest) -> dict:
        """Queue a yt-dlp download into the media root, and, unless the caller
        opts out, the clean that follows it.

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
        job = jobs.submit_download(url, library.rel(dest), clean=req.clean,
                                   model=check_model(req.model),
                                   tuning=req.tuning.overrides() if req.tuning else None)
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
