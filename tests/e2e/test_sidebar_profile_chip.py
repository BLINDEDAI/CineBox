"""Browser E2E tests for sidebar-profile-chip (AC-1..AC-7).

Covers:
  AC-1 — authenticated + username: chip shows generated avatar + username in the
         sidebar footer near the email / "Cerrar sesión" controls.
  AC-2 — username + public profile: clicking the chip navigates to /u/<username>.
  AC-3 — username + private profile: clicking opens the sharing settings view and
         does NOT navigate to /u/<username>.
  AC-4 — no username: chip shows an invite (not a handle); clicking opens the
         sharing settings view and never navigates to a /u/ URL.
  AC-5 — deterministic avatar: same username yields identical initials + gradient,
         generated client-side (via page.evaluate over the app.js helpers).
  AC-6 — automated axe WCAG 2.2 A/AA scan (zero critical/serious) on the
         route-stubbed sidebar, desktop 1280px + mobile 375px.
  AC-7 — accessible control assertions: <button>, keyboard-operable, visible
         focus, target ≥ 24px, accessible name "Tu perfil, {username}" (and the
         invite name), avatar aria-hidden, no container ARIA role.

Strategy:
  - The real CineBox server is booted (conftest.py base_url fixture), no DB/auth.
  - The authenticated chip states cannot be reached by a real login, so we stub
    GET /api/profile via page.route to return each profile state, and mount the
    chip by driving the app.js seam (_updateSidebarUser + _loadProfileChip)
    directly via page.evaluate.
  - axe-core (4.9.0) is injected via a same-origin routed <script> (CSP:
    script-src 'self'), exactly as test_public_profiles_a11y.py does. node is NOT
    installed in this environment; axe is the vendored tests/e2e/axe.min.js — no
    new dependency is introduced.
  - The a11y rows (AC-6 axe, AC-7 control assertions) are AUTOMATED here, never
    deferred to manual (lessons-learned/general.md "deferred AC gate").
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
    / "sidebar-profile-chip"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_USERNAME = "testuser"

_PROFILE_PUBLIC = {
    "ok": True,
    "profile": {
        "username": _USERNAME,
        "is_public": True,
        "show_collection": True,
        "show_stats": True,
    },
}
_PROFILE_PRIVATE = {
    "ok": True,
    "profile": {
        "username": _USERNAME,
        "is_public": False,
        "show_collection": False,
        "show_stats": False,
    },
}
_PROFILE_NO_USERNAME = {
    "ok": True,
    "profile": {
        "username": None,
        "is_public": False,
        "show_collection": False,
        "show_stats": False,
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _route_config(page: Page, base_url: str):
    """Stub /api/config so initApp() runs without real Supabase credentials."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"supabase_url": "", "supabase_anon_key": ""}),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_profile(page: Page, base_url: str, payload: dict):
    """Stub GET /api/profile so the chip mounts without a DB/session."""

    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(f"{base_url}/api/profile", handle)


def _mount_chip(page: Page, base_url: str, payload: dict):
    """Navigate to the SPA and drive the sidebar-user seam so the chip renders.

    _updateSidebarUser(email) is the production seam; it calls _loadProfileChip()
    which fetches the (stubbed) /api/profile and renders the chip. We invoke it
    directly (await the async chip load) since there is no real Supabase session.
    """
    _route_config(page, base_url)
    _route_profile(page, base_url, payload)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """async () => {
            // Remove the first-visit welcome overlay so it does not intercept
            // pointer events over the sidebar (it is shown before any session).
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
            _updateSidebarUser('user@example.com');
            // _updateSidebarUser fires _loadProfileChip() (async, not awaited
            // internally); call it again and await so the chip is rendered.
            await _loadProfileChip();
        }"""
    )
    # Wait until the chip has been rendered (aria-label set by _renderProfileChip).
    # On mobile (≤720px) the sidebar footer is display:none by design, so the chip
    # is present in the DOM but not "visible"; we wait for the rendered marker
    # rather than visibility so both viewports work.
    page.locator("#profile-chip[aria-label]").wait_for(state="attached", timeout=5000)


def _inject_axe(page: Page, base_url: str):
    """Inject axe-core via a same-origin routed <script> (CSP: script-src 'self').

    Mirrors test_public_profiles_a11y.py: page.add_script_tag(path=...) injects an
    inline script that CSP blocks, so we route a same-origin URL to serve the
    local axe.min.js bytes and load it as <script src=...>.
    """
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
    """Run axe-core, return violations filtered to critical/serious impact."""
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
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


# ── AC-1: display + placement ─────────────────────────────────────────────────


def test_chip_displays_avatar_and_username(page: Page, base_url: str):
    """AC-1: chip shows generated avatar + username in the sidebar footer."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _mount_chip(page, base_url, _PROFILE_PUBLIC)

    chip = page.locator("#profile-chip")
    assert chip.is_visible(), "AC-1: chip not visible after authentication"

    # Username text present
    label = page.locator("#profile-chip .profile-chip-label")
    assert label.inner_text() == _USERNAME, "AC-1: chip label is not the username"

    # Generated avatar present with initials
    avatar = page.locator("#profile-chip .profile-chip-avatar")
    assert avatar.count() == 1, "AC-1: avatar element missing"
    assert avatar.inner_text() == "TE", "AC-1: avatar initials wrong"

    # Placement: chip is inside .side-footer near email + logout
    in_footer = page.evaluate(
        "() => !!document.getElementById('profile-chip').closest('.side-footer')"
    )
    assert in_footer, "AC-1: chip is not in the sidebar footer"
    siblings = page.evaluate(
        """() => {
            const f = document.querySelector('.side-footer');
            return ['profile-chip','sidebar-user-email','logout-btn']
                .every(id => !!f.querySelector('#' + id));
        }"""
    )
    assert siblings, "AC-1: footer missing chip/email/logout grouping"
    _screenshot(page, "chip-public")


# ── AC-2: username + public → /u/<username> ───────────────────────────────────


def test_chip_public_navigates_to_public_profile(page: Page, base_url: str):
    """AC-2: clicking the chip (public) navigates to /u/<username>."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _mount_chip(page, base_url, _PROFILE_PUBLIC)

    # Stub the public page navigation target so we don't depend on its render.
    page.route(
        f"{base_url}/u/{_USERNAME}",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>pub</title>",
        ),
    )

    with page.expect_navigation(url=f"{base_url}/u/{_USERNAME}"):
        page.locator("#profile-chip").click()

    assert page.url == f"{base_url}/u/{_USERNAME}", (
        f"AC-2: expected /u/{_USERNAME}, got {page.url}"
    )


# ── AC-3: username + private → sharing settings (not /u/) ──────────────────────


def test_chip_private_opens_sharing_view(page: Page, base_url: str):
    """AC-3: clicking the chip (private) opens sharing settings, not /u/."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _mount_chip(page, base_url, _PROFILE_PRIVATE)

    navigated = {"u": False}
    page.on(
        "framenavigated",
        lambda f: navigated.__setitem__("u", navigated["u"] or "/u/" in f.url),
    )

    page.locator("#profile-chip").click()
    page.wait_for_timeout(300)

    active = page.evaluate("() => document.body.dataset.activeView")
    assert active == "sharing-view", f"AC-3: expected sharing-view, got {active}"
    assert not navigated["u"], (
        "AC-3: must NOT navigate to a /u/ URL for a private profile"
    )


# ── AC-4: no username → invite + sharing settings, never /u/ ───────────────────


def test_chip_no_username_shows_invite_and_opens_sharing(page: Page, base_url: str):
    """AC-4 (updated for choose-username-at-registration): when username is null,
    the blocking #username-gate is shown instead of the old invite chip.

    The choose-username-at-registration feature (2026-06-30) supersedes the old
    invite-chip path: _loadProfileChip() now calls _claimOrGateUsername() when
    username is null, which shows the gate — the profile chip is not rendered for
    the no-username state any more. This test is updated to assert the new expected
    behaviour (gate visible, chip absent) instead of the now-stale invite chip.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_profile(page, base_url, _PROFILE_NO_USERNAME)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
            _currentUser = null;  // no desired_username -> straight to gate
            _updateSidebarUser('user@example.com');
            // Do NOT call _loadProfileChip() a second time; _updateSidebarUser already
            // called it internally, consuming the single GET route slot.
        }"""
    )
    # Wait for the gate to appear
    page.wait_for_function(
        "() => !document.getElementById('username-gate').hidden",
        timeout=5000,
    )

    # Gate is visible; profile chip is NOT rendered for no-username state
    gate_hidden = page.evaluate("() => document.getElementById('username-gate').hidden")
    assert not gate_hidden, (
        "AC-4 (updated): #username-gate must be visible when username is null"
    )
    # No navigated /u/ URL (same invariant as before)
    navigated = {"u": False}
    page.on(
        "framenavigated",
        lambda f: navigated.__setitem__("u", navigated["u"] or "/u/" in f.url),
    )
    page.wait_for_timeout(200)
    assert not navigated["u"], "AC-4: must NEVER navigate to a /u/ URL with no username"
    _screenshot(page, "chip-no-username")


# ── AC-5: deterministic avatar (via page.evaluate over the helpers) ────────────


def test_avatar_helpers_deterministic(page: Page, base_url: str):
    """AC-5: same username → identical initials + gradient; client-side only."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    out = page.evaluate(
        """() => ({
            initials_a: _avatarInitials('testuser'),
            initials_b: _avatarInitials('testuser'),
            grad_a: _avatarGradient('testuser'),
            grad_b: _avatarGradient('testuser'),
            grad_other: _avatarGradient('someoneelse'),
            init_one: _avatarInitials('x'),
            init_empty: _avatarInitials(''),
            init_null: _avatarInitials(null),
            grad_empty: _avatarGradient(''),
        })"""
    )

    # Determinism
    assert out["initials_a"] == out["initials_b"] == "TE", (
        "AC-5: initials not deterministic"
    )
    assert out["grad_a"] == out["grad_b"], "AC-5: gradient not deterministic"
    # Different username → (generally) different gradient
    assert out["grad_other"] != out["grad_a"], (
        "AC-5: distinct usernames share a gradient"
    )
    # It is a CSS gradient (generated client-side, no uploaded image)
    assert out["grad_a"].startswith("linear-gradient("), (
        "AC-5: not a generated gradient"
    )
    # Edge cases handled without error
    assert out["init_one"] == "X", "AC-5: 1-char username mishandled"
    assert out["init_empty"] == "?", "AC-5: empty username mishandled"
    assert out["init_null"] == "?", "AC-5: null username mishandled"
    assert out["grad_empty"].startswith("linear-gradient("), (
        "AC-5: empty gradient mishandled"
    )


# ── AC-6: automated axe WCAG 2.2 A/AA (desktop + mobile) ──────────────────────


def test_chip_a11y_axe_desktop(page: Page, base_url: str):
    """AC-6: route-stubbed sidebar (with chip), desktop 1280px, zero crit/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _mount_chip(page, base_url, _PROFILE_PUBLIC)

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "chip-axe-desktop")

    assert violations == [], (
        f"AC-6: axe found {len(violations)} critical/serious violations on desktop: "
        + json.dumps(violations, indent=2)
    )


def test_chip_a11y_axe_mobile(page: Page, base_url: str):
    """AC-6: route-stubbed sidebar (with chip), mobile 375px, zero crit/serious.

    At ≤720px the whole .side-footer (chip + email + logout) is display:none by
    design (pre-existing responsive rule). To actually scan the chip markup at a
    375px width — rather than have axe skip a hidden subtree — we reveal the
    footer via CSSOM for the duration of the scan. This keeps the chip subject to
    the mobile axe gate instead of silently skipped.
    """
    page.set_viewport_size({"width": 375, "height": 667})
    _mount_chip(page, base_url, _PROFILE_PUBLIC)

    page.evaluate(
        """() => {
            const f = document.querySelector('.side-footer');
            if (f) f.style.display = 'block';
            const c = document.getElementById('profile-chip');
            if (c) c.style.display = 'flex';
        }"""
    )
    page.locator("#profile-chip").wait_for(state="visible", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "chip-axe-mobile")

    assert violations == [], (
        f"AC-6: axe found {len(violations)} critical/serious violations on mobile: "
        + json.dumps(violations, indent=2)
    )


def test_chip_a11y_axe_no_username_state(page: Page, base_url: str):
    """AC-6 (updated for choose-username-at-registration): the no-username state now
    shows the blocking #username-gate instead of the invite chip. We scan the gate's
    WCAG 2.2 A/AA compliance here (zero critical/serious violations).

    The choose-username-at-registration feature (2026-06-30) replaced the invite chip
    with a blocking gate for all username=null users. The gate axe scan is fully covered
    by test_choose_username_at_registration.py::test_ac8_username_gate_axe_desktop.
    This test is updated to use the gate state so the full e2e suite stays coherent.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_profile(page, base_url, _PROFILE_NO_USERNAME)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")

    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
            _currentUser = null;
            _updateSidebarUser('user@example.com');
        }"""
    )
    page.wait_for_function(
        "() => !document.getElementById('username-gate').hidden",
        timeout=5000,
    )

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#username-gate")

    assert violations == [], (
        f"AC-6 (updated): axe found {len(violations)} critical/serious violations "
        f"on #username-gate (no-username state): "
        + json.dumps(violations, indent=2)
    )


# ── AC-7: accessible control assertions ───────────────────────────────────────


def test_chip_is_accessible_button(page: Page, base_url: str):
    """AC-7: <button>, accessible name, decorative avatar, no container role."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _mount_chip(page, base_url, _PROFILE_PUBLIC)

    chip = page.locator("#profile-chip")

    # Real <button>
    tag = page.evaluate("() => document.getElementById('profile-chip').tagName")
    assert tag == "BUTTON", f"AC-7: chip must be a <button>, got {tag}"

    # Accessible name "Tu perfil, {username}"
    assert chip.get_attribute("aria-label") == f"Tu perfil, {_USERNAME}", (
        "AC-7: accessible name (aria-label) wrong"
    )

    # No container ARIA role on the chip or the avatar
    assert chip.get_attribute("role") is None, (
        "AC-7: chip must not carry a container role"
    )
    avatar_role = page.evaluate(
        "() => document.querySelector('#profile-chip .profile-chip-avatar').getAttribute('role')"
    )
    assert avatar_role is None, "AC-7: avatar must not carry a container role"

    # Decorative avatar (aria-hidden)
    avatar_hidden = page.evaluate(
        "() => document.querySelector('#profile-chip .profile-chip-avatar')"
        ".getAttribute('aria-hidden')"
    )
    assert avatar_hidden == "true", "AC-7: avatar must be aria-hidden"


def test_chip_keyboard_focus_and_target_size(page: Page, base_url: str):
    """AC-7: keyboard-operable with a visible focus indicator, target ≥ 24px."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _mount_chip(page, base_url, _PROFILE_PUBLIC)

    chip = page.locator("#profile-chip")

    # Target size ≥ 24px (WCAG 2.5.8)
    box = chip.bounding_box()
    assert box is not None, "AC-7: chip has no bounding box"
    assert box["width"] >= 24, f"AC-7: chip width {box['width']}px < 24px"
    assert box["height"] >= 24, f"AC-7: chip height {box['height']}px < 24px"

    # Keyboard-operable: focusable and triggers its handler via Enter.
    chip.focus()
    focused_id = page.evaluate("() => document.activeElement.id")
    assert focused_id == "profile-chip", "AC-7: chip is not keyboard-focusable"

    # Visible focus (global :focus-visible outline OR box-shadow)
    outline = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).boxShadow"
    )
    has_visible_focus = outline not in ("0px", "") or box_shadow not in ("none", "")
    assert has_visible_focus, (
        f"AC-7: no visible focus on the chip: outline={outline}, box-shadow={box_shadow}"
    )

    # Enter activates the chip (public → navigation away).
    page.route(
        f"{base_url}/u/{_USERNAME}",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>pub</title>",
        ),
    )
    with page.expect_navigation(url=f"{base_url}/u/{_USERNAME}"):
        page.keyboard.press("Enter")
    assert page.url == f"{base_url}/u/{_USERNAME}", "AC-7: chip not operable via Enter"


# ── XSS defense-in-depth (threat model) ───────────────────────────────────────


def test_chip_username_renders_as_text(page: Page, base_url: str):
    """Threat model: a crafted username renders inert as text (textContent)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    xss = {
        "ok": True,
        "profile": {
            "username": "<img src=x onerror=alert(1)>",
            "is_public": True,
            "show_collection": True,
            "show_stats": True,
        },
    }
    alerts = []
    page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))

    _mount_chip(page, base_url, xss)

    assert alerts == [], f"XSS alert fired: {alerts}"
    # No <img> element injected inside the chip
    img_count = page.locator("#profile-chip img[onerror]").count()
    assert img_count == 0, "XSS img element injected into the chip"
    # The raw text is present as label text
    label_text = page.locator("#profile-chip .profile-chip-label").inner_text()
    assert "<img" in label_text, "Expected raw text, got something else"
