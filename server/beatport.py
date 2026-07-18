"""Beatport track resolve + download.

Full-quality first-party streams require a Beatport Streaming subscription
(beatportdl-style `/catalog/tracks/{id}/download|stream/`). Without credentials
we:

1. Scrape public track metadata (Cloudflare-protected page → __NEXT_DATA__).
2. Prefer any free_download URLs when Beatport marks a track free.
3. Otherwise fall through to the app-wide SoundCloud-decrypt / YouTube-match
   cascade (wired in main.py via prefers_youtube_match).

Optional env for native full streams (paid plan):
  BEATPORT_ACCESS_TOKEN  — OAuth bearer from a streaming-enabled account
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse  # noqa: F401 — kept for download quality query encoding

from . import audio_common as ac
from .jobs import JobStore

log = logging.getLogger("ytdl4me.beatport")

_TRACK_RE = re.compile(
    r"beatport\.com/track/(?P<slug>[^/?#]+)/(?P<id>\d+)",
    re.I,
)


class BeatportError(Exception):
    pass


def _track_id_from_url(url: str) -> tuple[str, str]:
    m = _TRACK_RE.search(url)
    if not m:
        raise BeatportError(
            "Couldn't find a Beatport track id — use a /track/<slug>/<id> link."
        )
    return m.group("id"), m.group("slug")


def _scrape_track(url: str) -> dict:
    """Load public track JSON from the Next.js page (needs cloudscraper for CF)."""
    try:
        import cloudscraper
    except ImportError as exc:
        raise BeatportError(
            "cloudscraper is required for Beatport (pip install cloudscraper)."
        ) from exc

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "mobile": False}
    )
    try:
        resp = scraper.get(url, timeout=45)
    except Exception as exc:
        raise BeatportError(f"Couldn't reach Beatport: {exc}") from exc
    if resp.status_code != 200:
        raise BeatportError(f"Beatport returned HTTP {resp.status_code}.")

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    if not m:
        raise BeatportError("Couldn't parse Beatport page metadata.")
    try:
        next_data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise BeatportError("Beatport page JSON was invalid.") from exc

    # Walk dehydrated React Query cache for the track object.
    track = _find_track_blob(next_data)
    if not track:
        raise BeatportError("Couldn't find track data on that Beatport page.")
    return track


def _find_track_blob(node) -> dict | None:
    if isinstance(node, dict):
        # Track objects always carry sample_url + length_ms + artists.
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
    return {
        "artist": artist or None,
        "title": title,
        "thumbnail": _thumbnail(track),
        "duration": duration,
        "isrc": track.get("isrc"),
        "search_query": f"{artist} - {title}" if artist else title,
        "search_query_isrc": (
            f"{artist} - {title} {track['isrc']}"
            if artist and track.get("isrc")
            else (f"{title} {track['isrc']}" if track.get("isrc") else None)
        ),
        "source_label": "Beatport",
        "sample_url": track.get("sample_url"),
        "free_downloads": _free_download_urls(track),
        "track_id": tid,
        "bpm": track.get("bpm"),
        "key": (track.get("key") or {}).get("name") if isinstance(track.get("key"), dict) else track.get("key"),
    }


def probe(url: str) -> dict:
    """Blocking probe — used only for native/free paths; cascade uses resolve_public."""
    meta = resolve_public(url)
    free = meta.get("free_downloads") or []
    if free:
        quality = "Full free download"
        best_size = None
    elif os.environ.get("BEATPORT_ACCESS_TOKEN"):
        quality = "AAC/FLAC (account stream)"
        best_size = int(meta["duration"] * 256 * 125) if meta.get("duration") else None
    else:
        # Cascade will pick SC decrypt / YT — report honestly.
        quality = "via SoundCloud/YouTube match (no Beatport token)"
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


def _native_download_url(track_id: str, token: str) -> tuple[str, str]:
    """Return (url, ext) for full stream/download with bearer token."""
    # Prefer progressive download qualities, then HLS stream.
    headers = {
        "User-Agent": ac.UA,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    for quality in ("lossless", "high", "medium"):
        try:
            raw = ac.http_get(
                f"https://api.beatport.com/v4/catalog/tracks/{track_id}/download/"
                f"?quality={urllib.parse.quote(quality)}",
                headers=headers,
                timeout=30,
            )
            data = json.loads(raw)
            loc = data.get("location") or data.get("url")
            if loc:
                ext = "flac" if quality == "lossless" else "m4a"
                return loc, ext
        except Exception:
            continue
    # HLS stream (Essential plan)
    try:
        raw = ac.http_get(
            f"https://api.beatport.com/v4/catalog/tracks/{track_id}/stream/",
            headers=headers,
            timeout=30,
        )
        data = json.loads(raw)
        stream = data.get("stream_url") or data.get("url")
        if stream:
            return stream, "m3u8"
    except Exception as exc:
        raise BeatportError(
            "Beatport native stream failed — check BEATPORT_ACCESS_TOKEN "
            "and that the account has an active Streaming plan."
        ) from exc
    raise BeatportError("No native Beatport stream was returned.")


def run_download(
    store: JobStore,
    job_id: str,
    url: str,
    option_id: str,
    job_dir: str,
    filename_stem: str | None = None,
) -> None:
    """Native/free Beatport download only. Cascade path is handled by main.py."""
    if option_id not in ac.AUDIO_OPTION_IDS:
        raise BeatportError(f"Unknown option '{option_id}'.")
    store.update(job_id, status="downloading", progress=5.0)
    meta = resolve_public(url)
    artist, title = meta.get("artist"), meta.get("title")
    stem = filename_stem or ac.stem_for(title, artist, f"beatport-{meta.get('track_id')}")
    thumb = ac.fetch_thumb(meta.get("thumbnail"), job_dir)

    # 1) Free full downloads when Beatport marks the track free
    free = meta.get("free_downloads") or []
    if free:
        store.update(job_id, progress=20.0)
        data = ac.http_get(free[0], headers={"User-Agent": ac.UA}, timeout=180)
        # sniff ext
        ext = ".mp3"
        if data[:4] == b"fLaC":
            ext = ".flac"
        elif data[4:8] == b"ftyp":
            ext = ".m4a"
        raw = os.path.join(job_dir, f"raw{ext}")
        open(raw, "wb").write(data)
        store.update(job_id, progress=80.0, downloaded_bytes=len(data))
        ac.finalize_audio(
            store, job_id, job_dir, raw, ext, option_id, stem,
            title=title, artist=artist, thumb_path=thumb,
        )
        return

    # 2) Native paid stream
    token = os.environ.get("BEATPORT_ACCESS_TOKEN")
    if not token:
        raise BeatportError(
            "No free full download and no BEATPORT_ACCESS_TOKEN — "
            "use the SoundCloud/YouTube match path."
        )
    store.update(job_id, progress=15.0)
    stream_url, kind = _native_download_url(meta["track_id"], token)
    store.update(job_id, progress=30.0)
    if kind == "m3u8" or ".m3u8" in stream_url:
        raw = os.path.join(job_dir, "raw.m4a")
        ac.ffmpeg(["-i", stream_url, "-c", "copy", "-vn", raw])
        ext = ".m4a"
    else:
        data = ac.http_get(stream_url, headers={"User-Agent": ac.UA}, timeout=180)
        ext = f".{kind}" if not kind.startswith(".") else kind
        if ext == ".m3u8":
            ext = ".m4a"
        raw = os.path.join(job_dir, f"raw{ext}")
        open(raw, "wb").write(data)
        store.update(job_id, downloaded_bytes=len(data))
    store.update(job_id, progress=85.0)
    ac.finalize_audio(
        store, job_id, job_dir, raw, ext, option_id, stem,
        title=title, artist=artist, thumb_path=thumb,
    )
