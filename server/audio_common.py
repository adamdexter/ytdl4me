"""Shared helpers for audio-only platform clients (SoundCloud, Deezer, …)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from yt_dlp.utils import sanitize_filename

log = logging.getLogger("ytdl4me.audio")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AUDIO_OPTION_IDS = ("audio_best", "mp3_320", "mp3_256", "mp3_192", "mp3_128")


def http_get(url: str, headers: dict | None = None, data: bytes | None = None,
             timeout: float = 45) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url.split('?', 1)[0]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def audio_options(duration: float | None, quality: str | None,
                  best_size: int | None = None) -> list[dict]:
    opts = [{
        "id": "audio_best",
        "label": "Original (best quality)",
        "detail": f"{quality} · no re-encode" if quality else "no re-encode",
        "approx_size": best_size,
    }]
    for kbps in (320, 256, 192, 128):
        opts.append({
            "id": f"mp3_{kbps}",
            "label": f"MP3 {kbps}",
            "detail": f"{kbps} kbps CBR",
            "approx_size": int(duration * kbps * 125) if duration else None,
        })
    return opts


def probe_payload(*, platform: str, url: str, title: str | None,
                  uploader: str | None, duration: float | None,
                  thumbnail: str | None, quality: str | None,
                  best_size: int | None = None) -> dict:
    return {
        "platform": platform,
        "kind": "audio",
        "url": url,
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "thumbnail": thumbnail,
        "video_options": [],
        "original_quality": quality,
        "audio_options": audio_options(duration, quality, best_size),
    }


def stem_for(title: str | None, artist: str | None, fallback: str) -> str:
    raw = f"{artist} - {title}" if artist and title else (title or artist or fallback)
    return sanitize_filename(raw)[:180] or fallback


def ffmpeg(args: list[str]) -> None:
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", *args],
            capture_output=True, text=True, timeout=600,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required but was not found.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out processing the audio.") from exc
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[-400:]
        raise RuntimeError(f"ffmpeg failed: {err or r.returncode}")


def apply_tags(path: str, *, title: str | None = None, artist: str | None = None,
               album: str | None = None, thumb_path: str | None = None) -> None:
    try:
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, error as ID3Error
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        return
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
                fmt = (
                    MP4Cover.FORMAT_PNG
                    if thumb[:8] == b"\x89PNG\r\n\x1a\n"
                    else MP4Cover.FORMAT_JPEG
                )
                audio["covr"] = [MP4Cover(thumb, imageformat=fmt)]
            audio.save()
    except Exception:
        log.debug("tag write failed for %s", path, exc_info=True)


def finalize_audio(
    store, job_id: str, job_dir: str, raw_path: str, source_ext: str,
    option_id: str, stem: str, *, title=None, artist=None, album=None,
    thumb_path=None,
) -> None:
    """Encode if needed, tag, and mark job done."""
    store.update(job_id, status="processing", progress=92.0, speed=None, eta=None)
    if option_id == "audio_best":
        final = os.path.join(job_dir, f"{stem}{source_ext}")
        if os.path.abspath(raw_path) != os.path.abspath(final):
            os.replace(raw_path, final)
    else:
        bitrate = option_id.rsplit("_", 1)[1]
        final = os.path.join(job_dir, f"{stem}.mp3")
        ffmpeg([
            "-i", raw_path, "-vn",
            "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k",
            final,
        ])
        if os.path.abspath(raw_path) != os.path.abspath(final):
            try:
                os.remove(raw_path)
            except OSError:
                pass
    apply_tags(final, title=title, artist=artist, album=album, thumb_path=thumb_path)
    if thumb_path:
        try:
            os.remove(thumb_path)
        except OSError:
            pass
    store.update(
        job_id,
        status="done",
        progress=100.0,
        speed=None,
        eta=None,
        filepath=os.path.realpath(final),
        filename=os.path.basename(final),
        filesize=os.path.getsize(final),
        total_bytes=os.path.getsize(final),
    )


def fetch_thumb(url: str | None, job_dir: str) -> str | None:
    if not url:
        return None
    try:
        path = os.path.join(job_dir, "cover.jpg")
        Path(path).write_bytes(http_get(url, timeout=20))
        return path
    except Exception:
        return None


def parse_duration_ms(ms) -> float | None:
    if ms is None:
        return None
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return None


def host_matches(hostname: str, suffixes: set[str]) -> bool:
    h = hostname.lower()
    for prefix in ("www.", "m.", "listen.", "open.", "play.", "geo.", "embed."):
        if h.startswith(prefix):
            h = h[len(prefix):]
    return h in suffixes or any(h.endswith("." + s) for s in suffixes)
