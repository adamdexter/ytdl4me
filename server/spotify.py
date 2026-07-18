"""Spotify link resolution.

Spotify streams are DRM-protected, so (like spotDL) we read the track's public
metadata — oEmbed + og/ld+json tags from the page — and later download the best
matching audio from YouTube via a `ytsearch1:` query.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from urllib.parse import urlparse

import httpx

PLAYLIST_ERROR = (
    "Playlists aren't supported yet — paste a link to a single video/track."
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SpotifyError(Exception):
    """str(exc) is a user-facing message (HTTP 422)."""


class SpotifyPlaylistError(SpotifyError):
    def __init__(self, message: str = PLAYLIST_ERROR) -> None:
        super().__init__(message)


async def resolve_track(url: str) -> dict:
    """Resolve a Spotify track link to
    {"artist", "title", "thumbnail", "duration", "search_query"}."""
    result: dict = {"artist": None, "title": None, "thumbnail": None, "duration": None}
    page: str | None = None

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15.0,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
    ) as client:
        track_url = url
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "spotify.link":
            # Shortlinks redirect to the real track page; keep the body we got.
            try:
                resp = await client.get(url)
                track_url = str(resp.url)
                if resp.status_code == 200:
                    page = resp.text
            except httpx.HTTPError as exc:
                raise SpotifyError("Couldn't resolve that Spotify short link.") from exc
            # Only trust redirects that land back on Spotify itself.
            final_host = (urlparse(track_url).hostname or "").lower()
            if final_host.startswith("www."):
                final_host = final_host[4:]
            if final_host != "open.spotify.com":
                raise SpotifyError("That short link doesn't lead to a Spotify track.")

        _check_track_path(track_url)

        try:
            resp = await client.get(
                "https://open.spotify.com/oembed", params={"url": track_url}
            )
            data = resp.json()
            result["title"] = data.get("title") or None
            result["thumbnail"] = data.get("thumbnail_url") or None
        except Exception:
            pass

        if page is None:
            try:
                resp = await client.get(track_url)
                page = resp.text
            except httpx.HTTPError:
                page = None
        if page:
            _parse_page(page, result)

        if not result["artist"] or not result["duration"]:
            # open.spotify.com often serves a JS challenge page to non-browser
            # clients; the embed page still exposes full metadata as JSON.
            await _fill_from_embed(client, track_url, result)

    if not result["title"]:
        raise SpotifyError(
            "Couldn't read that Spotify link — is it a public track?"
        )
    if result["artist"]:
        result["search_query"] = f"{result['artist']} - {result['title']}"
    else:
        result["search_query"] = result["title"]
    return result


async def _fill_from_embed(client: httpx.AsyncClient, track_url: str, result: dict) -> None:
    """Best-effort: read artist/duration/title from the embed page's JSON blob."""
    try:
        m = re.search(r"/track/([A-Za-z0-9]+)", urlparse(track_url).path)
        if not m:
            return
        resp = await client.get(f"https://open.spotify.com/embed/track/{m.group(1)}")
        blob = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if not blob:
            return
        entity = _find_track_entity(json.loads(blob.group(1)))
        if not entity:
            return
        if not result["artist"]:
            artists = entity.get("artists") or []
            names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
            if names:
                result["artist"] = ", ".join(names)
        if not result["duration"]:
            duration = entity.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                # entity durations are always in milliseconds
                result["duration"] = duration / 1000
        if not result["title"] and entity.get("name"):
            result["title"] = str(entity["name"])
    except Exception:
        pass


def _find_track_entity(node) -> dict | None:
    """Depth-first search for a dict that looks like a track entity."""
    if isinstance(node, dict):
        artists = node.get("artists")
        if (
            isinstance(artists, list)
            and artists
            and isinstance(artists[0], dict)
            and artists[0].get("name")
        ):
            return node
        for value in node.values():
            found = _find_track_entity(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_track_entity(value)
            if found:
                return found
    return None


def _check_track_path(url: str) -> None:
    path = (urlparse(url).path or "").lower()
    if "/album/" in path or "/playlist/" in path:
        raise SpotifyPlaylistError()
    if "/track/" not in path:
        raise SpotifyError("Only Spotify track links are supported.")


def _parse_page(page: str, result: dict) -> None:
    """Fill missing fields from og tags / ld+json. Every step is best-effort."""
    if not result["title"]:
        result["title"] = _meta_content(page, "property", "og:title")
    if not result["thumbnail"]:
        result["thumbnail"] = _meta_content(page, "property", "og:image")

    ld = _parse_ld_json(page)
    if not result["artist"] and ld.get("artist"):
        result["artist"] = ld["artist"]
    if not result["duration"] and ld.get("duration"):
        result["duration"] = ld["duration"]
    if not result["title"] and ld.get("title"):
        result["title"] = ld["title"]

    if not result["duration"]:
        for attr in ("property", "name"):
            raw = _meta_content(page, attr, "music:duration")
            if raw and raw.isdigit():
                result["duration"] = float(raw)
                break

    if not result["artist"]:
        result["artist"] = _artist_from_description(page)


def _meta_content(page: str, attr: str, value: str) -> str | None:
    for pattern in (
        rf'<meta[^>]+{attr}=["\']{re.escape(value)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{attr}=["\']{re.escape(value)}["\']',
    ):
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            content = html_lib.unescape(m.group(1)).strip()
            if content:
                return content
    return None


def _parse_ld_json(page: str) -> dict:
    out: dict = {}
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page,
        re.DOTALL | re.IGNORECASE,
    )
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            by_artist = item.get("byArtist")
            if isinstance(by_artist, list) and by_artist:
                by_artist = by_artist[0]
            if isinstance(by_artist, dict) and by_artist.get("name"):
                out.setdefault("artist", str(by_artist["name"]))
            elif isinstance(by_artist, str) and by_artist:
                out.setdefault("artist", by_artist)
            duration = _parse_iso_duration(item.get("duration"))
            if duration:
                out.setdefault("duration", duration)
            if item.get("name") and item.get("@type") in ("MusicRecording", "MusicComposition"):
                out.setdefault("title", str(item["name"]))
    return out


def _parse_iso_duration(value) -> float | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value.strip())
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = m.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def _artist_from_description(page: str) -> str | None:
    """Spotify descriptions look like "Artist · Song · 2015"."""
    desc = _meta_content(page, "property", "og:description") or _meta_content(
        page, "name", "description"
    )
    if not desc:
        return None
    if desc.lower().startswith("listen to"):
        # "Listen to <song> on Spotify. Artist · Song · 2015."
        _, _, rest = desc.partition(". ")
        desc = rest or desc
    if "·" not in desc:
        return None
    first = desc.split("·")[0].strip().rstrip(".")
    if first and "listen" not in first.lower() and len(first) < 100:
        return first
    return None
