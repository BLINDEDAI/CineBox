"""Tests for the static-serving-allowlist-hardening feature (Platform aggregate).

Boots the *real* ``server.Handler`` over an ephemeral-port ``ThreadingHTTPServer``
(same construction as ``tests/e2e/conftest.py``'s ``base_url`` fixture, adapted to
``unittest``) and drives it over real HTTP — static serving is DB-independent, so
no ``DATABASE_URL`` / Postgres / ``init_pool`` / ``init_db`` is needed.

Covers:
  AC-1 — every allow-listed asset serves 200 with the correct content type
  AC-2 — DB schema files (migrations/*.sql) and /server.py -> 404, no body
  AC-3 — documentation files (CLAUDE.md, CONTEXT.md, README.md) -> 404
  AC-4 — CI/hook/config files -> 404
  AC-5 — directory paths -> 404, no listing
  AC-6 — existing routes (/health, /api/config, clean URLs, /u, /l) unchanged
  AC-7 — generic 404 + normalisation-bypass paths (./, ../, %2e%2e)
  AC-8 — HEAD parity: HEAD on a blocked file/dir -> 404, zero body bytes
  BR-7 — drift guard: every local src/href in the HTML pages resolves to 200
"""

import functools
import http.client
import http.server
import re
import socket
import struct
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
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


class StaticServerTestCase(unittest.TestCase):
    """Base class that boots one real server.Handler instance for the whole class.

    Mirrors tests/e2e/conftest.py's base_url fixture but at the unittest layer:
    same ThreadingHTTPServer + real Handler bound to server.BASE_DIR, no DB init.
    """

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

    # ── HTTP helpers ────────────────────────────────────────────────────────

    # NOTE: headers are returned as the original email.message.Message (NOT a
    # plain dict) so lookups stay case-insensitive — the stdlib SimpleHTTPRequestHandler
    # emits "Content-type" (lowercase "t"), while our own _json()/_deny_static() emit
    # "Content-Type"; a plain dict() built from either would silently miss the other
    # under a case-sensitive `.get("Content-Type")`.

    def _get(self, path):
        """GET `path`; returns (status, headers, body_bytes) — never raises on 4xx/5xx."""
        req = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _head(self, path):
        """HEAD `path`; returns (status, headers, body_bytes) — never raises on 4xx/5xx."""
        req = urllib.request.Request(self.base_url + path, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _get_raw_target(self, raw_target):
        """GET an already-encoded request target verbatim (bypasses urllib's own
        normalisation) using http.client, so normalisation-bypass strings like
        '/./migrations/x.sql' or '%2e%2e' reach the server exactly as written."""
        conn = http.client.HTTPConnection(_HOST, self.port, timeout=10)
        try:
            conn.request("GET", raw_target)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, resp.msg, body
        finally:
            conn.close()


# ── AC-1: allow-listed assets serve 200 with correct content type ────────────


class AllowlistedAssetsServedTests(StaticServerTestCase):
    HTML_PAGES = ["index.html", "public.html", "privacy.html", "terms.html", "about.html"]
    JS_MODULES = [
        "boot", "api", "ui", "collection", "modal", "discover",
        "stats", "settings", "activity", "app", "public",
    ]
    CSS_FILES = ["styles", "landing", "legal"]

    def test_root_serves_index_html(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<html", body.lower())

    def test_index_html_via_explicit_path(self):
        status, headers, _ = self._get("/index.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_all_html_shells_serve_200(self):
        for page in self.HTML_PAGES:
            with self.subTest(page=page):
                status, headers, _ = self._get("/" + page)
                self.assertEqual(status, 200, f"{page} did not serve 200")
                self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_all_js_modules_serve_200_with_js_content_type(self):
        for module in self.JS_MODULES:
            path = f"/{module}.js"
            with self.subTest(module=module):
                status, headers, body = self._get(path)
                self.assertEqual(status, 200, f"{path} did not serve 200")
                content_type = headers.get("Content-Type", "")
                self.assertTrue(
                    "javascript" in content_type,
                    f"{path} served content-type {content_type!r}, expected javascript",
                )
                self.assertGreater(len(body), 0, f"{path} served empty body")

    def test_all_css_files_serve_200_with_css_content_type(self):
        for name in self.CSS_FILES:
            path = f"/{name}.css"
            with self.subTest(name=name):
                status, headers, body = self._get(path)
                self.assertEqual(status, 200, f"{path} did not serve 200")
                self.assertIn("text/css", headers.get("Content-Type", ""))
                self.assertGreater(len(body), 0, f"{path} served empty body")

    def test_vendored_supabase_bundle_serves_200(self):
        status, headers, body = self._get("/vendor/supabase-js/2.108.1/supabase.min.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 0)

    def test_assets_image_serves_200(self):
        status, headers, body = self._get("/assets/Guts.png")
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 0)


# ── AC-2 / AC-3: blocked source / DB-schema / docs ───────────────────────────


class BlockedSourceSchemaDocsTests(StaticServerTestCase):
    def test_migration_sql_files_return_404_no_body_disclosure(self):
        sql_files = sorted(p.name for p in (BASE_DIR / "migrations").glob("*.sql"))
        self.assertGreaterEqual(len(sql_files), 2, "expected at least 2 migration files to test siblings")
        for name in sql_files:
            path = f"/migrations/{name}"
            with self.subTest(path=path):
                status, headers, body = self._get(path)
                self.assertEqual(status, 404, f"{path} did not 404")
                self.assertNotIn("sql", headers.get("Content-Type", "").lower())
                self.assertNotIn(b"CREATE TABLE", body)
                self.assertNotIn(b"ALTER TABLE", body)

    def test_server_py_returns_404_no_source_disclosure(self):
        status, headers, body = self._get("/server.py")
        self.assertEqual(status, 404)
        self.assertNotIn(b"import psycopg2", body)
        self.assertNotIn(b"def do_GET", body)

    def test_doc_files_return_404(self):
        for name in ("CLAUDE.md", "CONTEXT.md", "README.md"):
            path = "/" + name
            with self.subTest(path=path):
                status, _, body = self._get(path)
                self.assertEqual(status, 404, f"{path} did not 404")
                self.assertEqual(len(body), len(body))  # sanity: read succeeded


# ── AC-4: blocked CI / hook / config files ───────────────────────────────────


class BlockedCiHookConfigTests(StaticServerTestCase):
    PATHS = [
        "/.github/workflows/unit.yml",
        "/hooks/pre-push",
        "/requirements.txt",
        "/requirements-dev.txt",
        "/ruff.toml",
    ]

    def test_ci_hook_config_paths_return_404(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                status, _, _ = self._get(path)
                self.assertEqual(status, 404, f"{path} did not 404")


# ── AC-5: directory paths — no listing ───────────────────────────────────────


class DirectoryPathsTests(StaticServerTestCase):
    PATHS = [
        "/migrations/",
        "/tests/",
        "/scripts/",
        "/vendor/",
        "/assets/",
        "/vendor/supabase-js/",
    ]

    def test_directory_paths_return_404_no_listing(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                status, headers, body = self._get(path)
                self.assertEqual(status, 404, f"{path} did not 404")
                self.assertNotIn(
                    "text/html", headers.get("Content-Type", ""),
                    f"{path} looks like an HTML directory listing",
                )
                self.assertNotIn(b"Directory listing", body)
                self.assertNotIn(b"<title>", body)


# ── AC-6: existing routes unchanged ──────────────────────────────────────────


class ExistingRoutesUnchangedTests(StaticServerTestCase):
    def test_health_endpoint_unchanged(self):
        status, headers, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIn(b'"ok": true', body)

    def test_api_config_endpoint_unchanged(self):
        status, headers, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_clean_url_legal_pages_serve_html(self):
        for path in ("/privacy", "/terms", "/about"):
            with self.subTest(path=path):
                status, headers, body = self._get(path)
                self.assertEqual(status, 200, f"{path} did not serve 200")
                self.assertIn("text/html", headers.get("Content-Type", ""))
                self.assertIn(b"<html", body.lower())

    def test_public_username_route_serves_public_html(self):
        status, headers, body = self._get("/u/some-test-user")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        expected = (BASE_DIR / "public.html").read_bytes()
        self.assertEqual(body, expected)

    def test_public_list_route_serves_public_html(self):
        status, headers, body = self._get("/l/12345678-1234-1234-1234-123456789012")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        expected = (BASE_DIR / "public.html").read_bytes()
        self.assertEqual(body, expected)


# ── AC-7: generic denial + normalisation-bypass attempts ─────────────────────


class GenericDenialBypassTests(StaticServerTestCase):
    def _assert_generic_404(self, status, headers, body, label):
        self.assertEqual(status, 404, f"{label} did not 404")
        self.assertIn("application/json", headers.get("Content-Type", ""))
        # No filesystem path or directory-listing disclosure in the body.
        self.assertNotIn(b"BASE_DIR", body)
        self.assertNotIn(b"Traceback", body)
        self.assertNotIn(b"Directory listing", body)

    def test_arbitrary_nonexistent_path_returns_generic_404(self):
        status, headers, body = self._get("/this-path-does-not-exist-anywhere.xyz")
        self._assert_generic_404(status, headers, body, "/this-path-does-not-exist-anywhere.xyz")

    def test_dot_segment_bypass_returns_generic_404(self):
        target = "/./migrations/001_public_profiles_and_lists.sql"
        status, headers, body = self._get_raw_target(target)
        self._assert_generic_404(status, headers, body, target)

    def test_dot_dot_bypass_returns_generic_404(self):
        target = "/vendor/../server.py"
        status, headers, body = self._get_raw_target(target)
        self._assert_generic_404(status, headers, body, target)

    def test_percent_encoded_traversal_returns_generic_404(self):
        # %2e%2e = ".." percent-encoded
        target = "/vendor/%2e%2e/server.py"
        status, headers, body = self._get_raw_target(target)
        self._assert_generic_404(status, headers, body, target)


# ── AC-8: HEAD parity ─────────────────────────────────────────────────────────


class HeadParityTests(StaticServerTestCase):
    def test_head_on_blocked_file_returns_404_zero_body(self):
        path = "/migrations/001_public_profiles_and_lists.sql"
        status, headers, body = self._head(path)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"", "HEAD response must carry zero body bytes")

    def test_head_on_server_py_returns_404_zero_body(self):
        status, headers, body = self._head("/server.py")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_head_on_directory_returns_404_zero_body_no_listing(self):
        status, headers, body = self._head("/vendor/")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_head_404_carries_no_real_file_headers(self):
        # A real allow-listed file's HEAD response carries its actual size/type
        # (e.g. "text/javascript" + the file's byte size); the denied HEAD must
        # disclose neither — it carries the generic JSON-error Content-Type and a
        # Content-Length matching the (unwritten, HEAD-suppressed) JSON body, never
        # the real target file's actual size or MIME type.
        real_size = (BASE_DIR / "migrations" / "001_public_profiles_and_lists.sql").stat().st_size

        status, denied_headers, body = self._head("/migrations/001_public_profiles_and_lists.sql")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"", "HEAD response must carry zero body bytes")

        content_type = denied_headers.get("Content-Type", "")
        self.assertIn("application/json", content_type)
        self.assertNotIn("sql", content_type.lower())

        declared_length = denied_headers.get("Content-Length")
        self.assertIsNotNone(declared_length)
        self.assertNotEqual(
            int(declared_length), real_size,
            "denied HEAD Content-Length must not disclose the real file's size",
        )

    def test_get_404_still_carries_generic_body_unlike_head(self):
        path = "/migrations/001_public_profiles_and_lists.sql"
        get_status, get_headers, get_body = self._get(path)
        head_status, _, head_body = self._head(path)
        self.assertEqual(get_status, 404)
        self.assertEqual(head_status, 404)
        self.assertGreater(len(get_body), 0, "GET 404 body must still be present")
        self.assertEqual(head_body, b"", "HEAD 404 body must be empty")


# ── BR-7 drift guard: every local src/href in the HTML pages resolves 200 ────


class DriftGuardTests(StaticServerTestCase):
    """Parses the shipped HTML pages for local (same-origin) src/href references
    and asserts every one resolves to 200 — so a future frontend module added to
    an HTML page without a matching allow-list entry fails this test instead of
    404-ing in production (BR-7)."""

    HTML_PAGES = ["index.html", "public.html", "privacy.html", "terms.html", "about.html"]
    EXCLUDED_PREFIXES = ("http://", "https://", "data:", "mailto:")
    EXCLUDED_ROUTES = {"/", "/u", "/l", "/privacy", "/terms", "/about"}

    def _local_refs(self, html):
        refs = re.findall(r'(?:src|href)="([^"]+)"', html)
        local = []
        for ref in refs:
            if ref.startswith(self.EXCLUDED_PREFIXES):
                continue
            if ref in self.EXCLUDED_ROUTES:
                continue
            local.append(ref if ref.startswith("/") else "/" + ref)
        return local

    def test_every_local_reference_in_html_pages_resolves_200(self):
        checked = 0
        for page in self.HTML_PAGES:
            html = (BASE_DIR / page).read_text(encoding="utf-8")
            for ref in self._local_refs(html):
                checked += 1
                with self.subTest(page=page, ref=ref):
                    status, _, _ = self._get(ref)
                    self.assertEqual(
                        status, 200,
                        f"{page} references {ref!r} which does not serve 200 "
                        f"(missing allow-list entry?)",
                    )
        # Sanity: make sure the parser actually found references to check —
        # an empty run would make this test vacuously green.
        self.assertGreater(checked, 5, "drift guard found suspiciously few local refs to check")


# ── AC-1: /robots.txt ─────────────────────────────────────────────────────────


class RobotsTxtTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-1: robots.txt allows crawling and names
    the sitemap on the canonical www host."""

    def test_robots_txt_serves_200_text_allows_crawling_and_names_sitemap(self):
        status, headers, body = self._get("/robots.txt")
        self.assertEqual(status, 200)
        content_type = headers.get("Content-Type", "")
        self.assertIn("text/plain", content_type, f"unexpected content type {content_type!r}")
        text = body.decode("utf-8")
        self.assertIn("User-agent", text)
        self.assertIn("Allow: /", text)
        self.assertIn("Sitemap: https://www.cinephora.com/sitemap.xml", text)


# ── AC-2: /sitemap.xml ────────────────────────────────────────────────────────


class SitemapXmlTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-2: sitemap.xml is a valid urlset listing
    exactly the four canonical www URLs, excluding the app/public-profile surface."""

    EXPECTED_LOCS = {
        "https://www.cinephora.com/",
        "https://www.cinephora.com/about",
        "https://www.cinephora.com/privacy",
        "https://www.cinephora.com/terms",
    }

    def test_sitemap_xml_serves_200_xml_and_is_parseable(self):
        status, headers, body = self._get("/sitemap.xml")
        self.assertEqual(status, 200)
        content_type = headers.get("Content-Type", "")
        self.assertTrue(
            "xml" in content_type,
            f"sitemap.xml served content-type {content_type!r}, expected an xml type",
        )
        # Parseable per AC-2 -- a malformed sitemap must fail this test, not the assertions below.
        root = ET.fromstring(body)
        self.assertTrue(root.tag.endswith("urlset"), f"unexpected root tag {root.tag!r}")

    def test_sitemap_xml_lists_exactly_the_four_www_urls(self):
        _, _, body = self._get("/sitemap.xml")
        root = ET.fromstring(body)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = {loc.text.strip() for loc in root.findall(".//sm:loc", ns)}
        self.assertEqual(locs, self.EXPECTED_LOCS)

    def test_sitemap_xml_excludes_app_and_public_profile_routes(self):
        # Check the *parsed* <loc> path components, not a raw substring search --
        # the sitemap schema's own closing tags (</url>, </loc>) contain "/u" and
        # "/l" as bare substrings and would false-positive a naive text search.
        _, _, body = self._get("/sitemap.xml")
        root = ET.fromstring(body)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [loc.text.strip() for loc in root.findall(".//sm:loc", ns)]
        for loc in locs:
            path = loc[len("https://www.cinephora.com") :]
            with self.subTest(loc=loc):
                self.assertFalse(path.startswith("/u"), f"{loc} looks like a public-profile route")
                self.assertFalse(path.startswith("/l"), f"{loc} looks like a shared-list route")
                self.assertNotIn("public.html", loc)


# ── AC-3: per-page description + self-canonical on the four static pages ─────


class StaticPagesDescriptionAndCanonicalTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-3: each of the four indexable static
    pages carries a unique <meta name="description"> and a self-<link
    rel="canonical"> to its own clean-route www URL."""

    PAGE_TO_CANONICAL = {
        "index.html": "https://www.cinephora.com/",
        "about.html": "https://www.cinephora.com/about",
        "privacy.html": "https://www.cinephora.com/privacy",
        "terms.html": "https://www.cinephora.com/terms",
    }

    @staticmethod
    def _description(html):
        m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
        return m.group(1) if m else None

    @staticmethod
    def _canonical(html):
        m = re.search(r'<link\s+rel="canonical"\s+href="([^"]*)"', html)
        return m.group(1) if m else None

    def test_each_static_page_has_a_description_and_self_canonical(self):
        for page, canonical_url in self.PAGE_TO_CANONICAL.items():
            with self.subTest(page=page):
                _, _, body = self._get("/" + page)
                html = body.decode("utf-8")
                description = self._description(html)
                self.assertIsNotNone(description, f"{page} is missing <meta name=\"description\">")
                self.assertGreater(len(description), 0, f"{page} has an empty description")
                self.assertEqual(
                    self._canonical(html), canonical_url,
                    f"{page} canonical does not point to its own clean-route www URL",
                )

    def test_descriptions_are_unique_across_the_four_pages(self):
        descriptions = []
        for page in self.PAGE_TO_CANONICAL:
            _, _, body = self._get("/" + page)
            descriptions.append(self._description(body.decode("utf-8")))
        self.assertEqual(
            len(descriptions), len(set(descriptions)),
            f"expected 4 unique descriptions, got {descriptions}",
        )


# ── AC-4 / AC-5: Open Graph + Twitter tag sets on all five public pages ──────


class OpenGraphAndTwitterTagsTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-4/AC-5: all five public pages expose the
    full Open Graph set and the Twitter summary_large_image set; the four static
    pages additionally carry a per-page og:url."""

    STATIC_PAGES = ["index.html", "about.html", "privacy.html", "terms.html"]
    ALL_PAGES = STATIC_PAGES + ["public.html"]
    OG_IMAGE_URL = "https://www.cinephora.com/assets/og-cinephora.png"

    @staticmethod
    def _meta_property(html, prop):
        m = re.search(
            rf'<meta\s+property="{re.escape(prop)}"\s+content="([^"]*)"', html,
        )
        return m.group(1) if m else None

    @staticmethod
    def _meta_name(html, name):
        m = re.search(rf'<meta\s+name="{re.escape(name)}"\s+content="([^"]*)"', html)
        return m.group(1) if m else None

    def test_all_five_pages_expose_the_full_og_set(self):
        for page in self.ALL_PAGES:
            with self.subTest(page=page):
                _, _, body = self._get("/" + page)
                html = body.decode("utf-8")
                self.assertEqual(self._meta_property(html, "og:type"), "website", page)
                self.assertEqual(self._meta_property(html, "og:site_name"), "Cinephora", page)
                self.assertTrue(self._meta_property(html, "og:title"), f"{page} missing og:title")
                self.assertTrue(
                    self._meta_property(html, "og:description"), f"{page} missing og:description",
                )
                self.assertEqual(self._meta_property(html, "og:image"), self.OG_IMAGE_URL, page)
                self.assertEqual(self._meta_property(html, "og:image:width"), "1200", page)
                self.assertEqual(self._meta_property(html, "og:image:height"), "630", page)
                self.assertEqual(self._meta_property(html, "og:locale"), "es_ES", page)

    def test_the_four_static_pages_additionally_carry_a_per_page_og_url(self):
        for page in self.STATIC_PAGES:
            with self.subTest(page=page):
                _, _, body = self._get("/" + page)
                html = body.decode("utf-8")
                og_url = self._meta_property(html, "og:url")
                self.assertIsNotNone(og_url, f"{page} is missing og:url")
                self.assertTrue(og_url.startswith("https://www.cinephora.com/"), og_url)

    def test_all_five_pages_expose_the_twitter_summary_large_image_set(self):
        for page in self.ALL_PAGES:
            with self.subTest(page=page):
                _, _, body = self._get("/" + page)
                html = body.decode("utf-8")
                self.assertEqual(
                    self._meta_name(html, "twitter:card"), "summary_large_image", page,
                )
                self.assertTrue(self._meta_name(html, "twitter:title"), f"{page} missing twitter:title")
                self.assertTrue(
                    self._meta_name(html, "twitter:description"),
                    f"{page} missing twitter:description",
                )
                self.assertEqual(
                    self._meta_name(html, "twitter:image"), self.OG_IMAGE_URL, page,
                )


# ── AC-6: branded OG image serves 200 image/png at 1200x630 ─────────────────


class OgImageTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-6: /assets/og-cinephora.png serves 200
    image/png at exactly 1200x630, read from the PNG IHDR chunk in the served
    bytes (no image library)."""

    @staticmethod
    def _png_dimensions(body):
        # PNG signature (8 bytes) + IHDR chunk: length(4) + "IHDR"(4) + width(4) + height(4).
        # Width = bytes 16..20, height = bytes 20..24 (big-endian), per the PNG spec.
        width = struct.unpack(">I", body[16:20])[0]
        height = struct.unpack(">I", body[20:24])[0]
        return width, height

    def test_og_image_serves_200_png_at_1200x630(self):
        status, headers, body = self._get("/assets/og-cinephora.png")
        self.assertEqual(status, 200)
        self.assertIn("image/png", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 24, "PNG body too short to contain an IHDR chunk")
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n", "not a valid PNG signature")
        width, height = self._png_dimensions(body)
        self.assertEqual((width, height), (1200, 630))


# ── AC-9: public.html is noindex with no canonical ───────────────────────────


class PublicHtmlNoindexTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-9: public.html <head> carries
    <meta name="robots" content="noindex"> and no <link rel="canonical">, since
    it is one static shell served for every /u/<username> and /l/<share_token>."""

    def test_public_html_has_noindex_and_no_canonical(self):
        _, _, body = self._get("/public.html")
        html = body.decode("utf-8")
        self.assertRegex(html, r'<meta\s+name="robots"\s+content="noindex">')
        self.assertNotRegex(html, r'<link\s+rel="canonical"')

    def test_public_profile_route_also_has_noindex_and_no_canonical(self):
        # /u/<username> serves public.html verbatim (ExistingRoutesUnchangedTests
        # already proves byte-equality) -- re-assert on the actual routed response
        # so this AC does not depend on that other test class staying green.
        _, _, body = self._get("/u/some-test-user")
        html = body.decode("utf-8")
        self.assertRegex(html, r'<meta\s+name="robots"\s+content="noindex">')
        self.assertNotRegex(html, r'<link\s+rel="canonical"')


# ── AC-7: perimeter regression — only the two new files newly serve ─────────


class NewFilesOnlyPerimeterRegressionTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-7: the previously-hardened internal
    paths still 404; STATIC_FILES gained exactly the two new crawler filenames
    and nothing else."""

    EXPECTED_NEW_STATIC_FILES = {"robots.txt", "sitemap.xml"}

    def test_static_files_allowlist_gained_only_the_two_new_filenames(self):
        # Mirrors the drift-guard's own sanity-check style: assert the frozenset's
        # membership directly, so a broader (accidental) allow-list widening fails
        # this test even if every individual URL still 404s or 200s as expected.
        pre_existing = {
            "index.html", "public.html", "privacy.html", "terms.html", "about.html",
            "boot.js", "api.js", "ui.js", "collection.js", "modal.js", "discover.js",
            "stats.js", "settings.js", "activity.js", "app.js", "public.js",
            "styles.css", "landing.css", "legal.css",
        }
        added = server.STATIC_FILES - pre_existing
        self.assertEqual(added, self.EXPECTED_NEW_STATIC_FILES)

    def test_internal_paths_still_404_after_the_allowlist_change(self):
        paths = [
            "/migrations/001_public_profiles_and_lists.sql",
            "/CLAUDE.md",
            "/vendor/",
        ]
        for path in paths:
            with self.subTest(path=path):
                status, _, _ = self._get(path)
                self.assertEqual(status, 404, f"{path} did not 404")


# ── AC-8: CSP/HSTS unchanged + no inline script/style on the changed pages ──


class SecurityHeadersUnchangedTests(StaticServerTestCase):
    """seo-and-open-graph-public-pages AC-8: the CSP header on a public page and
    on the two new crawler files is byte-identical to the existing perimeter
    CSP; HSTS is emitted under X-Forwarded-Proto: https; no inline <script> or
    <style> appears in any changed page."""

    EXPECTED_CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "img-src 'self' https://image.tmdb.org https://*.supabase.co data: blob:; "
        "connect-src 'self' https://*.supabase.co; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )

    def test_csp_byte_identical_on_public_page_and_the_two_new_files(self):
        for path in ("/", "/robots.txt", "/sitemap.xml"):
            with self.subTest(path=path):
                _, headers, _ = self._get(path)
                self.assertEqual(headers.get("Content-Security-Policy"), self.EXPECTED_CSP, path)

    def test_hsts_emitted_when_forwarded_proto_is_https(self):
        req = urllib.request.Request(self.base_url + "/", method="GET")
        req.add_header("X-Forwarded-Proto", "https")
        with urllib.request.urlopen(req, timeout=10) as resp:
            hsts = resp.headers.get("Strict-Transport-Security")
        self.assertIsNotNone(hsts, "HSTS header not emitted under X-Forwarded-Proto: https")
        self.assertIn("max-age=63072000", hsts)

    def test_hsts_absent_without_forwarded_proto_header(self):
        _, headers, _ = self._get("/")
        self.assertIsNone(headers.get("Strict-Transport-Security"))

    def test_changed_pages_have_no_inline_script_or_style(self):
        for page in ("index.html", "about.html", "privacy.html", "terms.html", "public.html"):
            with self.subTest(page=page):
                _, _, body = self._get("/" + page)
                html = body.decode("utf-8")
                # An inline <script> is one with no src= attribute (a src="..." script
                # is an external reference, not inline code); likewise <style> blocks
                # (as opposed to <link rel="stylesheet">) would be inline CSS.
                for m in re.finditer(r"<script\b([^>]*)>", html, re.IGNORECASE):
                    attrs = m.group(1)
                    self.assertIn("src=", attrs, f"{page} has an inline <script> tag: {m.group(0)!r}")
                self.assertNotIn("<style", html.lower(), f"{page} has an inline <style> block")


if __name__ == "__main__":
    unittest.main()
