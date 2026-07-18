"""Enumerate multi-item links (playlists, albums, sets) into track entries.

Download still goes through the single-item pipeline per entry. This module
only builds metadata lists for the probe UI.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import audio_common as ac
from .platforms import platform_kind

MAX_PLAYLIST_TRACKS = max(1, int(os.environ.get("MAX_PLAYLIST_TRACKS") or 100))

# YouTube / Vimeo-style quality chips for multi-item video sources.
_PLAYLIST_VIDEO_OPTIONS = [
    {"id": "original", "label": "Original", "detail": "best available · each item",
     "approx_size": None},
    {"id": "1080p", "label": "1080p", "detail": "≤1080p · each item", "approx_size": None},
    {"id": "720p", "label": "720p", "detail": "≤720p · each item", "approx_size": None},
]


class PlaylistError(Exception):
    """User-facing multi-item failure (HTTP 422)."""


def max_tracks() -> int:
    return MAX_PLAYLIST_TRACKS


def enumerate_playlist(url: str, platform: str) -> dict:
    """Return a probe-shaped dict with kind=playlist and entries[]."""
    url = (url or "").strip()
    if platform == "youtube":
        data = _enum_youtube(url)
    elif platform == "soundcloud":
        data = _enum_soundcloud(url)
    elif platform == "spotify":
        data = _enum_spotify(url)
    elif platform == "deezer":
        data = _enum_deezer(url)
    elif platform == "tidal":
        data = _enum_tidal(url)
    elif platform == "applemusic":
        data = _enum_apple(url)
    elif platform == "beatport":
        data = _enum_beatport(url)
    elif platform == "joox":
        data = _enum_joox(url)
    elif platform == "vimeo":
        data = _enum_ytdlp_generic(url, platform)
    else:
        raise PlaylistError("That site doesn't support playlist downloads here.")

    entries = data.get("entries") or []
    if not entries:
        raise PlaylistError("Couldn't find any tracks in that playlist.")

    truncated = bool(data.get("truncated"))
    if len(entries) > MAX_PLAYLIST_TRACKS:
        entries = entries[:MAX_PLAYLIST_TRACKS]
        truncated = True

    # Normalize indices
    for i, e in enumerate(entries, 1):
        e["index"] = i
        e.setdefault("url", None)
        e.setdefault("title", None)
        e.setdefault("uploader", None)
        e.setdefault("duration", None)
        e.setdefault("thumbnail", None)

    kind_media = platform_kind(platform)
    quality = data.get("original_quality") or (
        f"Playlist · {len(entries)} item{'s' if len(entries) != 1 else ''}"
        + (" · truncated" if truncated else "")
    )
    payload = {
        "platform": platform,
        "kind": "playlist",
        "url": data.get("url") or url,
        "title": data.get("title") or "Playlist",
        "uploader": data.get("uploader"),
        "thumbnail": data.get("thumbnail"),
        "duration": None,
        "track_count": len(entries),
        "truncated": truncated,
        "entries": entries,
        "original_quality": quality,
        "video_options": list(_PLAYLIST_VIDEO_OPTIONS) if kind_media == "video" else [],
        "audio_options": ac.audio_options(
            None,
            "per track · best available",
            best_size=None,
        ),
    }
    return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(url: str | None, title: str | None = None, uploader: str | None = None,
           duration: float | None = None, thumbnail: str | None = None) -> dict:
    return {
        "url": url,
        "title": title,
        "uploader": uploader,
        "duration": float(duration) if isinstance(duration, (int, float)) and duration else None,
        "thumbnail": thumbnail,
    }


def _http_json(url: str, headers: dict | None = None, data: bytes | None = None,
               method: str | None = None) -> Any:
    h = {"User-Agent": ac.UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise PlaylistError(f"Couldn't read playlist (HTTP {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise PlaylistError(f"Network error reading playlist: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise PlaylistError("Playlist response wasn't valid JSON.") from exc


def _path_id(url: str, kinds: tuple[str, ...]) -> tuple[str, str] | None:
    """Return (kind, id) for /playlist/ID or /album/ID style paths."""
    path = urllib.parse.urlparse(url).path or ""
    for kind in kinds:
        m = re.search(rf"/{kind}/([A-Za-z0-9._-]+)", path, re.I)
        if m:
            return kind.lower(), m.group(1)
    return None


# ---------------------------------------------------------------------------
# YouTube / yt-dlp generic
# ---------------------------------------------------------------------------

def _enum_youtube(url: str) -> dict:
    return _enum_ytdlp_generic(url, "youtube")


def _enum_ytdlp_generic(url: str, platform: str) -> dict:
    from yt_dlp import YoutubeDL

    from . import downloader

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_TRACKS,
        "noplaylist": False,
        "ignoreerrors": True,
    }
    # Cookies help YouTube age/region-gated playlists.
    if downloader.COOKIES_FILE:
        opts["cookiefile"] = downloader.COOKIES_FILE

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise PlaylistError(f"Couldn't read that playlist: {exc}") from exc

    if not info:
        raise PlaylistError("Couldn't read that playlist.")

    # Single video mistaken as multi — shouldn't happen for /playlist paths.
    if info.get("_type") not in ("playlist", "multi_video") and not info.get("entries"):
        # One entry: still wrap as playlist of 1 for consistent UI if URL shape said multi.
        vid = info.get("id") or info.get("url")
        if platform == "youtube" and vid and not str(vid).startswith("http"):
            entry_url = f"https://www.youtube.com/watch?v={vid}"
        else:
            entry_url = info.get("webpage_url") or info.get("url") or url
        return {
            "url": url,
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "thumbnail": info.get("thumbnail"),
            "entries": [_entry(
                entry_url, info.get("title"),
                info.get("uploader") or info.get("channel"),
                info.get("duration"), info.get("thumbnail"),
            )],
            "truncated": False,
        }

    entries_raw = list(info.get("entries") or [])
    entries: list[dict] = []
    for e in entries_raw:
        if not e:
            continue
        eid = e.get("id")
        eurl = e.get("url") or e.get("webpage_url")
        if platform == "youtube":
            if eid and not (isinstance(eurl, str) and eurl.startswith("http")):
                eurl = f"https://www.youtube.com/watch?v={eid}"
            elif isinstance(eurl, str) and eurl.startswith("http"):
                pass
            elif eid:
                eurl = f"https://www.youtube.com/watch?v={eid}"
        if not eurl:
            continue
        entries.append(_entry(
            eurl,
            e.get("title"),
            e.get("uploader") or e.get("channel") or e.get("artist"),
            e.get("duration"),
            e.get("thumbnail") or (e.get("thumbnails") or [{}])[-1].get("url"),
        ))
        if len(entries) >= MAX_PLAYLIST_TRACKS:
            break

    return {
        "url": info.get("webpage_url") or url,
        "title": info.get("title") or "Playlist",
        "uploader": info.get("uploader") or info.get("channel"),
        "thumbnail": info.get("thumbnail") or (
            (info.get("thumbnails") or [{}])[-1].get("url") if info.get("thumbnails") else None
        ),
        "entries": entries,
        "truncated": len(entries_raw) > len(entries) or len(entries) >= MAX_PLAYLIST_TRACKS,
    }


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

def _enum_soundcloud(url: str) -> dict:
    from . import soundcloud as sc

    cid = sc._get_client_id()  # noqa: SLF001 — shared client_id bootstrap
    info = sc._http_json(  # noqa: SLF001
        f"{sc._API}/resolve?url={urllib.parse.quote(url, safe='')}&client_id={cid}"  # noqa: SLF001
    )
    kind = info.get("kind")
    if kind not in ("playlist", "system-playlist"):
        raise PlaylistError("That SoundCloud link isn't a playlist/set.")

    tracks = list(info.get("tracks") or [])
    # Some sets only return track stubs; fetch full playlist tracks if needed.
    if info.get("id") and (not tracks or any(not t.get("title") for t in tracks[:3])):
        try:
            more = sc._http_json(  # noqa: SLF001
                f"{sc._API}/playlists/{info['id']}/tracks?client_id={cid}&limit={MAX_PLAYLIST_TRACKS}&linked_partitioning=1"
            )
            if isinstance(more, dict) and more.get("collection"):
                tracks = list(more["collection"])
            elif isinstance(more, list):
                tracks = more
        except Exception:
            pass

    entries: list[dict] = []
    for t in tracks:
        if not t or t.get("kind") not in (None, "track"):
            # stubs may lack kind
            if not t.get("id") and not t.get("permalink_url"):
                continue
        purl = t.get("permalink_url") or t.get("uri")
        if purl and str(purl).startswith("soundcloud:tracks:"):
            purl = f"https://soundcloud.com/{t.get('user', {}).get('permalink', 'unknown')}/{t.get('permalink') or t.get('id')}"
        if not purl and t.get("id"):
            # Can't build a reliable URL without permalink — skip
            continue
        if purl and not str(purl).startswith("http"):
            continue
        user = t.get("user") or {}
        dur = t.get("duration")
        if isinstance(dur, (int, float)) and dur > 1000:
            dur = dur / 1000.0
        entries.append(_entry(
            purl,
            t.get("title"),
            user.get("username") or user.get("full_name"),
            dur,
            (t.get("artwork_url") or user.get("avatar_url") or "").replace("-large", "-t500x500") or None,
        ))
        if len(entries) >= MAX_PLAYLIST_TRACKS:
            break

    user = info.get("user") or {}
    art = info.get("artwork_url") or user.get("avatar_url")
    if art:
        art = art.replace("-large", "-t500x500")
    return {
        "url": info.get("permalink_url") or url,
        "title": info.get("title") or "SoundCloud set",
        "uploader": user.get("username") or user.get("full_name"),
        "thumbnail": art,
        "entries": entries,
        "truncated": len(tracks) > len(entries),
    }


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

def _enum_spotify(url: str) -> dict:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "spotify.link" or host.endswith("spotify.link"):
        # Resolve shortlink
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ac.UA}, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as resp:
                url = resp.geturl()
        except Exception:
            try:
                raw = ac.http_get(url, headers={"User-Agent": ac.UA})
                # may not redirect; leave url
                _ = raw
            except Exception as exc:
                raise PlaylistError("Couldn't resolve that Spotify short link.") from exc

    hit = _path_id(url, ("playlist", "album"))
    if not hit:
        raise PlaylistError("Paste a Spotify playlist or album link.")
    kind, sid = hit

    client_id = (os.environ.get("SPOTIFY_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("SPOTIFY_CLIENT_SECRET") or "").strip()
    if client_id and client_secret:
        try:
            return _enum_spotify_api(kind, sid, url, client_id, client_secret)
        except PlaylistError:
            raise
        except Exception:
            pass  # fall through to scrape

    scraped = _enum_spotify_scrape(kind, sid, url)
    if scraped and scraped.get("entries"):
        return scraped
    raise PlaylistError(
        "Couldn't list that Spotify playlist. Set SPOTIFY_CLIENT_ID and "
        "SPOTIFY_CLIENT_SECRET (free Web API app) for reliable album/playlist support."
    )


def _spotify_token(client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    auth = urllib.parse.urlencode({}).encode()  # placate
    _ = auth
    import base64
    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = _http_json(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=body,
        method="POST",
    )
    access = data.get("access_token")
    if not access:
        raise PlaylistError("Spotify client credentials login failed.")
    return access


def _enum_spotify_api(kind: str, sid: str, url: str, client_id: str, client_secret: str) -> dict:
    access = _spotify_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {access}"}
    if kind == "playlist":
        meta = _http_json(f"https://api.spotify.com/v1/playlists/{sid}", headers=headers)
        title = meta.get("name")
        owner = (meta.get("owner") or {}).get("display_name")
        images = meta.get("images") or []
        thumb = images[0]["url"] if images else None
        entries: list[dict] = []
        next_url = f"https://api.spotify.com/v1/playlists/{sid}/tracks?limit=50"
        while next_url and len(entries) < MAX_PLAYLIST_TRACKS:
            page = _http_json(next_url, headers=headers)
            for item in page.get("items") or []:
                tr = item.get("track") or {}
                if not tr or tr.get("is_local") or not tr.get("id"):
                    continue
                artists = ", ".join(
                    a.get("name") for a in (tr.get("artists") or []) if a.get("name")
                )
                al_imgs = (tr.get("album") or {}).get("images") or []
                entries.append(_entry(
                    tr.get("external_urls", {}).get("spotify")
                    or f"https://open.spotify.com/track/{tr['id']}",
                    tr.get("name"),
                    artists or None,
                    (tr.get("duration_ms") or 0) / 1000.0 or None,
                    al_imgs[0]["url"] if al_imgs else None,
                ))
                if len(entries) >= MAX_PLAYLIST_TRACKS:
                    break
            next_url = page.get("next")
        total = (meta.get("tracks") or {}).get("total") or len(entries)
        return {
            "url": meta.get("external_urls", {}).get("spotify") or url,
            "title": title,
            "uploader": owner,
            "thumbnail": thumb,
            "entries": entries,
            "truncated": total > len(entries),
        }

    # album
    meta = _http_json(f"https://api.spotify.com/v1/albums/{sid}", headers=headers)
    title = meta.get("name")
    artists = ", ".join(
        a.get("name") for a in (meta.get("artists") or []) if a.get("name")
    )
    images = meta.get("images") or []
    thumb = images[0]["url"] if images else None
    entries = []
    next_url = f"https://api.spotify.com/v1/albums/{sid}/tracks?limit=50"
    while next_url and len(entries) < MAX_PLAYLIST_TRACKS:
        page = _http_json(next_url, headers=headers)
        for tr in page.get("items") or []:
            if not tr.get("id"):
                continue
            tr_artists = ", ".join(
                a.get("name") for a in (tr.get("artists") or []) if a.get("name")
            ) or artists
            entries.append(_entry(
                tr.get("external_urls", {}).get("spotify")
                or f"https://open.spotify.com/track/{tr['id']}",
                tr.get("name"),
                tr_artists or None,
                (tr.get("duration_ms") or 0) / 1000.0 or None,
                thumb,
            ))
            if len(entries) >= MAX_PLAYLIST_TRACKS:
                break
        next_url = page.get("next")
    total = (meta.get("tracks") or {}).get("total") or len(entries)
    return {
        "url": meta.get("external_urls", {}).get("spotify") or url,
        "title": title,
        "uploader": artists or None,
        "thumbnail": thumb,
        "entries": entries,
        "truncated": total > len(entries),
    }


def _enum_spotify_scrape(kind: str, sid: str, url: str) -> dict | None:
    """Best-effort: embed page __NEXT_DATA__ / oEmbed title only."""
    entries: list[dict] = []
    title = None
    thumb = None
    uploader = None
    try:
        embed = ac.http_get(
            f"https://open.spotify.com/embed/{kind}/{sid}",
            headers={"User-Agent": ac.UA, "Accept-Language": "en"},
        ).decode("utf-8", "replace")
    except Exception:
        embed = ""
    if embed:
        m = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            embed, re.DOTALL,
        )
        if m:
            try:
                blob = json.loads(m.group(1))
                entries, title, uploader, thumb = _spotify_walk_embed(blob, kind)
            except Exception:
                pass
        if not title:
            om = re.search(r'property="og:title"\s+content="([^"]+)"', embed)
            if om:
                title = om.group(1)
        if not thumb:
            om = re.search(r'property="og:image"\s+content="([^"]+)"', embed)
            if om:
                thumb = om.group(1)

    if not entries:
        # oEmbed gives title/thumb only — not enough for multi download
        try:
            oe = _http_json(
                "https://open.spotify.com/oembed?" + urllib.parse.urlencode({"url": url})
            )
            title = title or oe.get("title")
            thumb = thumb or oe.get("thumbnail_url")
        except Exception:
            pass
        return None

    return {
        "url": url,
        "title": title or ("Album" if kind == "album" else "Playlist"),
        "uploader": uploader,
        "thumbnail": thumb,
        "entries": entries[:MAX_PLAYLIST_TRACKS],
        "truncated": len(entries) > MAX_PLAYLIST_TRACKS,
    }


def _spotify_walk_embed(node: Any, kind: str) -> tuple[list[dict], str | None, str | None, str | None]:
    entries: list[dict] = []
    title = None
    uploader = None
    thumb = None
    seen: set[str] = set()

    def walk(n: Any) -> None:
        nonlocal title, uploader, thumb
        if isinstance(n, dict):
            # entity list items
            eid = n.get("id")
            name = n.get("name")
            artists = n.get("artists")
            if (
                isinstance(artists, list)
                and artists
                and isinstance(artists[0], dict)
                and artists[0].get("name")
                and eid
                and name
                and len(str(eid)) >= 10
            ):
                # Heuristic: track-like (has duration or album or uri contains track)
                uri = str(n.get("uri") or "")
                dur = n.get("duration") or n.get("duration_ms")
                if "track" in uri or dur or n.get("album"):
                    tid = str(eid)
                    if tid not in seen and "playlist" not in uri and "album" not in uri.split(":")[-2:-1]:
                        if "album" in uri and "track" not in uri:
                            pass
                        else:
                            seen.add(tid)
                            if isinstance(dur, (int, float)) and dur > 1000:
                                dur = dur / 1000.0
                            artist = ", ".join(
                                a.get("name") for a in artists if isinstance(a, dict) and a.get("name")
                            )
                            entries.append(_entry(
                                f"https://open.spotify.com/track/{tid}",
                                str(name),
                                artist or None,
                                float(dur) if isinstance(dur, (int, float)) else None,
                                None,
                            ))
            # playlist/album title
            if n.get("type") in ("playlist", "album") and n.get("name") and not title:
                title = str(n["name"])
                if n.get("type") == "playlist":
                    owner = n.get("owner") or {}
                    uploader = owner.get("display_name") or owner.get("name")
                elif n.get("type") == "album":
                    al_artists = n.get("artists") or []
                    uploader = ", ".join(
                        a.get("name") for a in al_artists if isinstance(a, dict) and a.get("name")
                    ) or uploader
                images = n.get("images") or []
                if images and isinstance(images[0], dict):
                    thumb = images[0].get("url") or thumb
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return entries, title, uploader, thumb


# ---------------------------------------------------------------------------
# Deezer
# ---------------------------------------------------------------------------

def _enum_deezer(url: str) -> dict:
    hit = _path_id(url, ("playlist", "album"))
    if not hit:
        raise PlaylistError("Paste a Deezer playlist or album link.")
    kind, did = hit
    # strip locale prefix ids already handled by regex on path

    meta = _http_json(f"https://api.deezer.com/{kind}/{did}")
    if meta.get("error"):
        raise PlaylistError(meta["error"].get("message") or "Deezer playlist not found.")

    title = meta.get("title")
    if kind == "playlist":
        uploader = (meta.get("creator") or {}).get("name")
    else:
        uploader = (meta.get("artist") or {}).get("name")
    thumb = meta.get("picture_xl") or meta.get("picture_big") or meta.get("cover_xl")

    entries: list[dict] = []
    next_url = f"https://api.deezer.com/{kind}/{did}/tracks?limit=50"
    total = meta.get("nb_tracks") or 0
    while next_url and len(entries) < MAX_PLAYLIST_TRACKS:
        page = _http_json(next_url)
        for tr in page.get("data") or []:
            tid = tr.get("id")
            if not tid:
                continue
            artist = (tr.get("artist") or {}).get("name")
            entries.append(_entry(
                tr.get("link") or f"https://www.deezer.com/track/{tid}",
                tr.get("title"),
                artist,
                tr.get("duration"),
                (tr.get("album") or {}).get("cover_xl") or thumb,
            ))
            if len(entries) >= MAX_PLAYLIST_TRACKS:
                break
        next_url = page.get("next")

    return {
        "url": meta.get("link") or url,
        "title": title,
        "uploader": uploader,
        "thumbnail": thumb,
        "entries": entries,
        "truncated": (total or 0) > len(entries),
    }


# ---------------------------------------------------------------------------
# TIDAL
# ---------------------------------------------------------------------------

def _enum_tidal(url: str) -> dict:
    path = urllib.parse.urlparse(url).path or ""
    # /playlist/uuid or /album/123
    m = re.search(r"/playlist/([0-9a-fA-F-]{16,})", path)
    kind = None
    tid = None
    if m:
        kind, tid = "playlist", m.group(1)
    else:
        m = re.search(r"/album/(\d+)", path)
        if m:
            kind, tid = "album", m.group(1)
    if not kind:
        raise PlaylistError("Paste a TIDAL playlist or album link.")

    country = os.environ.get("TIDAL_COUNTRY_CODE") or "US"
    client_id = os.environ.get("TIDAL_CLIENT_ID") or "fX2JxdmntZWK0ixT"
    access = os.environ.get("TIDAL_ACCESS_TOKEN") or ""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "X-Tidal-Token": client_id,
    }
    if access:
        headers["Authorization"] = f"Bearer {access}"

    if kind == "playlist":
        meta = _http_json(
            f"https://api.tidal.com/v1/playlists/{tid}?countryCode={country}",
            headers=headers,
        )
        title = meta.get("title")
        uploader = (meta.get("creator") or {}).get("name")
        sq = meta.get("squareImage") or meta.get("image")
        thumb = f"https://resources.tidal.com/images/{str(sq).replace('-', '/')}/640x640.jpg" if sq else None
        entries = []
        offset = 0
        total = meta.get("numberOfTracks") or 0
        while len(entries) < MAX_PLAYLIST_TRACKS:
            page = _http_json(
                f"https://api.tidal.com/v1/playlists/{tid}/items"
                f"?countryCode={country}&limit=50&offset={offset}",
                headers=headers,
            )
            items = page.get("items") or []
            if not items:
                break
            for item in items:
                tr = item.get("item") or item
                if tr.get("type") and tr.get("type") != "track":
                    tr = item.get("item") or tr
                if not tr.get("id"):
                    continue
                artists = ", ".join(
                    a.get("name") for a in (tr.get("artists") or []) if a.get("name")
                )
                entries.append(_entry(
                    f"https://tidal.com/browse/track/{tr['id']}",
                    tr.get("title"),
                    artists or None,
                    tr.get("duration"),
                    None,
                ))
                if len(entries) >= MAX_PLAYLIST_TRACKS:
                    break
            offset += len(items)
            if offset >= (page.get("totalNumberOfItems") or total or 0):
                break
        return {
            "url": url,
            "title": title,
            "uploader": uploader,
            "thumbnail": thumb,
            "entries": entries,
            "truncated": (total or 0) > len(entries),
        }

    # album
    meta = _http_json(
        f"https://api.tidal.com/v1/albums/{tid}?countryCode={country}",
        headers=headers,
    )
    title = meta.get("title")
    artists = ", ".join(
        a.get("name") for a in (meta.get("artists") or []) if a.get("name")
    )
    cover = meta.get("cover")
    thumb = (
        f"https://resources.tidal.com/images/{str(cover).replace('-', '/')}/640x640.jpg"
        if cover else None
    )
    entries = []
    offset = 0
    total = meta.get("numberOfTracks") or 0
    while len(entries) < MAX_PLAYLIST_TRACKS:
        page = _http_json(
            f"https://api.tidal.com/v1/albums/{tid}/items"
            f"?countryCode={country}&limit=50&offset={offset}",
            headers=headers,
        )
        items = page.get("items") or []
        if not items:
            break
        for item in items:
            tr = item.get("item") or item
            if not tr.get("id"):
                continue
            tr_artists = ", ".join(
                a.get("name") for a in (tr.get("artists") or []) if a.get("name")
            ) or artists
            entries.append(_entry(
                f"https://tidal.com/browse/track/{tr['id']}",
                tr.get("title"),
                tr_artists or None,
                tr.get("duration"),
                thumb,
            ))
            if len(entries) >= MAX_PLAYLIST_TRACKS:
                break
        offset += len(items)
        if offset >= (page.get("totalNumberOfItems") or total or 0):
            break
    return {
        "url": url,
        "title": title,
        "uploader": artists or None,
        "thumbnail": thumb,
        "entries": entries,
        "truncated": (total or 0) > len(entries),
    }


# ---------------------------------------------------------------------------
# Apple Music
# ---------------------------------------------------------------------------

def _enum_apple(url: str) -> dict:
    from . import applemusic as am

    parsed = urllib.parse.urlparse(url)
    parts = [p for p in (parsed.path or "").split("/") if p]
    storefront = parts[0] if parts and len(parts[0]) == 2 else "us"
    path = parsed.path or ""

    kind = None
    aid = None
    if "/playlist/" in path:
        kind = "playlists"
        m = re.search(r"/playlist/[^/]+/(pl\.[A-Za-z0-9.-]+)", path)
        if not m:
            m = re.search(r"/(pl\.[A-Za-z0-9.-]+)", path)
        if m:
            aid = m.group(1)
    elif "/album/" in path:
        kind = "albums"
        m = re.search(r"/album/[^/]+/(\d+)", path)
        if m:
            aid = m.group(1)

    if not kind or not aid:
        raise PlaylistError("Paste an Apple Music album or playlist link.")

    try:
        dev = am._developer_token()  # noqa: SLF001
    except Exception as exc:
        raise PlaylistError("Couldn't reach Apple Music catalog.") from exc

    headers = {
        "User-Agent": ac.UA,
        "Authorization": f"Bearer {dev}",
        "Accept": "application/json",
        "Origin": "https://music.apple.com",
        "Referer": "https://music.apple.com/",
    }
    include = "tracks" if kind == "albums" else "tracks"
    meta = _http_json(
        f"https://amp-api.music.apple.com/v1/catalog/{storefront}/{kind}/{aid}"
        f"?l=en-US&include={include}",
        headers=headers,
    )
    data = (meta.get("data") or [{}])[0]
    attrs = data.get("attributes") or {}
    title = attrs.get("name")
    uploader = attrs.get("artistName") or attrs.get("curatorName")
    artwork = (attrs.get("artwork") or {}).get("url")
    if artwork:
        artwork = artwork.replace("{w}", "600").replace("{h}", "600").replace("{f}", "jpg")

    entries: list[dict] = []
    # relationships.tracks.data or included
    rel = ((data.get("relationships") or {}).get("tracks") or {}).get("data") or []
    if not rel and meta.get("included"):
        rel = [x for x in meta["included"] if x.get("type") == "songs"]

    # Paginate tracks if href present
    track_href = ((data.get("relationships") or {}).get("tracks") or {}).get("href")
    pages = 0
    while True:
        for tr in rel:
            tid = tr.get("id")
            tattrs = tr.get("attributes") or {}
            if not tid:
                continue
            # album track link with ?i=
            if kind == "albums":
                turl = f"https://music.apple.com/{storefront}/album/{aid}?i={tid}"
            else:
                turl = (
                    tattrs.get("url")
                    or f"https://music.apple.com/{storefront}/song/x/{tid}"
                )
            entries.append(_entry(
                turl,
                tattrs.get("name"),
                tattrs.get("artistName") or uploader,
                tattrs.get("durationInMillis", 0) / 1000.0 or None,
                artwork,
            ))
            if len(entries) >= MAX_PLAYLIST_TRACKS:
                break
        if len(entries) >= MAX_PLAYLIST_TRACKS or not track_href or pages > 20:
            break
        # next page
        next_meta = _http_json(
            track_href if track_href.startswith("http")
            else f"https://amp-api.music.apple.com{track_href}",
            headers=headers,
        )
        rel = next_meta.get("data") or []
        track_href = (next_meta.get("next") or
                      ((next_meta.get("meta") or {}).get("next")))
        pages += 1
        if not rel:
            break

    # If album list empty, try /tracks endpoint
    if not entries and kind == "albums":
        page = _http_json(
            f"https://amp-api.music.apple.com/v1/catalog/{storefront}/albums/{aid}/tracks?l=en-US&limit=100",
            headers=headers,
        )
        for tr in page.get("data") or []:
            tid = tr.get("id")
            tattrs = tr.get("attributes") or {}
            if not tid:
                continue
            entries.append(_entry(
                f"https://music.apple.com/{storefront}/album/{aid}?i={tid}",
                tattrs.get("name"),
                tattrs.get("artistName") or uploader,
                tattrs.get("durationInMillis", 0) / 1000.0 or None,
                artwork,
            ))
            if len(entries) >= MAX_PLAYLIST_TRACKS:
                break

    return {
        "url": attrs.get("url") or url,
        "title": title,
        "uploader": uploader,
        "thumbnail": artwork,
        "entries": entries,
        "truncated": len(entries) >= MAX_PLAYLIST_TRACKS,
    }


# ---------------------------------------------------------------------------
# Beatport release / chart
# ---------------------------------------------------------------------------

def _enum_beatport(url: str) -> dict:
    path = (urllib.parse.urlparse(url).path or "").lower()
    if any(x in path for x in ("/label/", "/genre/")):
        raise PlaylistError(
            "Beatport label/genre pages aren't supported — paste a release or chart link."
        )
    if not any(x in path for x in ("/release/", "/chart/", "/playlist/")):
        raise PlaylistError("Paste a Beatport release, chart, or playlist link.")

    # Reuse cloudscraper / jina path similar to track scrape
    from . import beatport as bp

    html = None
    errors: list[str] = []
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )
        resp = scraper.get(url, timeout=45)
        if resp.status_code == 200:
            html = resp.text
        else:
            errors.append(f"HTTP {resp.status_code}")
    except Exception as exc:
        errors.append(str(exc))

    if not html:
        try:
            raw = ac.http_get(
                "https://r.jina.ai/" + url,
                headers={"User-Agent": ac.UA, "Accept": "text/html"},
            )
            html = raw.decode("utf-8", "replace")
        except Exception as exc:
            errors.append(f"jina: {exc}")

    if not html:
        raise PlaylistError(
            "Couldn't read that Beatport page"
            + (f" ({'; '.join(errors[:2])})" if errors else ".")
        )

    tracks: list[dict] = []
    title = None
    thumb = None

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if m:
        try:
            blob = json.loads(m.group(1))
            tracks, title, thumb = _beatport_walk(blob)
        except Exception:
            pass

    if not tracks:
        # Fallback: track links in HTML
        for tm in re.finditer(
            r"https?://(?:www\.)?beatport\.com/track/([^/\"'?]+)/(\d+)", html
        ):
            slug, tid = tm.group(1), tm.group(2)
            turl = f"https://www.beatport.com/track/{slug}/{tid}"
            if any(e.get("url") == turl for e in tracks):
                continue
            tracks.append({"url": turl, "title": slug.replace("-", " "), "id": tid})
            if len(tracks) >= MAX_PLAYLIST_TRACKS:
                break

    entries = []
    for t in tracks[:MAX_PLAYLIST_TRACKS]:
        turl = t.get("url")
        if not turl and t.get("id"):
            slug = t.get("slug") or "track"
            turl = f"https://www.beatport.com/track/{slug}/{t['id']}"
        if not turl:
            continue
        artists = t.get("artists")
        if isinstance(artists, list):
            uploader = ", ".join(
                a.get("name") if isinstance(a, dict) else str(a) for a in artists
            )
        else:
            uploader = t.get("uploader") or t.get("artist")
        dur = t.get("length_ms") or t.get("duration")
        if isinstance(dur, (int, float)) and dur > 1000:
            dur = dur / 1000.0
        name = t.get("name") or t.get("title")
        mix = t.get("mix_name") or t.get("mix")
        if name and mix and mix.lower() not in str(name).lower():
            name = f"{name} ({mix})"
        entries.append(_entry(turl, name, uploader, dur, t.get("thumbnail") or thumb))

    if not entries:
        raise PlaylistError("No tracks found on that Beatport page.")

    return {
        "url": url,
        "title": title or "Beatport release",
        "uploader": None,
        "thumbnail": thumb,
        "entries": entries,
        "truncated": len(tracks) > len(entries),
    }


def _beatport_walk(node: Any) -> tuple[list[dict], str | None, str | None]:
    tracks: list[dict] = []
    title = None
    thumb = None
    seen: set[str] = set()

    def walk(n: Any) -> None:
        nonlocal title, thumb
        if isinstance(n, dict):
            # release-like
            if n.get("name") and (n.get("tracks") or n.get("track_count")) and not title:
                if "catalog_number" in n or "label" in n or n.get("track_count"):
                    title = str(n.get("name"))
                    img = n.get("image") or {}
                    if isinstance(img, dict):
                        thumb = img.get("uri") or img.get("url") or thumb
            # track-like
            tid = n.get("id")
            tname = n.get("name")
            if tid and tname and ("mix_name" in n or "artists" in n or "isrc" in n):
                sid = str(tid)
                if sid not in seen and (
                    isinstance(tid, int)
                    or (isinstance(tid, str) and tid.isdigit())
                ):
                    seen.add(sid)
                    slug = n.get("slug") or re.sub(r"[^a-z0-9]+", "-", str(tname).lower()).strip("-")
                    tracks.append({
                        "id": sid,
                        "slug": slug,
                        "url": f"https://www.beatport.com/track/{slug}/{sid}",
                        "name": tname,
                        "mix_name": n.get("mix_name") or n.get("mix"),
                        "artists": n.get("artists"),
                        "length_ms": n.get("length_ms"),
                    })
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return tracks, title, thumb


# ---------------------------------------------------------------------------
# JOOX
# ---------------------------------------------------------------------------

def _enum_joox(url: str) -> dict:
    raise PlaylistError(
        "JOOX playlists/albums aren't supported yet — paste a single track link."
    )


__all__ = [
    "PlaylistError",
    "enumerate_playlist",
    "max_tracks",
    "MAX_PLAYLIST_TRACKS",
]
