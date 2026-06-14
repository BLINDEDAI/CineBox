"""Browser E2E tests for the selfhost-supabase-sri supply-chain guarantees (ADR-003).

Covers (browser-runtime — these cannot be observed from server-side unittest):

  AC-2  — supabase-js is fetched from the app's own origin; no request is made
           to cdn.jsdelivr.net; the genuine bundle executes (window.supabase defined).
           Ports source selfhost-supabase-sri AC-1 (network half).

  AC-3  — a vendored bundle corrupted in-memory via page.route (bytes differ from
           the integrity hash) is SRI-blocked: window.supabase is undefined and no
           external-CDN fallback request occurs.
           Ports source selfhost-supabase-sri AC-3.

  AC-4  — after the AC-3 case the committed vendor/supabase-js/2.108.1/supabase.min.js
           is byte-identical on disk (sha384 unchanged; no on-disk mutation).

  AC-5  — these two security cases carry no pytest.mark.skip / xfail; a failing
           assertion fails the suite with a non-zero exit.

No pytest.mark.skip or pytest.mark.xfail decorators appear on the two security
cases (test_supabase_fetched_from_own_origin, test_sri_fail_closed_no_cdn_fallback)
— a failing assertion must propagate as a suite failure (non-zero exit), per AC-5.
"""

import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

# ── Constants ─────────────────────────────────────────────────────────────────

# Relative URL path of the vendored supabase-js bundle, exactly as it appears
# in the <script src="..."> attribute of index.html.
VENDOR_BUNDLE_URL_PATH = "/vendor/supabase-js/2.108.1/supabase.min.js"

# Filesystem path to the committed vendored bundle (used by AC-4 to assert
# no on-disk mutation occurred).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # CineBox/
VENDOR_BUNDLE_PATH = (
    _REPO_ROOT / "vendor" / "supabase-js" / "2.108.1" / "supabase.min.js"
)

# Junk bytes appended to the real bundle to produce a sha384 mismatch for AC-3.
_CORRUPTION_SUFFIX = b"\x00CORRUPTED_FOR_SRI_TEST"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sha384_sri(data: bytes) -> str:
    """Return the SRI string ``sha384-<base64>`` for *data*."""
    digest = hashlib.sha384(data).digest()
    return "sha384-" + base64.b64encode(digest).decode("ascii")


def _is_same_origin(url: str, base_url: str) -> bool:
    """Return True when *url* has the same scheme + host + port as *base_url*."""
    u = urlparse(url)
    b = urlparse(base_url)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc)


# ── AC-2: same-origin fetch, no jsDelivr, genuine bundle executes ─────────────


def test_supabase_fetched_from_own_origin(page, base_url):
    """AC-2 — supabase-js is fetched from the app's own origin on page load.

    Asserts:
    (a) A request to the vendored bundle path on 127.0.0.1 was made.
    (b) No request host is cdn.jsdelivr.net.
    (c) window.supabase is defined (the genuine bundle executed successfully).

    No skip / xfail — a failing assertion fails the suite (AC-5).
    """
    captured_urls: list[str] = []

    def _record_request(request) -> None:
        captured_urls.append(request.url)

    page.on("request", _record_request)

    page.goto(base_url)
    # Wait for the page to settle so all deferred scripts have been requested.
    page.wait_for_load_state("networkidle")

    # (a) supabase-js must have been requested from the app's own origin.
    vendor_full_url = base_url.rstrip("/") + VENDOR_BUNDLE_URL_PATH
    own_origin_bundle_requested = any(url == vendor_full_url for url in captured_urls)
    assert own_origin_bundle_requested, (
        f"Expected a request to {vendor_full_url!r} but none was captured. "
        f"Captured URLs: {captured_urls}"
    )

    # (b) No request must have gone to cdn.jsdelivr.net.
    jsdelivr_requests = [
        url for url in captured_urls if "cdn.jsdelivr.net" in urlparse(url).netloc
    ]
    assert not jsdelivr_requests, (
        f"Unexpected request(s) to cdn.jsdelivr.net: {jsdelivr_requests}"
    )

    # (c) Positive sanity: the genuine bundle executed and exposed window.supabase.
    supabase_type = page.evaluate("() => typeof window.supabase")
    assert supabase_type != "undefined", (
        "window.supabase is undefined after page load — the genuine bundle did not "
        "execute. This may indicate an SRI mismatch on disk or a serving error."
    )


# ── AC-3: SRI fail-closed — corrupted bundle blocked, no CDN fallback ─────────


def test_sri_fail_closed_no_cdn_fallback(page, base_url):
    """AC-3 — a tampered vendored bundle is SRI-blocked; no CDN fallback runs.

    Registers a page.route for the vendored bundle URL and fulfils it with the
    real bytes plus a corruption suffix so the sha384 no longer matches the
    integrity attribute in index.html.  The on-disk committed bundle is NEVER
    modified (corruption is purely in-memory via page.route).

    Asserts:
    (a) window.supabase is undefined (the browser refused to execute the tampered
        script).
    (b) No request to cdn.jsdelivr.net (or any non-same-origin host) occurred as
        a CDN fallback.

    No skip / xfail — a failing assertion fails the suite (AC-5).
    """
    real_bytes = VENDOR_BUNDLE_PATH.read_bytes()
    corrupted_bytes = real_bytes + _CORRUPTION_SUFFIX

    fallback_urls: list[str] = []

    def _record_external_request(request) -> None:
        if not _is_same_origin(request.url, base_url):
            fallback_urls.append(request.url)

    def _serve_corrupted(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=corrupted_bytes,
        )

    page.route(base_url.rstrip("/") + VENDOR_BUNDLE_URL_PATH, _serve_corrupted)
    page.on("request", _record_external_request)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    # (a) The browser must have refused to execute the tampered script.
    supabase_type = page.evaluate("() => typeof window.supabase")
    assert supabase_type == "undefined", (
        "window.supabase is defined after loading a corrupted bundle — the browser "
        "did NOT enforce SRI fail-closed. This is a security regression: a tampered "
        "supabase-js can execute in the browser without the user's knowledge."
    )

    # (b) No external-CDN fallback request must have occurred.
    assert not fallback_urls, (
        f"External (non-same-origin) request(s) detected after SRI block — the app "
        f"fell back to a CDN: {fallback_urls}"
    )


# ── AC-4: on-disk bundle is byte-identical after the AC-3 case ────────────────


def test_vendor_bundle_on_disk_unchanged():
    """AC-4 — the committed vendored bundle is byte-identical after the AC-3 case.

    Re-reads vendor/supabase-js/2.108.1/supabase.min.js and asserts its sha384
    is unchanged from the value recorded before the suite ran.  This guarantees
    that the page.route corruption used in AC-3 is purely in-memory and that the
    working tree is left clean regardless of whether AC-3 passed or failed.
    """
    # Read the bundle as it stands now (after any preceding tests).
    current_bytes = VENDOR_BUNDLE_PATH.read_bytes()
    current_sri = _sha384_sri(current_bytes)

    # The SRI value in index.html is the committed ground truth: if the file
    # were mutated on disk, this assertion would catch it.
    index_html = (_REPO_ROOT / "index.html").read_text(encoding="utf-8")
    match = re.search(r'integrity=["\']([^"\']+)["\']', index_html)
    assert match, "Could not extract integrity attribute from index.html"
    committed_sri = match.group(1)

    assert current_sri == committed_sri, (
        f"vendor bundle on disk has been mutated!\n"
        f"  sha384 now      : {current_sri!r}\n"
        f"  sha384 committed: {committed_sri!r}\n"
        "The AC-3 page.route corruption must not modify the on-disk file."
    )
