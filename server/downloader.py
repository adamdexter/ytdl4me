"""yt-dlp option builders, probe(), run_download() with progress hooks.

Uses the yt-dlp Python API only (no subprocess). Video is never re-encoded:
quality tiers select source streams; ffmpeg merges with stream copy.
"""
from __future__ import annotations

import atexit
import base64
import binascii
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yt_dlp
from yt_dlp.utils import sanitize_filename

from .jobs import JobStore


def _resolve_cookies_file() -> str | None:
    """Locate a Netscape cookies.txt for yt-dlp.

    Priority: COOKIES_FILE (a path, e.g. a Docker bind mount) > COOKIES_B64
    (base64 of a cookies.txt) > COOKIES_CONTENT (raw cookies.txt). The env-var
    forms exist for hosts like Railway/Fly where you can't easily mount a file:
    paste the cookies into a variable and we materialise it to a private temp
    file (0600) for the process lifetime.
    """
    path = os.environ.get("COOKIES_FILE")
    if path:
        return path

    data: bytes | None = None
    b64 = os.environ.get("COOKIES_B64")
    raw = os.environ.get("COOKIES_CONTENT")
    if b64:
        try:
            data = base64.b64decode(b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            data = None
    elif raw:
        data = raw.encode("utf-8")
    if not data:
        return None

    fd, tmp = tempfile.mkstemp(prefix="ytdl4me-cookiesrc-", suffix=".txt")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)

    @atexit.register
    def _cleanup() -> None:
        try:
            os.remove(tmp)
        except OSError:
            pass

    return tmp


COOKIES_FILE = _resolve_cookies_file()

PLAYLIST_ERROR = (
    "Playlists aren't supported yet — paste a link to a single video/track."
)

VIDEO_OPTION_IDS = ("original", "1080p", "720p")
MP3_OPTION_IDS = ("mp3_320", "mp3_256", "mp3_192", "mp3_128")
AUDIO_OPTION_IDS = ("audio_best", *MP3_OPTION_IDS)
ALL_OPTION_IDS = (*VIDEO_OPTION_IDS, *AUDIO_OPTION_IDS)

_FORMAT_SPECS = {
    "original": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@contextmanager
def _cookies_copy():
    """Yield the path of a private, writable copy of COOKIES_FILE (or None).

    yt-dlp rewrites the cookie file when a YoutubeDL context exits, so pointing
    concurrent probes/downloads at the shared file corrupts it (no lock, no
    atomic rename) and a read-only mount (e.g. Docker ":ro") raises on exit.
    Each YoutubeDL gets its own throwaway copy instead."""
    if not COOKIES_FILE:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix="ytdl4me-cookies-", suffix=".txt")
    try:
        try:
            with os.fdopen(fd, "wb") as tmp, open(COOKIES_FILE, "rb") as src:
                shutil.copyfileobj(src, tmp)
        except OSError:
            # Unreadable/missing cookie file: proceed without cookies rather
            # than failing every request.
            yield None
            return
        yield path
    finally:
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
        # Never re-encode: yt-dlp picks streams, ffmpeg merges with stream copy
        # into whatever container fits (mp4 / webm / mkv).
        opts["format"] = _FORMAT_SPECS[option_id]
    elif option_id in AUDIO_OPTION_IDS:
        opts["format"] = "bestaudio/b"
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
    info = _extract(url)
    formats = info.get("formats") or ([info] if info.get("url") else [])

    if platform in ("youtube", "vimeo"):
        kind = "video"
    elif platform in ("soundcloud", "spotify"):
        kind = "audio"
    else:
        kind = "video" if _pick_video(formats) else "audio"

    duration = float(info["duration"]) if info.get("duration") else None
    best_audio = _pick_audio(formats)

    payload = {
        "platform": platform,
        "kind": kind,
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel") or info.get("artist"),
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
    }
    if kind == "video":
        options, quality = _video_options(formats, best_audio)
        payload["video_options"] = options
        payload["original_quality"] = quality
    else:
        payload["video_options"] = []
        payload["original_quality"] = _audio_quality(best_audio)
    payload["audio_options"] = _audio_options(best_audio, duration)
    return payload


def _extract(url: str) -> dict:
    opts = _probe_opts()
    is_search = url.startswith("ytsearch")
    if not is_search:
        # Keep playlist probes cheap: flat entries, capped, so the playlist
        # check below never crawls individual videos.
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = 5
    try:
        with _cookies_copy() as cookies:
            if cookies:
                opts["cookiefile"] = cookies
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
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
        raise PlaylistError()
    return info


def _pick_video(formats: list[dict], cap: int | None = None) -> dict | None:
    """Best video format (yt-dlp lists formats worst-to-best) under a height cap."""
    for f in reversed(formats):
        if f.get("vcodec") in (None, "none") or f.get("ext") == "mhtml":
            continue
        height = f.get("height")
        if cap is not None and (not height or height > cap):
            continue
        return f
    return None


def _pick_audio(formats: list[dict]) -> dict | None:
    for f in reversed(formats):
        if f.get("acodec") in (None, "none"):
            continue
        if f.get("vcodec") not in (None, "none"):
            continue  # combined format, not a pure audio stream
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


def _res_label(height, fps) -> str | None:
    if not height:
        return None
    label = f"{height}p"
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


def _video_options(formats: list[dict], best_audio: dict | None):
    original = _pick_video(formats)
    if original is None:
        return [], None
    height = original.get("height")
    res = _res_label(height, original.get("fps"))
    codec = _codec_name(original.get("vcodec"))
    quality = f"{res} ({codec})" if res and codec else (res or codec)

    options = [{
        "id": "original",
        "label": "Original",
        "detail": " · ".join(p for p in (res, codec) if p) or None,
        "height": height,
        "approx_size": _pair_size(original, best_audio),
    }]
    for cap in (1080, 720):
        if not height or height <= cap:
            continue
        f = _pick_video(formats, cap)
        if f is None:
            continue
        options.append({
            "id": f"{cap}p",
            "label": f"{cap}p",
            "detail": _codec_name(f.get("vcodec")),
            "height": cap,
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

def run_download(
    store: JobStore,
    job_id: str,
    url: str,
    option_id: str,
    job_dir: str,
    filename_stem: str | None = None,
    tags: dict | None = None,
) -> None:
    """Blocking download (call via asyncio.to_thread). Raises on failure."""
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

    opts = build_ydl_opts(option_id, job_dir, progress_hook, pp_hook, filename_stem)
    store.update(job_id, status="downloading")
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
                if info.get("_type") in ("playlist", "multi_video") and not url.startswith("ytsearch"):
                    raise DownloadFailed(PLAYLIST_ERROR)
                ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadFailed(friendly_error(exc)) from exc

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
