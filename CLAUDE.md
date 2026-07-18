# CLAUDE.md — agent runbook for ytdl4me

Operational guide for an AI agent (Claude Code) maintaining, debugging, or extending this
app. Read this first. Human-facing docs live in `README.md`; the original design intent is
`SPEC.md` (see its "As-built" note — reality has drifted from it, this file is ground
truth). Verify recipes: `.claude/skills/verify/SKILL.md`.

**What it is:** a self-hosted web app that downloads media from YouTube, Vimeo, SoundCloud,
and Spotify. Python FastAPI + yt-dlp (Python API, never the CLI) + ffmpeg + Deno, one
Docker container, vanilla-JS frontend (no build step). Deployed on Railway.

## ⚠️ Secrets & repo hygiene (this repo is PUBLIC)

- **Never commit** the real `ACCESS_KEY`, any `cookies.txt`, its base64, the share link with
  the key in it, or a `.env`. They live only in Railway Variables. `.gitignore` covers
  `.env`, `.venv`, `downloads/`. Cookies are session credentials — treat like passwords.
- Commit identity must stay the GitHub noreply address (repo-local git config already set to
  `8442611+adamdexter@users.noreply.github.com`). Don't expose a real email in history.
- To read a secret's current value: `railway variables --service ytdl4me --environment production --kv | grep '^ACCESS_KEY='` (needs the linked project; see Railway ops).

## Live deployment facts

- **URL:** https://ytdl4me-production.up.railway.app
- **Railway:** project `abundant-laughter`, service `ytdl4me`, environment `production`,
  volume `ytdl4me-volume` mounted at `/state`. CLI authed as the owner.
- **Vars set in Railway (values not in repo):** `ACCESS_KEY`, `COOKIES_B64` (seed),
  `COOKIES_STATE_FILE=/state/cookies.txt`, `DOWNLOAD_DIR=/data`.
- **Access = unlisted:** share `https://ytdl4me-production.up.railway.app/#key=<ACCESS_KEY>`.
  Anyone with that link is unlocked; the bare URL is gated. Retrieve `<ACCESS_KEY>` from
  Railway vars.
- **Deploys** from GitHub `main` (repo `adamdexter/ytdl4me`). Container runs as **root**
  (needed for the writable `/state` volume).

## Architecture & request flow

```
Browser (static/*, vanilla JS)
  → POST /api/probe   → downloader.probe()  → yt_dlp.extract_info(download=False)  → options JSON
  → POST /api/download→ creates Job (uuid), 202 → async task → downloader.run_download() in a thread
  → GET  /api/jobs/{id}      poll status/progress (frontend polls every 800ms)
  → GET  /api/jobs/{id}/file FileResponse when done
```

- **Never blocks the event loop:** yt-dlp is blocking, so probe/download run in dedicated
  `ThreadPoolExecutor`s (`_probe_executor`, `_download_executor` in `main.py`) via
  `asyncio.to_thread`/`run_in_executor`. Progress hooks fire from worker threads and write
  to the thread-safe `JobStore` under a lock.
- **Never re-encodes video:** quality tiers pick source streams; ffmpeg only merges with
  stream copy. Audio "Original" is a bit-exact copy; MP3 tiers transcode with libmp3lame.
- **SoundCloud is a custom client** (`server/soundcloud.py`), not yt-dlp: progressive HTTP
  first, then concurrent HLS, then Widevine `ctr-encrypted-hls` (license + pure-Python CENC
  decrypt + ffmpeg remux). yt-dlp alone reports DRM tracks as undownloadable. Device via
  `WIDEVINE_DEVICE_FILE` / `WIDEVINE_DEVICE_B64` or auto-cached public L3 `.wvd`.
- **Never shells out with user input:** yt-dlp is used as a Python library (no subprocess,
  no injection surface). ffmpeg is invoked by yt-dlp's postprocessors, not by us.

## File map

| File | Responsibility | Key symbols |
|---|---|---|
| `server/main.py` | FastAPI app, routes, auth middleware, rate limit, job orchestration, TTL cleanup, static mount, `X-Robots-Tag` header | `_access_key_middleware`, `_rate_limited`, `_run_job`, `_cleanup_loop`, `api_health/probe/download/job/file` |
| `server/downloader.py` | yt-dlp option builders, `probe()`, `run_download()`, cookie resolution + **self-renewal**, `friendly_error()`; dispatches SoundCloud to `soundcloud.py` | `_resolve_cookies`, `_cookies_copy`, `build_ydl_opts`, `_video_options`, `_FORMAT_SPECS` |
| `server/soundcloud.py` | SoundCloud API client: progressive / concurrent HLS / Widevine CENC DRM decrypt | `probe`, `run_download`, `_pick_stream`, `_decrypt_fragment`, `_widevine_content_key` |
| `server/jobs.py` | `Job` dataclass + thread-safe `JobStore` | `JobStore.update/get/prune` |
| `server/platforms.py` | URL → platform detection + playlist-shape rejection | `detect_platform`, `platform_kind`, `looks_like_playlist` |
| `server/spotify.py` | Spotify link → public metadata → `ytsearch1:` query (spotDL approach; no DRM) | `resolve_track`, `SpotifyError` |
| `static/index.html` | Single page + inline SVG icons + `<dialog>` key modal + `noindex` meta | — |
| `static/app.js` | IIFE: `api()` helper, platform badge, `renderProbe`, `JobPoller`, unlisted-link token consume | `consumeKeyFromUrl`, `keyStore`, `JobPoller` |
| `static/app.css` | Dark-first responsive styles | — |
| `static/robots.txt` | Blocks all crawlers incl. AI bots | — |
| `Dockerfile` | python:3.12-slim + ffmpeg + Deno (multi-stage) + pip; runs as root | — |

## The three YouTube walls (hardest-won knowledge)

YouTube fights datacenter IPs in layers. Diagnosis: reproduce locally with
`.venv/bin/python -m yt_dlp --cookies <file> -v -F <url>` and read the debug lines.

1. **"Sign in to confirm you're not a bot"** — IP-reputation gate. Datacenter IPs (Railway)
   get it; residential IPs rarely do. **Fix = cookies** from a logged-in (throwaway) YouTube
   account. Only affects YouTube + Spotify (Spotify resolves via YouTube search); SoundCloud
   and Vimeo are unaffected.
2. **"No video formats found" / "n challenge solving failed"** — yt-dlp must run YouTube's JS
   signature challenge; with no JS runtime it drops every real format. Debug shows
   `JS runtimes: none` / `JS Challenge Providers: … (unavailable)`. **Fix = a JS runtime +
   solver:** the image ships **Deno** (`COPY --from=denoland/deno:bin-2.9.3`) and
   **`yt-dlp-ejs`** (in `requirements.txt`). Both are required; Deno alone still fails.
3. **(Possible future) SABR / PO tokens** — if formats come back empty *with* cookies+Deno
   working, YouTube may be gating streams behind Proof-of-Origin tokens. Fix would be a
   `bgutil-ytdlp-pot-provider` sidecar. Not needed as of last check.

When YouTube changes its player, wall #2 can reappear after working — the fix is almost
always **`pip install -U yt-dlp yt-dlp-ejs`** (bump `requirements.txt`) and redeploy.

## Cookies model (self-renewing)

`_resolve_cookies()` in `downloader.py`:
- **Seed** priority: `COOKIES_B64` (base64 of cookies.txt) > `COOKIES_CONTENT` (raw) >
  `COOKIES_FILE` (a path/bind-mount).
- If **`COOKIES_STATE_FILE`** is set (a path on a *persistent volume*): seed it **once** (only
  when the state file is missing/empty), then use it directly. After each yt-dlp run,
  `_cookies_copy()` writes the rotated cookies back atomically (`os.replace`, same-filesystem,
  under `_cookie_lock`) so the session self-renews across runs and restarts. Health reports
  `cookies_renewing: true`.
- Without `COOKIES_STATE_FILE`: a read-only `COOKIES_FILE` is used as-is, or B64/CONTENT is
  written to an ephemeral temp for the process lifetime (no renewal).

**Refreshing cookies (when the bot error returns):** re-export `cookies.txt`, set new
`COOKIES_B64`. ⚠️ **The seed only applies when the state file is missing** — so also clear the
stale volume copy, or it keeps using the old cookies. Since `railway volume files` needs an
unencrypted SSH key (often unavailable headless), the reliable reset is: temporarily point
`COOKIES_STATE_FILE` at a new path (e.g. `/state/cookies2.txt`), redeploy (reseeds fresh),
done. Then update the guidance here if the path changed.

## Access model (unlisted link)

- `ACCESS_KEY` set → `_access_key_middleware` requires it on every `/api/*` except
  `/api/health`, via `X-Access-Key` header or `?key=` query (constant-time compare).
- Frontend `consumeKeyFromUrl()` reads a token from the URL **fragment** (`#key=`/`#k=`) or
  query (`?key=`/`?k=`) on load, stores it in `localStorage`, and strips it from the address
  bar. Fragment is preferred because it never reaches the server/logs. Result: friends click
  the share link and are silently authed; the bare URL shows the key modal and can't proceed.
- Not indexed: `robots.txt` + `<meta name=robots noindex>` + `X-Robots-Tag` header. These
  only deter well-behaved bots — the link token is the real gate.

## Config / env (all optional)

Full table with meanings is in `README.md`. Quick list read by the code:
`PORT`, `ACCESS_KEY`, `DOWNLOAD_DIR`, `FILE_TTL_MINUTES`, `MAX_CONCURRENT_JOBS`,
`MAX_ACTIVE_JOBS`, `RATE_LIMIT_PER_MINUTE`, `ALLOW_ANY_SITE`, `COOKIES_FILE`, `COOKIES_B64`,
`COOKIES_CONTENT`, `COOKIES_STATE_FILE`, `WIDEVINE_DEVICE_FILE`, `WIDEVINE_DEVICE_B64`.
(`SPOTIFY_CLIENT_ID/SECRET` are reserved, unused.)
When you add a new one, update: `README.md` table, `.env.example`, and this list.

## Local dev, run & verify

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
curl -fsSL https://deno.land/install.sh | sh    # JS runtime for YouTube (wall #2)
export PATH="$HOME/.deno/bin:$PATH"
# cookies needed only for YouTube/Spotify; SoundCloud/Vimeo work without.
ACCESS_KEY=dev COOKIES_FILE=/path/to/cookies.txt \
  .venv/bin/uvicorn server.main:app --reload --port 8000
```

**Verification is runtime observation, not unit tests.** Drive the real surface:

- API smoke: `curl -s localhost:8000/api/health` then `POST /api/probe` with
  `{"url": "..."}` and `X-Access-Key: dev`. Known-good URLs:
  - YouTube 4K (tier logic): `https://www.youtube.com/watch?v=aqz-KE-bpKQ`
  - YouTube tiny (fast full download): `https://www.youtube.com/watch?v=jNQXAC9IVRw`
  - SoundCloud: `https://soundcloud.com/forss/flickermood`
  - Vimeo (720p src → no lower tiers): `https://vimeo.com/76979871`
  - Spotify: `https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT`
- Download flow: `POST /api/download` → poll `GET /api/jobs/{id}` → `GET /api/jobs/{id}/file`.
  A ~20-line urllib helper (submit/poll/fetch, sends `X-Access-Key`) is the fastest driver.
- Check outputs with `ffprobe -show_entries stream=codec_name,width,height,bit_rate`: tiers
  must be exactly 1080/720 high; `mp3_N` must be `bit_rate=N000`; `audio_best` native codec.
- UI / unlisted flow: Playwright (chromium). Assert `/#key=<KEY>` unlocks with no modal, bare
  URL shows `#key-modal[open]`, exactly one `download` event per job, zero pageerrors.

## Railway operations

Link once from the repo dir: `railway link -p abundant-laughter -e production -s ytdl4me`.

| Task | Command |
|---|---|
| Health / which build is live | `curl -s $URL/api/health` (field presence signals new code) |
| Set a var (auto-deploys) | `railway variables --service ytdl4me --environment production --set "K=V"` |
| Set a secret without arg-leak | `… --set-from-stdin K` (pipe the value) |
| Set var, defer deploy | add `--skip-deploys` |
| **Deploy latest pushed commit** | `railway redeploy --service ytdl4me --environment production --from-source -y` |
| Restart current build (picks up new vars/volume) | `railway redeploy -y` |
| Add a persistent volume | `railway volume add -m /state` (service must be linked) |
| List vars (names/values) | `railway variables … --kv` |
| Logs | `railway logs` |

**Deploy a code change:** commit → `git push origin main` → `railway redeploy --from-source -y`
→ poll `/api/health` until the change is observable. A full rebuild (Dockerfile/requirements
change) takes ~1–3 min; config redeploys ~30s.

**Rollback:** `git revert <sha> && git push && railway redeploy --from-source -y`, or redeploy
a prior deployment from the Railway dashboard.

**Limitation:** `railway volume files …` needs an unencrypted SSH key registered with Railway;
in headless/agent runs it fails on encrypted keys. Prefer the env-var/reseed approach above.

## Troubleshooting decision tree

- **Site 5xx / down right after a deploy** → build or boot failed. `railway logs`. Common
  cause: a Dockerfile change (Deno copy path, entrypoint) — the previous deploy keeps serving
  until the new one is healthy, so revert the bad commit and redeploy from source.
- **YouTube "not a bot" (422)** → wall #1: cookies missing/expired. `/api/health` →
  `cookies_configured`? Re-export cookies, reset `COOKIES_B64`, clear the stale state file
  (see Cookies model). SoundCloud/Vimeo still working confirms it's YouTube-specific.
- **YouTube "No video formats found"** → wall #2: JS runtime/solver. Confirm the image has
  Deno (`/usr/local/bin/deno`) and `yt-dlp-ejs` installed. If it regressed after working,
  bump `yt-dlp` + `yt-dlp-ejs` and redeploy.
- **Friends get a password prompt** → they opened the bare URL, not the `#key=` share link,
  or `ACCESS_KEY` changed. Re-send the full share link.
- **429 Too Many Requests** → `RATE_LIMIT_PER_MINUTE` (per-IP, default 30) or `MAX_ACTIVE_JOBS`
  cap hit. Raise the var if legitimate.
- **Playlist/album URL** → intentionally rejected (`platforms.looks_like_playlist` + a probe
  guard). Single items only.
- **Spotify title wrong / missing** → `spotify.py` scrapes public pages (oEmbed + embed
  `__NEXT_DATA__`); Spotify markup changes break it. Fix the fallbacks there.

## Extending

- **Add a quality tier / change format selection:** edit `_FORMAT_SPECS` and `_video_options`
  in `downloader.py`; keep the "never re-encode video" rule (merge/stream-copy only). Add the
  new `option_id` to `VIDEO_OPTION_IDS`/`AUDIO_OPTION_IDS` and render it in `renderProbe`
  (`app.js`). Verify the output resolution/bitrate with `ffprobe`.
- **Touch SoundCloud carefully:** logic lives in `server/soundcloud.py` (not yt-dlp format
  strings). Prefer progressive → plain HLS → CTR DRM. Verify a known DRM track
  (`https://soundcloud.com/1985music1985/line-the-money`) finishes in a few seconds as AAC
  ~160 kbps with clean `ffprobe` decode (zero AAC errors).
- **Add a platform:** extend `PLATFORM_HOSTS` in `platforms.py` (backend) *and* the mirror in
  `app.js` (badge), add an inline SVG icon in `index.html`, set its `kind` (video/audio), and
  confirm yt-dlp supports it. If audio-only, ensure `video_options` is empty.
- **Add an env var:** read it in the relevant module; document in `README.md` table,
  `.env.example`, and the config list above.
- **Bump yt-dlp / Deno:** edit `requirements.txt` (`yt-dlp`, `yt-dlp-ejs`) and the Deno pin in
  `Dockerfile` (`denoland/deno:bin-<ver>`); redeploy from source; verify a real YouTube
  download. This is the usual fix when YouTube breaks extraction.

## Invariants (don't violate)

- Never re-encode video; audio "Original" stays bit-exact.
- Never pass user input to a shell; keep using the yt-dlp Python API.
- File serving is path-traversal-guarded (`realpath` contained in the job dir) — keep it.
- Keep the README disclaimer and `robots.txt`; this is a research/educational, unlisted tool.
- Keep commits on the noreply identity; never commit secrets/cookies/.env.
