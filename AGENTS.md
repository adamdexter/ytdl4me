# AGENTS.md — portable entry for any coding agent

**Read this first** if you are Claude Code, Codex, Cursor, Grok, or any other agent
working on this repo. Human-facing product docs: `README.md`. Historical design intent:
`SPEC.md` (drifted). **Ground truth for ops + architecture: `CLAUDE.md`.**

This file is intentionally short: it routes you to the right artifacts and encodes
non-negotiable rules that must not be lost when hopping between agents.

## Doc map (start here)

| Priority | Path | Audience / purpose |
|---|---|---|
| **1** | [`CLAUDE.md`](CLAUDE.md) | **Agent runbook** — architecture, Railway, cookies, YouTube walls, invariants |
| **2** | [`docs/AS_BUILT.md`](docs/AS_BUILT.md) | Chronological as-built log of major features + design decisions |
| **3** | [`docs/PLATFORMS.md`](docs/PLATFORMS.md) | Per-platform resolve/download matrix (native vs match cascade) |
| **4** | [`docs/PLAYLISTS.md`](docs/PLAYLISTS.md) | Playlist/album probe + ZIP batch API and UI |
| **5** | [`docs/RESEARCH.md`](docs/RESEARCH.md) | External research notes (Beatport walls, lucida/free-mp3 patterns) |
| **6** | [`docs/README.md`](docs/README.md) | Index of `docs/` |
| — | [`.claude/skills/verify/SKILL.md`](.claude/skills/verify/SKILL.md) | Runtime verification recipes (API + UI) |
| — | [`README.md`](README.md) | Humans: features, config table, disclaimer |
| — | [`.env.example`](.env.example) | All env vars as commented placeholders |
| — | [`SPEC.md`](SPEC.md) | Original design only — do not treat as current behavior |

## What this app is

Self-hosted FastAPI + vanilla JS downloader for YouTube, Vimeo, SoundCloud, Spotify,
Deezer, JOOX, TIDAL, Apple Music, Beatport. Deployed on Railway (see `CLAUDE.md`).
**Public GitHub repo** — never commit secrets.

## Hard invariants (do not violate)

1. **Never re-encode video**; audio “Original” stays bit-exact (MP3 tiers may transcode).
2. **Never pass user input to a shell**; yt-dlp via Python API only.
3. **Never commit** `ACCESS_KEY`, cookies, ARL, tokens, passwords, `.env`, share links with keys.
4. Commit identity: GitHub **noreply** only (repo-local config).
5. File serve stays **path-traversal-guarded** (`realpath` under job dir).
6. Keep README disclaimer + `robots.txt` + unlisted access model.

## Quick orientation

```
static/*  →  POST /api/probe  →  single track OR kind:playlist + entries[]
          →  POST /api/download → Job (single or batch ZIP)
          →  GET  /api/jobs/{id} → poll
          →  GET  /api/jobs/{id}/file
```

- **Match cascade** (Spotify / unpaid Deezer·TIDAL·Apple·Beatport): public meta → SoundCloud
  decrypt match → else YouTube `ytsearch1:`.
- **Optional native tokens** unlock first-party streams (e.g. `DEEZER_ARL`, Beatport login,
  TIDAL access token). See `docs/PLATFORMS.md`.
- **Playlists**: same URL field; see `docs/PLAYLISTS.md`.

## Before you change code

1. Read the relevant section of `CLAUDE.md` + the feature doc under `docs/`.
2. Prefer extending existing modules over new frameworks.
3. After behavior changes: update **README config table**, **`.env.example`**, **`CLAUDE.md`**,
   and the matching `docs/*` file in the same PR/commit when possible.
4. Verify with runtime observation (`.claude/skills/verify/SKILL.md`), not unit-test theater.
5. Deploy path: commit → `git push origin main` →
   `railway redeploy --service ytdl4me --environment production --from-source -y`.

## Secrets & Railway (names only in git)

Live secrets live **only** in Railway Variables (service `ytdl4me`, env `production`).
Typical names: `ACCESS_KEY`, `COOKIES_B64`, `COOKIES_STATE_FILE`, `DOWNLOAD_DIR`,
`DEEZER_ARL`, optional platform tokens. **Never paste secret values into commits or docs.**

Share link form (value not in repo):  
`https://ytdl4me-production.up.railway.app/#key=<ACCESS_KEY from Railway>`.

## Extending safely

| Goal | Where |
|---|---|
| New platform | `platforms.py` + `app.js` hosts + icon + module or match path; `docs/PLATFORMS.md` |
| New playlist enumerator | `server/playlists.py` + `looks_like_playlist`; `docs/PLAYLISTS.md` |
| Quality tier | `downloader.py` `_FORMAT_SPECS` / `audio_common.AUDIO_OPTION_IDS` |
| Match scoring | `match_sources.py` |
| Ops / deploy | `CLAUDE.md` Railway section |

## When docs conflict

**Order of authority:** running code → `CLAUDE.md` / `docs/AS_BUILT.md` → `README.md` → `SPEC.md`.
If you fix drift, update the higher-authority docs in the same change.
