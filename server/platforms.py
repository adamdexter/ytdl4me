"""URL platform detection + validation."""
from __future__ import annotations

from urllib.parse import urlparse

_YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "music.youtube.com"}
_VIMEO_HOSTS = {"vimeo.com", "player.vimeo.com"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "on.soundcloud.com", "snd.sc"}
_SPOTIFY_HOSTS = {"open.spotify.com", "spotify.link"}


def detect_platform(url: str) -> str | None:
    """Return "youtube" | "vimeo" | "soundcloud" | "spotify" | "other",
    or None when the input isn't a valid http(s) URL."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    if host in _VIMEO_HOSTS:
        return "vimeo"
    if host in _SOUNDCLOUD_HOSTS:
        return "soundcloud"
    if host in _SPOTIFY_HOSTS:
        return "spotify"
    return "other"


def platform_kind(platform: str) -> str:
    """youtube/vimeo -> "video"; soundcloud/spotify -> "audio"."""
    return "audio" if platform in ("soundcloud", "spotify") else "video"


def looks_like_playlist(url: str, platform: str) -> bool:
    """Cheap URL-shape check for obvious playlist/album/set links."""
    try:
        path = (urlparse(url).path or "").lower()
    except ValueError:
        return False
    if platform == "youtube":
        return path.startswith("/playlist")
    if platform == "soundcloud":
        return "/sets/" in path
    if platform == "spotify":
        return "/album/" in path or "/playlist/" in path
    return False
