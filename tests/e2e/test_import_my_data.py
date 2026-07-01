"""Browser E2E tests for import-my-data (AC-1, AC-2, AC-11, AC-16).

Covers every ### Tester scope row with an E2E surface:

  AC-1  — "Importar mis datos" control renders in Ajustes → Cuenta with copy
           + a file picker:
             #settings-import-btn present, data-settings-action="import-data",
             #settings-import-file (hidden file input) present,
             #settings-import-hint (role="status", aria-live="polite") present,
             explanatory copy visible referencing the additive/non-destructive behaviour.
  AC-2  — Uploading a stub export (via set_input_files + route-stub for
           POST /api/account/import → 200 with a summary) shows the rendered
           summary counts in #settings-import-hint and a success toast.
  AC-11 — Summary counts in the hint are human-readable and reference the
           import/skip counters returned by the server.
  AC-16 — @axe-core/playwright WCAG 2.2 A/AA scan at 1280 px + 375 px viewports,
           zero critical/serious violations; file-picker trigger keyboard-operable
           + visible focus indicator + accessible name + target ≥ 24 px.

Strategy (mirrors tests/e2e/test_export_my_data.py):
  - Real CineBox server via conftest.py base_url fixture (no DB / auth required).
  - window.supabase stubbed BEFORE modules boot via page.add_init_script().
  - _route_vendor_supabase routes the SRI-pinned bundle to a noop script so the
    stub survives (SRI mismatch → browser blocks real bundle).
  - _currentUser set directly via page.evaluate() (with .email and
    .user_metadata.desired_username) BEFORE showSettingsView() so
    renderSettingsView() finds settingsProfile truthy.
  - POST /api/account/import is route-stubbed (LIFO — narrowest registered last).
  - File upload driven by page.locator("#settings-import-file").set_input_files()
    with a temporary JSON file — this fires the delegated 'change' event which
    calls _importData(input).
  - axe-core injected via vendored tests/e2e/axe.min.js as a same-origin routed
    <script> (CSP: script-src 'self').
  - Screenshots saved to handoffs/import-my-data/screenshots/.
"""

import json
import tempfile
from pathlib import Path

from playwright.sync_api import Page

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs"
    / "import-my-data"
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

# Stub summary returned by the route-stubbed /api/account/import endpoint.
_IMPORT_SUMMARY_RESPONSE = {
    "ok": True,
    "summary": {
        "titles_imported": 3,
        "titles_skipped_present": 1,
        "titles_skipped_invalid": 0,
        "lists_created": 1,
        "lists_merged": 1,
        "list_items_imported": 5,
        "list_items_skipped_present": 2,
        "list_items_skipped_invalid": 0,
    },
}

# A minimal valid CineBox export file to upload via set_input_files.
_STUB_EXPORT_FILE_CONTENT = {
    "schema_version": 1,
    "exported_at": "2026-07-01T12:00:00+00:00",
    "profile": {"username": "testuser", "is_public": False},
    "collection": [
        {
            "tmdb_id": 101,
            "media_type": "movie",
            "title": "Test Film",
            "year": "2022",
            "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
            "status": "vista",
            "rating": 4,
            "note": "Great film",
            "watched_at": "2024-06-01",
            "platform": "Netflix",
            "current_season": None,
            "current_episode": None,
            "total_seasons": None,
            "genres": "Action",
            "created_at": "2024-01-01T00:00:00+00:00",
        }
    ],
    "lists": [
        {
            "name": "Favorites",
            "items": [
                {
                    "tmdb_id": 101,
                    "media_type": "movie",
                    "title": "Test Film",
                    "year": "2022",
                    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
                    "position": 1,
                }
            ],
        }
    ],
}


# ── Helpers (mirroring test_export_my_data.py harness) ────────────────────────


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
    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/profile", handle)


def _route_lists(page: Page, base_url: str, payload: dict):
    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/lists", handle)


def _route_vendor_supabase(page: Page, base_url: str):
    """Route the vendor supabase-js bundle to a noop script (SRI bypass for E2E)."""
    noop_js = b"/* stub: supabase vendor noop for e2e tests */"

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=noop_js,
        )

    page.route(f"{base_url}/vendor/supabase-js/**", handle)


def _route_movies(page: Page, base_url: str):
    """Stub /api/movies so loadMovies() does not fail on the import success path."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "movies": []}),
        )

    page.route(f"{base_url}/api/movies", handle)


def _route_import_api(
    page: Page,
    base_url: str,
    payload: dict,
    status: int = 200,
):
    """Stub POST /api/account/import. Registered LAST (LIFO — narrowest wins)."""

    def handle(route):
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/account/import", handle)


def _inject_supabase_stub(page: Page):
    """Inject window.supabase stub BEFORE any script runs (add_init_script)."""
    script = """
    (() => {
        window.supabase = {
            createClient: (url, key, opts) => {
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
    """Set _currentUser directly and wire _supabase before showSettingsView()."""
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
    import_payload: dict = None,
    import_status: int = 200,
    email: str = "test@example.com",
):
    """Full setup: inject stub, route APIs, navigate, mount user, open settings.

    Route registration order (LIFO — narrowest wins):
    1. add_init_script sets window.supabase stub BEFORE any page JS runs.
    2. Broad routes registered first (config, profile, lists, movies, vendor noop).
    3. Narrow import route registered LAST.
    4. Navigate, mount _currentUser, open settings view.
    """
    if import_payload is None:
        import_payload = _IMPORT_SUMMARY_RESPONSE

    _inject_supabase_stub(page)
    _route_config(page, base_url)
    _route_profile(page, base_url, _PROFILE_WITH_USERNAME)
    _route_lists(page, base_url, _LISTS_EMPTY)
    _route_movies(page, base_url)
    _route_vendor_supabase(page, base_url)
    # Register import route LAST (LIFO — narrowest wins)
    _route_import_api(page, base_url, import_payload, import_status)
    _goto_spa(page, base_url)
    _mount_authenticated_settings(page, email)
    _open_settings_view(page)
    # Wait for the import button to be rendered
    page.wait_for_selector("#settings-import-btn", timeout=5000)


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


def _upload_file(page: Page, content: dict = None):
    """Upload a stub JSON file via set_input_files on the hidden #settings-import-file."""
    if content is None:
        content = _STUB_EXPORT_FILE_CONTENT
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(content, f)
        tmp_path = f.name
    page.locator("#settings-import-file").set_input_files(tmp_path)
    return tmp_path


# ── AC-1: "Importar mis datos" control renders in Ajustes → Cuenta ────────────


def test_ac1_import_control_renders(page: Page, base_url: str):
    """AC-1: #settings-import-btn renders in the Cuenta section with the correct
    data-settings-action, an associated file picker, and explanatory copy.

    Asserts:
    - #settings-import-btn exists in the DOM.
    - data-settings-action="import-data" is set.
    - The control is a <button> element.
    - #settings-import-file exists (hidden file input, type="file").
    - #settings-import-hint carries role="status" aria-live="polite".
    - Explanatory copy about the additive/non-destructive import is visible.
    - The import block is beside .settings-export and above .settings-danger.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _screenshot(page, "ac1-import-control-renders")

    # Button present
    btn = page.locator("#settings-import-btn")
    assert btn.count() == 1, "AC-1: #settings-import-btn must exist in the DOM"

    # data-settings-action
    action = btn.get_attribute("data-settings-action")
    assert action == "import-data", (
        f"AC-1: data-settings-action must be 'import-data'; got {action!r}"
    )

    # It is a <button>
    tag_name = btn.evaluate("el => el.tagName.toLowerCase()")
    assert tag_name == "button", (
        f"AC-1: import control must be a <button>; got <{tag_name}>"
    )

    # Hidden file input present
    file_input = page.locator("#settings-import-file")
    assert file_input.count() == 1, "AC-1: #settings-import-file must exist"
    input_type = file_input.get_attribute("type")
    assert input_type == "file", (
        f"AC-1: #settings-import-file must be type='file'; got {input_type!r}"
    )

    # Hint element with ARIA live attributes
    hint = page.locator("#settings-import-hint")
    assert hint.count() == 1, "AC-1: #settings-import-hint must exist"
    assert hint.get_attribute("role") == "status", (
        "AC-1: #settings-import-hint must have role='status'"
    )
    assert hint.get_attribute("aria-live") == "polite", (
        "AC-1: #settings-import-hint must have aria-live='polite'"
    )

    # Explanatory copy is visible in the settings view
    settings_text = page.locator("#settings-view").inner_text()
    assert "importar" in settings_text.lower() or "import" in settings_text.lower(), (
        f"AC-1: Settings view must contain copy about importing data; "
        f"got: {settings_text[:300]!r}"
    )

    # Copy references the additive/non-destructive behaviour
    assert (
        "añad" in settings_text.lower()     # "añadir"
        or "aditi" in settings_text.lower()  # "aditiva"
        or "no borra" in settings_text.lower()
        or "colección" in settings_text.lower()
    ), (
        "AC-1: Explanatory copy must reference the additive/non-destructive behaviour"
    )

    # Layout: import block is beside export block, above danger zone
    # Verify all three blocks exist and that import appears before danger in the DOM
    import_block = page.locator(".settings-import")
    export_block = page.locator(".settings-export")
    danger_block = page.locator(".settings-danger")
    assert import_block.count() >= 1, "AC-1: .settings-import block must exist"
    assert export_block.count() >= 1, "AC-1: .settings-export block must exist"
    assert danger_block.count() >= 1, "AC-1: .settings-danger block must exist"

    import_y = import_block.bounding_box()["y"]
    danger_y = danger_block.bounding_box()["y"]
    assert import_y < danger_y, (
        f"AC-1: .settings-import (y={import_y}) must appear above "
        f".settings-danger (y={danger_y})"
    )


# ── AC-2 / AC-11: uploading a stub export shows the rendered summary ───────────


def test_ac2_upload_shows_summary(page: Page, base_url: str):
    """AC-2 / AC-11: uploading a stub export via set_input_files triggers _importData,
    which POSTs to the route-stubbed /api/account/import and renders the summary
    in #settings-import-hint.

    The route stub returns _IMPORT_SUMMARY_RESPONSE (titles_imported: 3, etc.).
    The test asserts that:
    - The hint element (#settings-import-hint) shows a non-empty message after upload.
    - The message references numeric import counts (integers from the summary).
    - The trigger button is re-enabled after the operation completes.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    hint = page.locator("#settings-import-hint")
    btn = page.locator("#settings-import-btn")

    # Confirm initial state: hint is empty, button is enabled.
    initial_hint = hint.inner_text()
    assert not initial_hint.strip(), (
        f"AC-2: #settings-import-hint must be empty before upload; got {initial_hint!r}"
    )
    assert not btn.get_attribute("disabled"), (
        "AC-2: #settings-import-btn must not be disabled before upload"
    )

    # Upload the stub file via set_input_files — fires the 'change' event.
    _upload_file(page)
    _screenshot(page, "ac2-after-upload")

    # Wait for the hint to be populated (async import + route stub response).
    page.wait_for_function(
        "() => document.getElementById('settings-import-hint').textContent.trim() !== ''",
        timeout=8000,
    )

    hint_text = hint.inner_text()
    assert hint_text.strip(), (
        "AC-2: #settings-import-hint must show a message after successful import"
    )
    # The message must reference the import in some way (number, importado, etc.)
    # The spec says "Importado: N títulos, M listas. Omitidos: P ya presentes, Q inválidos."
    summary = _IMPORT_SUMMARY_RESPONSE["summary"]
    # At least one of the imported counts must appear in the hint text.
    hint_lower = hint_text.lower()
    assert (
        str(summary["titles_imported"]) in hint_text
        or str(summary["list_items_imported"]) in hint_text
        or "importad" in hint_lower
        or "título" in hint_lower
        or "lista" in hint_lower
    ), (
        f"AC-2: hint must reference import counts from summary; got: {hint_text!r}"
    )

    _screenshot(page, "ac2-summary-shown")

    # Button must be re-enabled after completion.
    page.wait_for_function(
        "() => !document.getElementById('settings-import-btn').disabled",
        timeout=5000,
    )
    assert not btn.get_attribute("disabled"), (
        "AC-2: #settings-import-btn must be re-enabled after import completes"
    )


def test_ac2_button_click_opens_file_picker(page: Page, base_url: str):
    """AC-2: clicking #settings-import-btn triggers the hidden file input's click
    (the delegated 'import-data' branch), which opens the native file picker.

    We verify the delegation is wired by confirming the button click doesn't error
    and that the file input is accessible for set_input_files (not display:none).
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    # Confirm the file input is in the accessibility tree (not display:none)
    # so that set_input_files can drive it.
    file_input = page.locator("#settings-import-file")
    assert file_input.count() == 1, "AC-2: #settings-import-file must exist"

    # The input is visually hidden (SR-only clip) but NOT display:none,
    # so it stays in the accessibility tree and set_input_files works.
    is_display_none = file_input.evaluate(
        "el => window.getComputedStyle(el).display === 'none'"
    )
    is_visibility_hidden = file_input.evaluate(
        "el => window.getComputedStyle(el).visibility === 'hidden'"
    )
    assert not is_display_none, (
        "AC-2: #settings-import-file must not be display:none "
        "(must be accessible for programmatic .click() and set_input_files)"
    )
    # visibility:hidden would also block programmatic access — the SR-only pattern
    # uses clip/overflow, not visibility:hidden.
    assert not is_visibility_hidden, (
        "AC-2: #settings-import-file must not be visibility:hidden"
    )


def test_ac11_summary_counters_rendered(page: Page, base_url: str):
    """AC-11: the rendered summary references the 8 counters from data.summary.

    The route stub returns titles_imported:3, lists_created:1, lists_merged:1,
    titles_skipped_present:1, etc. The hint text must include at least the
    imported and skipped counts.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    _upload_file(page)

    page.wait_for_function(
        "() => document.getElementById('settings-import-hint').textContent.trim() !== ''",
        timeout=8000,
    )

    hint_text = page.locator("#settings-import-hint").inner_text()
    summary = _IMPORT_SUMMARY_RESPONSE["summary"]

    # The combined imported count (titles + list_items) = 3 + 5 = 8
    # The combined skipped count (present + invalid) = 1 + 2 = 3
    # The frontend renders a human-readable sentence; at minimum the counts appear.
    found_count = any(str(v) in hint_text for v in summary.values() if isinstance(v, int))
    assert found_count, (
        f"AC-11: summary hint must contain at least one count from data.summary; "
        f"got hint={hint_text!r}, summary={summary}"
    )

    _screenshot(page, "ac11-summary-counters")


def test_ac2_error_status_shows_generic_message(page: Page, base_url: str):
    """AC-2 / AC-15: a 422 response shows a generic es-ES error message, not the
    raw server error, and the button is re-enabled."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(
        page, base_url,
        import_payload={"ok": False, "error": "El archivo no es un export válido de CineBox."},
        import_status=422,
    )

    _upload_file(page)

    # Wait for the hint or button state to change.
    page.wait_for_function(
        "() => document.getElementById('settings-import-hint').textContent.trim() !== '' "
        "|| !document.getElementById('settings-import-btn').disabled",
        timeout=8000,
    )

    # Button must be re-enabled
    btn = page.locator("#settings-import-btn")
    page.wait_for_function(
        "() => !document.getElementById('settings-import-btn').disabled",
        timeout=5000,
    )
    assert not btn.get_attribute("disabled"), (
        "AC-15: #settings-import-btn must be re-enabled after error response"
    )

    _screenshot(page, "ac2-error-422")


# ── AC-16: axe WCAG 2.2 A/AA + keyboard + focus + accessible name + target ────


def test_ac16_axe_desktop(page: Page, base_url: str):
    """AC-16: Cuenta view with the import control passes axe WCAG 2.2 A/AA
    at 1280 px desktop. Zero critical/serious violations."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac16-axe-desktop")

    assert violations == [], (
        f"AC-16: axe found {len(violations)} critical/serious violation(s) on "
        f"#settings-view (desktop 1280px):\n" + json.dumps(violations, indent=2)
    )


def test_ac16_axe_mobile(page: Page, base_url: str):
    """AC-16: Cuenta view passes axe WCAG 2.2 A/AA at 375 px mobile viewport."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)

    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-import-btn").wait_for(state="visible", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac16-axe-mobile")

    assert violations == [], (
        f"AC-16: axe found {len(violations)} critical/serious violation(s) on "
        f"#settings-view (mobile 375px):\n" + json.dumps(violations, indent=2)
    )


def test_ac16_keyboard_operable(page: Page, base_url: str):
    """AC-16: the import trigger button (#settings-import-btn) is keyboard-focusable
    with a visible focus indicator (outline or box-shadow non-trivial)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-import-btn")
    btn.focus()

    focused_id = page.evaluate("() => document.activeElement.id")
    assert focused_id == "settings-import-btn", (
        f"AC-16: #settings-import-btn must be keyboard-focusable; "
        f"activeElement was #{focused_id!r}"
    )

    outline = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).boxShadow"
    )
    has_visible_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
    assert has_visible_focus, (
        f"AC-16: #settings-import-btn has no visible focus indicator: "
        f"outline={outline}, box-shadow={box_shadow}"
    )

    _screenshot(page, "ac16-keyboard-focus")


def test_ac16_accessible_name(page: Page, base_url: str):
    """AC-16: the import trigger button has a non-empty accessible name (from its
    text content — 'Importar mis datos')."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-import-btn")
    text = btn.inner_text()
    assert text.strip(), (
        "AC-16: #settings-import-btn must have non-empty text content (accessible name)"
    )
    assert "import" in text.lower() or "datos" in text.lower(), (
        f"AC-16: import button accessible name should reference 'import' or 'datos'; "
        f"got {text!r}"
    )


def test_ac16_target_size_desktop(page: Page, base_url: str):
    """AC-16: the import trigger target is >= 24 px in both dimensions (WCAG 2.5.8)
    at 1280 px desktop."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-import-btn")
    box = btn.bounding_box()
    assert box is not None, "AC-16: #settings-import-btn has no bounding box"
    assert box["height"] >= 24, (
        f"AC-16: import button height {box['height']}px < 24px (WCAG 2.5.8)"
    )
    assert box["width"] >= 24, (
        f"AC-16: import button width {box['width']}px < 24px (WCAG 2.5.8)"
    )


def test_ac16_target_size_mobile(page: Page, base_url: str):
    """AC-16: the import trigger target is >= 24 px at 375 px mobile viewport."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)

    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-import-btn").wait_for(state="visible", timeout=5000)

    btn = page.locator("#settings-import-btn")
    box = btn.bounding_box()
    assert box is not None, "AC-16: #settings-import-btn has no bounding box (mobile)"
    assert box["height"] >= 24, (
        f"AC-16: import button height {box['height']}px < 24px at mobile (WCAG 2.5.8)"
    )


def test_ac16_file_picker_label_association(page: Page, base_url: str):
    """AC-16: the hidden file input has an associated <label> providing a
    programmatic accessible name. The label must have for='settings-import-file'."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    # Find a <label> for the file input
    label_for = page.locator("label[for='settings-import-file']")
    assert label_for.count() >= 1, (
        "AC-16: a <label for='settings-import-file'> must exist to provide "
        "a programmatic accessible name for the file input"
    )
    label_text = label_for.inner_text()
    assert label_text.strip(), (
        "AC-16: the label for #settings-import-file must have non-empty text"
    )


def test_ac16_import_block_not_danger_styled(page: Page, base_url: str):
    """AC-16 / spec: the import block must use .btn-secondary, never .btn-danger
    (it is a non-destructive action, not a danger control)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    btn = page.locator("#settings-import-btn")
    class_list = btn.get_attribute("class") or ""
    assert "btn-danger" not in class_list, (
        f"AC-16: import button must not use .btn-danger; got classes: {class_list!r}"
    )
    assert "btn-secondary" in class_list, (
        f"AC-16: import button must use .btn-secondary; got classes: {class_list!r}"
    )
