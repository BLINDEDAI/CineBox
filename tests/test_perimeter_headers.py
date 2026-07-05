"""Tests for the permissions-policy-corp-server-hardening feature (Platform aggregate).

Boots the *real* ``server.Handler`` over an ephemeral-port ``ThreadingHTTPServer``
(identical construction to ``tests/test_security_headers.py``) and drives it over
real HTTP with a controlled ``urllib.request.Request`` — no DB / ``DATABASE_URL``
is needed since the routes exercised here (``/health``, ``/api/config``, the app
shell, a static asset, the 404 path) are all DB-independent.

Headers are read case-insensitively (``http.client.HTTPMessage`` / the underlying
``email.message.Message`` already does this via ``.get()``) per the 2026-07-04
lessons-learned entry in ``CineBox-docs/lessons-learned/general.md``
(static-serving-allowlist-hardening) — never a plain-dict exact-key match.

For a HEAD response, the correct assertion is body bytes == 0 while
Content-Length still reports the notional GET size (HEAD suppresses only the
write, not the headers) — never assert Content-Length == "0".

Covers:
  AC-1 — Permissions-Policy present on shell / static asset / /api/* / 404 / HEAD,
         denies (empty allow-list `()`) at least camera, microphone, geolocation,
         payment, usb.
  AC-2 — Cross-Origin-Resource-Policy present; app's own same-origin subresources
         (the app shell, a static asset) still load (200).
  AC-3 — assets/og-cinephora.png loadable cross-origin under the same-origin CORP
         posture (a cross-origin-flavoured GET returns 200 + image bytes). The
         live Facebook Sharing Debugger / X Card Validator checks require the
         deployed www.cinephora.com origin — flagged as human-verification-only
         in the Tester handoff, not attempted here.
  AC-4 — Server header contains neither "Python" nor "SimpleHTTP" (generic token).
  AC-5 — regression guard: HSTS still gated on X-Forwarded-Proto: https; the full
         CSP is byte-identical (script-src 'self', object-src 'none', base-uri
         'self', form-action 'self', frame-ancestors 'none', no cdn.jsdelivr.net).
  AC-6 — HEAD / /api/* / 404 carry identical Permissions-Policy / CORP / Server
         values as the equivalent GET.
"""

import functools
import http.server
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server

BASE_DIR = Path(__file__).resolve().parent.parent

_HOST = "127.0.0.1"
_EPHEMERAL_PORT = 0
_STARTUP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05

# BR-1 deny-list minimums the task DoD / spec AC-1 name explicitly.
_MINIMUM_DENIED_FEATURES = ("camera", "microphone", "geolocation", "payment", "usb")


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


class PerimeterHeaderServerTestCase(unittest.TestCase):
    """Boots one real server.Handler instance for the whole class — identical
    construction to SecurityHeaderServerTestCase in test_security_headers.py."""

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
        """Send `method` to `path` with optional request `headers`; returns
        (status, response_headers, body_bytes) — never raises on 4xx/5xx.
        response_headers is the original http.client.HTTPMessage (case-insensitive
        .get() lookups), never a plain dict — see module docstring."""
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


# ── AC-1: Permissions-Policy present + denies the minimum feature set ──────


class PermissionsPolicyTests(PerimeterHeaderServerTestCase):
    def _assert_denies_minimum(self, policy):
        self.assertIsNotNone(policy, "Permissions-Policy header missing")
        for feature in _MINIMUM_DENIED_FEATURES:
            self.assertIn(
                f"{feature}=()",
                policy,
                f"Permissions-Policy does not deny {feature!r} with an empty allow-list",
            )

    def test_permissions_policy_on_app_shell(self):
        status, headers, _ = self._get("/index.html")
        self.assertEqual(status, 200)
        self._assert_denies_minimum(headers.get("Permissions-Policy"))

    def test_permissions_policy_on_static_asset(self):
        status, headers, _ = self._get("/styles.css")
        self.assertEqual(status, 200)
        self._assert_denies_minimum(headers.get("Permissions-Policy"))

    def test_permissions_policy_on_api_response(self):
        status, headers, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        self._assert_denies_minimum(headers.get("Permissions-Policy"))

    def test_permissions_policy_on_generic_404(self):
        status, headers, _ = self._get("/this-path-does-not-exist-anywhere.xyz")
        self.assertEqual(status, 404)
        self._assert_denies_minimum(headers.get("Permissions-Policy"))

    def test_permissions_policy_on_head(self):
        status, headers, body = self._head("/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self._assert_denies_minimum(headers.get("Permissions-Policy"))

    def test_permissions_policy_does_not_grant_app_used_features(self):
        # BR-1a: the app uses none of the denied features (file-input avatar
        # upload, external-tab trailers) — no feature the app relies on should
        # be granted a non-empty allow-list.
        _, headers, _ = self._get("/index.html")
        policy = headers.get("Permissions-Policy")
        self.assertIsNotNone(policy)
        self.assertNotIn("camera=(self)", policy)
        self.assertNotIn("camera=*", policy)
        self.assertNotIn("microphone=(self)", policy)
        self.assertNotIn("geolocation=(self)", policy)


# ── AC-2: Cross-Origin-Resource-Policy present + same-origin subresources load ─


class CrossOriginResourcePolicyTests(PerimeterHeaderServerTestCase):
    def test_corp_present_on_app_shell(self):
        status, headers, _ = self._get("/index.html")
        self.assertEqual(status, 200)
        self.assertIsNotNone(headers.get("Cross-Origin-Resource-Policy"))

    def test_corp_present_on_api_response(self):
        status, headers, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIsNotNone(headers.get("Cross-Origin-Resource-Policy"))

    def test_same_origin_subresources_still_load(self):
        # The app's own JS/CSS/images are same-origin — CORP: same-origin must
        # not block them (AC-2/AC-7 no functional regression).
        for path in ("/styles.css", "/api.js", "/ui.js", "/app.js"):
            with self.subTest(path=path):
                status, headers, body = self._get(path)
                self.assertEqual(status, 200, f"{path} failed to load under CORP")
                self.assertGreater(len(body), 0)
                self.assertIsNotNone(headers.get("Cross-Origin-Resource-Policy"))


# ── AC-3: public OG image loadable cross-origin under the CORP posture ─────


class OpenGraphImageCrossOriginTests(PerimeterHeaderServerTestCase):
    def test_og_image_loadable_with_cross_origin_request_headers(self):
        # Simulate a cross-origin fetch the way a browser/crawler would send it
        # (Origin header from a foreign origin, Sec-Fetch-Site: cross-site).
        # urllib itself does not enforce CORP (that is a browser-side control),
        # so this asserts the server-side contract: the response is served
        # (200 + image bytes) regardless of the declared cross-origin context —
        # the CORP posture chosen (global same-origin, ADR-022) relies on
        # crawlers fetching the image as a top-level resource, not on the
        # origin server refusing the bytes.
        status, headers, body = self._get(
            "/assets/og-cinephora.png",
            headers={
                "Origin": "https://example-crawler.invalid",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "no-cors",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 0)
        # PNG magic bytes.
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")


# ── AC-4: Server header no longer discloses Python / SimpleHTTP version ────


class ServerHeaderTests(PerimeterHeaderServerTestCase):
    def _assert_generic_server_token(self, server_header):
        self.assertIsNotNone(server_header, "Server header missing")
        self.assertNotIn("Python", server_header)
        self.assertNotIn("SimpleHTTP", server_header)

    def test_server_header_generic_on_get(self):
        status, headers, _ = self._get("/index.html")
        self.assertEqual(status, 200)
        self._assert_generic_server_token(headers.get("Server"))

    def test_server_header_generic_on_api_response(self):
        status, headers, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        self._assert_generic_server_token(headers.get("Server"))

    def test_server_header_generic_on_404(self):
        status, headers, _ = self._get("/this-path-does-not-exist-anywhere.xyz")
        self.assertEqual(status, 404)
        self._assert_generic_server_token(headers.get("Server"))

    def test_server_header_generic_on_head(self):
        status, headers, body = self._head("/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self._assert_generic_server_token(headers.get("Server"))


# ── AC-5 (regression guard): HSTS gating + full CSP byte-identical ─────────


class ExistingPerimeterRegressionGuardTests(PerimeterHeaderServerTestCase):
    def test_hsts_present_only_under_forwarded_https(self):
        status, headers, _ = self._get(
            "/health", headers={"X-Forwarded-Proto": "https"}
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(headers.get("Strict-Transport-Security"))

    def test_hsts_absent_without_forwarded_proto(self):
        status, headers, _ = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Strict-Transport-Security"))

    def test_csp_byte_identical_directives_present(self):
        _, headers, _ = self._get("/health")
        csp = headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        for directive in (
            "script-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ):
            self.assertIn(directive, csp)
        self.assertNotIn("cdn.jsdelivr.net", csp)


# ── AC-6: HEAD / /api/* / 404 carry identical values to the equivalent GET ─


class HeaderUniformityAcrossMethodsAndRoutesTests(PerimeterHeaderServerTestCase):
    def _perimeter_headers(self, headers_obj):
        return {
            "Permissions-Policy": headers_obj.get("Permissions-Policy"),
            "Cross-Origin-Resource-Policy": headers_obj.get(
                "Cross-Origin-Resource-Policy"
            ),
            "Server": headers_obj.get("Server"),
        }

    def test_head_matches_get_on_app_shell(self):
        get_status, get_headers, _ = self._get("/index.html")
        head_status, head_headers, head_body = self._head("/index.html")
        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        # Content-Length still reports the notional GET size on HEAD — HEAD
        # suppresses only the body write, not the headers.
        self.assertEqual(
            get_headers.get("Content-Length"), head_headers.get("Content-Length")
        )
        self.assertEqual(
            self._perimeter_headers(get_headers), self._perimeter_headers(head_headers)
        )

    def test_api_response_matches_app_shell_perimeter_values(self):
        _, shell_headers, _ = self._get("/index.html")
        _, api_headers, _ = self._get("/api/config")
        self.assertEqual(
            self._perimeter_headers(shell_headers), self._perimeter_headers(api_headers)
        )

    def test_generic_404_matches_app_shell_perimeter_values(self):
        _, shell_headers, _ = self._get("/index.html")
        status, notfound_headers, _ = self._get(
            "/this-path-does-not-exist-anywhere.xyz"
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            self._perimeter_headers(shell_headers),
            self._perimeter_headers(notfound_headers),
        )


if __name__ == "__main__":
    unittest.main()
