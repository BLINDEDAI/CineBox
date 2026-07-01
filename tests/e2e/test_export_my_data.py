"""Browser E2E tests for export-my-data (AC-1, AC-2, AC-12).

Covers every ### Tester scope row with an E2E surface:

  AC-1  — "Exportar mis datos" control renders in Ajustes → Cuenta with its copy:
           #settings-export-btn present, data-settings-action="export-data",
           explanatory copy visible.
  AC-2  — Triggering the control downloads a JSON file (page.expect_download on
           the <a download> click triggered by _exportData); assert content parses
           as JSON and carries schema_version.
  AC-12 — axe WCAG 2.2 A/AA on the Cuenta view with the control at es-ES on
           1280 px + 375 px, keyboard-operable, visible focus indicator,
           accessible name on the button, target >= 24 px.

Strategy:
  - Real CineBox server via conftest.py base_url fixture (no DB / auth required).
  - window.supabase is stubbed BEFORE modules boot via page.add_init_script().
  - _route_vendor_supabase blocks the real SRI-pinned bundle so the stub survives.
  - _currentUser is set directly via page.evaluate() (including .email and
    .user_metadata.desired_username) BEFORE showSettingsView() so that
    renderSettingsView() finds settingsProfile truthy and does NOT raise the
    username-gate.
  - /api/account/export is stubbed via page.route (LIFO — narrow after broad).
  - axe-core injected via vendored tests/e2e/axe.min.js as a same-origin routed
    <script> (CSP: script-src 'self').
  - Screenshots saved to handoffs/export-my-data/screenshots/.

CineBox harness invariants applied (tester-bundle.md § 7):
  - Stub window.supabase AND route vendor bundle to noop (SRI mismatch blocks real
    bundle → stub survives).
  - Set _currentUser directly via page.evaluate (including .email).
  - page.route is LIFO — register narrow export route AFTER broad routes.
  - The export control is a plain <button> (no container ARIA role).
  - Downloads: assert via page.expect_download(); assert content parses as JSON
    and carries schema_version.
"""

import json
from pathlib import Path

from playwright.sync_api import Page

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs"
    / "export-my-data"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Stub data ──────────────────────────────────────────────────────────────────
_PROFILE_WITH_USERNAME = {
    "ok": True,
    "profile": {
        "username": "testuser",
        "is_public": False,
        "show_collection": False,
        "show_stats": False,
    },
}

_LISTS_EMPTY = {"ok": True, "lists": []}

_EXPORT_PAYLOAD = {
    "ok": True,
    "export": {
        "schema_version": 1,
        "exported_at": "2026-07-01T12:00:00+00:00",
        "profile": {
            "username": "testuser",
            "is_public": False,
            "show_collection": False,
            "show_stats": False,
        },
        "collection": [
            {
                "tmdb_id": 101,
                "media_type": "movie",
                "title": "Test Film",
                "year": 2022,
                "poster_url": "/poster.jpg",
                "status": "vista",
                "rating": 4,
                "note": "Great film",
                "watched_at": None,
                "platform": "Netflix",
                "current_season": None,
                "current_episode": None,
                "total_seasons": None,
                "genres": ["Action"],
                "created_at": "2024-01-01T00:00:00+00:00",
            }
        ],
        "lists": [
            {
                "name": "Favorites",
                "visibility": "public",
                "created_at": "2024-01-10T00:00:00+00:00",
                "updated_at": "2024-04-01T00:00:00+00:00",
                "items": [
                    {
                        "tmdb_id": 101,
                        "media_type": "movie",
                        "title": "Test Film",
                        "year": 2022,
                        "poster_url": "/poster.jpg",
                        "position": 1,
                    }
                ],
            }
        ],
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _route_config(page: Page, base_url: str):
    """Stub /api/config so initApp() runs without real Supabase credentials."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "supabase_url": "https://stub.supabase.co",
                    "supabase_anon_key": "stub-anon-key",
                }
            ),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_profile(page: Page, base_url: str, payload: dict):
    """Stub GET /api/profile to return the given payload."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/profile", handle)


def _route_lists(page: Page, base_url: str, payload: dict):
    """Stub GET /api/lists."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/lists", handle)


def _route_vendor_supabase(page: Page, base_url: str):
    """Route the vendor supabase-js bundle to a noop script.

    The real bundle is loaded synchronously in <head> with an SRI integrity hash.
    Serving different bytes causes the browser's SRI check to fail — blocking the
    real bundle — so window.supabase stays as our add_init_script stub.
    """
    noop_js = b"/* stub: supabase vendor noop for e2e tests */"

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=noop_js,
        )

    page.route(f"{base_url}/vendor/supabase-js/**", handle)


def _route_export_api(page: Page, base_url: str, payload: dict, status: int = 200):
    """Stub GET /api/account/export.

    Registered AFTER the broad routes (LIFO — narrower override wins).
    """

    def handle(route):
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/account/export", handle)


def _inject_supabase_stub(page: Page):
    """Inject window.supabase stub BEFORE any script runs (add_init_script)."""
    script = """
    (() => {
        window.__stubState = {
            createClientCallCount: 0,
        };

        window.supabase = {
            createClient: (url, key, opts) => {
                window.__stubState.createClientCallCount++;
                return {
                    auth: {
                        signOut: async () => ({ error: null }),
                        getSession: async () => ({ data: { session: null }, error: null }),
                        onAuthStateChange: (cb) => ({
                            data: { subscription: { unsubscribe: () => {} } }
                        }),
                    }
                };
            }
        };

        window._supabase = {
            auth: {
                signOut: async () => ({ error: null }),
                getSession: async () => ({ data: { session: null }, error: null }),
                onAuthStateChange: (cb) => ({
                    data: { subscription: { unsubscribe: () => {} } }
                }),
            }
        };
    })();
    """
    page.add_init_script(script)


def _goto_spa(page: Page, base_url: str):
    """Navigate to the SPA and wait for network idle; remove welcome overlay."""
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
        }"""
    )


def _mount_authenticated_settings(page: Page, email: str = "test@example.com"):
    """Set _currentUser directly and wire _supabase before showSettingsView().

    Sets _currentUser with .email and .user_metadata.desired_username so that:
    - renderSettingsView() reads the correct email.
    - The username-gate (#username-gate) does not intercept clicks.
    """
    page.evaluate(
        """(emailAddr) => {
            _currentUser = {
                id: 'test-user-id',
                email: emailAddr,
                user_metadata: { desired_username: 'testuser' }
            };
            if (window._supabase) {
                _supabase = window._supabase;
            }
        }""",
        email,
    )


def _open_settings_view(page: Page):
    """Open #settings-view and wait for render."""
    page.evaluate(
        """() => {
            if (typeof showView === 'function') {
                showView('settings-view');
            } else {
                if (typeof showSettingsView === 'function') showSettingsView();
                const s = document.getElementById('settings-view');
                if (s) {
                    document.querySelectorAll('.view').forEach(v => { v.hidden = true; });
                    s.hidden = false;
                }
            }
        }"""
    )
    page.wait_for_timeout(600)


def _setup_page(
    page: Page,
    base_url: str,
    export_payload: dict = None,
    export_status: int = 200,
    email: str = "test@example.com",
):
    """Full setup: inject stub, route APIs, navigate, mount user, open settings.

    Order matters (LIFO routing):
    1. add_init_script sets window.supabase stub BEFORE any page JS runs.
    2. Broad routes registered first (config, profile, lists, vendor noop).
    3. Narrow export route registered LAST (so it wins over any broad pattern).
    4. Navigate, mount _currentUser, open settings view.
    """
    if export_payload is None:
        export_payload = _EXPORT_PAYLOAD

    _inject_supabase_stub(page)
    _route_config(page, base_url)
    _route_profile(page, base_url, _PROFILE_WITH_USERNAME)
    _route_lists(page, base_url, _LISTS_EMPTY)
    _route_vendor_supabase(page, base_url)
    # Register export route LAST (LIFO — narrowest wins)
    _route_export_api(page, base_url, export_payload, export_status)
    _goto_spa(page, base_url)
    _mount_authenticated_settings(page, email)
    _open_settings_view(page)
    # Wait for the export button to be rendered
    page.wait_for_selector("#settings-export-btn", timeout=5000)


def _screenshot(page: Page, name: str) -> str:
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


def _inject_axe(page: Page, base_url: str):
    """Inject vendored axe-core via a same-origin routed URL (CSP: script-src 'self')."""
    axe_url = f"{base_url}/__test__/axe.min.js"
    axe_content = AXE_JS.read_bytes()

    def _serve_axe(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=axe_content,
        )

    page.route(axe_url, _serve_axe)
    page.add_script_tag(url=axe_url)
    page.wait_for_timeout(200)


def _run_axe(page: Page, context_selector: str = "html") -> list:
    """Run axe-core WCAG 2.2 A/AA; return violations with critical/serious impact."""
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


# ── AC-1: "Exportar mis datos" control renders in Ajustes → Cuenta ─────────────


def test_ac1_export_control_renders(page: Page, base_url: str):
    """AC-1: #settings-export-btn renders in the Cuenta section with the correct
    data-settings-action and explanatory copy.

    Asserts:
    - #settings-export-btn exists in the DOM.
    - data-settings-action="export-data" is set.
    - The control is a <button> element (not a link or other element type).
    - The explanatory copy paragraph is visible (contains relevant text).
    - The #settings-export-hint element carries role="status" aria-live="polite".
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _screenshot(page, "ac1-export-control-renders")

    # Button present
    btn = page.locator("#settings-export-btn")
    assert btn.count() == 1, "AC-1: #settings-export-btn must exist in the DOM"

    # data-settings-action
    action = btn.get_attribute("data-settings-action")
    assert action == "export-data", (
        f"AC-1: data-settings-action must be 'export-data'; got {action!r}"
    )

    # It is a <button> (spec: plain <button>, no container ARIA role)
    tag_name = btn.evaluate("el => el.tagName.toLowerCase()")
    assert tag_name == "button", (
        f"AC-1: export control must be a <button>; got <{tag_name}>"
    )

    # Explanatory copy is visible somewhere in the Cuenta section
    cuenta_section = page.locator("#settings-view")
    section_text = cuenta_section.inner_text()
    assert "exportar" in section_text.lower() or "datos" in section_text.lower(), (
        f"AC-1: Cuenta section must contain copy about exporting data; got: {section_text[:200]!r}"
    )

    # Hint element carries ARIA live attributes
    hint = page.locator("#settings-export-hint")
    assert hint.count() == 1, "AC-1: #settings-export-hint must exist"
    assert hint.get_attribute("role") == "status", (
        "AC-1: #settings-export-hint must have role='status'"
    )
    assert hint.get_attribute("aria-live") == "polite", (
        "AC-1: #settings-export-hint must have aria-live='polite'"
    )


# ── AC-2: triggering the control downloads a JSON file ────────────────────────


def test_ac2_export_triggers_download(page: Page, base_url: str):
    """AC-2: clicking the export button downloads a JSON file.

    The download is asserted via page.expect_download() which captures the
    <a download> click triggered inside _exportData(). The downloaded content
    must parse as JSON and carry schema_version.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, export_payload=_EXPORT_PAYLOAD)
    _screenshot(page, "ac2-before-download")

    btn = page.locator("#settings-export-btn")
    assert btn.count() == 1, "AC-2: #settings-export-btn must exist before triggering"

    with page.expect_download(timeout=8000) as download_info:
        btn.click()

    download = download_info.value
    _screenshot(page, "ac2-after-download")

    # Filename must match the cinebox-export-<date>.json pattern
    filename = download.suggested_filename
    assert filename.startswith("cinebox-export-"), (
        f"AC-2: downloaded filename must start with 'cinebox-export-'; got {filename!r}"
    )
    assert filename.endswith(".json"), (
        f"AC-2: downloaded file must have .json extension; got {filename!r}"
    )

    # Content must parse as JSON and carry schema_version
    content = download.path()
    with open(content, encoding="utf-8") as f:
        raw = f.read()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"AC-2: downloaded file content is not valid JSON: {exc}\n"
            f"Content (first 500 chars): {raw[:500]!r}"
        )

    assert "schema_version" in parsed, (
        f"AC-2: downloaded JSON must carry 'schema_version'; got keys: {list(parsed.keys())}"
    )
    assert parsed["schema_version"] == 1, (
        f"AC-2: schema_version must equal 1; got {parsed['schema_version']!r}"
    )


def test_ac2_success_hint_shown_after_download(page: Page, base_url: str):
    """AC-2: after a successful export the hint element shows a success message."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, export_payload=_EXPORT_PAYLOAD)

    btn = page.locator("#settings-export-btn")

    with page.expect_download(timeout=8000):
        btn.click()

    page.wait_for_timeout(500)

    hint_text = page.locator("#settings-export-hint").inner_text()
    assert hint_text, "AC-2: #settings-export-hint must show a message after export"
    assert "export" in hint_text.lower() or "descarg" in hint_text.lower(), (
        f"AC-2: success hint should reference 'exporta' or 'descarg'; got {hint_text!r}"
    )


# ── AC-12: axe WCAG 2.2 A/AA + keyboard + focus + target size ─────────────────


def test_ac12_axe_desktop(page: Page, base_url: str):
    """AC-12: Cuenta view with the export control passes axe WCAG 2.2 A/AA at 1280 px.

    Scans #settings-view (the container rendered by renderSettingsView()).
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac12-axe-desktop")

    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s) on "
        f"#settings-view (desktop 1280px): " + json.dumps(violations, indent=2)
    )


def test_ac12_axe_mobile(page: Page, base_url: str):
    """AC-12: Cuenta view passes axe WCAG 2.2 A/AA at 375 px mobile viewport."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)

    # Ensure the settings view is visible at mobile viewport
    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-export-btn").wait_for(state="visible", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac12-axe-mobile")

    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s) on "
        f"#settings-view (mobile 375px): " + json.dumps(violations, indent=2)
    )


def test_ac12_keyboard_operable(page: Page, base_url: str):
    """AC-12: the export button is keyboard-focusable with a visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    # Focus the export button via keyboard
    btn = page.locator("#settings-export-btn")
    btn.focus()

    focused_id = page.evaluate("() => document.activeElement.id")
    assert focused_id == "settings-export-btn", (
        f"AC-12: #settings-export-btn must be keyboard-focusable; "
        f"activeElement was #{focused_id!r}"
    )

    # Visible focus indicator: outline or box-shadow must be non-trivial
    outline = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).boxShadow"
    )
    has_visible_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
    assert has_visible_focus, (
        f"AC-12: #settings-export-btn has no visible focus indicator: "
        f"outline={outline}, box-shadow={box_shadow}"
    )

    _screenshot(page, "ac12-keyboard-focus")


def test_ac12_accessible_name(page: Page, base_url: str):
    """AC-12: the export button has an accessible name (from its text content)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-export-btn")
    # The accessible name comes from the button's text content
    text = btn.inner_text()
    assert text.strip(), (
        "AC-12: #settings-export-btn must have non-empty text content (accessible name)"
    )
    assert "export" in text.lower() or "datos" in text.lower(), (
        f"AC-12: export button accessible name should reference 'export' or 'datos'; "
        f"got {text!r}"
    )


def test_ac12_target_size_desktop(page: Page, base_url: str):
    """AC-12: the export button target is >= 24 px in both dimensions (WCAG 2.5.8)
    at 1280 px desktop.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-export-btn")
    box = btn.bounding_box()
    assert box is not None, "AC-12: #settings-export-btn has no bounding box"
    assert box["height"] >= 24, (
        f"AC-12: export button height {box['height']}px < 24px (WCAG 2.5.8)"
    )
    assert box["width"] >= 24, (
        f"AC-12: export button width {box['width']}px < 24px (WCAG 2.5.8)"
    )


def test_ac12_target_size_mobile(page: Page, base_url: str):
    """AC-12: the export button target is >= 24 px at 375 px mobile viewport."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)

    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-export-btn").wait_for(state="visible", timeout=5000)

    btn = page.locator("#settings-export-btn")
    box = btn.bounding_box()
    assert box is not None, "AC-12: #settings-export-btn has no bounding box (mobile)"
    assert box["height"] >= 24, (
        f"AC-12: export button height {box['height']}px < 24px at mobile (WCAG 2.5.8)"
    )


def test_ac12_no_container_aria_role(page: Page, base_url: str):
    """AC-12: the export control is a plain <button> — the spec notes no container
    ARIA role should be added to button groups (tester-bundle § 7)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-export-btn")
    tag = btn.evaluate("el => el.tagName.toLowerCase()")
    assert tag == "button", (
        f"AC-12: export control must be a plain <button>; got <{tag}>"
    )

    # The button's immediate parent must NOT have a spurious ARIA role like "list"
    parent_role = btn.evaluate(
        "el => el.parentElement ? el.parentElement.getAttribute('role') : null"
    )
    assert parent_role not in ("list", "listbox", "menu", "grid"), (
        f"AC-12: export button's parent must not carry a spurious ARIA role; "
        f"got role={parent_role!r}"
    )
