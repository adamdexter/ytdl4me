# ytdl4me — Technical Specification

Self-hostable web app for downloading media from YouTube, Vimeo, SoundCloud, and Spotify
(personal / research / educational use). Single Docker container: Python FastAPI backend +
yt-dlp + ffmpeg, serving a no-build-step vanilla JS frontend.

## Guiding principles

- **Never re-encode video.** Quality tiers are achieved by selecting source streams via
  yt-dlp format selection; ffmpeg only remuxes/merges (stream copy). Re-encoding video
  always loses quality.
- **Smaller tiers use efficient codecs.** yt-dlp's default format sorting already prefers
  VP9/AV1 over H.264 at equal resolution, which gives the best compression at a given
  quality. Do not fight it; just cap resolution.
- **"Lossless" audio = original stream, bit-exact copy** into its native container
  (Opus or M4A/AAC). YouTube/SoundCloud sources are lossy, so converting to FLAC/WAV
  would waste space with zero gain — we don't offer it; the UI labels the option
  "Original (best quality)". MP3 tiers (320/256/192/128 kbps CBR) are transcoded with
  libmp3lame.
- Everything runs server-side; the browser only talks to our API.

## Repository layout & file ownership

```
server/
  __init__.py        (empty)
  main.py            FastAPI app: routes, static serving, auth middleware, job cleanup task
  jobs.py            Job dataclass + in-memory JobStore (thread-safe)
  downloader.py      yt-dlp option builders, probe(), run_download() with progress hooks
  platforms.py       URL platform detection + validation
  spotify.py         Spotify link resolution (oEmbed/og-tags scrape + optional spotipy)
requirements.txt
static/
  index.html
  app.css
  app.js
Dockerfile
docker-compose.yml
.dockerignore
.gitignore
.env.example
fly.toml
README.md
```

## Backend (Python 3.12, FastAPI, uvicorn)

Dependencies (requirements.txt): `fastapi`, `uvicorn[standard]`, `yt-dlp` (latest),
`httpx`, `mutagen` (for thumbnail embedding in mp3), `spotipy` (optional-use, still listed).

### Configuration (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | listen port |
| `ACCESS_KEY` | unset | if set, all `/api/*` routes require it (header `X-Access-Key` or `?key=` query param) |
| `DOWNLOAD_DIR` | `<repo>/downloads` | working dir for job files (each job gets a subdir `DOWNLOAD_DIR/<job_id>/`) |
| `FILE_TTL_MINUTES` | `60` | completed job files older than this are deleted by a background task (runs every 5 min); also delete job dir immediately after the file has been successfully served once + 5 minutes grace |
| `MAX_CONCURRENT_JOBS` | `3` | simultaneous downloads; extra jobs wait in `queued` |
| `ALLOW_ANY_SITE` | `false` | if false, only the four supported platforms are accepted (avoid abuse of yt-dlp's generic extractor) |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | unset | enables Spotify album/playlist enumeration via spotipy |
| `COOKIES_FILE` | unset | path to a Netscape cookies.txt passed to yt-dlp (helps with YouTube bot checks) |

### Platform detection (`platforms.py`)

`detect_platform(url) -> "youtube" | "vimeo" | "soundcloud" | "spotify" | "other" | None`
(None = not a valid http(s) URL). Match on hostname (after stripping `www.` / `m.`):

- youtube: `youtube.com`, `youtu.be`, `music.youtube.com`
- vimeo: `vimeo.com`, `player.vimeo.com`
- soundcloud: `soundcloud.com`, `on.soundcloud.com`, `snd.sc`
- spotify: `open.spotify.com`, `spotify.link`

`other` is rejected with a clear error unless `ALLOW_ANY_SITE=true`.
Platform "kind": youtube/vimeo → `video`; soundcloud/spotify → `audio`.

### API

All responses JSON unless noted. Errors: appropriate 4xx/5xx with body `{"error": "<human-readable message>"}`.

#### `GET /api/health`
`{"status": "ok", "yt_dlp_version": "..."}`  — no auth required.

#### `POST /api/probe`  body `{"url": "..."}`
Resolves metadata + available options. For Spotify, first resolve to a YouTube search
(see Spotify section) and probe the matched YouTube video, but report `platform: "spotify"`
and Spotify's own title/artist/thumbnail.

Response:
```json
{
  "platform": "youtube",
  "kind": "video",            // "video" | "audio"
  "url": "<canonical webpage url — for spotify: the ORIGINAL spotify url>",
  "title": "...",
  "uploader": "...",          // channel / artist
  "duration": 213.0,          // seconds, may be null
  "thumbnail": "https://...", // may be null
  "original_quality": "2160p60 (AV1)",  // human string; for audio kind e.g. "Opus ~160 kbps"
  "video_options": [           // empty list when kind == "audio"
    {"id": "original", "label": "Original", "detail": "2160p60 · AV1", "height": 2160, "approx_size": 734003200},
    {"id": "1080p",    "label": "1080p",    "detail": "VP9",           "height": 1080, "approx_size": 183500800},
    {"id": "720p",     "label": "720p",     "detail": "VP9",           "height": 720,  "approx_size": 94371840}
  ],
  "audio_options": [
    {"id": "audio_best", "label": "Original (best quality)", "detail": "Opus · no re-encode", "approx_size": 4194304},
    {"id": "mp3_320", "label": "MP3 320", "detail": "320 kbps CBR", "approx_size": null},
    {"id": "mp3_256", "label": "MP3 256", "detail": "256 kbps CBR", "approx_size": null},
    {"id": "mp3_192", "label": "MP3 192", "detail": "192 kbps CBR", "approx_size": null},
    {"id": "mp3_128", "label": "MP3 128", "detail": "128 kbps CBR", "approx_size": null}
  ]
}
```

Video-option rules: `original` always present (label shows real max height/fps/codec).
Add `1080p` only if original height > 1080; add `720p` only if original height > 720.
`approx_size`: sum of best matching video+audio stream `filesize` or `filesize_approx`
from yt-dlp's format list, null if unknown. For mp3 tiers estimate `duration * bitrate/8`
(bytes) when duration known, else null.

Probing runs `yt_dlp.YoutubeDL.extract_info(download=False)` in a thread
(`asyncio.to_thread` or run_in_executor) — never block the event loop. Use a 45 s timeout;
on failure return 422 with a friendly message (private video, geo-block, bad URL, etc.).
Playlist/album/set URLs (YouTube playlists without a video id, SoundCloud sets, Spotify
albums/playlists without creds): return 422 `{"error": "Playlists aren't supported yet — paste a link to a single video/track."}`.
For watch URLs that merely carry a `&list=` param, probe the single video (`noplaylist: True`).

#### `POST /api/download`  body `{"url": "...", "option_id": "original" | "1080p" | "720p" | "audio_best" | "mp3_320" | ...}`
Creates a job, returns `202 {"job_id": "<uuid4-hex>"}`. Validates url+option first
(re-detect platform; audio-only platforms reject video option ids).

#### `GET /api/jobs/{job_id}`
```json
{
  "job_id": "...", "status": "queued" | "downloading" | "processing" | "done" | "error",
  "progress": 87.4,          // 0-100, only meaningful while downloading
  "downloaded_bytes": 123, "total_bytes": 456,   // may be null
  "speed": 1234567.0,        // bytes/s, may be null
  "eta": 12,                 // seconds, may be null
  "filename": "Title [id].mkv",   // basename only, set when done
  "filesize": 123456,        // set when done
  "error": null              // message when status == "error"
}
```
`processing` = ffmpeg merge/extract/transcode phase (use yt-dlp postprocessor hooks).
Unknown job id → 404.

#### `GET /api/jobs/{job_id}/file`
When `done`, stream the file with `FileResponse`, `Content-Disposition: attachment` with a
properly RFC 5987-encoded filename. Guard against path traversal: the served path must be
`realpath`-contained inside that job's own directory; serve exactly the file recorded on
the job object, never anything derived from user input. 409 if not done.

### Jobs (`jobs.py`)

Dataclass `Job` (id, url, option_id, platform, title, status, progress fields, dir,
filepath, error, created_at, served_at). `JobStore`: dict + `threading.Lock` (progress
hooks fire from worker threads). A `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` gates
execution; each job runs as `asyncio.create_task` → inside, the blocking yt-dlp call runs
via `asyncio.to_thread`. Progress hook writes to the Job under the lock; the hook must be
cheap and never raise (wrap in try/except). Background cleanup task started on app
startup (`lifespan`): every 5 min delete expired job dirs and drop old error/done jobs
from the store (keep last 200).

### Downloader (`downloader.py`)

Use the **yt-dlp Python API** (never shell out — no injection surface). Common opts:
```python
{
  "outtmpl": {"default": f"{job_dir}/%(title).180B [%(id)s].%(ext)s"},
  "noplaylist": True,
  "concurrent_fragment_downloads": 4,
  "retries": 3, "fragment_retries": 5,
  "quiet": True, "no_warnings": True, "noprogress": True,
  "progress_hooks": [hook], "postprocessor_hooks": [pp_hook],
  # + "cookiefile": COOKIES_FILE if set
}
```
Per option:
- `original`: `format: "bv*+ba/b"` — default sorting picks highest res/fps/best codec.
  Let yt-dlp pick the merge container (mp4 when h264+aac, webm when vp9+opus, else mkv).
- `1080p`: `format: "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"`
- `720p`: `format: "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"`
- `audio_best`: `format: "bestaudio/b"`, postprocessor `FFmpegExtractAudio` with
  `preferredcodec: "best"` (bit-exact copy into native container), plus `FFmpegMetadata`
  and `EmbedThumbnail` (with `writethumbnail: True`).
- `mp3_320|256|192|128`: same but `preferredcodec: "mp3", preferredquality: "320"` etc.
  (yt-dlp passes CBR bitrate to lame), plus metadata + thumbnail embed.

After download, locate the final produced file (use the path from the last
postprocessor/progress hook `info_dict["filepath"]` when available; fall back to newest
file in job dir), store on job, set status done.

For Spotify jobs the download target is the matched YouTube URL, but tag the output
(`FFmpegMetadata` + `postprocessor_args` or outtmpl) so the filename is
`"{artist} - {title} [spotify]"` and only audio options are allowed.

### Spotify (`spotify.py`)

Spotify streams are DRM-protected; the standard approach (as used by spotDL) is: read the
track's public metadata, then download the best matching audio from YouTube.

- `resolve_track(url) -> {"artist", "title", "thumbnail", "duration", "search_query"}`:
  fetch `https://open.spotify.com/oembed?url=<url>` via httpx (title + thumbnail), and
  additionally fetch the page HTML with a desktop browser User-Agent, parsing
  `og:title` / `og:description` / `application/ld+json` for the artist name and duration.
  Be defensive: any parse failure falls back to whatever fields we did get.
- Search query: `"{artist} - {title}"`; probe/download via yt-dlp URL
  `ytsearch1:{query}` (quote nothing — Python API takes it as-is).
- Track links only (`/track/<id>` or spotify.link shortlinks that redirect to a track —
  follow redirects). Album/playlist links: if `SPOTIFY_CLIENT_ID/SECRET` set, spotipy
  could enumerate — **out of scope for v1**; return the friendly playlist error above.

### Auth middleware

If `ACCESS_KEY` is set: every `/api/*` route except `/api/health` requires
`X-Access-Key: <key>` header OR `?key=<key>` query param (query form needed for the
`<a download>` file link). Constant-time compare (`secrets.compare_digest`). 401 JSON
error otherwise. Static files and `/` are always served (the UI itself prompts for the key).

## Frontend (`static/`, vanilla JS, no build step, no external CDNs)

Single page, modern dark UI (system font stack, CSS variables, subtle radius/shadows,
accent color; responsive down to phones; light theme optional via
`prefers-color-scheme` — dark is the primary look).

Flow:
1. Hero: app name, one-line tagline, big input bar + "Fetch" button. On input/paste,
   detect platform client-side (same hostname rules) and show a platform badge
   (YouTube/Vimeo/SoundCloud/Spotify — inline SVG icons, colored). Enter key submits.
2. On fetch: loading state → `POST /api/probe` → result card: thumbnail, title, uploader,
   duration (h:mm:ss), original quality string. Below, two option groups: "Video"
   (hidden when kind=audio) and "Audio" — buttons showing label + detail + approx size
   (humanized, e.g. "~183 MB"; omit when null).
3. Clicking an option: `POST /api/download` → job appears in a "Downloads" list (newest
   first, persists across multiple fetches in-session): title, chosen option, progress
   bar with % and speed/ETA while downloading, "processing…" indeterminate state, then
   a "Save file" button linking `/api/jobs/{id}/file` (with `?key=` if access key in use)
   using the `download` attribute; also auto-click the link once when the job first
   reaches `done` (guard: only once per job). Errors show the message inline in red.
4. Poll each active job every 800 ms (`GET /api/jobs/{id}`); stop polling on done/error.
5. Access key: if any API call returns 401, show a small modal asking for the access key,
   store it in `localStorage`, retry. Send it as `X-Access-Key` on all fetches.
6. Small footer: "Personal / research use only. Respect creators and platform terms."

No frameworks, no external requests (fonts/icons inline). Keep the JS tidy: small module
with `api()` helper, `renderProbe()`, `Job` poller class. Escape all user-derived strings
via `textContent` (no innerHTML with untrusted data).

## Infra

- **Dockerfile**: `python:3.12-slim`; `apt-get install -y --no-install-recommends ffmpeg`;
  copy requirements, pip install (no cache); copy app; create non-root user `app`,
  writable `/data` download dir (`ENV DOWNLOAD_DIR=/data`); `EXPOSE 8000`;
  `HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"`;
  `CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]`.
- **docker-compose.yml**: build ., ports 8000:8000, env_file .env (optional), volume for
  /data (tmpfs or named volume), restart unless-stopped.
- **fly.toml**: app name placeholder `ytdl4me`, internal_port 8000, auto_stop, 1GB VM hint.
- **.env.example**: all config vars with comments.
- **.gitignore**: .venv/, __pycache__/, downloads/, .env, *.pyc, .DS_Store.
- **README.md**: what it is, feature list, screenshot placeholder, quickstart
  (docker compose up / plain docker run / local dev with venv), configuration table,
  deployment: Railway (recommended: connect repo, it auto-detects Dockerfile, add
  ACCESS_KEY env) and Fly.io (`fly launch`/`fly deploy`), VPS/home server via compose;
  an honest section on why Vercel (serverless time/size limits, no ffmpeg/yt-dlp
  runtime, datacenter-IP bot blocks) and SiteGround shared hosting (no long-running
  processes/daemons) can't run this workload; note on YouTube bot checks + COOKIES_FILE;
  legal note: personal/research/educational use, respect platform ToS and copyright.

## Non-goals (v1)

Playlists/albums/sets, user accounts, download history persistence across restarts,
transcoding video, FLAC/WAV output, browser extensions.
