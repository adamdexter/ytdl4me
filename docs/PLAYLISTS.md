# Playlists, albums, and sets

Feature: multi-item links in the **same URL field**, track selection UI, ZIP (or multi-job)
download. Implemented in `server/playlists.py`, batch orchestration in `server/main.py`,
UI in `static/app.js` / `app.css`.

## User flow

1. Paste playlist/album/set URL → **Fetch**.
2. Probe returns `kind: "playlist"` with `entries[]`.
3. Checkboxes (default all selected), Select all / none, **Download as ZIP** toggle.
4. Click a quality option (audio or video for YT playlists).
5. One download row for ZIP batches; progress shows `done/total` tracks.
6. Auto-save ZIP or file when `status=done`.

Single-item links are unchanged (`kind: "audio"` | `"video"`).

## Detection

`platforms.looks_like_playlist(url, platform)` — URL shape only (cheap).

| Platform | Multi-item shapes |
|---|---|
| YouTube | path starts with `/playlist` |
| SoundCloud | `/sets/` |
| Spotify, Deezer, JOOX, TIDAL | `/album/` or `/playlist/` |
| Apple Music | `/playlist/` **or** `/album/` without `?i=` and without `/song/` |
| Beatport | `/release/`, `/chart/`, `/playlist/` (not label/genre indexes) |

**Not expanded:** YouTube `watch?v=…&list=…` remains a single video (`noplaylist: True` on
single downloads). Users must open the playlist page to grab the full list.

## Probe API

`POST /api/probe` `{"url": "…"}`

Success (playlist):

```json
{
  "platform": "deezer",
  "kind": "playlist",
  "url": "https://www.deezer.com/album/…",
  "title": "Discovery",
  "uploader": "Daft Punk",
  "thumbnail": "…",
  "track_count": 14,
  "truncated": false,
  "entries": [
    {
      "index": 1,
      "url": "https://www.deezer.com/track/…",
      "title": "One More Time",
      "uploader": "Daft Punk",
      "duration": 320.0,
      "thumbnail": "…"
    }
  ],
  "video_options": [],
  "audio_options": [/* same ids as single: audio_best, mp3_320, … */],
  "original_quality": "Playlist · 14 items"
}
```

YouTube playlists also populate `video_options` (`original` / `1080p` / `720p`).

**Cap:** `MAX_PLAYLIST_TRACKS` (default **100**). Excess sets `truncated: true`.

**Timeout:** playlist probes allow ~90s (pagination / yt-dlp flat extract).

## Download API

`POST /api/download`

```json
{
  "url": "<playlist url>",
  "option_id": "audio_best",
  "entries": ["https://…/track/1", "https://…/track/2"],
  "zip": true,
  "title": "Optional archive name"
}
```

| Case | Behavior |
|---|---|
| No `entries`, single-item URL | Classic one-file job |
| `entries` + `zip: true` (default if N>1) | One **batch** job → ZIP of successes |
| `entries` + `zip: false` + N>1 | **N jobs**; response `{job_id, job_ids:[]}` |
| `entries` empty + playlist URL | Server re-enumerates and takes all (up to cap) |
| Playlist URL + no entries on single path | 422 asking to select tracks |

Each entry URL is re-validated: same `detect_platform` as parent, public host if `other`.

### Batch job public fields

```json
{
  "job_id": "…",
  "status": "downloading",
  "progress": 50.0,
  "filename": "Discovery.zip",
  "batch": { "total": 2, "done": 1, "failed": 0, "zip": true },
  "error": null
}
```

Partial failure: `status=done`, ZIP contains successes, `error` may be a soft warning string
(e.g. “Finished with 1 failed track(s)…”). Full failure only if **zero** successes.

### Layout on disk

```
DOWNLOAD_DIR/<job_id>/
  tracks/
    001 - Artist - Title.flac
    002 - …
    001/   (empty leftovers ok)
  Title.zip
```

Zip members are **basenames only** (no path traversal).

## Enumeration backends (`playlists.py`)

| Platform | Method |
|---|---|
| YouTube | yt-dlp `extract_flat` + `playlistend` |
| SoundCloud | `api-v2` resolve playlist + tracks |
| Deezer | `api.deezer.com/{album\|playlist}/{id}` + paging |
| Spotify | Web API client-credentials **or** embed scrape fallback |
| TIDAL | `api.tidal.com/v1/…/items` (+ optional user token) |
| Apple Music | amp-api catalog with scraped developer JWT |
| Beatport | scrape `__NEXT_DATA__` / jina / track-link harvest |
| JOOX | intentional error — not supported yet |

### Spotify notes

- Without `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`, enumeration may fail on many lists.
- Create a free app at developer.spotify.com → Client Credentials (no user OAuth needed for
  public playlists/albums).
- Single Spotify **tracks** never need these keys (oEmbed/embed scrape still works).

## Frontend symbols (`static/app.js`)

| Symbol | Role |
|---|---|
| `looksLikePlaylist` | Badge “playlist” chip (mirrors backend shapes) |
| `renderPlaylistPicker` | Checkboxes, ZIP toggle, select all/none |
| `startPlaylistDownload` | POST with `entries` + `zip` + `title` |
| `addDownload` | Shows batch progress from `job.batch` |

## Concurrency & abuse

- Parent batch counts as **1** active job toward `MAX_ACTIVE_JOBS`.
- Intra-batch download concurrency limited (~2) via semaphore.
- `zip: false` multi creates N jobs and can hit the active-job cap — UI/API returns 429.
- Rate limit still applies to probe/download POSTs.

## Extending

1. Add URL shape to `looks_like_playlist` (backend + `looksLikePlaylist` in app.js).
2. Implement `_enum_<platform>` in `playlists.py` returning `{title, uploader, thumbnail, entries, truncated?}`.
3. Ensure each entry `url` is a **single-item** link the existing download path accepts.
4. Document in this file + `PLATFORMS.md` + known-good seed in verify skill.
5. Runtime: probe list length → ZIP of 2 tracks → `unzip -l` / `ffprobe`.

## Known-good multi-item seeds

| Platform | Example |
|---|---|
| Deezer album | `https://www.deezer.com/album/302127` (Discovery — 14 tracks) |
| YouTube playlist | `https://www.youtube.com/playlist?list=PLBCF2DAC6FFB574DE` |
| SoundCloud set | any public `/sets/` URL |
| Spotify | public playlist (prefer with API credentials set) |

## Verify snippet

```bash
# probe
curl -sS -X POST "$URL/api/probe" -H "Content-Type: application/json" \
  -H "X-Access-Key: $KEY" \
  -d '{"url":"https://www.deezer.com/album/302127"}' | jq '{kind,title,track_count,n:(.entries|length)}'

# zip two tracks
curl -sS -X POST "$URL/api/download" -H "Content-Type: application/json" \
  -H "X-Access-Key: $KEY" \
  -d '{"url":"https://www.deezer.com/album/302127","option_id":"audio_best","zip":true,"title":"Discovery","entries":["https://www.deezer.com/track/3135553","https://www.deezer.com/track/3135554"]}'
# → poll job until done; filename Discovery.zip
```
