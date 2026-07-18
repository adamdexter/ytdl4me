"""JOOX track resolve + direct CDN URL download."""
from __future__ import annotations

import json
import os
import re
import urllib.parse

from . import audio_common as ac
from .jobs import JobStore

# Guest session used by public web clients (same approach as musicdl).
_DEFAULT_COOKIE = (
    "wmid=142420656; user_type=1; country=id; "
    "session_key=2a5d97d05dc8fe238150184eaf3519ad;"
)
_HEADERS = {
    "User-Agent": ac.UA,
    "Cookie": os.environ.get("JOOX_COOKIE") or _DEFAULT_COOKIE,
    "X-Forwarded-For": os.environ.get("JOOX_XFF") or "36.73.34.109",
}

# Prefer higher quality URL keys first.
_URL_KEYS = (
    "r320Url", "r320url", "320Url", "mp3Url",
    "r192Url", "r192url", "192Url",
    "m4aUrl", "r128Url", "r128url", "128Url",
    "r96Url", "96Url",
)


class JooxError(Exception):
    pass


def _song_id_from_url(url: str) -> str:
    # https://www.joox.com/…/single/… or track id in query
    for pat in (
        r"/single/([A-Za-z0-9_-]+)",
        r"/track/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
        r"/song/([A-Za-z0-9_-]+)",
    ):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    # path last segment
    path = urllib.parse.urlparse(url).path.strip("/").split("/")
    if path and path[-1] and path[-1] not in ("intl", "hk", "id", "th", "my"):
        return path[-1]
    raise JooxError("Couldn't find a JOOX track id in that link.")


def _songinfo(song_id: str, lang: str = "en_US", country: str = "hk") -> dict:
    qs = urllib.parse.urlencode({
        "songid": song_id, "lang": lang, "country": country,
    })
    raw = ac.http_get(
        f"https://api.joox.com/web-fcgi-bin/web_get_songinfo?{qs}",
        headers=_HEADERS,
    ).decode("utf-8", "replace")
    # MusicInfoCallback({...});
    raw = raw.strip()
    if raw.startswith("MusicInfoCallback("):
        raw = raw[len("MusicInfoCallback("):]
        if raw.endswith(");"):
            raw = raw[:-2]
        elif raw.endswith(")"):
            raw = raw[:-1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JooxError("JOOX returned unreadable track metadata.") from exc


def _best_url(info: dict) -> tuple[str, str, int | None]:
    for key in _URL_KEYS:
        url = info.get(key)
        if url and str(url).startswith("http"):
            abr = None
            m = re.search(r"(\d{2,3})", key)
            if m:
                abr = int(m.group(1))
            ext = "m4a" if "m4a" in key.lower() else "mp3"
            if ".m4a" in url.split("?", 1)[0]:
                ext = "m4a"
            elif ".mp3" in url.split("?", 1)[0]:
                ext = "mp3"
            return url, ext, abr
    # fuzzy scan
    for k, v in info.items():
        if not isinstance(v, str) or not v.startswith("http"):
            continue
        if any(x in k.lower() for x in ("url", "src")) and "img" not in k.lower():
            ext = "m4a" if ".m4a" in v else "mp3"
            return v, ext, None
    raise JooxError(
        "No downloadable stream for that JOOX track "
        "(it may need a regional session — set JOOX_COOKIE)."
    )


def probe(url: str) -> dict:
    sid = _song_id_from_url(url)
    info = _songinfo(sid)
    if not info or info.get("code") not in (None, 0, "0"):
        # still try if song name present
        if not info.get("msong") and not info.get("songname"):
            raise JooxError("Couldn't read that JOOX track.")
    title = info.get("msong") or info.get("songname") or info.get("song_name")
    artist = info.get("msinger") or info.get("singername") or info.get("singer_name")
    duration = None
    if info.get("minterval"):
        try:
            duration = float(info["minterval"])
        except (TypeError, ValueError):
            pass
    try:
        _, ext, abr = _best_url(info)
        quality = f"{ext.upper()} ~{abr} kbps" if abr else ext.upper()
        best_size = int(duration * abr * 125) if duration and abr else None
    except JooxError:
        quality = None
        best_size = None
    return ac.probe_payload(
        platform="joox",
        url=url,
        title=title,
        uploader=artist,
        duration=duration,
        thumbnail=info.get("imgSrc") or info.get("imgSrcs"),
        quality=quality,
        best_size=best_size,
    )


def run_download(store: JobStore, job_id: str, url: str, option_id: str,
                 job_dir: str, filename_stem: str | None = None) -> None:
    if option_id not in ac.AUDIO_OPTION_IDS:
        raise JooxError(f"Unknown option '{option_id}'.")
    store.update(job_id, status="downloading", progress=5.0)
    sid = _song_id_from_url(url)
    info = _songinfo(sid)
    stream_url, ext, abr = _best_url(info)
    store.update(job_id, progress=20.0)
    data = ac.http_get(stream_url, headers={"User-Agent": ac.UA}, timeout=120)
    raw = os.path.join(job_dir, f"raw.{ext}")
    open(raw, "wb").write(data)
    store.update(job_id, progress=80.0, downloaded_bytes=len(data))

    title = info.get("msong") or info.get("songname")
    artist = info.get("msinger") or info.get("singername")
    stem = filename_stem or ac.stem_for(title, artist, f"joox-{sid}")
    thumb = ac.fetch_thumb(info.get("imgSrc"), job_dir)
    ac.finalize_audio(
        store, job_id, job_dir, raw, f".{ext}", option_id, stem,
        title=title, artist=artist, thumb_path=thumb,
    )
