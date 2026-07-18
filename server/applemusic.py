"""Apple Music track download via webplayback + Widevine (AAC legacy).

Requires APPLE_MEDIA_USER_TOKEN from a browser session on music.apple.com
(cookie `media-user-token`) with an active subscription.
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import re
import struct
import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util import Counter

from . import audio_common as ac
from .jobs import JobStore

_HOME = "https://music.apple.com"
_AMP = "https://amp-api.music.apple.com"
_PLAY = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
_LICENSE = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"


class AppleMusicError(Exception):
    pass


def _media_user_token() -> str:
    tok = os.environ.get("APPLE_MEDIA_USER_TOKEN") or ""
    if not tok:
        raise AppleMusicError(
            "Apple Music needs APPLE_MEDIA_USER_TOKEN "
            "(the media-user-token cookie from music.apple.com while logged in)."
        )
    return tok


def _track_id_from_url(url: str) -> tuple[str, str]:
    """Return (storefront, track_id)."""
    # https://music.apple.com/us/album/.../id?i=123
    # https://music.apple.com/us/song/name/123
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    storefront = parts[0] if parts and len(parts[0]) == 2 else "us"
    qs = urllib.parse.parse_qs(parsed.query)
    if "i" in qs:
        return storefront, qs["i"][0]
    m = re.search(r"/(?:song|album)/[^/]+/(\d+)", parsed.path)
    if m:
        # album id — need ?i= for track; if song path, id is track
        if "/song/" in parsed.path:
            return storefront, m.group(1)
        raise AppleMusicError(
            "Paste a link to a single song (with ?i= track id), not only the album."
        )
    m = re.search(r"/(\d+)(?:\?|$)", parsed.path)
    if m:
        return storefront, m.group(1)
    raise AppleMusicError("Couldn't find an Apple Music song id in that link.")


def _developer_token() -> str:
    html = ac.http_get(f"{_HOME}/us/new", headers={"User-Agent": ac.UA}).decode(
        "utf-8", "replace"
    )
    m = re.search(r'src="(/(?:assets/)?index(?:-legacy)?[~-][^"]+\.js)"', html)
    if not m:
        m = re.search(r'src="(/assets/index[^"]+\.js)"', html)
    if not m:
        raise AppleMusicError("Couldn't find Apple Music index.js for developer token.")
    js_path = m.group(1)
    js = ac.http_get(f"{_HOME}{js_path}", headers={"User-Agent": ac.UA}).decode(
        "utf-8", "replace"
    )
    # eyJ… JWT
    for pat in (
        r'"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"',
        r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}',
    ):
        m2 = re.search(pat, js)
        if m2:
            return m2.group(0).strip('"')
    raise AppleMusicError("Couldn't extract Apple Music developer token.")


class _API:
    def __init__(self, storefront: str = "us"):
        self.storefront = storefront
        self.media_user_token = _media_user_token()
        self.token = _developer_token()
        self.language = "en-US"

    def headers(self) -> dict:
        return {
            "User-Agent": ac.UA,
            "Authorization": f"Bearer {self.token}",
            "media-user-token": self.media_user_token,
            "Origin": _HOME,
            "Referer": f"{_HOME}/",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get_song(self, track_id: str) -> dict:
        url = (
            f"{_AMP}/v1/catalog/{self.storefront}/songs/{track_id}"
            f"?l={self.language}"
        )
        return json.loads(ac.http_get(url, headers=self.headers()))

    def webplayback(self, track_id: str) -> dict:
        body = json.dumps({
            "salableAdamId": track_id,
            "language": self.language,
        }).encode()
        raw = ac.http_get(
            f"{_PLAY}?l={self.language}",
            data=body,
            headers=self.headers(),
            timeout=45,
        )
        data = json.loads(raw)
        if "songList" not in data:
            raise AppleMusicError(
                "Apple Music webplayback failed — check APPLE_MEDIA_USER_TOKEN "
                "and that the account has an active subscription."
            )
        return data

    def license(self, track_id: str, uri: str, challenge_b64: str) -> bytes:
        body = json.dumps({
            "challenge": challenge_b64,
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": track_id,
            "isLibrary": False,
            "user-initiated": True,
        }).encode()
        raw = ac.http_get(
            f"{_LICENSE}?l={self.language}",
            data=body,
            headers=self.headers(),
            timeout=30,
        )
        data = json.loads(raw)
        lic_b64 = data.get("license")
        if not lic_b64:
            raise AppleMusicError("Apple Music license exchange returned no key.")
        return base64.b64decode(lic_b64)


# ---- CENC helpers (same approach as soundcloud.py) ----

def _read_boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack(">I4s", data[i:i + 8])
        typ_s = typ.decode("latin1")
        if size == 1:
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


def _find_box(data, target, start=0, end=None):
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


def _parse_senc(data, off, size, hdr):
    body = memoryview(data)[off + hdr:off + size]
    flags = int.from_bytes(body[1:4], "big")
    sample_count = int.from_bytes(body[4:8], "big")
    pos = 8
    remaining = len(body) - pos
    if flags & 0x2:
        raise AppleMusicError("Unexpected subsample encryption on Apple Music stream.")
    if remaining == sample_count * 16:
        iv_size = 16
    elif remaining == sample_count * 8:
        iv_size = 8
    else:
        raise AppleMusicError("Couldn't parse Apple Music sample IVs.")
    return [bytes(body[pos + i * iv_size:pos + (i + 1) * iv_size])
            for i in range(sample_count)]


def _parse_trun_sizes(data, off, size, hdr, default_size=None):
    body = memoryview(data)[off + hdr:off + size]
    flags = int.from_bytes(body[1:4], "big")
    sample_count = int.from_bytes(body[4:8], "big")
    pos = 8
    if flags & 0x1:
        pos += 4
    if flags & 0x4:
        pos += 4
    sizes = []
    for _ in range(sample_count):
        if flags & 0x100:
            pos += 4
        if flags & 0x200:
            sizes.append(int.from_bytes(body[pos:pos + 4], "big"))
            pos += 4
        else:
            sizes.append(default_size)
        if flags & 0x400:
            pos += 4
        if flags & 0x800:
            pos += 4
    return sizes


def _aes_ctr(key, iv, data):
    if len(iv) == 8:
        counter = Counter.new(64, prefix=iv, initial_value=0)
    else:
        counter = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
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
        if sz is None:
            raise AppleMusicError("Missing sample size in Apple Music fragment.")
        sample = bytes(out[cursor:cursor + sz])
        out[cursor:cursor + sz] = _aes_ctr(key, iv, sample)
        cursor += sz
    return bytes(out)


def _widevine_key(pssh_uri: str, track_id: str, api: _API) -> bytes:
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import PSSH
    from .soundcloud import _resolve_wvd

    # URI like data:text/plain;base64,<kid_b64>  or full pssh
    kid = None
    if "," in pssh_uri:
        try:
            kid = base64.b64decode(pssh_uri.split(",", 1)[1] + "==")
        except Exception:
            kid = None
    if kid and len(kid) == 16:
        pssh = PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[kid])
    elif pssh_uri.startswith("data:") and "base64," in pssh_uri:
        pssh = PSSH(pssh_uri.split("base64,", 1)[1])
    else:
        # try raw base64
        pssh = PSSH(pssh_uri)

    device = Device.load(_resolve_wvd())
    cdm = Cdm.from_device(device)
    sid = cdm.open()
    try:
        challenge = cdm.get_license_challenge(sid, pssh)
        challenge_b64 = base64.b64encode(challenge).decode()
        lic = api.license(track_id, pssh_uri, challenge_b64)
        cdm.parse_license(sid, lic)
        for key in cdm.get_keys(sid):
            if key.type == "CONTENT":
                return key.key if isinstance(key.key, bytes) else bytes.fromhex(key.key.hex())
        raise AppleMusicError("Widevine license had no content key.")
    finally:
        cdm.close(sid)


def _parse_m3u8(text: str, base: str):
    init = None
    segs = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init = urllib.parse.urljoin(base, m.group(1))
        elif line and not line.startswith("#"):
            segs.append(urllib.parse.urljoin(base, line))
    return init, segs


def _legacy_stream(webplayback: dict) -> tuple[str, str, str]:
    """Return (m3u8_url, pssh_uri, m3u8_text) for aac-legacy flavor."""
    assets = webplayback["songList"][0].get("assets") or []
    asset = next((a for a in assets if a.get("flavor") == "28:ctrp256"), None)
    if asset is None:
        asset = next((a for a in assets if a.get("URL")), None)
    if not asset:
        raise AppleMusicError("No AAC stream asset in Apple Music webplayback.")
    m3u8_url = asset["URL"]
    text = ac.http_get(m3u8_url, headers={"User-Agent": ac.UA}).decode("utf-8", "replace")
    # may be master playlist
    if "#EXT-X-STREAM-INF" in text:
        for i, line in enumerate(text.splitlines()):
            if line.startswith("#EXT-X-STREAM-INF"):
                nxt = text.splitlines()[i + 1].strip()
                m3u8_url = urllib.parse.urljoin(m3u8_url, nxt)
                text = ac.http_get(m3u8_url, headers={"User-Agent": ac.UA}).decode(
                    "utf-8", "replace"
                )
                break
    pssh = None
    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                pssh = m.group(1)
                break
    if not pssh:
        raise AppleMusicError("Apple Music playlist missing DRM key URI.")
    return m3u8_url, pssh, text


def probe(url: str) -> dict:
    storefront, tid = _track_id_from_url(url)
    # Public catalog metadata doesn't need media-user-token
    try:
        dev = _developer_token()
        raw = ac.http_get(
            f"{_AMP}/v1/catalog/{storefront}/songs/{tid}?l=en-US",
            headers={
                "User-Agent": ac.UA,
                "Authorization": f"Bearer {dev}",
                "Origin": _HOME,
                "Referer": f"{_HOME}/",
            },
        )
        data = json.loads(raw)
        attrs = (data.get("data") or [{}])[0].get("attributes") or {}
    except Exception as exc:
        raise AppleMusicError("Couldn't read that Apple Music song.") from exc

    duration = (attrs.get("durationInMillis") or 0) / 1000.0 or None
    artists = attrs.get("artistName")
    title = attrs.get("name")
    thumb = None
    if attrs.get("artwork"):
        thumb = (
            attrs["artwork"].get("url", "")
            .replace("{w}", "600").replace("{h}", "600")
        )
    quality = "AAC ~256 kbps"
    need_token = not os.environ.get("APPLE_MEDIA_USER_TOKEN")
    if need_token:
        quality = "AAC (needs APPLE_MEDIA_USER_TOKEN)"
    return ac.probe_payload(
        platform="applemusic",
        url=attrs.get("url") or url,
        title=title,
        uploader=artists,
        duration=duration,
        thumbnail=thumb,
        quality=quality,
        best_size=int(duration * 256 * 125) if duration else None,
    )


def run_download(store: JobStore, job_id: str, url: str, option_id: str,
                 job_dir: str, filename_stem: str | None = None) -> None:
    if option_id not in ac.AUDIO_OPTION_IDS:
        raise AppleMusicError(f"Unknown option '{option_id}'.")
    store.update(job_id, status="downloading", progress=3.0)
    storefront, tid = _track_id_from_url(url)
    api = _API(storefront=storefront)
    store.update(job_id, progress=10.0)

    song = api.get_song(tid)
    attrs = (song.get("data") or [{}])[0].get("attributes") or {}
    title = attrs.get("name")
    artist = attrs.get("artistName")
    album = attrs.get("albumName")

    wp = api.webplayback(tid)
    store.update(job_id, progress=20.0)
    m3u8_url, pssh_uri, m3u8_text = _legacy_stream(wp)
    key = _widevine_key(pssh_uri, tid, api)
    store.update(job_id, progress=30.0)

    init_url, segs = _parse_m3u8(m3u8_text, m3u8_url)
    if not init_url or not segs:
        raise AppleMusicError("Apple Music playlist had no segments.")

    def fetch(u):
        return ac.http_get(u, headers={"User-Agent": ac.UA}, timeout=60)

    init = fetch(init_url)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        seg_data = list(ex.map(fetch, segs))
    store.update(job_id, status="processing", progress=80.0)

    enc_path = os.path.join(job_dir, "enc.mp4")
    with open(enc_path, "wb") as f:
        f.write(init)
        for frag in seg_data:
            f.write(_decrypt_fragment(frag, key))

    raw = os.path.join(job_dir, "raw.m4a")
    ac.ffmpeg(["-i", enc_path, "-c", "copy", "-vn", raw])
    try:
        os.remove(enc_path)
    except OSError:
        pass

    stem = filename_stem or ac.stem_for(title, artist, f"apple-{tid}")
    thumb_url = None
    if attrs.get("artwork"):
        thumb_url = (
            attrs["artwork"].get("url", "")
            .replace("{w}", "600").replace("{h}", "600")
        )
    thumb = ac.fetch_thumb(thumb_url, job_dir)
    ac.finalize_audio(
        store, job_id, job_dir, raw, ".m4a", option_id, stem,
        title=title, artist=artist, album=album, thumb_path=thumb,
    )
