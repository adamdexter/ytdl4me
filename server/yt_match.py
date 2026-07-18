"""Resolve public track metadata for YouTube-matched downloads.

Deezer / TIDAL / Apple Music full streams normally need paid or account
sessions. When those credentials aren't configured we do what Spotify already
does: read public title/artist (and ISRC when available), then download the
best YouTube match via `ytsearch1:`.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

from . import audio_common as ac


class MatchError(Exception):
    """User-facing metadata resolution failure."""


def prefers_youtube_match(platform: str) -> bool:
    """True when we should skip native DRM streams and match via SC/YouTube."""
    if platform == "deezer":
        return not bool(os.environ.get("DEEZER_ARL"))
    if platform == "tidal":
        return not bool(os.environ.get("TIDAL_ACCESS_TOKEN"))
    if platform == "applemusic":
        return not bool(os.environ.get("APPLE_MEDIA_USER_TOKEN"))
    if platform == "beatport":
        # Full masters need Streaming plan (beatportdl-style login).
        # Without creds: SC decrypt / YouTube cascade.
        try:
            from .beatport import has_native_credentials
            return not has_native_credentials()
        except Exception:
            return not bool(os.environ.get("BEATPORT_ACCESS_TOKEN"))
    return False


def resolve_track(platform: str, url: str) -> dict:
    """Return {artist, title, thumbnail, duration, search_query, source_label}."""
    if platform == "deezer":
        return _resolve_deezer(url)
    if platform == "tidal":
        return _resolve_tidal(url)
    if platform == "applemusic":
        return _resolve_apple(url)
    if platform == "beatport":
        from . import beatport as bp
        try:
            return bp.resolve_public(url)
        except bp.BeatportError as exc:
            raise MatchError(str(exc)) from exc
    raise MatchError(f"No YouTube-match resolver for {platform}.")

def _pack(artist: str | None, title: str | None, *, thumbnail=None,
          duration=None, isrc=None, source_label: str) -> dict:
    if not title:
        raise MatchError(f"Couldn't read that {source_label} link.")
    if artist and title:
        query = f"{artist} - {title}"
    else:
        query = title
    # ISRC can help disambiguate covers/remixes on YouTube when present.
    if isrc:
        query_isrc = f"{query} {isrc}"
    else:
        query_isrc = query
    return {
        "artist": artist,
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration,
        "isrc": isrc,
        "search_query": query,
        "search_query_isrc": query_isrc,
        "source_label": source_label,
    }


# ---------------------------------------------------------------------------
# Deezer (public API — no account)
# ---------------------------------------------------------------------------

def _resolve_deezer(url: str) -> dict:
    m = re.search(r"/track/(\d+)", url)
    if not m:
        # follow short links
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": ac.UA}, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as resp:
                url = resp.geturl()
            m = re.search(r"/track/(\d+)", url)
        except Exception:
            m = None
    if not m:
        raise MatchError("Couldn't find a Deezer track id in that link.")
    info = json.loads(ac.http_get(
        f"https://api.deezer.com/track/{m.group(1)}",
        headers={"User-Agent": ac.UA},
    ))
    if info.get("error"):
        raise MatchError(info["error"].get("message") or "Deezer track not found.")
    artist = (info.get("artist") or {}).get("name")
    title = info.get("title")
    thumb = (info.get("album") or {}).get("cover_xl") or (info.get("album") or {}).get("cover_big")
    duration = float(info["duration"]) if info.get("duration") else None
    return _pack(
        artist, title,
        thumbnail=thumb,
        duration=duration,
        isrc=info.get("isrc"),
        source_label="Deezer",
    )


# ---------------------------------------------------------------------------
# TIDAL (oEmbed + public pages — no login)
# ---------------------------------------------------------------------------

def _resolve_tidal(url: str) -> dict:
    m = re.search(r"/track[s]?/(\d+)", url)
    track_id = m.group(1) if m else None
    title = artist = thumb = duration = None

    # Public catalog API (no login) — works for many territories with a web client token.
    if track_id:
        for host in ("api.tidal.com", "api.tidalhifi.com"):
            try:
                raw = ac.http_get(
                    f"https://{host}/v1/tracks/{track_id}?countryCode=US",
                    headers={
                        "User-Agent": ac.UA,
                        "x-tidal-token": "CzET4vdadNUFQ5JU",
                    },
                )
                t = json.loads(raw)
                title = t.get("title")
                arts = t.get("artists") or []
                if arts:
                    artist = ", ".join(
                        a.get("name") for a in arts if a.get("name")
                    ) or (t.get("artist") or {}).get("name")
                else:
                    artist = (t.get("artist") or {}).get("name")
                if t.get("duration"):
                    duration = float(t["duration"])
                if (t.get("album") or {}).get("cover"):
                    c = t["album"]["cover"].replace("-", "/")
                    thumb = f"https://resources.tidal.com/images/{c}/640x640.jpg"
                break
            except Exception:
                continue

    # HTML og: tags from the public track page ("Artist - Title")
    if track_id and (not title or not artist):
        for page_url in (
            f"https://tidal.com/browse/track/{track_id}",
            f"https://tidal.com/track/{track_id}",
            url,
        ):
            try:
                html = ac.http_get(
                    page_url, headers={"User-Agent": ac.UA},
                ).decode("utf-8", "replace")
            except Exception:
                continue
            if not title:
                m2 = re.search(
                    r'property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    html, re.I,
                ) or re.search(
                    r'content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                    html, re.I,
                )
                if m2:
                    title = html_unescape(m2.group(1))
            if not thumb:
                m2 = re.search(
                    r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    html, re.I,
                ) or re.search(
                    r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    html, re.I,
                )
                if m2:
                    thumb = m2.group(1)
            if title and " - " in title and not artist:
                artist, title = title.split(" - ", 1)
            if title:
                break

    return _pack(artist, title, thumbnail=thumb, duration=duration, source_label="TIDAL")


def html_unescape(s: str) -> str:
    import html as html_lib
    return html_lib.unescape(s)

# ---------------------------------------------------------------------------
# Apple Music (catalog API with scraped developer JWT — free)
# ---------------------------------------------------------------------------

def _apple_dev_token() -> str:
    html = ac.http_get(
        "https://music.apple.com/us/new",
        headers={"User-Agent": ac.UA},
    ).decode("utf-8", "replace")
    m = re.search(r'src="(/(?:assets/)?index(?:-legacy)?[~-][^"]+\.js)"', html)
    if not m:
        m = re.search(r'src="(/assets/index[^"]+\.js)"', html)
    if not m:
        raise MatchError("Couldn't bootstrap Apple Music catalog access.")
    js = ac.http_get(
        f"https://music.apple.com{m.group(1)}",
        headers={"User-Agent": ac.UA},
    ).decode("utf-8", "replace")
    m2 = re.search(
        r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}',
        js,
    )
    if not m2:
        raise MatchError("Couldn't bootstrap Apple Music catalog access.")
    return m2.group(0)


def _resolve_apple(url: str) -> dict:
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    storefront = parts[0] if parts and len(parts[0]) == 2 else "us"
    qs = urllib.parse.parse_qs(parsed.query)
    track_id = None
    if "i" in qs:
        track_id = qs["i"][0]
    else:
        m = re.search(r"/(?:song|album)/[^/]+/(\d+)", parsed.path)
        if m and "/song/" in parsed.path:
            track_id = m.group(1)
        elif m and "/album/" in parsed.path and "i=" not in (parsed.query or ""):
            raise MatchError(
                "Paste a link to a single Apple Music song (with ?i=…), not only the album."
            )
        else:
            m = re.search(r"/(\d+)(?:\?|$)", parsed.path)
            if m:
                track_id = m.group(1)
    if not track_id:
        raise MatchError("Couldn't find an Apple Music song id in that link.")

    token = _apple_dev_token()
    raw = ac.http_get(
        f"https://amp-api.music.apple.com/v1/catalog/{storefront}/songs/{track_id}?l=en-US",
        headers={
            "User-Agent": ac.UA,
            "Authorization": f"Bearer {token}",
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
        },
    )
    data = json.loads(raw)
    attrs = (data.get("data") or [{}])[0].get("attributes") or {}
    if not attrs:
        raise MatchError("Couldn't read that Apple Music song.")
    title = attrs.get("name")
    artist = attrs.get("artistName")
    duration = (attrs.get("durationInMillis") or 0) / 1000.0 or None
    thumb = None
    if attrs.get("artwork"):
        thumb = (
            attrs["artwork"].get("url", "")
            .replace("{w}", "600").replace("{h}", "600")
        )
    isrc = attrs.get("isrc")
    return _pack(
        artist, title,
        thumbnail=thumb,
        duration=duration,
        isrc=isrc,
        source_label="Apple Music",
    )
