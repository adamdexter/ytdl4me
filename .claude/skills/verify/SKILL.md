---
name: verify
description: Build, launch, and drive ytdl4me end-to-end to verify changes at the real surfaces (API + browser UI).
---

# Verifying ytdl4me

See `AGENTS.md` + `CLAUDE.md` for architecture, Railway ops, and the "three YouTube walls".
Platform matrix: `docs/PLATFORMS.md`. Playlists: `docs/PLAYLISTS.md`.

## Launch

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
curl -fsSL https://deno.land/install.sh | sh                         # once — YouTube needs a JS runtime
export PATH="$HOME/.deno/bin:$PATH"
.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8741 &
curl -s http://127.0.0.1:8741/api/health   # {"status":"ok","cookies_configured":...,"cookies_renewing":...}
```

Startup takes ~3 s (yt-dlp import) — retry the health check once before assuming failure.
**YouTube/Spotify need Deno on PATH** (else "No video formats found") **and cookies** (else
"not a bot"): pass `COOKIES_FILE=/path/cookies.txt`. SoundCloud/Vimeo need neither.
For auth/unlisted flows, run with `ACCESS_KEY=<key> ...` (a second instance on `--port 8742`).
To exercise self-renewing cookies, also set `COOKIES_STATE_FILE=/tmp/state/cookies.txt`.

## Drive — API surface

- Probe: `POST /api/probe {"url": ...}`. Good known inputs:
  - 4K YouTube (tier logic: original+1080p+720p): `https://www.youtube.com/watch?v=aqz-KE-bpKQ`
  - Tiny YouTube video (fast full download): `https://www.youtube.com/watch?v=jNQXAC9IVRw`
  - Short 4K clip (11 s — fast tier downloads): `https://www.youtube.com/watch?v=2PuFyjAs7JA`
  - SoundCloud: `https://soundcloud.com/forss/flickermood`
  - Vimeo (720p source → no lower tiers): `https://vimeo.com/76979871`
  - Spotify: `https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT`
  - Deezer track (native if `DEEZER_ARL`): `https://www.deezer.com/track/3135556`
  - Deezer album (playlist): `https://www.deezer.com/album/302127` → expect `kind:"playlist"`
  - YouTube playlist: `https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE`
- Download: `POST /api/download {"url", "option_id"}` → poll `GET /api/jobs/{id}` (~1 s
  interval) until `done` → `GET /api/jobs/{id}/file`. ~20 lines of urllib is enough.
- **Playlist ZIP:** probe album → take 2 `entries[].url` →
  `POST /api/download` with `entries`, `zip:true`, `title` → poll until `batch.done==2` and
  `filename` ends with `.zip` → download file; `unzip -l` should list two audio files.
- Verify outputs with `ffprobe -show_entries stream=codec_name,width,height,bit_rate`:
  tier files must be exactly 1080/720 high; `mp3_N` must show `bit_rate=N000`;
  `audio_best` must be the native codec (opus/aac), not mp3.
- Error paths: non-platform URL, garbage URL (422), unknown job (404), burst rate limit (429).
  Playlist URLs should **not** 422 anymore — they return `kind:"playlist"`. Empty selection
  download should 422. Spotify playlist without API keys may 422 with a clear credentials hint.

## Drive — browser UI

Playwright (chromium) against the running server; key selectors:
`#url-input`, `#fetch-btn`, `#platform-badge`, `#probe-card button.option` (option tiles),
`.playlist-picker`, `.track-list input[type=checkbox]`, `.playlist-zip input`,
`#downloads-list li[data-state="done"]`, `#key-modal[open]`, `#key-input`, `#key-hint`.
Use `colorScheme: "dark"`, `acceptDownloads: true`; assert exactly ONE `download` event
per job (auto-click-once guard) and zero pageerrors. For playlists: assert track rows render
and ZIP toggle defaults on when ≥2 tracks.

**Unlisted access flow** (when `ACCESS_KEY` is set): visiting `/#key=<KEY>` must auto-store
the token (localStorage `ytdl4me.accessKey`), strip the fragment, and let a probe through
with no modal; the bare URL must show `#key-modal[open]` on fetch. Verify `/robots.txt`
disallows all, and responses carry `X-Robots-Tag: noindex`.

## Gotchas

- **YouTube "not a bot"** (datacenter IPs) → cookies missing/expired; set `COOKIES_FILE`
  locally. **YouTube "No video formats found"** → Deno not on PATH / `yt-dlp-ejs` missing.
- When YouTube breaks extraction after a player change, the fix is usually
  `pip install -U yt-dlp yt-dlp-ejs` + redeploy — not a code bug.
- MP3 size estimates exclude embedded cover art, so actual files run larger — not a bug.
- The Spotify path downloads the matched YouTube audio; titles come from Spotify's
  oEmbed/embed page. If Spotify markup changes, `server/spotify.py` fallbacks are the
  first place to look.
- Live deployment verification (Railway) is in `CLAUDE.md`; the container runs as root.
