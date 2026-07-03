"""Browser E2E tests for modal-edit-section (Collection — Phase 2).

Covers the ### Tester scope live-browser rows of the task DoD:

  AC-1  — a collection title WITH a tmdb_id opens the detail modal and the
          modal shows an always-expanded edit section; a non-collection title
          shows NO edit section.
  AC-2  — a tmdb-LESS title's poster opens the modal in edit-only mode with NO
          /api/details (nor /api/similar) request and full editability.
  AC-3/4/5/7 — status / rating / watch-date / platform saves persist (PATCH
          /api/movies/{id}) and the modal stays open reflecting the change.
  AC-6  — the series progress editor rejects a season > total_seasons with the
          same es-ES message the card shows (and does not PATCH).
  AC-8  — the "Reseña pública" toggle is disabled while the note is empty.
  AC-10 — delete removes the title and closes the modal.
  AC-11 — a successful non-delete save keeps the modal open + re-rendered.
  AC-13 — a note with markup renders INERT AS TEXT in the modal editor (esc()).
  AC-15 — the modal edit section passes axe WCAG 2.2 A/AA (zero critical/serious)
          @1280 + @375, keyboard-operable with visible focus, targets >= 24px,
          es-ES.

Strategy mirrors tests/e2e/test_social_reviews.py exactly:
  - Real Cinephora server via conftest.py base_url fixture (no DB/auth).
  - The authenticated SPA is driven via the production seam: mount
    `_currentUser`, stub `/api/config` + `/api/movies` + `/api/level`, call
    `loadMovies()`. The modal is opened by clicking the collection card poster
    (detail mode) or by calling `openEditOnly(id)` (edit-only mode).
  - PATCH/DELETE /api/movies/{id} are stubbed by ONE stateful handler per URL
    (page.route matches ALL methods on a URL — branch on method), updating an
    in-memory store that the GET /api/movies re-fetch reads back, so the on-save
    re-render reflects the persisted value (AC-11).
  - axe-core injected via the vendored tests/e2e/axe.min.js as a same-origin
    routed <script> (CSP: script-src 'self').
  - Screenshots saved to handoffs/modal-edit-section/screenshots/.
"""

import json
import re
from pathlib import Path

from playwright.sync_api import Page

# ── Paths ──────────────────────────────────────────────────────────────────────
_E2E_DIR = Path(__file__).resolve().parent
AXE_JS = _E2E_DIR / "axe.min.js"
_SCREENSHOTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "handoffs"
    / "modal-edit-section"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_SUPABASE_URL = "https://demoproject.supabase.co"
_MOVIE_ID = 42
_TMDB_ID = 550

_XSS_NOTE = '<img src=x onerror="window.__xss_fired = true">Look <script>window.__xss_fired = true</script>'


# ── Shared helpers (mirror test_social_reviews.py) ───────────────────────────


def _inject_axe(page: Page, base_url: str):
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
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"supabase_url": _SUPABASE_URL, "supabase_anon_key": "demo-anon-key"}
            ),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_level(page: Page, base_url: str):
    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "points": 0,
                    "level": 1,
                    "name": "Espectador",
                    "current_min": 0,
                    "next_min": 50,
                    "next_name": "Aficionado",
                    "points_into_level": 0,
                    "points_to_next": 50,
                    "progress_pct": 0,
                }
            ),
        )

    page.route(f"{base_url}/api/level", handle)


def _mock_movie(**overrides):
    row = {
        "id": _MOVIE_ID,
        "tmdb_id": _TMDB_ID,
        "media_type": "movie",
        "title": "Fight Club",
        "year": "1999",
        "status": "vista",
        "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        "rating": None,
        "note": "Great movie.",
        "note_public": False,
        "watched_at": "2026-01-01",
        "platform": None,
        "current_season": None,
        "current_episode": None,
        "total_seasons": None,
    }
    row.update(overrides)
    return row


def _route_movies_stateful(page: Page, base_url: str, initial: list) -> dict:
    """GET/POST /api/movies returns the mutable store; PATCH/DELETE
    /api/movies/{id} mutate it (one stateful handler per URL)."""
    store = {"movies": [dict(m) for m in initial], "patches": [], "deletes": []}

    def handle_collection(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "movies": store["movies"]}),
        )

    page.route(f"{base_url}/api/movies", handle_collection)

    def handle_item(route):
        req = route.request
        if req.method == "PATCH":
            payload = json.loads(req.post_data or "{}")
            store["patches"].append(payload)
            for m in store["movies"]:
                if m["id"] == _MOVIE_ID:
                    m.update(payload)
            return route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True}),
            )
        if req.method == "DELETE":
            store["deletes"].append(_MOVIE_ID)
            store["movies"] = [m for m in store["movies"] if m["id"] != _MOVIE_ID]
            return route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True}),
            )
        return route.fallback()

    page.route(f"{base_url}/api/movies/{_MOVIE_ID}", handle_item)
    return store


def _details_payload(**overrides):
    d = {
        "overview": "An office worker forms an underground club.",
        "genres": ["Drama"],
        "genre_ids": [18],
        "runtime": 139,
        "title": "Fight Club",
        "poster_path": "/p.jpg",
        "backdrop_path": "/bd.jpg",
        "vote_average": 8.4,
        "trailer": "",
        "dir_label": "Dirección",
        "directors": ["David Fincher"],
        "cast": [{"name": "Edward Norton", "profile_path": "/c.jpg"}],
        "providers": [],
        "providers_link": "",
        "total_seasons": None,
    }
    d.update(overrides)
    return d


def _route_details(page: Page, base_url: str, details: dict, hits: dict):
    def handle(route):
        hits["details"] = hits.get("details", 0) + 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "details": details}),
        )

    page.route(re.compile(re.escape(f"{base_url}/api/details") + r"\?.*"), handle)


def _route_similar(page: Page, base_url: str, hits: dict):
    def handle(route):
        hits["similar"] = hits.get("similar", 0) + 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "results": []}),
        )

    page.route(re.compile(re.escape(f"{base_url}/api/similar") + r"\?.*"), handle)


def _goto_spa(page: Page, base_url: str, movies: list) -> dict:
    _route_config(page, base_url)
    _route_level(page, base_url)
    store = _route_movies_stateful(page, base_url, movies)
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
    page.wait_for_timeout(300)
    return store


def _open_detail_modal(page: Page):
    """Click the collection card poster (detail mode) and wait for the edit
    section to render inside the modal."""
    page.locator(".card .poster[data-tmdb]").first.click()
    page.wait_for_selector("#modal-edit-section", timeout=4000)
    page.wait_for_timeout(200)


# ══════════════════════════════════════════════════════════════════════════════
# AC-1 — detail-mode edit section
# ══════════════════════════════════════════════════════════════════════════════


def test_ac1_detail_modal_shows_edit_section_for_collection_title(
    page: Page, base_url: str
):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_movie()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    section = page.locator("#modal-edit-section")
    assert section.count() == 1, "AC-1: expected the edit section inside the modal"
    # Every editor is exposed.
    assert page.locator(".modal-status-pill").count() == 4
    assert page.locator("#modal-edit-section .stars .star").count() == 5
    assert page.locator("#modal-edit-date").count() == 1
    assert page.locator("#modal-edit-section .platform-chip").count() >= 1
    assert page.locator("#modal-edit-note").count() == 1
    assert page.locator("[data-action='edit-add-to-list']").count() == 1
    assert page.locator("[data-action='edit-delete']").count() == 1
    _screenshot(page, "ac1-detail-edit-section")


def test_ac1_no_edit_section_for_non_collection_title(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [])  # empty collection
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    page.evaluate("async () => { await openDetail(999, 'movie'); }")
    page.wait_for_timeout(500)

    assert page.locator("#modal-edit-section").count() == 0, (
        "AC-1: a non-collection title must show NO edit section"
    )
    # The add-to-collection buttons still render (unchanged behaviour).
    assert page.locator("#modal-add-btns").count() == 1
    _screenshot(page, "ac1-non-collection-no-edit-section")


# ══════════════════════════════════════════════════════════════════════════════
# AC-2 — edit-only mode (tmdb-less), NO /api/details request
# ══════════════════════════════════════════════════════════════════════════════


def test_ac2_edit_only_mode_no_details_request(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    # tmdb-less title (manually added).
    _goto_spa(page, base_url, [_mock_movie(tmdb_id=None, title="Cortometraje casero")])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)

    # The poster carries data-edit-id (not data-tmdb) — click it.
    poster = page.locator(".card .poster[data-edit-id]")
    assert poster.count() == 1, "AC-2: tmdb-less poster must carry data-edit-id"
    poster.first.click()
    page.wait_for_selector("#modal-edit-section", timeout=4000)
    page.wait_for_timeout(400)

    assert hits.get("details", 0) == 0, (
        "AC-2: edit-only mode must NOT call /api/details"
    )
    assert hits.get("similar", 0) == 0, (
        "AC-2: edit-only mode must NOT call /api/similar"
    )
    # No hero/backdrop/cast in edit-only mode.
    assert page.locator("#modal .modal-hero").count() == 0
    assert page.locator("#modal .modal-cast").count() == 0
    # Full editability.
    assert page.locator(".modal-status-pill").count() == 4
    assert page.locator("#modal-edit-note").count() == 1
    _screenshot(page, "ac2-edit-only-mode")


# ══════════════════════════════════════════════════════════════════════════════
# AC-3 / AC-11 — status change persists, modal stays open
# ══════════════════════════════════════════════════════════════════════════════


def test_ac3_status_change_persists_and_modal_stays_open(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    store = _goto_spa(page, base_url, [_mock_movie(status="pendiente")])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    page.locator(".modal-status-pill[data-status='viendo']").click()
    page.wait_for_timeout(500)

    assert {"status": "viendo"} in store["patches"] or any(
        p.get("status") == "viendo" for p in store["patches"]
    ), "AC-3: status change must PATCH /api/movies/{id}"
    # AC-11: modal stays open and the edit section re-rendered from the update.
    assert not page.locator("#modal").is_hidden(), (
        "AC-11: modal must stay open after save"
    )
    assert page.locator("#modal-edit-section").count() == 1
    selected = page.locator(".modal-status-pill.is-active").get_attribute("data-status")
    assert selected == "viendo", "AC-11: re-render must reflect the saved status"
    _screenshot(page, "ac3-status-change")


# ══════════════════════════════════════════════════════════════════════════════
# AC-4 — rating set + clear
# ══════════════════════════════════════════════════════════════════════════════


def test_ac4_rating_set_and_clear(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    store = _goto_spa(page, base_url, [_mock_movie(rating=None)])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    # Click the 4th star → rating 4.
    page.locator("#modal-edit-section .stars .star").nth(3).click()
    page.wait_for_timeout(500)
    assert any(p.get("rating") == 4 for p in store["patches"]), (
        "AC-4: rating must persist"
    )

    # Click the same star again → clear (null).
    page.locator("#modal-edit-section .stars .star").nth(3).click()
    page.wait_for_timeout(500)
    assert any(p.get("rating") is None for p in store["patches"]), (
        "AC-4: click-to-clear → null"
    )
    _screenshot(page, "ac4-rating")


# ══════════════════════════════════════════════════════════════════════════════
# AC-5 — watch-date save + clear
# ══════════════════════════════════════════════════════════════════════════════


def test_ac5_watch_date_save_and_clear(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    store = _goto_spa(page, base_url, [_mock_movie(watched_at="2026-01-01")])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    page.locator("#modal-edit-date").fill("2026-05-20")
    page.locator("[data-action='edit-date-save']").click()
    page.wait_for_timeout(500)
    assert any(p.get("watched_at") == "2026-05-20" for p in store["patches"]), (
        "AC-5: date persists"
    )

    # Clear.
    page.locator("[data-action='edit-date-clear']").click()
    page.wait_for_timeout(500)
    assert any(p.get("watched_at") is None for p in store["patches"]), (
        "AC-5: date clear → null"
    )
    _screenshot(page, "ac5-watch-date")


# ══════════════════════════════════════════════════════════════════════════════
# AC-6 — series progress: season > total_seasons rejected (es-ES message, no PATCH)
# ══════════════════════════════════════════════════════════════════════════════


def test_ac6_manual_progress_editor_superseded_by_episode_tracker(page: Page, base_url: str):
    """AC-6 (modal-edit-section) is SUPERSEDED by BR-13/AC-14 of the later
    series-episode-progress feature: the manual season/episode number editor
    this test used to exercise (`.progress-form [data-field='season']` +
    `edit-progress-save`, client-side season-over-total rejection) was
    deliberately REMOVED and replaced by the season selector + episode-list
    tracker (`.modal-ep-season-pill`, `[data-action='ep-toggle']`, ...). Position
    is now derived-only from marked episodes; there is no manual season input
    to reject an over-total value against. This test was updated (not
    deleted) by the series-episode-progress Tester so the suite does not
    carry a permanently-red assertion for behaviour the spec intentionally
    removed — see `Cinephora-docs/specs/Collection/series-episode-progress-specs.md`
    BR-13 / AC-14."""
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(
        page,
        base_url,
        [
            _mock_movie(
                media_type="tv",
                status="viendo",
                total_seasons=3,
                current_season=1,
                current_episode=1,
            )
        ],
    )
    _route_details(page, base_url, _details_payload(total_seasons=3), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    assert page.locator("#modal-edit-section .progress-form").count() == 0, (
        "AC-14 (series-episode-progress): the manual progress-form must be gone"
    )
    assert page.locator("[data-action='edit-progress-save']").count() == 0, (
        "AC-14 (series-episode-progress): the manual save action must be gone"
    )
    # The episode tracker's own season selector (pills) is what remains for a tv title.
    assert page.locator(".modal-ep-season-pill").count() >= 1, (
        "BR-13 (series-episode-progress): the season/episode tracker replaces "
        "the removed manual editor"
    )
    _screenshot(page, "ac6-series-progress-superseded")


def test_ac6_progress_hidden_for_vista_series(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(
        page, base_url, [_mock_movie(media_type="tv", status="vista", total_seasons=3)]
    )
    _route_details(page, base_url, _details_payload(total_seasons=3), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    assert page.locator("#modal-edit-section .progress-form").count() == 0, (
        "edge case: a vista series shows no progress editor (mirrors the card)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# AC-7 — platform pick + clear
# ══════════════════════════════════════════════════════════════════════════════


def test_ac7_platform_pick_and_clear(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    store = _goto_spa(page, base_url, [_mock_movie(platform=None)])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    page.locator("#modal-edit-section .platform-chip[data-platform='Netflix']").click()
    page.wait_for_timeout(500)
    assert any(p.get("platform") == "Netflix" for p in store["patches"]), (
        "AC-7: platform persists"
    )

    page.locator("#modal-edit-section .platform-chip-clear").click()
    page.wait_for_timeout(500)
    assert any(p.get("platform") is None for p in store["patches"]), (
        "AC-7: platform clear → null"
    )
    _screenshot(page, "ac7-platform")


# ══════════════════════════════════════════════════════════════════════════════
# AC-8 — "Reseña pública" toggle disabled while note empty
# ══════════════════════════════════════════════════════════════════════════════


def test_ac8_public_toggle_disabled_when_note_empty(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_movie(note="", note_public=False)])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    checkbox = page.locator("#modal-edit-section [data-note-public]")
    assert checkbox.is_disabled(), (
        "AC-8: toggle must be disabled while the note is empty"
    )

    # Type text → toggle enables live.
    page.locator("#modal-edit-note").fill("Una reseña con texto")
    page.wait_for_timeout(200)
    assert not checkbox.is_disabled(), "AC-8: typing a note must enable the toggle live"

    # Empty it again → disabled + unchecked.
    page.locator("#modal-edit-note").fill("")
    page.wait_for_timeout(200)
    assert checkbox.is_disabled(), (
        "AC-8: emptying the note must disable the toggle again"
    )
    assert not checkbox.is_checked()
    _screenshot(page, "ac8-public-toggle")


def test_ac8_empty_note_coerces_public_false(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    store = _goto_spa(
        page, base_url, [_mock_movie(note="was public", note_public=True)]
    )
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    page.locator("#modal-edit-note").fill("")
    page.wait_for_timeout(150)
    page.locator("[data-action='edit-note-save']").click()
    page.wait_for_timeout(500)
    assert any(
        p.get("note", None) == "" and p.get("note_public") is False
        for p in store["patches"]
    ), "AC-8: saving an empty note must coerce note_public=false"


# ══════════════════════════════════════════════════════════════════════════════
# AC-10 — delete closes the modal
# ══════════════════════════════════════════════════════════════════════════════


def test_ac10_delete_closes_modal(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    store = _goto_spa(page, base_url, [_mock_movie()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    page.locator("[data-action='edit-delete']").click()
    page.wait_for_timeout(600)

    assert store["deletes"] == [_MOVIE_ID], "AC-10: delete must DELETE /api/movies/{id}"
    assert page.locator("#modal").is_hidden(), (
        "AC-10: the modal must close after delete"
    )
    _screenshot(page, "ac10-delete")


# ══════════════════════════════════════════════════════════════════════════════
# AC-13 — note markup renders inert as text in the modal editor
# ══════════════════════════════════════════════════════════════════════════════


def test_ac13_xss_note_inert_in_modal_editor(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_movie(note=_XSS_NOTE, note_public=False)])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    fired = page.evaluate("() => window.__xss_fired === true")
    assert not fired, "AC-13: XSS payload in the modal note editor must NOT execute"
    real_script = page.evaluate(
        "() => document.querySelectorAll('#modal-edit-section script').length"
    )
    assert real_script == 0, (
        "AC-13: no real <script> element must be created in the modal"
    )
    # The textarea shows the raw markup as literal text (esc() into the template).
    val = page.locator("#modal-edit-note").input_value()
    assert "<script>" in val, "AC-13: the note markup must survive as inert text"
    _screenshot(page, "ac13-xss-inert")


# ══════════════════════════════════════════════════════════════════════════════
# AC-15 — a11y: axe, keyboard, target size, es-ES
# ══════════════════════════════════════════════════════════════════════════════


def test_ac15_modal_edit_section_a11y_desktop(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(
        page,
        base_url,
        [
            _mock_movie(
                media_type="tv", status="viendo", total_seasons=4, note="Con texto"
            )
        ],
    )
    _route_details(page, base_url, _details_payload(total_seasons=4), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    _screenshot(page, "ac15-desktop")
    _inject_axe(page, base_url)
    violations = _run_axe(page, "#modal-edit-section")
    _screenshot(page, "ac15-desktop-axe")
    assert violations == [], (
        f"AC-15: axe found {len(violations)} critical/serious violations in the modal "
        f"edit section (desktop): " + json.dumps(violations, indent=2)
    )


def test_ac15_modal_edit_section_a11y_mobile(page: Page, base_url: str):
    page.set_viewport_size({"width": 375, "height": 667})
    hits = {}
    _goto_spa(
        page,
        base_url,
        [
            _mock_movie(
                media_type="tv", status="viendo", total_seasons=4, note="Con texto"
            )
        ],
    )
    _route_details(page, base_url, _details_payload(total_seasons=4), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#modal-edit-section")
    _screenshot(page, "ac15-mobile-axe")
    assert violations == [], (
        f"AC-15: axe found {len(violations)} critical/serious violations in the modal "
        f"edit section (mobile): " + json.dumps(violations, indent=2)
    )


def test_ac15_keyboard_focus_and_target_size(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_movie(note="Con texto")])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    # Every interactive control has a >= 24px target.
    for sel in [
        ".modal-status-pill",
        "#modal-edit-section .stars .star",
        "#modal-edit-date",
        "[data-action='edit-date-save']",
        "#modal-edit-section .platform-chip",
        "#modal-edit-note",
        "[data-action='edit-note-save']",
        "[data-action='edit-add-to-list']",
        "[data-action='edit-delete']",
    ]:
        box = page.locator(sel).first.bounding_box()
        assert box, f"AC-15: {sel} has no bounding box"
        assert box["height"] >= 24, f"AC-15: {sel} height {box['height']}px < 24px"
        assert box["width"] >= 24, f"AC-15: {sel} width {box['width']}px < 24px"

    # Keyboard-operable with a visible focus ring.
    page.locator(".modal-status-pill").first.focus()
    assert _has_visible_focus(page), (
        "AC-15: no visible focus indicator on the status pills"
    )
    _screenshot(page, "ac15-keyboard-focus")


def test_ac15_es_es_copy(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_movie()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    _open_detail_modal(page)

    text = page.locator("#modal-edit-section").inner_text()
    # "Editar" is uppercased via CSS text-transform → compare case-insensitively.
    assert "editar" in text.lower()
    assert "Estado" in text
    assert "Reseña pública" in text
    assert "Añadir a lista" in text


if __name__ == "__main__":
    pass
