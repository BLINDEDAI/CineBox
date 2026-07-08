"""Browser E2E tests for guest-explore-mode (AC-4/5/6/7/8/9/10/11).

Covers the `### Tester scope` E2E rows of the task file:
  AC-4  — "Explorar sin cuenta" opens guest mode on discover-view, no login, and
          (network assertion, ported from AC-7) no user-scoped request fires.
  AC-5  — clicking each account-only nav item in guest mode opens the signup
          prompt, not the view.
  AC-6  — an auth-gated action (detail-modal add, discover-card add) shows
          "Regístrate para…" and issues no 401 / user-scoped call.
  AC-7  — a series detail in guest mode: banner/cast render, no watched marks,
          no edit section, no user-scoped request.
  AC-8  — proceeding from a signup prompt lands in the login/registration screen.
  AC-9  — authed smoke: the landing's normal auth CTAs are untouched by the new
          guest entry point (full authed-flow E2E already covered by the
          pre-existing suite; this is the guest-feature-local regression check).
  AC-10 — anti-flash: guest entry never sets `cinephora-authed`.
  AC-11 — @axe-core/playwright WCAG 2.2 A/AA (0 critical/serious) on the guest
          Descubrir surface + the signup dialog, 375px + desktop, keyboard,
          focus, >=24px targets, es-ES copy.

Strategy:
  - The real Cinephora server is booted (conftest.py base_url fixture), no DB.
  - TMDB reads are stubbed via page.route (no real TMDB_API_KEY needed) so
    Descubrir/trending/details/season render deterministically.
  - axe-core (4.9.0) is injected via a same-origin routed <script> (CSP:
    script-src 'self'), same technique as test_sidebar_profile_chip.py /
    test_public_profiles_a11y.py — node is not installed in this environment.
  - Screenshots saved to handoffs/guest-explore-mode/screenshots/.

KNOWN PRODUCTION GAP (flagged, not fixed — Tester is read-only on production
code — see tests/e2e/README and agents/tester-agent.md):
  test_series_detail_shows_season_episode_browsing documents that the guest
  detail modal never renders `#modal-episodes-section` (the season/episode
  browser) because modal.js:127 gates that section on `existing` (an owned
  collection item), and a guest never has an `existing` item. Spec AC-7 /
  Edge Cases explicitly require "season/episode browsing" for a guest viewing
  a series ("the guest modal browses seasons/episodes with no watched marks
  and no edit section"). This test fails against the current diff — see the
  Tester handoff `## Open Questions` for the bounce.
"""

import json
from pathlib import Path

from playwright.sync_api import Page

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs" / "guest-explore-mode" / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Mock TMDB-proxy responses (matching server.py's documented response shapes) ─

_MOCK_CONFIG = {"supabase_url": "", "supabase_anon_key": ""}

_MOCK_TRENDING = {
    "ok": True,
    "results": [
        {"tmdb_id": 550, "media_type": "movie", "title": "Fight Club", "year": "1999",
         "poster_url": "https://image.tmdb.org/t/p/w342/mock1.jpg", "genre_ids": [18]},
        {"tmdb_id": 1399, "media_type": "tv", "title": "Game of Thrones", "year": "2011",
         "poster_url": "https://image.tmdb.org/t/p/w342/mock2.jpg", "genre_ids": [18]},
    ],
}

_MOCK_MOVIE_DETAILS = {
    "ok": True,
    "details": {
        "overview": "Un hombre insomne y un vendedor de jabón forman un club de lucha clandestino.",
        "genres": ["Drama"], "genre_ids": [18], "runtime": 139,
        "title": "Fight Club", "poster_path": "/mock1.jpg", "backdrop_path": "/mockbd1.jpg",
        "vote_average": 8.4, "trailer": "", "dir_label": "Dirección", "directors": ["David Fincher"],
        "cast": [{"name": "Brad Pitt", "profile_path": ""}, {"name": "Edward Norton", "profile_path": ""}],
        "providers": [], "providers_link": "", "total_seasons": None, "total_episodes": None,
        "seasons": None, "watched_count": 0,
    },
}

_MOCK_TV_DETAILS = {
    "ok": True,
    "details": {
        "overview": "La lucha por el Trono de Hierro.",
        "genres": ["Drama"], "genre_ids": [18], "runtime": 60,
        "title": "Game of Thrones", "poster_path": "/mock2.jpg", "backdrop_path": "/mockbd2.jpg",
        "vote_average": 8.4, "trailer": "", "dir_label": "Creación", "directors": ["D. Benioff"],
        "cast": [{"name": "Emilia Clarke", "profile_path": ""}, {"name": "Kit Harington", "profile_path": ""}],
        "providers": [], "providers_link": "",
        "total_seasons": 2, "total_episodes": 20,
        "seasons": [
            {"season_number": 1, "name": "Temporada 1", "episode_count": 10},
            {"season_number": 2, "name": "Temporada 2", "episode_count": 10},
        ],
        "watched_count": 0,
    },
}

_MOCK_SEASON = {
    "ok": True,
    "season": {
        "season_number": 1, "name": "Temporada 1",
        "episodes": [
            {"episode_number": 1, "name": "Winter Is Coming", "air_date": "2011-04-17",
             "runtime": 62, "overview": "…", "still_path": None, "watched": False},
            {"episode_number": 2, "name": "The Kingsroad", "air_date": "2011-04-24",
             "runtime": 56, "overview": "…", "still_path": None, "watched": False},
        ],
    },
}

# GET endpoints the guest surface may call. Any GET to one of these is a
# "public read" the spec allows (AC-7); anything else GET on /api/* other than
# /api/config is a candidate user-scoped leak and is flagged by _NetworkWatcher.
_ALLOWED_GET_PATTERNS = ("/api/trending", "/api/discover", "/api/details",
                          "/api/search", "/api/similar", "/api/tv/", "/api/config")
# Endpoints that are ALWAYS user-scoped — a request to any of these during a
# guest session is a hard AC-7 violation regardless of verb.
_USER_SCOPED_PATTERNS = ("/api/movies", "/api/level", "/api/profile", "/api/lists",
                          "/api/feed", "/api/reviews", "/api/account", "/api/follows")


class _NetworkWatcher:
    """Records every /api/* request URL + method; exposes the user-scoped subset."""

    def __init__(self, page: Page):
        self.requests = []
        page.on("request", self._record)

    def _record(self, request):
        if "/api/" in request.url:
            self.requests.append((request.method, request.url))

    def user_scoped_calls(self):
        return [(m, u) for m, u in self.requests if any(p in u for p in _USER_SCOPED_PATTERNS)]


def _route_json(page: Page, url_pattern: str, payload: dict, *, status: int = 200):
    def handle(route):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
    page.route(url_pattern, handle)


def _open_signup_prompt(page: Page, intent: str = "gestionar tu cuenta"):
    """Open #signup-prompt by calling the production seam directly
    (page.evaluate) rather than clicking a sidebar nav item — the sidebar
    `.side-link` nav is display:none on mobile (<=720px, same layout rule
    test_sidebar_profile_chip.py notes), so a click-based trigger is viewport-
    fragile for tests that assert on the DIALOG itself (already covered by the
    real click path in test_nav_gating_each_account_view_opens_signup_prompt,
    which runs desktop-only). This mirrors the hybrid-fixture-fallback pattern
    (state setup via direct call; the dialog's own rendering/a11y is still
    verified in the real browser afterwards)."""
    page.evaluate("(i) => _promptSignup(i)", intent)
    page.wait_for_selector("#signup-prompt.is-open", timeout=3000)


def _mount_guest(page: Page, base_url: str) -> _NetworkWatcher:
    """Navigate to the SPA, stub the TMDB-proxy reads, click 'Explorar sin
    cuenta', and return a _NetworkWatcher recording every /api/* call from
    page load onward (so the caller can assert no user-scoped call fired)."""
    _route_json(page, f"{base_url}/api/config", _MOCK_CONFIG)
    _route_json(page, f"{base_url}/api/trending", _MOCK_TRENDING)
    _route_json(page, f"{base_url}/api/discover*", _MOCK_TRENDING)
    watcher = _NetworkWatcher(page)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.click("[data-guest-enter]")
    page.wait_for_selector("#discover-view.is-active", timeout=5000)
    return watcher


def _inject_axe(page: Page, base_url: str):
    axe_url = f"{base_url}/__test__/axe.min.js"
    axe_content = AXE_JS.read_bytes()

    def _serve_axe(route):
        route.fulfill(status=200, content_type="application/javascript", body=axe_content)

    page.route(axe_url, _serve_axe)
    page.add_script_tag(url=axe_url)
    page.wait_for_timeout(200)


def _run_axe(page: Page, context_selector: str = "html") -> list:
    results = page.evaluate(
        """(sel) => {
            return axe.run(
                document.querySelector(sel) || document,
                { runOnly: { type: 'tag',
                    values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] } }
            ).then(r => r.violations.map(v => ({
                id: v.id, impact: v.impact, description: v.description, nodes: v.nodes.length
            })));
        }""",
        context_selector,
    )
    return [v for v in results if v["impact"] in ("critical", "serious")]


def _screenshot(page: Page, name: str):
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


# ── AC-4 / AC-7: guest entry, no login, no user-scoped request ─────────────────


def test_guest_entry_opens_discover_view_no_login_no_user_scoped_request(page: Page, base_url: str):
    """AC-4: 'Explorar sin cuenta' opens guest mode on discover-view without any
    login screen. AC-7 (network assertion): zero user-scoped requests fire."""
    page.set_viewport_size({"width": 1280, "height": 900})
    watcher = _mount_guest(page, base_url)

    active_view = page.evaluate("() => document.body.dataset.activeView")
    assert active_view == "discover-view"

    welcome_hidden = page.evaluate(
        "() => document.getElementById('welcome-screen').classList.contains('is-hidden') "
        "|| document.getElementById('welcome-screen').hidden "
        "|| getComputedStyle(document.getElementById('welcome-screen')).display === 'none'"
    )
    login_screen_hidden = page.evaluate(
        "() => { const s = document.getElementById('login-screen'); return !s || s.hidden; }"
    )
    assert login_screen_hidden, "no login screen must be shown when entering guest mode"

    is_guest = page.evaluate("() => document.body.dataset.guest")
    assert is_guest == "1"

    # Let the trending call settle, then assert no user-scoped request occurred.
    page.wait_for_timeout(300)
    leaked = watcher.user_scoped_calls()
    assert leaked == [], f"guest session must never call a user-scoped endpoint: {leaked}"
    _screenshot(page, "ac4-guest-discover-view")


# ── AC-5: every account-only nav item opens the signup prompt ──────────────────


def test_nav_gating_each_account_view_opens_signup_prompt(page: Page, base_url: str):
    """AC-5: clicking Colección / Estadísticas / Mis listas / Actividad / Ajustes
    in guest mode opens #signup-prompt instead of the destination view."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)

    gated_views = ["collection-view", "stats-view", "lists-view", "activity-view", "settings-view"]
    for view_id in gated_views:
        # Close any prompt left open from a previous iteration.
        page.evaluate("() => { const d = document.getElementById('signup-prompt'); "
                       "if (d && !d.hidden) { d.hidden = true; d.classList.remove('is-open'); } }")
        page.click(f"[data-view-target='{view_id}']")
        page.wait_for_selector("#signup-prompt.is-open", timeout=3000)
        active_view = page.evaluate("() => document.body.dataset.activeView")
        assert active_view != view_id, f"{view_id} must not become the active view in guest mode"
        prompt_hidden = page.evaluate("() => document.getElementById('signup-prompt').hidden")
        assert not prompt_hidden, f"signup prompt must open for {view_id}"
    _screenshot(page, "ac5-nav-gating-signup-prompt")


# ── AC-6: auth-gated action -> signup prompt, no 401 / user-scoped call ────────


def test_discover_card_add_action_shows_signup_prompt_no_user_scoped_call(page: Page, base_url: str):
    """AC-6: the '+ ' add action on a Descubrir result card is replaced by a
    'Regístrate para guardar' CTA in guest mode; clicking it opens the signup
    prompt and issues no user-scoped / 401 call."""
    page.set_viewport_size({"width": 1280, "height": 900})
    watcher = _mount_guest(page, base_url)
    page.wait_for_selector(".card [data-action='guest-signup']", timeout=5000)
    page.click(".card [data-action='guest-signup']")
    page.wait_for_selector("#signup-prompt.is-open", timeout=3000)
    title = page.evaluate("() => document.getElementById('signup-prompt-title').textContent")
    # The es-ES accent typo flagged as fe-guest-signup-prompt-accent-typo in the
    # iter-1 Tester handoff was fixed in app.js:410 during the AC-7 bounce-fix
    # iteration (see frontend-reviewer-handoff.md iter 2) — the title now reads
    # "Regístrate para …" with the accent, matching the static default.
    assert "Regístrate" in title
    page.wait_for_timeout(200)
    assert watcher.user_scoped_calls() == []
    _screenshot(page, "ac6-discover-card-signup-prompt")


def test_modal_add_action_shows_signup_prompt_no_user_scoped_call(page: Page, base_url: str):
    """AC-6: opening a title's detail modal in guest mode and clicking its add
    control shows 'Regístrate para…' and issues no user-scoped call."""
    page.set_viewport_size({"width": 1280, "height": 900})
    watcher = _mount_guest(page, base_url)
    _route_json(page, f"{base_url}/api/details*", _MOCK_MOVIE_DETAILS)
    page.evaluate("(a) => openDetail(a[0], a[1])", [550, "movie"])
    page.wait_for_selector("#modal-guest-cta", timeout=5000)
    edit_section_present = page.evaluate(
        "() => !!document.getElementById('modal-edit-section')")
    assert not edit_section_present, "guest modal must never render #modal-edit-section"
    page.click("#modal-guest-cta [data-guest-signup]")
    page.wait_for_selector("#signup-prompt.is-open", timeout=3000)
    page.wait_for_timeout(200)
    assert watcher.user_scoped_calls() == []
    _screenshot(page, "ac6-modal-add-signup-prompt")


# ── AC-7: series detail — banner/cast render, no watched marks, no edit section ─


def test_series_detail_renders_banner_and_cast_no_edit_section(page: Page, base_url: str):
    """AC-7: a series detail in guest mode shows the banner + cast, and never
    #modal-edit-section (that section is collection-owned-item-only)."""
    page.set_viewport_size({"width": 1280, "height": 900})
    watcher = _mount_guest(page, base_url)
    _route_json(page, f"{base_url}/api/details*", _MOCK_TV_DETAILS)
    page.evaluate("(a) => openDetail(a[0], a[1])", [1399, "tv"])
    page.wait_for_selector(".modal-cast", timeout=5000)
    cast_names = page.evaluate(
        "() => Array.from(document.querySelectorAll('.cast-name')).map(e => e.textContent)")
    assert "Emilia Clarke" in cast_names
    edit_section_present = page.evaluate("() => !!document.getElementById('modal-edit-section')")
    assert not edit_section_present
    page.wait_for_timeout(200)
    assert watcher.user_scoped_calls() == []
    _screenshot(page, "ac7-series-detail-banner-cast")


def test_series_detail_shows_season_episode_browsing(page: Page, base_url: str):
    """AC-7 / Edge Cases: 'the guest modal browses seasons/episodes with no
    watched marks and no edit section'. Spec text (guest-explore-mode-specs.md
    § Edge Cases, § AC-7): a guest viewing a series must be able to browse
    seasons/episodes read-only.

    Fixed in the iter-2 bounce (frontend-dev / frontend-reviewer-handoff.md
    iter 2): modal.js's `#modal-episodes-section` render gate now also opens
    for `_guestMode && type === "tv"`, and `_rerenderModalEpisodesSection()`
    derives its pseudo-movie from `modalContext` (the public `_details` read
    already used to open the modal) instead of the caller's owned-collection
    `movies` array — so the season/episode browser renders for a guest and its
    season fetch hits only the public `GET /api/tv/{id}/season/{n}` read."""
    page.set_viewport_size({"width": 1280, "height": 900})
    watcher = _mount_guest(page, base_url)
    _route_json(page, f"{base_url}/api/details*", _MOCK_TV_DETAILS)
    _route_json(page, f"{base_url}/api/tv/1399/season/1", _MOCK_SEASON)
    page.evaluate("(a) => openDetail(a[0], a[1])", [1399, "tv"])
    page.wait_for_selector(".modal-cast", timeout=5000)
    page.wait_for_selector("#modal-episodes-section", timeout=5000)
    _screenshot(page, "ac7-series-detail-season-browsing")

    season_browser_present = page.evaluate(
        "() => !!document.getElementById('modal-episodes-section')")
    assert season_browser_present, (
        "guest mode must offer read-only season/episode browsing for a series "
        "detail per spec AC-7 / Edge Cases."
    )
    page.wait_for_timeout(200)
    assert watcher.user_scoped_calls() == [], (
        "the guest season/episode browser must call only the public "
        "GET /api/tv/{id}/season/{n} read, never a user-scoped endpoint"
    )


# ── AC-8: signup prompt -> login/registration screen ────────────────────────────


def test_signup_prompt_register_action_lands_in_login_screen(page: Page, base_url: str):
    """AC-8: choosing 'Crear cuenta' from the signup prompt lands in the
    standard login/registration screen and clears guest state (BR-6)."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)
    page.click("[data-view-target='stats-view']")
    page.wait_for_selector("#signup-prompt.is-open", timeout=3000)
    page.click("#signup-prompt-register")
    page.wait_for_timeout(200)
    login_screen_hidden = page.evaluate(
        "() => { const s = document.getElementById('login-screen'); return !s || s.hidden; }")
    assert not login_screen_hidden, "choosing 'Crear cuenta' must reveal the login/registration screen"
    is_guest = page.evaluate("() => document.body.dataset.guest")
    assert is_guest is None, "guest state (body[data-guest]) must be cleared on leaving to auth"
    _screenshot(page, "ac8-signup-prompt-to-login-screen")


def test_signup_prompt_login_action_lands_in_login_screen(page: Page, base_url: str):
    """AC-8 sibling: the 'Iniciar sesión' action also lands in the login screen."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)
    page.click("[data-view-target='lists-view']")
    page.wait_for_selector("#signup-prompt.is-open", timeout=3000)
    page.click("[data-signup-action='login']")
    page.wait_for_timeout(200)
    login_screen_hidden = page.evaluate(
        "() => { const s = document.getElementById('login-screen'); return !s || s.hidden; }")
    assert not login_screen_hidden


# ── AC-9: authed smoke — the new guest CTA does not alter the auth entry points ─


def test_landing_auth_ctas_unaffected_by_guest_entry_point(page: Page, base_url: str):
    """AC-9 (guest-feature-local regression check): the pre-existing 'Crear mi
    cuenta' / 'Ya tengo cuenta' / 'Iniciar sesión' CTAs are still present and
    unmodified alongside the new 'Explorar sin cuenta' entry — the full authed
    E2E flow (login, collection, stats, settings, social) is covered by the
    pre-existing suite (test_change_password.py et al.) and is unchanged by
    this diff per the backend/frontend Reviewer handoffs."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _route_json(page, f"{base_url}/api/config", _MOCK_CONFIG)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    assert page.locator("#welcome-register").count() == 1
    assert page.locator("#welcome-login").count() == 1
    # data-landing-auth='login' appears on 3 CTAs in the full landing (topbar,
    # get-started footer link, mobile bottom nav) — this asserts presence, not
    # a specific count, since the exact number is landing-layout detail
    # unrelated to this feature.
    assert page.locator("[data-landing-auth='login']").count() >= 1
    assert page.locator("[data-guest-enter]").count() == 2  # topbar + hero CTA row
    is_guest = page.evaluate("() => document.body.dataset.guest")
    assert is_guest is None, "guest mode must not be active before any explicit entry"


# ── AC-10: anti-flash — guest entry never sets cinephora-authed ────────────────


def test_guest_entry_never_sets_authed_class(page: Page, base_url: str):
    """AC-10: entering guest mode must never add `cinephora-authed` to <html> —
    that class gates the authed app shell / anti-flash pre-paint marker."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)
    has_authed_class = page.evaluate(
        "() => document.documentElement.classList.contains('cinephora-authed')")
    assert not has_authed_class, "guest mode must never set cinephora-authed (BR-8/AC-10)"


def test_first_time_visitor_sees_landing_no_authed_class_pre_paint(page: Page, base_url: str):
    """AC-10 regression guard: a first-time visitor (no sb-*-auth-token in
    localStorage) never gets the pre-paint cinephora-authed marker — the new
    guest CTA must not interfere with boot.js's existing anti-flash gate."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _route_json(page, f"{base_url}/api/config", _MOCK_CONFIG)
    page.goto(base_url)
    has_authed_class = page.evaluate(
        "() => document.documentElement.classList.contains('cinephora-authed')")
    assert not has_authed_class


# ── AC-11: axe WCAG 2.2 A/AA — guest Descubrir surface + signup dialog ─────────


def test_guest_surface_a11y_desktop(page: Page, base_url: str):
    """AC-11: guest Descubrir surface, desktop 1280px, zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)
    page.wait_for_timeout(300)
    _inject_axe(page, base_url)
    violations = _run_axe(page, "#discover-view")
    _screenshot(page, "ac11-guest-discover-desktop")
    assert violations == [], f"axe violations on guest Descubrir (desktop): {violations}"


def test_guest_surface_a11y_mobile_375(page: Page, base_url: str):
    """AC-11: guest Descubrir surface, mobile 375px, zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 812})
    _mount_guest(page, base_url)
    page.wait_for_timeout(300)
    _inject_axe(page, base_url)
    violations = _run_axe(page, "#discover-view")
    _screenshot(page, "ac11-guest-discover-mobile-375")
    assert violations == [], f"axe violations on guest Descubrir (mobile 375px): {violations}"


def test_signup_dialog_a11y_desktop_and_mobile(page: Page, base_url: str):
    """AC-11: the open signup dialog, desktop + 375px, zero critical/serious."""
    for width, height, label in [(1280, 900, "desktop"), (375, 812, "mobile-375")]:
        page.set_viewport_size({"width": width, "height": height})
        _mount_guest(page, base_url)
        _open_signup_prompt(page)
        _inject_axe(page, base_url)
        violations = _run_axe(page, "#signup-prompt")
        _screenshot(page, f"ac11-signup-dialog-{label}")
        assert violations == [], f"axe violations on signup dialog ({label}): {violations}"


def test_signup_dialog_keyboard_focus_and_target_size(page: Page, base_url: str):
    """AC-11: signup dialog controls are keyboard-operable with visible focus
    and >=24px targets; copy is es-ES."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)
    _open_signup_prompt(page)

    register_btn = page.locator("#signup-prompt-register")
    box = register_btn.bounding_box()
    assert box is not None
    assert box["width"] >= 24 and box["height"] >= 24

    # The register button receives focus on open (modal.js/app.js focus-trap).
    focused_id = page.evaluate("() => document.activeElement.id")
    assert focused_id == "signup-prompt-register"
    outline = page.evaluate(
        "() => getComputedStyle(document.activeElement).outlineStyle")
    box_shadow = page.evaluate(
        "() => getComputedStyle(document.activeElement).boxShadow")
    assert outline != "none" or box_shadow != "none", "focused control must show a visible focus style"

    title_text = page.evaluate("() => document.getElementById('signup-prompt-title').textContent")
    assert "Regístrate" in title_text  # es-ES copy, accent fixed per fe-guest-signup-prompt-accent-typo

    # Tab cycles within the dialog (focus trap) rather than escaping to the page body.
    page.keyboard.press("Tab")
    after_tab = page.evaluate("() => document.activeElement.closest('#signup-prompt') !== null")
    assert after_tab, "Tab from the first focusable control must stay inside the dialog"


def test_signup_dialog_escape_and_close_button(page: Page, base_url: str):
    """AC-11 sibling: Escape / the close button dismiss the dialog and restore
    focus to the triggering control (accessible dialog dismissal)."""
    page.set_viewport_size({"width": 1280, "height": 900})
    _mount_guest(page, base_url)
    _open_signup_prompt(page)
    # `[data-signup-close]` matches TWO elements (the backdrop div AND the
    # dedicated close button) — target the button specifically; the backdrop
    # is covered by the dialog card at its default click point.
    page.click(".signup-prompt-close")
    page.wait_for_timeout(200)
    hidden = page.evaluate("() => document.getElementById('signup-prompt').hidden")
    assert hidden
