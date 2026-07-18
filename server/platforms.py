"""URL platform detection + validation."""
from __future__ import annotations

from urllib.parse import urlparse

_YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "music.youtube.com"}
_VIMEO_HOSTS = {"vimeo.com", "player.vimeo.com"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "on.soundcloud.com", "snd.sc"}
_SPOTIFY_HOSTS = {"spotify.com", "open.spotify.com", "spotify.link"}
_DEEZER_HOSTS = {"deezer.com", "deezer.page.link"}
_JOOX_HOSTS = {"joox.com"}
# After _host() strips listen./embed., bare tidal.com remains.
_TIDAL_HOSTS = {"tidal.com"}
_APPLE_HOSTS = {
    "music.apple.com", "itunes.apple.com", "apple.com",
}
_BEATPORT_HOSTS = {"beatport.com", "pro.beatport.com", "stream.beatport.com"}

# Platforms that are always audio-only in this app.
_AUDIO_PLATFORMS = {
    "soundcloud", "spotify", "deezer", "joox", "tidal", "applemusic", "beatport",
}


def _host(hostname: str) -> str:
    host = hostname.lower()
    for prefix in ("www.", "m.", "listen.", "open.", "play.", "geo.", "embed."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def detect_platform(url: str) -> str | None:
    """Return platform id or "other", or None when not a valid http(s) URL."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = _host(parsed.hostname)
    if host in _YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        return "youtube"
    if host in _VIMEO_HOSTS:
        return "vimeo"
    if host in _SOUNDCLOUD_HOSTS or host.endswith(".soundcloud.com"):
        return "soundcloud"
    if host in _SPOTIFY_HOSTS or host.endswith(".spotify.com"):
        return "spotify"
    if host in _DEEZER_HOSTS or host.endswith(".deezer.com"):
        return "deezer"
    if host in _JOOX_HOSTS or host.endswith(".joox.com"):
        return "joox"
    if host in _TIDAL_HOSTS or host.endswith(".tidal.com"):
        return "tidal"
    if host in _APPLE_HOSTS or (host.endswith(".apple.com") and "music" in host):
        return "applemusic"
    if host in ("apple.com",) and "/music" in (parsed.path or ""):
        return "applemusic"
    if host in _BEATPORT_HOSTS or host.endswith(".beatport.com"):
        return "beatport"
    return "other"


def platform_kind(platform: str) -> str:
    """youtube/vimeo -> video; music services -> audio."""
    return "audio" if platform in _AUDIO_PLATFORMS else "video"


def looks_like_playlist(url: str, platform: str) -> bool:
    """Cheap URL-shape check for obvious playlist/album/set links."""
    try:
        path = (urlparse(url).path or "").lower()
        query = (urlparse(url).query or "").lower()
    except ValueError:
        return False
    if platform == "youtube":
        return path.startswith("/playlist")
    if platform == "soundcloud":
        return "/sets/" in path
    if platform == "spotify":
        return "/album/" in path or "/playlist/" in path
    if platform == "deezer":
        return "/album/" in path or "/playlist/" in path
    if platform == "joox":
        return "/playlist/" in path or "/album/" in path
    if platform == "tidal":
        return "/album/" in path or "/playlist/" in path
    if platform == "applemusic":
        # album without ?i= track id
        if "/album/" in path and "i=" not in query and "/song/" not in path:
            return True
        return "/playlist/" in path
    if platform == "beatport":
        # release/chart/playlist have discrete track lists; label/genre are indexes.
        return any(x in path for x in ("/release/", "/chart/", "/playlist/"))
    return False
