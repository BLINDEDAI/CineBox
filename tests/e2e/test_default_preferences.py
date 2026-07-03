"""Browser E2E tests for default-preferences / Preferencias (AC-1..AC-13).

The prefs helper (readPrefs / getPref / setPref) is pure JS, so it is exercised
in a real browser via page.evaluate() (unittest cannot run JS). The apply-points
(startup home-view routing, default sort, default-platform pre-fill on the
watched transition) are driven through the production seams.

Covers every ### Tester scope row:
  AC-1  — Preferencias section shows 3 selects, each with a leading
           "Por defecto (sin preferencia)" option.
  AC-2  — home_view=discover-view → startup routing lands on Descubrir.
  AC-3  — no/invalid home_view → startup routing lands on Mi colección.
  AC-4  — collection_sort=pending-first → collectionSort initialises to it.
  AC-5  — no collection_sort → collectionSort defaults to "recent".
  AC-6  — default_platform=Netflix → marking a platform-less title watched sends
           platform=Netflix on the PATCH payload (both seams).
  AC-7  — no default_platform → PATCH payload carries no platform.
  AC-8  — a saved preference survives a reload.
  AC-9  — changing the live #collection-sort control does NOT persist.
  AC-10 — resetting one preference to "" clears only that field.
  AC-11 — invalid stored value → getPref falls back, app does not crash.
  AC-12 — axe WCAG 2.2 A/AA + keyboard + labels + focus + >=24px, es-ES.
  AC-13 — renders at desktop + mobile breakpoints.

Cinephora harness invariants (tester-bundle.md § 7):
  - Stub window.supabase BEFORE modules boot (add_init_script).
  - Route the vendor supabase-js bundle to noop (SRI mismatch → stub survives).
  - Set _currentUser directly (incl. .email + user_metadata.desired_username).
  - Stub /api/profile with a username → dodge the blocking #username-gate.
  - page.route is LIFO — register narrow overrides AFTER broad ones.
  - The prefs selects sit in a plain container (no false container ARIA role).
  - Screenshots → handoffs/default-preferences/screenshots/.
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
    / "default-preferences"
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

# A single platform-less title so the default-platform pre-fill applies.
_MOVIES_ONE_PLATFORMLESS = {
    "ok": True,
    "movies": [
        {
            "id": 1,
            "tmdb_id": 101,
            "media_type": "movie",
            "title": "Sin Plataforma",
            "year": 2022,
            "poster_url": None,
            "status": "pendiente",
            "rating": None,
            "note": None,
            "watched_at": None,
            "platform": None,
            "current_season": None,
            "current_episode": None,
            "total_seasons": None,
            "created_at": "2024-01-01T00:00:00+00:00",
        }
    ],
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _route_config(page: Page, base_url: str):
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


def _route_json(page: Page, url_glob: str, payload: dict, status: int = 200):
    def handle(route):
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route(url_glob, handle)


def _route_vendor_supabase(page: Page, base_url: str):
    """Route the vendor supabase-js bundle to a noop (SRI mismatch → stub wins)."""
    noop_js = b"/* stub: supabase vendor noop for e2e tests */"

    def handle(route):
        route.fulfill(status=200, content_type="application/javascript", body=noop_js)

    page.route(f"{base_url}/vendor/supabase-js/**", handle)


def _inject_supabase_stub(page: Page):
    script = """
    (() => {
        window.supabase = {
            createClient: (url, key, opts) => ({
                auth: {
                    signOut: async () => ({ error: null }),
                    getSession: async () => ({ data: { session: null }, error: null }),
                    onAuthStateChange: (cb) => ({
                        data: { subscription: { unsubscribe: () => {} } }
                    }),
                }
            })
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


def _clear_prefs_init(page: Page):
    """Clear cinephora_prefs before every load so tests start from a clean slate."""
    page.add_init_script(
        "try { localStorage.removeItem('cinephora_prefs'); } catch (e) {}"
    )


def _goto_spa(page: Page, base_url: str):
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.evaluate(
        """() => {
            const ws = document.getElementById('welcome-screen');
            if (ws) ws.remove();
        }"""
    )


def _mount_authenticated(page: Page, email: str = "test@example.com"):
    page.evaluate(
        """(emailAddr) => {
            _currentUser = {
                id: 'test-user-id',
                email: emailAddr,
                user_metadata: { desired_username: 'testuser' }
            };
            if (window._supabase) { _supabase = window._supabase; }
        }""",
        email,
    )


def _open_settings_view(page: Page):
    page.evaluate(
        """() => {
            if (typeof showView === 'function') showView('settings-view');
            else if (typeof showSettingsView === 'function') showSettingsView();
        }"""
    )
    page.wait_for_timeout(500)


def _base_routes(page: Page, base_url: str, movies: dict = None):
    """Register the broad routes (config, profile, lists, movies, vendor)."""
    _inject_supabase_stub(page)
    _clear_prefs_init(page)
    _route_config(page, base_url)
    _route_json(page, f"{base_url}/api/profile", _PROFILE_WITH_USERNAME)
    _route_json(page, f"{base_url}/api/lists", _LISTS_EMPTY)
    _route_json(page, f"{base_url}/api/movies", movies or {"ok": True, "movies": []})
    _route_json(
        page,
        f"{base_url}/api/level",
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
        },
    )
    _route_vendor_supabase(page, base_url)


def _screenshot(page: Page, name: str) -> str:
    path = str(_SCREENSHOTS_DIR / f"{name}.png")
    page.screenshot(path=path)
    return path


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
                { runOnly: { type: 'tag', values:
                    ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa'] } }
            ).then(r => r.violations.map(v => ({
                id: v.id, impact: v.impact, description: v.description,
                nodes: v.nodes.length
            })));
        }""",
        context_selector,
    )
    return [v for v in results if v["impact"] in ("critical", "serious")]


# ══════════════════════════════════════════════════════════════════════════════
# Prefs helper — pure JS logic (readPrefs / getPref / setPref)  AC-8/AC-10/AC-11
# ══════════════════════════════════════════════════════════════════════════════


def test_helper_getpref_valid_and_fallback(page: Page, base_url: str):
    """AC-11: getPref returns the stored value when valid; fallback otherwise."""
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    result = page.evaluate(
        """() => {
            setPref('home_view', 'discover-view');
            const valid = getPref('home_view', HOME_VIEWS, 'collection-view');
            // Corrupt/out-of-allow-list value → fallback.
            setPref('home_view', 'evil-view');
            const invalid = getPref('home_view', HOME_VIEWS, 'collection-view');
            // Unset field → fallback.
            setPref('home_view', null);
            const unset = getPref('home_view', HOME_VIEWS, 'collection-view');
            return { valid, invalid, unset };
        }"""
    )
    assert result["valid"] == "discover-view", "AC-11: valid stored value returned"
    assert result["invalid"] == "collection-view", "AC-11: out-of-allow-list → fallback"
    assert result["unset"] == "collection-view", "AC-11: unset → fallback"


def test_helper_readprefs_malformed_and_throwing(page: Page, base_url: str):
    """readPrefs returns {} on malformed JSON and when localStorage access throws."""
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    malformed = page.evaluate(
        """() => {
            localStorage.setItem('cinephora_prefs', '{not valid json');
            const r = readPrefs();
            return { isObject: r && typeof r === 'object', keys: Object.keys(r).length };
        }"""
    )
    assert malformed["isObject"] and malformed["keys"] == 0, (
        "readPrefs must return {} on malformed JSON"
    )

    throwing = page.evaluate(
        """() => {
            const orig = Storage.prototype.getItem;
            Storage.prototype.getItem = () => { throw new Error('privacy mode'); };
            let ok = true;
            try {
                const r = readPrefs();
                ok = r && typeof r === 'object' && Object.keys(r).length === 0;
            } finally {
                Storage.prototype.getItem = orig;
            }
            return ok;
        }"""
    )
    assert throwing, "readPrefs must degrade to {} when localStorage.getItem throws"


def test_helper_setpref_roundtrip_and_single_clear(page: Page, base_url: str):
    """AC-10: setPref round-trips; clearing one field leaves the other two intact."""
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    result = page.evaluate(
        """() => {
            setPref('home_view', 'stats-view');
            setPref('collection_sort', 'pending-first');
            setPref('default_platform', 'Netflix');
            const all = readPrefs();
            // Clear just collection_sort.
            setPref('collection_sort', null);
            const after = readPrefs();
            return { all, after };
        }"""
    )
    assert result["all"] == {
        "home_view": "stats-view",
        "collection_sort": "pending-first",
        "default_platform": "Netflix",
    }, "setPref must round-trip all three fields"
    assert result["after"] == {
        "home_view": "stats-view",
        "default_platform": "Netflix",
    }, "AC-10: clearing collection_sort must leave the other two intact"


def test_helper_setpref_throwing_is_noop(page: Page, base_url: str):
    """setPref is a silent no-op (no throw) when localStorage.setItem throws."""
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    ok = page.evaluate(
        """() => {
            const orig = Storage.prototype.setItem;
            Storage.prototype.setItem = () => { throw new Error('quota'); };
            let threw = false;
            try { setPref('home_view', 'discover-view'); }
            catch (e) { threw = true; }
            finally { Storage.prototype.setItem = orig; }
            return !threw;
        }"""
    )
    assert ok, "setPref must not throw when storage throws"


# ══════════════════════════════════════════════════════════════════════════════
# Preferencias UI  — AC-1, AC-9, AC-10
# ══════════════════════════════════════════════════════════════════════════════


def test_ac1_three_controls_with_default_option(page: Page, base_url: str):
    """AC-1: three labelled selects, each with a leading 'Por defecto' option."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _base_routes(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)
    _open_settings_view(page)
    page.wait_for_selector("#settings-pref-home-view", timeout=5000)
    _screenshot(page, "ac1-prefs-controls")

    for sel_id, label_frag in [
        ("#settings-pref-home-view", "inicio"),
        ("#settings-pref-collection-sort", "orden"),
        ("#settings-pref-default-platform", "plataforma"),
    ]:
        sel = page.locator(sel_id)
        assert sel.count() == 1, f"AC-1: {sel_id} must exist"
        tag = sel.evaluate("el => el.tagName.toLowerCase()")
        assert tag == "select", f"AC-1: {sel_id} must be a <select>; got <{tag}>"
        # Leading "Por defecto (sin preferencia)" option (empty value, first).
        first_opt = sel.evaluate(
            "el => ({ value: el.options[0].value, text: el.options[0].textContent })"
        )
        assert first_opt["value"] == "", f"AC-1: {sel_id} first option value must be ''"
        assert "por defecto" in first_opt["text"].lower(), (
            f"AC-1: {sel_id} first option must be 'Por defecto (sin preferencia)'; "
            f"got {first_opt['text']!r}"
        )
        # Associated <label for>.
        sel_dom_id = sel_id.lstrip("#")
        label = page.locator(f"label[for='{sel_dom_id}']")
        assert label.count() == 1, f"AC-1: {sel_id} must have an associated <label for>"
        assert label_frag in label.inner_text().lower(), (
            f"AC-1: {sel_id} label should reference '{label_frag}'"
        )


def test_ac9_live_sort_does_not_persist(page: Page, base_url: str):
    """AC-9: changing the live #collection-sort control does NOT write a preference."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    persisted = page.evaluate(
        """() => {
            // Simulate the live sort control change (the app.js handler must not setPref).
            const sel = document.getElementById('collection-sort');
            sel.value = 'title-asc';
            sel.dispatchEvent(new Event('change'));
            return readPrefs();
        }"""
    )
    assert "collection_sort" not in persisted, (
        "AC-9: the live sort control must not persist a preference"
    )
    # But the session variable did change.
    session_sort = page.evaluate("() => collectionSort")
    assert session_sort == "title-asc", "AC-9: live change still updates session sort"


def test_ac10_reset_one_via_ui_leaves_others(page: Page, base_url: str):
    """AC-10: choosing 'Por defecto' for one select clears only that field."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _base_routes(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)

    # Pre-seed all three, then open the view so the selects reflect them.
    page.evaluate(
        """() => {
            setPref('home_view', 'discover-view');
            setPref('collection_sort', 'pending-first');
            setPref('default_platform', 'Netflix');
        }"""
    )
    _open_settings_view(page)
    page.wait_for_selector("#settings-pref-home-view", timeout=5000)

    # The selects should reflect the seeded values.
    assert page.locator("#settings-pref-home-view").input_value() == "discover-view"
    assert (
        page.locator("#settings-pref-collection-sort").input_value() == "pending-first"
    )
    assert page.locator("#settings-pref-default-platform").input_value() == "Netflix"

    # Reset only home_view to the "Por defecto" (empty) option.
    page.select_option("#settings-pref-home-view", "")
    page.wait_for_timeout(200)

    after = page.evaluate("() => readPrefs()")
    assert "home_view" not in after, "AC-10: home_view must be cleared"
    assert after.get("collection_sort") == "pending-first", "AC-10: sort intact"
    assert after.get("default_platform") == "Netflix", "AC-10: platform intact"


def test_ac8_saved_preference_survives_reload(page: Page, base_url: str):
    """AC-8: a preference written via the UI is still present after a reload."""
    page.set_viewport_size({"width": 1280, "height": 800})
    # Do NOT clear prefs on reload for this test — we assert persistence.
    _inject_supabase_stub(page)
    _route_config(page, base_url)
    _route_json(page, f"{base_url}/api/profile", _PROFILE_WITH_USERNAME)
    _route_json(page, f"{base_url}/api/lists", _LISTS_EMPTY)
    _route_json(page, f"{base_url}/api/movies", {"ok": True, "movies": []})
    _route_json(
        page,
        f"{base_url}/api/level",
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
        },
    )
    _route_vendor_supabase(page, base_url)

    _goto_spa(page, base_url)
    page.evaluate(
        "() => { localStorage.removeItem('cinephora_prefs'); setPref('collection_sort', 'pending-first'); }"
    )

    # Reload — prefs live in localStorage so they survive.
    page.reload()
    page.wait_for_load_state("networkidle")
    stored = page.evaluate(
        "() => getPref('collection_sort', COLLECTION_SORTS, 'recent')"
    )
    assert stored == "pending-first", "AC-8: saved preference must survive reload"


# ══════════════════════════════════════════════════════════════════════════════
# Startup routing (home view)  — AC-2, AC-3
# ══════════════════════════════════════════════════════════════════════════════


def test_ac2_home_view_routes_to_discover(page: Page, base_url: str):
    """AC-2: home_view=discover-view → startup routing lands on Descubrir."""
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    active = page.evaluate(
        """() => {
            setPref('home_view', 'discover-view');
            // Mirror the initApp §5 apply-point after loadMovies().
            showView(getPref('home_view', HOME_VIEWS, 'collection-view'));
            return document.body.dataset.activeView;
        }"""
    )
    assert active == "discover-view", "AC-2: startup must route to discover-view"


def test_ac3_no_home_view_routes_to_collection(page: Page, base_url: str):
    """AC-3: no/invalid home_view → startup routing lands on Mi colección."""
    _base_routes(page, base_url)
    _goto_spa(page, base_url)

    # None saved.
    none_active = page.evaluate(
        """() => {
            localStorage.removeItem('cinephora_prefs');
            showView(getPref('home_view', HOME_VIEWS, 'collection-view'));
            return document.body.dataset.activeView;
        }"""
    )
    assert none_active == "collection-view", "AC-3: unset → collection-view"

    # Invalid saved (AC-11 fallback via the routing seam).
    invalid_active = page.evaluate(
        """() => {
            setPref('home_view', 'settings-view');  // excluded from HOME_VIEWS
            showView(getPref('home_view', HOME_VIEWS, 'collection-view'));
            return document.body.dataset.activeView;
        }"""
    )
    assert invalid_active == "collection-view", "AC-3/AC-11: invalid → collection-view"


# ══════════════════════════════════════════════════════════════════════════════
# Default sort  — AC-4, AC-5
# ══════════════════════════════════════════════════════════════════════════════


def test_ac4_default_sort_applied_on_startup(page: Page, base_url: str):
    """AC-4: collection_sort=pending-first → collectionSort initialises to it,
    and the #collection-sort control reflects it on startup."""
    # Seed the preference BEFORE the modules load (add_init_script), because
    # collectionSort is initialised at load-time from getPref.
    _inject_supabase_stub(page)
    page.add_init_script(
        "try { localStorage.setItem('cinephora_prefs', JSON.stringify({collection_sort: 'pending-first'})); } catch (e) {}"
    )
    _route_config(page, base_url)
    _route_json(page, f"{base_url}/api/profile", _PROFILE_WITH_USERNAME)
    _route_json(page, f"{base_url}/api/lists", _LISTS_EMPTY)
    _route_json(page, f"{base_url}/api/movies", {"ok": True, "movies": []})
    _route_vendor_supabase(page, base_url)

    _goto_spa(page, base_url)

    session_sort = page.evaluate("() => collectionSort")
    assert session_sort == "pending-first", "AC-4: collectionSort must init from pref"
    control_value = page.locator("#collection-sort").input_value()
    assert control_value == "pending-first", (
        "AC-4: #collection-sort control must reflect the applied default"
    )


def test_ac5_no_sort_defaults_to_recent(page: Page, base_url: str):
    """AC-5: no collection_sort → collectionSort defaults to 'recent'."""
    _inject_supabase_stub(page)
    _clear_prefs_init(page)
    _route_config(page, base_url)
    _route_json(page, f"{base_url}/api/profile", _PROFILE_WITH_USERNAME)
    _route_json(page, f"{base_url}/api/lists", _LISTS_EMPTY)
    _route_json(page, f"{base_url}/api/movies", {"ok": True, "movies": []})
    _route_vendor_supabase(page, base_url)
    _goto_spa(page, base_url)

    session_sort = page.evaluate("() => collectionSort")
    assert session_sort == "recent", "AC-5: no pref → default 'recent'"
    assert page.locator("#collection-sort").input_value() == "recent"


# ══════════════════════════════════════════════════════════════════════════════
# Default platform on watched transition  — AC-6, AC-7  (both seams)
# ══════════════════════════════════════════════════════════════════════════════


def _capture_patch(page: Page, base_url: str):
    """Route PATCH /api/movies/{id}, capturing the request body into a JS global."""

    def handle(route):
        req = route.request
        body = req.post_data or "{}"
        # Stash the captured payload on window for the test to read.
        page.evaluate("(b) => { window.__lastPatch = b; }", body)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True}),
        )

    page.route(f"{base_url}/api/movies/*", handle)


def test_ac6_platform_prefill_status_select_seam(page: Page, base_url: str):
    """AC-6: default_platform=Netflix → marking a platform-less title watched via
    the status-select seam sends platform=Netflix on the PATCH payload."""
    _base_routes(page, base_url, movies=_MOVIES_ONE_PLATFORMLESS)
    _capture_patch(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)

    page.evaluate("() => { setPref('default_platform', 'Netflix'); }")
    # Load the collection then flip the status select to 'vista'.
    page.evaluate("() => loadMovies()")
    page.wait_for_selector(".card[data-id='1']", timeout=5000)
    page.evaluate("() => openEditOnly(1)")
    page.wait_for_selector(".modal-status-pill", timeout=5000)
    _screenshot(page, "ac6-before-watched")

    page.locator(".modal-status-pill[data-status='vista']").click()
    page.wait_for_timeout(400)

    body = page.evaluate("() => window.__lastPatch")
    payload = json.loads(body)
    assert payload.get("status") == "vista", "AC-6: status must be vista"
    assert payload.get("platform") == "Netflix", (
        f"AC-6: platform pre-fill must send Netflix; got {payload!r}"
    )


def test_ac7_no_platform_pref_no_prefill(page: Page, base_url: str):
    """AC-7: no default_platform → marking watched sends no platform."""
    _base_routes(page, base_url, movies=_MOVIES_ONE_PLATFORMLESS)
    _capture_patch(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)

    page.evaluate("() => { localStorage.removeItem('cinephora_prefs'); }")
    page.evaluate("() => loadMovies()")
    page.wait_for_selector(".card[data-id='1']", timeout=5000)
    page.evaluate("() => openEditOnly(1)")
    page.wait_for_selector(".modal-status-pill", timeout=5000)

    page.locator(".modal-status-pill[data-status='vista']").click()
    page.wait_for_timeout(400)

    payload = json.loads(page.evaluate("() => window.__lastPatch"))
    assert payload.get("status") == "vista", "AC-7: status must be vista"
    assert "platform" not in payload, (
        f"AC-7: no preference → no platform key; got {payload!r}"
    )


def test_ac6_platform_prefill_pickpanel_seam(page: Page, base_url: str):
    """AC-6: the pick-panel 'watched' seam also pre-fills the default platform."""
    _base_routes(page, base_url, movies=_MOVIES_ONE_PLATFORMLESS)
    _capture_patch(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)

    page.evaluate("() => { setPref('default_platform', 'HBO Max'); }")
    page.evaluate("() => loadMovies()")
    page.wait_for_timeout(200)

    # Open the pick panel on the platform-less pending title, then click "watched".
    page.evaluate(
        """() => {
            const movie = movies.find(m => m.id === 1);
            renderPickPanel(movie);
        }"""
    )
    page.wait_for_selector("[data-pick-action='watched']", timeout=5000)
    page.click("[data-pick-action='watched']")
    page.wait_for_timeout(400)

    payload = json.loads(page.evaluate("() => window.__lastPatch"))
    assert payload.get("status") == "vista", "AC-6: pick-panel status must be vista"
    assert payload.get("platform") == "HBO Max", (
        f"AC-6: pick-panel must pre-fill platform; got {payload!r}"
    )


def test_platform_prefill_respects_existing_platform(page: Page, base_url: str):
    """Edge case: a title that already has a platform is left untouched."""
    movies = {
        "ok": True,
        "movies": [{**_MOVIES_ONE_PLATFORMLESS["movies"][0], "platform": "Cine"}],
    }
    _base_routes(page, base_url, movies=movies)
    _capture_patch(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)

    page.evaluate("() => { setPref('default_platform', 'Netflix'); }")
    page.evaluate("() => loadMovies()")
    page.wait_for_selector(".card[data-id='1']", timeout=5000)
    page.evaluate("() => openEditOnly(1)")
    page.wait_for_selector(".modal-status-pill", timeout=5000)
    page.locator(".modal-status-pill[data-status='vista']").click()
    page.wait_for_timeout(400)

    payload = json.loads(page.evaluate("() => window.__lastPatch"))
    assert "platform" not in payload, (
        f"Existing platform must not be overwritten by the default; got {payload!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# AC-11 — app does not crash on invalid stored value
# ══════════════════════════════════════════════════════════════════════════════


def test_ac11_invalid_value_no_crash(page: Page, base_url: str):
    """AC-11: an invalid stored value is ignored, the default applies, no crash."""
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    _inject_supabase_stub(page)
    page.add_init_script(
        "try { localStorage.setItem('cinephora_prefs', JSON.stringify({"
        "home_view: 'nope-view', collection_sort: 'bogus', default_platform: 'Betamax'"
        "})); } catch (e) {}"
    )
    _route_config(page, base_url)
    _route_json(page, f"{base_url}/api/profile", _PROFILE_WITH_USERNAME)
    _route_json(page, f"{base_url}/api/lists", _LISTS_EMPTY)
    _route_json(page, f"{base_url}/api/movies", {"ok": True, "movies": []})
    _route_vendor_supabase(page, base_url)
    _goto_spa(page, base_url)

    assert errors == [], f"AC-11: app must not throw on invalid prefs; got {errors!r}"
    # All three fall back to defaults.
    result = page.evaluate(
        """() => ({
            sort: collectionSort,
            home: getPref('home_view', HOME_VIEWS, 'collection-view'),
            platform: getPref('default_platform', PLATFORMS, null),
        })"""
    )
    assert result["sort"] == "recent", "AC-11: bogus sort → recent"
    assert result["home"] == "collection-view", "AC-11: bogus home → collection-view"
    assert result["platform"] is None, "AC-11: bogus platform → null"


# ══════════════════════════════════════════════════════════════════════════════
# AC-12 / AC-13 — a11y (axe, keyboard, labels, focus, target size) + breakpoints
# ══════════════════════════════════════════════════════════════════════════════


def test_ac12_axe_desktop(page: Page, base_url: str):
    """AC-12: Preferencias passes axe WCAG 2.2 A/AA at 1280 px."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _base_routes(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)
    _open_settings_view(page)
    page.wait_for_selector("#settings-pref-home-view", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac12-axe-desktop")
    assert violations == [], (
        f"AC-12: axe found {len(violations)} critical/serious violation(s): "
        + json.dumps(violations, indent=2)
    )


def test_ac13_axe_mobile(page: Page, base_url: str):
    """AC-12/AC-13: Preferencias passes axe at 375 px and renders (no overflow)."""
    page.set_viewport_size({"width": 375, "height": 667})
    _base_routes(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)
    _open_settings_view(page)
    page.wait_for_selector("#settings-pref-home-view", timeout=5000)

    _inject_axe(page, base_url)
    violations = _run_axe(page, "#settings-view")
    _screenshot(page, "ac13-mobile")
    assert violations == [], (
        "AC-13: axe (mobile 375px) found violations: "
        + json.dumps(violations, indent=2)
    )

    # No horizontal overflow of the prefs selects at mobile width.
    overflow = page.evaluate(
        """() => {
            const el = document.getElementById('settings-pref-home-view');
            const r = el.getBoundingClientRect();
            return { right: r.right, vw: window.innerWidth };
        }"""
    )
    assert overflow["right"] <= overflow["vw"] + 1, (
        f"AC-13: prefs select overflows viewport at mobile; {overflow!r}"
    )


def test_ac12_keyboard_labels_and_target_size(page: Page, base_url: str):
    """AC-12: selects are keyboard-focusable with visible focus, labelled, >=24px."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _base_routes(page, base_url)
    _goto_spa(page, base_url)
    _mount_authenticated(page)
    _open_settings_view(page)
    page.wait_for_selector("#settings-pref-home-view", timeout=5000)

    for sel_id in (
        "#settings-pref-home-view",
        "#settings-pref-collection-sort",
        "#settings-pref-default-platform",
    ):
        sel = page.locator(sel_id)
        sel.focus()
        focused = page.evaluate("() => document.activeElement.id")
        assert focused == sel_id.lstrip("#"), f"AC-12: {sel_id} must be focusable"

        outline = page.evaluate(
            "() => window.getComputedStyle(document.activeElement).outlineWidth"
        )
        box_shadow = page.evaluate(
            "() => window.getComputedStyle(document.activeElement).boxShadow"
        )
        assert outline not in ("0px", "") or box_shadow not in ("none", ""), (
            f"AC-12: {sel_id} must show a visible focus indicator"
        )

        box = sel.bounding_box()
        assert box["height"] >= 24, f"AC-12: {sel_id} height {box['height']}px < 24px"
        assert box["width"] >= 24, f"AC-12: {sel_id} width {box['width']}px < 24px"

    _screenshot(page, "ac12-keyboard-focus")
