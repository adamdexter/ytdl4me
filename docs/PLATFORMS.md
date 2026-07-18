# Platform matrix

How each platform is **detected**, **probed**, and **downloaded**. Keep this file updated
when adding a storefront or changing cascade rules.

Detection: `server/platforms.py` + mirror hosts in `static/app.js`.  
Dispatch: `server/main.py` `_probe` / `_run_job` + `server/downloader.py`.

## Legend

| Path | Meaning |
|---|---|
| **yt-dlp** | `downloader.probe` / `run_download` |
| **Native** | Custom module stream (may need token) |
| **Match** | Public metadata → SoundCloud match → else YouTube `ytsearch1:` |
| **Enum** | Playlist/album listing via `playlists.py` |

## Matrix

| Platform | Single probe | Single download | Playlist enum | Optional env for native / better enum |
|---|---|---|---|---|
| **YouTube** | yt-dlp | yt-dlp (video tiers + audio) | yt-dlp flat `/playlist` | cookies (`COOKIES_*`) |
| **Vimeo** | yt-dlp | yt-dlp | (shape not special-cased) | — |
| **SoundCloud** | `soundcloud.py` | progressive → HLS → Widevine CENC | `/sets/` via API resolve | `WIDEVINE_DEVICE_*` |
| **Spotify** | public meta only | **Match** always | playlist/album (API preferred) | `SPOTIFY_CLIENT_ID`/`SECRET` for lists |
| **Deezer** | public API track **or** Match if no ARL | Native Blowfish if `DEEZER_ARL`; else Match | public `api.deezer.com` album/playlist | `DEEZER_ARL` |
| **TIDAL** | Match if no token; else catalog | Native if `TIDAL_ACCESS_TOKEN`; else Match | catalog items API | `TIDAL_ACCESS_TOKEN`, refresh, country |
| **Apple Music** | Match if no media-user-token | Native Widevine AAC if token; else Match | catalog JWT album/playlist | `APPLE_MEDIA_USER_TOKEN` |
| **Beatport** | public scrape (+ jina CF fallback) | free sample only / Streaming OAuth / else Match | release/chart scrape | `BEATPORT_USERNAME`+`PASSWORD` or tokens |
| **JOOX** | public songinfo | direct streams | not supported (clear error) | `JOOX_COOKIE` |

## Match cascade (detail)

```
prefers_youtube_match(platform) == True
  OR platform == "spotify"
       │
       ▼
  resolve public meta (artist, title, duration, search_query)
       │
       ▼
  find_soundcloud_match(artist, title, duration)   # match_sources.py
       │ score ≥ threshold (~50)
       ├─ yes → soundcloud download (decrypt if needed), retag with storefront meta
       └─ no  → yt-dlp ytsearch1:{search_query}, retag
```

**When Match is skipped** (native first):

- Deezer: `DEEZER_ARL` set → `deezer.py`
- TIDAL: `TIDAL_ACCESS_TOKEN` set → `tidal.py`
- Apple: `APPLE_MEDIA_USER_TOKEN` set → `applemusic.py`
- Beatport: login/tokens present → `beatport.py` native download/stream

## SoundCloud match scoring notes

- Strip common remix suffixes carefully; Beatport “Original Mix” should not poison search.
- Bare word “mix” is not treated as a rework flag.
- Duration mismatch is heavily penalized (avoid wrong promo edits).
- If SC match is wrong in production, tune `match_sources.py` and re-verify with real titles.

## Beatport specifics

| URL shape | Behavior |
|---|---|
| `/track/slug/id` | Single track probe/download |
| `/release/`, `/chart/`, `/playlist/` | Multi-item enum |
| `/label/`, `/genre/` | Not discrete lists — error on playlist enum |

Cloudflare on `www.beatport.com` from datacenter IPs → try jina reader fallback for HTML/JSON.

API client id (public OAuth app id used by web/apps) lives in code; **user/password never committed**.

## Deezer native

- Cookie `arl` via env `DEEZER_ARL`.
- Formats tried: FLAC → MP3_320 → MP3_128 (account flags `web_hq` / `web_lossless`).
- Decrypt: Blowfish CBC stripe (secret constant shared by all open Deezer tools — not a user secret).

## Adding a platform (checklist)

1. Hosts in `platforms.py` + `app.js` `PLATFORM_HOSTS` / `PLATFORMS`.
2. Icon symbol in `index.html`.
3. `platform_kind` audio vs video.
4. Single-item: module **or** Match via `yt_match` + `prefers_youtube_match`.
5. Optional: `playlists.py` enumerator + `looks_like_playlist` shape.
6. Document env in README + `.env.example` + `CLAUDE.md`.
7. Add row to this matrix + known-good URL in verify skill.
8. Runtime probe + one download; `ffprobe` output.

## Known-good single URLs

| Platform | URL |
|---|---|
| YouTube 4K tiers | `https://www.youtube.com/watch?v=aqz-KE-bpKQ` |
| YouTube tiny | `https://www.youtube.com/watch?v=jNQXAC9IVRw` |
| SoundCloud | `https://soundcloud.com/forss/flickermood` |
| Spotify | `https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT` |
| Deezer | `https://www.deezer.com/track/3135556` |
| Vimeo | `https://vimeo.com/76979871` |

Playlist seeds: see [`PLAYLISTS.md`](PLAYLISTS.md).
