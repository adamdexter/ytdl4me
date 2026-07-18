"""FastAPI app: routes, static serving, auth middleware, job cleanup task."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import shutil
import socket
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from yt_dlp.version import __version__ as YT_DLP_VERSION

from . import downloader
from .downloader import (
    ALL_OPTION_IDS,
    VIDEO_OPTION_IDS,
    PLAYLIST_ERROR,
    PlaylistError,
    ProbeError,
    DownloadFailed,
)
from .jobs import Job, JobStore
from .platforms import detect_platform, looks_like_playlist, platform_kind
from .spotify import SpotifyError, resolve_track

_ROOT = Path(__file__).resolve().parent.parent

ACCESS_KEY = os.environ.get("ACCESS_KEY") or None
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR") or _ROOT / "downloads")
FILE_TTL_MINUTES = int(os.environ.get("FILE_TTL_MINUTES") or 60)
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS") or 3)
ALLOW_ANY_SITE = (os.environ.get("ALLOW_ANY_SITE") or "false").strip().lower() in (
    "1", "true", "yes", "on",
)
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE") or 30)
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS") or MAX_CONCURRENT_JOBS * 4)

_STATIC_DIR = _ROOT / "static"
_PROBE_TIMEOUT = 45
_PROBE_WORKERS = 4
_CLEANUP_INTERVAL = 300  # 5 min
_SERVED_GRACE = 300  # delete job dir 5 min after first successful serve

UNSUPPORTED_SITE_ERROR = (
    "Only YouTube, Vimeo, SoundCloud and Spotify links are supported."
)
INVALID_URL_ERROR = "That doesn't look like a valid link — paste a full http(s) URL."

store = JobStore()
_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
# Keep strong references to job tasks: the event loop only holds weak refs,
# so an unreferenced task can be garbage-collected mid-execution.
_job_tasks: set[asyncio.Task] = set()
# Dedicated bounded pools so yt-dlp work never saturates the loop's default
# executor: a timed-out probe thread keeps running (threads aren't cancelable)
# and must not starve downloads or unrelated to_thread work.
_probe_executor = ThreadPoolExecutor(max_workers=_PROBE_WORKERS, thread_name_prefix="probe")
_download_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="download")
# Bounds probes queued behind the probe pool; excess requests get a 429.
_probe_slots = asyncio.Semaphore(_PROBE_WORKERS * 2)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not ACCESS_KEY:
        logging.getLogger("uvicorn.error").warning(
            "ACCESS_KEY is not set — the API is open to anyone who can reach "
            "this server. Set ACCESS_KEY before exposing it publicly."
        )
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cleanup = asyncio.create_task(_cleanup_loop())
    yield
    cleanup.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup
    _probe_executor.shutdown(wait=False, cancel_futures=True)
    _download_executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="ytdl4me", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Errors & auth
# ---------------------------------------------------------------------------

def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return _error(exc.status_code, detail)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    return _error(422, "Invalid request body.")


@app.middleware("http")
async def _access_key_middleware(request: Request, call_next):
    path = request.url.path
    if ACCESS_KEY and path.startswith("/api/") and path != "/api/health":
        supplied = (
            request.headers.get("x-access-key")
            or request.query_params.get("key")
            or ""
        )
        if not secrets.compare_digest(supplied.encode(), ACCESS_KEY.encode()):
            return _error(401, "Invalid or missing access key.")
    return await call_next(request)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class ProbeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    option_id: str


@app.get("/api/health")
async def api_health():
    return {"status": "ok", "yt_dlp_version": YT_DLP_VERSION}


# Per-IP sliding-window rate limit for the expensive endpoints (probe/download).
_rate_buckets: dict[str, deque] = {}


def _rate_limited(request: Request) -> bool:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return False
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    if len(_rate_buckets) > 1024:  # shed stale per-IP buckets
        for stale in [k for k, b in _rate_buckets.items() if not b or now - b[-1] > 60]:
            del _rate_buckets[stale]
    bucket = _rate_buckets.setdefault(ip, deque())
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        return True
    bucket.append(now)
    return False


RATE_LIMIT_ERROR = "Too many requests — please slow down and try again shortly."


def _ip_is_public(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%")[0]).is_global
    except ValueError:
        return False


async def _host_is_public(url: str) -> bool:
    """SSRF guard for ALLOW_ANY_SITE: every address the host resolves to must
    be public (no loopback/private/link-local/metadata ranges)."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a name — resolve it below
    else:
        return _ip_is_public(host)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    return bool(infos) and all(_ip_is_public(info[4][0]) for info in infos)


async def _validate_url(url: str) -> str | JSONResponse:
    platform = detect_platform(url)
    if platform is None:
        return _error(422, INVALID_URL_ERROR)
    if platform == "other":
        if not ALLOW_ANY_SITE:
            return _error(422, UNSUPPORTED_SITE_ERROR)
        if not await _host_is_public(url):
            return _error(422, "That link points at a private or unreachable address.")
    if looks_like_playlist(url, platform):
        return _error(422, PLAYLIST_ERROR)
    return platform


@app.post("/api/probe")
async def api_probe(request: Request, body: ProbeRequest):
    if _rate_limited(request):
        return _error(429, RATE_LIMIT_ERROR)
    url = body.url.strip()
    platform = await _validate_url(url)
    if isinstance(platform, JSONResponse):
        return platform
    if _probe_slots.locked():
        return _error(429, "The server is busy reading other links — try again in a moment.")
    try:
        async with _probe_slots:
            result = await asyncio.wait_for(_probe(url, platform), _PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return _error(422, "Timed out reading that link — please try again.")
    except (ProbeError, SpotifyError) as exc:
        return _error(422, str(exc))
    return result


async def _probe(url: str, platform: str) -> dict:
    loop = asyncio.get_running_loop()
    if platform == "spotify":
        track = await resolve_track(url)
        info = await loop.run_in_executor(
            _probe_executor, downloader.probe,
            f"ytsearch1:{track['search_query']}", "spotify",
        )
        # Report Spotify's own metadata and the original Spotify URL.
        info["url"] = url
        for src, dst in (("title", "title"), ("artist", "uploader"),
                         ("thumbnail", "thumbnail"), ("duration", "duration")):
            if track.get(src):
                info[dst] = track[src]
        return info
    return await loop.run_in_executor(_probe_executor, downloader.probe, url, platform)


@app.post("/api/download", status_code=202)
async def api_download(request: Request, body: DownloadRequest):
    if _rate_limited(request):
        return _error(429, RATE_LIMIT_ERROR)
    url = body.url.strip()
    option_id = body.option_id
    platform = await _validate_url(url)
    if isinstance(platform, JSONResponse):
        return platform
    if option_id not in ALL_OPTION_IDS:
        return _error(422, f"Unknown option '{option_id}'.")
    if platform != "other" and platform_kind(platform) == "audio" and option_id in VIDEO_OPTION_IDS:
        return _error(422, "That's an audio-only source — pick an audio option.")
    active = sum(1 for j in store.all() if j.status not in ("done", "error"))
    if active >= MAX_ACTIVE_JOBS:
        return _error(429, "Too many downloads in flight — try again once some finish.")

    job_id = uuid.uuid4().hex
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    store.add(Job(id=job_id, url=url, option_id=option_id,
                  platform=platform, dir=str(job_dir)))
    task = asyncio.create_task(_run_job(job_id, url, option_id, platform, str(job_dir)))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return {"job_id": job_id}


async def _run_job(job_id: str, url: str, option_id: str,
                   platform: str, job_dir: str) -> None:
    try:
        target_url = url
        filename_stem = None
        tags = None
        if platform == "spotify":
            track = await resolve_track(url)
            target_url = f"ytsearch1:{track['search_query']}"
            artist, title = track.get("artist"), track.get("title")
            filename_stem = (
                f"{artist} - {title} [spotify]" if artist else f"{title} [spotify]"
            )
            tags = {"artist": artist, "title": title}
        async with _job_semaphore:
            if store.get(job_id) is None:
                return
            await asyncio.get_running_loop().run_in_executor(
                _download_executor, downloader.run_download, store, job_id,
                target_url, option_id, job_dir, filename_stem, tags,
            )
    except (DownloadFailed, PlaylistError, ProbeError, SpotifyError) as exc:
        store.update(job_id, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — job must never crash silently
        store.update(job_id, status="error", error=downloader.friendly_error(exc))


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    snapshot = store.snapshot(job_id)
    if snapshot is None:
        return _error(404, "Unknown job.")
    return snapshot


@app.get("/api/jobs/{job_id}/file")
async def api_job_file(job_id: str):
    job = store.get(job_id)
    if job is None:
        return _error(404, "Unknown job.")
    if job.status != "done" or not job.filepath:
        return _error(409, "The file isn't ready yet.")
    # Serve exactly the file recorded on the job, and only if it really lives
    # inside this job's own directory (path-traversal guard).
    real_path = os.path.realpath(job.filepath)
    real_dir = os.path.realpath(job.dir)
    try:
        contained = os.path.commonpath([real_path, real_dir]) == real_dir
    except ValueError:
        contained = False
    if not contained or not os.path.isfile(real_path):
        return _error(410, "The file has expired — start the download again.")
    # Starlette RFC 5987-encodes non-ASCII filenames (filename*=UTF-8''...).
    # served_at is stamped only after the response finishes (background task),
    # so a failed/slow first transfer doesn't start the deletion grace timer.
    return FileResponse(
        real_path,
        filename=job.filename or os.path.basename(real_path),
        content_disposition_type="attachment",
        background=BackgroundTask(_mark_served, job_id) if job.served_at is None else None,
    )


def _mark_served(job_id: str) -> None:
    job = store.get(job_id)
    if job is not None and job.served_at is None:
        store.update(job_id, served_at=time.time())


# ---------------------------------------------------------------------------
# Cleanup task
# ---------------------------------------------------------------------------

async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        try:
            # In a thread: rmtree of large job dirs must not block the loop.
            await asyncio.to_thread(_cleanup_pass)
        except Exception:  # noqa: BLE001 — cleanup must never die
            pass


def _cleanup_pass() -> None:
    now = time.time()
    ttl_seconds = FILE_TTL_MINUTES * 60
    jobs = store.all()
    for job in jobs:
        if job.status not in ("done", "error"):
            continue
        expired = (now - job.created_at) > ttl_seconds
        served = job.served_at is not None and (now - job.served_at) > _SERVED_GRACE
        if (expired or served) and job.dir and os.path.isdir(job.dir):
            shutil.rmtree(job.dir, ignore_errors=True)
    for job in store.prune(keep=200):
        if job.dir and os.path.isdir(job.dir):
            shutil.rmtree(job.dir, ignore_errors=True)
    # Sweep dirs orphaned by a restart (the store is in-memory only). The TTL
    # age guard makes this safe for dirs of jobs created moments ago.
    known = {job.id for job in jobs}
    with suppress(OSError):
        for entry in DOWNLOAD_DIR.iterdir():
            if not entry.is_dir() or entry.name in known:
                continue
            with suppress(OSError):
                if (now - entry.stat().st_mtime) > ttl_seconds:
                    shutil.rmtree(entry, ignore_errors=True)


# Static frontend (mounted last so /api/* wins).
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or 8000))
