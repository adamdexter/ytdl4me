"""Multi-source full-length audio for storefronts that gate native streams.

When Deezer / TIDAL / Apple Music (and optionally Spotify) can't give a free
full-length first-party file, we try:

1. SoundCloud search → progressive HTTP or Widevine CENC decrypt (our SC client)
2. YouTube `ytsearch1:` match (yt-dlp)

SoundCloud is preferred when a confident match exists because we already have
a working decrypt path and often get clean AAC ~160 kbps at network speed.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from . import audio_common as ac
from .jobs import JobStore

log = logging.getLogger("ytdl4me.match_sources")

# Title tokens that usually mean a rework rather than the original master.
_REWORK = re.compile(
    r"\b(remix|flip|edit|cover|mashup?|bootleg|mix|vip|refix|remake|"
    r"instrumental|karaoke|live|sped\s*up|slowed|nightcore|8d)\b",
    re.I,
)


def find_soundcloud_match(
    artist: str | None,
    title: str | None,
    duration: float | None = None,
) -> dict | None:
    """Return best SoundCloud hit {url, title, uploader, duration, score} or None."""
    if not title:
        return None
    try:
        from .soundcloud import _get_client_id
        client_id = _get_client_id()
    except Exception:
        return None

    query = f"{artist} {title}".strip() if artist else title
    api = (
        "https://api-v2.soundcloud.com/search/tracks?"
        + urllib.parse.urlencode({
            "q": query,
            "client_id": client_id,
            "limit": "12",
            "app_locale": "en",
        })
    )
    try:
        raw = ac.http_get(
            api,
            headers={
                "User-Agent": ac.UA,
                "Origin": "https://soundcloud.com",
                "Referer": "https://soundcloud.com/",
            },
            timeout=20,
        )
        collection = json.loads(raw).get("collection") or []
    except Exception as exc:
        log.debug("SoundCloud search failed: %s", exc)
        return None

    title_l = title.lower()
    artist_l = (artist or "").lower()
    title_tokens = {t for t in re.findall(r"[a-z0-9]+", title_l) if len(t) > 2}
    want_rework = bool(_REWORK.search(title))

    best = None
    best_score = -1e9
    for t in collection:
        if not t or not t.get("permalink_url"):
            continue
        if t.get("policy") == "BLOCK":
            continue
        cand_title = (t.get("title") or "").lower()
        cand_user = ((t.get("user") or {}).get("username") or "").lower()
        cand_full = f"{cand_user} {cand_title}"
        cand_dur = float(t.get("full_duration") or t.get("duration") or 0) / 1000.0

        score = 0.0
        # Token overlap on title
        cand_tokens = {w for w in re.findall(r"[a-z0-9]+", cand_title) if len(w) > 2}
        if title_tokens:
            overlap = len(title_tokens & cand_tokens) / max(len(title_tokens), 1)
            score += overlap * 40
        if title_l and title_l in cand_title:
            score += 25
        if artist_l and artist_l in cand_full:
            score += 20
        # Duration closeness is a strong signal for the same master
        if duration and cand_dur:
            delta = abs(cand_dur - duration)
            if delta <= 2:
                score += 35
            elif delta <= 5:
                score += 20
            elif delta <= 12:
                score += 5
            else:
                score -= min(40, delta)  # heavily penalize wrong length
        # Prefer non-reworks unless the original title is a rework
        if _REWORK.search(cand_title) and not want_rework:
            score -= 30
        # Prefer streamable
        if t.get("streamable") is False:
            score -= 50
        # Slight boost for verified / high play count
        if (t.get("user") or {}).get("verified"):
            score += 5
        plays = t.get("playback_count") or 0
        if plays > 100_000:
            score += 5
        elif plays > 10_000:
            score += 2

        if score > best_score:
            best_score = score
            best = {
                "url": t["permalink_url"],
                "title": t.get("title"),
                "uploader": (t.get("user") or {}).get("username"),
                "duration": cand_dur or None,
                "score": score,
            }

    # Threshold: require a reasonably confident hit
    if not best or best_score < 45:
        log.debug(
            "No confident SoundCloud match for %r / %r (best=%s)",
            artist, title, best,
        )
        return None
    log.info(
        "SoundCloud match score=%.1f %s ← %s - %s",
        best_score, best["url"], artist, title,
    )
    return best


def download_soundcloud_match(
    store: JobStore,
    job_id: str,
    sc_url: str,
    option_id: str,
    job_dir: str,
    *,
    filename_stem: str | None,
    tags: dict | None,
) -> None:
    """Download via SoundCloud client (progressive / HLS / Widevine CENC)."""
    from . import soundcloud as sc
    from .downloader import DownloadFailed

    try:
        sc.run_download(store, job_id, sc_url, option_id, job_dir, filename_stem)
    except sc.SoundCloudError as exc:
        raise DownloadFailed(str(exc)) from exc

    # Re-tag with storefront metadata when provided (overwrite SC tags).
    if tags:
        job = store.get(job_id)
        if job and job.filepath:
            ac.apply_tags(
                job.filepath,
                title=tags.get("title"),
                artist=tags.get("artist"),
                album=tags.get("album"),
            )
