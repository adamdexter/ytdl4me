"""TIDAL track download (AES-CTR when encryptionKey present).

Requires a logged-in session via env:
  TIDAL_ACCESS_TOKEN   (required for streams)
  TIDAL_CLIENT_ID      (default: Android TV client)
  TIDAL_COUNTRY_CODE   (default: US)
  TIDAL_REFRESH_TOKEN  (optional; used to refresh access)
  TIDAL_CLIENT_SECRET  (optional; for refresh)
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util import Counter

from . import audio_common as ac
from .jobs import JobStore

_DEFAULT_CLIENT_ID = "fX2JxdmntZWK0ixT"
_DEFAULT_CLIENT_SECRET = "1Nm5AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg="
_MASTER_KEY = base64.b64decode("UIlTTEMmmLfGowo/UC60x2H45W6MdGgTRfo/umg4754=")
_API = "https://api.tidal.com/v1"
_AUTH = "https://auth.tidal.com/v1"

# Prefer higher quality first
_QUALITIES = ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW")


class TidalError(Exception):
    pass


def _cfg() -> dict:
    return {
        "access": os.environ.get("TIDAL_ACCESS_TOKEN") or "",
        "refresh": os.environ.get("TIDAL_REFRESH_TOKEN") or "",
        "client_id": os.environ.get("TIDAL_CLIENT_ID") or _DEFAULT_CLIENT_ID,
        "client_secret": os.environ.get("TIDAL_CLIENT_SECRET") or _DEFAULT_CLIENT_SECRET,
        "country": os.environ.get("TIDAL_COUNTRY_CODE") or "US",
    }


def _headers(token: str, client_id: str) -> dict:
    return {
        "User-Agent": "TIDAL_ANDROID/1039 okhttp/3.14.9",
        "Authorization": f"Bearer {token}",
        "X-Tidal-Token": client_id,
        "Accept": "application/json",
    }


def _api_get(path: str, cfg: dict, params: dict | None = None) -> dict:
    if not cfg["access"]:
        raise TidalError(
            "TIDAL needs a session. Set TIDAL_ACCESS_TOKEN "
            "(and optionally TIDAL_REFRESH_TOKEN / TIDAL_COUNTRY_CODE)."
        )
    q = {"countryCode": cfg["country"]}
    if params:
        q.update(params)
    url = f"{_API}/{path.lstrip('/')}?{urllib.parse.urlencode(q)}"
    try:
        raw = ac.http_get(url, headers=_headers(cfg["access"], cfg["client_id"]))
    except RuntimeError as exc:
        if "401" in str(exc) and cfg["refresh"]:
            cfg["access"] = _refresh(cfg)
            raw = ac.http_get(url, headers=_headers(cfg["access"], cfg["client_id"]))
        else:
            raise TidalError(str(exc)) from exc
    return json.loads(raw)


def _refresh(cfg: dict) -> str:
    data = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh"],
    }).encode()
    raw = ac.http_get(
        f"{_AUTH}/oauth2/token",
        data=data,
        headers={
            "User-Agent": ac.UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    body = json.loads(raw)
    token = body.get("access_token")
    if not token:
        raise TidalError("TIDAL token refresh failed.")
    return token


def _track_id_from_url(url: str) -> str:
    m = re.search(r"/track[s]?/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=(\d+)", url)
    if m:
        return m.group(1)
    raise TidalError("Couldn't find a TIDAL track id in that link.")


def _decrypt_security_token(security_token: str) -> tuple[bytes, bytes]:
    token = base64.b64decode(security_token)
    iv, encrypted = token[:16], token[16:]
    plain = AES.new(_MASTER_KEY, AES.MODE_CBC, iv).decrypt(encrypted)
    return plain[:16], plain[16:24]


def _decrypt_file(src: str, dst: str, key: bytes, nonce: bytes) -> None:
    counter = Counter.new(64, prefix=nonce, initial_value=0)
    dec = AES.new(key, AES.MODE_CTR, counter=counter)
    with open(src, "rb") as s, open(dst, "wb") as d:
        d.write(dec.decrypt(s.read()))


def _playback(track_id: str, cfg: dict) -> dict:
    last_err = None
    for quality in _QUALITIES:
        try:
            return _api_get(
                f"tracks/{track_id}/playbackinfopostpaywall",
                cfg,
                {
                    "audioquality": quality,
                    "playbackmode": "STREAM",
                    "assetpresentation": "FULL",
                },
            )
        except Exception as exc:
            last_err = exc
            continue
    raise TidalError(
        f"No stream available for that TIDAL track ({last_err})."
    )


def _parse_manifest(playback: dict) -> tuple[str, str | None, str]:
    """Return (url, encryption_key_or_None, ext)."""
    mime = (playback.get("manifestMimeType") or "").lower()
    manifest = playback.get("manifest") or ""
    if not manifest:
        raise TidalError("TIDAL stream manifest was empty.")
    try:
        data = json.loads(base64.b64decode(manifest))
    except Exception:
        # plain url
        if manifest.startswith("http"):
            return manifest, None, "m4a"
        raise TidalError("Couldn't decode TIDAL stream manifest.")

    urls = data.get("urls") or []
    if not urls:
        # HLS
        if data.get("url"):
            return data["url"], data.get("encryptionKey") or data.get("keyId"), "m4a"
        raise TidalError("TIDAL manifest had no stream URLs.")
    enc = data.get("encryptionKey") or data.get("keyId")
    url = urls[0]
    ext = "flac" if "flac" in (playback.get("codecs") or "").lower() else "m4a"
    if "mp4a" in (playback.get("codecs") or "").lower():
        ext = "m4a"
    return url, enc, ext


def probe(url: str) -> dict:
    tid = _track_id_from_url(url)
    cfg = _cfg()
    if not cfg["access"]:
        # metadata-only via public oembed when possible
        try:
            o = json.loads(ac.http_get(
                "https://oembed.tidal.com/?" + urllib.parse.urlencode({"url": url}),
                headers={"User-Agent": ac.UA},
            ))
            return ac.probe_payload(
                platform="tidal",
                url=url,
                title=o.get("title"),
                uploader=o.get("author_name"),
                duration=None,
                thumbnail=o.get("thumbnail_url"),
                quality="login required for stream",
            )
        except Exception:
            raise TidalError(
                "TIDAL needs TIDAL_ACCESS_TOKEN set to probe/download streams."
            )
    track = _api_get(f"tracks/{tid}", cfg)
    duration = float(track["duration"]) if track.get("duration") else None
    artists = ", ".join(
        a.get("name") for a in (track.get("artists") or []) if a.get("name")
    ) or (track.get("artist") or {}).get("name")
    quality = track.get("audioQuality") or "HIGH"
    return ac.probe_payload(
        platform="tidal",
        url=track.get("url") or url,
        title=track.get("title"),
        uploader=artists,
        duration=duration,
        thumbnail=(
            f"https://resources.tidal.com/images/"
            f"{(track.get('album') or {}).get('cover', '').replace('-', '/')}/640x640.jpg"
            if (track.get("album") or {}).get("cover") else None
        ),
        quality=str(quality),
        best_size=int(duration * 320 * 125) if duration else None,
    )


def run_download(store: JobStore, job_id: str, url: str, option_id: str,
                 job_dir: str, filename_stem: str | None = None) -> None:
    if option_id not in ac.AUDIO_OPTION_IDS:
        raise TidalError(f"Unknown option '{option_id}'.")
    store.update(job_id, status="downloading", progress=5.0)
    tid = _track_id_from_url(url)
    cfg = _cfg()
    track = _api_get(f"tracks/{tid}", cfg)
    store.update(job_id, progress=15.0)
    playback = _playback(tid, cfg)
    stream_url, enc_key, ext = _parse_manifest(playback)
    store.update(job_id, progress=25.0)

    # Progressive URL download (most common for HIGH/LOSSLESS)
    if ".m3u8" in stream_url or "m3u8" in (playback.get("manifestMimeType") or ""):
        # Let ffmpeg pull HLS
        raw = os.path.join(job_dir, f"raw.{ext}")
        ac.ffmpeg(["-i", stream_url, "-c", "copy", raw])
    else:
        data = ac.http_get(stream_url, headers={"User-Agent": ac.UA}, timeout=180)
        enc_path = os.path.join(job_dir, f"enc.{ext}")
        open(enc_path, "wb").write(data)
        store.update(job_id, progress=70.0, downloaded_bytes=len(data))
        raw = os.path.join(job_dir, f"raw.{ext}")
        if enc_key:
            key, nonce = _decrypt_security_token(enc_key)
            _decrypt_file(enc_path, raw, key, nonce)
            try:
                os.remove(enc_path)
            except OSError:
                pass
        else:
            os.replace(enc_path, raw)

    artists = ", ".join(
        a.get("name") for a in (track.get("artists") or []) if a.get("name")
    ) or (track.get("artist") or {}).get("name")
    title = track.get("title")
    album = (track.get("album") or {}).get("title")
    stem = filename_stem or ac.stem_for(title, artists, f"tidal-{tid}")
    cover = (track.get("album") or {}).get("cover")
    thumb_url = (
        f"https://resources.tidal.com/images/{cover.replace('-', '/')}/640x640.jpg"
        if cover else None
    )
    thumb = ac.fetch_thumb(thumb_url, job_dir)
    ac.finalize_audio(
        store, job_id, job_dir, raw, f".{ext}", option_id, stem,
        title=title, artist=artists, album=album, thumb_path=thumb,
    )
