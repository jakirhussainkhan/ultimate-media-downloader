"""Unit tests for the SSRF guard and rate limiter."""

from __future__ import annotations

import unittest

from security import RateLimiter, UnsafeURLError, validate_public_url


class ValidatePublicURLTests(unittest.TestCase):
    def assert_blocked(self, url: str) -> None:
        with self.assertRaises(UnsafeURLError, msg=f"Expected blocked URL: {url}"):
            validate_public_url(url, resolve=False)

    def assert_allowed(self, url: str) -> None:
        self.assertEqual(validate_public_url(url, resolve=False), url.strip())

    def test_loopback_and_private_addresses_are_blocked(self) -> None:
        for url in (
            "http://127.0.0.1/",
            "http://127.0.0.1:80/",
            "http://127.1.2.3/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://100.64.0.1/",
            "http://0.0.0.0/",
            "http://[::1]/",
            "http://[::]/",
            "http://[fd00::1]/",
            "http://[::ffff:127.0.0.1]/",
        ):
            self.assert_blocked(url)

    def test_cloud_metadata_addresses_are_blocked(self) -> None:
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://169.254.170.2/v2/credentials",
            "http://100.100.100.200/latest/meta-data/",
            "http://[fd00:ec2::254]/",
        ):
            self.assert_blocked(url)

    def test_internal_hostnames_are_blocked(self) -> None:
        for url in (
            "http://localhost/",
            "http://localhost:443/",
            "http://metadata.google.internal/",
            "http://instance-data/",
            "http://kubernetes.default.svc/",
            "http://router.local/",
            "http://service.internal/",
            "http://printer/",
        ):
            self.assert_blocked(url)

    def test_non_http_schemes_are_blocked(self) -> None:
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/file.mp4",
            "javascript:alert(1)",
            "data:text/html,hello",
            "gopher://example.com/",
        ):
            self.assert_blocked(url)

    def test_credentials_and_unusual_ports_are_blocked(self) -> None:
        self.assert_blocked("http://user:password@example.com/")
        self.assert_blocked("http://user@example.com/")
        self.assert_blocked("https://example.com:8443/")
        self.assert_blocked("http://example.com:22/")

    def test_control_characters_and_empty_values_are_blocked(self) -> None:
        self.assert_blocked("")
        self.assert_blocked("   ")
        self.assert_blocked("https://example.com/\nheader: value")
        self.assert_blocked("https://exa\x00mple.com/")

    def test_public_urls_are_allowed_without_dns(self) -> None:
        for url in (
            "https://example.com/",
            "https://www.pinterest.com/pin/123456789/",
            "http://example.com:80/path?q=1#fragment",
            "https://example.com:443/path",
            "https://8.8.8.8/resolve",
            "https://[2606:4700:4700::1111]/",
            "https://example.com./trailing-dot",
        ):
            self.assert_allowed(url)

    def test_dns_failure_is_rejected(self) -> None:
        with self.assertRaises(UnsafeURLError):
            validate_public_url("https://this-domain-should-not-exist-umd.invalid/")


class RateLimiterTests(unittest.TestCase):
    def test_sliding_window_limit(self) -> None:
        limiter = RateLimiter()
        self.assertTrue(limiter.allow("client", limit=2, window_seconds=60)[0])
        self.assertTrue(limiter.allow("client", limit=2, window_seconds=60)[0])
        allowed, retry_after = limiter.allow("client", limit=2, window_seconds=60)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)
        # A different key keeps its own budget.
        self.assertTrue(limiter.allow("other", limit=2, window_seconds=60)[0])


if __name__ == "__main__":
    unittest.main()
