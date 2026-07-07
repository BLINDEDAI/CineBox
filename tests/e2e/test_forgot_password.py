"""Browser E2E tests for forgot-password-reset-flow (AC-1..AC-12).

Covers every ### Tester scope row in the task DoD:
  AC-1  — reset affordance renders in login mode; link reveals the email form
  AC-2  — registered-looking email submit -> generic confirmation message
  AC-3  — unregistered-looking email submit -> byte-identical confirmation
          (anti-enumeration); a forced resetPasswordForEmail error shows the
          SAME string too
  AC-4  — empty / malformed email -> field-level error, resetPasswordForEmail
          NOT called
  AC-5  — PASSWORD_RECOVERY event shows #password-recovery-screen, hides
          app/login
  AC-6  — new password < 8 chars -> too-short message, updateUser NOT called
  AC-7  — repeat mismatch -> mismatch message, updateUser NOT called
  AC-8  — valid matching new password -> success + fields cleared + routed to
          login; updateUser called with the new password value
  AC-9  — updateUser error (stands in for "no recovery session" too, since the
          production code has no separate branch — both surface only via a
          truthy updateUser error) -> generic expired-link message +
          #recovery-request-again visible -> returns to the request step; raw
          SDK error text never rendered
  AC-10 — no entered email / password value in location, post-success DOM, or
          console, across both success and failure paths
  AC-11 — regression: NOT a new test here — verified by running the full
          `pytest tests/e2e/` suite once at the end (see Tester handoff
          ## Test Results). The existing login / register / change-password /
          username-gate specs are untouched by this file.
  AC-12 — axe WCAG 2.2 A/AA (0 critical/serious) + keyboard operability +
          visible focus + labelled fields + targets >= 24 px on the reset form
          and the recovery screen, at 1280 px and 375 px

Strategy:
  - Real Cinephora server via conftest.py base_url fixture (no DB/auth required).
  - window.supabase is stubbed BEFORE any page script runs via
    page.add_init_script() (mirrors test_change_password.py's proven pattern):
    createClient() returns a controllable client whose auth.resetPasswordForEmail
    / auth.updateUser / auth.onAuthStateChange are stubbed, with call counters
    so zero-call guarantees (AC-4/AC-6/AC-7) can be asserted.
  - The vendor supabase-js bundle is routed to noop bytes so its SRI check fails
    and the browser blocks it, leaving our add_init_script stub in place as
    window.supabase (same rationale as test_change_password.py).
  - /api/config is routed to return non-empty stub creds so initApp() actually
    calls window.supabase.createClient(...) and wires the real onAuthStateChange
    listener from app.js -- the callback is captured into
    window.__stubState.authCallback so tests can fire a synthetic
    PASSWORD_RECOVERY event exactly the way the real SDK would.
  - The welcome-screen overlay is removed after navigation (same as every other
    e2e spec in this suite) so #login-screen can be revealed deterministically;
    _setLoginMode('login') is invoked via the production seam (same technique
    as test_choose_username_at_registration.py's _navigate_to_register_mode).
  - Call tracking uses JS-side counters injected into the stub so assertions do
    not need to intercept Supabase network calls (which are stubbed away
    anyway).
  - axe-core injected via vendored tests/e2e/axe.min.js as a same-origin routed
    <script> (CSP: script-src 'self') -- no new npm dependency.
  - Screenshots saved to handoffs/forgot-password-reset-flow/screenshots/.
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
    / "forgot-password-reset-flow"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Constants (must match app.js byte-for-byte — AC-2/AC-3/AC-6/AC-7/AC-9) ──────
GENERIC_RESET_MESSAGE = (
    "Si existe una cuenta con ese email, "
    "te hemos enviado un enlace para restablecer la contraseña."
)
TOO_SHORT_MESSAGE = "La nueva contraseña debe tener al menos 8 caracteres."
MISMATCH_MESSAGE = "Las contraseñas no coinciden."
EXPIRED_LINK_MESSAGE = "El enlace ha caducado o no es válido."
INVALID_EMAIL_MESSAGE = "Introduce un email válido."

# ── Route stubs ──────────────────────────────────────────────────────────────


def _route_config(page: Page, base_url: str):
    """Stub /api/config with non-empty creds so initApp() calls createClient()."""
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


def _route_vendor_supabase(page: Page, base_url: str):
    """Route the vendor supabase-js bundle to a noop script.

    The real vendor bundle is loaded synchronously in <head> with an SRI
    integrity hash. Serving different bytes fails the SRI check and the
    browser blocks the script -- it does NOT overwrite window.supabase that
    our add_init_script already set (same pattern as test_change_password.py).
    """
    noop_js = b"/* stub: supabase vendor noop for e2e tests */"

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/javascript",
            body=noop_js,
        )

    page.route(f"{base_url}/vendor/supabase-js/**", handle)


def _inject_supabase_stub(page: Page, reset_error=None, update_error=None):
    """Inject window.supabase stub BEFORE any page script runs (add_init_script).

    - createClient(...) returns a client whose auth methods are controllable:
      * resetPasswordForEmail(email, opts) -> counted, records email/redirectTo,
        errors iff `reset_error` is set (the caller-visible message is IGNORED
        by app.js -- see AC-2/AC-3 -- but we still let tests force it to prove
        that).
      * updateUser({password}) -> counted, records the submitted password,
        errors iff `update_error` is set.
      * onAuthStateChange(cb) -> stores `cb` on window.__stubState.authCallback
        so a test can fire a synthetic PASSWORD_RECOVERY event exactly the way
        app.js's own listener would receive it from the real SDK.
      * getSession() -> resolves no session (pre-auth landing state).
    - Call counters (resetCallCount, updateCallCount, createClientCallCount)
      let tests assert the zero-call guarantees (AC-4, AC-6, AC-7).
    """
    script = f"""
    (() => {{
        window.__stubState = {{
            resetError: {json.dumps(reset_error)},
            updateError: {json.dumps(update_error)},
            resetCallCount: 0,
            updateCallCount: 0,
            createClientCallCount: 0,
            authCallback: null,
            lastResetEmail: null,
            lastRedirectTo: null,
            lastUpdatePassword: null,
        }};

        const authObj = {{
            resetPasswordForEmail: async (email, opts) => {{
                window.__stubState.resetCallCount++;
                window.__stubState.lastResetEmail = email;
                window.__stubState.lastRedirectTo = opts && opts.redirectTo;
                const err = window.__stubState.resetError;
                return err ? {{ data: null, error: {{ message: err }} }} : {{ data: {{}}, error: null }};
            }},
            updateUser: async (params) => {{
                window.__stubState.updateCallCount++;
                window.__stubState.lastUpdatePassword = params && params.password;
                const err = window.__stubState.updateError;
                return err ? {{ data: null, error: {{ message: err }} }} : {{ data: {{ user: {{}} }}, error: null }};
            }},
            onAuthStateChange: (cb) => {{
                window.__stubState.authCallback = cb;
                return {{ data: {{ subscription: {{ unsubscribe: () => {{}} }} }} }};
            }},
            getSession: async () => ({{ data: {{ session: null }}, error: null }}),
            signOut: async () => ({{ error: null }}),
        }};

        const client = {{ auth: authObj }};

        window.supabase = {{
            createClient: (url, key, opts) => {{
                window.__stubState.createClientCallCount++;
                return client;
            }}
        }};
    }})();
    """
    page.add_init_script(script)


def _setup_page(page: Page, base_url: str, reset_error=None, update_error=None):
    """Full setup: inject stub, route config/vendor, navigate, reveal #login-screen.

    Order matters (mirrors test_change_password.py):
    1. add_init_script sets window.supabase stub BEFORE any page JS runs.
    2. /api/config is routed so initApp() actually calls createClient(),
       wiring the real onAuthStateChange listener (captured by the stub).
    3. The vendor bundle route serves noop bytes -> SRI mismatch -> browser
       blocks the real vendor script -> window.supabase stays as our stub.
    4. Navigate, wait for networkidle, remove the welcome-screen overlay, and
       drive #login-screen open via the production _setLoginMode('login') seam
       (same technique as test_choose_username_at_registration.py).
    """
    _inject_supabase_stub(page, reset_error=reset_error, update_error=update_error)
    _route_config(page, base_url)
    _route_vendor_supabase(page, base_url)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
            const ls = document.getElementById('login-screen');
            if (ls) ls.hidden = false;
            _setLoginMode('login');
        }"""
    )
    page.wait_for_selector("#login-forgot-link", state="visible", timeout=5000)


def _reveal_reset_form(page: Page):
    """Click '¿Olvidaste tu contraseña?' and wait for the reset-request form."""
    page.click("#login-forgot-link")
    page.wait_for_selector("#password-reset-form", state="visible", timeout=5000)


def _fire_password_recovery(page: Page, email: str = "user@example.com"):
    """Invoke the captured onAuthStateChange callback with a PASSWORD_RECOVERY
    event, exactly as the real SDK would when the app loads from the emailed
    deep-link. Waits for #password-recovery-screen to become visible (AC-5).
    """
    page.evaluate(
        """(emailAddr) => {
            if (window.__stubState && window.__stubState.authCallback) {
                window.__stubState.authCallback('PASSWORD_RECOVERY', { user: { email: emailAddr } });
            }
        }""",
        email,
    )
    page.wait_for_selector("#password-recovery-screen", state="visible", timeout=5000)


def _get_stub_counts(page: Page) -> dict:
    return page.evaluate(
        """() => ({
            resetCallCount: window.__stubState.resetCallCount,
            updateCallCount: window.__stubState.updateCallCount,
            createClientCallCount: window.__stubState.createClientCallCount,
            lastResetEmail: window.__stubState.lastResetEmail,
            lastRedirectTo: window.__stubState.lastRedirectTo,
            lastUpdatePassword: window.__stubState.lastUpdatePassword,
        })"""
    )


def _reset_stub_counts(page: Page):
    page.evaluate(
        """() => {
            window.__stubState.resetCallCount = 0;
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


# ── AC-1: reset affordance renders in login mode + link reveals the form ───────


def test_ac1_forgot_link_visible_in_login_mode(page: Page, base_url: str):
    """AC-1: '¿Olvidaste tu contraseña?' is shown on #login-screen in login mode."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _screenshot(page, "ac1-forgot-link-visible")

    link = page.locator("#login-forgot-link")
    assert link.count() == 1, "AC-1: #login-forgot-link must exist"
    assert link.is_visible(), "AC-1: #login-forgot-link must be visible in login mode"


def test_ac1_forgot_link_reveals_email_form(page: Page, base_url: str):
    """AC-1: activating the link reveals the email input + submit control,
    and hides the sign-in form."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    _reveal_reset_form(page)
    _screenshot(page, "ac1-reset-form-revealed")

    reset_form = page.locator("#password-reset-form")
    assert reset_form.is_visible(), "AC-1: #password-reset-form must be visible after clicking the link"

    email_input = page.locator("#reset-email")
    assert email_input.count() == 1, "AC-1: #reset-email must exist"
    assert email_input.get_attribute("type") == "email", "AC-1: #reset-email must be type='email'"

    submit_btn = page.locator("#reset-submit")
    assert submit_btn.is_visible(), "AC-1: #reset-submit must be visible"

    # The sign-in form is hidden while the reset-request form is shown.
    login_form = page.locator("#login-form")
    assert login_form.is_hidden(), "AC-1: #login-form must be hidden while the reset form is shown"

    # Associated label for the email field (a11y precondition also covered by AC-12).
    label = page.locator("label[for='reset-email']")
    assert label.count() >= 1, "AC-1: missing <label for='reset-email'>"


# ── AC-2/AC-3: anti-enumeration -- byte-identical confirmation on every path ────


def test_ac2_registered_email_shows_generic_confirmation(page: Page, base_url: str):
    """AC-2: submitting a well-formed (registered-looking) email shows the
    exact generic confirmation string, and resetPasswordForEmail is called."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    _reset_stub_counts(page)
    page.fill("#reset-email", "registered@example.com")
    page.click("#reset-submit")
    page.wait_for_timeout(500)

    _screenshot(page, "ac2-registered-email-confirmation")

    hint_text = page.locator("#reset-hint").inner_text()
    assert hint_text == GENERIC_RESET_MESSAGE, (
        f"AC-2: expected the exact generic message, got {hint_text!r}"
    )

    counts = _get_stub_counts(page)
    assert counts["resetCallCount"] == 1, (
        f"AC-2: resetPasswordForEmail must be called once; got {counts['resetCallCount']}"
    )
    assert counts["lastResetEmail"] == "registered@example.com"
    # redirectTo must resolve to the app's own origin (open-redirect guard, SE-*).
    assert counts["lastRedirectTo"] == base_url, (
        f"AC-2/SE-*: redirectTo must be the app's own origin ({base_url!r}), "
        f"got {counts['lastRedirectTo']!r}"
    )


def test_ac3_unregistered_email_shows_byte_identical_confirmation(page: Page, base_url: str):
    """AC-3: submitting an unregistered-looking email shows the EXACT SAME
    string as AC-2 -- no existence signal (anti-enumeration)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    _reset_stub_counts(page)
    page.fill("#reset-email", "definitely-not-registered@example.com")
    page.click("#reset-submit")
    page.wait_for_timeout(500)

    _screenshot(page, "ac3-unregistered-email-confirmation")

    hint_text = page.locator("#reset-hint").inner_text()
    assert hint_text == GENERIC_RESET_MESSAGE, (
        f"AC-3: expected the byte-identical generic message, got {hint_text!r}"
    )
    assert hint_text == GENERIC_RESET_MESSAGE, "AC-3: message must equal AC-2's message exactly"

    counts = _get_stub_counts(page)
    assert counts["resetCallCount"] == 1, (
        "AC-3: resetPasswordForEmail must still be called for an unregistered email"
    )


def test_ac3_sdk_error_shows_same_generic_confirmation(page: Page, base_url: str):
    """AC-3 (edge case): resetPasswordForEmail throwing/erroring must NOT change
    the shown message -- it stays the identical generic confirmation, never a
    raw SDK error (anti-enumeration + no-leak of internals)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, reset_error="Some internal SDK failure detail")
    _reveal_reset_form(page)

    _reset_stub_counts(page)
    page.fill("#reset-email", "whatever@example.com")
    page.click("#reset-submit")
    page.wait_for_timeout(500)

    _screenshot(page, "ac3-sdk-error-same-confirmation")

    hint_text = page.locator("#reset-hint").inner_text()
    assert hint_text == GENERIC_RESET_MESSAGE, (
        f"AC-3: a forced SDK error must still show the identical generic message; got {hint_text!r}"
    )
    assert "Some internal SDK failure detail" not in hint_text, (
        "AC-3: the raw SDK error must never be shown"
    )

    counts = _get_stub_counts(page)
    assert counts["resetCallCount"] == 1, "AC-3: resetPasswordForEmail must be called even though it errors"


# ── AC-4: empty / malformed email -> field error, ZERO Supabase calls ──────────


def test_ac4_empty_email_blocked_client_side(page: Page, base_url: str):
    """AC-4: an empty email is blocked before any Supabase call."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    _reset_stub_counts(page)
    page.fill("#reset-email", "")
    page.click("#reset-submit")
    page.wait_for_timeout(300)

    _screenshot(page, "ac4-empty-email")

    hint_text = page.locator("#reset-hint").inner_text()
    assert hint_text == INVALID_EMAIL_MESSAGE, (
        f"AC-4: expected {INVALID_EMAIL_MESSAGE!r} for an empty email; got {hint_text!r}"
    )

    counts = _get_stub_counts(page)
    assert counts["resetCallCount"] == 0, (
        f"AC-4: resetPasswordForEmail must NOT be called for an empty email; "
        f"got resetCallCount={counts['resetCallCount']}"
    )


def test_ac4_malformed_email_blocked_client_side(page: Page, base_url: str):
    """AC-4: a malformed email (no '@', no domain) is blocked before any Supabase call."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    _reset_stub_counts(page)
    page.fill("#reset-email", "not-an-email")
    page.click("#reset-submit")
    page.wait_for_timeout(300)

    _screenshot(page, "ac4-malformed-email")

    hint_text = page.locator("#reset-hint").inner_text()
    assert hint_text == INVALID_EMAIL_MESSAGE, (
        f"AC-4: expected {INVALID_EMAIL_MESSAGE!r} for a malformed email; got {hint_text!r}"
    )

    counts = _get_stub_counts(page)
    assert counts["resetCallCount"] == 0, (
        f"AC-4: resetPasswordForEmail must NOT be called for a malformed email; "
        f"got resetCallCount={counts['resetCallCount']}"
    )


# ── AC-5: PASSWORD_RECOVERY event shows the recovery screen ────────────────────


def test_ac5_password_recovery_event_shows_recovery_screen(page: Page, base_url: str):
    """AC-5: firing a PASSWORD_RECOVERY event shows #password-recovery-screen
    and hides the login screen (no normal authenticated landing)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)

    _fire_password_recovery(page)
    _screenshot(page, "ac5-password-recovery-screen")

    recovery_screen = page.locator("#password-recovery-screen")
    assert recovery_screen.is_visible(), "AC-5: #password-recovery-screen must be shown"

    login_screen = page.locator("#login-screen")
    assert login_screen.is_hidden(), "AC-5: #login-screen must be hidden"

    # No authenticated landing revealed underneath (AC-5/AC-11 routing guard).
    is_authed = page.evaluate(
        "() => document.documentElement.classList.contains('cinephora-authed')"
    )
    assert not is_authed, "AC-5: the authenticated app shell must not be revealed during recovery"


# ── AC-6/AC-7: client-side validation on the new-password form ─────────────────


def test_ac6_new_password_too_short(page: Page, base_url: str):
    """AC-6: new password shorter than 8 chars -> too-short message, ZERO updateUser calls."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _fire_password_recovery(page)

    page.fill("#recovery-new-password", "short1")
    page.fill("#recovery-new-password-repeat", "short1")
    _reset_stub_counts(page)
    page.click("#recovery-submit")
    page.wait_for_timeout(300)

    _screenshot(page, "ac6-password-too-short")

    hint_text = page.locator("#recovery-hint").inner_text()
    assert hint_text == TOO_SHORT_MESSAGE, (
        f"AC-6: expected {TOO_SHORT_MESSAGE!r}; got {hint_text!r}"
    )

    counts = _get_stub_counts(page)
    assert counts["updateCallCount"] == 0, (
        f"AC-6: updateUser must NOT be called for a too-short password; "
        f"got updateCallCount={counts['updateCallCount']}"
    )


def test_ac7_new_password_repeat_mismatch(page: Page, base_url: str):
    """AC-7: repeat does not match the new password -> mismatch message, ZERO updateUser calls."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _fire_password_recovery(page)

    page.fill("#recovery-new-password", "validPassword1!")
    page.fill("#recovery-new-password-repeat", "differentPassword2!")
    _reset_stub_counts(page)
    page.click("#recovery-submit")
    page.wait_for_timeout(300)

    _screenshot(page, "ac7-password-mismatch")

    hint_text = page.locator("#recovery-hint").inner_text()
    assert hint_text == MISMATCH_MESSAGE, (
        f"AC-7: expected {MISMATCH_MESSAGE!r}; got {hint_text!r}"
    )

    counts = _get_stub_counts(page)
    assert counts["updateCallCount"] == 0, (
        f"AC-7: updateUser must NOT be called on a repeat mismatch; "
        f"got updateCallCount={counts['updateCallCount']}"
    )


# ── AC-8: valid matching new password -> success, cleared fields, routed to login ─


def test_ac8_valid_new_password_success(page: Page, base_url: str):
    """AC-8: a valid, matching new password on a valid recovery session ->
    success confirmation, fields cleared, routed to the login screen;
    updateUser is called with the new (usable) password value."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, update_error=None)
    _fire_password_recovery(page)

    console_logs = []
    page.on("console", lambda msg: console_logs.append(msg.text))

    new_password = "brandNewPassword9!"
    page.fill("#recovery-new-password", new_password)
    page.fill("#recovery-new-password-repeat", new_password)
    _reset_stub_counts(page)
    page.click("#recovery-submit")
    page.wait_for_timeout(800)

    _screenshot(page, "ac8-success")

    # Routed to the login screen; recovery screen hidden.
    assert page.locator("#password-recovery-screen").is_hidden(), (
        "AC-8: #password-recovery-screen must be hidden after success"
    )
    assert page.locator("#login-screen").is_visible(), (
        "AC-8: #login-screen must be shown after success"
    )

    # Confirmation shown.
    success_text = page.locator("#login-success").inner_text()
    assert success_text, "AC-8: a success confirmation must be shown"
    assert "contraseña" in success_text.lower(), (
        f"AC-8: success message should reference 'contraseña'; got {success_text!r}"
    )

    # updateUser called with the new password (proves it is the usable value).
    counts = _get_stub_counts(page)
    assert counts["updateCallCount"] == 1, (
        f"AC-8: updateUser must be called exactly once; got {counts['updateCallCount']}"
    )
    assert counts["lastUpdatePassword"] == new_password, (
        "AC-8: updateUser must be called with the exact new password value"
    )

    # _passwordRecovery cleared so a normal session lifecycle can resume.
    still_recovery = page.evaluate("() => _passwordRecovery")
    assert still_recovery is False, "AC-8: _passwordRecovery must be reset to false after success"

    # Fields cleared (form.reset()-equivalent -- the code clears .value directly).
    assert page.input_value("#recovery-new-password") == "", (
        "AC-8: #recovery-new-password must be cleared after success"
    )
    assert page.input_value("#recovery-new-password-repeat") == "", (
        "AC-8: #recovery-new-password-repeat must be cleared after success"
    )

    # AC-10 (co-verified here on the success path): no password value leaked.
    current_url = page.url
    assert new_password not in current_url, (
        f"AC-10: password value must not appear in the URL: {current_url}"
    )
    body_text = page.evaluate("() => document.body.innerText")
    assert new_password not in body_text, "AC-10: password value must not appear in the post-success DOM"
    console_text = " ".join(console_logs)
    assert new_password not in console_text, "AC-10: password value must not appear in console logs"


# ── AC-9: updateUser error (stands in for "no recovery session" too) ───────────


def test_ac9_expired_or_invalid_link_shows_generic_message(page: Page, base_url: str):
    """AC-9: updateUser erroring (expired/invalid recovery token, or -- in this
    implementation -- the equivalent no-recovery-session case, since there is
    no separate code branch for the two) -> generic expired-link message,
    #recovery-request-again revealed, raw SDK error never shown; clicking
    "pedir un nuevo enlace" returns to the reset-request step."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, update_error="recovery token invalid or expired XYZ")
    _fire_password_recovery(page)

    valid_password = "wouldBeValid1!"
    page.fill("#recovery-new-password", valid_password)
    page.fill("#recovery-new-password-repeat", valid_password)
    _reset_stub_counts(page)
    page.click("#recovery-submit")
    page.wait_for_timeout(800)

    _screenshot(page, "ac9-expired-link")

    hint_text = page.locator("#recovery-hint").inner_text()
    assert hint_text == EXPIRED_LINK_MESSAGE, (
        f"AC-9: expected {EXPIRED_LINK_MESSAGE!r}; got {hint_text!r}"
    )
    assert "recovery token invalid or expired XYZ" not in hint_text, (
        "AC-9: the raw SDK error must never be shown"
    )

    again_btn = page.locator("#recovery-request-again")
    assert again_btn.is_visible(), "AC-9: #recovery-request-again must be revealed"

    # Clicking it returns to the reset-request step.
    again_btn.click()
    page.wait_for_timeout(300)
    _screenshot(page, "ac9-back-to-request-step")

    assert page.locator("#login-screen").is_visible(), (
        "AC-9: clicking 'pedir un nuevo enlace' must return to the login screen"
    )
    assert page.locator("#password-reset-form").is_visible(), (
        "AC-9: clicking 'pedir un nuevo enlace' must reveal the reset-request form again"
    )
    assert page.locator("#password-recovery-screen").is_hidden(), (
        "AC-9: the recovery screen must be hidden after returning to the request step"
    )


# ── AC-10: no email/password leak (URL, DOM, console) -- failure paths ─────────


def test_ac10_no_email_leak_on_reset_request(page: Page, base_url: str):
    """AC-10: the typed reset-request email never appears in the URL, the DOM
    after the confirmation is shown, or console logs."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    console_logs = []
    page.on("console", lambda msg: console_logs.append(msg.text))

    email = "leaky-marker-address@example.com"
    page.fill("#reset-email", email)
    page.click("#reset-submit")
    page.wait_for_timeout(500)

    current_url = page.url
    assert email not in current_url, f"AC-10: email must not appear in the URL: {current_url}"

    body_text = page.evaluate("() => document.body.innerText")
    assert email not in body_text, "AC-10: email must not appear in the post-submit DOM"

    console_text = " ".join(console_logs)
    assert email not in console_text, "AC-10: email must not appear in console logs"


def test_ac10_no_password_leak_on_expired_link_error(page: Page, base_url: str):
    """AC-10: the typed new-password value never appears in the URL, the DOM
    after the error is shown, or console logs -- error path."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url, update_error="expired")
    _fire_password_recovery(page)

    console_logs = []
    page.on("console", lambda msg: console_logs.append(msg.text))

    pw = "leakyMarkerPassword9!"
    page.fill("#recovery-new-password", pw)
    page.fill("#recovery-new-password-repeat", pw)
    page.click("#recovery-submit")
    page.wait_for_timeout(500)

    current_url = page.url
    assert pw not in current_url, f"AC-10: password must not appear in the URL: {current_url}"

    body_text = page.evaluate("() => document.body.innerText")
    assert pw not in body_text, "AC-10: password must not appear in the post-error DOM"

    console_text = " ".join(console_logs)
    assert pw not in console_text, "AC-10: password must not appear in console logs"


def test_ac10_password_inputs_are_masked(page: Page, base_url: str):
    """AC-10 (supporting check): both recovery-form password inputs are
    type='password' (masked in the browser)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _fire_password_recovery(page)

    for input_id in ("recovery-new-password", "recovery-new-password-repeat"):
        input_type = page.locator(f"#{input_id}").get_attribute("type")
        assert input_type == "password", (
            f"AC-10: #{input_id} must be type='password' to mask the value; got {input_type!r}"
        )


# ── AC-12: axe WCAG 2.2 A/AA + keyboard operability + target size ──────────────


def test_ac12_axe_reset_form_desktop(page: Page, base_url: str):
    """AC-12: the reset-request form passes axe WCAG 2.2 A/AA at 1280 px desktop."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#login-screen")
    _screenshot(page, "ac12-axe-reset-form-desktop")

    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s) on "
        f"the reset form (desktop 1280px): " + json.dumps(violations, indent=2)
    )


def test_ac12_axe_reset_form_mobile(page: Page, base_url: str):
    """AC-12: the reset-request form passes axe WCAG 2.2 A/AA at 375 px mobile."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#login-screen")
    _screenshot(page, "ac12-axe-reset-form-mobile")

    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s) on "
        f"the reset form (mobile 375px): " + json.dumps(violations, indent=2)
    )


def test_ac12_axe_recovery_screen_desktop(page: Page, base_url: str):
    """AC-12: the recovery new-password screen passes axe WCAG 2.2 A/AA at 1280 px."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _fire_password_recovery(page)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#password-recovery-screen")
    _screenshot(page, "ac12-axe-recovery-screen-desktop")

    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s) on "
        f"the recovery screen (desktop 1280px): " + json.dumps(violations, indent=2)
    )


def test_ac12_axe_recovery_screen_mobile(page: Page, base_url: str):
    """AC-12: the recovery new-password screen passes axe WCAG 2.2 A/AA at 375 px."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_page(page, base_url)
    _fire_password_recovery(page)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#password-recovery-screen")
    _screenshot(page, "ac12-axe-recovery-screen-mobile")

    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s) on "
        f"the recovery screen (mobile 375px): " + json.dumps(violations, indent=2)
    )


def test_ac12_keyboard_operability_and_visible_focus(page: Page, base_url: str):
    """AC-12: reset-email, reset-submit, both recovery password inputs, and
    recovery-submit are keyboard-focusable with a visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    def _assert_focusable_with_visible_focus(selector: str, expected_id: str):
        page.locator(selector).focus()
        focused_id = page.evaluate("() => document.activeElement.id")
        assert focused_id == expected_id, (
            f"AC-12: {selector} must be keyboard-focusable; activeElement was {focused_id!r}"
        )
        outline = page.evaluate("() => window.getComputedStyle(document.activeElement).outlineWidth")
        box_shadow = page.evaluate("() => window.getComputedStyle(document.activeElement).boxShadow")
        has_visible_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
        assert has_visible_focus, (
            f"AC-12: {selector} has no visible focus indicator: outline={outline}, box-shadow={box_shadow}"
        )

    _assert_focusable_with_visible_focus("#reset-email", "reset-email")
    _assert_focusable_with_visible_focus("#reset-submit", "reset-submit")

    _fire_password_recovery(page)
    _assert_focusable_with_visible_focus("#recovery-new-password", "recovery-new-password")
    _assert_focusable_with_visible_focus("#recovery-new-password-repeat", "recovery-new-password-repeat")
    _assert_focusable_with_visible_focus("#recovery-submit", "recovery-submit")


def test_ac12_target_sizes(page: Page, base_url: str):
    """AC-12: all interactive targets on the reset form and the recovery
    screen are >= 24 px (WCAG 2.5.8)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_page(page, base_url)
    _reveal_reset_form(page)

    for selector in ("#reset-email", "#reset-submit"):
        box = page.locator(selector).bounding_box()
        assert box is not None, f"AC-12: {selector} has no bounding box"
        assert box["height"] >= 24, f"AC-12: {selector} height {box['height']}px < 24px (WCAG 2.5.8)"
        assert box["width"] >= 24, f"AC-12: {selector} width {box['width']}px < 24px (WCAG 2.5.8)"

    _fire_password_recovery(page)
    for selector in (
        "#recovery-new-password",
        "#recovery-new-password-repeat",
        "#recovery-submit",
    ):
        box = page.locator(selector).bounding_box()
        assert box is not None, f"AC-12: {selector} has no bounding box"
        assert box["height"] >= 24, f"AC-12: {selector} height {box['height']}px < 24px (WCAG 2.5.8)"
        assert box["width"] >= 24, f"AC-12: {selector} width {box['width']}px < 24px (WCAG 2.5.8)"
