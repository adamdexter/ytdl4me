"""Beatport track resolve + download.

Inspired by https://github.com/unspok3n/beatportdl — same official API surface:

  POST /auth/login/          → sessionid cookie
  GET  /auth/o/authorize/    → authorization code
  POST /auth/o/token/        → access_token + refresh_token
  GET  /catalog/tracks/{id}/download/?quality=lossless|high|medium
  GET  /catalog/tracks/{id}/stream/   → AES-128 HLS (Essential plan)

Full masters need a Beatport Streaming subscription. Without credentials we:

1. Scrape public metadata (cloudscraper → jina → URL slug)
2. Honor free_downloads[] when present
3. Fall through to SoundCloud-decrypt / YouTube-match cascade (main.py)

Env (native full quality):
  BEATPORT_USERNAME + BEATPORT_PASSWORD  — preferred (beatportdl login flow)
  BEATPORT_ACCESS_TOKEN                  — raw bearer (optional override)
  BEATPORT_REFRESH_TOKEN                 — optional companion to access token
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from . import audio_common as ac
from .jobs import JobStore

log = logging.getLogger("ytdl4me.beatport")

_API = "https://api.beatport.com/v4"
# Public Beatport web client id (same as beatportdl / official web app)
_CLIENT_ID = "ryZ8LuyQVPqbK2mBX2Hwt4qSMtnWuTYSqBPO92yQ"

_TRACK_RE = re.compile(
    r"beatport\.com/track/(?P<slug>[^/?#]+)/(?P<id>\d+)",
    re.I,
)

_token_lock = threading.Lock()
_token_cache: dict | None = None  # {access_token, refresh_token, expires_at}


class BeatportError(Exception):
    pass


def has_native_credentials() -> bool:
    return bool(
        os.environ.get("BEATPORT_ACCESS_TOKEN")
        or (
            os.environ.get("BEATPORT_USERNAME")
            and os.environ.get("BEATPORT_PASSWORD")
        )
    )


def _track_id_from_url(url: str) -> tuple[str, str]:
    m = _TRACK_RE.search(url)
    if not m:
        raise BeatportError(
            "Couldn't find a Beatport track id — use a /track/<slug>/<id> link."
        )
    return m.group("id"), m.group("slug")


# ---------------------------------------------------------------------------
# Metadata scrape (no auth)
# ---------------------------------------------------------------------------

def _scrape_track(url: str) -> dict:
    """Load public track metadata.

    Order: cloudscraper → jina.ai reader → URL slug fallback.
    """
    errors: list[str] = []

    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "mobile": False}
        )
        resp = scraper.get(url, timeout=45)
        if resp.status_code == 200:
            m = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if m:
                next_data = json.loads(m.group(1))
                track = _find_track_blob(next_data)
                if track:
                    return track
            errors.append("page had no track JSON")
        else:
            errors.append(f"HTTP {resp.status_code}")
    except Exception as exc:
        errors.append(f"cloudscraper: {exc}")

    try:
        track = _scrape_via_jina(url)
        if track:
            return track
        errors.append("jina: no metadata")
    except Exception as exc:
        errors.append(f"jina: {exc}")

    try:
        tid, slug = _track_id_from_url(url)
        words = slug.replace("-", " ").strip()
        title = " ".join(w.capitalize() for w in words.split())
        return {
            "id": int(tid) if tid.isdigit() else tid,
            "name": title,
            "mix_name": "",
            "artists": [],
            "length_ms": None,
            "sample_url": None,
            "isrc": None,
            "image": {},
            "free_downloads": [],
            "_slug_fallback": True,
        }
    except Exception as exc:
        errors.append(f"slug: {exc}")

    raise BeatportError(
        "Couldn't read that Beatport page (" + "; ".join(errors[:3]) + ")."
    )


def _scrape_via_jina(url: str) -> dict | None:
    raw = ac.http_get(
        f"https://r.jina.ai/{url}",
        headers={"User-Agent": ac.UA, "Accept": "text/plain"},
        timeout=50,
    )
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

    artist = title = None
    m = re.search(r"^Title:\s*(.+?)\s*\|\s*Music", text, re.I | re.M)
    if m:
        head = re.sub(r"\s*\[[^\]]+\]\s*$", "", m.group(1).strip()).strip()
        if " - " in head:
            artist, title = head.split(" - ", 1)
        else:
            title = head
    if not artist:
        m = re.search(r"Artists?:\s*\[([^\]]+)\]", text, re.I)
        if m:
            artist = m.group(1).strip()
    if not title:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            title = m.group(1).strip()
    if not title:
        return None

    length_ms = None
    m = re.search(r"Length:\s*\n\s*(\d+):(\d{2})", text)
    if m:
        length_ms = (int(m.group(1)) * 60 + int(m.group(2))) * 1000

    thumb = None
    m = re.search(
        r"https://geo-media\.beatport\.com/image_size/\d+x\d+/[a-f0-9-]+\.jpg",
        text,
        re.I,
    )
    if m:
        thumb = m.group(0)

    tid, _slug = _track_id_from_url(url)
    return {
        "id": int(tid) if tid.isdigit() else tid,
        "name": title,
        "mix_name": "",
        "artists": [{"name": artist}] if artist else [],
        "length_ms": length_ms,
        "sample_url": None,
        "isrc": None,
        "image": {"uri": thumb} if thumb else {},
        "free_downloads": [],
    }


def _find_track_blob(node) -> dict | None:
    if isinstance(node, dict):
        if (
            "sample_url" in node
            and "length_ms" in node
            and "artists" in node
            and "name" in node
            and "id" in node
        ):
            return node
        for v in node.values():
            found = _find_track_blob(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_track_blob(v)
            if found:
                return found
    return None


def _artists(track: dict) -> str:
    names = []
    for a in track.get("artists") or []:
        if isinstance(a, dict) and a.get("name"):
            names.append(a["name"])
    return ", ".join(names)


def _title(track: dict) -> str:
    name = track.get("name") or "Unknown"
    mix = track.get("mix_name") or track.get("mix")
    if mix and mix.lower() not in name.lower():
        return f"{name} ({mix})"
    return name


def _thumbnail(track: dict) -> str | None:
    img = track.get("image") or {}
    uri = img.get("dynamic_uri") or img.get("uri")
    if not uri:
        rel = track.get("release") or {}
        img = rel.get("image") or {}
        uri = img.get("dynamic_uri") or img.get("uri")
    if uri and "{w}" in uri:
        return uri.replace("{w}", "600").replace("{h}", "600")
    return uri


def _free_download_urls(track: dict) -> list[str]:
    urls = []
    for item in track.get("free_downloads") or []:
        if isinstance(item, str) and item.startswith("http"):
            urls.append(item)
        elif isinstance(item, dict):
            for k in ("url", "location", "download_url", "file"):
                if item.get(k) and str(item[k]).startswith("http"):
                    urls.append(item[k])
                    break
    return urls


def resolve_public(url: str) -> dict:
    """Public metadata for cascade matching (no paid token)."""
    track = _scrape_track(url)
    tid = str(track.get("id") or "")
    artist = _artists(track)
    title = _title(track)
    duration = None
    if track.get("length_ms"):
        duration = float(track["length_ms"]) / 1000.0
    # Prefer a clean search string for SC/YT (strip "Original Mix" noise).
    search_title = re.sub(
        r"\s*[\(\[]\s*(original\s+mix|extended\s+mix|club\s+mix|radio\s+edit|"
        r"original|extended)\s*[\)\]]\s*$",
        "",
        title,
        flags=re.I,
    ).strip() or title
    search_query = f"{artist} - {search_title}" if artist else search_title
    return {
        "artist": artist or None,
        "title": title,
        "thumbnail": _thumbnail(track),
        "duration": duration,
        "isrc": track.get("isrc"),
        "search_query": search_query,
        "search_query_isrc": (
            f"{search_query} {track['isrc']}" if track.get("isrc") else None
        ),
        "source_label": "Beatport",
        "sample_url": track.get("sample_url"),
        "free_downloads": _free_download_urls(track),
        "track_id": tid,
        "bpm": track.get("bpm"),
        "key": (
            (track.get("key") or {}).get("name")
            if isinstance(track.get("key"), dict)
            else track.get("key")
        ),
    }


# ---------------------------------------------------------------------------
# Auth (beatportdl-compatible)
# ---------------------------------------------------------------------------

def _token_cache_path() -> Path:
    root = os.environ.get("DOWNLOAD_DIR") or tempfile_dir()
    p = Path(root) / ".ytdl4me-cache" / "beatport_token.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


def _api_request(
    method: str,
    path: str,
    *,
    data: dict | bytes | None = None,
    content_type: str | None = None,
    token: str | None = None,
    cookies: str | None = None,
    allow_redirects: bool = False,
) -> tuple[int, dict, bytes, dict]:
    """Return (status, headers, body_bytes, parsed_json_or_empty)."""
    url = path if path.startswith("http") else f"{_API}{path}"
    headers = {
        "User-Agent": ac.UA,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    body = None
    if data is not None:
        if content_type == "application/json":
            body = json.dumps(data).encode() if isinstance(data, dict) else data
            headers["Content-Type"] = "application/json"
        elif content_type == "application/x-www-form-urlencoded":
            body = (
                urllib.parse.urlencode(data).encode()
                if isinstance(data, dict)
                else data
            )
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = data if isinstance(data, bytes) else str(data).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookies:
        headers["Cookie"] = cookies

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    # Manual redirect control for authorize
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    if not allow_redirects:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(_NoRedirect)

    try:
        with opener.open(req, timeout=40) as resp:
            raw = resp.read()
            hdrs = dict(resp.headers)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        hdrs = dict(exc.headers) if exc.headers else {}
        status = exc.code
        if status not in (301, 302, 303, 307, 308):
            # try parse error detail
            try:
                err = json.loads(raw)
                detail = err.get("detail") or err.get("error") or raw[:200]
            except Exception:
                detail = raw[:200]
            if status >= 400:
                raise BeatportError(f"Beatport API {status}: {detail}") from exc
    parsed: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {}
    return status, hdrs, raw, parsed


def _login_session(username: str, password: str) -> str:
    """POST /auth/login/ → sessionid cookie (beatportdl)."""
    status, hdrs, raw, parsed = _api_request(
        "POST",
        "/auth/login/",
        data={"username": username, "password": password},
        content_type="application/json",
        allow_redirects=True,
    )
    # urllib folds set-cookie; parse from headers
    set_cookie = hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or ""
    # Also try raw multi-header via email.message style - urllib may only give one
    m = re.search(r"sessionid=([^;,\s]+)", set_cookie)
    if m:
        return m.group(1)
    # Fallback: cookie jar style from response isn't available; re-request with http.cookiejar
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        f"{_API}/auth/login/",
        data=body,
        headers={
            "User-Agent": ac.UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        opener.open(req, timeout=40)
    except urllib.error.HTTPError as exc:
        raise BeatportError(f"Beatport login failed: HTTP {exc.code}") from exc
    for c in cj:
        if c.name == "sessionid":
            return c.value
    raise BeatportError("Beatport login failed — no sessionid cookie (check credentials).")


def _authorize_code(session_id: str) -> str:
    status, hdrs, raw, parsed = _api_request(
        "GET",
        f"/auth/o/authorize/?client_id={_CLIENT_ID}&response_type=code",
        cookies=f"sessionid={session_id}",
        allow_redirects=False,
    )
    loc = hdrs.get("Location") or hdrs.get("location") or ""
    if not loc and status in (200,):
        # some environments return code in body
        code = parsed.get("code")
        if code:
            return code
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    code = (q.get("code") or [None])[0]
    if not code:
        raise BeatportError("Beatport authorize failed — no code in redirect.")
    return code


def _issue_token(code: str | None = None, refresh_token: str | None = None) -> dict:
    if refresh_token:
        payload = {
            "client_id": _CLIENT_ID,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    elif code:
        payload = {
            "client_id": _CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
        }
    else:
        raise BeatportError("No authorization code or refresh token.")
    status, hdrs, raw, parsed = _api_request(
        "POST",
        "/auth/o/token/",
        data=payload,
        content_type="application/x-www-form-urlencoded",
        allow_redirects=True,
    )
    if not parsed.get("access_token"):
        raise BeatportError(f"Beatport token issue failed: {parsed or raw[:200]}")
    return {
        "access_token": parsed["access_token"],
        "refresh_token": parsed.get("refresh_token") or refresh_token or "",
        "expires_at": time.time() + int(parsed.get("expires_in") or 3600) - 120,
    }


def _load_token_file() -> dict | None:
    try:
        p = _token_cache_path()
        if p.is_file():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


def _save_token_file(tok: dict) -> None:
    try:
        p = _token_cache_path()
        p.write_text(json.dumps(tok))
        os.chmod(p, 0o600)
    except Exception:
        log.debug("could not cache beatport token", exc_info=True)


def get_access_token() -> str:
    """Return a valid OAuth access token (env, cache, or username/password login)."""
    global _token_cache
    with _token_lock:
        # Explicit access token wins (short-lived unless refresh also set)
        env_access = os.environ.get("BEATPORT_ACCESS_TOKEN")
        env_refresh = os.environ.get("BEATPORT_REFRESH_TOKEN")
        if env_access and not os.environ.get("BEATPORT_USERNAME"):
            return env_access

        if _token_cache and time.time() < _token_cache.get("expires_at", 0):
            return _token_cache["access_token"]

        cached = _load_token_file()
        if cached and time.time() < cached.get("expires_at", 0):
            _token_cache = cached
            return cached["access_token"]

        # Refresh
        refresh = (
            (cached or {}).get("refresh_token")
            or env_refresh
            or (_token_cache or {}).get("refresh_token")
        )
        if refresh:
            try:
                tok = _issue_token(refresh_token=refresh)
                _token_cache = tok
                _save_token_file(tok)
                return tok["access_token"]
            except Exception as exc:
                log.warning("Beatport token refresh failed: %s", exc)

        # Full login (beatportdl flow)
        user = os.environ.get("BEATPORT_USERNAME")
        password = os.environ.get("BEATPORT_PASSWORD")
        if not user or not password:
            if env_access:
                return env_access
            raise BeatportError(
                "Set BEATPORT_USERNAME + BEATPORT_PASSWORD (Streaming plan) "
                "or BEATPORT_ACCESS_TOKEN for native Beatport masters."
            )
        session_id = _login_session(user, password)
        code = _authorize_code(session_id)
        tok = _issue_token(code=code)
        _token_cache = tok
        _save_token_file(tok)
        return tok["access_token"]


# ---------------------------------------------------------------------------
# Native download / HLS (beatportdl)
# ---------------------------------------------------------------------------

def _native_download_url(track_id: str, token: str) -> tuple[str, str]:
    """Return (url, kind) where kind is flac|m4a|m3u8."""
    headers_auth = token
    for quality in ("lossless", "high", "medium"):
        try:
            status, hdrs, raw, data = _api_request(
                "GET",
                f"/catalog/tracks/{track_id}/download/?quality={urllib.parse.quote(quality)}",
                token=headers_auth,
                allow_redirects=True,
            )
            loc = data.get("location") or data.get("url")
            if loc:
                ext = "flac" if quality == "lossless" else "m4a"
                return loc, ext
        except BeatportError:
            continue
    # Essential plan: AES-128 HLS via /stream/
    status, hdrs, raw, data = _api_request(
        "GET",
        f"/catalog/tracks/{track_id}/stream/",
        token=headers_auth,
        allow_redirects=True,
    )
    stream = data.get("stream_url") or data.get("url")
    if stream:
        return stream, "m3u8"
    raise BeatportError(
        "No native Beatport stream — need an active Streaming plan "
        "(Essential for HLS, Advanced+ for progressive AAC/FLAC)."
    )


def _download_hls_aes(stream_url: str, out_path: str, progress_cb=None) -> None:
    """Download AES-128 CBC HLS (beatportdl medium-hls path) and remux to m4a."""
    playlist = ac.http_get(stream_url, headers={"User-Agent": ac.UA}, timeout=40).decode(
        "utf-8", "replace"
    )
    base = stream_url.rsplit("/", 1)[0] + "/"
    key_uri = iv_hex = None
    segs: list[str] = []
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                key_uri = m.group(1)
            m = re.search(r"IV=0x([0-9a-fA-F]+)", line)
            if m:
                iv_hex = m.group(1)
        elif line and not line.startswith("#"):
            segs.append(urllib.parse.urljoin(base, line))
    if not segs:
        raise BeatportError("Beatport HLS playlist had no segments.")
    if not key_uri:
        # clear HLS — rare
        key = iv = None
    else:
        key_url = urllib.parse.urljoin(base, key_uri)
        key = ac.http_get(key_url, headers={"User-Agent": ac.UA}, timeout=20)
        iv = bytes.fromhex(iv_hex) if iv_hex else b"\x00" * 16

    tmp = out_path + ".ts"
    total = len(segs)
    with open(tmp, "wb") as f:
        for i, seg_url in enumerate(segs):
            seg = ac.http_get(seg_url, headers={"User-Agent": ac.UA}, timeout=40)
            if key is not None:
                cipher = AES.new(key, AES.MODE_CBC, iv)
                try:
                    seg = unpad(cipher.decrypt(seg), AES.block_size)
                except ValueError:
                    seg = cipher.decrypt(seg)
            f.write(seg)
            if progress_cb:
                progress_cb(i + 1, total)
    ac.ffmpeg(["-i", tmp, "-map_metadata", "-1", "-c:a", "copy", out_path])
    try:
        os.remove(tmp)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public probe / download
# ---------------------------------------------------------------------------

def probe(url: str) -> dict:
    meta = resolve_public(url)
    free = meta.get("free_downloads") or []
    if free:
        quality = "Full free download"
        best_size = None
    elif has_native_credentials():
        quality = "AAC/FLAC (Beatport Streaming)"
        best_size = int(meta["duration"] * 256 * 125) if meta.get("duration") else None
    else:
        quality = "via SoundCloud/YouTube match (no Beatport Streaming creds)"
        best_size = int(meta["duration"] * 160 * 125) if meta.get("duration") else None
    return ac.probe_payload(
        platform="beatport",
        url=url,
        title=meta.get("title"),
        uploader=meta.get("artist"),
        duration=meta.get("duration"),
        thumbnail=meta.get("thumbnail"),
        quality=quality,
        best_size=best_size,
    )


def run_download(
    store: JobStore,
    job_id: str,
    url: str,
    option_id: str,
    job_dir: str,
    filename_stem: str | None = None,
) -> None:
    """Native/free Beatport download. Cascade is handled by main.py when no creds."""
    if option_id not in ac.AUDIO_OPTION_IDS:
        raise BeatportError(f"Unknown option '{option_id}'.")
    store.update(job_id, status="downloading", progress=5.0)
    meta = resolve_public(url)
    artist, title = meta.get("artist"), meta.get("title")
    stem = filename_stem or ac.stem_for(title, artist, f"beatport-{meta.get('track_id')}")
    thumb = ac.fetch_thumb(meta.get("thumbnail"), job_dir)

    free = meta.get("free_downloads") or []
    if free:
        store.update(job_id, progress=20.0)
        data = ac.http_get(free[0], headers={"User-Agent": ac.UA}, timeout=180)
        ext = ".mp3"
        if data[:4] == b"fLaC":
            ext = ".flac"
        elif len(data) > 8 and data[4:8] == b"ftyp":
            ext = ".m4a"
        raw = os.path.join(job_dir, f"raw{ext}")
        open(raw, "wb").write(data)
        store.update(job_id, progress=80.0, downloaded_bytes=len(data))
        ac.finalize_audio(
            store, job_id, job_dir, raw, ext, option_id, stem,
            title=title, artist=artist, thumb_path=thumb,
        )
        return

    if not has_native_credentials():
        raise BeatportError(
            "No free full download and no Beatport Streaming credentials — "
            "use the SoundCloud/YouTube match path."
        )

    store.update(job_id, progress=12.0)
    token = get_access_token()
    store.update(job_id, progress=20.0)
    stream_url, kind = _native_download_url(meta["track_id"], token)
    store.update(job_id, progress=30.0)

    if kind == "m3u8" or ".m3u8" in stream_url:
        raw = os.path.join(job_dir, "raw.m4a")

        def cb(done, total):
            pct = 30.0 + (done / max(total, 1)) * 55.0
            store.update(job_id, progress=pct, status="downloading")

        _download_hls_aes(stream_url, raw, progress_cb=cb)
        ext = ".m4a"
    else:
        data = ac.http_get(stream_url, headers={"User-Agent": ac.UA}, timeout=180)
        ext = ".flac" if kind == "flac" else ".m4a"
        raw = os.path.join(job_dir, f"raw{ext}")
        open(raw, "wb").write(data)
        store.update(job_id, progress=85.0, downloaded_bytes=len(data))

    ac.finalize_audio(
        store, job_id, job_dir, raw, ext, option_id, stem,
        title=title, artist=artist, thumb_path=thumb,
    )
