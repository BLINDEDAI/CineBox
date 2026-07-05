"""Tests for the response-compression-and-caching feature (Platform aggregate).

Boots the *real* ``server.Handler`` over an ephemeral-port ``ThreadingHTTPServer``
(same construction as ``tests/test_security_headers.py`` /
``tests/test_static_serving_allowlist.py``) and drives it over real HTTP —
compression/caching header behaviour is DB-independent, so no ``DATABASE_URL`` /
Postgres / ``init_pool`` / ``init_db`` is needed. The one exception (AC-7's
"authenticated 200" leg) monkeypatches the specific handler method that would
otherwise need a DB round-trip, so the `_json` choke-point header logic under
test still runs unmodified and unmocked.

Headers are read case-insensitively (``http.client.HTTPMessage`` /
``email.message.Message`` already does this via ``.get()``).

Covers:
  Unit — _gzip_eligible / _client_accepts_gzip / _cache_control_for
  AC-1  — gzip round-trip on app.js when client offers gzip
  AC-2  — no gzip without Accept-Encoding
  AC-3  — png/jpeg/webp never gzipped
  AC-4  — Content-Encoding + Vary + correct compressed Content-Length
  AC-5  — static Cache-Control public/max-age=300/must-revalidate, never immutable
  AC-6  — HTML Cache-Control: no-cache
  AC-7  — /api/* no-store on an authenticated 200 path AND an unauthenticated 401 path
  AC-8  — boot smoke
  AC-9  — non-allow-listed path -> generic 404 (allow-list unchanged)
  AC-10 — HSTS/CSP/X-Frame-Options/X-Content-Type-Options/Referrer-Policy unchanged
  AC-11 — HEAD/GET header parity (compressed Content-Length, empty HEAD body)
  AC-12 — If-Modified-Since -> 304, no body, no Content-Encoding, Cache-Control kept
  AC-13 — no Accept-Encoding -> uncompressed yet still carries Vary: Accept-Encoding
"""

import functools
import gzip
import http.server
import re
import socket
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

import server

BASE_DIR = Path(__file__).resolve().parent.parent

_HOST = "127.0.0.1"
_EPHEMERAL_PORT = 0
_STARTUP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05


def _wait_until_accepting(host, port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=_POLL_INTERVAL_SECONDS):
                return
        except OSError:
            time.sleep(_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"Cinephora test server did not accept a connection on {host}:{port} "
        f"within {timeout}s"
    )


# ── Unit tests: _gzip_eligible / _client_accepts_gzip / _cache_control_for ──
#
# These call the Handler's methods directly on an unbound-style stub instance
# (no socket, no request) since the three helpers only touch `self.headers`
# (for _client_accepts_gzip) or their argument (the other two) — mirroring how
# BaseHTTPRequestHandler subclasses are commonly unit-tested without a real
# connection: bypass __init__ via __new__ and set only what's read.


class _HandlerHeaderStub:
    """Minimal self.headers stand-in — an email.message.Message-like object
    supporting .get(name, default)."""

    def __init__(self, headers):
        self._headers = headers

    def get(self, name, default=""):
        return self._headers.get(name, default)


def _make_bare_handler(accept_encoding=None):
    """Builds a server.Handler instance without running __init__ (which would
    try to parse an HTTP request off a real socket). Only `self.headers` is
    set, since that's the only instance state _gzip_eligible/_client_accepts_gzip/
    _cache_control_for read."""
    handler = server.Handler.__new__(server.Handler)
    headers = {}
    if accept_encoding is not None:
        headers["Accept-Encoding"] = accept_encoding
    handler.headers = _HandlerHeaderStub(headers)
    return handler


class GzipEligibleUnitTests(unittest.TestCase):
    def setUp(self):
        self.handler = _make_bare_handler()

    def test_true_for_text_html(self):
        self.assertTrue(self.handler._gzip_eligible("text/html"))

    def test_true_for_text_html_with_charset_suffix(self):
        self.assertTrue(self.handler._gzip_eligible("text/html; charset=utf-8"))

    def test_true_for_text_css(self):
        self.assertTrue(self.handler._gzip_eligible("text/css"))

    def test_true_for_application_javascript(self):
        self.assertTrue(self.handler._gzip_eligible("application/javascript"))

    def test_true_for_text_javascript(self):
        # Python >= 3.11's mimetypes returns text/javascript for .js; both
        # spellings MUST be eligible or .js silently ships uncompressed.
        self.assertTrue(self.handler._gzip_eligible("text/javascript"))

    def test_true_for_application_json(self):
        self.assertTrue(self.handler._gzip_eligible("application/json"))

    def test_true_for_image_svg_xml(self):
        self.assertTrue(self.handler._gzip_eligible("image/svg+xml"))

    def test_false_for_image_png(self):
        self.assertFalse(self.handler._gzip_eligible("image/png"))

    def test_false_for_image_jpeg(self):
        self.assertFalse(self.handler._gzip_eligible("image/jpeg"))

    def test_false_for_image_webp(self):
        self.assertFalse(self.handler._gzip_eligible("image/webp"))

    def test_false_for_none(self):
        self.assertFalse(self.handler._gzip_eligible(None))


class ClientAcceptsGzipUnitTests(unittest.TestCase):
    def test_true_for_plain_gzip(self):
        handler = _make_bare_handler(accept_encoding="gzip")
        self.assertTrue(handler._client_accepts_gzip())

    def test_true_for_gzip_deflate(self):
        handler = _make_bare_handler(accept_encoding="gzip, deflate")
        self.assertTrue(handler._client_accepts_gzip())

    def test_false_for_absent_header(self):
        handler = _make_bare_handler(accept_encoding=None)
        self.assertFalse(handler._client_accepts_gzip())

    def test_false_for_gzip_q0(self):
        # Explicit refusal: a naive substring check would wrongly compress.
        handler = _make_bare_handler(accept_encoding="gzip;q=0")
        self.assertFalse(handler._client_accepts_gzip())

    def test_false_for_empty_header(self):
        handler = _make_bare_handler(accept_encoding="")
        self.assertFalse(handler._client_accepts_gzip())


class CacheControlForUnitTests(unittest.TestCase):
    def setUp(self):
        self.handler = _make_bare_handler()

    def test_no_cache_for_text_html(self):
        self.assertEqual(self.handler._cache_control_for("text/html"), "no-cache")

    def test_no_cache_for_text_html_with_charset(self):
        self.assertEqual(
            self.handler._cache_control_for("text/html; charset=utf-8"), "no-cache"
        )

    def test_public_max_age_for_css(self):
        result = self.handler._cache_control_for("text/css")
        self.assertEqual(result, "public, max-age=300, must-revalidate")

    def test_public_max_age_for_javascript(self):
        result = self.handler._cache_control_for("application/javascript")
        self.assertEqual(result, "public, max-age=300, must-revalidate")

    def test_public_max_age_for_image_png(self):
        result = self.handler._cache_control_for("image/png")
        self.assertEqual(result, "public, max-age=300, must-revalidate")

    def test_never_emits_immutable(self):
        for ctype in ("text/html", "text/css", "application/javascript", "image/png"):
            with self.subTest(ctype=ctype):
                self.assertNotIn("immutable", self.handler._cache_control_for(ctype))


# ── Integration: booted Handler ──────────────────────────────────────────────


class CompressionCachingServerTestCase(unittest.TestCase):
    """Boots one real server.Handler instance for the whole class — same
    construction as test_static_serving_allowlist.py / test_security_headers.py."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(server.Handler, directory=str(server.BASE_DIR))
        cls.httpd = http.server.ThreadingHTTPServer((_HOST, _EPHEMERAL_PORT), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        _wait_until_accepting(_HOST, cls.port, _STARTUP_TIMEOUT_SECONDS)
        cls.base_url = f"http://{_HOST}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=_STARTUP_TIMEOUT_SECONDS)

    def _request(self, path, method="GET", headers=None):
        req = urllib.request.Request(
            self.base_url + path, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _get(self, path, headers=None):
        return self._request(path, method="GET", headers=headers)

    def _head(self, path, headers=None):
        return self._request(path, method="HEAD", headers=headers)


# ── AC-1 / AC-2: gzip round-trip on app.js, gated on Accept-Encoding ─────────


class GzipRoundTripTests(CompressionCachingServerTestCase):
    def test_app_js_gzip_round_trip_with_accept_encoding(self):
        status, headers, body = self._get("/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        original = (BASE_DIR / "app.js").read_bytes()
        # Decode-compare, not exact bytes: gzip.compress(mtime=0) is deterministic
        # but the robust assertion is the lossless round-trip itself.
        self.assertEqual(gzip.decompress(body), original)

    def test_app_js_uncompressed_without_accept_encoding(self):
        status, headers, body = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIsNone(
            headers.get("Content-Encoding"),
            "Content-Encoding must be absent when the client did not offer gzip",
        )
        original = (BASE_DIR / "app.js").read_bytes()
        self.assertEqual(body, original)


# ── AC-3: already-compressed binaries are never gzipped ──────────────────────


class BinaryNeverGzippedTests(CompressionCachingServerTestCase):
    def test_png_asset_not_gzipped_even_when_client_offers_gzip(self):
        status, headers, body = self._get(
            "/assets/Guts.png", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers.get("Content-Type", ""))
        self.assertIsNone(
            headers.get("Content-Encoding"),
            "a .png must never be gzipped, even when the client offers gzip",
        )
        # Sanity: the bytes served are the exact original file, not a gzip stream.
        original = (BASE_DIR / "assets" / "Guts.png").read_bytes()
        self.assertEqual(body, original)


# ── AC-4: gzip headers — Content-Encoding + Vary + correct Content-Length ───


class GzipResponseHeadersTests(CompressionCachingServerTestCase):
    def test_gzip_response_carries_encoding_vary_and_correct_length(self):
        status, headers, body = self._get("/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        declared_length = headers.get("Content-Length")
        self.assertIsNotNone(declared_length)
        self.assertEqual(int(declared_length), len(body))
        # Regression guard: the compressed length must differ from (be smaller
        # than) the uncompressed original — otherwise a stale/precompression
        # Content-Length bug would still pass a naive "matches body" check.
        original = (BASE_DIR / "app.js").read_bytes()
        self.assertLess(len(body), len(original))


# ── AC-5: static assets carry public/max-age=300/must-revalidate, never immutable ─


class StaticCacheControlTests(CompressionCachingServerTestCase):
    def test_js_asset_cache_control(self):
        _, headers, _ = self._get("/app.js")
        cache_control = headers.get("Cache-Control", "")
        self.assertEqual(cache_control, "public, max-age=300, must-revalidate")
        self.assertNotIn("immutable", cache_control)

    def test_css_asset_cache_control(self):
        _, headers, _ = self._get("/styles.css")
        cache_control = headers.get("Cache-Control", "")
        self.assertEqual(cache_control, "public, max-age=300, must-revalidate")
        self.assertNotIn("immutable", cache_control)

    def test_vendor_asset_cache_control(self):
        _, headers, _ = self._get("/vendor/supabase-js/2.108.1/supabase.min.js")
        cache_control = headers.get("Cache-Control", "")
        self.assertEqual(cache_control, "public, max-age=300, must-revalidate")
        self.assertNotIn("immutable", cache_control)

    def test_assets_image_cache_control(self):
        _, headers, _ = self._get("/assets/Guts.png")
        cache_control = headers.get("Cache-Control", "")
        self.assertEqual(cache_control, "public, max-age=300, must-revalidate")
        self.assertNotIn("immutable", cache_control)


# ── AC-6: HTML documents carry Cache-Control: no-cache ───────────────────────


class HtmlCacheControlTests(CompressionCachingServerTestCase):
    def test_index_html_no_cache(self):
        _, headers, _ = self._get("/index.html")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")

    def test_root_no_cache(self):
        _, headers, _ = self._get("/")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")

    def test_public_html_no_cache(self):
        _, headers, _ = self._get("/public.html")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")

    def test_privacy_clean_url_no_cache(self):
        _, headers, _ = self._get("/privacy")
        self.assertEqual(headers.get("Cache-Control"), "no-cache")


# ── AC-7: every /api/* response is no-store — authenticated 200 + unauth 401 ─


class ApiNoStoreTests(CompressionCachingServerTestCase):
    def _assert_no_store_only(self, cache_control, label):
        self.assertIsNotNone(cache_control, f"{label}: Cache-Control missing")
        self.assertIn("no-store", cache_control)
        self.assertNotIn("public", cache_control)
        self.assertNotIn("max-age", cache_control)

    def test_unauthenticated_api_401_is_no_store(self):
        # No Authorization header -> _get_user_id() returns None -> real 401
        # path through _json, no DB touched (server.py:942-945).
        status, headers, _ = self._get("/api/movies")
        self.assertEqual(status, 401)
        self._assert_no_store_only(headers.get("Cache-Control"), "/api/movies (401)")

    def test_authenticated_api_200_is_no_store(self):
        # Simulates the authenticated-200 leg without a DB: patches the one
        # handler method that would otherwise need a DB round-trip so the
        # _json() choke-point header logic under test (BR-7/AS-028) runs
        # unmodified and unmocked. If Cache-Control: no-store were removed
        # from _json(), this assertion fails.
        with unittest.mock.patch.object(
            server.Handler,
            "_list_movies",
            lambda self: self._json(200, {"ok": True, "movies": []}),
        ):
            status, headers, _ = self._get(
                "/api/movies", headers={"Authorization": "Bearer fake-token-for-header-test"}
            )
        self.assertEqual(status, 200)
        self._assert_no_store_only(headers.get("Cache-Control"), "/api/movies (200, authenticated)")

    def test_health_json_response_is_no_store(self):
        # Belt-and-suspenders: every _json response, not just /api/*, is no-store.
        status, headers, _ = self._get("/health")
        self.assertEqual(status, 200)
        self._assert_no_store_only(headers.get("Cache-Control"), "/health")


# ── AC-8: boot smoke ──────────────────────────────────────────────────────────


class BootSmokeTests(CompressionCachingServerTestCase):
    def test_server_serves_a_request_after_the_change(self):
        status, _, _ = self._get("/health")
        self.assertEqual(status, 200)


# ── AC-9: allow-list unchanged — non-allow-listed path still 404s ───────────


class AllowlistUnchangedTests(CompressionCachingServerTestCase):
    def test_non_allowlisted_path_returns_generic_404(self):
        status, headers, body = self._get("/this-path-does-not-exist-anywhere.xyz")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers.get("Content-Type", ""))


# ── AC-10: security headers unchanged (HSTS/CSP/XFO/XCTO/Referrer-Policy) ───


class SecurityHeaderParityTests(CompressionCachingServerTestCase):
    def test_security_headers_present_and_correct_with_forwarded_https(self):
        status, headers, _ = self._get("/health", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)

        hsts = headers.get("Strict-Transport-Security")
        self.assertIsNotNone(hsts, "HSTS missing with X-Forwarded-Proto: https")
        self.assertIn("includeSubDomains", hsts)
        self.assertIn("preload", hsts)
        match = re.search(r"max-age=(\d+)", hsts)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 63072000)

        csp = headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("cdn.jsdelivr.net", csp)

        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_hsts_still_absent_without_forwarded_proto(self):
        # Regression guard: this feature must not have changed the HSTS gate.
        status, headers, _ = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Strict-Transport-Security"))
        self.assertIsNotNone(headers.get("Content-Security-Policy"))


# ── AC-11: HEAD/GET header parity on a gzip-eligible static asset ──────────


class HeadGetGzipParityTests(CompressionCachingServerTestCase):
    def test_head_matches_get_content_encoding_length_and_cache_control(self):
        get_status, get_headers, get_body = self._get(
            "/app.js", headers={"Accept-Encoding": "gzip"}
        )
        head_status, head_headers, head_body = self._head(
            "/app.js", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"", "HEAD must carry no body")

        # Sanity: the GET leg actually compressed (positive branch of the parity check).
        self.assertEqual(get_headers.get("Content-Encoding"), "gzip")

        self.assertEqual(head_headers.get("Content-Encoding"), get_headers.get("Content-Encoding"))
        self.assertEqual(head_headers.get("Content-Length"), get_headers.get("Content-Length"))
        self.assertEqual(head_headers.get("Cache-Control"), get_headers.get("Cache-Control"))

        # Framing-mismatch guard: the HEAD Content-Length must equal the
        # compressed length, never the uncompressed original's length.
        original = (BASE_DIR / "app.js").read_bytes()
        self.assertNotEqual(int(head_headers.get("Content-Length")), len(original))
        self.assertEqual(int(head_headers.get("Content-Length")), len(get_body))


# ── AC-12: If-Modified-Since -> 304, no body, no Content-Encoding, Cache-Control kept ─


class ConditionalRequestTests(CompressionCachingServerTestCase):
    def test_matching_if_modified_since_returns_304(self):
        # First fetch to obtain a real Last-Modified value from the server.
        first_status, first_headers, _ = self._get("/app.js")
        self.assertEqual(first_status, 200)
        last_modified = first_headers.get("Last-Modified")
        self.assertIsNotNone(last_modified)

        status, headers, body = self._get(
            "/app.js", headers={"If-Modified-Since": last_modified}
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"", "304 must carry no body")
        self.assertIsNone(
            headers.get("Content-Encoding"),
            "304 must never carry Content-Encoding (no entity body to encode)",
        )
        self.assertEqual(
            headers.get("Cache-Control"), "public, max-age=300, must-revalidate"
        )

    def test_matching_if_modified_since_with_gzip_offered_still_304s_no_encoding(self):
        first_status, first_headers, _ = self._get("/app.js")
        last_modified = first_headers.get("Last-Modified")

        status, headers, body = self._get(
            "/app.js",
            headers={"If-Modified-Since": last_modified, "Accept-Encoding": "gzip"},
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(
            headers.get("Cache-Control"), "public, max-age=300, must-revalidate"
        )


# ── AC-13: no Accept-Encoding -> uncompressed, still carries Vary ───────────


class VaryAlwaysOnStaticTests(CompressionCachingServerTestCase):
    def test_static_asset_without_accept_encoding_still_carries_vary(self):
        status, headers, _ = self._get("/app.js")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")


if __name__ == "__main__":
    unittest.main()
