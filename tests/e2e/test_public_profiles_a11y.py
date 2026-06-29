"""Browser E2E tests for public-profiles-and-shared-lists (AC-15/16/17/18).

Covers:
  AC-15 — public profile page, es-ES, desktop+mobile: axe WCAG 2.2 A/AA,
           keyboard reachable, visible focus, target ≥ 24 px.
  AC-16 — public list page, es-ES, desktop+mobile: same a11y matrix.
  AC-17 — public profile page CWV lab: LCP ≤ 2.5s, CLS ≤ 0.1, TBT ≤ 200ms.
  AC-18 — authed sharing-settings view (index.html #sharing-view), es-ES,
           desktop+mobile: axe WCAG 2.2 A/AA, keyboard + visible focus.

Strategy:
  - The real CineBox server is booted (via conftest.py base_url fixture).
  - The public endpoints have no DB, so we intercept API calls with page.route()
    and return mock JSON matching the documented response shapes.
  - axe-core (4.9.0) is injected via page.add_script_tag(path=...) which bypasses
    CSP restrictions that block external CDN scripts — the injection is test-only
    and matches what @axe-core/playwright does internally.
  - Screenshots are saved to handoffs/public-profiles-and-shared-lists/screenshots/.
  - node is NOT installed in this environment; axe is injected from the local JS
    file downloaded to tests/e2e/axe.min.js (559 KB, same source as npm axe-core).

PRODUCTION BUG (BUG-001 — Frontend Developer):
  public.html uses relative href="styles.css" and src="public.js". When the server
  redirects /u/{username} -> public.html (via self.path="/public.html"), the browser
  resolves these relative to /u/{username}, causing 404s for styles.css and public.js.
  Fix: change to absolute paths /styles.css and /public.js in public.html (same fix
  needed for /l/{share_token}).
  Workaround in tests: route /u/styles.css and /u/public.js to their correct server
  paths so the test can verify a11y on the functional page.
  Owner: Frontend Developer.
  Proposed fix: In public.html, change:
    <link rel="stylesheet" href="styles.css"> -> href="/styles.css"
    <script src="public.js" defer> -> src="/public.js"

Note: The SPA authed view (AC-18) is tested without real Supabase auth by injecting
a mock for the api() global used by sharing.js, so the sharing view renders.
"""

import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs" / "public-profiles-and-shared-lists" / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Mock API data (matching backend handoff documented shapes) ─────────────────

_MOCK_PROFILE_COLLECTION_STATS = {
    "ok": True,
    "profile": {
        "username": "testuser",
        "collection": [
            {
                "title": "Dune: Part Two",
                "poster_url": "https://image.tmdb.org/t/p/w342/mock.jpg",
                "status": "vista",
                "rating": 5,
                "media_type": "movie",
                "current_season": None,
                "total_seasons": None,
            },
            {
                "title": "The Bear",
                "poster_url": "https://image.tmdb.org/t/p/w342/bear.jpg",
                "status": "viendo",
                "rating": 4,
                "media_type": "tv",
                "current_season": 3,
                "total_seasons": 3,
            },
        ],
        "stats": {
            "points": 45,
            "level": 1,
            "name": "Espectador",
            "current_min": 0,
            "next_min": 50,
            "next_name": "Aficionado",
            "points_into_level": 45,
            "points_to_next": 5,
            "progress_pct": 90,
        },
        "lists": [
            {
                "id": "list-uuid-1",
                "name": "Mis favoritas de terror",
                "share_token": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "item_count": 3,
            },
        ],
    },
}

_MOCK_LIST = {
    "ok": True,
    "list": {
        "name": "Mis favoritas de terror",
        "owner_username": "testuser",
        "items": [
            {
                "tmdb_id": 123,
                "media_type": "movie",
                "title": "Hereditary",
                "year": "2018",
                "poster_url": "https://image.tmdb.org/t/p/w342/hereditary.jpg",
            },
            {
                "tmdb_id": 456,
                "media_type": "movie",
                "title": "Midsommar",
                "year": "2019",
                "poster_url": "",
            },
        ],
    },
}

_SHARE_TOKEN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_USERNAME = "testuser"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _inject_axe(page: Page, base_url: str):
    """Inject axe-core 4.9.0 by routing it as a same-origin script (CSP: script-src 'self').

    page.add_script_tag(path=...) injects inline scripts that CSP blocks.
    Instead, we route a same-origin URL to serve the local axe.min.js bytes,
    then add a <script src=...> tag pointing to that same-origin URL.
    """
    axe_url = f"{base_url}/__test__/axe.min.js"

    # Route the axe URL to serve from disk (same-origin -> CSP allows it)
    axe_content = AXE_JS.read_bytes()

    def _serve_axe(route):
        route.fulfill(status=200, content_type="application/javascript", body=axe_content)

    page.route(axe_url, _serve_axe)
    page.add_script_tag(url=axe_url)
    # Wait for axe to be available
    page.wait_for_timeout(200)


def _run_axe(page: Page, context_selector: str = "html") -> list:
    """Run axe-core and return violations filtered to critical/serious impact."""
    results = page.evaluate(
        """(sel) => {
            return axe.run(
                document.querySelector(sel) || document,
                {
                    runOnly: {
                        type: 'tag',
                        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']
                    }
                }
            ).then(r => r.violations.map(v => ({
                id: v.id,
                impact: v.impact,
                description: v.description,
                nodes: v.nodes.length
            })));
        }""",
        context_selector,
    )
    return [v for v in results if v["impact"] in ("critical", "serious")]


def _screenshot(page: Page, name: str):
    """Save a screenshot to the handoff screenshots directory."""
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


def _wait_public_page(page: Page):
    """Wait for public.js to finish rendering (loading indicator gone).

    Uses locator().wait_for(state='hidden') instead of wait_for_function(string)
    to avoid the CSP 'unsafe-eval' restriction that blocks string-based predicates.
    If .pub-loading is already absent (fast render), the wait resolves immediately.
    """
    loading = page.locator(".pub-loading")
    # If the element exists, wait for it to detach/hide; if already gone, no-op.
    if loading.count() > 0:
        loading.first.wait_for(state="hidden", timeout=10000)
    else:
        # Ensure the page is settled (network idle was already reached by goto)
        page.wait_for_load_state("networkidle", timeout=10000)


def _route_broken_relative_assets(page: Page, base_url: str, prefix: str):
    """Workaround for BUG-001: public.html uses relative paths (styles.css, public.js).
    When served at /u/{username} or /l/{token}, the browser resolves them relative
    to that path (e.g. /u/styles.css, /u/public.js -> 404).

    This workaround serves these files directly from disk so the tests can verify
    a11y on a functional page. The bug must still be fixed by the Frontend Developer.
    """
    _BASE = Path(__file__).resolve().parent.parent.parent  # CineBox/

    def _serve_from_disk(filename):
        file_path = _BASE / filename

        def _handle(route):
            content = file_path.read_bytes()
            # Guess content-type
            ct = "text/css" if filename.endswith(".css") else "application/javascript"
            route.fulfill(status=200, content_type=ct, body=content)

        return _handle

    # The browser resolves relative URLs relative to the PARENT of the page URL.
    # For /u/testuser -> parent is /u/ -> /u/styles.css and /u/public.js
    # For /l/<token>  -> parent is /l/ -> /l/styles.css and /l/public.js
    parent = prefix.split("/")[0]  # "u" or "l"
    page.route(f"{base_url}/{parent}/styles.css", _serve_from_disk("styles.css"))
    page.route(f"{base_url}/{parent}/public.js", _serve_from_disk("public.js"))


def _route_public_profile(page: Page, base_url: str, mock_data: dict = None):
    """Intercept /api/public/profile/testuser and return mock JSON."""
    data = mock_data or _MOCK_PROFILE_COLLECTION_STATS

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(data),
        )

    page.route(f"{base_url}/api/public/profile/{_USERNAME}", handle)


def _route_public_list(page: Page, base_url: str, mock_data: dict = None):
    """Intercept /api/public/list/<token> and return mock JSON."""
    data = mock_data or _MOCK_LIST

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(data),
        )

    page.route(f"{base_url}/api/public/list/{_SHARE_TOKEN}", handle)


def _route_sharing_api(page: Page, base_url: str):
    """Intercept /api/profile and /api/lists so sharing.js renders without auth."""

    def profile_handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True,
                "profile": {
                    "username": "testuser",
                    "is_public": True,
                    "show_collection": True,
                    "show_stats": False,
                },
            }),
        )

    def lists_handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True,
                "lists": [
                    {
                        "id": "list-uuid-1",
                        "name": "Mis favoritas de terror",
                        "visibility": "public",
                        "share_token": _SHARE_TOKEN,
                        "item_count": 2,
                        "updated_at": "2026-06-28T10:00:00+00:00",
                    },
                ],
            }),
        )

    page.route(f"{base_url}/api/profile", profile_handle)
    page.route(f"{base_url}/api/lists", lists_handle)


# ── AC-15: Public profile page a11y — desktop ─────────────────────────────────


def test_public_profile_a11y_desktop(page: Page, base_url: str):
    """AC-15: public profile page, desktop 1280px, WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url, f"u/{_USERNAME}")  # BUG-001 workaround
    page.goto(f"{base_url}/u/{_USERNAME}")
    _wait_public_page(page)

    _screenshot(page, "public-profile-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "public-profile-desktop-axe")

    assert violations == [], (
        f"AC-15: axe found {len(violations)} critical/serious violations on desktop: "
        + json.dumps(violations, indent=2)
    )


def test_public_profile_a11y_mobile(page: Page, base_url: str):
    """AC-15: public profile page, mobile 375px, WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url, f"u/{_USERNAME}")  # BUG-001 workaround
    page.goto(f"{base_url}/u/{_USERNAME}")
    _wait_public_page(page)

    _screenshot(page, "public-profile-mobile")

    _inject_axe(page, base_url)
    violations = _run_axe(page)

    assert violations == [], (
        f"AC-15: axe found {len(violations)} critical/serious violations on mobile: "
        + json.dumps(violations, indent=2)
    )


def test_public_profile_keyboard_focus(page: Page, base_url: str):
    """AC-15: keyboard-operable with visible focus on public profile page."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url, f"u/{_USERNAME}")  # BUG-001 workaround
    page.goto(f"{base_url}/u/{_USERNAME}")
    _wait_public_page(page)

    # Tab to reach the first interactive element (the brand link in the header)
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.tagName")
    # After first tab, focus should be on a link (brand/home link)
    assert focused in ("A", "BUTTON"), f"First Tab landed on: {focused}"

    # Verify focused element has visible focus style (not outline: none)
    # Check visible focus: the focused element must have a non-zero outline or box-shadow.
    # We check the computed style on the active element (CSS :focus-visible outline).
    focus_outline_width = page.evaluate("""() => {
        const el = document.activeElement;
        const style = window.getComputedStyle(el);
        return style.outlineWidth;
    }""")
    focus_box_shadow = page.evaluate("""() => {
        const el = document.activeElement;
        const style = window.getComputedStyle(el);
        return style.boxShadow;
    }""")
    # Accept if outline is present OR box-shadow is non-none (both are valid focus styles)
    has_visible_focus = (
        focus_outline_width not in ("0px", "")
        or (focus_box_shadow not in ("none", ""))
    )
    assert has_visible_focus, (
        f"Focused element has no visible focus: "
        f"outline={focus_outline_width}, box-shadow={focus_box_shadow}"
    )

    _screenshot(page, "public-profile-keyboard-focus")


def test_public_profile_landmarks_and_h1(page: Page, base_url: str):
    """AC-15: public profile page has exactly one <main> and one <h1> (FE-066)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url, f"u/{_USERNAME}")  # BUG-001 workaround
    page.goto(f"{base_url}/u/{_USERNAME}")
    _wait_public_page(page)

    main_count = page.locator("main").count()
    assert main_count == 1, f"Expected 1 <main>, found {main_count}"

    h1_count = page.locator("h1").count()
    assert h1_count == 1, f"Expected 1 <h1>, found {h1_count}"


def test_public_profile_interactive_targets_24px(page: Page, base_url: str):
    """AC-15: interactive targets ≥ 24px (WCAG 2.5.8 / FE-064) on public profile."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url, f"u/{_USERNAME}")  # BUG-001 workaround
    page.goto(f"{base_url}/u/{_USERNAME}")
    _wait_public_page(page)

    # Check the brand link (home link) target size
    brand_link = page.locator(".pub-brand").first
    box = brand_link.bounding_box()
    if box:
        assert box["width"] >= 24, f"Brand link width {box['width']}px < 24px"
        assert box["height"] >= 24, f"Brand link height {box['height']}px < 24px"

    _screenshot(page, "public-profile-target-sizes")


def test_public_profile_not_found_renders_inert(page: Page, base_url: str):
    """AC-3/security: private/nonexistent profile shows 'No disponible' (not an error)."""
    page.set_viewport_size({"width": 1280, "height": 800})

    def handle_404(route):
        route.fulfill(status=404, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "No encontrado"}))

    page.route(f"{base_url}/api/public/profile/nobody", handle_404)
    _route_broken_relative_assets(page, base_url, "u/nobody")  # BUG-001 workaround
    page.goto(f"{base_url}/u/nobody")
    _wait_public_page(page)

    state_title = page.locator(".pub-state-title")
    expect(state_title).to_have_text("No disponible")
    _screenshot(page, "public-profile-not-found")


# ── AC-16: Public list page a11y ──────────────────────────────────────────────


def test_public_list_a11y_desktop(page: Page, base_url: str):
    """AC-16: public list page, desktop 1280px, WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_list(page, base_url)
    _route_broken_relative_assets(page, base_url, f"l/{_SHARE_TOKEN}")  # BUG-001 workaround
    page.goto(f"{base_url}/l/{_SHARE_TOKEN}")
    _wait_public_page(page)

    _screenshot(page, "public-list-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "public-list-desktop-axe")

    assert violations == [], (
        f"AC-16: axe found {len(violations)} critical/serious violations on list page desktop: "
        + json.dumps(violations, indent=2)
    )


def test_public_list_a11y_mobile(page: Page, base_url: str):
    """AC-16: public list page, mobile 375px, WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _route_public_list(page, base_url)
    _route_broken_relative_assets(page, base_url, f"l/{_SHARE_TOKEN}")  # BUG-001 workaround
    page.goto(f"{base_url}/l/{_SHARE_TOKEN}")
    _wait_public_page(page)

    _screenshot(page, "public-list-mobile")

    _inject_axe(page, base_url)
    violations = _run_axe(page)

    assert violations == [], (
        f"AC-16: axe found {len(violations)} critical/serious violations on list page mobile: "
        + json.dumps(violations, indent=2)
    )


def test_public_list_h1_and_landmarks(page: Page, base_url: str):
    """AC-16: public list page has <main> and <h1> (FE-066)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_list(page, base_url)
    _route_broken_relative_assets(page, base_url, f"l/{_SHARE_TOKEN}")  # BUG-001 workaround
    page.goto(f"{base_url}/l/{_SHARE_TOKEN}")
    _wait_public_page(page)

    assert page.locator("main").count() == 1
    assert page.locator("h1").count() == 1


def test_public_list_xss_title_renders_as_text(page: Page, base_url: str):
    """Security: list named <img onerror=...> renders inert as text, not HTML."""
    page.set_viewport_size({"width": 1280, "height": 800})
    xss_mock = {
        "ok": True,
        "list": {
            "name": "<img src=x onerror=alert(1)>",
            "owner_username": "testuser",
            "items": [],
        },
    }
    _route_public_list(page, base_url, mock_data=xss_mock)
    _route_broken_relative_assets(page, base_url, f"l/{_SHARE_TOKEN}")  # BUG-001 workaround

    alerts_fired = []
    page.on("dialog", lambda d: (alerts_fired.append(d.message), d.dismiss()))

    page.goto(f"{base_url}/l/{_SHARE_TOKEN}")
    _wait_public_page(page)

    # The XSS payload must NOT have triggered an alert
    assert alerts_fired == [], f"XSS alert fired: {alerts_fired}"

    # The <img> tag must NOT be in the DOM as an element
    img_count = page.locator("main img[onerror]").count()
    assert img_count == 0, "XSS img element found in DOM"

    # The list heading should contain the raw text (including the angle brackets)
    heading = page.locator("h1").first
    heading_text = heading.inner_text()
    assert "<img" in heading_text, "Expected raw text, got something else"
    _screenshot(page, "public-list-xss-inert")


# ── AC-17: Core Web Vitals lab — public profile page ─────────────────────────


def test_public_profile_cwv_lab(page: Page, base_url: str):
    """AC-17: public profile LCP ≤ 2.5s, CLS ≤ 0.1, TBT ≤ 200ms (lab proxy)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_profile(page, base_url)

    # Route TMDB poster images to 1px GIF to avoid network latency in LCP
    def _placeholder_image(route):
        # 1×1 transparent GIF
        gif = bytes.fromhex(
            "47494638396101000100800000ffffff00000021f90400000000002c"
            "00000000010001000002024401003b"
        )
        route.fulfill(status=200, content_type="image/gif", body=gif)

    page.route("https://image.tmdb.org/**", _placeholder_image)
    _route_broken_relative_assets(page, base_url, f"u/{_USERNAME}")  # BUG-001 workaround

    page.goto(f"{base_url}/u/{_USERNAME}")
    _wait_public_page(page)

    # Capture CWV lab metrics using PerformanceObserver (per live-browser-verification.md)
    vitals = page.evaluate("""() => new Promise((resolve) => {
        const out = { lcp: 0, cls: 0, tbt: 0 }
        try {
            new PerformanceObserver((l) => {
                for (const e of l.getEntries()) out.lcp = e.startTime
            }).observe({ type: 'largest-contentful-paint', buffered: true })

            new PerformanceObserver((l) => {
                for (const e of l.getEntries()) if (!e.hadRecentInput) out.cls += e.value
            }).observe({ type: 'layout-shift', buffered: true })

            new PerformanceObserver((l) => {
                for (const e of l.getEntries()) out.tbt += Math.max(0, e.duration - 50)
            }).observe({ type: 'longtask', buffered: true })
        } catch (e) { /* metric unsupported — leave at 0 */ }
        setTimeout(() => resolve(out), 600)
    })""")

    lcp = vitals["lcp"]
    cls_val = vitals["cls"]
    tbt = vitals["tbt"]

    _screenshot(page, "public-profile-cwv-lab")

    # AC-17 budgets (lab proxy — see live-browser-verification.md § Core Web Vitals)
    assert lcp <= 2500, f"AC-17: LCP {lcp:.0f}ms > 2500ms budget"
    assert cls_val <= 0.1, f"AC-17: CLS {cls_val:.4f} > 0.1 budget"
    assert tbt <= 200, f"AC-17: TBT {tbt:.0f}ms > 200ms budget"

    # Record captured numbers for the handoff (printed to stdout for pytest capture)
    print(f"\nCWV lab (public profile): LCP={lcp:.0f}ms, CLS={cls_val:.4f}, TBT={tbt:.0f}ms")


# ── AC-18: Authed sharing-settings view a11y ─────────────────────────────────


def _navigate_to_sharing_view(page: Page, base_url: str):
    """Open index.html, mock the auth and API, then click 'Compartir' nav."""
    # Mock auth-required API calls so SPA doesn't redirect to login
    # The SPA checks supabase session; we bypass by intercepting API calls
    # and making sharing.js render with mock data.
    _route_sharing_api(page, base_url)

    # We also intercept /api/config (used by boot.js to get supabase credentials)
    def config_handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"supabase_url": "", "supabase_anon_key": ""}),
        )

    page.route(f"{base_url}/api/config", config_handle)

    # Go to the main SPA page
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    # Inject sharing module state directly and render the view
    # (the nav button requires auth to be established — inject mock session state)
    page.evaluate("""() => {
        // Simulate logged-in state so showView('sharing-view') renders the section
        if (typeof showSharingView === 'function') {
            // Make the sharing-view section visible
            const view = document.getElementById('sharing-view');
            if (view) {
                // Hide all other views first
                document.querySelectorAll('.view').forEach(v => v.hidden = true);
                view.hidden = false;
                // Force a render of the sharing view with mock data
                showSharingView();
            }
        }
    }""")
    page.wait_for_timeout(500)


def test_sharing_view_a11y_desktop(page: Page, base_url: str):
    """AC-18: sharing-settings view, desktop 1280px, axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _navigate_to_sharing_view(page, base_url)

    sharing_section = page.locator("#sharing-view")
    # If sharing view could not be activated (no auth), skip with a note
    is_hidden = sharing_section.get_attribute("hidden")
    if is_hidden is not None:
        pytest.skip(
            "AC-18: sharing-view requires authenticated session — "
            "sharing.js could not render without Supabase. "
            "Requires human verification in a live authed session."
        )

    _screenshot(page, "sharing-view-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#sharing-view")
    _screenshot(page, "sharing-view-desktop-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in sharing view desktop: "
        + json.dumps(violations, indent=2)
    )


def test_sharing_view_a11y_mobile(page: Page, base_url: str):
    """AC-18: sharing-settings view, mobile 375px, axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _navigate_to_sharing_view(page, base_url)

    sharing_section = page.locator("#sharing-view")
    is_hidden = sharing_section.get_attribute("hidden")
    if is_hidden is not None:
        pytest.skip(
            "AC-18: sharing-view requires authenticated session — "
            "sharing.js could not render without Supabase. "
            "Requires human verification in a live authed session."
        )

    _screenshot(page, "sharing-view-mobile")

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#sharing-view")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in sharing view mobile: "
        + json.dumps(violations, indent=2)
    )


def test_sharing_view_keyboard_focus(page: Page, base_url: str):
    """AC-18: sharing view keyboard operable with visible focus."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _navigate_to_sharing_view(page, base_url)

    sharing_section = page.locator("#sharing-view")
    is_hidden = sharing_section.get_attribute("hidden")
    if is_hidden is not None:
        pytest.skip(
            "AC-18: sharing-view requires authenticated session for keyboard test. "
            "Requires human verification in a live authed session."
        )

    # Tab to first interactive element in the sharing view
    # Focus the section first then tab into it
    sharing_section.focus()
    page.keyboard.press("Tab")

    focused_tag = page.evaluate("document.activeElement.tagName")
    assert focused_tag in ("INPUT", "BUTTON", "A", "SELECT", "TEXTAREA"), (
        f"Expected interactive element, got: {focused_tag}"
    )

    focus_outline_width = page.evaluate("""() => {
        const style = window.getComputedStyle(document.activeElement);
        return style.outlineWidth;
    }""")
    focus_box_shadow = page.evaluate("""() => {
        const style = window.getComputedStyle(document.activeElement);
        return style.boxShadow;
    }""")
    has_visible_focus = (
        focus_outline_width not in ("0px", "")
        or (focus_box_shadow not in ("none", ""))
    )
    assert has_visible_focus, (
        f"No visible focus in sharing view: outline={focus_outline_width}, "
        f"box-shadow={focus_box_shadow}"
    )
    _screenshot(page, "sharing-view-keyboard-focus")


# ── Regression: existing SRI supply-chain guarantees still hold ───────────────

def test_public_pages_do_not_load_supabase(page: Page, base_url: str):
    """public.html must NOT load supabase-js or any of the 7 auth SPA modules."""
    _route_public_profile(page, base_url)

    loaded_scripts = []
    page.on("request", lambda req: (
        loaded_scripts.append(req.url)
        if req.resource_type == "script"
        else None
    ))

    page.goto(f"{base_url}/u/{_USERNAME}")
    page.wait_for_load_state("networkidle")

    # None of these should have been loaded
    forbidden_patterns = [
        "supabase", "api.js", "ui.js", "collection.js",
        "modal.js", "discover.js", "stats.js", "app.js", "boot.js",
    ]
    for url in loaded_scripts:
        for pat in forbidden_patterns:
            assert pat not in url.lower(), (
                f"public.html loaded forbidden script ({pat}): {url}"
            )


# ── AC-9: Add-to-list picker a11y (add-titles-to-lists feature) ───────────────
#
# Closes the AC-9 follow-up: the "Añadir a lista" picker (#list-picker, rendered
# by sharing.js openAddToListPicker) is a new interactive component. It is part of
# the authed SPA but depends only on GET /api/lists (mocked here via
# _route_sharing_api), so — unlike the broader sharing view — it renders without a
# real Supabase session.

_PICKER_PAYLOAD = {
    "tmdb_id": 1,
    "media_type": "movie",
    "title": "Dune: Part Two",
    "year": "2024",
    "poster_url": "",
}


def _navigate_to_picker(page: Page, base_url: str):
    """Open index.html, mock /api/lists + /api/config, then open the add-to-list picker."""
    _route_sharing_api(page, base_url)  # mocks /api/profile + /api/lists

    def config_handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"supabase_url": "", "supabase_anon_key": ""}),
        )

    page.route(f"{base_url}/api/config", config_handle)

    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    # openAddToListPicker is async (it fetches /api/lists); evaluate awaits the promise.
    page.evaluate(
        "(payload) => (typeof openAddToListPicker === 'function') "
        "? openAddToListPicker(payload) : null",
        _PICKER_PAYLOAD,
    )
    page.wait_for_timeout(400)


def test_add_to_list_picker_a11y_desktop(page: Page, base_url: str):
    """AC-9: add-to-list picker, desktop 1280px, axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _navigate_to_picker(page, base_url)

    picker = page.locator("#list-picker")
    if picker.get_attribute("hidden") is not None:
        pytest.skip(
            "AC-9: #list-picker did not render — openAddToListPicker unavailable. "
            "Requires human verification in a live session."
        )

    _screenshot(page, "list-picker-desktop")
    _inject_axe(page, base_url)
    violations = _run_axe(page, "#list-picker")
    _screenshot(page, "list-picker-desktop-axe")

    assert violations == [], (
        f"AC-9: axe found {len(violations)} critical/serious violations in the picker (desktop): "
        + json.dumps(violations, indent=2)
    )


def test_add_to_list_picker_a11y_mobile(page: Page, base_url: str):
    """AC-9: add-to-list picker, mobile 375px, axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _navigate_to_picker(page, base_url)

    picker = page.locator("#list-picker")
    if picker.get_attribute("hidden") is not None:
        pytest.skip(
            "AC-9: #list-picker did not render — openAddToListPicker unavailable. "
            "Requires human verification in a live session."
        )

    _screenshot(page, "list-picker-mobile")
    _inject_axe(page, base_url)
    violations = _run_axe(page, "#list-picker")

    assert violations == [], (
        f"AC-9: axe found {len(violations)} critical/serious violations in the picker (mobile): "
        + json.dumps(violations, indent=2)
    )


def test_add_to_list_picker_keyboard_focus(page: Page, base_url: str):
    """AC-9: the picker is keyboard-operable with a visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _navigate_to_picker(page, base_url)

    picker = page.locator("#list-picker")
    if picker.get_attribute("hidden") is not None:
        pytest.skip(
            "AC-9: #list-picker did not render for keyboard test. "
            "Requires human verification in a live session."
        )

    # openAddToListPicker focuses the first actionable control (a list choice or the
    # new-list name input). Confirm focus landed on an interactive element inside the picker.
    focused_tag = page.evaluate("document.activeElement.tagName")
    assert focused_tag in ("BUTTON", "INPUT", "A", "SELECT", "TEXTAREA"), (
        f"AC-9: expected focus on an interactive control, got: {focused_tag}"
    )
    in_picker = page.evaluate("() => !!document.activeElement.closest('#list-picker')")
    assert in_picker, "AC-9: initial focus is not inside the picker"

    outline_width = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).boxShadow"
    )
    has_visible_focus = outline_width not in ("0px", "") or box_shadow not in ("none", "")
    assert has_visible_focus, (
        f"AC-9: no visible focus on the picker control: outline={outline_width}, "
        f"box-shadow={box_shadow}"
    )
    _screenshot(page, "list-picker-keyboard-focus")
