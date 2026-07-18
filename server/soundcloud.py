"""SoundCloud resolve + download (progressive, HLS, and Widevine CENC DRM).

yt-dlp refuses DRM-only tracks (`ctr-encrypted-hls` / `cbc-encrypted-hls`).
Those streams are still just CDN-hosted fMP4 segments plus a Widevine license
step — the same path klickaud-style converters use. We:

1. Prefer progressive HTTP MP3 when the API still serves it (network-speed).
2. Else non-DRM HLS (concurrent fragment fetch).
3. Else CTR-encrypted HLS: Widevine license → concurrent segment download →
   pure-Python CENC AES-CTR decrypt → ffmpeg remux / optional MP3 encode.

CBC/FairPlay is skipped (CTR carries the same audio). Requires a Widevine L3
device (`.wvd`); see `_resolve_wvd()`.
"""
from __future__ import annotations

import base64
import binascii
import concurrent.futures
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util import Counter
from yt_dlp.utils import sanitize_filename

from .jobs import JobStore

log = logging.getLogger("ytdl4me.soundcloud")

_API = "https://api-v2.soundcloud.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Origin": "https://soundcloud.com",
    "Referer": "https://soundcloud.com/",
    "Accept": "application/json, text/plain, */*",
}

# Public L3 device used by musicdl for the same SoundCloud license endpoint.
# Overridable via WIDEVINE_DEVICE_FILE / WIDEVINE_DEVICE_B64.
_DEFAULT_WVD_URL = (
    "https://raw.githubusercontent.com/CharlesPikachu/musicdl/master/"
    "musicdl/modules/wvds/musicdl_charlespikachu_device_v1.wvd"
)

_client_id_lock = threading.Lock()
_client_id: str | None = None
_wvd_lock = threading.Lock()
_wvd_path: str | None = None

MP3_OPTION_IDS = ("mp3_320", "mp3_256", "mp3_192", "mp3_128")
AUDIO_OPTION_IDS = ("audio_best", *MP3_OPTION_IDS)


class SoundCloudError(Exception):
    """User-facing SoundCloud failure."""


# ---------------------------------------------------------------------------
# HTTP / client_id
# ---------------------------------------------------------------------------

def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 45) -> bytes:
    h = dict(_HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        raise SoundCloudError(
            f"SoundCloud HTTP {exc.code} for {url.split('?', 1)[0]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SoundCloudError(f"Couldn't reach SoundCloud: {exc.reason}") from exc


def _http_json(url: str, **kwargs) -> dict:
    raw = _http(url, **kwargs)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SoundCloudError("SoundCloud returned non-JSON data.") from exc


def _get_client_id() -> str:
    global _client_id
    with _client_id_lock:
        if _client_id:
            return _client_id
        # yt-dlp cache first
        cache = Path.home() / ".cache" / "yt-dlp" / "soundcloud" / "client_id.json"
        if cache.is_file():
            try:
                data = json.loads(cache.read_text())
                cid = data.get("data") or data.get("client_id")
                if isinstance(cid, str) and len(cid) >= 20:
                    _client_id = cid
                    return _client_id
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        page = _http("https://soundcloud.com/", headers={
            "User-Agent": _UA, "Accept": "text/html",
        }).decode("utf-8", errors="replace")
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', page)
        for src in reversed(scripts):
            if not src.startswith("http"):
                continue
            try:
                js = _http(src, headers={"User-Agent": _UA}).decode(
                    "utf-8", errors="replace"
                )
            except SoundCloudError:
                continue
            m = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', js)
            if m:
                _client_id = m.group(1)
                return _client_id
        raise SoundCloudError("Couldn't obtain a SoundCloud client_id.")


# ---------------------------------------------------------------------------
# Widevine device
# ---------------------------------------------------------------------------

def _resolve_wvd() -> str:
    """Return path to a .wvd device file (cached after first resolve)."""
    global _wvd_path
    with _wvd_lock:
        if _wvd_path and os.path.isfile(_wvd_path) and os.path.getsize(_wvd_path) > 0:
            return _wvd_path

        path = os.environ.get("WIDEVINE_DEVICE_FILE")
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            _wvd_path = path
            return _wvd_path

        b64 = os.environ.get("WIDEVINE_DEVICE_B64")
        if b64:
            try:
                blob = base64.b64decode(b64.strip(), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SoundCloudError(
                    "WIDEVINE_DEVICE_B64 is not valid base64."
                ) from exc
            fd, tmp = tempfile.mkstemp(prefix="ytdl4me-wvd-", suffix=".wvd")
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.chmod(tmp, 0o600)
            _wvd_path = tmp
            return _wvd_path

        # Cache dir next to downloads or under /tmp
        cache_root = os.environ.get("DOWNLOAD_DIR") or tempfile.gettempdir()
        cache = Path(cache_root) / ".ytdl4me-cache" / "widevine_device.wvd"
        if cache.is_file() and cache.stat().st_size > 0:
            _wvd_path = str(cache)
            return _wvd_path

        # Bundled / auto-fetched public L3 device (personal/educational use).
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            blob = _http(_DEFAULT_WVD_URL, headers={"User-Agent": _UA}, timeout=30)
            if len(blob) < 100:
                raise SoundCloudError("Widevine device download looked empty.")
            tmp = cache.with_suffix(".wvd.part")
            tmp.write_bytes(blob)
            os.replace(tmp, cache)
            os.chmod(cache, 0o600)
            _wvd_path = str(cache)
            log.info("Cached Widevine device at %s", cache)
            return _wvd_path
        except Exception as exc:
            raise SoundCloudError(
                "This track is DRM-protected and no Widevine device is configured. "
                "Set WIDEVINE_DEVICE_FILE or WIDEVINE_DEVICE_B64 (a pywidevine .wvd)."
            ) from exc


def _widevine_content_key(pssh_b64: str, license_token: str) -> tuple[str, bytes]:
    try:
        from pywidevine.cdm import Cdm
        from pywidevine.device import Device
        from pywidevine.pssh import PSSH
    except ImportError as exc:
        raise SoundCloudError(
            "pywidevine is required for DRM SoundCloud tracks "
            "(pip install pywidevine)."
        ) from exc

    device = Device.load(_resolve_wvd())
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh_b64))
        license_url = (
            "https://license.media-streaming.soundcloud.cloud/playback/widevine"
            f"?license_token={urllib.parse.quote(license_token, safe='')}"
        )
        lic = _http(
            license_url,
            data=challenge,
            headers={**_HEADERS, "Content-Type": "application/octet-stream"},
            timeout=30,
        )
        cdm.parse_license(session_id, lic)
        for key in cdm.get_keys(session_id):
            if key.type != "CONTENT":
                continue
            kid = key.kid.hex
            raw = key.key if isinstance(key.key, bytes) else bytes.fromhex(key.key.hex())
            return kid, raw
        raise SoundCloudError("Widevine license returned no content key.")
    finally:
        cdm.close(session_id)


# ---------------------------------------------------------------------------
# Resolve / formats
# ---------------------------------------------------------------------------

def resolve_track(url: str) -> dict:
    cid = _get_client_id()
    info = _http_json(
        f"{_API}/resolve?url={urllib.parse.quote(url, safe='')}&client_id={cid}"
    )
    if info.get("kind") and info.get("kind") != "track":
        if info.get("kind") in ("playlist", "system-playlist"):
            raise SoundCloudError(
                "Playlists aren't supported yet — paste a link to a single track."
            )
        raise SoundCloudError("That SoundCloud link isn't a single track.")
    if not info.get("id"):
        raise SoundCloudError("Couldn't read that SoundCloud track.")
    return info


def _transcodings(info: dict) -> list[dict]:
    return list((info.get("media") or {}).get("transcodings") or [])


def _stream_lookup(transcoding: dict, track_auth: str | None) -> dict:
    cid = _get_client_id()
    q = {"client_id": cid}
    if track_auth:
        q["track_authorization"] = track_auth
    url = transcoding["url"]
    if "?" in url:
        full = url + "&" + urllib.parse.urlencode(q)
    else:
        full = url + "?" + urllib.parse.urlencode(q)
    return _http_json(full)


def _pick_stream(info: dict) -> dict:
    """Choose best available stream. Returns dict with keys:
    mode: progressive|hls|drm
    preset, quality, abr, mime, playlist_or_url, license_token?
    """
    auth = info.get("track_authorization")
    trans = [t for t in _transcodings(info) if not t.get("snipped")]

    def abr_of(t: dict) -> int:
        preset = t.get("preset") or ""
        m = re.search(r"(\d+)k", preset)
        if m:
            return int(m.group(1))
        if "mp3" in preset:
            return 128
        return 0

    # 1) progressive HTTP
    for t in sorted(trans, key=abr_of, reverse=True):
        proto = (t.get("format") or {}).get("protocol") or ""
        if proto != "progressive":
            continue
        try:
            st = _stream_lookup(t, auth)
        except SoundCloudError:
            continue
        url = st.get("url")
        if url:
            return {
                "mode": "progressive",
                "preset": t.get("preset"),
                "quality": t.get("quality"),
                "abr": abr_of(t) or 128,
                "mime": (t.get("format") or {}).get("mime_type") or "audio/mpeg",
                "url": url,
            }

    # 2) plain HLS (mp3/aac)
    for t in sorted(trans, key=abr_of, reverse=True):
        proto = (t.get("format") or {}).get("protocol") or ""
        if proto != "hls":
            continue
        if (t.get("preset") or "").startswith("abr"):
            continue
        try:
            st = _stream_lookup(t, auth)
        except SoundCloudError:
            continue
        url = st.get("url")
        if url:
            return {
                "mode": "hls",
                "preset": t.get("preset"),
                "quality": t.get("quality"),
                "abr": abr_of(t) or 128,
                "mime": (t.get("format") or {}).get("mime_type") or "audio/mpeg",
                "url": url,
            }

    # 3) CTR-encrypted HLS (Widevine) — same audio as CBC/FairPlay
    drm = [
        t for t in trans
        if ((t.get("format") or {}).get("protocol") or "").startswith("ctr-")
        and not (t.get("preset") or "").startswith("abr")
    ]
    drm.sort(key=lambda t: (
        0 if t.get("quality") == "hq" else 1 if t.get("quality") == "sq" else 2,
        -abr_of(t),
    ))
    for t in drm:
        try:
            st = _stream_lookup(t, auth)
        except SoundCloudError:
            continue
        url, token = st.get("url"), st.get("licenseAuthToken")
        if url and token:
            return {
                "mode": "drm",
                "preset": t.get("preset"),
                "quality": t.get("quality"),
                "abr": abr_of(t) or 160,
                "mime": (t.get("format") or {}).get("mime_type") or "audio/mp4",
                "url": url,
                "license_token": token,
            }

    raise SoundCloudError(
        "No downloadable stream was found for that track "
        "(it may be blocked or region-restricted)."
    )


def _best_quality_label(info: dict) -> str | None:
    """Describe the best stream we *can* fetch without actually resolving CDN URLs."""
    auth = info.get("track_authorization")
    # Prefer cheap inspection: try progressive/hls existence via resolve only
    # Full pick_stream hits the media API — fine for probe.
    try:
        stream = _pick_stream(info)
    except SoundCloudError:
        # DRM without device still has known AAC tiers in metadata
        abrs = []
        for t in _transcodings(info):
            if t.get("snipped"):
                continue
            proto = (t.get("format") or {}).get("protocol") or ""
            m = re.search(r"(\d+)k", t.get("preset") or "")
            if m and (proto.startswith(("ctr-", "cbc-", "hls")) or proto == "progressive"):
                abrs.append(int(m.group(1)))
        if abrs:
            return f"AAC ~{max(abrs)} kbps"
        return None
    abr = stream.get("abr")
    mime = (stream.get("mime") or "").lower()
    preset = (stream.get("preset") or "").lower()
    if "mp3" in mime or "mp3" in preset or "mpeg" in mime:
        codec = "MP3"
    elif "opus" in mime or "opus" in preset:
        codec = "Opus"
    else:
        codec = "AAC"
    if abr:
        return f"{codec} ~{abr} kbps"
    return codec


# ---------------------------------------------------------------------------
# CENC decrypt (pure Python)
# ---------------------------------------------------------------------------

def _read_boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", data[i:i + 8])
        typ_s = typ.decode("latin1")
        if size == 1:
            if i + 16 > end:
                break
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
            hdr = 8
        else:
            hdr = 8
        if size < hdr or i + size > end:
            break
        yield i, size, typ_s, hdr
        i += size


def _find_box(data: bytes, target: str, start: int = 0, end: int | None = None):
    containers = {
        "moov", "trak", "mdia", "minf", "stbl", "moof", "traf", "mvex",
        "edts", "udta", "sinf", "schi",
    }
    for off, size, typ, hdr in _read_boxes(data, start, end):
        if typ == target:
            return off, size, hdr
        if typ in containers:
            res = _find_box(data, target, off + hdr, off + size)
            if res:
                return res
    return None


def _parse_senc(data: bytes, off: int, size: int, hdr: int) -> list[bytes]:
    body = memoryview(data)[off + hdr:off + size]
    flags = int.from_bytes(body[1:4], "big")
    sample_count = int.from_bytes(body[4:8], "big")
    pos = 8
    remaining = len(body) - pos
    if flags & 0x2:
        raise SoundCloudError("Unexpected subsample-encrypted SoundCloud stream.")
    if remaining == sample_count * 16:
        iv_size = 16
    elif remaining == sample_count * 8:
        iv_size = 8
    else:
        raise SoundCloudError(
            f"Couldn't parse CENC sample IVs ({remaining}/{sample_count})."
        )
    return [bytes(body[pos + i * iv_size:pos + (i + 1) * iv_size])
            for i in range(sample_count)]


def _parse_trun_sizes(data: bytes, off: int, size: int, hdr: int,
                      default_size: int | None) -> list[int]:
    body = memoryview(data)[off + hdr:off + size]
    flags = int.from_bytes(body[1:4], "big")
    sample_count = int.from_bytes(body[4:8], "big")
    pos = 8
    if flags & 0x1:
        pos += 4
    if flags & 0x4:
        pos += 4
    sizes: list[int] = []
    for _ in range(sample_count):
        if flags & 0x100:
            pos += 4
        if flags & 0x200:
            sizes.append(int.from_bytes(body[pos:pos + 4], "big"))
            pos += 4
        else:
            if default_size is None:
                raise SoundCloudError("Missing sample size in DRM fragment.")
            sizes.append(default_size)
        if flags & 0x400:
            pos += 4
        if flags & 0x800:
            pos += 4
    return sizes


def _aes_ctr_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(iv) == 8:
        counter = Counter.new(64, prefix=iv, initial_value=0)
    elif len(iv) == 16:
        counter = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
    else:
        raise SoundCloudError(f"Unsupported CENC IV length {len(iv)}.")
    return AES.new(key, AES.MODE_CTR, counter=counter).decrypt(data)


def _decrypt_fragment(frag: bytes, key: bytes) -> bytes:
    moof = _find_box(frag, "moof")
    mdat = _find_box(frag, "mdat")
    if not moof or not mdat:
        return frag
    moof_off, moof_size, _ = moof
    mdat_off, _, mdat_hdr = mdat
    senc = _find_box(frag, "senc", moof_off, moof_off + moof_size)
    trun = _find_box(frag, "trun", moof_off, moof_off + moof_size)
    if not senc or not trun:
        return frag
    default_size = None
    tfhd = _find_box(frag, "tfhd", moof_off, moof_off + moof_size)
    if tfhd:
        body = memoryview(frag)[tfhd[0] + tfhd[2]:tfhd[0] + tfhd[1]]
        flags = int.from_bytes(body[1:4], "big")
        pos = 8
        if flags & 0x1:
            pos += 8
        if flags & 0x2:
            pos += 4
        if flags & 0x8:
            pos += 4
        if flags & 0x10:
            default_size = int.from_bytes(body[pos:pos + 4], "big")
    ivs = _parse_senc(frag, *senc)
    sizes = _parse_trun_sizes(frag, *trun, default_size)
    out = bytearray(frag)
    cursor = mdat_off + mdat_hdr
    for iv, sz in zip(ivs, sizes):
        sample = bytes(out[cursor:cursor + sz])
        out[cursor:cursor + sz] = _aes_ctr_decrypt(key, iv, sample)
        cursor += sz
    return bytes(out)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _parse_m3u8(text: str, base_url: str) -> tuple[str | None, list[str]]:
    init = None
    segs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init = urllib.parse.urljoin(base_url, m.group(1))
        elif line and not line.startswith("#"):
            segs.append(urllib.parse.urljoin(base_url, line))
    return init, segs


def _download_many(urls: list[str], progress_cb=None,
                   workers: int = 12) -> list[bytes]:
    total = len(urls)
    done = 0
    lock = threading.Lock()
    out: list[bytes | None] = [None] * total

    def one(idx_url):
        nonlocal done
        idx, url = idx_url
        data = _http(url, headers={"User-Agent": _UA}, timeout=60)
        with lock:
            out[idx] = data
            done += 1
            if progress_cb:
                progress_cb(done, total, len(data))
        return data

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, enumerate(urls)))
    if any(x is None for x in out):
        raise SoundCloudError("Failed to download one or more audio segments.")
    return out  # type: ignore[return-value]


def _ffmpeg(args: list[str]) -> None:
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", *args],
            capture_output=True, text=True, timeout=600,
        )
    except FileNotFoundError as exc:
        raise SoundCloudError("ffmpeg is required but was not found.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SoundCloudError("ffmpeg timed out processing the audio.") from exc
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[-400:]
        raise SoundCloudError(f"ffmpeg failed: {err or r.returncode}")


def _artwork_url(info: dict) -> str | None:
    art = info.get("artwork_url") or (info.get("user") or {}).get("avatar_url")
    if not art:
        return None
    # Prefer high-res
    return re.sub(r"-large(\.\w+)$", r"-t500x500\1", art)


def _apply_tags(path: str, info: dict, thumb_path: str | None) -> None:
    try:
        import mutagen
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, error as ID3Error
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        return

    title = info.get("title") or ""
    artist = (
        (info.get("publisher_metadata") or {}).get("artist")
        or (info.get("user") or {}).get("username")
        or ""
    )
    album = (info.get("publisher_metadata") or {}).get("album_title") or ""
    ext = Path(path).suffix.lower()
    thumb = None
    if thumb_path and os.path.isfile(thumb_path):
        thumb = Path(thumb_path).read_bytes()

    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3Error:
                tags = ID3()
            if title:
                tags["TIT2"] = TIT2(encoding=3, text=title)
            if artist:
                tags["TPE1"] = TPE1(encoding=3, text=artist)
            if album:
                tags["TALB"] = TALB(encoding=3, text=album)
            if thumb:
                mime = "image/png" if thumb[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=thumb))
            tags.save(path, v2_version=3)
        else:
            audio = MP4(path)
            if title:
                audio["\xa9nam"] = [title]
            if artist:
                audio["\xa9ART"] = [artist]
            if album:
                audio["\xa9alb"] = [album]
            if thumb:
                fmt = MP4Cover.FORMAT_PNG if thumb[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(thumb, imageformat=fmt)]
            audio.save()
    except Exception:
        log.debug("tag write failed for %s", path, exc_info=True)


def _stem(info: dict) -> str:
    title = info.get("title") or f"soundcloud-{info.get('id')}"
    artist = (
        (info.get("publisher_metadata") or {}).get("artist")
        or (info.get("user") or {}).get("username")
    )
    raw = f"{artist} - {title}" if artist else title
    return sanitize_filename(raw)[:180] or f"soundcloud-{info.get('id')}"


# ---------------------------------------------------------------------------
# Public API (probe / download) — same shape as downloader.*
# ---------------------------------------------------------------------------

def probe(url: str) -> dict:
    """Blocking metadata probe for a SoundCloud track URL."""
    info = resolve_track(url)
    duration_ms = info.get("duration") or info.get("full_duration")
    duration = float(duration_ms) / 1000.0 if duration_ms else None
    quality = _best_quality_label(info)

    # Size estimate from best known abr
    abr = None
    if quality:
        m = re.search(r"(\d+)\s*kbps", quality)
        if m:
            abr = int(m.group(1))
    best_size = int(duration * abr * 125) if duration and abr else None

    audio_options = [{
        "id": "audio_best",
        "label": "Original (best quality)",
        "detail": f"{quality} · no re-encode" if quality else "no re-encode",
        "approx_size": best_size,
    }]
    for kbps in (320, 256, 192, 128):
        audio_options.append({
            "id": f"mp3_{kbps}",
            "label": f"MP3 {kbps}",
            "detail": f"{kbps} kbps CBR",
            "approx_size": int(duration * kbps * 125) if duration else None,
        })

    return {
        "platform": "soundcloud",
        "kind": "audio",
        "url": info.get("permalink_url") or url,
        "title": info.get("title"),
        "uploader": (
            (info.get("publisher_metadata") or {}).get("artist")
            or (info.get("user") or {}).get("username")
            or info.get("user", {}).get("full_name")
        ),
        "duration": duration,
        "thumbnail": _artwork_url(info),
        "video_options": [],
        "original_quality": quality,
        "audio_options": audio_options,
    }


def run_download(
    store: JobStore,
    job_id: str,
    url: str,
    option_id: str,
    job_dir: str,
    filename_stem: str | None = None,
) -> None:
    """Blocking SoundCloud download into job_dir. Raises SoundCloudError."""
    if option_id not in AUDIO_OPTION_IDS:
        raise SoundCloudError(f"Unknown option '{option_id}'.")

    store.update(job_id, status="downloading", progress=0.0)
    info = resolve_track(url)
    stream = _pick_stream(info)
    stem = filename_stem or _stem(info)
    stem = sanitize_filename(stem).replace("%", "")

    thumb_path = None
    art = _artwork_url(info)
    if art:
        try:
            thumb_path = os.path.join(job_dir, "cover.jpg")
            Path(thumb_path).write_bytes(_http(art, headers={"User-Agent": _UA}))
        except SoundCloudError:
            thumb_path = None

    raw_path = os.path.join(job_dir, f"{stem}.source")
    total_hint = None

    def on_progress(downloaded: int, total: int | None, speed: float | None = None):
        fields = {
            "status": "downloading",
            "downloaded_bytes": downloaded,
            "total_bytes": int(total) if total else total_hint,
            "speed": speed,
        }
        if total and downloaded:
            fields["progress"] = min(99.0, downloaded / total * 100.0)
        elif total_hint and downloaded:
            fields["progress"] = min(99.0, downloaded / total_hint * 100.0)
        store.update(job_id, **fields)

    mode = stream["mode"]
    t0 = time.time()
    downloaded_bytes = 0

    if mode == "progressive":
        data = _http(stream["url"], headers={"User-Agent": _UA}, timeout=120)
        Path(raw_path).write_bytes(data)
        downloaded_bytes = len(data)
        on_progress(downloaded_bytes, downloaded_bytes)
        # progressive is already mp3
        source_ext = ".mp3"
        os.replace(raw_path, raw_path + source_ext)
        raw_path = raw_path + source_ext

    elif mode == "hls":
        m3u8 = _http(stream["url"], headers={"User-Agent": _UA}).decode("utf-8", "replace")
        init_url, segs = _parse_m3u8(m3u8, stream["url"])
        urls = ([init_url] if init_url else []) + segs
        if not segs:
            raise SoundCloudError("HLS playlist had no segments.")
        got = 0

        def cb(done, total, nbytes):
            nonlocal got
            got += nbytes
            on_progress(got, None)

        parts = _download_many(urls, progress_cb=cb)
        with open(raw_path + ".bin", "wb") as f:
            for p in parts:
                f.write(p)
        # Remux via ffmpeg (mp3 HLS segments are often MPEG-TS or raw ADTS)
        src = raw_path + ".bin"
        # Try stream copy to m4a/mp3 depending on preset
        if "mp3" in (stream.get("preset") or "") or "mpeg" in (stream.get("mime") or ""):
            out = raw_path + ".mp3"
            # Concatenated mp3 frames often work as-is; ffmpeg rewraps safely
            _ffmpeg(["-i", src, "-c", "copy", out])
            raw_path = out
            source_ext = ".mp3"
        else:
            out = raw_path + ".m4a"
            _ffmpeg(["-i", src, "-c", "copy", out])
            raw_path = out
            source_ext = ".m4a"
        try:
            os.remove(src)
        except OSError:
            pass
        downloaded_bytes = os.path.getsize(raw_path)

    else:  # drm
        store.update(job_id, status="downloading", progress=2.0)
        m3u8 = _http(stream["url"], headers={"User-Agent": _UA}).decode("utf-8", "replace")
        pssh_m = re.search(r'URI="data:text/plain;base64,([^"]+)"', m3u8)
        if not pssh_m:
            raise SoundCloudError("DRM playlist is missing a Widevine PSSH box.")
        kid, key = _widevine_content_key(pssh_m.group(1), stream["license_token"])
        log.debug("SoundCloud DRM key kid=%s", kid)
        store.update(job_id, progress=8.0)

        init_url, segs = _parse_m3u8(m3u8, stream["url"])
        if not init_url or not segs:
            raise SoundCloudError("DRM playlist is missing init/segments.")

        got = 0
        n_segs = len(segs)

        def cb(done, total, nbytes):
            nonlocal got
            got += nbytes
            # map segment fetch to 8..85%
            pct = 8.0 + (done / max(total, 1)) * 77.0
            store.update(
                job_id,
                status="downloading",
                progress=pct,
                downloaded_bytes=got,
                total_bytes=None,
            )

        init_data = _http(init_url, headers={"User-Agent": _UA})
        seg_data = _download_many(segs, progress_cb=cb)
        store.update(job_id, status="processing", progress=88.0, speed=None, eta=None)

        dec_path = raw_path + ".mp4"
        with open(dec_path, "wb") as f:
            f.write(init_data)
            for frag in seg_data:
                f.write(_decrypt_fragment(frag, key))

        out = raw_path + ".m4a"
        _ffmpeg(["-i", dec_path, "-c", "copy", "-vn", out])
        try:
            os.remove(dec_path)
        except OSError:
            pass
        raw_path = out
        source_ext = ".m4a"
        downloaded_bytes = os.path.getsize(raw_path)

    # Encode / finalize
    store.update(job_id, status="processing", progress=92.0, speed=None, eta=None)
    if option_id == "audio_best":
        final = os.path.join(job_dir, f"{stem}{source_ext}")
        if os.path.abspath(raw_path) != os.path.abspath(final):
            os.replace(raw_path, final)
    else:
        bitrate = option_id.rsplit("_", 1)[1]
        final = os.path.join(job_dir, f"{stem}.mp3")
        _ffmpeg([
            "-i", raw_path,
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", f"{bitrate}k",
            final,
        ])
        if os.path.abspath(raw_path) != os.path.abspath(final):
            try:
                os.remove(raw_path)
            except OSError:
                pass

    _apply_tags(final, info, thumb_path)
    if thumb_path:
        try:
            os.remove(thumb_path)
        except OSError:
            pass

    elapsed = time.time() - t0
    log.info(
        "SoundCloud %s %s in %.1fs (%s bytes, mode=%s preset=%s)",
        info.get("id"), option_id, elapsed, os.path.getsize(final),
        mode, stream.get("preset"),
    )
    store.update(
        job_id,
        status="done",
        progress=100.0,
        speed=None,
        eta=None,
        filepath=os.path.realpath(final),
        filename=os.path.basename(final),
        filesize=os.path.getsize(final),
        downloaded_bytes=downloaded_bytes or os.path.getsize(final),
        total_bytes=os.path.getsize(final),
    )
