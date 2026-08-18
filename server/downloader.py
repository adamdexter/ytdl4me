"""yt-dlp option builders, probe(), run_download() with progress hooks.

Uses the yt-dlp Python API only (no subprocess). Video is never re-encoded
behind the user's back: "Original" and the 1080p/720p MP4 tiers select source
streams and ffmpeg merges with stream copy. Only the explicitly labeled
"4K MP4"/"2K MP4" tiers transcode (YouTube serves nothing but VP9/AV1 above
1080p, which Macs and editors can't open — H.264 is the only editable path).
"""
from __future__ import annotations

import atexit
import base64
import binascii
import os
import re
import shutil
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import determine_protocol, get_compatible_ext, sanitize_filename

from .jobs import JobStore


def _read_seed_cookies() -> bytes | None:
    """Bootstrap cookie bytes from COOKIES_B64 / COOKIES_CONTENT / COOKIES_FILE."""
    b64 = os.environ.get("COOKIES_B64")
    if b64:
        try:
            return base64.b64decode(b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            return None
    raw = os.environ.get("COOKIES_CONTENT")
    if raw:
        return raw.encode("utf-8")
    path = os.environ.get("COOKIES_FILE")
    if path:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None
    return None


def _resolve_cookies() -> tuple[str | None, bool]:
    """Locate the Netscape cookies.txt for yt-dlp and whether to self-renew it.

    With COOKIES_STATE_FILE set (a path on a *persistent* volume), cookies live
    there and yt-dlp's rotated writeback is persisted after each run, so the
    YouTube session renews itself instead of going stale. The state file is
    seeded once from COOKIES_B64/COOKIES_CONTENT/COOKIES_FILE.

    Without it: an explicit COOKIES_FILE path is used read-only (e.g. a Docker
    bind mount), else COOKIES_B64/COOKIES_CONTENT is materialised to a private
    temp file (0600) for the process lifetime. Returns (path_or_None, renewing).
    """
    state = os.environ.get("COOKIES_STATE_FILE")
    if state:
        state_path = os.path.abspath(state)
        try:
            os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        except OSError:
            pass
        if not (os.path.isfile(state_path) and os.path.getsize(state_path) > 0):
            seed = _read_seed_cookies()
            if seed:
                try:
                    with open(state_path, "wb") as f:
                        f.write(seed)
                    os.chmod(state_path, 0o600)
                except OSError:
                    pass
        if os.path.isfile(state_path) and os.path.getsize(state_path) > 0:
            return state_path, True
        return None, False

    path = os.environ.get("COOKIES_FILE")
    if path:
        return path, False

    seed = _read_seed_cookies()  # COOKIES_B64 / COOKIES_CONTENT only here
    if not seed:
        return None, False
    fd, tmp = tempfile.mkstemp(prefix="ytdl4me-cookiesrc-", suffix=".txt")
    with os.fdopen(fd, "wb") as f:
        f.write(seed)
    os.chmod(tmp, 0o600)

    @atexit.register
    def _cleanup() -> None:
        try:
            os.remove(tmp)
        except OSError:
            pass

    return tmp, False


COOKIES_FILE, COOKIES_RENEW = _resolve_cookies()
_cookie_lock = threading.Lock()

PLAYLIST_ERROR = (
    "Playlists aren't supported yet — paste a link to a single video/track."
)

VIDEO_OPTION_IDS = ("original", "h264", "2160p_mp4", "1440p_mp4", "1080p", "720p")
MP3_OPTION_IDS = ("mp3_320", "mp3_256", "mp3_192", "mp3_128")
AUDIO_OPTION_IDS = ("audio_best", *MP3_OPTION_IDS)
ALL_OPTION_IDS = (*VIDEO_OPTION_IDS, *AUDIO_OPTION_IDS)


# Matches both fourcc ("avc1.640028") and plain ("h264") codec strings —
# TikTok/Instagram report the latter, YouTube the former.
_H264_FILTER = "[vcodec~='^(avc|h264)']"


def _mp4_copy_spec(cap: int) -> str:
    # Prefer H.264 + AAC so the stream-copy merge lands in .mp4 (QuickTime /
    # NLE friendly); fall back to any codec rather than failing the download.
    return (
        f"bv*{_H264_FILTER}[height<={cap}]+ba[ext=m4a]/"
        f"bv*{_H264_FILTER}[height<={cap}]+ba/"
        f"b{_H264_FILTER}[height<={cap}]/"
        f"bv*[height<={cap}]+ba/b[height<={cap}]/bv*+ba/b"
    )


def _convert_spec(cap: int) -> str:
    # Above 1080p YouTube only has VP9/AV1. Prefer VP9: same quality class,
    # much faster to decode during the H.264 transcode than AV1.
    return (
        f"bv*[vcodec^=vp09][height<={cap}]+ba/"
        f"bv*[height<={cap}]+ba/b[height<={cap}]/bv*+ba/b"
    )


_FORMAT_SPECS = {
    "original": "bv*+ba/b",
    # Best H.264 at any resolution — the right "editable MP4" choice for
    # social video (Instagram/TikTok), where a height cap would punish
    # portrait 1080x1920 sources.
    "h264": (
        f"bv*{_H264_FILTER}+ba[ext=m4a]/"
        f"bv*{_H264_FILTER}+ba/"
        f"b{_H264_FILTER}/"
        "bv*+ba/b"
    ),
    "2160p_mp4": _convert_spec(2160),
    "1440p_mp4": _convert_spec(1440),
    "1080p": _mp4_copy_spec(1080),
    "720p": _mp4_copy_spec(720),
}

_MP4_COPY_IDS = {"h264", "1080p", "720p"}
_MP4_CONVERT_IDS = {"2160p_mp4", "1440p_mp4"}

# High-quality H.264 for the >1080p MP4 tiers: CRF 18 is visually lossless
# territory, veryfast keeps a 4K encode tolerable on a small server, and
# yuv420p (8-bit 4:2:0) is what QuickTime/editors require. Audio becomes AAC.
# yt-dlp itself appends -movflags +faststart to postprocessor output args.
_CONVERT_FFMPEG_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k",
]

# Some SoundCloud tracks expose direct progressive HTTP media in addition to
# HLS. Prefer the direct file so downloads run at network speed instead of
# behaving like a realtime stream, while keeping HLS as a fallback.
_SOUNDCLOUD_AUDIO_FORMAT = "bestaudio[protocol=http]/bestaudio[protocol=https]/bestaudio"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@contextmanager
def _cookies_copy():
    """Yield the path of a private, writable copy of COOKIES_FILE (or None).

    yt-dlp rewrites the cookie file when a YoutubeDL context exits, so pointing
    concurrent probes/downloads at the shared file corrupts it (no lock, no
    atomic rename) and a read-only mount (e.g. Docker ":ro") raises on exit.
    Each YoutubeDL gets its own throwaway copy instead.

    When renewing, the (rotated) copy is atomically written back to the state
    file under a lock so the session stays fresh across runs and restarts; the
    temp lives beside the state file so the replace is same-filesystem."""
    if not COOKIES_FILE:
        yield None
        return
    tmp_dir = os.path.dirname(COOKIES_FILE) if COOKIES_RENEW else None
    fd, path = tempfile.mkstemp(prefix="ytdl4me-cookies-", suffix=".txt", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as tmp, open(COOKIES_FILE, "rb") as src:
            shutil.copyfileobj(src, tmp)
    except OSError:
        # Unreadable/missing cookie file: proceed without cookies rather than
        # failing every request.
        try:
            os.remove(path)
        except OSError:
            pass
        yield None
        return
    try:
        yield path
    finally:
        if COOKIES_RENEW and os.path.isfile(path) and os.path.getsize(path) > 0:
            with _cookie_lock:
                try:
                    os.replace(path, COOKIES_FILE)  # persist rotated cookies
                    path = None
                except OSError:
                    pass
        if path is not None:
            try:
                os.remove(path)
            except OSError:
                pass


class ProbeError(Exception):
    """Probe failed; str(exc) is safe to show to the user (HTTP 422)."""


class PlaylistError(ProbeError):
    def __init__(self, message: str = PLAYLIST_ERROR) -> None:
        super().__init__(message)


class DownloadFailed(Exception):
    """Download finished abnormally; str(exc) is user-facing."""


# ---------------------------------------------------------------------------
# Option builders
# ---------------------------------------------------------------------------

def build_ydl_opts(
    option_id: str,
    job_dir: str,
    progress_hook=None,
    pp_hook=None,
    filename_stem: str | None = None,
    platform: str | None = None,
) -> dict:
    if filename_stem:
        stem = sanitize_filename(filename_stem).replace("%", "%%")
        outtmpl = os.path.join(job_dir, f"{stem}.%(ext)s")
    else:
        outtmpl = os.path.join(job_dir, "%(title).180B [%(id)s].%(ext)s")

    opts: dict = {
        "outtmpl": {"default": outtmpl},
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "retries": 3,
        "fragment_retries": 5,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook] if progress_hook else [],
        "postprocessor_hooks": [pp_hook] if pp_hook else [],
    }
    # cookiefile is injected per-run from a private copy — see _cookies_copy().

    if option_id in _FORMAT_SPECS:
        opts["format"] = _FORMAT_SPECS[option_id]
        if option_id in _MP4_COPY_IDS:
            # Stream-copy merge; lands in .mp4 whenever the picked codecs
            # allow it (H.264 + AAC nearly always), .mkv as the safe fallback.
            opts["merge_output_format"] = "mp4/mkv"
        elif option_id in _MP4_CONVERT_IDS:
            opts["postprocessors"] = [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
            ]
            opts["postprocessor_args"] = {"videoconvertor": _CONVERT_FFMPEG_ARGS}
        # "original" stays a stream-copy merge into whatever container fits
        # (mp4 / webm / mkv) — bit-exact source streams.
    elif option_id in AUDIO_OPTION_IDS:
        opts["format"] = (
            _SOUNDCLOUD_AUDIO_FORMAT if platform == "soundcloud" else "bestaudio/b"
        )
        opts["writethumbnail"] = True
        if option_id == "audio_best":
            # Bit-exact copy of the source stream into its native container.
            extract = {"key": "FFmpegExtractAudio", "preferredcodec": "best"}
        else:
            bitrate = option_id.rsplit("_", 1)[1]
            extract = {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,  # CBR bitrate for libmp3lame
            }
        opts["postprocessors"] = [
            extract,
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ]
    else:
        raise ValueError(f"unknown option_id: {option_id!r}")
    return opts


def _probe_opts() -> dict:
    # cookiefile is injected per-run from a private copy — see _cookies_copy().
    return {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
    }


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def probe(url: str, platform: str) -> dict:
    """Blocking metadata probe (call via asyncio.to_thread)."""
    # Custom clients for services yt-dlp can't (or shouldn't) handle alone.
    _CUSTOM = {
        "soundcloud": ("soundcloud", "SoundCloudError"),
        "deezer": ("deezer", "DeezerError"),
        "joox": ("joox", "JooxError"),
        "tidal": ("tidal", "TidalError"),
        "applemusic": ("applemusic", "AppleMusicError"),
        "beatport": ("beatport", "BeatportError"),
    }
    if platform in _CUSTOM:
        mod_name, _err_name = _CUSTOM[platform]
        import importlib
        mod = importlib.import_module(f".{mod_name}", __package__)
        try:
            return mod.probe(url)
        except Exception as exc:
            raise ProbeError(str(exc)) from exc

    info = _extract(url, platform)
    if platform == "instagram" and info.get("_type") in ("playlist", "multi_video"):
        return _carousel_payload(info, url)
    formats = info.get("formats") or ([info] if info.get("url") else [])
    if platform == "instagram":
        # Raw (unprocessed) extraction: formats aren't yt-dlp-sorted, so
        # restore the worst-to-best order the pickers below rely on.
        formats = _sort_raw_formats(formats)

    if platform in ("youtube", "vimeo", "instagram", "tiktok"):
        kind = "video"
    elif platform in ("soundcloud", "spotify"):
        kind = "audio"
    else:
        kind = "video" if _pick_video(formats) else "audio"

    duration = float(info["duration"]) if info.get("duration") else None
    best_audio = _pick_audio(formats, prefer_direct=platform == "soundcloud")

    payload = {
        "platform": platform,
        "kind": kind,
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("artist"),
        "duration": duration,
        "thumbnail": info.get("thumbnail") or _best_thumbnail(info),
    }
    if kind == "video":
        options, quality = _video_options(formats, best_audio, platform)
        payload["video_options"] = options
        payload["original_quality"] = quality
    else:
        payload["video_options"] = []
        payload["original_quality"] = _audio_quality(best_audio)
    payload["audio_options"] = _audio_options(best_audio, duration)
    return payload


def _extract(url: str, platform: str | None = None) -> dict:
    opts = _probe_opts()
    is_search = url.startswith("ytsearch")
    # Instagram: skip processing so a mixed video+image carousel doesn't abort
    # on the first image entry (processing raises "No video formats found").
    # The raw ie_result carries everything the probe needs.
    raw = platform == "instagram"
    if not is_search and not raw:
        # Keep playlist probes cheap: flat entries, capped, so the playlist
        # check below never crawls individual videos.
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = 5
    try:
        with _cookies_copy() as cookies:
            if cookies:
                opts["cookiefile"] = cookies
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=not raw)
    except yt_dlp.utils.DownloadError as exc:
        raise ProbeError(friendly_error(exc)) from exc
    if not info:
        raise ProbeError("Couldn't read anything from that link.")
    if info.get("_type") in ("playlist", "multi_video"):
        if is_search:
            entries = list(info.get("entries") or [])
            if not entries or not entries[0]:
                raise ProbeError("No YouTube match was found for that track.")
            return entries[0]
        if platform == "instagram":
            return info  # carousel post — probe() builds a payload for it
        raise PlaylistError()
    return info


def _is_h264(f: dict) -> bool:
    return (f.get("vcodec") or "").lower().startswith(("avc", "h264"))


def _sort_raw_formats(formats: list[dict]) -> list[dict]:
    """Worst-to-best by pixel count then bitrate (labels only — downloads go
    through yt-dlp's own format selection, which sorts properly)."""
    return sorted(formats, key=lambda f: (
        (f.get("height") or 0) * (f.get("width") or 0),
        f.get("tbr") or 0,
    ))


def _best_thumbnail(info: dict) -> str | None:
    """Unprocessed ie_results only carry a thumbnails list (worst-to-best)."""
    thumbs = info.get("thumbnails") or []
    return (thumbs[-1] or {}).get("url") if thumbs else None


def _carousel_videos(info: dict) -> list[dict]:
    """Video entries of an Instagram carousel (image items have no formats)."""
    return [e for e in (info.get("entries") or []) if e and e.get("formats")]


def _carousel_payload(info: dict, url: str) -> dict:
    """Probe payload for an Instagram carousel post: one card, one job that
    downloads every video in the post (ZIP when there's more than one)."""
    videos = _carousel_videos(info)
    if not videos:
        raise ProbeError(
            "That Instagram post only contains images — nothing to download."
        )
    n = len(videos)
    noun = f"{n} video{'s' if n != 1 else ''}"
    zip_note = " · ZIP" if n > 1 else ""
    first = videos[0]
    duration = sum(float(e.get("duration") or 0) for e in videos) or None
    return {
        "platform": "instagram",
        "kind": "video",
        "url": info.get("webpage_url") or url,
        "title": info.get("title") or first.get("title"),
        "uploader": info.get("channel") or info.get("uploader"),
        "duration": duration,
        "thumbnail": _best_thumbnail(first) or _best_thumbnail(info),
        "original_quality": f"Carousel · {noun}",
        "video_options": [
            {"id": "original", "label": "Original",
             "detail": f"{noun} · best available{zip_note}",
             "height": None, "approx_size": None},
            {"id": "h264", "label": "MP4 (H.264)",
             "detail": f"{noun} · most compatible{zip_note}",
             "height": None, "approx_size": None},
        ],
        "audio_options": _audio_options(None, None),
    }


def _pick_video(
    formats: list[dict], cap: int | None = None, prefer_avc: bool = False
) -> dict | None:
    """Best video format (yt-dlp lists formats worst-to-best) under a height cap.

    With prefer_avc, the best H.264 format under the cap wins even when a
    VP9/AV1 format is ranked higher — mirroring the _mp4_copy_spec selector —
    falling back to any codec when no H.264 exists."""
    def pick(require_avc: bool) -> dict | None:
        for f in reversed(formats):
            if f.get("vcodec") in (None, "none") or f.get("ext") == "mhtml":
                continue
            if require_avc and not _is_h264(f):
                continue
            height = f.get("height")
            if cap is not None and (not height or height > cap):
                continue
            return f
        return None

    if prefer_avc:
        found = pick(True)
        if found is not None:
            return found
    return pick(False)


def _is_audio_only(f: dict) -> bool:
    if f.get("acodec") in (None, "none"):
        return False
    if f.get("vcodec") not in (None, "none"):
        return False
    return True


def _format_protocol(f: dict) -> str | None:
    try:
        return f.get("protocol") or determine_protocol(f)
    except Exception:
        return f.get("protocol")


def _pick_audio(formats: list[dict], prefer_direct: bool = False) -> dict | None:
    if prefer_direct:
        for f in reversed(formats):
            if _is_audio_only(f) and _format_protocol(f) in ("http", "https"):
                return f
    for f in reversed(formats):
        if _is_audio_only(f):
            return f
    return None


def _codec_name(codec: str | None) -> str | None:
    if not codec or codec == "none":
        return None
    c = codec.lower()
    if c.startswith(("avc", "h264")):
        return "H.264"
    if c.startswith(("av01", "av1")):
        return "AV1"
    if c.startswith(("vp09", "vp9")):
        return "VP9"
    if c.startswith("vp8"):
        return "VP8"
    if c.startswith(("hev", "hvc", "h265")):
        return "H.265"
    if c.startswith("opus"):
        return "Opus"
    if c.startswith(("mp4a", "aac")):
        return "AAC"
    if c.startswith("mp3"):
        return "MP3"
    if c.startswith("flac"):
        return "FLAC"
    if c.startswith("vorbis"):
        return "Vorbis"
    return codec.split(".")[0].upper()


def _res_label(f: dict | None) -> str | None:
    """Colloquial resolution: the SHORT side (a portrait 1080x1920 reel is
    "1080p", same as landscape 1920x1080), fps suffix above 30."""
    if not f:
        return None
    height, width = f.get("height"), f.get("width")
    side = min(height, width) if height and width else height
    if not side:
        return None
    label = f"{side}p"
    fps = f.get("fps")
    if fps and fps > 30:
        label += str(int(round(fps)))
    return label


def _size_of(f: dict | None) -> int | None:
    if not f:
        return None
    size = f.get("filesize") or f.get("filesize_approx")
    return int(size) if size else None


def _pair_size(video_fmt: dict, best_audio: dict | None) -> int | None:
    total = _size_of(video_fmt)
    if total is None:
        return None
    if video_fmt.get("acodec") in (None, "none"):
        total += _size_of(best_audio) or 0
    return total


def _fmt_ext(f: dict) -> str | None:
    # Unprocessed formats (e.g. raw Instagram probes) may lack 'ext'.
    ext = f.get("ext")
    if ext:
        return ext
    tail = os.path.splitext(urlparse(f.get("url") or "").path)[1].lstrip(".").lower()
    return tail or None


def _merged_ext(video_fmt: dict, best_audio: dict | None) -> str | None:
    """Container the download will actually land in (post yt-dlp merge)."""
    if video_fmt.get("acodec") not in (None, "none") or not best_audio:
        return _fmt_ext(video_fmt)
    try:
        return get_compatible_ext(
            vcodecs=[video_fmt.get("vcodec")],
            acodecs=[best_audio.get("acodec")],
            vexts=[_fmt_ext(video_fmt)],
            aexts=[_fmt_ext(best_audio)],
        )
    except Exception:
        return _fmt_ext(video_fmt)


def _video_options(formats: list[dict], best_audio: dict | None, platform: str | None = None):
    original = _pick_video(formats)
    if original is None:
        return [], None
    height = original.get("height")
    res = _res_label(original)
    codec = _codec_name(original.get("vcodec"))
    quality = f"{res} ({codec})" if res and codec else (res or codec)

    ext = _merged_ext(original, best_audio)
    options = [{
        "id": "original",
        "label": "Original",
        "detail": " · ".join(
            p for p in (res, codec, f".{ext}" if ext else None) if p
        ) or None,
        "height": height,
        "approx_size": _pair_size(original, best_audio),
    }]

    # Social video: portrait sources make height-capped tiers meaningless, and
    # the source is H.264/H.265 mp4 already — offer Original plus best-H.264.
    if platform in ("instagram", "tiktok"):
        f = _pick_video(formats, prefer_avc=True)
        if f is not None and _is_h264(f) and f.get("format_id") != original.get("format_id"):
            options.append({
                "id": "h264",
                "label": "MP4 (H.264)",
                "detail": " · ".join(
                    p for p in (_res_label(f), "H.264 · no re-encode") if p
                ),
                "height": f.get("height"),
                "approx_size": _pair_size(f, best_audio),
            })
        return options, quality

    # >1080p MP4 tiers exist only as a transcode (no H.264 sources up there).
    # Skipped when the source at that cap is already H.264 (e.g. Vimeo) —
    # "Original" or a copy tier covers it without a re-encode.
    for cap, name in ((2160, "4K"), (1440, "2K")):
        if not height or height < cap:
            continue
        src = _pick_video(formats, cap)
        if src is None or _is_h264(src):
            continue
        options.append({
            "id": f"{cap}p_mp4",
            "label": f"{name} MP4",
            "detail": "H.264 · converted for editing (slower)",
            "height": src.get("height") or cap,
            "approx_size": None,
        })

    seen = {original.get("format_id")}
    for cap in (1080, 720):
        if not height or height < cap:
            continue
        f = _pick_video(formats, cap, prefer_avc=True)
        if f is None or f.get("format_id") in seen:
            continue
        seen.add(f.get("format_id"))
        pick_res = _res_label(f) or f"{cap}p"
        options.append({
            "id": f"{cap}p",
            "label": f"{pick_res} MP4" if _is_h264(f) else pick_res,
            "detail": (
                "H.264 · no re-encode" if _is_h264(f)
                else _codec_name(f.get("vcodec"))
            ),
            "height": f.get("height") or cap,
            "approx_size": _pair_size(f, best_audio),
        })
    return options, quality


def _audio_quality(best_audio: dict | None) -> str | None:
    if not best_audio:
        return None
    codec = _codec_name(best_audio.get("acodec")) or (best_audio.get("ext") or "").upper() or None
    abr = best_audio.get("abr") or best_audio.get("tbr")
    if codec and abr:
        return f"{codec} ~{int(round(abr))} kbps"
    return codec


def _audio_options(best_audio: dict | None, duration: float | None) -> list[dict]:
    codec = _codec_name(best_audio.get("acodec")) if best_audio else None
    options = [{
        "id": "audio_best",
        "label": "Original (best quality)",
        "detail": f"{codec} · no re-encode" if codec else "no re-encode",
        "approx_size": _size_of(best_audio),
    }]
    for kbps in (320, 256, 192, 128):
        options.append({
            "id": f"mp3_{kbps}",
            "label": f"MP3 {kbps}",
            "detail": f"{kbps} kbps CBR",
            "approx_size": int(duration * kbps * 125) if duration else None,
        })
    return options


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

# URL path segment → human kind for social filenames.
_SOCIAL_KIND_SEGMENTS = (
    ("/reels/", "reel"), ("/reel/", "reel"), ("/stories/", "story"),
    ("/tv/", "igtv"), ("/p/", "post"), ("/video/", "video"), ("/photo/", "photo"),
)
_SOCIAL_CODE_RE = re.compile(r"/(?:reels?|p|tv|video|photo)/([A-Za-z0-9_-]+)")


def _social_stem(info: dict, url: str, platform: str) -> str | None:
    """Filename stem for Instagram/TikTok downloads:
    handle - YYYY-MM-DD - first caption words - reel|post|story|video - permalink id
    Empty segments are dropped; sanitize_filename runs on the result."""
    if platform == "tiktok":
        handle = info.get("uploader") or info.get("uploader_id")
    else:
        # Instagram: channel = username (primary owner on collab posts);
        # uploader_id is a numeric pk, uploader the display name.
        uid = info.get("uploader_id")
        handle = (
            info.get("channel")
            or (uid if uid and not str(uid).isdigit() else None)
            or info.get("uploader")
        )
    handle = (handle or "").strip().lstrip("@") or None

    date = info.get("upload_date")  # YYYYMMDD (may be absent pre-processing)
    if date and len(str(date)) == 8:
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    elif info.get("timestamp"):
        try:
            date = datetime.fromtimestamp(
                float(info["timestamp"]), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            date = None
    else:
        date = None

    caption = " ".join((info.get("description") or info.get("title") or "").split())
    words: list[str] = []
    length = 0
    for w in caption.split():
        add = len(w) + (1 if words else 0)
        if words and (length + add > 44 or len(words) >= 6):
            break
        words.append(w)
        length += add
    snippet = " ".join(words) or None

    path = (urlparse(info.get("webpage_url") or url).path or "").lower()
    kind = next((k for seg, k in _SOCIAL_KIND_SEGMENTS if seg in path), None)
    if kind is None:
        kind = "video" if platform == "tiktok" else "post"

    code = None
    m = _SOCIAL_CODE_RE.search(urlparse(info.get("webpage_url") or url).path or "")
    if m:
        code = m.group(1)
    code = code or (str(info["id"]) if info.get("id") else None)

    parts = [p for p in (handle, date, snippet, kind, code) if p]
    return " - ".join(parts) if parts else None


def run_download(
    store: JobStore,
    job_id: str,
    url: str,
    option_id: str,
    job_dir: str,
    filename_stem: str | None = None,
    tags: dict | None = None,
    platform: str | None = None,
) -> None:
    """Blocking download (call via asyncio.to_thread). Raises on failure."""
    _CUSTOM = {
        "soundcloud": "soundcloud",
        "deezer": "deezer",
        "joox": "joox",
        "tidal": "tidal",
        "applemusic": "applemusic",
        "beatport": "beatport",
    }
    if platform in _CUSTOM:
        import importlib
        mod = importlib.import_module(f".{_CUSTOM[platform]}", __package__)
        try:
            mod.run_download(store, job_id, url, option_id, job_dir, filename_stem)
        except Exception as exc:
            raise DownloadFailed(str(exc)) from exc
        return

    holder: dict = {}

    def progress_hook(d: dict) -> None:
        # Fires from the worker thread; must be cheap and never raise.
        try:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes")
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                fields = {
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": int(total) if total else None,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                }
                if downloaded and total:
                    fields["progress"] = min(100.0, downloaded / total * 100.0)
                store.update(job_id, **fields)
            elif status == "finished":
                holder["progress_file"] = d.get("filename")
        except Exception:
            pass

    def pp_hook(d: dict) -> None:
        try:
            if d.get("status") == "started":
                store.update(job_id, status="processing", speed=None, eta=None)
            filepath = (d.get("info_dict") or {}).get("filepath")
            if filepath:
                holder["pp_file"] = filepath
        except Exception:
            pass

    opts = build_ydl_opts(
        option_id, job_dir, progress_hook, pp_hook, filename_stem, platform
    )
    store.update(job_id, status="downloading")
    carousel = False
    carousel_base = None
    try:
        with _cookies_copy() as cookies:
            if cookies:
                opts["cookiefile"] = cookies
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Two-phase so pure playlist URLs error out instead of expanding
                # (noplaylist only guards watch URLs that carry a &list= param).
                info = ydl.extract_info(url, download=False, process=False)
                if not info:
                    raise DownloadFailed("Couldn't read anything from that link.")
                is_list = info.get("_type") in ("playlist", "multi_video")
                carousel = is_list and platform == "instagram"
                if is_list and not carousel and not url.startswith("ytsearch"):
                    raise DownloadFailed(PLAYLIST_ERROR)
                stem = None
                if platform in ("instagram", "tiktok") and not filename_stem:
                    # handle - date - caption words - kind - permalink id
                    stem = _social_stem(info, url, platform)
                if carousel:
                    # One job downloads every video in the post; image items
                    # must be dropped up front or processing aborts on them.
                    videos = _carousel_videos(info)
                    if not videos:
                        raise DownloadFailed(
                            "That Instagram post only contains images — "
                            "nothing to download."
                        )
                    info["entries"] = videos
                    carousel_base = sanitize_filename(
                        filename_stem or stem or "instagram post"
                    )[:180]
                    escaped = carousel_base.replace("%", "%%")
                    tmpl = (
                        f"{escaped} - %(playlist_index)02d.%(ext)s"
                        if len(videos) > 1 else f"{escaped}.%(ext)s"
                    )
                    ydl.params["outtmpl"]["default"] = os.path.join(job_dir, tmpl)
                elif stem:
                    stem = sanitize_filename(stem).replace("%", "%%")
                    # prepare_filename reads params['outtmpl'] at call time,
                    # so swapping it before processing is safe.
                    ydl.params["outtmpl"]["default"] = os.path.join(
                        job_dir, f"{stem}.%(ext)s"
                    )
                ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadFailed(friendly_error(exc)) from exc

    if carousel:
        final = _carousel_final(job_dir, carousel_base)
    else:
        final = _final_path(holder, job_dir)
    if final is None:
        raise DownloadFailed("Download finished but no output file was found.")
    if tags:
        _apply_tags(final, tags)
    store.update(
        job_id,
        status="done",
        progress=100.0,
        speed=None,
        eta=None,
        filepath=final,
        filename=os.path.basename(final),
        filesize=os.path.getsize(final),
    )


_TEMP_SUFFIXES = {
    ".part", ".ytdl", ".temp", ".tmp", ".frag",
    ".webp", ".jpg", ".jpeg", ".png", ".json",
}


def _carousel_final(job_dir: str, base: str | None) -> str | None:
    """One file → serve it directly; several → bundle into a ZIP."""
    files = sorted(
        p for p in Path(job_dir).iterdir()
        if p.is_file() and p.suffix.lower() not in _TEMP_SUFFIXES
    )
    if not files:
        return None
    if len(files) == 1:
        return str(files[0].resolve())
    zip_path = Path(job_dir) / f"{base or 'instagram post'}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.name)
    return str(zip_path.resolve())


def _final_path(holder: dict, job_dir: str) -> str | None:
    for key in ("pp_file", "progress_file"):
        path = holder.get(key)
        if path and os.path.isfile(path):
            return os.path.realpath(path)
    candidates = [
        p for p in Path(job_dir).iterdir()
        if p.is_file() and p.suffix.lower() not in _TEMP_SUFFIXES
    ]
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def _apply_tags(path: str, tags: dict) -> None:
    """Best-effort artist/title tagging (used for Spotify-sourced files)."""
    try:
        import mutagen

        audio = mutagen.File(path, easy=True)
        if audio is None:
            return
        if tags.get("title"):
            audio["title"] = tags["title"]
        if tags.get("artist"):
            audio["artist"] = tags["artist"]
        audio.save()
    except Exception:
        pass


def friendly_error(exc: BaseException) -> str:
    msg = _ANSI_RE.sub("", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    lower = msg.lower()
    if "sign in to confirm" in lower or "not a bot" in lower:
        return ("YouTube is asking this server to sign in to prove it's not a bot "
                "(common on cloud IPs). Add browser cookies — see the README "
                "\"YouTube bot check\" section — and try again.")
    if "drm" in lower:
        return ("This track uses platform DRM that couldn't be unlocked on this "
                "server. For SoundCloud, ensure a Widevine device is available "
                "(WIDEVINE_DEVICE_FILE / WIDEVINE_DEVICE_B64) and try again.")
    if "rate-limit reached or login required" in lower or (
        "instagram" in lower and ("login" in lower or "logged" in lower)
    ):
        return ("Instagram is blocking anonymous access from this server. Add "
                "instagram.com cookies to the configured cookies.txt (same file "
                "as the YouTube cookies) and try again.")
    if "private" in lower:
        return "That video is private."
    if "members-only" in lower or "join this channel" in lower:
        return "That video is members-only."
    if "not available in your country" in lower or "geo restrict" in lower or "geo-restrict" in lower:
        return "That content isn't available in this server's region."
    if "video unavailable" in lower or "no longer available" in lower:
        return "That video is unavailable — it may have been removed."
    if "unsupported url" in lower:
        return "That link isn't supported."
    if "429" in lower or "too many requests" in lower:
        return "The source is rate-limiting this server — try again in a few minutes."
    return msg[:300] or "Download failed."
