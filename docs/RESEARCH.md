# Research notes — free full-quality streams

Agent-oriented summary of investigations into how third-party “free download” sites work
and what that implies for **ytdl4me**. Not a howto for piracy markets. Conclusions drive
product design: match cascade + optional **operator-owned** credentials.

## Beatport

### What we tested

- Public web + `api.beatport.com/v4` (OAuth client id used by the website).
- Unauthenticated `download/?quality=` and `stream/` → **401**.
- Free `sample_url` / LOFI geo samples → short ~96 kbps previews, not masters.
- `free_downloads[]` rare promotional assets only.
- yt-dlp Beatport extractor → previews / CF issues.
- Datacenter Cloudflare blocks on `www` → mitigated for **metadata** via jina.ai reader.
- [unspok3n/beatportdl](https://github.com/unspok3n/beatportdl): username/password → OAuth
  authorize → progressive AAC/FLAC or AES-HLS — requires **Streaming plan**.

### Conclusion

There is **no unpaid full-master path**. Free product behavior:

1. Public metadata (scrape / jina).
2. SoundCloud match (decrypt) → YouTube match.
3. Optional native: `BEATPORT_USERNAME`/`PASSWORD` or tokens for plan holders.

## free-mp3-download.net (and similar)

- Public Deezer search/metadata.
- Server-side download via **shared Deezer ARL** + standard Blowfish decrypt (same crypto
  as open-source Deezer tools / lucida Deezer module).
- Not free cryptography breakthroughs; **account pooling**.

## lucida.to

### Public facts (FAQ / credits / open clients)

- Wrapper around open-source **lucida** TypeScript library ([git.gay/tasky/lucida](https://git.gay/tasky/lucida), npm `lucida`).
- FAQ: “100% real **decrypted** files from the services”; library does not ship accounts.
- Credits list **“Account providers”** — site runs shared paid sessions.
- Roadmap openly references purchasing accounts for new services.
- Services (library): Deezer (ARL + Blowfish), TIDAL (OAuth tokens), Qobuz (login token),
  Spotify (librespot), SoundCloud — **no Beatport**.
- Clients reverse site API: CSRF from SSR page → `POST /api/load?url=%2Fapi%2Ffetch%2Fstream%2Fv2`
  → poll `{server}.lucida.to/api/fetch/request/{handoff}` → download. Cloudflare common.

### Conclusion for ytdl4me

Do **not** proxy lucida.to (ToS, CF, third-party account farm, instability). Mirror the
**honest** model: optional first-party tokens on **this** deployment + free match cascade.

## Design rule of thumb

| Approach | Use in ytdl4me? |
|---|---|
| Shared grey-market accounts | **No** |
| Operator’s own subscription tokens in Railway env | **Yes** (optional) |
| Public metadata + SC/YT match | **Yes** (default free path) |
| Scraping third-party ripper sites | **No** |

## Related code

- Beatport: `server/beatport.py`
- Deezer ARL: `server/deezer.py`, env `DEEZER_ARL`
- Match: `server/match_sources.py`, `server/yt_match.py`
- Platform matrix: [`PLATFORMS.md`](PLATFORMS.md)
