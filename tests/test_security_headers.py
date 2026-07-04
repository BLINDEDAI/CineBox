"""Tests for the hsts-and-csp-hardening feature (Platform aggregate).

Boots the *real* ``server.Handler`` over an ephemeral-port ``ThreadingHTTPServer``
(same construction as ``tests/test_static_serving_allowlist.py`` /
``tests/e2e/conftest.py``'s ``base_url`` fixture) and drives it over real HTTP with a
controlled ``urllib.request.Request`` so the test sets request headers itself
(``X-Forwarded-Proto``) — static/API serving here is DB-independent, so no
``DATABASE_URL`` / Postgres / ``init_pool`` / ``init_db`` is needed.

Headers are read case-insensitively (``http.client.HTTPMessage`` / ``email.message.Message``
already does this via ``.get()``) per the 2026-07-04 lessons-learned entry in
``CineBox-docs/lessons-learned/general.md`` (static-serving-allowlist-hardening).

Covers:
  AC-1 — X-Forwarded-Proto: https -> Strict-Transport-Security max-age>=63072000,
         includeSubDomains, preload present
  AC-7 — no X-Forwarded-Proto (absent, and value "http") -> NO Strict-Transport-Security;
         CSP still present (key regression guard: local/e2e never forced onto https://localhost)
  AC-2 — CSP contains object-src 'none', base-uri 'self', form-action 'self' regardless
         of X-Forwarded-Proto
  AC-3 — CSP still contains script-src 'self' and no external script origin
         (cdn.jsdelivr.net absent)
  AC-5 — HEAD carries the identical header set as GET for the same X-Forwarded-Proto
  AC-1/AC-2 uniformity — headers present on an /api/* JSON response and on the generic 404
  AC-6 — full unittest suite green (this file's own suite + the repo suite, run separately)
"""

import functools
import http.server
import re
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


class SecurityHeaderServerTestCase(unittest.TestCase):
    """Boots one real server.Handler instance for the whole class, same
    construction as StaticServerTestCase in test_static_serving_allowlist.py,
    but the request helpers here accept a `headers` dict so the test itself
    controls X-Forwarded-Proto on the outgoing request."""

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


# ── AC-1: positive path — X-Forwarded-Proto: https -> HSTS present ──────────


class HstsPositivePathTests(SecurityHeaderServerTestCase):
    def test_hsts_present_with_forwarded_https(self):
        status, headers, _ = self._get("/health", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        hsts = headers.get("Strict-Transport-Security")
        self.assertIsNotNone(hsts, "Strict-Transport-Security missing with X-Forwarded-Proto: https")
        self.assertIn("includeSubDomains", hsts)
        self.assertIn("preload", hsts)
        match = re.search(r"max-age=(\d+)", hsts)
        self.assertIsNotNone(match, f"no max-age directive found in {hsts!r}")
        self.assertGreaterEqual(int(match.group(1)), 63072000)

    def test_hsts_present_case_insensitive_https_value(self):
        # RFC 7230 scheme tokens are case-insensitive; server.py lowercases the
        # parsed value before comparing, so "HTTPS" must also gate HSTS on.
        status, headers, _ = self._get("/health", headers={"X-Forwarded-Proto": "HTTPS"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(headers.get("Strict-Transport-Security"))

    def test_hsts_present_on_first_hop_of_comma_separated_chain(self):
        status, headers, _ = self._get(
            "/health", headers={"X-Forwarded-Proto": "https, http"}
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(headers.get("Strict-Transport-Security"))


# ── AC-7: negative path (KEY regression guard) — no HSTS without XFP https ──


class HstsNegativePathTests(SecurityHeaderServerTestCase):
    def test_hsts_absent_when_header_missing(self):
        status, headers, _ = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIsNone(
            headers.get("Strict-Transport-Security"),
            "Strict-Transport-Security must NOT be emitted with no X-Forwarded-Proto "
            "(would force http://localhost onto https://localhost)",
        )
        # CSP must still be present in the negative path.
        self.assertIsNotNone(headers.get("Content-Security-Policy"))

    def test_hsts_absent_when_header_value_is_http(self):
        status, headers, _ = self._get("/health", headers={"X-Forwarded-Proto": "http"})
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Strict-Transport-Security"))
        self.assertIsNotNone(headers.get("Content-Security-Policy"))

    def test_hsts_absent_on_root_index_with_no_forwarded_proto(self):
        # Belt-and-suspenders on a second route (the app shell, not just /health).
        status, headers, _ = self._get("/")
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Strict-Transport-Security"))
        self.assertIsNotNone(headers.get("Content-Security-Policy"))


# ── AC-2: CSP tightened directives, present regardless of X-Forwarded-Proto ─


class CspTightenedDirectivesTests(SecurityHeaderServerTestCase):
    def _csp_for(self, xfp_headers):
        _, headers, _ = self._get("/health", headers=xfp_headers)
        csp = headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp, "Content-Security-Policy missing")
        return csp

    def test_tightened_directives_present_without_forwarded_proto(self):
        csp = self._csp_for({})
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)

    def test_tightened_directives_present_with_forwarded_https(self):
        csp = self._csp_for({"X-Forwarded-Proto": "https"})
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)


# ── AC-3: script-src 'self' retained, no external script origin ────────────


class ScriptSrcInvariantTests(SecurityHeaderServerTestCase):
    def test_script_src_self_retained_and_no_jsdelivr(self):
        _, headers, _ = self._get("/health")
        csp = headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        match = re.search(r"script-src\s+([^;]+)", csp)
        self.assertIsNotNone(match, "script-src directive not found in CSP")
        script_src = match.group(1)
        self.assertIn("'self'", script_src)
        self.assertNotIn("cdn.jsdelivr.net", script_src)
        self.assertNotIn("cdn.jsdelivr.net", csp, "cdn.jsdelivr.net must not appear anywhere in CSP")


# ── AC-5: HEAD carries the identical header set as GET, per X-Forwarded-Proto ─


class HeadGetParityTests(SecurityHeaderServerTestCase):
    def _security_headers(self, headers_obj):
        """Extract the comparable security-header subset (case-insensitive)."""
        return {
            "Strict-Transport-Security": headers_obj.get("Strict-Transport-Security"),
            "Content-Security-Policy": headers_obj.get("Content-Security-Policy"),
            "X-Frame-Options": headers_obj.get("X-Frame-Options"),
            "X-Content-Type-Options": headers_obj.get("X-Content-Type-Options"),
            "Referrer-Policy": headers_obj.get("Referrer-Policy"),
        }

    # NOTE: HEAD parity is exercised against the static app shell ("/index.html"),
    # not a custom /api/* or /health route. do_HEAD is inherited unmodified from
    # SimpleHTTPRequestHandler (this feature adds no do_HEAD override) and only
    # resolves paths through the static file-serving path (send_head); the
    # do_GET() custom-route dispatch table (["/health", "/api/config", ...]) has
    # no HEAD equivalent, so a HEAD request to those routes 404s regardless of
    # this feature (pre-existing behaviour, out of scope here). end_headers() —
    # the single choke point this feature edits — runs identically for both
    # methods on any path that reaches it, which the static shell exercises.

    def test_head_get_parity_with_forwarded_https(self):
        get_status, get_headers, _ = self._get("/index.html", headers={"X-Forwarded-Proto": "https"})
        head_status, head_headers, head_body = self._head(
            "/index.html", headers={"X-Forwarded-Proto": "https"}
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(
            self._security_headers(get_headers), self._security_headers(head_headers)
        )
        # Sanity: HSTS must actually be present in this parity check (positive branch).
        self.assertIsNotNone(get_headers.get("Strict-Transport-Security"))

    def test_head_get_parity_without_forwarded_proto(self):
        get_status, get_headers, _ = self._get("/index.html")
        head_status, head_headers, head_body = self._head("/index.html")
        self.assertEqual(get_status, 200)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(
            self._security_headers(get_headers), self._security_headers(head_headers)
        )
        # Sanity: HSTS must actually be absent in this parity check (negative branch).
        self.assertIsNone(get_headers.get("Strict-Transport-Security"))


# ── AC-1/AC-2 uniformity: /api/* JSON response and the generic 404 ─────────


class HeaderUniformityAcrossRouteKindsTests(SecurityHeaderServerTestCase):
    def test_api_json_response_carries_csp_and_gated_hsts(self):
        # /api/config is a real, DB-independent /api/* JSON endpoint.
        status, headers, _ = self._get("/api/config", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIsNotNone(headers.get("Content-Security-Policy"))
        self.assertIsNotNone(headers.get("Strict-Transport-Security"))

    def test_api_json_response_omits_hsts_without_forwarded_proto(self):
        status, headers, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIsNotNone(headers.get("Content-Security-Policy"))
        self.assertIsNone(headers.get("Strict-Transport-Security"))

    def test_generic_404_carries_csp_and_gated_hsts(self):
        status, headers, _ = self._get(
            "/this-path-does-not-exist-anywhere.xyz",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 404)
        self.assertIsNotNone(headers.get("Content-Security-Policy"))
        self.assertIsNotNone(headers.get("Strict-Transport-Security"))

    def test_generic_404_omits_hsts_without_forwarded_proto(self):
        status, headers, _ = self._get("/this-path-does-not-exist-anywhere.xyz")
        self.assertEqual(status, 404)
        self.assertIsNotNone(headers.get("Content-Security-Policy"))
        self.assertIsNone(headers.get("Strict-Transport-Security"))


if __name__ == "__main__":
    unittest.main()
