"""Deezer track resolve + Blowfish-encrypted CDN download.

Full-length streams need a free/paid account cookie (`DEEZER_ARL`). Without it
we still probe metadata and can fetch the official 30s preview.
"""
from __future__ import annotations

import functools
import hashlib
import http.cookiejar
import json
import os
import re
import urllib.parse
import urllib.request

from Crypto.Cipher import Blowfish

from . import audio_common as ac
from .jobs import JobStore

_BLOWFISH_SECRET = "g4el58wc0zvf9na1"
_GW = "https://www.deezer.com/ajax/gw-light.php"


class DeezerError(Exception):
    pass


class _Session:
    def __init__(self) -> None:
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        arl = os.environ.get("DEEZER_ARL")
        if arl:
            # Seed ARL cookie for full-track streams
            self.opener.addheaders = []  # placate type checkers
            cookie = http.cookiejar.Cookie(
                version=0, name="arl", value=arl.strip(), port=None,
                port_specified=False, domain=".deezer.com",
                domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None,
                discard=True, comment=None, comment_url=None,
                rest={}, rfc2109=False,
            )
            self.cj.set_cookie(cookie)
        self.api_token = "null"
        self.license_token: str | None = None

    def open(self, url: str, data: bytes | None = None,
             headers: dict | None = None, timeout: float = 45) -> bytes:
        h = {
            "User-Agent": ac.UA,
            "Origin": "https://www.deezer.com",
            "Referer": "https://www.deezer.com/",
        }
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise DeezerError(f"Deezer HTTP {exc.code}") from exc

    def bootstrap(self) -> None:
        self.open("https://www.deezer.com/")
        raw = self.gw("deezer.getUserData")
        results = raw.get("results") or {}
        self.api_token = results.get("checkForm") or "null"
        opts = (results.get("USER") or {}).get("OPTIONS") or {}
        self.license_token = opts.get("license_token")

    def gw(self, method: str, body: dict | None = None,
           api_token: str | None = None) -> dict:
        token = api_token if api_token is not None else self.api_token
        params = urllib.parse.urlencode({
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": token,
        })
        raw = self.open(
            f"{_GW}?{params}",
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        return json.loads(raw)


def _track_id_from_url(url: str) -> str:
    m = re.search(r"/track/(\d+)", url)
    if m:
        return m.group(1)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ac.UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as resp:
            final = resp.geturl()
        m = re.search(r"/track/(\d+)", final)
        if m:
            return m.group(1)
    except Exception:
        pass
    raise DeezerError("Couldn't find a Deezer track id in that link.")


def _blowfish_key(track_id: str) -> bytes:
    md5_hash = hashlib.md5(str(track_id).encode()).hexdigest()
    return "".join(
        chr(functools.reduce(lambda x, y: x ^ y, map(ord, t)))
        for t in zip(md5_hash[:16], md5_hash[16:], _BLOWFISH_SECRET)
    ).encode()


def _decrypt_file(src: str, dst: str, key: bytes) -> None:
    chunk = 3 * 2048
    with open(src, "rb") as s, open(dst, "wb") as d:
        while True:
            data = s.read(chunk)
            if not data:
                break
            if len(data) >= 2048:
                d.write(
                    Blowfish.new(
                        key, Blowfish.MODE_CBC,
                        b"\x00\x01\x02\x03\x04\x05\x06\x07",
                    ).decrypt(data[:2048]) + data[2048:]
                )
            else:
                d.write(data)


def _public_track(track_id: str) -> dict:
    return json.loads(ac.http_get(
        f"https://api.deezer.com/track/{track_id}",
        headers={"User-Agent": ac.UA},
    ))


def _resolve_stream(sess: _Session, track_id: str) -> tuple[str, str, int, bool]:
    """Return (url, ext, abr, encrypted)."""
    data = sess.gw("song.getData", {"sng_id": int(track_id)})
    results = data.get("results") or {}
    if not results:
        raise DeezerError("Couldn't load Deezer track data.")

    track_token = results.get("TRACK_TOKEN")
    if sess.license_token and track_token:
        for fmt, abr, ext in (
            ("FLAC", 900, "flac"),
            ("MP3_320", 320, "mp3"),
            ("MP3_128", 128, "mp3"),
        ):
            body = {
                "license_token": sess.license_token,
                "media": [{
                    "type": "FULL",
                    "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt}],
                }],
                "track_tokens": [track_token],
            }
            try:
                raw = sess.open(
                    "https://media.deezer.com/v1/get_url",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                resp = json.loads(raw)
            except DeezerError:
                continue
            try:
                sources = resp["data"][0]["media"][0]["sources"]
                url = sources[0]["url"]
                if url:
                    return url, ext, abr, True
            except (KeyError, IndexError, TypeError):
                continue

    # Official 30s preview (always available when track is readable)
    media = results.get("MEDIA") or []
    for m in media:
        if m.get("TYPE") == "preview" and m.get("HREF"):
            return m["HREF"], "mp3", 64, False
    preview = (_public_track(track_id) or {}).get("preview")
    if preview:
        return preview, "mp3", 64, False
    raise DeezerError(
        "No Deezer stream available. Set DEEZER_ARL (account cookie) "
        "for full-length downloads."
    )


def probe(url: str) -> dict:
    tid = _track_id_from_url(url)
    try:
        info = _public_track(tid)
    except Exception as exc:
        raise DeezerError("Couldn't read that Deezer track.") from exc
    if info.get("error"):
        raise DeezerError(info["error"].get("message") or "Deezer track not found.")
    duration = float(info["duration"]) if info.get("duration") else None
    artist = (info.get("artist") or {}).get("name")
    thumb = (info.get("album") or {}).get("cover_xl") or (info.get("album") or {}).get("cover_big")
    if os.environ.get("DEEZER_ARL"):
        # Premium accounts resolve FLAC first, then MP3_320 / MP3_128.
        quality = "FLAC / MP3 up to 320 (account)"
        best_size = int(duration * 900 * 125) if duration else None
    else:
        quality = "preview ~30s (set DEEZER_ARL for full)"
        best_size = int(30 * 64 * 125)
    return ac.probe_payload(
        platform="deezer",
        url=info.get("link") or url,
        title=info.get("title"),
        uploader=artist,
        duration=duration,
        thumbnail=thumb,
        quality=quality,
        best_size=best_size,
    )


def run_download(store: JobStore, job_id: str, url: str, option_id: str,
                 job_dir: str, filename_stem: str | None = None) -> None:
    if option_id not in ac.AUDIO_OPTION_IDS:
        raise DeezerError(f"Unknown option '{option_id}'.")
    store.update(job_id, status="downloading", progress=5.0)
    tid = _track_id_from_url(url)
    info = _public_track(tid)
    if info.get("error"):
        raise DeezerError(info["error"].get("message") or "Deezer track not found.")

    sess = _Session()
    sess.bootstrap()
    store.update(job_id, progress=20.0)
    stream_url, ext, abr, encrypted = _resolve_stream(sess, tid)
    store.update(job_id, progress=30.0)

    data = sess.open(stream_url, timeout=120)
    enc_path = os.path.join(job_dir, f"enc.{ext}")
    open(enc_path, "wb").write(data)
    store.update(job_id, progress=75.0, downloaded_bytes=len(data))

    raw = os.path.join(job_dir, f"raw.{ext}")
    if encrypted:
        _decrypt_file(enc_path, raw, _blowfish_key(tid))
        try:
            os.remove(enc_path)
        except OSError:
            pass
    else:
        os.replace(enc_path, raw)

    artist = (info.get("artist") or {}).get("name")
    title = info.get("title")
    album = (info.get("album") or {}).get("title")
    stem = filename_stem or ac.stem_for(title, artist, f"deezer-{tid}")
    if not encrypted:
        stem = f"{stem} [preview]"
    thumb = ac.fetch_thumb(
        (info.get("album") or {}).get("cover_xl")
        or (info.get("album") or {}).get("cover_big"),
        job_dir,
    )
    ac.finalize_audio(
        store, job_id, job_dir, raw, f".{ext}", option_id, stem,
        title=title, artist=artist, album=album, thumb_path=thumb,
    )
