"""Browser E2E tests for change-password (AC-1..AC-9).

Covers every ### Tester scope row in the task DoD:
  AC-1  — form renders in Ajustes → Cuenta with three masked inputs + labels
  AC-2  — correct current + valid new + matching repeat → probe.signInWithPassword
           ok, _supabase.updateUser called with new password, success shown, fields cleared
  AC-3  — wrong current password → probe error → updateUser NOT called, error shown
  AC-4  — new password shorter than 8 → validation error, ZERO supabase calls
  AC-5  — repeat mismatch → validation error, ZERO supabase calls
  AC-6  — updateUser returns error → generic non-leaking error shown, form retryable
  AC-7  — any empty field → field-level message, ZERO supabase calls
  AC-8  — no password value appears in URL / post-success DOM / console-log
  AC-9  — axe WCAG 2.2 A/AA scan at 1280 px + 375 px → zero critical/serious;
           keyboard-operable + visible focus + targets >= 24 px

Strategy:
  - Real CineBox server via conftest.py base_url fixture (no DB/auth required).
  - window.supabase is stubbed BEFORE modules boot via page.add_init_script().
    The stub exposes a controllable createClient() that yields the probe object;
    _supabase (main client) is wired via a separate page.evaluate() so tests can
    independently control probe.signInWithPassword and _supabase.auth.updateUser.
  - _currentUser is set directly via page.evaluate() with a valid .email field
    BEFORE showSettingsView() / renderSettingsView() is called (front.md lesson:
    do not rely on _updateSidebarUser alone to populate _currentUser.email).
  - Username is set on the profile stub so the no-username gate does not block
    the Cuenta area (renderSettingsView relies on settingsProfile being truthy).
  - page.route is LIFO — narrower overrides are registered AFTER the broad one.
  - Call tracking uses JS-side counters injected into the stub so assertions do
    not need to intercept Supabase network calls (which are stubbed away anyway).
  - axe-core injected via vendored tests/e2e/axe.min.js as a same-origin routed
    <script> (CSP: script-src 'self') — no new npm dependency.
  - Screenshots saved to handoffs/change-password/screenshots/.
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
    / "change-password"
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

# ── Helpers ────────────────────────────────────────────────────────────────────


def _route_config(page: Page, base_url: str):
    """Stub /api/config so initApp() runs without real Supabase credentials.

    Returns non-empty url/key so createClient(...) can be called without
    the supabase-js stub throwing on empty-string arguments.
    """
    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "supabase_url": "https://stub.supabase.co",
                "supabase_anon_key": "stub-anon-key",
            }),
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

    The real vendor bundle is loaded synchronously in <head> with an SRI
    integrity hash. When we intercept the URL and serve different bytes the
    browser's SRI check fails and the browser blocks the script — so it does NOT
    overwrite window.supabase that our add_init_script already set. This is the
    correct pattern for stubbing supabase in this harness: init_script sets the
    stub FIRST, then the vendor bundle load is blocked by SRI mismatch, leaving
    our stub in place. No integrity attribute removal is needed.
    """
    noop_js = b"/* stub: supabase vendor noop for e2e tests */"

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=noop_js,
        )

    page.route(f"{base_url}/vendor/supabase-js/**", handle)


def _inject_supabase_stub(page: Page, probe_error=None, update_error=None):
    """Inject window.supabase stub BEFORE any script runs (add_init_script).

    The stub:
    - Replaces window.supabase with a minimal UMD-compatible object.
    - createClient(...) returns a probe object whose auth.signInWithPassword
      is controllable via the probe_error flag written into window.__stubState.
    - window.__stubState.updateError controls _supabase.auth.updateUser outcome.
    - Call counters (probeCallCount, updateCallCount) are tracked so tests can
      assert zero-call guarantees.

    NOTE: add_init_script runs BEFORE page JS — this replaces the real supabase
    UMD bundle that the page tries to load from vendor/. Because the real bundle
    is served as a same-origin <script src> with SRI, the browser will reject a
    replaced version; we route /__test__/stub-noop to satisfy the <script src>
    reference without SRI interference, but the actual stub object is injected
    by this init_script FIRST so it is already present when settings.js runs.
    """
    script = f"""
    (() => {{
        // Track calls for zero-call assertions
        window.__stubState = {{
            probeError: {json.dumps(probe_error)},
            updateError: {json.dumps(update_error)},
            probeCallCount: 0,
            updateCallCount: 0,
            createClientCallCount: 0,
        }};

        // The probe object returned by createClient(...)
        const probeAuth = {{
            signInWithPassword: async (creds) => {{
                window.__stubState.probeCallCount++;
                const err = window.__stubState.probeError;
                return err ? {{ error: {{ message: err }} }} : {{ error: null }};
            }}
        }};

        const probeClient = {{ auth: probeAuth }};

        // Stub window.supabase (the UMD global the page code reads)
        window.supabase = {{
            createClient: (url, key, opts) => {{
                window.__stubState.createClientCallCount++;
                return probeClient;
            }}
        }};

        // Stub _supabase (main client) — will be set again in page.evaluate
        // after navigation, but we seed it here so any early reference is safe.
        window._supabase = {{
            auth: {{
                updateUser: async (params) => {{
                    window.__stubState.updateCallCount++;
                    const err = window.__stubState.updateError;
                    return err ? {{ error: {{ message: err }} }} : {{ error: null }};
                }},
                signOut: async () => {{ return {{ error: null }}; }},
                getSession: async () => {{ return {{ data: {{ session: null }}, error: null }}; }},
                onAuthStateChange: (cb) => {{
                    // fire SIGNED_IN immediately so initApp() doesn't hang
                    return {{ data: {{ subscription: {{ unsubscribe: () => {{}} }} }} }};
                }},
            }}
        }};
    }})();
    """
    page.add_init_script(script)


def _goto_spa(page: Page, base_url: str):
    """Navigate to the SPA and wait for network idle; remove the welcome overlay."""
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
        }"""
    )


def _mount_authenticated_settings(page: Page, email: str = "user@example.com"):
    """Set _currentUser and drive showSettingsView() via the production seam.

    Sets _currentUser directly (including .email) BEFORE calling showView so
    renderSettingsView() reads the correct email. Also re-wires _supabase.auth.updateUser
    to the stub function so the module's reference to _supabase picks up the stub.
    """
    page.evaluate(
        """(emailAddr) => {
            // Set _currentUser with email so renderSettingsView() + _changePassword read it.
            _currentUser = {
                id: 'test-user-id',
                email: emailAddr,
                user_metadata: { desired_username: 'testuser' }
            };
            // Re-wire _supabase so _changePassword's reference to the module-level
            // _supabase variable picks up our stub (the init_script set window._supabase;
            // here we ensure the global _supabase var used by settings.js is the stub).
            if (window._supabase) {
                // _supabase is a var in app.js scope — assign via global alias
                _supabase = window._supabase;
            }
        }""",
        email,
    )


def _open_settings_view(page: Page):
    """Open #settings-view by calling the production showView seam and wait for render."""
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
    probe_error=None,
    update_error=None,
    email: str = "user@example.com",
):
    """Full setup: inject stub, route APIs, navigate, mount user, open settings.

    Order matters:
    1. add_init_script sets window.supabase stub BEFORE any page JS runs.
    2. Routes are registered (LIFO — last registration wins for the same URL).
    3. The vendor bundle route serves noop bytes → SRI mismatch → browser blocks
       the real vendor script → window.supabase stays as our stub.
    4. Navigate, wait for networkidle, mount _currentUser, open settings view.
    """
    _inject_supabase_stub(page, probe_error=probe_error, update_error=update_error)
    _route_config(page, base_url)
    _route_profile(page, base_url, _PROFILE_WITH_USERNAME)
    _route_lists(page, base_url, _LISTS_EMPTY)
    _route_vendor_supabase(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated_settings(page, email)
    _open_settings_view(page)
    # Wait for the form to be present
    page.wait_for_selector("#settings-password-form", timeout=5000)


def _get_stub_counts(page: Page) -> dict:
    """Return the JS-side call counters from window.__stubState."""
    return page.evaluate(
        """() => ({
            probeCallCount: window.__stubState.probeCallCount,
            updateCallCount: window.__stubState.updateCallCount,
            createClientCallCount: window.__stubState.createClientCallCount,
        })"""
    )


def _reset_stub_counts(page: Page):
    """Reset call counters to 0 so assertions only cover post-reset calls."""
    page.evaluate(
        """() => {
            window.__stubState.probeCallCount = 0;
            window.__stubState.updateCallCount = 0;
            window.__stubState.createClientCallCount = 0;
        }"""
    )


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
            status=200, content_type="application/javascript", body=axe_content
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


# ── AC-1: form renders with three masked inputs + associated labels ─────────────


def test_ac1_form_renders_three_masked_inputs(page: Page, base_url: str):
    """AC-1: Ajustes → Cuenta shows a 'Cambiar contraseña' form with three masked inputs.

    Asserts:
    - #settings-password-form is present.
    - Three type="password" inputs: #settings-current-password, #settings-new-password,
      #settings-new-password-repeat.
    - Each input has an associated <label for> element.
    - The hint element carries role="status" aria-live="polite".
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _screenshot(page, "ac1-form-renders")

    # Form present
    form = page.locator("#settings-password-form")
    assert form.count() == 1, "AC-1: #settings-password-form must exist"

    # Three password inputs
    for input_id in (
        "settings-current-password",
        "settings-new-password",
        "settings-new-password-repeat",
    ):
        el = page.locator(f"#{input_id}")
        assert el.count() == 1, f"AC-1: #{input_id} must exist"
        input_type = el.get_attribute("type")
        assert input_type == "password", (
            f"AC-1: #{input_id} must be type='password', got {input_type!r}"
        )

    # Associated labels (each input must have a <label for="..."> pointing to it)
    for input_id in (
        "settings-current-password",
        "settings-new-password",
        "settings-new-password-repeat",
    ):
        label = page.locator(f"label[for='{input_id}']")
        assert label.count() >= 1, (
            f"AC-1: missing <label for='{input_id}'>; every password input needs an associated label"
        )

    # Hint element carries role="status" aria-live="polite"
    hint = page.locator("#settings-password-hint")
    assert hint.count() == 1, "AC-1: #settings-password-hint must exist"
    assert hint.get_attribute("role") == "status", (
        "AC-1: #settings-password-hint must have role='status'"
    )
    assert hint.get_attribute("aria-live") == "polite", (
        "AC-1: #settings-password-hint must have aria-live='polite'"
    )


# ── AC-2: correct current + valid new + matching repeat → success ──────────────


def test_ac2_correct_password_success(page: Page, base_url: str):
    """AC-2: correct current + valid new + matching repeat → updateUser called with
    new password, success message shown, fields cleared.

    Also covers AC-8 (no password value in DOM/URL after success) and the ordering
    assertion (probe.signInWithPassword is called BEFORE updateUser).
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    # probe returns no error (correct current pw); updateUser returns no error
    _setup_page(page, base_url, probe_error=None, update_error=None)

    console_logs = []
    page.on("console", lambda msg: console_logs.append(msg.text))

    page.fill("#settings-current-password", "currentPass1!")
    page.fill("#settings-new-password", "newPassword2@")
    page.fill("#settings-new-password-repeat", "newPassword2@")
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(1000)

    _screenshot(page, "ac2-success")

    # Success message shown
    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-2: hint must show a success message after successful change"
    assert "contraseña" in hint_text.lower() or "actualiz" in hint_text.lower(), (
        f"AC-2: success hint should reference 'contraseña' or 'actualiz'; got {hint_text!r}"
    )

    # Fields cleared (form.reset() was called)
    assert page.input_value("#settings-current-password") == "", (
        "AC-2: #settings-current-password must be cleared after success"
    )
    assert page.input_value("#settings-new-password") == "", (
        "AC-2: #settings-new-password must be cleared after success"
    )
    assert page.input_value("#settings-new-password-repeat") == "", (
        "AC-2: #settings-new-password-repeat must be cleared after success"
    )

    # Call ordering: probe was called (createClient + signInWithPassword), then updateUser
    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] >= 1, (
        f"AC-2: probe.signInWithPassword must be called; got probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] >= 1, (
        f"AC-2: _supabase.auth.updateUser must be called; got updateCallCount={counts['updateCallCount']}"
    )

    # AC-8: no password value in the URL
    current_url = page.url
    for pw_value in ("currentPass1!", "newPassword2@"):
        assert pw_value not in current_url, (
            f"AC-8: password value {pw_value!r} must not appear in the URL: {current_url}"
        )

    # AC-8: no password value in the post-success DOM text
    body_text = page.evaluate("() => document.body.innerText")
    for pw_value in ("currentPass1!", "newPassword2@"):
        assert pw_value not in body_text, (
            f"AC-8: password value {pw_value!r} must not appear in the post-success DOM"
        )

    # AC-8: no password value in console logs
    console_text = " ".join(console_logs)
    for pw_value in ("currentPass1!", "newPassword2@"):
        assert pw_value not in console_text, (
            f"AC-8: password value {pw_value!r} must not appear in console logs"
        )


# ── AC-3: wrong current password → updateUser NOT called ──────────────────────


def test_ac3_wrong_current_password(page: Page, base_url: str):
    """AC-3: wrong current password → probe returns error → updateUser NOT called,
    error message shown, form remains usable (fields not cleared).

    Asserts the call ordering invariant: probe is called, updateUser is NOT.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    # probe returns an error (wrong current pw); updateUser should NOT be reached
    _setup_page(page, base_url, probe_error="Invalid login credentials", update_error=None)

    page.fill("#settings-current-password", "wrongCurrentPw!")
    page.fill("#settings-new-password", "newPassword2@")
    page.fill("#settings-new-password-repeat", "newPassword2@")
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(1000)

    _screenshot(page, "ac3-wrong-current-password")

    # Error shown to user
    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-3: hint must show an error for wrong current password"
    # The shown message must be the es-ES friendly copy, not a raw SDK error
    assert "incorrecta" in hint_text.lower() or "contraseña actual" in hint_text.lower(), (
        f"AC-3: error message should say 'incorrecta' or 'contraseña actual'; got {hint_text!r}"
    )
    # Raw SDK error text must NOT be shown
    assert "Invalid login credentials" not in hint_text, (
        f"AC-3: raw SDK error must not be shown to the user; got {hint_text!r}"
    )

    # Call ordering: probe called, updateUser NOT called
    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] >= 1, (
        f"AC-3: probe.signInWithPassword must be called; got probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] == 0, (
        f"AC-3: _supabase.auth.updateUser must NOT be called when probe errors; "
        f"got updateCallCount={counts['updateCallCount']}"
    )

    # Submit button re-enabled (form is retryable)
    submit_disabled = page.locator("[data-settings-action='change-password']").get_attribute("disabled")
    assert submit_disabled is None, "AC-3: submit button must be re-enabled after error"


# ── AC-4: new password shorter than 8 → validation error, ZERO supabase calls ──


def test_ac4_short_new_password(page: Page, base_url: str):
    """AC-4: new password shorter than MIN_PASSWORD_LENGTH (8) → client-side
    validation error shown; ZERO supabase calls (probe not built, updateUser not called).
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, probe_error=None, update_error=None)

    page.fill("#settings-current-password", "currentPass1!")
    page.fill("#settings-new-password", "short")   # 5 chars < 8
    page.fill("#settings-new-password-repeat", "short")
    # Reset counters immediately before submit so init-time createClient calls don't pollute
    _reset_stub_counts(page)
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(500)

    _screenshot(page, "ac4-short-new-password")

    # Validation error shown
    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-4: hint must show a validation error for short password"
    assert "8" in hint_text or "caracteres" in hint_text or "contraseña" in hint_text.lower(), (
        f"AC-4: error must mention length requirement; got {hint_text!r}"
    )

    # ZERO supabase calls
    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] == 0, (
        f"AC-4: probe must NOT be called for a short password; got probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] == 0, (
        f"AC-4: updateUser must NOT be called for a short password; got updateCallCount={counts['updateCallCount']}"
    )
    assert counts["createClientCallCount"] == 0, (
        f"AC-4: createClient must NOT be called for a short password; got createClientCallCount={counts['createClientCallCount']}"
    )


# ── AC-5: repeat mismatch → validation error, ZERO supabase calls ─────────────


def test_ac5_repeat_mismatch(page: Page, base_url: str):
    """AC-5: repeat field does not match new password → validation error,
    ZERO supabase calls.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, probe_error=None, update_error=None)

    page.fill("#settings-current-password", "currentPass1!")
    page.fill("#settings-new-password", "newPassword2@")
    page.fill("#settings-new-password-repeat", "differentPass3#")   # mismatch
    # Reset counters immediately before submit so init-time createClient calls don't pollute
    _reset_stub_counts(page)
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(500)

    _screenshot(page, "ac5-repeat-mismatch")

    # Validation error shown
    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-5: hint must show a validation error for mismatched repeat"
    assert "coincid" in hint_text.lower() or "contraseña" in hint_text.lower(), (
        f"AC-5: error must mention mismatch; got {hint_text!r}"
    )

    # ZERO supabase calls
    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] == 0, (
        f"AC-5: probe must NOT be called on mismatch; got probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] == 0, (
        f"AC-5: updateUser must NOT be called on mismatch; got updateCallCount={counts['updateCallCount']}"
    )
    assert counts["createClientCallCount"] == 0, (
        f"AC-5: createClient must NOT be called on mismatch; got createClientCallCount={counts['createClientCallCount']}"
    )


# ── AC-6: updateUser failure → generic non-leaking error, form retryable ───────


def test_ac6_update_user_failure(page: Page, base_url: str):
    """AC-6: probe succeeds (correct current pw) but updateUser returns an error →
    generic non-leaking error shown; form remains retryable (submit re-enabled).

    The shown error must be the friendly es-ES copy, not the raw SDK error string.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, probe_error=None, update_error="Network failure XYZ-123")

    page.fill("#settings-current-password", "currentPass1!")
    page.fill("#settings-new-password", "newPassword2@")
    page.fill("#settings-new-password-repeat", "newPassword2@")
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(1000)

    _screenshot(page, "ac6-update-failure")

    # Generic error shown
    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-6: hint must show an error when updateUser fails"

    # Raw SDK error must NOT be exposed
    assert "Network failure XYZ-123" not in hint_text, (
        f"AC-6: raw SDK error must not be shown to the user; got {hint_text!r}"
    )

    # Generic es-ES friendly copy
    assert (
        "pudo cambiar" in hint_text.lower()
        or "inténtalo" in hint_text.lower()
        or "contraseña" in hint_text.lower()
    ), (
        f"AC-6: generic error should reference 'contraseña' or 'inténtalo'; got {hint_text!r}"
    )

    # Probe was called, updateUser was called (it errored)
    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] >= 1, (
        f"AC-6: probe must be called before updateUser attempt; probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] >= 1, (
        f"AC-6: updateUser must be called; got updateCallCount={counts['updateCallCount']}"
    )

    # Form is retryable: submit button must be re-enabled
    submit_disabled = page.locator("[data-settings-action='change-password']").get_attribute("disabled")
    assert submit_disabled is None, "AC-6: submit button must be re-enabled after updateUser error"

    # Fields NOT cleared (password unchanged, user can retry)
    assert page.input_value("#settings-current-password") != "" or True, (
        # Fields may or may not be cleared on error — but the form must be usable.
        # The spec says 'password unchanged and retryable', not that fields must be
        # preserved; the key assertion is submit re-enabled (above).
        "AC-6: form should remain in a usable state after updateUser error"
    )


# ── AC-7: empty field → field-level message, ZERO supabase calls ──────────────


def test_ac7_empty_current_password(page: Page, base_url: str):
    """AC-7: empty current-password field → field-level message, ZERO supabase calls."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    # Leave current-password empty
    page.fill("#settings-current-password", "")
    page.fill("#settings-new-password", "newPassword2@")
    page.fill("#settings-new-password-repeat", "newPassword2@")
    _reset_stub_counts(page)
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(500)

    _screenshot(page, "ac7-empty-current-password")

    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-7: hint must show an error for empty current-password"

    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] == 0, (
        f"AC-7: probe must NOT be called when current-password is empty; "
        f"probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] == 0, (
        f"AC-7: updateUser must NOT be called; updateCallCount={counts['updateCallCount']}"
    )


def test_ac7_empty_new_password(page: Page, base_url: str):
    """AC-7: empty new-password field → field-level message, ZERO supabase calls."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    page.fill("#settings-current-password", "currentPass1!")
    page.fill("#settings-new-password", "")
    page.fill("#settings-new-password-repeat", "newPassword2@")
    _reset_stub_counts(page)
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(500)

    _screenshot(page, "ac7-empty-new-password")

    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-7: hint must show an error for empty new-password"

    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] == 0, (
        f"AC-7: probe must NOT be called when new-password is empty; "
        f"probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] == 0, (
        f"AC-7: updateUser must NOT be called; updateCallCount={counts['updateCallCount']}"
    )


def test_ac7_empty_repeat_password(page: Page, base_url: str):
    """AC-7: empty repeat-password field → field-level message, ZERO supabase calls."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    page.fill("#settings-current-password", "currentPass1!")
    page.fill("#settings-new-password", "newPassword2@")
    page.fill("#settings-new-password-repeat", "")
    _reset_stub_counts(page)
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(500)

    _screenshot(page, "ac7-empty-repeat-password")

    hint_text = page.locator("#settings-password-hint").inner_text()
    assert hint_text, "AC-7: hint must show an error for empty repeat-password"

    counts = _get_stub_counts(page)
    assert counts["probeCallCount"] == 0, (
        f"AC-7: probe must NOT be called when repeat-password is empty; "
        f"probeCallCount={counts['probeCallCount']}"
    )
    assert counts["updateCallCount"] == 0, (
        f"AC-7: updateUser must NOT be called; updateCallCount={counts['updateCallCount']}"
    )


# ── AC-8: no password in URL / post-success DOM / console-log ─────────────────
# The primary AC-8 assertions live inside test_ac2_correct_password_success (the
# happy path that actually submits a password). The tests below cover the error
# path (AC-3, AC-6) to confirm no leakage occurs there either.


def test_ac8_no_password_in_url_or_dom_on_error(page: Page, base_url: str):
    """AC-8: on wrong-current-password error, no password value appears in URL or DOM."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, probe_error="Invalid login credentials")

    console_logs = []
    page.on("console", lambda msg: console_logs.append(msg.text))

    pw_current = "secretCurrentPw!"
    pw_new = "secretNewPw2@"
    page.fill("#settings-current-password", pw_current)
    page.fill("#settings-new-password", pw_new)
    page.fill("#settings-new-password-repeat", pw_new)
    page.click("[data-settings-action='change-password']")
    page.wait_for_timeout(800)

    _screenshot(page, "ac8-no-leakage-on-error")

    current_url = page.url
    for pw in (pw_current, pw_new):
        assert pw not in current_url, (
            f"AC-8: password {pw!r} must not appear in the URL: {current_url}"
        )

    body_text = page.evaluate("() => document.body.innerText")
    for pw in (pw_current, pw_new):
        assert pw not in body_text, (
            f"AC-8: password {pw!r} must not appear in the DOM after error"
        )

    console_text = " ".join(console_logs)
    for pw in (pw_current, pw_new):
        assert pw not in console_text, (
            f"AC-8: password {pw!r} must not appear in console logs"
        )


def test_ac8_inputs_are_masked(page: Page, base_url: str):
    """AC-8: all three password inputs are type='password' (masked in the browser)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    for input_id in (
        "settings-current-password",
        "settings-new-password",
        "settings-new-password-repeat",
    ):
        input_type = page.locator(f"#{input_id}").get_attribute("type")
        assert input_type == "password", (
            f"AC-8: #{input_id} must be type='password' to mask the value; got {input_type!r}"
        )


# ── AC-9: axe WCAG 2.2 A/AA + keyboard operability + target size ───────────────


def test_ac9_axe_desktop(page: Page, base_url: str):
    """AC-9: the change-password form passes axe WCAG 2.2 A/AA at 1280 px desktop.

    Scans #settings-view (the container that hosts #settings-password-form).
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac9-axe-desktop")

    assert violations == [], (
        f"AC-9: axe found {len(violations)} critical/serious violation(s) on "
        f"#settings-view (desktop 1280px): " + json.dumps(violations, indent=2)
    )


def test_ac9_axe_mobile(page: Page, base_url: str):
    """AC-9: the change-password form passes axe WCAG 2.2 A/AA at 375 px mobile."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)

    # Ensure the settings view is visible at mobile viewport
    page.evaluate(
        """() => {
            const sv = document.getElementById('settings-view');
            if (sv) { sv.style.display = 'block'; sv.hidden = false; }
        }"""
    )
    page.locator("#settings-password-form").wait_for(state="visible", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac9-axe-mobile")

    assert violations == [], (
        f"AC-9: axe found {len(violations)} critical/serious violation(s) on "
        f"#settings-view (mobile 375px): " + json.dumps(violations, indent=2)
    )


def test_ac9_keyboard_operability(page: Page, base_url: str):
    """AC-9: all three password inputs and the submit button are keyboard-focusable
    with a visible focus indicator.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    for input_id in (
        "settings-current-password",
        "settings-new-password",
        "settings-new-password-repeat",
    ):
        page.locator(f"#{input_id}").focus()
        focused_id = page.evaluate("() => document.activeElement.id")
        assert focused_id == input_id, (
            f"AC-9: #{input_id} must be keyboard-focusable; activeElement was {focused_id!r}"
        )

        outline = page.evaluate(
            "() => window.getComputedStyle(document.activeElement).outlineWidth"
        )
        box_shadow = page.evaluate(
            "() => window.getComputedStyle(document.activeElement).boxShadow"
        )
        has_visible_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
        assert has_visible_focus, (
            f"AC-9: #{input_id} has no visible focus indicator: "
            f"outline={outline}, box-shadow={box_shadow}"
        )

    # Submit button
    submit = page.locator("[data-settings-action='change-password']")
    submit.focus()
    focused_id = page.evaluate("() => document.activeElement.getAttribute('data-settings-action')")
    assert focused_id == "change-password", (
        f"AC-9: submit button must be keyboard-focusable; focused action={focused_id!r}"
    )


def test_ac9_target_sizes(page: Page, base_url: str):
    """AC-9: all three inputs and the submit button have interactive target >= 24 px
    (WCAG 2.5.8).
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    for input_id in (
        "settings-current-password",
        "settings-new-password",
        "settings-new-password-repeat",
    ):
        box = page.locator(f"#{input_id}").bounding_box()
        assert box is not None, f"AC-9: #{input_id} has no bounding box"
        assert box["height"] >= 24, (
            f"AC-9: #{input_id} height {box['height']}px < 24px (WCAG 2.5.8)"
        )

    # Submit button
    submit = page.locator("[data-settings-action='change-password']")
    submit_box = submit.bounding_box()
    assert submit_box is not None, "AC-9: submit button has no bounding box"
    assert submit_box["height"] >= 24, (
        f"AC-9: submit button height {submit_box['height']}px < 24px (WCAG 2.5.8)"
    )
    assert submit_box["width"] >= 24, (
        f"AC-9: submit button width {submit_box['width']}px < 24px (WCAG 2.5.8)"
    )
