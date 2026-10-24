"""Media extraction for Ultimate Media Downloader.

This module is deliberately free of Flask concerns.  It turns a user-supplied
URL into a validated direct media URL that the UI can preview and the proxy
endpoints can download.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

import requests
import yt_dlp
from bs4 import BeautifulSoup

from security import (
    DEFAULT_TIMEOUT,
    UnsafeURLError,
    read_limited,
    safe_get,
    validate_public_url,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_HTML_BYTES = 4 * 1024 * 1024
CACHE_TTL_SECONDS = 120
CACHE_MAX_ENTRIES = 256

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "tiff"}
PLAYABLE_VIDEO_EXTENSIONS = {"mp4", "webm", "m4v", "mov"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"}

# Progressive (video+audio) formats first so the browser can preview natively.
# Each fallback broadens the search; the manual selector below still enforces
# the 720p ceiling when such a format exists.
FORMAT_SELECTOR = (
    "best[height<=720][ext=mp4][vcodec!=none][acodec!=none]/"
    "best[height<=720][vcodec!=none][acodec!=none]/"
    "best[ext=mp4][vcodec!=none][acodec!=none]/"
    "best[vcodec!=none][acodec!=none]/"
    "best[vcodec!=none]/"
    "best"
)

_FORMAT_QUALITY_PATTERN = re.compile(r"^[a-zA-Z0-9]{1,6}$")
_URL_IN_TEXT_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_DOMAIN_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)


class ExtractionError(RuntimeError):
    """Base class for user-facing extraction failures."""


class UnsupportedMediaError(ExtractionError):
    """The link is valid but no downloadable media could be found."""


class SourceTimeoutError(ExtractionError):
    """The upstream source took too long to respond."""


class _TTLCache:
    """Small thread-safe TTL cache for extraction results."""

    def __init__(self, ttl: int = CACHE_TTL_SECONDS, max_entries: int = CACHE_MAX_ENTRIES):
        self._ttl = ttl
        self._max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return dict(value)

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, dict(value))
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)


_extraction_cache = _TTLCache()


def _clean_text(value: Any, *, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split())
    return cleaned[:limit]


def _safe_extension(value: Any, fallback: str = "bin") -> str:
    if not isinstance(value, str):
        return fallback
    value = value.lower().strip().lstrip(".")
    if _FORMAT_QUALITY_PATTERN.fullmatch(value):
        if value == "jpeg":
            return "jpg"
        return value
    return fallback


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def extract_url_from_text(raw_input: str) -> str:
    """Pull a usable URL from arbitrary pasted text."""

    if not isinstance(raw_input, str):
        raise UnsafeURLError("Please provide a URL.")

    raw_input = raw_input.strip()
    if not raw_input:
        raise UnsafeURLError("Please provide a URL.")

    match = _URL_IN_TEXT_PATTERN.search(raw_input)
    if match:
        candidate = match.group(0).rstrip(").,;:!?]}\"'")
    elif _BARE_DOMAIN_PATTERN.fullmatch(raw_input):
        candidate = f"https://{raw_input}"
    else:
        raise UnsafeURLError("Please paste a valid http:// or https:// link.")

    return validate_public_url(candidate)


def _is_pinterest_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in {"pin.it", "www.pin.it"} or host == "pinterest.com" or host.endswith(
        ".pinterest.com"
    )


def resolve_source_url(raw_input: str) -> str:
    """Normalise input and expand Pinterest short links safely."""

    url = extract_url_from_text(raw_input)
    host = (urlsplit(url).hostname or "").lower()

    if not _is_pinterest_host(host):
        return url

    try:
        response = safe_get(
            url,
            headers=BROWSER_HEADERS,
            timeout=DEFAULT_TIMEOUT,
            max_bytes=1024 * 1024,
            stream=True,
        )
        final_url = response.url
        response.close()
    except (requests.RequestException, UnsafeURLError) as exc:
        # The original URL is already validated; a short-link failure should not
        # make an otherwise valid Pinterest link unusable.
        logger.info("Pinterest short-link resolution failed: %s", exc.__class__.__name__)
        return url

    try:
        final_url = validate_public_url(final_url)
    except UnsafeURLError:
        return url

    # Drop tracking query strings, but keep the pin path intact.
    return final_url.split("?", 1)[0].split("#", 1)[0]


def _validate_media_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        raise UnsupportedMediaError("The source did not provide a downloadable media URL.")
    try:
        return validate_public_url(url)
    except UnsafeURLError as exc:
        logger.warning("Rejected an extracted media URL that failed SSRF checks.")
        raise UnsupportedMediaError(
            "The source returned an unsafe media URL, so it was blocked."
        ) from exc


def _extract_pinterest(url: str) -> dict[str, Any]:
    try:
        response = safe_get(
            url,
            headers=BROWSER_HEADERS,
            timeout=DEFAULT_TIMEOUT,
            max_bytes=MAX_HTML_BYTES,
            stream=True,
        )
        if response.status_code != 200:
            logger.info("Pinterest returned HTTP %s", response.status_code)
            raise UnsupportedMediaError(
                "Pinterest did not return a public page for this pin."
            )
        html = read_limited(response, MAX_HTML_BYTES).decode("utf-8", errors="replace")
    except requests.Timeout as exc:
        raise SourceTimeoutError("Pinterest took too long to respond.") from exc
    except requests.RequestException as exc:
        logger.info("Pinterest request failed: %s", exc.__class__.__name__)
        raise UnsupportedMediaError("Could not reach Pinterest for this pin.") from exc

    soup = BeautifulSoup(html, "html.parser")
    title = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "") or (
        "Pinterest media"
    )

    # 1) Schema.org metadata is the most reliable source when present.
    for script in soup.find_all("script", type="application/ld+json"):
        payload = script.string or script.get_text()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(str(entry).lower() in {"videoobject", "imageobject"} for entry in types):
                continue
            content_url = item.get("contentUrl") or item.get("url")
            if not isinstance(content_url, str):
                continue
            lowered = content_url.lower()
            if ".m3u8" in lowered or "hls" in lowered:
                # The download proxy streams files; HLS manifests need segment
                # concatenation, so skip them and fall through to other options.
                continue
            if ".mp4" in lowered or "videoobject" in {
                str(entry).lower() for entry in types
            }:
                return _build_result(
                    download_url=_validate_media_url(content_url),
                    source_url=url,
                    media_type="video",
                    ext=_safe_extension(urlsplit(content_url).path.rsplit(".", 1)[-1], "mp4"),
                    title=title,
                    platform="Pinterest",
                )

    # 2) Embedded MP4 URLs (skip HLS manifests, which the proxy cannot serve).
    raw_urls = re.findall(r'"(https:[^"]+?\.mp4[^"]*)"', html)
    video_urls = []
    for raw_url in raw_urls:
        candidate = raw_url.replace("\\/", "/")
        if "pinimg.com" in candidate and "hls" not in candidate.lower():
            video_urls.append(candidate)
    if video_urls:
        chosen = max(set(video_urls), key=len)
        return _build_result(
            download_url=_validate_media_url(chosen),
            source_url=url,
            media_type="video",
            ext="mp4",
            title=title,
            platform="Pinterest",
        )

    # 3) Open Graph video/image.
    og_video = soup.find("meta", property="og:video")
    if og_video and og_video.get("content"):
        return _build_result(
            download_url=_validate_media_url(og_video["content"]),
            source_url=url,
            media_type="video",
            ext="mp4",
            title=title,
            platform="Pinterest",
        )

    image_url = None
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        image_url = og_image["content"]
    if not image_url:
        for image in soup.find_all("img"):
            source = image.get("src", "")
            if "pinimg.com" in source:
                image_url = source
                break

    if image_url and "pinimg.com" in image_url:
        original_url = re.sub(
            r"/(?:120x|170x|200x|236x|300x|474x|564x|736x)/",
            "/originals/",
            image_url,
        )
        return _build_result(
            download_url=_validate_media_url(original_url),
            source_url=url,
            media_type="image",
            ext=_safe_extension(urlsplit(original_url).path.rsplit(".", 1)[-1], "jpg"),
            title=title,
            platform="Pinterest",
        )

    raise UnsupportedMediaError(
        "No public video or image was found on this Pinterest pin."
    )


def _first_playlist_entry(info: dict[str, Any]) -> dict[str, Any]:
    """Return the first usable entry from a playlist-shaped response."""

    current = info
    for _ in range(5):
        entries = current.get("entries")
        if not entries:
            return current
        first = next((entry for entry in entries if isinstance(entry, dict)), None)
        if first is None:
            break
        current = first
    return current


def _is_combined_format(fmt: dict[str, Any]) -> bool:
    return (
        bool(fmt.get("url"))
        and fmt.get("vcodec") not in (None, "none")
        and fmt.get("acodec") not in (None, "none")
    )


def _pick_best_format(formats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose a progressive format with a 720p preference and safe fallbacks."""

    combined = [fmt for fmt in formats if _is_combined_format(fmt)]
    if not combined:
        return None

    playable = [
        fmt
        for fmt in combined
        if _safe_extension(fmt.get("ext"), "") in PLAYABLE_VIDEO_EXTENSIONS
    ]
    pool = playable or combined

    capped = [
        fmt
        for fmt in pool
        if 0 < _number(fmt.get("height")) <= 720
    ]
    if capped:
        pool = capped

    mp4_only = [
        fmt for fmt in pool if _safe_extension(fmt.get("ext"), "") == "mp4"
    ]
    if mp4_only:
        pool = mp4_only

    return max(
        pool,
        key=lambda fmt: (
            _number(fmt.get("height")),
            _number(fmt.get("fps")),
            _number(fmt.get("tbr")),
            _number(fmt.get("filesize") or fmt.get("filesize_approx")),
        ),
    )


def _platform_name(info: dict[str, Any], url: str) -> str:
    extractor = _clean_text(
        info.get("extractor_key") or info.get("extractor") or "", limit=40
    )
    aliases = {
        "youtube": "YouTube",
        "instagram": "Instagram",
        "facebook": "Facebook",
        "pinterest": "Pinterest",
        "tiktok": "TikTok",
        "twitter": "X / Twitter",
        "reddit": "Reddit",
        "vimeo": "Vimeo",
    }
    key = extractor.lower().replace(" ", "")
    for alias, label in aliases.items():
        if key.startswith(alias):
            return label
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return _clean_text(host, limit=40) or "Media"


def _build_result(
    *,
    download_url: str,
    source_url: str,
    media_type: str,
    ext: str,
    title: str,
    platform: str,
    height: int | None = None,
    duration: float | None = None,
    has_audio: bool = True,
) -> dict[str, Any]:
    if media_type == "image":
        quality = "Original image"
    elif media_type == "audio":
        quality = "Audio"
    elif height:
        quality = f"{height}p"
    else:
        quality = "Best available"

    return {
        "type": media_type,
        "download_url": download_url,
        "source_url": source_url,
        "title": title or "Untitled media",
        "platform": platform,
        "quality": quality,
        "format": ext.upper() if ext else "MEDIA",
        "duration": round(duration, 1) if duration and duration > 0 else None,
        "has_audio": bool(has_audio),
    }


def _extract_universal(url: str) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "playlist_items": "1",
        "format": FORMAT_SELECTOR,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
        "cachedir": False,
        "ignoreerrors": False,
        "http_headers": {"User-Agent": USER_AGENT},
    }
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        options["ffmpeg_location"] = ffmpeg_path

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        logger.info("yt-dlp could not extract %s: %s", url, exc)
        message = str(exc).lower()
        if "timed out" in message or "timeout" in message:
            raise SourceTimeoutError("The source took too long to respond.") from exc
        raise UnsupportedMediaError(
            "This link is not supported or the media is not publicly accessible."
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise SourceTimeoutError("The source took too long to respond.") from exc
    except Exception as exc:  # yt-dlp extractors can raise a wide range of errors.
        logger.exception("Unexpected yt-dlp failure for %s", url)
        raise ExtractionError("The extractor failed unexpectedly.") from exc

    if not isinstance(info, dict):
        raise UnsupportedMediaError("The source did not return any media information.")

    info = _first_playlist_entry(info)
    if not info:
        raise UnsupportedMediaError("No downloadable media was found at this link.")

    formats = [fmt for fmt in info.get("formats") or [] if isinstance(fmt, dict)]
    best = _pick_best_format(formats)

    if best is None:
        # Image posts, direct media URLs, and audio-only sources land here.
        direct_url = info.get("url")
        if not direct_url:
            audio_formats = [
                fmt
                for fmt in formats
                if fmt.get("url")
                and fmt.get("acodec") not in (None, "none")
                and fmt.get("vcodec") in (None, "none")
            ]
            if audio_formats:
                best = max(
                    audio_formats,
                    key=lambda fmt: _number(fmt.get("abr") or fmt.get("tbr")),
                )
                direct_url = best.get("url")

        if not direct_url:
            # Last resort: the highest-quality video-only stream.  It may not
            # have audio, but it is still a usable download.
            video_only = [
                fmt
                for fmt in formats
                if fmt.get("url") and fmt.get("vcodec") not in (None, "none")
            ]
            if video_only:
                best = max(
                    video_only,
                    key=lambda fmt: (
                        _number(fmt.get("height")),
                        _number(fmt.get("tbr")),
                    ),
                )
                direct_url = best.get("url")

        if not direct_url:
            raise UnsupportedMediaError("No downloadable media URL was found at this link.")

        selected = best or info
        ext = _safe_extension(selected.get("ext") or info.get("ext"), "mp4")
        if ext in IMAGE_EXTENSIONS:
            media_type = "image"
        elif ext in AUDIO_EXTENSIONS and selected.get("vcodec") in (None, "none"):
            media_type = "audio"
        else:
            media_type = "video"
        height = selected.get("height")
        # Direct media URLs often omit codec metadata. Treat a missing codec as
        # unknown (audio may exist) and only flag an explicit video-only stream.
        has_audio = selected.get("acodec") != "none"
    else:
        direct_url = best.get("url")
        ext = _safe_extension(best.get("ext"), "mp4")
        media_type = "video"
        height = best.get("height")
        has_audio = True

    media_url = _validate_media_url(direct_url)
    title = _clean_text(info.get("title"), limit=200) or "Untitled media"
    platform = _platform_name(info, url)

    return _build_result(
        download_url=media_url,
        source_url=url,
        media_type=media_type,
        ext=ext,
        title=title,
        platform=platform,
        height=int(height) if _number(height) > 0 else None,
        duration=_number(info.get("duration")) or None,
        has_audio=has_audio,
    )


def extract_media(raw_input: str) -> dict[str, Any]:
    """Extract media metadata from a raw user-supplied URL or pasted text.

    Results are cached briefly because users often re-fetch the same link while
    choosing between the video and MP3 download buttons.
    """

    url = resolve_source_url(raw_input)
    cached = _extraction_cache.get(url)
    if cached is not None:
        return cached

    host = (urlsplit(url).hostname or "").lower()
    if _is_pinterest_host(host):
        result = _extract_pinterest(url)
    else:
        result = _extract_universal(url)

    _extraction_cache.put(url, result)
    logger.info(
        "Extracted %s media from %s (%s)",
        result.get("type"),
        result.get("platform"),
        result.get("quality"),
    )
    return result
