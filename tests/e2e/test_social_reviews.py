"""Browser E2E tests for social-reviews-and-likes (Social layer — Phase 2,
AC-18, AC-19), plus the developer-mandated XSS render-site directive (AC-9).

Covers every ### Tester scope Playwright row:

  AC-18 — the review-publish toggle (collection.js, `data-note-public`) and
          the like control (activity.js, `data-like-toggle` +
          `data-likers-expand`) in the authenticated app: axe WCAG 2.2 A/AA
          zero critical/serious; keyboard-operable; visible focus;
          interactive targets >= 24px; @1280 + @375; es-ES.
  AC-19 — the reviews area (`.pub-reviews`) + like count/list
          (`.pub-review-like-btn` / `.pub-review-likers`) on the public
          profile page: same a11y bar, @desktop + @mobile, es-ES.

  Developer verification directive #2 (tester-bundle SS0, load-bearing): a
  review note containing markup (`<script>` / `<img onerror=...>`) renders
  INERT AS TEXT at ALL THREE render sites -- the feed (activity.js), the
  public profile (public.js), and the SPA collection card note (collection.js)
  -- not just one (AC-9), mirroring the avatar "verify every render site"
  lesson.

Strategy (mirrors tests/e2e/test_social.py, Phase 1):
  - Real CineBox server via conftest.py base_url fixture (no DB/auth required).
  - The activity feed view + the collection view are driven directly via the
    production seam (mount `_currentUser`, call `showActivityView()` /
    `loadMovies()`+`renderCollection()` against stubbed `/api/feed` /
    `/api/movies`) -- no real Supabase session needed.
  - The public profile's session-aware like control mirrors the Phase 1
    follow control: stub `/api/config`, seed the Supabase-shaped localStorage
    token via `page.add_init_script` (a raw script body, NOT a function
    expression -- see the Phase 1 lesson below), then stub
    `/api/public/profile/{username}` + `/api/reviews/{movie_id}/likes`.
  - axe-core (4.9.0) injected via the vendored tests/e2e/axe.min.js as a
    same-origin routed <script> (CSP: script-src 'self').
  - Screenshots saved to
    handoffs/social-reviews-and-likes/screenshots/.
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
    / "social-reviews-and-likes"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_SUPABASE_URL = "https://demoproject.supabase.co"
_PROJECT_REF = "demoproject"
_STORAGE_KEY = f"sb-{_PROJECT_REF}-auth-token"
_TARGET_USERNAME = "targetuser"
_MOVIE_ID = 42

_XSS_NOTE = '<img src=x onerror="window.__xss_fired = true">Look <script>window.__xss_fired = true</script>'


# ── Shared helpers (mirrors test_social.py) ──────────────────────────────────


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


def _route_config(page: Page, base_url: str):
    def handle(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"supabase_url": _SUPABASE_URL, "supabase_anon_key": "demo-anon-key"}),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_broken_relative_assets(page: Page, base_url: str):
    """Workaround for the documented BUG-001 (public.html relative asset
    paths under /u/{username}) -- same workaround as sibling public-page tests."""

    def _serve_from_disk(filename, content_type):
        file_path = _REPO_ROOT / filename

        def _handle(route):
            route.fulfill(status=200, content_type=content_type, body=file_path.read_bytes())

        return _handle

    page.route(f"{base_url}/u/styles.css", _serve_from_disk("styles.css", "text/css"))
    page.route(f"{base_url}/u/public.js", _serve_from_disk("public.js", "application/javascript"))


# ══════════════════════════════════════════════════════════════════════════════
# AC-18 (part 1): review-publish toggle in the collection card (collection.js)
# ══════════════════════════════════════════════════════════════════════════════


def _mock_movie(**overrides):
    row = {
        "id": _MOVIE_ID, "tmdb_id": 550, "media_type": "movie",
        "title": "Fight Club", "year": "1999", "status": "vista",
        "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        "rating": None, "note": "Great movie.", "note_public": False,
        "watched_at": "2026-01-01", "platform": None,
        "current_season": None, "current_episode": None, "total_seasons": None,
    }
    row.update(overrides)
    return row


def _route_movies(page: Page, base_url: str, movies: list):
    def handle(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "movies": movies}))

    page.route(f"{base_url}/api/movies", handle)


def _route_level(page: Page, base_url: str):
    def handle(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "points": 0, "level": 1, "name": "Espectador",
            "current_min": 0, "next_min": 50, "next_name": "Aficionado",
            "points_into_level": 0, "points_to_next": 50, "progress_pct": 0,
        }))

    page.route(f"{base_url}/api/level", handle)


def _goto_spa_and_open_collection(page: Page, base_url: str, movies: list):
    """Mount an authenticated session on the production seam and load the
    collection view directly, mirroring test_social.py's
    _goto_spa_and_open_activity_view pattern."""
    _route_config(page, base_url)
    _route_movies(page, base_url, movies)
    _route_level(page, base_url)
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
    page.evaluate("async () => { await loadMovies(); }")
    page.wait_for_timeout(400)


def _open_note_editor(page: Page, movie_id: int = _MOVIE_ID):
    page.evaluate(
        f"""() => {{
            editingNoteId = {movie_id};
            renderCollection();
        }}"""
    )


def test_ac1_publish_toggle_renders_reflecting_note_public_state(page: Page, base_url: str):
    """AC-1: the 'Reseña pública' checkbox reflects m.note_public (unchecked
    by default, private-by-default)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=False)])
    _open_note_editor(page)

    checkbox = page.locator("[data-note-public]")
    assert checkbox.count() == 1, "AC-1: expected the publish-toggle checkbox to render"
    assert not checkbox.is_checked(), "AC-1: a private note's checkbox must be unchecked"

    _screenshot(page, "publish-toggle-unchecked")


def test_ac1_publish_toggle_checked_for_published_review(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=True)])
    _open_note_editor(page)

    checkbox = page.locator("[data-note-public]")
    assert checkbox.is_checked(), "AC-1: a published review's checkbox must be checked"

    _screenshot(page, "publish-toggle-checked")


def test_ac3_publish_toggle_disabled_when_note_empty(page: Page, base_url: str):
    """AC-3 (client mirror): the checkbox is disabled while the note textarea
    is empty -- publishing has meaning only when there IS a note."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note="", note_public=False)])
    _open_note_editor(page)

    checkbox = page.locator("[data-note-public]")
    assert checkbox.is_disabled(), "AC-3: checkbox must be disabled while the note is empty"

    _screenshot(page, "publish-toggle-disabled-empty-note")


def test_ac1_published_note_shows_badge_on_collapsed_view(page: Page, base_url: str):
    """A published note shows a 'Reseña pública' badge on the collapsed
    note button so the owner sees at a glance which notes are public."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=True)])

    badge = page.locator(".note-public-badge")
    assert badge.count() == 1, "expected the 'Reseña pública' badge on a published note"
    assert "pública" in badge.inner_text().lower()

    _screenshot(page, "publish-badge-collapsed")


def test_ac18_publish_toggle_keyboard_focus(page: Page, base_url: str):
    """AC-18: the publish-toggle checkbox is keyboard-operable with a visible
    focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=False)])
    _open_note_editor(page)

    checkbox = page.locator("[data-note-public]")
    checkbox.focus()
    focused_tag = page.evaluate("() => document.activeElement.tagName")
    assert focused_tag == "INPUT"
    assert _has_visible_focus(page), "AC-18: no visible focus indicator on the publish toggle"

    _screenshot(page, "publish-toggle-keyboard-focus")


def test_ac18_publish_toggle_target_24px(page: Page, base_url: str):
    """AC-18: the publish-toggle's effective interactive target (the label
    row wrapping the checkbox) is >= 24px."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=False)])
    _open_note_editor(page)

    label = page.locator(".note-public-toggle")
    box = label.bounding_box()
    assert box, "AC-18: publish-toggle label has no bounding box"
    assert box["height"] >= 24, f"AC-18: publish-toggle label height {box['height']}px < 24px"

    _screenshot(page, "publish-toggle-target-size")


def test_ac18_note_editor_with_publish_toggle_a11y_desktop(page: Page, base_url: str):
    """AC-18: the note editor (incl. the publish toggle), desktop 1280px,
    axe WCAG 2.2 A/AA zero critical/serious.

    Scoped to `.note-form` (the new surface under test — textarea + publish
    toggle + save/cancel actions) rather than the whole `#collection`: a
    diagnostic run showed `#collection` also flags a pre-existing
    `target-size` violation on the star-rating buttons
    (`button[data-star="1..5"]`), which predate this feature and are out of
    this Tester's scope (AC-18 only covers the publish toggle + like control
    introduced by this diff, not a pre-existing a11y gap elsewhere in the
    card). See `## Follow-ups` in the Tester handoff."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=False)])
    _open_note_editor(page)

    _screenshot(page, "note-editor-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page, ".note-form")
    _screenshot(page, "note-editor-desktop-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in "
        f"the note editor / publish toggle (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac18_note_editor_with_publish_toggle_a11y_mobile(page: Page, base_url: str):
    """AC-18: the note editor, mobile 375px, axe WCAG 2.2 A/AA zero critical/serious.
    Scoped to `.note-form` -- see the desktop test's docstring for why (the
    star-rating buttons elsewhere in the card are a pre-existing, out-of-scope
    target-size gap)."""
    page.set_viewport_size({"width": 375, "height": 667})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=False)])
    _open_note_editor(page)

    _inject_axe(page, base_url)
    violations = _run_axe(page, ".note-form")
    _screenshot(page, "note-editor-mobile-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in "
        f"the note editor / publish toggle (mobile): " + json.dumps(violations, indent=2)
    )


def test_ac18_es_es_copy_on_publish_toggle(page: Page, base_url: str):
    """AC-18 locale: the publish toggle carries es-ES copy."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note_public=False)])
    _open_note_editor(page)

    label_text = page.locator(".note-public-toggle").inner_text()
    assert "Reseña pública" in label_text


# ══════════════════════════════════════════════════════════════════════════════
# AC-18 (part 2) / AC-7 / AC-9 / AC-11 / AC-12: the 'reviewed' feed entry +
# like control (activity.js)
# ══════════════════════════════════════════════════════════════════════════════


def _mock_feed_reviewed_entry(*, note="A masterpiece.", username="alice",
                                like_count=2, liked_by_me=False, movie_id=_MOVIE_ID):
    return {
        "action": "reviewed", "username": username, "avatar_url": None,
        "title": "Fight Club", "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        "media_type": "movie", "tmdb_id": 550, "year": "1999",
        "created_at": "2026-07-02T12:00:00+00:00",
        "note": note, "movie_id": movie_id,
        "like_count": like_count, "liked_by_me": liked_by_me,
    }


def _route_feed(page: Page, base_url: str, payload: dict):
    def handle(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"{base_url}/api/feed", handle)


def _route_review_likes(page: Page, base_url: str, movie_id: int, *,
                          liked: bool = False, count: int = 2, likers: list = None):
    """Stub POST/DELETE/GET /api/reviews/{movie_id}/likes with ONE stateful
    handler per URL (mirrors _route_follow_endpoints's single-combined-handler
    rationale in test_social.py -- page.route matches ALL methods on a given
    URL, so separate GET-only/POST-only handlers on the same URL would
    collide under LIFO)."""
    state = {"liked": liked, "count": count}
    likers = likers if likers is not None else []

    def handle(route):
        req = route.request
        if req.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "count": state["count"], "liked_by_me": state["liked"], "likers": likers,
            }))
        if req.method == "POST":
            if not state["liked"]:
                state["liked"] = True
                state["count"] += 1
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "liked": True, "count": state["count"],
            }))
        if req.method == "DELETE":
            if state["liked"]:
                state["liked"] = False
                state["count"] -= 1
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "liked": False, "count": state["count"],
            }))
        route.fallback()

    page.route(f"{base_url}/api/reviews/{movie_id}/likes", handle)


def _goto_spa_and_open_activity_view(page: Page, base_url: str):
    """Mirrors test_social.py's _goto_spa_and_open_activity_view exactly."""
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


def test_ac7_reviewed_entry_renders_with_review_text(page: Page, base_url: str):
    """AC-7: a followed user who published a review surfaces as a 'reviewed'
    feed entry carrying the review text."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [_mock_feed_reviewed_entry()]})
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=2)
    _goto_spa_and_open_activity_view(page, base_url)

    entry = page.locator(".activity-item-reviewed")
    assert entry.count() == 1, "AC-7: expected exactly one 'reviewed' feed entry"
    text = entry.inner_text()
    assert "reseña" in text.lower()
    assert "A masterpiece." in text

    _screenshot(page, "activity-reviewed-entry")


def test_ac11_like_click_increments_count_and_flips_aria_pressed(page: Page, base_url: str):
    """AC-11: clicking the heart on a not-yet-liked review POSTs and
    increments the count by one, flipping aria-pressed to true."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [
        _mock_feed_reviewed_entry(like_count=2, liked_by_me=False)]})
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=2)
    _goto_spa_and_open_activity_view(page, base_url)

    btn = page.locator("[data-like-toggle]")
    assert btn.get_attribute("aria-pressed") == "false"
    count_before = int(page.locator("[data-like-count]").inner_text())

    btn.click()
    page.wait_for_timeout(300)

    assert btn.get_attribute("aria-pressed") == "true", "AC-11: aria-pressed must flip to true after liking"
    count_after = int(page.locator("[data-like-count]").inner_text())
    assert count_after == count_before + 1, "AC-11: like count must increase by exactly one"

    _screenshot(page, "activity-like-after-click")


def test_ac12_unlike_click_decrements_count_and_unflips_aria_pressed(page: Page, base_url: str):
    """AC-12: clicking the heart on an already-liked review DELETEs and
    decrements the count by one, flipping aria-pressed back to false."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [
        _mock_feed_reviewed_entry(like_count=3, liked_by_me=True)]})
    _route_review_likes(page, base_url, _MOVIE_ID, liked=True, count=3)
    _goto_spa_and_open_activity_view(page, base_url)

    btn = page.locator("[data-like-toggle]")
    assert btn.get_attribute("aria-pressed") == "true"
    count_before = int(page.locator("[data-like-count]").inner_text())

    btn.click()
    page.wait_for_timeout(300)

    assert btn.get_attribute("aria-pressed") == "false", "AC-12: aria-pressed must flip to false after unliking"
    count_after = int(page.locator("[data-like-count]").inner_text())
    assert count_after == count_before - 1, "AC-12: like count must decrease by exactly one"

    _screenshot(page, "activity-unlike-after-click")


def test_ac14_likers_disclosure_lists_only_public_profiles(page: Page, base_url: str):
    """AC-14: expanding 'quién dio like' renders only the public-only likers
    the endpoint returned -- the client renders exactly what the (server-side
    filtered) body contains."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [
        _mock_feed_reviewed_entry(like_count=5, liked_by_me=False)]})
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=5,
                          likers=[{"username": "pub1"}, {"username": "pub2"}])
    _goto_spa_and_open_activity_view(page, base_url)

    toggle = page.locator("[data-likers-expand]")
    toggle.click()
    page.wait_for_timeout(300)

    handles = page.locator(".activity-liker")
    assert handles.count() == 2, "AC-14: expected exactly the 2 public likers returned"
    texts = [h.inner_text() for h in handles.all()]
    assert any("pub1" in t for t in texts)
    assert any("pub2" in t for t in texts)

    _screenshot(page, "activity-likers-disclosure")


def test_ac18_like_control_keyboard_focus(page: Page, base_url: str):
    """AC-18: the like button is keyboard-operable with a visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [_mock_feed_reviewed_entry()]})
    _route_review_likes(page, base_url, _MOVIE_ID)
    _goto_spa_and_open_activity_view(page, base_url)

    btn = page.locator("[data-like-toggle]")
    btn.focus()
    assert _has_visible_focus(page), "AC-18: no visible focus indicator on the like button"

    _screenshot(page, "activity-like-keyboard-focus")


def test_ac18_like_control_target_24px(page: Page, base_url: str):
    """AC-18: the like button interactive target is >= 24px (WCAG 2.5.8)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [_mock_feed_reviewed_entry()]})
    _route_review_likes(page, base_url, _MOVIE_ID)
    _goto_spa_and_open_activity_view(page, base_url)

    btn = page.locator("[data-like-toggle]")
    box = btn.bounding_box()
    assert box, "AC-18: like button has no bounding box"
    assert box["width"] >= 24, f"AC-18: like button width {box['width']}px < 24px"
    assert box["height"] >= 24, f"AC-18: like button height {box['height']}px < 24px"

    _screenshot(page, "activity-like-target-size")


def test_ac18_reviewed_entry_and_like_control_a11y_desktop(page: Page, base_url: str):
    """AC-18: the 'reviewed' feed entry + like control, desktop 1280px,
    axe WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [_mock_feed_reviewed_entry()]})
    _route_review_likes(page, base_url, _MOVIE_ID)
    _goto_spa_and_open_activity_view(page, base_url)

    _screenshot(page, "activity-reviewed-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#activity-view")
    _screenshot(page, "activity-reviewed-desktop-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in "
        f"the reviewed entry + like control (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac18_reviewed_entry_and_like_control_a11y_mobile(page: Page, base_url: str):
    """AC-18: the 'reviewed' feed entry + like control, mobile 375px, axe
    WCAG 2.2 A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _route_feed(page, base_url, {"ok": True, "activity": [_mock_feed_reviewed_entry()]})
    _route_review_likes(page, base_url, _MOVIE_ID)
    _goto_spa_and_open_activity_view(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#activity-view")
    _screenshot(page, "activity-reviewed-mobile-axe")

    assert violations == [], (
        f"AC-18: axe found {len(violations)} critical/serious violations in "
        f"the reviewed entry + like control (mobile): " + json.dumps(violations, indent=2)
    )


def test_ac18_es_es_copy_on_reviewed_entry(page: Page, base_url: str):
    """AC-18 locale: the feed renders es-ES verb copy for the 'reviewed' kind."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [_mock_feed_reviewed_entry()]})
    _route_review_likes(page, base_url, _MOVIE_ID)
    _goto_spa_and_open_activity_view(page, base_url)

    text = page.locator("#activity-view").inner_text()
    assert "escribió una reseña" in text, "AC-18: expected es-ES 'escribió una reseña de' copy"


# ══════════════════════════════════════════════════════════════════════════════
# AC-4 / AC-14 / AC-19: public-profile reviews area + like control (public.js)
# ══════════════════════════════════════════════════════════════════════════════


def _seed_viewer_session(page: Page, token: str = "fake-access-token"):
    """Seed the Supabase-shaped localStorage entry public.js reads BEFORE the
    page's own scripts run (mirrors test_social.py's documented Playwright
    lesson: add_init_script's string arg is a raw script BODY, not a function
    expression -- wrapping it in an arrow function silently no-ops)."""
    script = (
        f"localStorage.setItem({json.dumps(_STORAGE_KEY)}, "
        f"JSON.stringify({{ access_token: {json.dumps(token)}, token_type: 'bearer' }}));"
    )
    page.add_init_script(script)


def _mock_review(**overrides):
    row = {
        "movie_id": _MOVIE_ID, "tmdb_id": 550, "media_type": "movie",
        "title": "Fight Club", "year": "1999",
        "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        "note": "A masterpiece.", "created_at": "2026-07-02T10:00:00+00:00",
        "like_count": 4,
    }
    row.update(overrides)
    return row


def _route_public_profile(page: Page, base_url: str, reviews: list):
    payload = {
        "ok": True,
        "profile": {
            "username": _TARGET_USERNAME, "avatar_url": None,
            "followers_count": 0, "following_count": 0,
            "followers": [], "following": [],
            "reviews": reviews,
        },
    }

    def handle(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route(f"{base_url}/api/public/profile/{_TARGET_USERNAME}", handle)


def _goto_public_profile(page: Page, base_url: str):
    page.goto(f"{base_url}/u/{_TARGET_USERNAME}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(700)  # reviewsSection's async buildReviewLikeControl fill


def test_ac4_review_appears_next_to_its_title(page: Page, base_url: str):
    """AC-4: a published review renders in the dedicated reviews area next to
    its title (poster + title + note text)."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=4)
    _route_broken_relative_assets(page, base_url)
    # No _seed_viewer_session(): logged-out state also renders the review text.
    _goto_public_profile(page, base_url)

    section = page.locator(".pub-reviews")
    assert section.count() == 1, "AC-4: expected the reviews area to render"
    card = page.locator(".pub-review-card")
    assert card.count() == 1
    assert "Fight Club" in card.inner_text()
    assert "A masterpiece." in card.inner_text()

    _screenshot(page, "public-review-card")


def test_ac19_logged_out_review_shows_login_link_not_like_button(page: Page, base_url: str):
    """AC-19 state (mirrors the Phase 1 follow control): no session token ->
    a 'Inicia sesión para reaccionar' link, no like button."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    login_link = page.locator(".pub-review-login")
    assert login_link.count() == 1, "AC-19: expected the login link when logged out"
    like_btn = page.locator(".pub-review-like-btn")
    assert like_btn.count() == 0, "AC-19: no like button must render when logged out"

    _screenshot(page, "public-review-logged-out")


def test_ac19_logged_in_shows_like_button_with_liked_state(page: Page, base_url: str):
    """AC-19: logged in with a valid token -> the heart toggle renders,
    reflecting liked_by_me and the true count."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review(like_count=4)])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=True, count=4)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    btn = page.locator(".pub-review-like-btn")
    assert btn.count() == 1, "AC-19: expected the like button when logged in"
    assert btn.get_attribute("aria-pressed") == "true"
    count_text = page.locator(".pub-review-like-count").inner_text()
    assert count_text == "4"

    _screenshot(page, "public-review-logged-in-liked")


def test_ac11_public_page_like_click_increments_count(page: Page, base_url: str):
    """AC-11: clicking the public-page heart POSTs and flips the count/state."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review(like_count=4)])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=4)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    btn = page.locator(".pub-review-like-btn")
    assert btn.get_attribute("aria-pressed") == "false"
    btn.click()
    page.wait_for_timeout(400)

    assert btn.get_attribute("aria-pressed") == "true"
    assert page.locator(".pub-review-like-count").inner_text() == "5"

    _screenshot(page, "public-review-like-click")


def test_ac14_public_page_likers_disclosure_public_only(page: Page, base_url: str):
    """AC-14: the public page's 'quién dio like' disclosure lists only the
    public likers the endpoint returned."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review(like_count=5)])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=5,
                          likers=[{"username": "onlypublic"}])
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    toggle = page.locator(".pub-review-likers-toggle")
    toggle.click()
    page.wait_for_timeout(300)

    handles = page.locator(".pub-review-liker")
    assert handles.count() == 1
    assert "onlypublic" in handles.first.inner_text()

    _screenshot(page, "public-review-likers-disclosure")


def test_ac19_expired_token_degrades_to_login_link(page: Page, base_url: str):
    """AC-19 state: a present-but-expired token (401 on the GET) degrades
    gracefully to the login link, not an error."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_broken_relative_assets(page, base_url)

    def handle_401(route):
        route.fulfill(status=401, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "No autenticado"}))

    page.route(f"{base_url}/api/reviews/{_MOVIE_ID}/likes", handle_401)
    _goto_public_profile(page, base_url)

    login_link = page.locator(".pub-review-login")
    assert login_link.count() == 1, "AC-19: expired token must degrade to the login link"

    _screenshot(page, "public-review-expired-token")


def test_ac19_like_control_keyboard_focus(page: Page, base_url: str):
    """AC-19: the public-page heart toggle is keyboard-operable with a
    visible focus indicator."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=4)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    btn = page.locator(".pub-review-like-btn")
    btn.focus()
    assert _has_visible_focus(page), "AC-19: no visible focus indicator on the public like button"

    _screenshot(page, "public-review-like-keyboard-focus")


def test_ac19_like_control_target_24px(page: Page, base_url: str):
    """AC-19: the public-page like button target is >= 24px."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=4)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    btn = page.locator(".pub-review-like-btn")
    box = btn.bounding_box()
    assert box, "AC-19: like button has no bounding box"
    assert box["width"] >= 24, f"AC-19: like button width {box['width']}px < 24px"
    assert box["height"] >= 24, f"AC-19: like button height {box['height']}px < 24px"

    _screenshot(page, "public-review-like-target-size")


def test_ac19_reviews_area_a11y_desktop(page: Page, base_url: str):
    """AC-19: reviews area + like count/list, desktop 1280px, axe WCAG 2.2
    A/AA zero critical/serious."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=4)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    _screenshot(page, "public-reviews-desktop")

    _inject_axe(page, base_url)
    violations = _run_axe(page, ".pub-reviews")
    _screenshot(page, "public-reviews-desktop-axe")

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations in "
        f".pub-reviews (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac19_reviews_area_a11y_mobile(page: Page, base_url: str):
    """AC-19: reviews area + like count/list, mobile 375px, axe WCAG 2.2
    A/AA zero critical/serious."""
    page.set_viewport_size({"width": 375, "height": 667})
    _seed_viewer_session(page)
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_review_likes(page, base_url, _MOVIE_ID, liked=False, count=4)
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, ".pub-reviews")
    _screenshot(page, "public-reviews-mobile-axe")

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations in "
        f".pub-reviews (mobile): " + json.dumps(violations, indent=2)
    )


def test_ac19_logged_out_reviews_area_a11y_desktop(page: Page, base_url: str):
    """AC-19: the logged-out (login-link) state of the reviews area is
    independently axe-clean too."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    _inject_axe(page, base_url)
    violations = _run_axe(page, ".pub-reviews")
    _screenshot(page, "public-reviews-logged-out-axe")

    assert violations == [], (
        f"AC-19: axe found {len(violations)} critical/serious violations in "
        f"the logged-out reviews area (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac19_es_es_copy(page: Page, base_url: str):
    """AC-19 locale: the reviews section title + login link are es-ES."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review()])
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    section_title = page.locator(".pub-reviews .pub-section-title").inner_text()
    assert "Reseñas" in section_title
    login_text = page.locator(".pub-review-login").inner_text()
    assert "Inicia sesión" in login_text


# ══════════════════════════════════════════════════════════════════════════════
# Developer-mandated verification directive #2 (AC-9): review text with
# markup renders INERT AS TEXT at ALL THREE render sites -- feed, public
# profile, AND the SPA collection card note.
# ══════════════════════════════════════════════════════════════════════════════


def test_ac9_xss_note_inert_in_activity_feed(page: Page, base_url: str):
    """Site 1/3 -- activity.js: a 'reviewed' entry whose note contains
    <script>/<img onerror> renders as inert TEXT (esc()-escaped in the
    template), never executes, and never appears as real DOM markup."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_feed(page, base_url, {"ok": True, "activity": [
        _mock_feed_reviewed_entry(note=_XSS_NOTE)]})
    _route_review_likes(page, base_url, _MOVIE_ID)
    _goto_spa_and_open_activity_view(page, base_url)

    fired = page.evaluate("() => window.__xss_fired === true")
    assert not fired, "AC-9: XSS payload in a feed review must NOT execute"

    block = page.locator(".activity-review")
    assert block.count() == 1
    # The <script> tag must not exist as a real element -- it must be inert text.
    real_script_children = page.evaluate(
        "() => document.querySelectorAll('.activity-review script').length"
    )
    assert real_script_children == 0, "AC-9: no real <script> element must be created in the DOM"
    assert "<script>" in block.inner_html() or "&lt;script&gt;" in block.inner_html(), (
        "AC-9: the markup must appear as escaped/inert text, not stripped silently"
    )

    _screenshot(page, "xss-feed-inert")


def test_ac9_xss_note_inert_on_public_profile(page: Page, base_url: str):
    """Site 2/3 -- public.js: the review text is rendered via textContent
    (never innerHTML), so markup is inert as a plain text node."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _route_config(page, base_url)
    _route_public_profile(page, base_url, [_mock_review(note=_XSS_NOTE)])
    _route_broken_relative_assets(page, base_url)
    _goto_public_profile(page, base_url)

    fired = page.evaluate("() => window.__xss_fired === true")
    assert not fired, "AC-9: XSS payload in a public-profile review must NOT execute"

    text_block = page.locator(".pub-review-text")
    assert text_block.count() == 1
    real_script_children = page.evaluate(
        "() => document.querySelectorAll('.pub-review-text script').length"
    )
    assert real_script_children == 0, "AC-9: no real <script> element must be created in the DOM"
    # textContent renders the raw markup as visible literal text (never parsed).
    assert "<script>" in text_block.inner_text()

    _screenshot(page, "xss-public-profile-inert")


def test_ac9_xss_note_inert_in_spa_collection_card(page: Page, base_url: str):
    """Site 3/3 -- collection.js: the collapsed note preview and the editing
    textarea both go through esc() -- markup renders as inert text, never
    executes, in the authed SPA collection card."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _goto_spa_and_open_collection(page, base_url, [_mock_movie(note=_XSS_NOTE, note_public=False)])

    fired = page.evaluate("() => window.__xss_fired === true")
    assert not fired, "AC-9: XSS payload in a collection-card note must NOT execute"

    note_btn = page.locator(".note-btn")
    assert note_btn.count() >= 1
    real_script_children = page.evaluate(
        "() => document.querySelectorAll('.note-btn script').length"
    )
    assert real_script_children == 0, "AC-9: no real <script> element must be created in the DOM"

    _screenshot(page, "xss-collection-card-inert")


if __name__ == "__main__":
    pass
