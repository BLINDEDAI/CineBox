"""Browser E2E tests for series-episode-progress (Collection).

Covers the browser-driven ### Tester scope rows of the task DoD:

  AC-1/AC-2  — series modal shows the season selector + episode list (still,
               number+title, air date, runtime, synopsis); switching season
               re-renders the list.
  AC-3/AC-4  — marking/unmarking a single episode persists (survives reload,
               simulated via the season route returning the updated `watched`).
  AC-5       — whole-season mark marks every episode; whole-season unmark
               clears them.
  AC-6/AC-9  — progress shows the legacy `S · E` label at zero marks, switches
               to `N/M episodios` after the first mark; unknown total stays
               `S · E`.
  AC-8       — a movie's modal shows no season/episode tracker (and never
               calls the season endpoint).
  AC-11      — an episode with HTML-significant title/synopsis renders
               escaped as text.
  AC-14      — the modal edit section has no manual season/episode number
               inputs (the removed "Progreso T/E" block).
  AC-15      — axe WCAG 2.2 A/AA (0 critical/serious) desktop + 375px mobile;
               toggles + selector keyboard-operable, >= 24px, es-ES.

Strategy mirrors tests/e2e/test_modal_edit_section.py exactly:
  - Real CineBox server via conftest.py base_url fixture (no DB/auth).
  - The authenticated SPA is driven via the production seam: mount
    `_currentUser`, stub `/api/config` + `/api/movies` + `/api/level`, call
    `loadMovies()`. The modal is opened by clicking the collection card poster.
  - GET /api/tv/{tmdb_id}/season/{n} and POST /api/movies/{id}/episodes are
    stubbed by ONE stateful handler per URL pattern, mutating an in-memory
    per-season mark store so a "reload" (re-opening the modal / re-fetching
    the season) reflects prior marks — the stand-in for real persistence.
  - axe-core injected via the vendored tests/e2e/axe.min.js (same-origin,
    CSP: script-src 'self').
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
    / "series-episode-progress"
    / "screenshots"
)
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_SUPABASE_URL = "https://demoproject.supabase.co"
_SERIES_ID = 42
_TMDB_ID = 95396
_MOVIE_ID = 43
_MOVIE_TMDB_ID = 550

_XSS_TITLE = '<img src=x onerror="window.__ep_xss_fired = true">Ep <script>window.__ep_xss_fired = true</script>'


# ── Shared helpers (mirror test_modal_edit_section.py) ───────────────────────


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
    outline = page.evaluate("() => window.getComputedStyle(document.activeElement).outlineWidth")
    box_shadow = page.evaluate("() => window.getComputedStyle(document.activeElement).boxShadow")
    return outline not in ("0px", "") or box_shadow not in ("none", "")


def _route_config(page: Page, base_url: str):
    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"supabase_url": _SUPABASE_URL, "supabase_anon_key": "demo-anon-key"}),
        )

    page.route(f"{base_url}/api/config", handle)


def _route_level(page: Page, base_url: str):
    def handle(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": True, "points": 0, "level": 1, "name": "Espectador",
                "current_min": 0, "next_min": 50, "next_name": "Aficionado",
                "points_into_level": 0, "points_to_next": 50, "progress_pct": 0,
            }),
        )

    page.route(f"{base_url}/api/level", handle)


def _mock_series(**overrides):
    row = {
        "id": _SERIES_ID,
        "tmdb_id": _TMDB_ID,
        "media_type": "tv",
        "title": "Severance",
        "year": "2022",
        "status": "viendo",
        "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        "rating": None,
        "note": "",
        "note_public": False,
        "watched_at": None,
        "platform": None,
        "current_season": None,
        "current_episode": None,
        "total_seasons": 2,
    }
    row.update(overrides)
    return row


def _mock_movie(**overrides):
    row = {
        "id": _MOVIE_ID,
        "tmdb_id": _MOVIE_TMDB_ID,
        "media_type": "movie",
        "title": "Fight Club",
        "year": "1999",
        "status": "vista",
        "poster_url": "https://image.tmdb.org/t/p/w342/y.jpg",
        "rating": None,
        "note": "",
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
    store = {"movies": [dict(m) for m in initial], "patches": [], "deletes": []}

    def handle_collection(route):
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "movies": store["movies"]}),
        )

    page.route(f"{base_url}/api/movies", handle_collection)
    return store


def _episode(number, name="Episode", air_date="2026-01-01", runtime=45,
             overview="Synopsis.", still_path="/still.jpg", watched=False):
    return {
        "episode_number": number, "name": name, "air_date": air_date,
        "runtime": runtime, "overview": overview, "still_path": still_path,
        "watched": watched,
    }


def _route_season_stateful(page: Page, base_url: str, tmdb_id: int, seasons: dict) -> dict:
    """GET /api/tv/{tmdb_id}/season/{n} returns `seasons[n]`, merging live marks
    from `marks` (a set of (season, episode) tuples mutated by the mark route),
    so a re-fetch (simulated reload) reflects prior marks."""
    marks = set()
    hits = {"season": 0}

    def handle(route):
        m = re.search(r"/season/(\d+)", route.request.url)
        n = int(m.group(1))
        hits["season"] += 1
        base = seasons.get(n, {"season_number": n, "name": f"Temporada {n}", "episodes": []})
        episodes = [
            dict(ep, watched=(n, ep["episode_number"]) in marks) for ep in base["episodes"]
        ]
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "season": {
                "season_number": base["season_number"], "name": base["name"], "episodes": episodes,
            }}),
        )

    page.route(re.compile(re.escape(f"{base_url}/api/tv/{tmdb_id}/season/") + r"\d+$"), handle)
    return {"marks": marks, "hits": hits}


def _route_episodes_mark(page: Page, base_url: str, movie_id: int, season_state: dict) -> dict:
    """POST /api/movies/{id}/episodes mutates `season_state["marks"]` and
    returns the authoritative {current_season, current_episode, watched_count}."""
    posts = {"bodies": []}

    def handle(route):
        body = json.loads(route.request.post_data or "{}")
        posts["bodies"].append(body)
        season = body["season"]
        nums = body.get("episodes") or ([body["episode"]] if body.get("episode") is not None else None)
        marks = season_state["marks"]
        if body["watched"]:
            for n in nums or []:
                marks.add((season, n))
        else:
            if nums:
                for n in nums:
                    marks.discard((season, n))
            else:
                for pair in list(marks):
                    if pair[0] == season:
                        marks.discard(pair)
        top = max(marks) if marks else (None, None)
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({
                "ok": True,
                "current_season": top[0], "current_episode": top[1],
                "watched_count": len(marks),
            }),
        )

    page.route(f"{base_url}/api/movies/{movie_id}/episodes", handle)
    return posts


def _details_payload(**overrides):
    d = {
        "overview": "An office worker forms an underground club.",
        "genres": ["Drama"], "genre_ids": [18], "runtime": 45,
        "title": "Severance", "poster_path": "/p.jpg", "backdrop_path": "/bd.jpg",
        "vote_average": 8.4, "trailer": "", "dir_label": "Creación",
        "directors": ["Dan Erickson"], "cast": [], "providers": [], "providers_link": "",
        "total_seasons": 2, "total_episodes": 18,
        "seasons": [
            {"season_number": 1, "name": "Temporada 1", "episode_count": 9},
            {"season_number": 2, "name": "Temporada 2", "episode_count": 9},
        ],
        "watched_count": 0,
    }
    d.update(overrides)
    return d


def _route_details(page: Page, base_url: str, details: dict, hits: dict):
    def handle(route):
        hits["details"] = hits.get("details", 0) + 1
        route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "details": details}),
        )

    page.route(re.compile(re.escape(f"{base_url}/api/details") + r"\?.*"), handle)


def _route_similar(page: Page, base_url: str, hits: dict):
    def handle(route):
        hits["similar"] = hits.get("similar", 0) + 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "results": []}))

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
    page.locator(".card .poster[data-tmdb]").first.click()
    page.wait_for_selector("#modal-edit-section", timeout=4000)
    page.wait_for_timeout(300)


# ══════════════════════════════════════════════════════════════════════════════
# AC-1 / AC-2 — season selector + episode list; switching season re-renders
# ══════════════════════════════════════════════════════════════════════════════


def test_ac1_season_selector_and_episode_list_render(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {
        1: {"season_number": 1, "name": "Temporada 1", "episodes": [
            _episode(1, "Good News About Hell"), _episode(2, "Half Loop"),
        ]},
        2: {"season_number": 2, "name": "Temporada 2", "episodes": [
            _episode(1, "Hello, Ms. Cobel"),
        ]},
    }
    _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, {"marks": set()})
    _open_detail_modal(page)

    assert page.locator(".modal-ep-season-pill").count() >= 1, "AC-1: season selector (pills) must render"
    rows = page.locator("[data-ep-row]")
    assert rows.count() == 2, "AC-1: season 1's two episodes must render"
    assert page.locator(".modal-ep-still, .modal-ep-still-fallback").count() >= 2
    assert "Good News About Hell" in page.locator("[data-ep-row]").nth(0).inner_text()
    _screenshot(page, "ac1-season-episode-list")


def test_ac2_switching_season_rerenders_episode_list(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {
        1: {"season_number": 1, "name": "Temporada 1", "episodes": [_episode(1, "S1E1"), _episode(2, "S1E2")]},
        2: {"season_number": 2, "name": "Temporada 2", "episodes": [_episode(1, "S2E1")]},
    }
    _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, {"marks": set()})
    _open_detail_modal(page)

    assert page.locator("[data-ep-row]").count() == 2

    page.locator(".modal-ep-season-pill[data-season='2']").click()
    page.wait_for_timeout(400)

    rows = page.locator("[data-ep-row]")
    assert rows.count() == 1, "AC-2: switching season must re-render that season's episodes"
    assert "S2E1" in rows.nth(0).inner_text()
    _screenshot(page, "ac2-season-switch")


# ══════════════════════════════════════════════════════════════════════════════
# AC-3 / AC-4 — mark / unmark a single episode, survives reload
# ══════════════════════════════════════════════════════════════════════════════


def test_ac3_mark_episode_persists_and_survives_reload(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1", "episodes": [_episode(1, "S1E1")]}}
    season_state = _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    posts = _route_episodes_mark(page, base_url, _SERIES_ID, season_state)
    _open_detail_modal(page)

    toggle = page.locator("[data-action='ep-toggle']").first
    assert toggle.get_attribute("aria-pressed") == "false"
    toggle.click()
    page.wait_for_timeout(400)

    assert toggle.get_attribute("aria-pressed") == "true", "AC-3: toggle reflects watched in place"
    assert any(b.get("watched") is True for b in posts["bodies"]), "AC-3: POST must mark watched=true"
    assert (1, 1) in season_state["marks"], "AC-3: server-side mark store updated"

    # Simulate "reload": close (Escape) and re-open the modal (re-fetches
    # /api/details + the season), the closest in-browser stand-in for a page
    # reload without tearing down the routed stubs.
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    _open_detail_modal(page)
    reloaded_toggle = page.locator("[data-action='ep-toggle']").first
    assert reloaded_toggle.get_attribute("aria-pressed") == "true", (
        "AC-3: mark must survive a reload (re-fetch of the season)"
    )
    _screenshot(page, "ac3-mark-survives-reload")


def test_ac4_unmark_episode_persists_and_survives_reload(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1", "episodes": [_episode(1, "S1E1")]}}
    season_state = _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    season_state["marks"].add((1, 1))  # pre-seeded as already watched
    posts = _route_episodes_mark(page, base_url, _SERIES_ID, season_state)
    _open_detail_modal(page)

    toggle = page.locator("[data-action='ep-toggle']").first
    assert toggle.get_attribute("aria-pressed") == "true", "pre-seeded as watched"
    toggle.click()
    page.wait_for_timeout(400)

    assert toggle.get_attribute("aria-pressed") == "false", "AC-4: unmark reflects in place"
    assert any(b.get("watched") is False for b in posts["bodies"])
    assert (1, 1) not in season_state["marks"]

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    _open_detail_modal(page)
    reloaded_toggle = page.locator("[data-action='ep-toggle']").first
    assert reloaded_toggle.get_attribute("aria-pressed") == "false", (
        "AC-4: unmark must survive a reload"
    )
    _screenshot(page, "ac4-unmark-survives-reload")


# ══════════════════════════════════════════════════════════════════════════════
# AC-5 — whole-season mark / unmark
# ══════════════════════════════════════════════════════════════════════════════


def test_ac5_whole_season_mark_and_unmark(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1",
                   "episodes": [_episode(1, "E1"), _episode(2, "E2"), _episode(3, "E3")]}}
    season_state = _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, season_state)
    _open_detail_modal(page)

    page.locator("[data-action='ep-season-mark']").click()
    page.wait_for_timeout(400)

    toggles = page.locator("[data-action='ep-toggle']")
    for i in range(toggles.count()):
        assert toggles.nth(i).get_attribute("aria-pressed") == "true"
    assert season_state["marks"] == {(1, 1), (1, 2), (1, 3)}, "AC-5: whole-season mark marks every episode"

    page.locator("[data-action='ep-season-unmark']").click()
    page.wait_for_timeout(400)

    toggles = page.locator("[data-action='ep-toggle']")
    for i in range(toggles.count()):
        assert toggles.nth(i).get_attribute("aria-pressed") == "false"
    assert season_state["marks"] == set(), "AC-5: whole-season unmark clears all marks"
    _screenshot(page, "ac5-whole-season")


# ══════════════════════════════════════════════════════════════════════════════
# AC-6 / AC-9 — progress metric: legacy S·E -> N/M after first mark; unknown total
# ══════════════════════════════════════════════════════════════════════════════


def test_ac6_progress_legacy_label_switches_to_n_of_m_after_first_mark(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series(current_season=1, current_episode=2)])
    _route_details(page, base_url, _details_payload(watched_count=0), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1", "episodes": [_episode(1, "E1")]}}
    season_state = _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, season_state)
    _open_detail_modal(page)

    progress = page.locator("[data-ep-progress]")
    assert progress.inner_text().strip() == "S1 · E2", "AC-9: zero marks -> legacy S·E label, never 0/M"

    page.locator("[data-action='ep-toggle']").first.click()
    page.wait_for_timeout(400)

    assert progress.inner_text().strip() == "1/18 episodios", (
        "AC-6: after the first mark, progress reads N/M (watched_count/total_episodes)"
    )
    _screenshot(page, "ac6-progress-n-of-m")


def test_ac6_unknown_total_stays_legacy_label_even_after_mark(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series(current_season=1, current_episode=1)])
    _route_details(page, base_url, _details_payload(total_episodes=None, watched_count=0), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1", "episodes": [_episode(1, "E1")]}}
    season_state = _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, season_state)
    _open_detail_modal(page)

    page.locator("[data-action='ep-toggle']").first.click()
    page.wait_for_timeout(400)

    progress = page.locator("[data-ep-progress]")
    assert progress.inner_text().strip() == "S1 · E1", (
        "AC-6: unknown total_episodes must keep the S·E label regardless of marks"
    )


# ══════════════════════════════════════════════════════════════════════════════
# AC-8 — a movie shows no tracker, no season endpoint call
# ══════════════════════════════════════════════════════════════════════════════


def test_ac8_movie_modal_has_no_tracker_and_never_calls_season_endpoint(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_movie()])
    _route_details(page, base_url, _details_payload(
        title="Fight Club", total_seasons=None, total_episodes=None, seasons=None, watched_count=0), hits)
    _route_similar(page, base_url, hits)
    season_hits = {"season": 0}

    def _fail_if_called(route):
        season_hits["season"] += 1
        route.fulfill(status=500, body="must not be called")

    page.route(re.compile(r"/api/tv/\d+/season/\d+$"), _fail_if_called)
    _open_detail_modal(page)

    assert page.locator(".modal-ep-season-pill").count() == 0, "AC-8: no season selector for a movie"
    assert page.locator("[data-ep-list]").count() == 0, "AC-8: no episode list for a movie"
    assert page.locator("[data-action='ep-toggle']").count() == 0
    assert season_hits["season"] == 0, "AC-8: a movie modal must never call the season endpoint"
    _screenshot(page, "ac8-movie-no-tracker")


# ══════════════════════════════════════════════════════════════════════════════
# AC-11 — HTML-significant episode title/synopsis rendered escaped
# ══════════════════════════════════════════════════════════════════════════════


def test_ac11_html_significant_episode_text_renders_escaped(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1", "episodes": [
        _episode(1, name=_XSS_TITLE, overview=_XSS_TITLE),
    ]}}
    _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, {"marks": set()})
    _open_detail_modal(page)

    fired = page.evaluate("() => window.__ep_xss_fired === true")
    assert not fired, "AC-11: the XSS payload in episode title/overview must NOT execute"
    real_script = page.evaluate("() => document.querySelectorAll('[data-ep-list] script').length")
    assert real_script == 0, "AC-11: no real <script> element must be created from episode text"
    row_html = page.locator("[data-ep-row]").first.inner_html()
    assert "&lt;script&gt;" in row_html, "AC-11: episode text must be HTML-escaped in the markup"
    _screenshot(page, "ac11-xss-escaped")


# ══════════════════════════════════════════════════════════════════════════════
# AC-14 — no manual season/episode number inputs in the edit section
# ══════════════════════════════════════════════════════════════════════════════


def test_ac14_no_manual_season_episode_inputs(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _goto_spa(page, base_url, [_mock_series()])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1", "episodes": [_episode(1, "E1")]}}
    _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, {"marks": set()})
    _open_detail_modal(page)

    assert page.locator("#modal-edit-section .progress-form").count() == 0, (
        "AC-14: the manual T/E progress-form must be gone"
    )
    assert page.locator("#modal-edit-section [data-field='season']").count() == 0
    assert page.locator("#modal-edit-section [data-field='episode']").count() == 0
    assert page.locator("[data-action='edit-progress-save']").count() == 0
    # The season <select> is the tracker's own control, not a free-typed number input.
    assert page.locator("#modal-edit-section input[type='number']").count() == 0
    _screenshot(page, "ac14-no-manual-inputs")


# ══════════════════════════════════════════════════════════════════════════════
# AC-15 — a11y: axe, keyboard, target size, es-ES
# ══════════════════════════════════════════════════════════════════════════════


def _open_series_with_tracker(page, base_url, hits):
    _goto_spa(page, base_url, [_mock_series(current_season=1, current_episode=2)])
    _route_details(page, base_url, _details_payload(), hits)
    _route_similar(page, base_url, hits)
    seasons = {1: {"season_number": 1, "name": "Temporada 1",
                   "episodes": [_episode(1, "E1"), _episode(2, "E2")]}}
    _route_season_stateful(page, base_url, _TMDB_ID, seasons)
    _route_episodes_mark(page, base_url, _SERIES_ID, {"marks": set()})
    _open_detail_modal(page)


def test_ac15_tracker_a11y_desktop(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _open_series_with_tracker(page, base_url, hits)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#modal-edit-section")
    _screenshot(page, "ac15-desktop-axe")
    assert violations == [], (
        f"AC-15: axe found {len(violations)} critical/serious violations (desktop tracker): "
        + json.dumps(violations, indent=2)
    )


def test_ac15_tracker_a11y_mobile(page: Page, base_url: str):
    page.set_viewport_size({"width": 375, "height": 667})
    hits = {}
    _open_series_with_tracker(page, base_url, hits)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#modal-episodes-section")
    _screenshot(page, "ac15-mobile-axe")
    assert violations == [], (
        f"AC-15: axe found {len(violations)} critical/serious violations (mobile tracker): "
        + json.dumps(violations, indent=2)
    )


def test_ac15_keyboard_focus_and_target_size(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _open_series_with_tracker(page, base_url, hits)

    for sel in [
        ".modal-ep-season-pill",
        "[data-action='ep-toggle']",
        "[data-action='ep-season-mark']",
        "[data-action='ep-season-unmark']",
    ]:
        box = page.locator(sel).first.bounding_box()
        assert box, f"AC-15: {sel} has no bounding box"
        assert box["height"] >= 24, f"AC-15: {sel} height {box['height']}px < 24px"
        assert box["width"] >= 24, f"AC-15: {sel} width {box['width']}px < 24px"

    page.locator(".modal-ep-season-pill").first.focus()
    assert _has_visible_focus(page), "AC-15: no visible focus indicator on the season selector"
    page.locator("[data-action='ep-toggle']").first.focus()
    assert _has_visible_focus(page), "AC-15: no visible focus indicator on an episode toggle"
    _screenshot(page, "ac15-keyboard-focus")


def test_ac15_es_es_copy(page: Page, base_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    hits = {}
    _open_series_with_tracker(page, base_url, hits)

    text = page.locator("#modal-episodes-section").inner_text()
    assert "episodios" in text.lower()
    assert "Temporada" in text
    assert "Marcar temporada" in text
    assert "Desmarcar temporada" in text


if __name__ == "__main__":
    pass
