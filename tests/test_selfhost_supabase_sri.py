"""Tests for the selfhost-supabase-sri feature (Platform aggregate).

Covers (automated):
  AC-4   — emitted CSP script-src contains 'self' and NOT cdn.jsdelivr.net
  AC-5   — index.html <script> for supabase references the pinned same-origin path
            with integrity="sha384-…" and crossorigin attributes; no cdn.jsdelivr.net
  AC-1 (server half) — vendored file exists on disk and its path is NOT caught by
            the BLOCKED filter or the dot-segment guard so the static handler serves it
  SRI self-check — the integrity value in index.html equals base64(sha384(vendored bytes))

Browser-only criteria (AC-1 network half, AC-2, AC-3) are covered by manual scripts
in the tester handoff.
"""

import base64
import hashlib
import re
import unittest
from pathlib import Path
from unittest import mock

import server

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent  # CineBox/
INDEX_HTML = BASE_DIR / "index.html"
VENDOR_BUNDLE = BASE_DIR / "vendor" / "supabase-js" / "2.108.1" / "supabase.min.js"

# Expected same-origin src prefix for the supabase <script> tag
EXPECTED_SRC_PREFIX = "vendor/supabase-js/2.108.1/"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _emitted_csp() -> str:
    """Drive Handler.end_headers() with a stubbed send_header and return the
    Content-Security-Policy value it emits.

    Handler.__new__ skips __init__, so no socket/connection needed.  We inject
    only the attributes end_headers() touches: send_header (intercepted) and
    the superclass chain (mocked so super().end_headers() is a no-op).
    """
    h = server.Handler.__new__(server.Handler)

    captured: dict[str, str] = {}

    def fake_send_header(key: str, value: str) -> None:
        captured[key.lower()] = value

    h.send_header = fake_send_header  # type: ignore[method-assign]

    # Prevent the real SimpleHTTPRequestHandler.end_headers from needing a wfile
    with mock.patch(
        "http.server.SimpleHTTPRequestHandler.end_headers",
        lambda self: None,
    ):
        h.end_headers()

    return captured.get("content-security-policy", "")


def _index_html_text() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _supabase_script_tag(html: str) -> str:
    """Return the first <script> tag that references the supabase bundle (any src)."""
    match = re.search(r"<script\b[^>]*supabase[^>]*>", html, re.IGNORECASE)
    if not match:
        return ""
    return match.group(0)


# ── AC-4: CSP header ──────────────────────────────────────────────────────────


class CspHeaderTests(unittest.TestCase):
    """AC-4 — the emitted Content-Security-Policy must tighten script-src."""

    def setUp(self) -> None:
        self.csp = _emitted_csp()

    def test_csp_is_emitted(self) -> None:
        """end_headers() must emit a Content-Security-Policy header."""
        self.assertTrue(self.csp, "No Content-Security-Policy header emitted")

    def test_script_src_contains_self(self) -> None:
        """script-src must include 'self'."""
        # Extract the script-src directive value
        match = re.search(r"script-src\s+([^;]+)", self.csp)
        self.assertIsNotNone(match, "script-src directive not found in CSP")
        script_src = match.group(1)  # type: ignore[union-attr]
        self.assertIn("'self'", script_src, f"'self' not in script-src: {script_src!r}")

    def test_script_src_does_not_contain_jsdelivr(self) -> None:
        """script-src must NOT contain cdn.jsdelivr.net (CDN removed per BR-4)."""
        match = re.search(r"script-src\s+([^;]+)", self.csp)
        self.assertIsNotNone(match, "script-src directive not found in CSP")
        script_src = match.group(1)  # type: ignore[union-attr]
        self.assertNotIn(
            "cdn.jsdelivr.net",
            script_src,
            f"cdn.jsdelivr.net must not appear in script-src, but found: {script_src!r}",
        )

    def test_full_csp_does_not_reference_jsdelivr_anywhere(self) -> None:
        """Belt-and-suspenders: cdn.jsdelivr.net must not appear anywhere in the CSP."""
        self.assertNotIn(
            "cdn.jsdelivr.net",
            self.csp,
            "cdn.jsdelivr.net found in the full CSP header value",
        )


# ── AC-5: index.html <script> tag ────────────────────────────────────────────


class IndexHtmlScriptTagTests(unittest.TestCase):
    """AC-5 — the supabase <script> tag in index.html must reference the
    same-origin vendored path, carry an SRI integrity attribute (sha384-*),
    and carry crossorigin.  The cdn.jsdelivr.net reference must be gone.
    """

    def setUp(self) -> None:
        self.html = _index_html_text()
        self.tag = _supabase_script_tag(self.html)

    def test_supabase_script_tag_exists(self) -> None:
        """index.html must contain a <script> tag referencing the supabase bundle."""
        self.assertTrue(
            self.tag, "No <script> tag referencing supabase found in index.html"
        )

    def test_script_src_is_same_origin_vendor_path(self) -> None:
        """The script src must start with the vendored same-origin path prefix."""
        src_match = re.search(r'src=["\']([^"\']+)["\']', self.tag)
        self.assertIsNotNone(src_match, f"No src attribute in tag: {self.tag!r}")
        src = src_match.group(1)  # type: ignore[union-attr]
        self.assertTrue(
            src.startswith(EXPECTED_SRC_PREFIX)
            or src.startswith("/" + EXPECTED_SRC_PREFIX),
            f"script src {src!r} does not start with expected same-origin prefix "
            f"{EXPECTED_SRC_PREFIX!r}",
        )

    def test_script_tag_has_integrity_attribute(self) -> None:
        """The <script> tag must carry an integrity attribute."""
        self.assertIn(
            "integrity=", self.tag, f"No integrity attribute in: {self.tag!r}"
        )

    def test_integrity_attribute_uses_sha384(self) -> None:
        """The integrity value must use the sha384 algorithm."""
        integrity_match = re.search(r'integrity=["\']([^"\']+)["\']', self.tag)
        self.assertIsNotNone(integrity_match, "No integrity attribute found")
        value = integrity_match.group(1)  # type: ignore[union-attr]
        self.assertTrue(
            value.startswith("sha384-"),
            f"integrity value must start with 'sha384-', got: {value!r}",
        )

    def test_script_tag_has_crossorigin_attribute(self) -> None:
        """The <script> tag must carry a crossorigin attribute (AC-5 explicit requirement)."""
        self.assertIn(
            "crossorigin", self.tag, f"No crossorigin attribute in: {self.tag!r}"
        )

    def test_no_jsdelivr_reference_in_index_html(self) -> None:
        """cdn.jsdelivr.net must not appear anywhere in index.html (CDN fully removed)."""
        self.assertNotIn(
            "cdn.jsdelivr.net",
            self.html,
            "cdn.jsdelivr.net still referenced in index.html",
        )


# ── AC-1 (server half): vendor path passes the BLOCKED / dot-segment filter ──


class VendorPathServedTests(unittest.TestCase):
    """AC-1 server-side half — the vendored bundle must exist on disk and must NOT
    be caught by the BLOCKED extension filter or the dot-segment guard, so the
    static do_GET fall-through reaches super().do_GET() and serves the file.
    """

    VENDOR_PATH = "vendor/supabase-js/2.108.1/supabase.min.js"

    def test_vendor_bundle_exists_on_disk(self) -> None:
        """The vendored bundle must exist at the expected path under CineBox/."""
        self.assertTrue(
            VENDOR_BUNDLE.exists(),
            f"Vendored bundle not found at {VENDOR_BUNDLE}",
        )

    def test_vendor_bundle_is_non_empty(self) -> None:
        """The vendored bundle must not be an empty file."""
        self.assertGreater(
            VENDOR_BUNDLE.stat().st_size,
            0,
            "Vendored bundle is empty",
        )

    def test_vendor_path_not_blocked_by_extension(self) -> None:
        """The vendor .js path must not match any entry in server.BLOCKED."""
        path = self.VENDOR_PATH
        for blocked_ext in server.BLOCKED:
            self.assertFalse(
                path.lower().endswith(blocked_ext),
                f"Vendor path {path!r} is blocked by BLOCKED entry {blocked_ext!r}",
            )

    def test_vendor_path_not_blocked_by_dot_segment(self) -> None:
        """No segment in the vendor path must start with '.' (dot-segment guard)."""
        import urllib.parse

        decoded = urllib.parse.unquote(self.VENDOR_PATH)
        parts = [p for p in decoded.split("/") if p]
        dot_parts = [p for p in parts if p.startswith(".")]
        self.assertEqual(
            dot_parts,
            [],
            f"Vendor path has dot-prefixed segment(s) that would be blocked: {dot_parts}",
        )

    def test_vendor_url_path_not_blocked_by_extension(self) -> None:
        """The URL path with leading slash also passes the BLOCKED check."""
        path = "/" + self.VENDOR_PATH
        for blocked_ext in server.BLOCKED:
            self.assertFalse(
                path.lower().endswith(blocked_ext),
                f"URL path {path!r} is blocked by BLOCKED entry {blocked_ext!r}",
            )


# ── SRI self-check: hash in index.html == sha384 of vendored bytes ───────────


class SriIntegrityMatchTests(unittest.TestCase):
    """SRI self-check — the integrity="sha384-…" value in index.html must equal
    base64(sha384(bytes of the vendored bundle)).

    If the bundle is swapped without updating the hash, this test fails and the
    suite catches the stale-hash regression before it ships.
    """

    def _compute_sha384_sri(self, file_path: Path) -> str:
        """Return the SRI string (sha384-<base64>) for the given file."""
        digest = hashlib.sha384(file_path.read_bytes()).digest()
        return "sha384-" + base64.b64encode(digest).decode("ascii")

    def _extract_integrity_from_html(self) -> str:
        """Return the integrity attribute value from the supabase <script> tag."""
        html = _index_html_text()
        tag = _supabase_script_tag(html)
        match = re.search(r'integrity=["\']([^"\']+)["\']', tag)
        if not match:
            return ""
        return match.group(1)

    def test_integrity_hash_matches_vendored_bundle(self) -> None:
        """integrity in index.html must equal sha384 of the vendored bundle bytes."""
        expected = self._compute_sha384_sri(VENDOR_BUNDLE)
        actual = self._extract_integrity_from_html()

        self.assertNotEqual(
            actual,
            "",
            "Could not extract integrity attribute from index.html supabase <script> tag",
        )
        self.assertEqual(
            actual,
            expected,
            f"\nSRI mismatch — bundle was likely swapped without updating the hash.\n"
            f"  index.html integrity : {actual!r}\n"
            f"  sha384(vendor bundle): {expected!r}\n"
            f"  Fix: re-compute the SRI hash and update integrity= in index.html "
            f"(see vendor/supabase-js/README for the procedure).",
        )


if __name__ == "__main__":
    unittest.main()
