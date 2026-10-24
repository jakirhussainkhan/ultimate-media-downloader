# Ultimate Media Downloader

**A security-hardened, self-hosted media toolkit for public social links.**
Paste a link, preview the original, download the source file, or extract a clean MP3 — without popups, fake resolutions, or stored media.

![Version](https://img.shields.io/badge/version-1.0.0-34d399?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat-square&logo=flask&logoColor=white)
![Security](https://img.shields.io/badge/SSRF-hardened-22d3ee?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-a78bfa?style=flat-square)

---

## Overview

Ultimate Media Downloader is a small Flask application that turns a public
Pinterest, Instagram, Facebook, YouTube, TikTok, or other `yt-dlp`-supported
link into a direct, original-quality download. It combines a dedicated
Pinterest metadata scraper with the `yt-dlp` extractor ecosystem, then proxies
the resulting media through a guarded streaming endpoint.

The project is designed to be easy to run locally and deliberately careful
about what the server is allowed to fetch. User-supplied URLs are untrusted
input, so every outbound request is validated against an SSRF guard before a
socket is opened.

## Features

- **Universal extraction** — Pinterest's Schema.org data is parsed directly;
  everything else goes through `yt-dlp`'s 1,000+ supported sites.
- **Original-quality media** — the source file is streamed as-is, with no
  re-encoding, watermark, or invented quality options.
- **Smart 720p strategy** — progressive MP4 is preferred for browser preview;
  when 720p is unavailable the extractor falls back to the best playable
  stream, then to the best available video-only source.
- **Native previews** — video, image, and audio previews render inline with
  graceful fallbacks when a CDN blocks hotlinking.
- **On-demand MP3** — `ffmpeg` extracts a 192 kbps MP3 into a private temp
  directory, which is removed immediately after streaming.
- **Premium UI** — responsive dark interface, page-load animations, loading
  states, inline toasts, keyboard-friendly controls, and reduced-motion support.
- **Security-first backend** — SSRF protection, redirect re-validation, strict
  content-type checks, download size caps, rate limiting, CSP, and hardened
  headers.
- **Zero media persistence** — no database, no download archive, no media
  directory. Temporary audio is created per request and shredded after use.
- **Operational visibility** — structured logs, request IDs, `/health`, and
  `/api/version` endpoints.

## Supported sources

| Source | Preview | Video | Image | MP3 |
| --- | :---: | :---: | :---: | :---: |
| Pinterest | Yes | Yes | Yes | Video pins |
| Instagram | Yes | Yes | Yes | Public videos |
| Facebook | Yes | Yes | Yes | Public videos |
| YouTube | Yes | Yes | — | Yes |
| TikTok, X, Reddit, Vimeo, and other `yt-dlp` sites | Yes | Yes | Varies | Public videos |

Public content only. Private, login-gated, age-restricted, or DRM-protected
media is intentionally out of scope.

## Tech stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.10+, Flask 3, Gunicorn |
| Extraction | yt-dlp, BeautifulSoup4 |
| HTTP client | Requests |
| Media processing | ffmpeg (optional, required for MP3) |
| Frontend | Semantic HTML, modern CSS, vanilla JavaScript |
| Configuration | Environment variables / `.env` via python-dotenv |

## Project structure

```text
ultimate-media-downloader/
├── app.py                      # Flask routes, streaming, headers, errors
├── media_extractor.py          # yt-dlp + Pinterest extraction, format selection
├── security.py                 # SSRF guard, safe redirects, rate limiter
├── version.py                  # Single source of truth for v1.0.0
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── run.sh / run.bat            # Local launchers
├── UltimateMediaDownloader.spec# Optional PyInstaller packaging
├── templates/
│   ├── index.html              # Main UI
│   └── error.html              # Branded error page
├── static/
│   ├── css/styles.css
│   ├── js/app.js
│   └── favicon.svg
├── tests/
│   ├── test_app.py             # HTTP validation smoke tests
│   └── test_security.py        # SSRF and rate-limit unit tests
└── docs/project-brief.md       # Historical product brief
```

## Getting started

### Prerequisites

- **Python 3.10 or newer**
- **ffmpeg** (only needed for MP3 extraction)

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg

# macOS
brew install ffmpeg

# Windows (PowerShell)
winget install Gyan.FFmpeg
```

### Installation

```bash
git clone https://github.com/jakirhussainkhan/ultimate-media-downloader.git
cd ultimate-media-downloader

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env                 # optional; edit as needed
python app.py
```

Open <http://127.0.0.1:5000>.

Or use a launcher, which creates the virtual environment automatically:

```bash
./run.sh            # Linux / macOS
run.bat             # Windows
```

### LAN access

The server binds to `127.0.0.1` by default. To reach it from a phone on the same
Wi-Fi network:

```bash
HOST=0.0.0.0 PORT=5000 python app.py
```

```bat
set HOST=0.0.0.0
set PORT=5000
run.bat
```

Only do this on a trusted network. The server has rate limiting but no user
authentication; do not expose it directly to the public internet.

## Configuration

Copy `.env.example` to `.env`, or inject environment variables directly.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for LAN. |
| `PORT` | `5000` | HTTP port. |
| `FLASK_DEBUG` | `false` | Never enable on a public server. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `TRUST_PROXY` | `false` | Trust one layer of `X-Forwarded-*` headers. Enable only behind a known reverse proxy. |
| `MAX_DOWNLOAD_BYTES` | `2147483648` (2 GiB) | Hard streaming cap for `/download`. |
| `MAX_AUDIO_INPUT_BYTES` | `536870912` (512 MiB) | Input/output cap for MP3 extraction. |
| `FETCH_RATE_LIMIT` | `20` / 60 s | Extraction attempts per client IP. |
| `DOWNLOAD_RATE_LIMIT` | `30` / 60 s | Download proxy requests per client IP. |
| `AUDIO_RATE_LIMIT` | `5` / 300 s | MP3 extraction attempts per client IP. |

## API

All errors are JSON and include a `code` and `request_id`:

```json
{
  "error": "Requests to local, private, or metadata addresses are blocked.",
  "code": "unsafe_url",
  "request_id": "5f2a8c1d9e30"
}
```

### `POST /fetch`

Extract media metadata and a validated direct media URL.

```bash
curl -X POST http://127.0.0.1:5000/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.pinterest.com/pin/123456789/"}'
```

```json
{
  "type": "video",
  "download_url": "https://v1.pinimg.com/...mp4",
  "source_url": "https://www.pinterest.com/pin/123456789/",
  "title": "Example pin",
  "platform": "Pinterest",
  "quality": "720p",
  "format": "MP4",
  "duration": 12.4,
  "has_audio": true
}
```

### `GET /download?url=...&type=video|image|audio`

SSRF-validated streaming proxy. Sets `Content-Disposition: attachment` and
never exposes the upstream `Content-Type` without validation.

### `GET /download_mp3?url=...`

Accepts the original post URL, extracts audio with `yt-dlp` + `ffmpeg`, and
streams a 192 kbps MP3. The temporary directory is removed when the response
closes, including client disconnects.

### `GET /health`

```json
{
  "status": "ok",
  "name": "Ultimate Media Downloader",
  "version": "1.0.0",
  "uptime_seconds": 42,
  "ffmpeg": true
}
```

### `GET /api/version`

Returns the application name and semantic version.

### Error codes

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `missing_url`, `unsafe_url`, `invalid_json` | Invalid or blocked input. |
| 413 | `media_too_large`, `source_too_large` | Configured size limit exceeded. |
| 415 | `unsupported_media_type` | `/fetch` requires JSON. |
| 422 | `no_media_found`, `audio_extraction_failed` | No public media, or extraction refused. |
| 429 | `rate_limited` | Client exceeded its window. Check `Retry-After`. |
| 502 | `upstream_error`, `invalid_media_response` | Source failed or returned non-media. |
| 503 | `ffmpeg_unavailable` | MP3 requested but ffmpeg is missing. |
| 504 | `source_timeout` | Upstream timed out. |

## Security model

### SSRF protection

`security.py` is the only path used for outbound URL fetching:

1. Only `http` and `https` schemes are accepted.
2. URLs with embedded credentials are rejected.
3. Only ports **80** and **443** are allowed.
4. Hostnames are resolved, and **every** returned address must be globally
   routable. Loopback, private, link-local, reserved, multicast, and shared
   address space are blocked.
5. Cloud metadata endpoints (`169.254.169.254`, ECS, Alibaba, AWS IPv6) and
   internal suffixes (`.local`, `.internal`, `.home.arpa`, etc.) are blocked.
6. Redirects are followed manually, with each hop re-validated before the next
   request is made.
7. Media URLs returned by extractors are validated again before the browser or
   download proxy sees them.
8. `/download` repeats validation immediately before opening the upstream
   connection and applies a content-type allowlist plus a byte ceiling.

### Application hardening

- **Rate limiting** on extraction, downloads, and MP3 jobs.
- **No debug mode by default**; the development debugger is opt-in and logged
  as a warning if bound beyond loopback.
- **Strict CSP** with no inline scripts or styles, plus `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and HSTS on
  HTTPS.
- **DOM-safe frontend** — server data is rendered with `textContent` and
  `URL` validation, never injected as HTML.
- **Bounded request bodies** — JSON payloads are capped at 16 KiB.
- **Private temp directories** — `tempfile.mkdtemp` with cleanup on response
  close and generator teardown.
- **No secrets in the repository** — `.env`, `TOKEN`, keys, media, and build
  artifacts are ignored.

### Residual risks and deployment notes

This is an SSRF-hardened application, not a network sandbox.

- `yt-dlp` extractors may issue additional subrequests derived from page
  metadata. The initial URL and returned media URL are validated, but not every
  extractor-internal request can be inspected by this application.
- DNS validation and the subsequent socket connection are separate operations;
  a hostile DNS server could theoretically change its answer in between (DNS
  rebinding).
- For internet-facing deployments, run as an unprivileged user, place the app
  behind a TLS reverse proxy, and apply egress firewall rules that deny access
  to private and link-local ranges at the network layer.
- The in-memory rate limiter is per process. Use a shared store such as Redis if
  you run multiple workers and need a global limit.

## Running in production

```bash
gunicorn --workers 2 --threads 4 --bind 127.0.0.1:5000 --timeout 180 app:app
```

Recommended:

- Terminate TLS at Nginx/Caddy and proxy to `127.0.0.1:5000`.
- Set `TRUST_PROXY=true` only when that proxy is trusted.
- Set a restrictive firewall and run the service as a non-root user.
- Keep `yt-dlp` current — extractor changes are frequent.
- Monitor logs and `/health`.

## Testing

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The suite covers SSRF blocking (loopback, RFC1918, metadata, IPv6, schemes,
ports, credentials), redirect safety, rate limiting, API validation, security
headers, branded errors, and payload limits. The tests do not require outbound
network access.

## v1.0.0 release notes

**Released 2026-10-24**

This is the first public release of Ultimate Media Downloader.

- Rebranded and rebuilt the prototype as **Ultimate Media Downloader v1.0.0**.
- Added a strict SSRF guard with DNS resolution checks, metadata blocking,
  manual redirect validation, and port/scheme restrictions.
- Replaced permissive error handling with JSON error contracts, correct HTTP
  statuses, request IDs, and recovery from extractor/network failures.
- Added a 720p-first progressive format selector with playable and
  video-only fallbacks.
- Added a two-minute extraction cache to speed up repeated preview/MP3 use.
- Added download byte ceilings, content-type allowlists, rate limiting, and
  immediate temp-file cleanup.
- Rebuilt the UI with glassmorphism styling, animated loading states, inline
  toasts, safe previews, MP3 progress handling, and responsive/mobile layouts.
- Added CSP and hardened security headers; removed all inline scripts and
  styles.
- Added versioning (`version.py`), `.env` support, tests, launch scripts,
  packaging metadata, and this documentation.

## Contributing

1. Fork the repository and create a feature branch.
2. Keep security boundaries intact: new outbound requests must go through
   `security.safe_get` / `validate_public_url`.
3. Add tests for new behaviour.
4. Run `python -m unittest discover -s tests -v`.
5. Open a pull request with a clear description and threat-model notes when
   behaviour affects network access.

## Disclaimer

Ultimate Media Downloader is provided for downloading content you own or have
permission to use. Respect copyright, platform terms of service, and applicable
law. The authors are not responsible for misuse. This project is not affiliated
with Pinterest, Meta, Google, or any supported platform.

## License

MIT — see [LICENSE](LICENSE).

---

Built with Flask, yt-dlp, and a healthy respect for the open web.
