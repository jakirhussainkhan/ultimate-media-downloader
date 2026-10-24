"""Ultimate Media Downloader - Flask application.

A small, security-conscious media extraction and download proxy built on
yt-dlp, BeautifulSoup, and Flask.  See README.md for architecture and usage.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import requests
import yt_dlp
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from media_extractor import (
    ExtractionError,
    SourceTimeoutError,
    UnsupportedMediaError,
    extract_media,
    resolve_source_url,
)
from security import (
    DEFAULT_USER_AGENT,
    RateLimiter,
    ResponseTooLargeError,
    UnsafeURLError,
    iter_limited,
    safe_get,
    validate_public_url,
)
from version import APP_NAME, APP_VERSION

# Load local development settings before configuration is read.  Production
# deployments should inject environment variables directly.
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


STARTED_AT = time.time()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

MAX_DOWNLOAD_BYTES = _env_int("MAX_DOWNLOAD_BYTES", 2 * 1024 * 1024 * 1024)
MAX_AUDIO_INPUT_BYTES = _env_int("MAX_AUDIO_INPUT_BYTES", 512 * 1024 * 1024)
TRUST_PROXY = _env_bool("TRUST_PROXY", False)

FETCH_RATE_LIMIT = _env_int("FETCH_RATE_LIMIT", 20)
FETCH_RATE_WINDOW = 60
DOWNLOAD_RATE_LIMIT = _env_int("DOWNLOAD_RATE_LIMIT", 30)
DOWNLOAD_RATE_WINDOW = 60
AUDIO_RATE_LIMIT = _env_int("AUDIO_RATE_LIMIT", 5)
AUDIO_RATE_WINDOW = 300

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ultimate_media_downloader")

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=16 * 1024,  # JSON payloads are tiny.
    JSON_SORT_KEYS=False,
)

if TRUST_PROXY:
    # Only enable when a trusted reverse proxy sets X-Forwarded-* headers.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_rate_limiter = RateLimiter()

CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
    "video/mpeg": "mpeg",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
}

ALLOWED_MEDIA_PREFIXES = ("video/", "audio/", "image/")
ALLOWED_BINARY_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/mp4",
    "application/ogg",
}

NON_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/pdf",
    "text/html",
    "text/plain",
    "text/xml",
}


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


@app.before_request
def _assign_request_id() -> None:
    g.request_id = uuid.uuid4().hex[:12]


def _error_payload(message: str, code: str) -> dict[str, Any]:
    return {
        "error": message,
        "code": code,
        "request_id": getattr(g, "request_id", None),
    }


def _json_error(message: str, status: int, code: str) -> tuple[Response, int]:
    return jsonify(_error_payload(message, code)), status


def _wants_json() -> bool:
    if request.path.startswith(
        ("/fetch", "/download", "/health", "/api", "/robots.txt")
    ):
        return True
    accept = request.accept_mimetypes
    return bool(accept.best == "application/json" and accept["application/json"] > 0)


def _rate_limit(bucket: str, limit: int, window: int) -> tuple[Response, int] | None:
    key = f"{bucket}:{request.remote_addr or 'unknown'}"
    allowed, retry_after = _rate_limiter.allow(key, limit=limit, window_seconds=window)
    if allowed:
        return None

    response, status = _json_error(
        "Too many requests. Please wait a moment and try again.",
        429,
        "rate_limited",
    )
    response.headers["Retry-After"] = str(retry_after)
    return response, status


def _clean_content_type(raw: str | None) -> str:
    if not raw:
        return ""
    # Never echo unsanitised header values back to the client.
    return re.sub(r"[^a-zA-Z0-9.+/-]", "", raw.split(";", 1)[0]).lower()


def _download_extension(content_type: str, requested_type: str) -> str:
    if content_type in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[content_type]
    return {"video": "mp4", "image": "jpg", "audio": "mp3"}.get(
        requested_type, "bin"
    )


def _mp3_available() -> str | None:
    return shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob: https:; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'none'"
)


@app.after_request
def _security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    response.headers["Server"] = APP_NAME

    if request.is_secure:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    if request.path.startswith(("/download", "/download_mp3", "/fetch", "/api")):
        response.headers["Cache-Control"] = "no-store"
    elif response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-cache"

    return response


# ---------------------------------------------------------------------------
# Pages and health
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        app_name=APP_NAME,
        app_version=APP_VERSION,
        ffmpeg_available=bool(_mp3_available()),
    )


@app.get("/favicon.ico")
def favicon() -> Response:
    response = app.send_static_file("favicon.svg")
    response.headers["Content-Type"] = "image/svg+xml"
    return response


@app.get("/health")
def health() -> Response:
    payload = {
        "status": "ok",
        "name": APP_NAME,
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "ffmpeg": bool(_mp3_available()),
    }
    return jsonify(payload)


@app.get("/api/version")
def api_version() -> Response:
    return jsonify({"name": APP_NAME, "version": APP_VERSION})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/fetch")
def fetch_media() -> tuple[Response, int] | Response:
    limited = _rate_limit("fetch", FETCH_RATE_LIMIT, FETCH_RATE_WINDOW)
    if limited:
        return limited

    if not request.is_json:
        return _json_error(
            "Content-Type must be application/json.", 415, "unsupported_media_type"
        )

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("Request body must be a JSON object.", 400, "invalid_json")

    raw_url = data.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return _json_error("Please provide a valid URL.", 400, "missing_url")
    if len(raw_url) > 4096:
        return _json_error("The URL is too long.", 400, "url_too_long")

    try:
        media = extract_media(raw_url)
    except UnsafeURLError as exc:
        return _json_error(str(exc), 400, "unsafe_url")
    except SourceTimeoutError:
        return _json_error(
            "The source took too long to respond. Please try again.",
            504,
            "source_timeout",
        )
    except UnsupportedMediaError as exc:
        return _json_error(str(exc), 422, "no_media_found")
    except ResponseTooLargeError:
        return _json_error(
            "The source page is too large to process.", 413, "source_too_large"
        )
    except ExtractionError:
        return _json_error(
            "Media extraction failed. Please check the link and try again.",
            502,
            "extraction_failed",
        )
    except requests.RequestException as exc:
        logger.warning("Network error while extracting media: %s", exc.__class__.__name__)
        return _json_error(
            "Could not reach the source. Please try again.", 502, "upstream_error"
        )
    except Exception:
        logger.exception("Unhandled error in /fetch")
        return _json_error(
            "An unexpected error occurred. The server is still running.",
            500,
            "internal_error",
        )

    return jsonify(media)


@app.get("/download")
def download_file() -> Response | tuple[Response, int]:
    limited = _rate_limit("download", DOWNLOAD_RATE_LIMIT, DOWNLOAD_RATE_WINDOW)
    if limited:
        return limited

    file_url = request.args.get("url", "").strip()
    requested_type = request.args.get("type", "video").strip().lower()
    if requested_type not in {"video", "image", "audio"}:
        requested_type = "video"

    if not file_url:
        return _json_error("A media URL is required.", 400, "missing_url")

    try:
        validate_public_url(file_url)
    except UnsafeURLError as exc:
        return _json_error(str(exc), 400, "unsafe_url")

    try:
        upstream = safe_get(
            file_url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
            max_bytes=MAX_DOWNLOAD_BYTES,
            stream=True,
        )
    except UnsafeURLError as exc:
        return _json_error(str(exc), 400, "unsafe_url")
    except ResponseTooLargeError:
        return _json_error(
            "The media exceeds the maximum download size.",
            413,
            "media_too_large",
        )
    except requests.Timeout:
        return _json_error(
            "The media server took too long to respond.", 504, "upstream_timeout"
        )
    except requests.RequestException as exc:
        logger.warning("Upstream request failed in /download: %s", exc.__class__.__name__)
        return _json_error(
            "Could not reach the media server.", 502, "upstream_error"
        )

    if upstream.status_code >= 400:
        upstream.close()
        return _json_error(
            "The media server rejected the download request.",
            502,
            "upstream_rejected",
        )

    content_type = _clean_content_type(upstream.headers.get("Content-Type"))
    if content_type in NON_MEDIA_TYPES or content_type.startswith("text/"):
        upstream.close()
        return _json_error(
            "The upstream response was not media and was blocked.",
            502,
            "invalid_media_response",
        )
    if content_type and not (
        content_type.startswith(ALLOWED_MEDIA_PREFIXES)
        or content_type in ALLOWED_BINARY_TYPES
    ):
        upstream.close()
        return _json_error(
            "The upstream response type is not allowed.",
            502,
            "invalid_media_response",
        )

    extension = _download_extension(content_type, requested_type)
    filename = f"ultimate-media-{uuid.uuid4().hex[:10]}.{extension}"
    response_type = content_type or {
        "video": "video/mp4",
        "image": "image/jpeg",
        "audio": "audio/mpeg",
    }[requested_type]

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": response_type,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    content_encoding = upstream.headers.get("Content-Encoding", "").lower()
    content_length = (upstream.headers.get("Content-Length") or "").strip()
    if content_length.isdigit() and content_encoding in {"", "identity"}:
        headers["Content-Length"] = content_length

    logger.info("Proxying %s download from %s", requested_type, urlsplit_host(file_url))
    response = Response(
        stream_with_context(iter_limited(upstream, MAX_DOWNLOAD_BYTES)),
        headers=headers,
    )
    # Guarantees the upstream socket is released even if the client disconnects
    # before the generator is iterated.
    response.call_on_close(upstream.close)
    return response


def urlsplit_host(url: str) -> str:
    """Return a log-safe host for a URL without leaking signed query strings."""

    from urllib.parse import urlsplit

    return urlsplit(url).hostname or "unknown"


@app.get("/download_mp3")
def download_mp3() -> Response | tuple[Response, int]:
    limited = _rate_limit("audio", AUDIO_RATE_LIMIT, AUDIO_RATE_WINDOW)
    if limited:
        return limited

    source_url = request.args.get("url", "").strip()
    if not source_url:
        return _json_error("A source URL is required.", 400, "missing_url")

    ffmpeg_path = _mp3_available()
    if not ffmpeg_path:
        return _json_error(
            "MP3 conversion is unavailable because ffmpeg is not installed on the server.",
            503,
            "ffmpeg_unavailable",
        )

    try:
        source_url = resolve_source_url(source_url)
    except UnsafeURLError as exc:
        return _json_error(str(exc), 400, "unsafe_url")

    temp_dir = tempfile.mkdtemp(prefix="umd-audio-")
    output_template = os.path.join(temp_dir, "audio.%(ext)s")

    ydl_options: dict[str, Any] = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_AUDIO_INPUT_BYTES,
        "prefer_ffmpeg": True,
        "ffmpeg_location": ffmpeg_path,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            ydl.download([source_url])
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info("MP3 extraction failed for %s: %s", urlsplit_host(source_url), exc)
        message = str(exc).lower()
        if "timed out" in message or "timeout" in message:
            return _json_error(
                "The source took too long to respond.", 504, "source_timeout"
            )
        return _json_error(
            "Could not extract audio from this link. The post may be private or unsupported.",
            422,
            "audio_extraction_failed",
        )
    except (TimeoutError, OSError):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return _json_error(
            "The source took too long to respond.", 504, "source_timeout"
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("Unexpected MP3 extraction error")
        return _json_error(
            "Audio extraction failed unexpectedly.", 500, "internal_error"
        )

    mp3_files = sorted(Path(temp_dir).glob("*.mp3"))
    if not mp3_files:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return _json_error(
            "The extractor did not produce an MP3 file.", 422, "audio_extraction_failed"
        )

    mp3_path = mp3_files[0]
    size = mp3_path.stat().st_size
    if size <= 0 or size > MAX_AUDIO_INPUT_BYTES:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return _json_error(
            "The generated audio file is empty or too large.",
            413,
            "audio_too_large",
        )

    filename = f"ultimate-media-audio-{uuid.uuid4().hex[:10]}.mp3"

    def generate_audio():
        try:
            with open(mp3_path, "rb") as audio_file:
                while chunk := audio_file.read(64 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info("Streaming MP3 extracted from %s", urlsplit_host(source_url))
    response = Response(
        stream_with_context(generate_audio()),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "audio/mpeg",
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
    # The generator also cleans up; this protects against clients that close
    # before the response body is consumed.
    response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
    return response


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException) -> Response | tuple[Response, int]:
    status = error.code or 500
    if _wants_json():
        return _json_error(
            error.description or "Request failed.",
            status,
            (error.name or "http_error").lower().replace(" ", "_"),
        )
    return (
        render_template(
            "error.html",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            code=status,
            title=error.name or "Error",
            message=error.description or "Something went wrong.",
        ),
        status,
    )


@app.errorhandler(ResponseTooLargeError)
def handle_too_large(_error: ResponseTooLargeError) -> tuple[Response, int]:
    return _json_error("The upstream response is too large.", 413, "response_too_large")


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception) -> Response | tuple[Response, int]:
    logger.exception("Unhandled server error: %s", error.__class__.__name__)
    if _wants_json():
        return _json_error(
            "An unexpected error occurred. The server is still running.",
            500,
            "internal_error",
        )
    return (
        render_template(
            "error.html",
            app_name=APP_NAME,
            app_version=APP_VERSION,
            code=500,
            title="Something went wrong",
            message="The server recovered, but this request could not be completed.",
        ),
        500,
    )


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = _env_int("PORT", 5000)
    debug = _env_bool("FLASK_DEBUG", False)

    if debug and host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning(
            "Debug mode is enabled while listening on %s. Do not expose this to the internet.",
            host,
        )

    logger.info("Starting %s v%s on http://%s:%s", APP_NAME, APP_VERSION, host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)
