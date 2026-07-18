---
name: verify
description: Build, launch, and drive ytdl4me end-to-end to verify changes at the real surfaces (API + browser UI).
---

# Verifying ytdl4me

## Launch

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8741 &
curl -s http://127.0.0.1:8741/api/health   # {"status":"ok",...}
```

Startup takes ~3 s (yt-dlp import) — retry the health check once before assuming failure.
For auth flows, run a second instance: `ACCESS_KEY=<key> ... --port 8742`.

## Drive — API surface

- Probe: `POST /api/probe {"url": ...}`. Good known inputs:
  - 4K YouTube (tier logic: original+1080p+720p): `https://www.youtube.com/watch?v=aqz-KE-bpKQ`
  - Tiny YouTube video (fast full download): `https://www.youtube.com/watch?v=jNQXAC9IVRw`
  - Short 4K clip (11 s — fast tier downloads): `https://www.youtube.com/watch?v=2PuFyjAs7JA`
  - SoundCloud: `https://soundcloud.com/forss/flickermood`
  - Vimeo (720p source → no lower tiers): `https://vimeo.com/76979871`
  - Spotify: `https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT`
- Download: `POST /api/download {"url", "option_id"}` → poll `GET /api/jobs/{id}` (~1 s
  interval) until `done` → `GET /api/jobs/{id}/file`. A poll/fetch helper pattern lives
  in the session that built this; ~20 lines of urllib is enough.
- Verify outputs with `ffprobe -show_entries stream=codec_name,width,height,bit_rate`:
  tier files must be exactly 1080/720 high; `mp3_N` must show `bit_rate=N000`;
  `audio_best` must be the native codec (opus/aac), not mp3.
- Error paths worth re-checking after route changes: non-platform URL, playlist URL,
  garbage URL (all 422 JSON), unknown job (404), burst >30 probes/min (429).

## Drive — browser UI

Playwright (chromium) against the running server; key selectors:
`#url-input`, `#fetch-btn`, `#platform-badge`, `#probe-card button` (option tiles),
`#downloads-list li[data-state="done"]`, `#key-modal[open]`, `#key-input`, `#key-hint`.
Use `colorScheme: "dark"`, `acceptDownloads: true`; assert exactly ONE `download` event
per job (auto-click-once guard) and zero pageerrors.

## Gotchas

- YouTube may serve "confirm you're not a bot" from datacenter IPs; local residential
  runs are fine. If it appears, set `COOKIES_FILE`.
- MP3 size estimates exclude embedded cover art, so actual files run larger — not a bug.
- The Spotify path downloads the matched YouTube audio; titles come from Spotify's
  oEmbed/embed page. If Spotify markup changes, `server/spotify.py` fallbacks are the
  first place to look.
