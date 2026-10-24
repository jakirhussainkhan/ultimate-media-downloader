"""HTTP-level smoke tests for Safety and validation behaviour.

These tests never make outbound network requests: every case is rejected during
input validation.
"""

from __future__ import annotations

import unittest

from app import app


class AppSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_index_renders_brand_and_version(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<title>Ultimate Media Downloader", html)
        self.assertIn("v1.0.0", html)
        self.assertIn("Extract media", html)

    def test_security_headers_are_present(self) -> None:
        response = self.client.get("/")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertTrue(response.headers["X-Request-ID"])

    def test_health_and_version_endpoints(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        payload = health.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], "1.0.0")
        self.assertIn("ffmpeg", payload)

        version = self.client.get("/api/version")
        self.assertEqual(version.status_code, 200)
        self.assertEqual(version.get_json()["name"], "Ultimate Media Downloader")

    def test_fetch_requires_json(self) -> None:
        response = self.client.post("/fetch", data="url=https://example.com")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.get_json()["code"], "unsupported_media_type")

    def test_fetch_requires_a_url(self) -> None:
        response = self.client.post("/fetch", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "missing_url")

    def test_fetch_blocks_private_and_metadata_urls(self) -> None:
        for url in (
            "http://127.0.0.1:5000/private",
            "http://192.168.1.10/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost/",
        ):
            response = self.client.post("/fetch", json={"url": url})
            self.assertEqual(response.status_code, 400, msg=url)
            self.assertEqual(response.get_json()["code"], "unsafe_url", msg=url)

    def test_fetch_blocks_non_http_schemes(self) -> None:
        response = self.client.post("/fetch", json={"url": "file:///etc/passwd"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "unsafe_url")

    def test_download_blocks_private_and_metadata_urls(self) -> None:
        for url in (
            "http://127.0.0.1/private.mp4",
            "http://10.0.0.1/private.mp4",
            "http://169.254.169.254/latest/meta-data/",
        ):
            response = self.client.get("/download", query_string={"url": url, "type": "video"})
            self.assertEqual(response.status_code, 400, msg=url)
            self.assertEqual(response.get_json()["code"], "unsafe_url", msg=url)

    def test_download_requires_url(self) -> None:
        response = self.client.get("/download")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "missing_url")

    def test_json_404_for_api_paths(self) -> None:
        response = self.client.get("/api/does-not-exist", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "not_found")

    def test_html_404_uses_branded_error_page(self) -> None:
        response = self.client.get("/not-a-real-page")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Back to downloader", response.get_data(as_text=True))

    def test_oversized_json_is_rejected(self) -> None:
        response = self.client.post(
            "/fetch",
            json={"url": "https://example.com/" + ("a" * 20000)},
        )
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
