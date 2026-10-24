"""Outbound-request safety: SSRF protection, redirect validation, and rate limiting.

The application accepts URLs from untrusted users and then asks the server to
fetch them.  Every outbound request must therefore pass through this module.

Defences implemented here
-------------------------
* Only ``http`` and ``https`` are accepted.
* Credentials embedded in URLs are rejected.
* Only ports 80 and 443 are accepted.
* Hostnames are resolved and *every* returned address must be globally routable.
  Loopback, private, link-local, reserved, multicast, and metadata addresses are
  rejected.
* Known internal/metadata hostnames and internal-only suffixes are rejected.
* Redirects are followed manually so every hop is re-validated.
* Response bodies can be capped in size and streamed with a hard byte limit.
* A small, thread-safe sliding-window rate limiter limits abusive clients.

Note: the resolved-IP validation and the subsequent socket connection are two
separate steps, so a hostile DNS server could in theory change its answer in
between (DNS rebinding).  This is a known residual risk for user-supplied URL
fetchers.  Redirects are re-validated, media URLs returned to the client are
re-validated, and the download proxy repeats validation immediately before it
opens the upstream connection.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import deque
from typing import Iterator
from urllib.parse import urljoin, urlsplit

import requests

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
MAX_URL_LENGTH = 4096
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = (5, 20)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hostnames that should never be reachable from a user-supplied URL.
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.google.internal.",
    "instance-data",
    "kubernetes",
    "kubernetes.default",
    "kubernetes.default.svc",
}

# Internal-only suffixes.  A single-label host is also rejected below.
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
    ".corp",
    ".intranet",
    ".test",
    ".invalid",
    ".example",
)

# Cloud metadata addresses, kept explicit for clarity even though ``is_global``
# already rejects most of them.
BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS
    ipaddress.ip_address("169.254.170.2"),  # ECS task metadata
    ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud metadata
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS (IPv6)
}


class UnsafeURLError(ValueError):
    """Raised when a URL fails SSRF validation."""


class ResponseTooLargeError(ValueError):
    """Raised when an upstream response exceeds its configured byte budget."""


def _normalise_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if not host:
        raise UnsafeURLError("The URL is missing a hostname.")
    if "%" in host:
        # IPv6 zone identifiers can address host-local interfaces.
        raise UnsafeURLError("Scoped IPv6 addresses are not allowed.")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeURLError("The URL hostname is not valid.") from exc


def _is_public_ip(raw_ip: str) -> bool:
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return False

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    if address in BLOCKED_IPS:
        return False

    # ``is_global`` rejects loopback, private, link-local, multicast, reserved,
    # unspecified, and shared address space in one check.
    return address.is_global


def _resolve_host(host: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise UnsafeURLError("The URL hostname could not be resolved.") from exc

    addresses = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            addresses.add(sockaddr[0].split("%", 1)[0])

    if not addresses:
        raise UnsafeURLError("The URL hostname could not be resolved.")
    return addresses


def validate_public_url(url: str, *, resolve: bool = True) -> str:
    """Validate *url* and return a stripped, normalised version.

    Raises :class:`UnsafeURLError` when the URL could be used for SSRF or is
    otherwise malformed.  ``resolve=False`` skips DNS resolution and is intended
    for unit tests and already-pinned values only.
    """

    if not isinstance(url, str):
        raise UnsafeURLError("The URL must be a string.")

    url = url.strip()
    if not url:
        raise UnsafeURLError("The URL is empty.")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError("The URL is too long.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise UnsafeURLError("The URL contains invalid control characters.")

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError("Only http:// and https:// URLs are supported.")
    if not parsed.netloc:
        raise UnsafeURLError("The URL is missing a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URLs containing credentials are not allowed.")

    if not parsed.hostname:
        raise UnsafeURLError("The URL is missing a hostname.")

    # Host-level checks come before the port check so that a local URL with a
    # development port reports the more important private-host reason.
    host = _normalise_host(parsed.hostname)
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError("Requests to local or internal hosts are blocked.")

    # Reject an IP literal immediately, before DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public_ip(str(literal)):
            raise UnsafeURLError(
                "Requests to local, private, or metadata addresses are blocked."
            )
    elif "." not in host:
        # Single-label names normally resolve through internal DNS/search
        # domains and are not valid public media hosts.
        raise UnsafeURLError("Requests to local or internal hosts are blocked.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("The URL contains an invalid port.") from exc

    if port is not None and port not in ALLOWED_PORTS:
        raise UnsafeURLError("Only standard web ports (80/443) are allowed.")

    if resolve:
        effective_port = port or (443 if scheme == "https" else 80)
        addresses = _resolve_host(host, effective_port)
        if any(not _is_public_ip(address) for address in addresses):
            raise UnsafeURLError(
                "Requests to local, private, or metadata addresses are blocked."
            )

    return url


def _check_content_length(response: requests.Response, max_bytes: int | None) -> None:
    if max_bytes is None:
        return
    raw_length = response.headers.get("Content-Length")
    if raw_length and raw_length.isdigit() and int(raw_length) > max_bytes:
        response.close()
        raise ResponseTooLargeError("The upstream response is too large.")


def safe_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    max_bytes: int | None = None,
    stream: bool = True,
) -> requests.Response:
    """Perform a GET request with SSRF-safe manual redirect handling.

    The returned response is the final hop.  The caller owns it and must close
    it (or consume it through :func:`iter_limited`).
    """

    request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)

    current = validate_public_url(url)

    for _ in range(MAX_REDIRECTS + 1):
        response = requests.get(
            current,
            headers=request_headers,
            timeout=timeout,
            allow_redirects=False,
            stream=stream,
        )

        if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException("Upstream returned an empty redirect.")
            current = urljoin(current, location)
            # Re-validate every hop; this is the important SSRF check.
            current = validate_public_url(current)
            continue

        _check_content_length(response, max_bytes)
        return response

    raise requests.TooManyRedirects("Upstream returned too many redirects.")


def read_limited(
    response: requests.Response,
    max_bytes: int,
    *,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a response body while enforcing a hard byte limit."""

    _check_content_length(response, max_bytes)
    buffer = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise ResponseTooLargeError("The upstream response is too large.")
    finally:
        response.close()
    return bytes(buffer)


def iter_limited(
    response: requests.Response,
    max_bytes: int,
    *,
    chunk_size: int = 64 * 1024,
) -> Iterator[bytes]:
    """Yield a response body, aborting and closing if it exceeds *max_bytes*."""

    sent = 0
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            sent += len(chunk)
            if sent > max_bytes:
                raise ResponseTooLargeError("The upstream response is too large.")
            yield chunk
    finally:
        response.close()


class RateLimiter:
    """Thread-safe in-memory sliding-window rate limiter."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for *key*."""

        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    # Drop the oldest bucket rather than growing without bound.
                    self._hits.pop(next(iter(self._hits)), None)
                hits = deque()
                self._hits[key] = hits

            while hits and hits[0] < cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_after = max(1, int(window_seconds - (now - hits[0])) + 1)
                return False, retry_after

            hits.append(now)
            return True, 0
