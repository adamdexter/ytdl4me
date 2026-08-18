# As-built log — ytdl4me

Living record of what the system **actually does**, in roughly chronological feature order.
Use this when hopping agents so context is not trapped in a single chat session.

**Last major update:** Instagram/TikTok support + batch paste (mixed-platform URL lists → picker/ZIP).  
**Live deploy:** https://ytdl4me-production.up.railway.app · Railway project `abundant-laughter`, service `ytdl4me`, volume `/state`.

---

## Stack (unchanged core)

| Layer | Choice |
|---|---|
| API | FastAPI + uvicorn |
| Extract | yt-dlp **Python API** (never CLI) + custom clients |
| JS challenge | Deno + `yt-dlp-ejs` in Docker image |
| Frontend | `static/*` vanilla JS/CSS/HTML — **no build step** |
| Jobs | In-memory `JobStore` + thread pools; files under `DOWNLOAD_DIR/<job_id>/` |
| Deploy | Single Docker image on Railway; root user for volume writes |

---

## Feature timeline (agent-relevant)

### Unlisted access + crawler hygiene

- `ACCESS_KEY` middleware on `/api/*` except `/api/health`.
- Share via **fragment** `#key=` (preferred) or `?key=`; frontend stores in `localStorage` and strips URL.
- `robots.txt`, meta noindex, `X-Robots-Tag`.

### Self-renewing YouTube cookies

- Seed: `COOKIES_B64` / `COOKIES_CONTENT` / `COOKIES_FILE`.
- Persist + renew: `COOKIES_STATE_FILE` on a volume (`/state/cookies.txt` in prod).
- yt-dlp may rotate cookies; we write back atomically under a lock.

### SoundCloud custom client + Widevine CENC

- **File:** `server/soundcloud.py` (not yt-dlp for DRM).
- Order: progressive HTTP → plain HLS (concurrent segments) → CTR-encrypted HLS.
- Widevine L3 device: env `.wvd` or auto-cache public L3 device.
- **Gotcha:** CENC IV length handling (exact-fit 8 vs 16) — fixed once; do not “simplify” IV code casually.

### Multi-storefront audio (Deezer, JOOX, TIDAL, Apple Music)

- Detection in `platforms.py` + badge icons in `static/`.
- **Default free path:** `yt_match.resolve_track` public metadata → `match_sources` SoundCloud match → YouTube match.
- **Optional native:**
  - Deezer: `DEEZER_ARL` → Blowfish CDN (`server/deezer.py`); Premium can yield FLAC.
  - TIDAL: `TIDAL_ACCESS_TOKEN` (+ refresh optional).
  - Apple: `APPLE_MEDIA_USER_TOKEN`.
  - JOOX: `JOOX_COOKIE` / public song APIs.

### Match cascade before pure YouTube

- **File:** `server/match_sources.py`.
- Used for Spotify always, and for Deezer/TIDAL/Apple/Beatport when native creds absent
  (`prefers_youtube_match` in `yt_match.py`).
- Scoring: title tokens, artist, duration window, rework/mix penalties (Beatport “Original Mix”
  stripping — see `3e352fe`).
- Threshold ~50; weak SC hits fall through to `ytsearch1:`.

### Beatport

- **File:** `server/beatport.py`.
- Public track meta via cloudscraper `__NEXT_DATA__`, **jina.ai fallback** when Cloudflare 403
  (common on Railway datacenter IPs).
- Free storefront audio is **LOFI sample only** (~96 kbps, often short) — not masters.
- Full quality free path = SC/YT cascade (same as other storefronts).
- Optional native: Streaming plan OAuth (`BEATPORT_USERNAME`/`PASSWORD` or access/refresh tokens)
  ported from beatportdl patterns (progressive AAC/FLAC or AES-128 HLS).
- Research conclusion: no unpaid full-master API; plan gates `download/` and `stream/`.

### Deezer Premium ARL on production

- App already supported `DEEZER_ARL`; production has ARL set via Railway (not in git).
- Probe label when ARL set: FLAC / MP3 up to 320.
- Login email/password → ARL via `connect.deezer.com/oauth/user_auth.php` hash  
  `md5(client_id + email + md5(password) + client_secret)` then `user.getArl` — **do this offline**;
  store only ARL in env, never commit credentials.

### Playlists / albums / sets + ZIP

- **Files:** `server/playlists.py`, batch path in `server/main.py`, UI in `static/app.js` + CSS.
- Same input field; `looks_like_playlist` **detects** (no longer hard-rejects).
- Probe `kind: "playlist"` + `entries[]`; download with `entries` + `zip`.
- Details: [`PLAYLISTS.md`](PLAYLISTS.md).

### Editing-friendly MP4 video tiers

- **File:** `server/downloader.py` (`_FORMAT_SPECS`, `_mp4_copy_spec`, `_convert_spec`,
  `_video_options`); playlist chips in `server/playlists.py`. No frontend change — the UI
  renders whatever the probe returns.
- **Why:** default `bv*+ba` gave AV1/VP9 `.webm`, which macOS/QuickTime/NLEs can't open.
- 1080p/720p tiers prefer `avc1`+`m4a` and stream-copy into `.mp4`
  (`merge_output_format: "mp4/mkv"` — mkv only when the codec fallback fires). The 1080p
  tier now also appears when the *source itself* is 1080p in AV1/VP9 (before, such videos
  offered only the unusable "Original").
- `2160p_mp4` / `1440p_mp4` ("4K MP4"/"2K MP4"): the only transcoding tiers — YouTube has
  no H.264 above 1080p. VP9 source preferred (faster decode than AV1) → libx264 CRF 18
  veryfast, `yuv420p`, AAC 192k, via `FFmpegVideoConvertor` + `postprocessor_args`
  (`videoconvertor` key). Slower; labeled as converted in the UI. Hidden when the source at
  that height is already H.264 (e.g. Vimeo) and not offered for playlist batches.
- "Original" unchanged: bit-exact best streams, whatever container fits; its detail now
  names the codec + container so picking `.webm` is informed.

### Instagram + TikTok + batch paste

- **Files:** `platforms.py` (hosts), `downloader.py` (`_social_stem`, `h264` option,
  short-side `_res_label`, IG cookie hint in `friendly_error`), `playlists.py`
  (`pasted_list_payload`), `main.py` (`ProbeRequest.urls`, per-entry platform validation),
  `app.js` (icons, hosts, `extractUrls`, paste handler, batch badge), `index.html` (icons).
- IG/TT probe offers **Original + MP4 (H.264)** only — height-capped tiers punish portrait
  video (a 1080×1920 reel is colloquially "1080p" → labels use the SHORT side everywhere).
- **Gotcha:** TikTok reports `vcodec: "h264"/"h265"`, not fourcc — format filters must use
  `[vcodec~='^(avc|h264)']`, and IG's username lives in `channel` (`uploader_id` is a
  numeric pk).
- Social filenames: `handle - YYYY-MM-DD - caption words - reel|post|story|video - id.ext`,
  built in `run_download` from the two-phase pre-extraction (mutating
  `ydl.params["outtmpl"]` before `process_ie_result` — prepare_filename reads it at call
  time). Works for batch entries too (no per-item stem passed).
- **Batch paste:** 2+ URLs in the input (frontend reads raw clipboard on paste; also
  splits on submit) → `POST /api/probe {urls}` → synthetic `kind:"playlist"` with **zero
  upfront network calls** (IG rate limits!). Mixed platforms allowed; download reuses the
  existing batch/ZIP pipeline.
- **Carousels:** an IG multi-item post probes as ONE video card (`Carousel · N videos`,
  Original/H.264 options) and downloads as ONE job — every video in the post, ZIP when >1,
  members named `<social stem> - NN.ext`. Mechanics: IG probes extract with
  `process=False` (processing aborts on image entries: "No video formats found"), raw
  formats get re-sorted worst-to-best for labels (`_sort_raw_formats`), image entries are
  filtered via empty `formats` before `process_ie_result`, and the carousel-level info
  carries all post metadata so `_social_stem` works unchanged. Image-only posts get a
  clear 422.

### Build version surfacing

- `/api/health` reports `version`: `RAILWAY_GIT_COMMIT_SHA` (short) in prod, `.git/HEAD`
  read directly for local runs, `"dev"` fallback (`_build_version` in `main.py`).
- Frontend fetches ungated `/api/health` on load and renders
  `build <sha> · yt-dlp <ver>` under the footer disclaimer (`#app-version`).
- This is the canonical "which build is live" check for agents after a deploy.

### Repo hygiene

- Public repo: no secrets; `.gitignore` covers `.env`, cookies, media artifacts, `.wvd`.
- Removed accidental tracked test audio; history may still contain old blobs (not secrets).

---

## Request architecture (current)

```
POST /api/probe {url}
  → detect_platform
  → if looks_like_playlist → enumerate_playlist (playlists.py)
  → else if spotify | prefers_youtube_match → meta → SC match probe | YT probe
  → else downloader.probe / native module

POST /api/download {url, option_id, entries?, zip?, title?}
  → single: Job + _run_job (cascade or native)
  → multi + zip: Job batch + _run_batch_job → tracks/NNN → .zip
  → multi + !zip + N>1: N jobs (job_ids[])

GET /api/jobs/{id}  → status, progress, optional batch:{total,done,failed,zip}
GET /api/jobs/{id}/file → single file or ZIP (path-traversal safe)
```

Executors: `_probe_executor`, `_download_executor`; semaphore `MAX_CONCURRENT_JOBS`.
Batch internal concurrency ≈ 2 so one playlist does not starve the server.

---

## Module ownership

| Concern | Module |
|---|---|
| HTTP routes, auth, batch orchestration | `server/main.py` |
| yt-dlp video + cookies | `server/downloader.py` |
| Platform host map / playlist shape | `server/platforms.py` |
| Playlist enumeration | `server/playlists.py` |
| SC decrypt / DRM | `server/soundcloud.py` |
| SC search match scoring | `server/match_sources.py` |
| Storefront public meta | `server/yt_match.py`, `spotify.py`, `beatport.py` |
| Native paid streams | `deezer.py`, `tidal.py`, `applemusic.py`, `beatport.py`, `joox.py` |
| Job model | `server/jobs.py` (`batch_*` fields for playlists) |
| UI | `static/app.js`, `app.css`, `index.html` |

---

## Production env (names only)

Documented as present or commonly used on Railway — **values never in git**:

| Variable | Role |
|---|---|
| `ACCESS_KEY` | Unlisted API gate |
| `COOKIES_B64` | YouTube cookie seed |
| `COOKIES_STATE_FILE` | `/state/cookies.txt` renewing path |
| `DOWNLOAD_DIR` | `/data` |
| `DEEZER_ARL` | Native Deezer full streams (FLAC when Premium) |
| Optional | `BEATPORT_*`, `TIDAL_*`, `APPLE_MEDIA_USER_TOKEN`, `SPOTIFY_CLIENT_*`, Widevine device |

When adding a var: code + `README.md` + `.env.example` + `CLAUDE.md` list + this section if production-relevant.

---

## Design decisions worth preserving

1. **Enumerate ≠ download.** Playlist listing is metadata-only; each track reuses single-item pipeline.
2. **No unpaid “full quality” miracle.** Sites like lucida.to / free-mp3-download use **shared paid accounts** or service ARLs server-side. ytdl4me uses match cascade + optional **operator-owned** tokens instead of scraping third-party rippers.
3. **Beatport free masters do not exist** via public API; samples are LOFI.
4. **YouTube `watch?v=&list=`** stays single-video (`noplaylist`) — only `/playlist` paths expand lists.
5. **Verification = runtime** against real URLs, not mock unit tests (see verify skill).

---

## Related commits (recent, for archaeology)

```
a088fe7 Add playlist/album support with track select and ZIP download
4d03003 Show FLAC in Deezer probe when DEEZER_ARL is set
3e352fe Tighten SoundCloud match scoring for Beatport-style titles
c260008 Port beatportdl OAuth + AES-HLS for native Beatport streams
ccaa9df Beatport: fall back to jina.ai metadata when Cloudflare blocks
17e9e68 Add Beatport with free SC/YouTube cascade for full tracks
3cbaae0 Cascade SoundCloud Widevine decrypt before YouTube match
24b01d1 Default Deezer/TIDAL/Apple Music to YouTube match without paid tokens
5671510 Add Deezer, JOOX, TIDAL, and Apple Music with link detection
2c1fad9 Add SoundCloud DRM download via Widevine CENC decrypt
```

Use `git log -p -- server/<file>` for exact diffs when continuing work.
