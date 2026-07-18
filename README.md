# ytdl4me

Self-hosted web app for downloading media from **YouTube, Vimeo, SoundCloud, and Spotify** — built as a study of modern media pipelines: stream selection, lossless remuxing, and metadata handling. One Docker container: FastAPI + [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg behind a clean, no-build-step web UI.

> [!WARNING]
> **FOR RESEARCH AND EDUCATIONAL PURPOSES ONLY.**
> This tool must **not** be used to download copyrighted content or any content you do not own or have explicit permission to save. You are solely responsible for how you use it. Read the [full disclaimer](#disclaimer--acceptable-use) before running it.

![ytdl4me UI](docs/screenshot.png)

## Disclaimer & acceptable use

This project exists **solely for research and educational purposes**: studying how media platforms deliver streams, and how stream selection, remuxing, transcoding, and metadata pipelines work. It is not designed, intended, or endorsed for infringing anyone's rights, and it is provided **as-is, without warranty of any kind**.

**✅ You may use it only with:**

- content **you created and own**;
- content whose rights holder has given you **explicit permission** to download;
- **public-domain** media, or media under an **open license** (e.g. Creative Commons) that permits downloading;
- your own uploads that you're backing up from your own accounts.

**🚫 You may not use it to:**

- download, copy, or archive **copyrighted content without authorization** from the rights holder;
- **redistribute, re-upload, sell, or monetize** anything you download;
- bypass **paywalls, subscriber-only content, purchases, or DRM** (the tool does not do this, and no support will be given for attempting it);
- perform **bulk scraping or mass downloading** of any platform.

**Platform terms.** The terms of service of YouTube, Vimeo, SoundCloud, and Spotify generally prohibit unauthorized downloading — in many cases *even for content that is freely licensed*. Using this tool against those platforms may breach their terms regardless of copyright status. Reviewing and complying with the applicable terms is **your responsibility**.

**Spotify.** This tool never touches Spotify's DRM-protected streams and does not circumvent any technical protection measure. It reads a track's *public metadata* (artist, title, artwork) and downloads the closest matching audio from YouTube — the same approach as spotDL. Everything above about copyright and platform terms applies to that YouTube download.

**Jurisdiction.** Copyright and private-copying law varies by country. What is lawful in one jurisdiction may be infringement in another. Know your local law before using this tool.

**No liability.** The authors and contributors accept **no responsibility or liability** for what you do with this software, for any content you download, or for any consequences of its use — including account suspensions, ToS enforcement, or legal claims. Misuse is entirely at your own risk.

**When in doubt, don't download it.** If you cannot clearly point to why you have the right to save a file, assume you don't.

## Features

- **Quality tiers without quality loss.** Original / 1080p / 720p are produced by *selecting source streams*, never by re-encoding. ffmpeg only merges and remuxes with stream copy — the video bits are exactly what the platform served. Smaller tiers stay small because yt-dlp prefers efficient codecs (VP9/AV1) at each resolution.
- **Honest "best" audio.** "Original (best quality)" is a bit-exact copy of the source audio stream in its native container (Opus or M4A). The sources are already lossy, so we deliberately don't offer FLAC/WAV — it would triple the file size and add zero quality.
- **MP3 tiers.** 320 / 256 / 192 / 128 kbps CBR via LAME, with metadata and cover art embedded — for players that need MP3.
- **Spotify, explained honestly.** Spotify streams are DRM-protected and cannot be ripped directly. Like spotDL, ytdl4me reads the track's *public metadata* (artist, title, artwork), finds the best matching audio on YouTube, and downloads that. Quality depends on the match; single tracks only in v1.
- **Live progress.** Per-job progress with speed and ETA; the file auto-downloads in your browser when ready.
- **Optional access key.** Set `ACCESS_KEY` and the API requires it — sensible for anything internet-facing.
- **Self-cleaning.** Job files are temporary and deleted after `FILE_TTL_MINUTES` (default 60).

## Quickstart

### Docker Compose (recommended)

```bash
git clone https://github.com/adamdexter/ytdl4me.git
cd ytdl4me
cp .env.example .env          # optional: set ACCESS_KEY etc.
docker compose up -d --build
```

Open <http://localhost:8000>.

### Plain `docker run`

```bash
docker build -t ytdl4me .
docker run -d --name ytdl4me \
  -p 8000:8000 \
  -e ACCESS_KEY=change-me \
  -v ytdl4me-data:/data \
  ytdl4me
```

### Local development (venv)

Requires Python 3.12+ and `ffmpeg` on your PATH.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --reload --port 8000
```

## Configuration

All variables are optional. See [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Listen port (the Docker image always listens on `8000` internally; remap with `-p`) |
| `ACCESS_KEY` | unset | If set, all `/api/*` routes (except `/api/health`) require it via `X-Access-Key` header or `?key=` query param |
| `DOWNLOAD_DIR` | `<repo>/downloads` (`/data` in Docker) | Working directory for job files; each job gets its own subdirectory |
| `FILE_TTL_MINUTES` | `60` | Completed job files older than this are deleted by a background task |
| `MAX_CONCURRENT_JOBS` | `3` | Simultaneous downloads; extra jobs wait in the queue |
| `ALLOW_ANY_SITE` | `false` | If `false`, only the four supported platforms are accepted |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | unset | Reserved for future album/playlist enumeration; unused in v1 |
| `COOKIES_FILE` | unset | Path to a Netscape-format `cookies.txt` passed to yt-dlp (see troubleshooting) |

## Deployment

This app needs a real, always-on process with ffmpeg and scratch disk — i.e. a container host or a VPS, not serverless.

### Railway (recommended)

1. Push this repo to GitHub, then in [Railway](https://railway.app) create a **New Project → Deploy from GitHub repo**. Railway auto-detects the Dockerfile and builds it.
2. In the service's **Variables**, add `ACCESS_KEY` (and anything else from the table above).
3. Under **Settings → Networking**, generate a public domain and set the target port to **8000**.

### Fly.io

The repo ships a `fly.toml` (edit the app name if `ytdl4me` is taken):

```bash
fly launch --no-deploy      # accepts the existing fly.toml
fly secrets set ACCESS_KEY=change-me
fly deploy
```

Machines auto-stop when idle. `/data` is ephemeral on Fly, which is fine — job files are temporary by design.

### VPS / home server

Any box with Docker:

```bash
git clone https://github.com/adamdexter/ytdl4me.git && cd ytdl4me
cp .env.example .env    # set ACCESS_KEY
docker compose up -d --build
```

Put a reverse proxy with TLS in front if it's internet-facing, e.g. Caddy:

```
media.example.com {
    reverse_proxy localhost:8000
}
```

### Why not Vercel or SiteGround?

- **Vercel** is a serverless platform: functions have short execution limits and small request/response size caps, there is no ffmpeg or persistent yt-dlp runtime, and no writable working disk for multi-hundred-MB jobs. On top of that, YouTube aggressively bot-blocks well-known datacenter IP ranges. A download that streams for minutes and merges gigabytes simply isn't a serverless workload.
- **SiteGround shared hosting** (and shared hosting generally) doesn't allow long-running daemons — you can't keep a uvicorn server or an ffmpeg process alive, and background jobs get killed.

Anything that runs Docker (Railway, Fly.io, Render, a VPS, a Raspberry Pi at home) works.

## Troubleshooting

### YouTube: "Sign in to confirm you're not a bot"

YouTube sometimes challenges requests from datacenter IPs (common on cloud hosts). Fix: give yt-dlp cookies from a logged-in browser session.

1. Export cookies for `youtube.com` in Netscape format (e.g. the "Get cookies.txt LOCALLY" browser extension).
2. Make the file available to the container and point `COOKIES_FILE` at it:

```yaml
# docker-compose.yml, under the service:
    volumes:
      - ./cookies.txt:/data/cookies.txt:ro
    environment:
      - COOKIES_FILE=/data/cookies.txt
```

Cookies expire; re-export them if the error returns. Prefer a throwaway Google account over your main one.

## Legal

See the [Disclaimer & acceptable use](#disclaimer--acceptable-use) section at the top of this README — it is a condition of using this software. **Research and educational purposes only**; never for copyrighted content or content that isn't yours.
