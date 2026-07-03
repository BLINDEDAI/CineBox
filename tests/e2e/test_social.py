"""Browser E2E tests for social-follows-and-activity-feed (AC-18, AC-19).

Covers every ### Tester scope Playwright row:

  AC-18 — the new authed "Actividad" feed view (#activity-view / activity.js
          showActivityView()): renders followed-users' activity + empty
          state; axe WCAG 2.2 A/AA zero critical/serious; keyboard-operable;
          visible focus; interactive targets >= 24px; @1280 + @375; es-ES.
  AC-19 — the public-page follow control + follower/following counts/lists
          (public.js buildFollowControl / socialSection on /u/<username>):
          renders follow/unfollow/is-self-hidden/login-link states; axe WCAG
          2.2 A/AA zero critical/serious; keyboard; focus; >= 24px;
          @1280 + @375; es-ES.

Strategy (mirrors tests/e2e/test_custom_avatar_upload.py + test_public_profiles_a11y.py):
  - Real Cinephora server via conftest.py base_url fixture (no DB/auth required).
  - AC-18 (authed SPA view): stub /api/config + /api/feed via page.route(),
    goto the SPA, set `_currentUser` + drive `showActivityView()` directly on
    `#activity-view` (production seam, per the tester-bundle's guidance for
    session-aware/auth-gated authed-SPA views) -- no real Supabase session.
  - AC-19 (public page): the follow control needs a session token read from
    localStorage (public.js readViewerToken -> derives the Supabase storage
    key from /api/config, reads `sb-{project_ref}-auth-token`). We stub
    /api/config with a fake supabase_url, seed that localStorage key with a
    JSON-encoded access_token BEFORE navigation (via page.add_init_script),
    and stub GET/POST/DELETE /api/follows/* via page.route(). page.route is
    LIFO: broad routes are registered first, narrower/state-specific
    overrides registered AFTER (per the tester-bundle's stubbing recipe).
  - axe-core (4.9.0) injected via the vendored tests/e2e/axe.min.js as a
    same-origin routed <script> (CSP: script-src 'self'), same pattern as
    the sibling social-adjacent test files.
  - Screenshots saved to
    handoffs/social-follows-and-activity-feed/screenshots/.
"""

import json
from pathlib import Path

from playwright.sync_api import Page

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _E2E_DIR.parent.parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs"
    / "social-follows-and-activity-feed"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_SUPABASE_URL = "https://demoproject.supabase.co"
_PROJECT_REF = "demoproject"
_STORAGE_KEY = f"sb-{_PROJECT_REF}-auth-token"
_USERNAME = "socialuser"
_TARGET_USERNAME = "targetuser"


# ── Mock feed data (matching the documented GET /api/feed shape) ─────────────

_MOCK_FEED_THREE_KINDS = {
    "ok": True,
    "activity": [
        {
            "action": "watched", "username": "alice", "avatar_url": None,
            "title": "Dune: Part Two", "poster_url": "https://image.tmdb.org/t/p/w342/dune.jpg",
            "media_type": "movie", "tmdb_id": 1, "year": "2024",
            "created_at": "2026-07-02T12:00:00+00:00",
        },
        {
            "action": "rated", "username": "bob", "avatar_url": None,
            "title": "Arrival", "poster_url": "https://image.tmdb.org/t/p/w342/arrival.jpg",
            "media_type": "movie", "tmdb_id": 2, "year": "2016",
            "rating": 4, "created_at": "2026-07-02T11:00:00+00:00",
        },
        {
            "action": "list_add", "username": "carol", "avatar_url": None,
            "title": "Her", "poster_url": "https://image.tmdb.org/t/p/w342/her.jpg",
            "media_type": "movie", "tmdb_id": 3, "year": "2013",
            "list_name": "Favoritas de ciencia ficción",
            "list_share_token": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "created_at": "2026-07-02T10:00:00+00:00",
        },
    ],
}

_MOCK_FEED_EMPTY = {"ok": True, "activity": []}

_MOCK_PUBLIC_PROFILE_BASE = {
    "ok": True,
    "profile": {
        "username": _TARGET_USERNAME,
        "avatar_url": None,
        "followers_count": 5,
        "following_count": 2,
        "followers": [
            {"username": "pubfollower1", "avatar_url": None},
            {"username": "pubfollower2", "avatar_url": None},
        ],
        "following": [
            {"username": "pubfollowing1", "avatar_url": None},
        ],
    },
}


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _inject_axe(page: Page, base_url: str):
    """Inject axe-core via a same-origin routed <script> (CSP: script-src 'self')."""
    axe_url = f"{base_url}/__test__/axe.min.js"
    axe_content = AXE_JS.read_bytes()

    def _serve_axe(route):
        route.fulfill(status=200, content_type="application/javascript", body=axe_content)

    page.route(axe_url, _serve_axe)
    page.add_script_tag(url=axe_url)
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


def _screenshot(page: Page, name: str) -> str:
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


def _has_visible_focus(page: Page) -> bool:
    outline = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).outlineWidth"
    )
    box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.activeElement).boxShadow"
    )
    return outline not in ("0px", "") or box_shadow not in ("none", "")


# ══════════════════════════════════════════════════════════════════════════════
# AC-18 — "Actividad" feed view (#activity-view)
# ══════════════════════════════════════════════════════════════════════════════


def _route_feed(page: Page, base_url: str, payload: dict):
    def handle(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"{base_url}/api/feed", handle)


def _route_config(page: Page, base_url: str):
    def handle(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"supabase_url": _SUPABASE_URL, "supabase_anon_key": "demo-anon-key"}),
        )

    page.route(f"{base_url}/api/config", handle)


def _goto_spa_and_open_activity_view(page: Page, base_url: str):
    """Open index.html, mount an authenticated session on the production seam,
    then drive showActivityView() directly on #activity-view -- no real
    Supabase session needed (mirrors test_custom_avatar_upload.py's
    _mount_authenticated_user + _open_settings_view pattern)."""
    _route_config(page, base_url)
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
            if (!_currentUser) {
                _currentUser = { id: 'test-user-id', email: 'user@example.com' };
            }
        }"""
    )
    page.evaluate(
        """async () => {
            document.querySelectorAll('.view').forEach(v => v.hidden = true);
            const view = document.getElementById('activity-view');
            if (view) view.hidden = false;
            if (typeof showActivityView === 'function') {
                await showActivityView();
            }
        }"""
    )
    page.wait_for_timeout(400)


def test_ac18_activity_feed_renders_three_kinds(page: Page, base_url: str):
    """AC-9/AC-10: the feed renders watched/rated/list_add as distinct entries,
    reverse-chronological (newest first, per the mock's created_at ordering)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_THREE_KINDS)
    _goto_spa_and_open_activity_view(page, base_url)

    items = page.locator("#activity-view .activity-item")
    assert items.count() == 3, f"AC-10: expected 3 distinct activity entries, got {items.count()}"

    first_text = items.nth(0).inner_text()
    assert "alice" in first_text and "vista" in first_text.lower(), (
        f"AC-9: expected the newest (watched) entry first, got: {first_text!r}"
    )

    _screenshot(page, "activity-feed-three-kinds")


def test_ac18_activity_feed_empty_state(page: Page, base_url: str):
    """AC-15: an empty feed shows a friendly empty-state message, not an error."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_EMPTY)
    _goto_spa_and_open_activity_view(page, base_url)

    empty = page.locator("#activity-view .activity-empty")
    assert empty.count() == 1, "AC-15: expected the empty-state block to render"
    error = page.locator("#activity-view .activity-error")
    assert error.count() == 0, "AC-15: an empty feed must not render the error state"

    _screenshot(page, "activity-feed-empty-state")


def test_ac18_activity_feed_a11y_desktop(page: Page, base_url: str):
    """AC-18: #activity-view, desktop 1280px, axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_THREE_KINDS)
    _goto_spa_and_open_activity_view(page, base_url)

    _screenshot(page, "activity-feed-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#activity-view")
    _screenshot(page, "activity-feed-desktop-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in "
        f"#activity-view (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac18_activity_feed_a11y_mobile(page: Page, base_url: str):
    """AC-18: #activity-view, mobile 375px, axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _route_feed(page, base_url, _MOCK_FEED_THREE_KINDS)
    _goto_spa_and_open_activity_view(page, base_url)

    _screenshot(page, "activity-feed-mobile")

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#activity-view")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in "
        f"#activity-view (mobile): " + json.dumps(violations, indent=2)
    )


def test_ac18_activity_feed_empty_state_a11y_desktop(page: Page, base_url: str):
    """AC-18/AC-15: the empty-state render is independently axe-clean too."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_EMPTY)
    _goto_spa_and_open_activity_view(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#activity-view")
    _screenshot(page, "activity-feed-empty-desktop-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in the "
        f"empty-state #activity-view (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac18_activity_feed_keyboard_focus(page: Page, base_url: str):
    """AC-18: the feed view is keyboard-operable with a visible focus indicator
    (the list_add entry link and the retry button are the interactive elements)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_THREE_KINDS)
    _goto_spa_and_open_activity_view(page, base_url)

    link = page.locator("#activity-view .activity-link").first
    assert link.count() == 1, "AC-18: expected the list_add entry's link to render"
    link.focus()

    focused_tag = page.evaluate("() => document.activeElement.tagName")
    assert focused_tag == "A", f"AC-18: expected focus on the activity link, got {focused_tag}"
    in_view = page.evaluate("() => !!document.activeElement.closest('#activity-view')")
    assert in_view, "AC-18: focused element is not inside #activity-view"

    assert _has_visible_focus(page), "AC-18: no visible focus indicator on the activity link"

    _screenshot(page, "activity-feed-keyboard-focus")


def test_ac18_activity_feed_retry_button_keyboard_focus(page: Page, base_url: str):
    """AC-18: the error-state retry button (data-activity-refresh) is
    keyboard-operable with a visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})

    def handle_error(route):
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "boom"}))

    page.route(f"{base_url}/api/feed", handle_error)
    _goto_spa_and_open_activity_view(page, base_url)

    retry_btn = page.locator("#activity-view .activity-retry")
    assert retry_btn.count() == 1, "AC-18: expected the retry button to render on error"
    retry_btn.focus()

    focused_id_class = page.evaluate(
        "() => document.activeElement.className"
    )
    assert "activity-retry" in focused_id_class

    assert _has_visible_focus(page), "AC-18: no visible focus indicator on the retry button"

    _screenshot(page, "activity-feed-retry-keyboard-focus")


def test_ac18_activity_feed_targets_24px(page: Page, base_url: str):
    """AC-18: interactive targets (the list_add entry link) are >= 24px in
    both dimensions (WCAG 2.5.8)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_THREE_KINDS)
    _goto_spa_and_open_activity_view(page, base_url)

    link = page.locator("#activity-view .activity-link").first
    box = link.bounding_box()
    assert box, "AC-18: activity link has no bounding box (not rendered/visible)"
    assert box["width"] >= 24, f"AC-18: activity link width {box['width']}px < 24px"
    assert box["height"] >= 24, f"AC-18: activity link height {box['height']}px < 24px"

    _screenshot(page, "activity-feed-target-sizes")


def test_ac18_activity_feed_es_es_copy(page: Page, base_url: str):
    """AC-18 locale: the feed renders es-ES verb copy for each action kind."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, _MOCK_FEED_THREE_KINDS)
    _goto_spa_and_open_activity_view(page, base_url)

    text = page.locator("#activity-view").inner_text()
    assert "como vista" in text, "AC-18: expected es-ES 'marcó ... como vista' copy"
    assert "valoró" in text, "AC-18: expected es-ES 'valoró' copy"
    assert "añadió" in text and "lista" in text, "AC-18: expected es-ES 'añadió ... a la lista' copy"


# ══════════════════════════════════════════════════════════════════════════════
# AC-19 — public-page follow control + followers/following counts/lists
# ══════════════════════════════════════════════════════════════════════════════


def _seed_viewer_session(page: Page, token: str = "fake-access-token"):
    """Seed the Supabase-shaped localStorage entry public.js reads BEFORE the
    page's own scripts run, via an init script (so it exists at page-load
    time, matching the real supabase-js persistence timing).

    IMPORTANT: Page.add_init_script (Python sync API) treats its string
    argument as a raw script BODY to evaluate, not a function expression --
    passing "() => { ... }" defines an unused arrow function and never calls
    it (silently a no-op, confirmed by isolated repro against this Playwright
    version). The statement must be a plain top-level script, not wrapped.
    """
    script = (
        f"localStorage.setItem({json.dumps(_STORAGE_KEY)}, "
        f"JSON.stringify({{ access_token: {json.dumps(token)}, token_type: 'bearer' }}));"
    )
    page.add_init_script(script)


def _route_public_config(page: Page, base_url: str):
    def handle(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"supabase_url": _SUPABASE_URL, "supabase_anon_key": "demo-anon-key"}),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_public_profile(page: Page, base_url: str, profile_overrides: dict = None):
    payload = json.loads(json.dumps(_MOCK_PUBLIC_PROFILE_BASE))
    if profile_overrides:
        payload["profile"].update(profile_overrides)

    def handle(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"{base_url}/api/public/profile/{_TARGET_USERNAME}", handle)


def _route_follow_endpoints(page: Page, base_url: str, *, following: bool = False,
                             is_self: bool = False, followable: bool = True,
                             status_code: int = 200):
    """Stub GET/POST/DELETE on /api/follows[/{username}] with ONE stateful
    handler per URL (page.route matches all HTTP methods on a given URL, so a
    GET-only and a POST/DELETE-only handler registered on the SAME URL would
    collide under LIFO -- the later registration would shadow the earlier
    one's GET branch entirely, which is exactly the failure mode this single
    combined handler avoids). `state["following"]` flips on a successful
    click, mirroring the real backend's idempotent toggle."""
    state = {"following": following}

    def handle_status(route):
        route.fulfill(
            status=status_code, content_type="application/json",
            body=json.dumps({"ok": status_code == 200, "following": state["following"],
                              "is_self": is_self, "followable": followable}),
        )

    def handle_username(route):
        req = route.request
        if req.method == "GET":
            return handle_status(route)
        if req.method == "DELETE":
            state["following"] = False
            return route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "following": False}))
        route.fallback()

    def handle_collection(route):
        if route.request.method == "POST":
            state["following"] = True
            return route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "following": True}))
        route.fallback()

    page.route(f"{base_url}/api/follows/{_TARGET_USERNAME}", handle_username)
    page.route(f"{base_url}/api/follows", handle_collection)


def _route_broken_relative_assets(page: Page, base_url: str):
    """Workaround for the documented BUG-001 (public.html relative asset
    paths under /u/{username}) -- same workaround as
    test_public_profiles_a11y.py / test_custom_avatar_upload.py."""

    def _serve_from_disk(filename, content_type):
        file_path = _REPO_ROOT / filename

        def _handle(route):
            route.fulfill(status=200, content_type=content_type, body=file_path.read_bytes())

        return _handle

    page.route(f"{base_url}/u/styles.css", _serve_from_disk("styles.css", "text/css"))
    page.route(f"{base_url}/u/public.js", _serve_from_disk("public.js", "application/javascript"))


def _goto_public_profile(page: Page, base_url: str):
    page.goto(f"{base_url}/u/{_TARGET_USERNAME}")
    page.wait_for_load_state("networkidle")
    # buildFollowControl() and socialSection() render asynchronously.
    page.wait_for_timeout(600)


def _setup_logged_in_profile(page: Page, base_url: str, *, following: bool,
                              is_self: bool = False, followable: bool = True):
    _seed_viewer_session(page)
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_follow_endpoints(page, base_url, following=following, is_self=is_self,
                             followable=followable)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)


# ── Render states: follow / unfollow / is-self-hidden / login-link ──────────


def test_ac19_logged_out_shows_login_link(page: Page, base_url: str):
    """AC-19 state: no session token -> 'Inicia sesión para seguir' link, no button."""
    page.set_viewport_size({"width": 1280, "height": 800})
    # No _seed_viewer_session(): localStorage has no auth-token key.
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    login_link = page.locator(".pub-follow-login")
    assert login_link.count() == 1, "AC-19: expected the login-link when logged out"
    follow_btn = page.locator(".pub-follow-btn")
    assert follow_btn.count() == 0, "AC-19: no follow button must render when logged out"

    _screenshot(page, "follow-control-logged-out")


def test_ac19_logged_in_not_following_shows_seguir(page: Page, base_url: str):
    """AC-19 state: logged in, not yet following -> 'Seguir' button, aria-pressed=false."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False)

    btn = page.locator(".pub-follow-btn")
    assert btn.count() == 1, "AC-19: expected the follow button to render"
    assert btn.inner_text().strip() == "Seguir"
    assert btn.get_attribute("aria-pressed") == "false"

    _screenshot(page, "follow-control-seguir")


def test_ac19_logged_in_following_shows_siguiendo(page: Page, base_url: str):
    """AC-19 state: logged in, already following -> 'Siguiendo' button, aria-pressed=true."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=True)

    btn = page.locator(".pub-follow-btn")
    assert btn.count() == 1
    assert btn.inner_text().strip() == "Siguiendo"
    assert btn.get_attribute("aria-pressed") == "true"

    _screenshot(page, "follow-control-siguiendo")


def test_ac19_is_self_hides_button(page: Page, base_url: str):
    """AC-19 state: viewing your own public profile -> no follow button at all."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False, is_self=True)

    btn = page.locator(".pub-follow-btn")
    assert btn.count() == 0, "AC-19: no follow button must render on your own profile"
    login_link = page.locator(".pub-follow-login")
    assert login_link.count() == 0, "AC-19: no login-link either -- is_self renders nothing"

    _screenshot(page, "follow-control-is-self")


def test_ac19_expired_token_401_degrades_to_login_link(page: Page, base_url: str):
    """AC-19 state: a token that is present but expired (401 on the status
    read) degrades gracefully to the login-link, not an error."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_follow_endpoints(page, base_url, status_code=401)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    login_link = page.locator(".pub-follow-login")
    assert login_link.count() == 1, "AC-19: expired token (401) must degrade to the login-link"

    _screenshot(page, "follow-control-expired-401")


def test_ac19_follow_click_flips_button_state(page: Page, base_url: str):
    """AC-1/AC-19 interaction: clicking 'Seguir' calls POST /api/follows and
    flips the button to 'Siguiendo'."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False)

    btn = page.locator(".pub-follow-btn")
    assert btn.inner_text().strip() == "Seguir"
    btn.click()
    page.wait_for_timeout(400)

    assert btn.inner_text().strip() == "Siguiendo", "AC-1: button must flip to 'Siguiendo' after a successful follow"
    assert btn.get_attribute("aria-pressed") == "true"

    _screenshot(page, "follow-control-after-follow-click")


def test_ac19_unfollow_click_flips_button_state(page: Page, base_url: str):
    """AC-2/AC-19 interaction: clicking 'Siguiendo' calls DELETE
    /api/follows/{username} and flips the button back to 'Seguir'."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=True)

    btn = page.locator(".pub-follow-btn")
    assert btn.inner_text().strip() == "Siguiendo"
    btn.click()
    page.wait_for_timeout(400)

    assert btn.inner_text().strip() == "Seguir", "AC-2: button must flip to 'Seguir' after a successful unfollow"
    assert btn.get_attribute("aria-pressed") == "false"

    _screenshot(page, "follow-control-after-unfollow-click")


# ── Counts + public-only lists (AC-7/AC-8) ──────────────────────────────────


def test_ac19_counts_and_public_only_handles_render(page: Page, base_url: str):
    """AC-7/AC-8/AC-19: counts render the true totals; the handle lists show
    only the public-only usernames the mock provides (private participants
    are never individually listed -- enforced server-side, verified here as
    the client rendering exactly what the body contains)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    counts_text = page.locator(".pub-social-counts").inner_text()
    assert "5" in counts_text, "AC-7: expected followers_count (5) to render"
    assert "2" in counts_text, "AC-7: expected following_count (2) to render"

    handles = page.locator(".pub-follow-handle")
    handle_texts = [h.inner_text().strip() for h in handles.all()]
    assert any("pubfollower1" in t for t in handle_texts)
    assert any("pubfollower2" in t for t in handle_texts)
    assert any("pubfollowing1" in t for t in handle_texts)

    for handle in handles.all():
        href = handle.get_attribute("href")
        assert href and href.startswith("/u/"), f"AC-8: handle must link to /u/<username>, got {href!r}"

    # No false ARIA container-role promise on the handle-list wrapper (dev-bundle FE lesson).
    wrapper_role = page.evaluate(
        "() => document.querySelector('.pub-follow-handles')?.getAttribute('role')"
    )
    assert wrapper_role is None, (
        f"AC-19: .pub-follow-handles must NOT carry a false role='list' promise, got {wrapper_role!r}"
    )

    _screenshot(page, "follow-counts-and-handles")


# ── Axe scans: desktop + mobile, es-ES ──────────────────────────────────────


def test_ac19_follow_control_a11y_desktop(page: Page, base_url: str):
    """AC-19: public profile (follow control + counts/lists), desktop 1280px,
    axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False)

    _screenshot(page, "follow-control-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "follow-control-desktop-axe")

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations (desktop): "
        + json.dumps(violations, indent=2)
    )


def test_ac19_follow_control_a11y_mobile(page: Page, base_url: str):
    """AC-19: public profile (follow control + counts/lists), mobile 375px,
    axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _setup_logged_in_profile(page, base_url, following=False)

    _screenshot(page, "follow-control-mobile")

    _inject_axe(page, base_url)
    violations = _run_axe(page)

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations (mobile): "
        + json.dumps(violations, indent=2)
    )


def test_ac19_follow_control_siguiendo_state_a11y_desktop(page: Page, base_url: str):
    """AC-19: the 'Siguiendo' (already-following) state is independently
    axe-clean too, not just the 'Seguir' state."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=True)

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "follow-control-siguiendo-axe")

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations in the "
        f"'Siguiendo' state (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac19_login_link_state_a11y_desktop(page: Page, base_url: str):
    """AC-19: the logged-out login-link state is independently axe-clean."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page)
    _screenshot(page, "follow-control-login-link-axe")

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations in the "
        f"login-link state (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac19_follow_control_keyboard_focus(page: Page, base_url: str):
    """AC-19: the Seguir/Siguiendo button is keyboard-operable with a visible
    focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False)

    btn = page.locator(".pub-follow-btn")
    btn.focus()

    focused_class = page.evaluate("() => document.activeElement.className")
    assert "pub-follow-btn" in focused_class

    assert _has_visible_focus(page), "AC-19: no visible focus indicator on the follow button"

    _screenshot(page, "follow-control-keyboard-focus")


def test_ac19_login_link_keyboard_focus(page: Page, base_url: str):
    """AC-19: the login-link is keyboard-reachable with a visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    link = page.locator(".pub-follow-login")
    link.focus()

    focused_class = page.evaluate("() => document.activeElement.className")
    assert "pub-follow-login" in focused_class

    assert _has_visible_focus(page), "AC-19: no visible focus indicator on the login-link"

    _screenshot(page, "follow-control-login-link-focus")


def test_ac19_follow_button_target_24px(page: Page, base_url: str):
    """AC-19: the follow button interactive target is >= 24px (WCAG 2.5.8)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False)

    btn = page.locator(".pub-follow-btn")
    box = btn.bounding_box()
    assert box, "AC-19: follow button has no bounding box"
    assert box["width"] >= 24, f"AC-19: follow button width {box['width']}px < 24px"
    assert box["height"] >= 24, f"AC-19: follow button height {box['height']}px < 24px"

    _screenshot(page, "follow-control-target-size")


def test_ac19_handle_link_target_24px(page: Page, base_url: str):
    """AC-19: a followers/following handle link is >= 24px (WCAG 2.5.8)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_public_config(page, base_url)
    _route_public_profile(page, base_url)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    handle = page.locator(".pub-follow-handle").first
    box = handle.bounding_box()
    assert box, "AC-19: handle link has no bounding box"
    assert box["width"] >= 24, f"AC-19: handle link width {box['width']}px < 24px"
    assert box["height"] >= 24, f"AC-19: handle link height {box['height']}px < 24px"


def test_ac19_es_es_copy(page: Page, base_url: str):
    """AC-19 locale: es-ES copy on both counts and the follow button."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _setup_logged_in_profile(page, base_url, following=False)

    counts_text = page.locator(".pub-social-counts").inner_text()
    assert "seguidor" in counts_text.lower()
    assert "siguiendo" in counts_text.lower()

    btn = page.locator(".pub-follow-btn")
    assert btn.inner_text().strip() == "Seguir"
