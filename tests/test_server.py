"""Unit + integration-style tests for the series-season-progress feature.

Covers the backend slice of the feature (spec: series-season-progress-specs.md):
  - do_POST: stores total_seasons for tv, forces null for movie, validates input.
  - do_PATCH: accepts total_seasons in the field loop (backfill write path).
  - _details: exposes total_seasons from TMDB number_of_seasons for tv, null for movie,
    and degrades cleanly with no key / no number_of_seasons.

The DB boundary is stubbed with a FakeCursor that captures the executed SQL +
params, so the integration-style AC-1 test asserts that total_seasons actually
reaches the parameterised INSERT. No live Postgres / TMDB / server is contacted.
"""

import os
import unittest
from unittest import mock

import server
from tests._harness import FakeCursor, make_handler, patch_db


def _post_body(**over):
    body = {"title": "Test Show", "media_type": "tv", "tmdb_id": 42}
    body.update(over)
    return body


def _insert_call(cursor):
    """Return the (sql, params) of the INSERT call recorded by the cursor."""
    for sql, params in cursor.calls:
        if sql.startswith("INSERT INTO movies"):
            return sql, params
    raise AssertionError("no INSERT INTO movies call recorded")


def _insert_total_seasons(cursor):
    """Extract the value bound to the total_seasons column in the INSERT."""
    sql, params = _insert_call(cursor)
    # Column list order in the INSERT mirrors the params tuple order.
    cols = sql.split("(", 1)[1].split(")", 1)[0]
    col_names = [c.strip() for c in cols.replace("\n", " ").split(",")]
    idx = col_names.index("total_seasons")
    return params[idx]


# ── do_POST ──────────────────────────────────────────────────────────────────


class DoPostTotalSeasons(unittest.TestCase):
    def _run_post(self, body, *, dup=False):
        # fetchone() #1 = duplicate check (None = not dup), #2 = RETURNING id
        fetch = [None if not dup else {"x": 1}, {"id": 7}]
        cur = FakeCursor(fetch_results=fetch)
        h, responses = make_handler(body=body)
        h.path = "/api/movies"
        with patch_db(cur):
            h.do_POST()
        return cur, responses

    def test_stores_total_seasons_for_tv_when_provided(self):
        """AC-1: a tv item with a body total_seasons stores it in the INSERT."""
        cur, responses = self._run_post(_post_body(total_seasons=5))
        self.assertEqual(responses[-1][0], 201)
        self.assertEqual(_insert_total_seasons(cur), 5)

    def test_forces_null_total_for_movie_body(self):
        """AC-2/BR-1: a movie body carrying total_seasons stores null."""
        cur, responses = self._run_post(_post_body(media_type="movie", total_seasons=5))
        self.assertEqual(responses[-1][0], 201)
        self.assertIsNone(_insert_total_seasons(cur))

    def test_accepts_null_total_seasons(self):
        """US-040: absent/null total_seasons is accepted and stored null (tv)."""
        cur, responses = self._run_post(_post_body(total_seasons=None))
        self.assertEqual(responses[-1][0], 201)
        self.assertIsNone(_insert_total_seasons(cur))

    def test_absent_total_seasons_key_accepted(self):
        """AC-2: tv body with no total_seasons key → stored null, add succeeds."""
        cur, responses = self._run_post(_post_body())
        self.assertEqual(responses[-1][0], 201)
        self.assertIsNone(_insert_total_seasons(cur))

    def test_rejects_zero_total_seasons(self):
        """US-040: total_seasons = 0 (non-positive) → 400, no INSERT."""
        cur, responses = self._run_post(_post_body(total_seasons=0))
        self.assertEqual(responses[-1][0], 400)
        self.assertEqual([c for c in cur.calls if c[0].startswith("INSERT")], [])

    def test_rejects_negative_total_seasons(self):
        cur, responses = self._run_post(_post_body(total_seasons=-3))
        self.assertEqual(responses[-1][0], 400)

    def test_rejects_non_integer_total_seasons(self):
        """US-040: non-int (string / float / bool-as-noise) → 400."""
        for bad in ("3", 2.5, [1]):
            cur, responses = self._run_post(_post_body(total_seasons=bad))
            self.assertEqual(responses[-1][0], 400, f"value {bad!r} should be 400")


# ── do_PATCH ─────────────────────────────────────────────────────────────────


class DoPatchTotalSeasons(unittest.TestCase):
    def _run_patch(self, body, *, rowcount=1):
        cur = FakeCursor(rowcount=rowcount)
        h, responses = make_handler(body=body)
        h.path = "/api/movies/7"
        with patch_db(cur):
            h.do_PATCH()
        return cur, responses

    def _update_call(self, cur):
        for sql, params in cur.calls:
            if sql.startswith("UPDATE movies"):
                return sql, params
        raise AssertionError("no UPDATE recorded")

    def test_updates_total_seasons_backfill_write(self):
        """AC-7: PATCH with total_seasons writes it via the parameterised UPDATE."""
        cur, responses = self._run_patch({"total_seasons": 4})
        self.assertEqual(responses[-1][0], 200)
        sql, params = self._update_call(cur)
        self.assertIn("total_seasons = %s", sql)
        self.assertIn(4, params)

    def test_accepts_null_total_seasons(self):
        cur, responses = self._run_patch({"total_seasons": None})
        self.assertEqual(responses[-1][0], 200)
        sql, params = self._update_call(cur)
        self.assertIn("total_seasons = %s", sql)
        self.assertIn(None, params)

    def test_rejects_zero(self):
        cur, responses = self._run_patch({"total_seasons": 0})
        self.assertEqual(responses[-1][0], 400)
        self.assertEqual([c for c in cur.calls if c[0].startswith("UPDATE")], [])

    def test_rejects_non_integer(self):
        cur, responses = self._run_patch({"total_seasons": "5"})
        self.assertEqual(responses[-1][0], 400)

    def test_permissive_no_cross_field_check(self):
        """ADR-001: a current_season above total_seasons is NOT rejected by the API."""
        cur, responses = self._run_patch({"current_season": 9, "total_seasons": 3})
        self.assertEqual(responses[-1][0], 200)


# ── _details ─────────────────────────────────────────────────────────────────


class DetailsTotalSeasons(unittest.TestCase):
    def _run_details(self, *, mtype, tmdb_payload, has_key=True, watched_count=0):
        h, responses = make_handler(qs={"id": ["42"], "type": [mtype]})
        h.path = f"/api/details?id=42&type={mtype}"
        env = {"TMDB_API_KEY": "k"} if has_key else {}
        # series-episode-progress addendum: _details now issues a real DB query
        # (SELECT count(*) FROM episode_progress ...) for mt=='tv' to surface the
        # per-user watched_count numerator on reload (AC-6/AC-9) — patch the DB
        # boundary so this helper still drives the handler without a live DB.
        cur = FakeCursor(fetch_results=[{"n": watched_count}])
        with mock.patch.dict(os.environ, env, clear=False):
            if not has_key:
                os.environ.pop("TMDB_API_KEY", None)
            h._tmdb = lambda path, extra=None, ttl=None: tmdb_payload
            with patch_db(cur):
                h._details()
        return responses

    def test_exposes_total_for_tv(self):
        """AC-1: _details exposes total_seasons from number_of_seasons for tv."""
        responses = self._run_details(
            mtype="tv", tmdb_payload={"name": "S", "number_of_seasons": 6}
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["details"]["total_seasons"], 6)

    def test_null_for_movie(self):
        """BR-1: _details returns null total_seasons for a movie id even if present."""
        responses = self._run_details(
            mtype="movie", tmdb_payload={"title": "M", "number_of_seasons": 6}
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["details"]["total_seasons"])

    def test_tv_without_number_of_seasons_yields_null(self):
        """AC-8: tv record lacking number_of_seasons → total_seasons null, response intact."""
        responses = self._run_details(mtype="tv", tmdb_payload={"name": "S"})
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["details"]["total_seasons"])
        # Degraded response still carries the existing keys unchanged.
        self.assertIn("overview", payload["details"])
        self.assertIn("providers", payload["details"])

    def test_backdrop_and_cast_shape_capped_at_8(self):
        """AC-14 (modal-edit-section): _details exposes backdrop_path and returns
        cast as a list of {name, profile_path} objects capped at 8 even when TMDB
        returns more than 8 cast entries. This is the payload shape the redesigned
        detail modal depends on; asserted here so the contract is regression-safe."""
        cast_in = [
            {"name": f"Actor {i}", "profile_path": f"/p{i}.jpg", "character": "Role"}
            for i in range(12)
        ]
        responses = self._run_details(
            mtype="movie",
            tmdb_payload={
                "title": "M",
                "backdrop_path": "/bd.jpg",
                "credits": {"cast": cast_in},
            },
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        details = payload["details"]
        self.assertIn("backdrop_path", details)
        self.assertEqual(details["backdrop_path"], "/bd.jpg")
        cast = details["cast"]
        self.assertIsInstance(cast, list)
        self.assertEqual(len(cast), 8, "cast capped at 8 when TMDB returns 12")
        for member in cast:
            self.assertEqual(set(member.keys()), {"name", "profile_path"})
            self.assertIsInstance(member["name"], str)
            self.assertIsInstance(member["profile_path"], str)
        # Order preserved from TMDB (first 8 of 12).
        self.assertEqual(cast[0]["name"], "Actor 0")
        self.assertEqual(cast[7]["name"], "Actor 7")

    def test_no_key_degraded_response_unchanged(self):
        """AC-8: with no TMDB key, _details returns the existing needs_key response,
        no total_seasons key, no TMDB call."""
        h, responses = make_handler(qs={"id": ["42"], "type": ["tv"]})
        h.path = "/api/details?id=42&type=tv"
        called = {"tmdb": False}

        def _tmdb(path, extra=None):
            called["tmdb"] = True
            return {}

        h._tmdb = _tmdb
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMDB_API_KEY", None)
            h._details()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("needs_key"))
        self.assertNotIn("details", payload)
        self.assertFalse(called["tmdb"], "no TMDB call when key absent")


# ── Integration-style: AC-1 through the real do_POST handler ──────────────────


class AddSeriesFromModalIntegration(unittest.TestCase):
    """AC-1: adding a series 'from the modal' (a POST body carrying the TMDB
    total) drives the real do_POST handler and asserts total_seasons reaches the
    parameterised INSERT. DB boundary stubbed (no live Supabase in this session).
    """

    def test_modal_add_series_persists_total_seasons(self):
        cur = FakeCursor(fetch_results=[None, {"id": 99}])
        body = {
            "title": "Severance",
            "media_type": "tv",
            "tmdb_id": 95396,
            "year": "2022",
            "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
            "genres": ["Drama", "Ciencia ficción"],
            "total_seasons": 2,
            "status": "pendiente",
        }
        h, responses = make_handler(body=body)
        h.path = "/api/movies"
        with patch_db(cur):
            h.do_POST()
        self.assertEqual(responses[-1][0], 201)
        sql, params = _insert_call(cur)
        # Parameterised: the SQL carries %s placeholders, the value rides params.
        self.assertIn("%s", sql)
        self.assertEqual(_insert_total_seasons(cur), 2)


# ── series-episode-progress: _set_episodes (AC-3/AC-4/AC-5) ───────────────────


def _run_set_episodes(*, user_id="user-1", movie_id=7, body, fetch_results=None):
    """Drive the real _set_episodes handler with the DB boundary stubbed.

    fetch_results is FIFO-consumed: [title_row_or_None, top_row_or_None, count_row]
    — the title lookup, then (when the title resolves as an owned tv show) the two
    fetchone() calls inside _recompute_progress (max-watched row, watched count).
    """
    h, responses = make_handler(user_id=user_id, body=body)
    h.path = f"/api/movies/{movie_id}/episodes"
    cur = FakeCursor(fetch_results=fetch_results if fetch_results is not None else [])
    with patch_db(cur):
        h._set_episodes(movie_id)
    return h, responses, cur


class SetEpisodesUnit(unittest.TestCase):
    """AC-3/AC-4/AC-5: mark/unmark (single + whole-season), idempotent upsert,
    scoped DELETE, user_id isolation, not-owned -> 404, non-tv -> 400, bad body
    -> 400."""

    def test_mark_single_episode_inserts_scoped_and_idempotent(self):
        body = {"season": 1, "episode": 3, "watched": True}
        fetch = [{"tmdb_id": 42, "media_type": "tv"}, {"season": 1, "episode": 3}, {"n": 1}]
        h, responses, cur = _run_set_episodes(body=body, fetch_results=fetch)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        inserts = [c for c in cur.calls if c[0].startswith("INSERT INTO episode_progress")]
        self.assertEqual(len(inserts), 1)
        sql, params = inserts[0]
        self.assertIn("ON CONFLICT DO NOTHING", sql, "insert must be idempotent")
        self.assertEqual(params, ("user-1", 42, 1, 3))

    def test_mark_whole_season_batch_inserts_one_row_per_episode(self):
        body = {"season": 2, "episodes": [1, 2, 3], "watched": True}
        fetch = [{"tmdb_id": 42, "media_type": "tv"}, {"season": 2, "episode": 3}, {"n": 3}]
        h, responses, cur = _run_set_episodes(body=body, fetch_results=fetch)
        self.assertEqual(responses[-1][0], 200)
        inserts = [c for c in cur.calls if c[0].startswith("INSERT INTO episode_progress")]
        self.assertEqual(len(inserts), 3)
        marked = sorted(params[3] for _, params in inserts)
        self.assertEqual(marked, [1, 2, 3])
        for _, params in inserts:
            self.assertEqual(params[:3], ("user-1", 42, 2), "insert must be user_id-scoped")

    def test_unmark_single_episode_deletes_scoped(self):
        body = {"season": 1, "episode": 2, "watched": False}
        fetch = [{"tmdb_id": 42, "media_type": "tv"}, None, {"n": 0}]
        h, responses, cur = _run_set_episodes(body=body, fetch_results=fetch)
        self.assertEqual(responses[-1][0], 200)
        deletes = [c for c in cur.calls if c[0].startswith("DELETE FROM episode_progress")]
        self.assertEqual(len(deletes), 1)
        sql, params = deletes[0]
        self.assertIn("episode = ANY(%s)", sql)
        self.assertEqual(params, ("user-1", 42, 1, [2]))

    def test_unmark_whole_season_without_episode_numbers_clears_season(self):
        body = {"season": 3, "watched": False}
        fetch = [{"tmdb_id": 42, "media_type": "tv"}, None, {"n": 0}]
        h, responses, cur = _run_set_episodes(body=body, fetch_results=fetch)
        self.assertEqual(responses[-1][0], 200)
        deletes = [c for c in cur.calls if c[0].startswith("DELETE FROM episode_progress")]
        self.assertEqual(len(deletes), 1)
        sql, params = deletes[0]
        self.assertNotIn("episode = ANY", sql, "whole-season unmark must not filter by episode")
        self.assertEqual(params, ("user-1", 42, 3))

    def test_title_lookup_scoped_by_movie_id_and_user_id(self):
        body = {"season": 1, "episode": 1, "watched": True}
        fetch = [{"tmdb_id": 42, "media_type": "tv"}, {"season": 1, "episode": 1}, {"n": 1}]
        h, responses, cur = _run_set_episodes(user_id="user-42", movie_id=7, body=body, fetch_results=fetch)
        sql, params = cur.calls[0]
        self.assertTrue(sql.startswith("SELECT tmdb_id, media_type FROM movies"))
        self.assertEqual(params, (7, "user-42"), "PS-001: title lookup must be scoped by user_id")

    def test_not_owned_or_missing_movie_returns_404_no_write(self):
        body = {"season": 1, "episode": 1, "watched": True}
        h, responses, cur = _run_set_episodes(body=body, fetch_results=[None])
        self.assertEqual(responses[-1][0], 404)
        writes = [c for c in cur.calls if c[0].startswith(("INSERT", "DELETE", "UPDATE"))]
        self.assertEqual(writes, [], "no write must reach the DB for an unowned/missing id (IDOR-safe)")

    def test_non_tv_media_type_returns_400(self):
        body = {"season": 1, "episode": 1, "watched": True}
        h, responses, cur = _run_set_episodes(body=body, fetch_results=[{"tmdb_id": 1, "media_type": "movie"}])
        self.assertEqual(responses[-1][0], 400)

    def test_tv_title_without_tmdb_id_returns_400(self):
        body = {"season": 1, "episode": 1, "watched": True}
        h, responses, cur = _run_set_episodes(body=body, fetch_results=[{"tmdb_id": None, "media_type": "tv"}])
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_missing_season_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"watched": True})
        self.assertEqual(responses[-1][0], 400)
        self.assertEqual(cur.calls, [])

    def test_bad_body_season_zero_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"season": 0, "watched": True})
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_missing_watched_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"season": 1})
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_watched_not_bool_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"season": 1, "watched": "yes"})
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_episode_bool_rejected_as_int(self):
        """bool ⊂ int in Python — a boolean episode number must not pass an int≥1 guard."""
        h, responses, cur = _run_set_episodes(body={"season": 1, "episode": True, "watched": True})
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_episode_non_positive_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"season": 1, "episode": 0, "watched": True})
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_episodes_not_a_list_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"season": 1, "episodes": "1,2", "watched": True})
        self.assertEqual(responses[-1][0], 400)

    def test_bad_body_episodes_with_non_positive_int_returns_400(self):
        h, responses, cur = _run_set_episodes(body={"season": 1, "episodes": [1, 0], "watched": True})
        self.assertEqual(responses[-1][0], 400)

    def test_unauth_returns_401_no_db_call(self):
        h, responses = make_handler(user_id=None, body={"season": 1, "episode": 1, "watched": True})
        h.path = "/api/movies/7/episodes"
        cur = FakeCursor()
        with patch_db(cur):
            h._set_episodes(7)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [])


# ── series-episode-progress: _recompute_progress (AC-7) ───────────────────────


class RecomputeProgressUnit(unittest.TestCase):
    """AC-7: current_season/current_episode = MAX watched (season, episode);
    NULL/NULL when no marks remain; returns watched_count."""

    def _run(self, *, top_row, count_row, user_id="user-1", tmdb_id=42, movie_id=7):
        cur = FakeCursor(fetch_results=[top_row, count_row])
        h, _ = make_handler(user_id=user_id)
        result = h._recompute_progress(cur, user_id, tmdb_id, movie_id)
        return result, cur

    def test_derives_max_watched_position(self):
        (watched_count, cs, ce), cur = self._run(
            top_row={"season": 3, "episode": 5}, count_row={"n": 12})
        self.assertEqual((cs, ce), (3, 5))
        self.assertEqual(watched_count, 12)

    def test_null_position_when_no_marks_remain(self):
        (watched_count, cs, ce), cur = self._run(top_row=None, count_row={"n": 0})
        self.assertIsNone(cs)
        self.assertIsNone(ce)
        self.assertEqual(watched_count, 0)

    def test_update_writes_derived_position_scoped_by_id_and_user_id(self):
        (watched_count, cs, ce), cur = self._run(
            top_row={"season": 2, "episode": 8}, count_row={"n": 9},
            user_id="user-99", tmdb_id=555, movie_id=13)
        update = [c for c in cur.calls if c[0].startswith("UPDATE movies")]
        self.assertEqual(len(update), 1)
        sql, params = update[0]
        self.assertEqual(params, (2, 8, 13, "user-99"))

    def test_update_writes_null_null_when_no_marks(self):
        (_, cs, ce), cur = self._run(top_row=None, count_row={"n": 0})
        sql, params = [c for c in cur.calls if c[0].startswith("UPDATE movies")][0]
        self.assertEqual(params[0], None)
        self.assertEqual(params[1], None)

    def test_ordered_by_season_desc_episode_desc_limit_1(self):
        _, cur = self._run(top_row={"season": 1, "episode": 1}, count_row={"n": 1})
        sql, _ = cur.calls[0]
        self.assertIn("ORDER BY season DESC, episode DESC LIMIT 1", sql)


# ── series-episode-progress: _season (AC-1/AC-2/AC-12) ────────────────────────


def _run_season(*, user_id="user-1", tmdb_id=550, season=1, rate_limited=False,
                 has_key=True, tmdb_payload=None, tmdb_raises=False, marks_fetch=None):
    """Drive the real _season handler with auth/rate-limit/TMDB/DB stubbed.

    `rate_limited=True` mirrors the real _rate_limited() contract: it emits the
    429 response itself and returns True (the caller just returns)."""
    h, responses = make_handler(user_id=user_id)
    h.path = f"/api/tv/{tmdb_id}/season/{season}"

    def _rl(uid):
        if rate_limited:
            h._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                     extra_headers={"Retry-After": 30})
        return rate_limited

    h._rate_limited = _rl
    if tmdb_raises:
        h._tmdb = mock.Mock(side_effect=Exception("boom"))
    else:
        h._tmdb = lambda path, extra=None, ttl=None: tmdb_payload
    cur = FakeCursor(fetch_results=[marks_fetch if marks_fetch is not None else []])
    env = {"TMDB_API_KEY": "k"} if has_key else {}
    with mock.patch.dict(os.environ, env, clear=False):
        if not has_key:
            os.environ.pop("TMDB_API_KEY", None)
        with patch_db(cur):
            h._season(tmdb_id, season)
    return h, responses, cur


_SEASON_PAYLOAD = {
    "season_number": 1,
    "name": "Season 1",
    "episodes": [
        {"episode_number": 1, "name": "Pilot", "air_date": "2020-01-01",
         "runtime": 45, "overview": "The beginning.", "still_path": "/s1.jpg"},
        {"episode_number": 2, "name": "Episode 2", "air_date": "2020-01-08",
         "runtime": 42, "overview": "It continues.", "still_path": None},
    ],
}


class SeasonEndpointUnit(unittest.TestCase):
    """AC-1/AC-2/AC-12: 401 w/o JWT, 429 when rate-limited, needs_key degrade,
    marks-merge sets `watched`, allow-list projection shape."""

    def test_unauth_returns_401_no_tmdb_or_db_call(self):
        h, responses = make_handler(user_id=None)
        h.path = "/api/tv/550/season/1"
        tmdb_calls = []
        h._tmdb = lambda *a, **k: tmdb_calls.append(1)
        cur = FakeCursor()
        with patch_db(cur):
            h._season(550, 1)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(tmdb_calls, [])
        self.assertEqual(cur.calls, [])

    def test_rate_limited_returns_429(self):
        h, responses, cur = _run_season(rate_limited=True, tmdb_payload=_SEASON_PAYLOAD)
        self.assertEqual(responses[-1][0], 429)
        self.assertEqual(cur.calls, [], "no DB call once rate-limited")

    def test_no_key_returns_needs_key_degrade(self):
        h, responses, cur = _run_season(has_key=False, tmdb_payload=_SEASON_PAYLOAD)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload.get("ok"))
        self.assertTrue(payload.get("needs_key"))

    def test_tmdb_failure_returns_502_generic(self):
        h, responses, cur = _run_season(tmdb_raises=True)
        status, payload = responses[-1]
        self.assertEqual(status, 502)
        self.assertNotIn("boom", payload.get("error", ""), "raw TMDB error must never be serialized")

    def test_marks_merge_sets_watched_per_episode(self):
        h, responses, cur = _run_season(
            tmdb_payload=_SEASON_PAYLOAD, marks_fetch=[{"season": 1, "episode": 2}])
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        episodes = payload["season"]["episodes"]
        watched_by_num = {e["episode_number"]: e["watched"] for e in episodes}
        self.assertFalse(watched_by_num[1])
        self.assertTrue(watched_by_num[2])

    def test_marks_query_scoped_by_user_tmdb_and_season(self):
        h, responses, cur = _run_season(user_id="user-7", tmdb_id=550, season=3,
                                         tmdb_payload={"season_number": 3, "name": "S3", "episodes": []})
        sql, params = cur.calls[0]
        self.assertTrue(sql.startswith("SELECT season, episode FROM episode_progress"))
        self.assertEqual(params, ("user-7", 550, 3))

    def test_response_projection_is_allow_listed(self):
        h, responses, cur = _run_season(tmdb_payload=_SEASON_PAYLOAD)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        season = payload["season"]
        self.assertEqual(set(season.keys()), {"season_number", "name", "episodes"})
        for ep in season["episodes"]:
            self.assertEqual(
                set(ep.keys()),
                {"episode_number", "name", "air_date", "runtime", "overview", "still_path", "watched"},
            )

    def test_uses_season_cache_ttl(self):
        """The season endpoint must pass ttl=SEASON_CACHE_TTL to _tmdb (BR-3/CA-*)."""
        h, responses = make_handler(user_id="user-1")
        h.path = "/api/tv/550/season/1"
        h._rate_limited = lambda uid: False
        calls = []

        def _tmdb(path, extra=None, ttl=None):
            calls.append(ttl)
            return _SEASON_PAYLOAD

        h._tmdb = _tmdb
        cur = FakeCursor(fetch_results=[[]])
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False):
            with patch_db(cur):
                h._season(550, 1)
        self.assertEqual(calls, [server.SEASON_CACHE_TTL])


# ── series-episode-progress: _details additive fields (AC-6) ──────────────────


class DetailsEpisodeFields(unittest.TestCase):
    """AC-6: _details exposes total_episodes + seasons (season 0 excluded) for
    tv, absent/null for movie."""

    def _run(self, *, mtype, tmdb_payload, watched_count=0):
        h, responses = make_handler(qs={"id": ["42"], "type": [mtype]})
        h.path = f"/api/details?id=42&type={mtype}"
        h._tmdb = lambda path, extra=None, ttl=None: tmdb_payload
        cur = FakeCursor(fetch_results=[{"n": watched_count}])
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False):
            with patch_db(cur):
                h._details()
        return responses, cur

    def test_tv_exposes_total_episodes_and_seasons_excluding_season_0(self):
        payload = {
            "name": "S", "number_of_seasons": 2, "number_of_episodes": 20,
            "seasons": [
                {"season_number": 0, "name": "Specials", "episode_count": 3},
                {"season_number": 1, "name": "Season 1", "episode_count": 10},
                {"season_number": 2, "name": "Season 2", "episode_count": 10},
            ],
        }
        responses, cur = self._run(mtype="tv", tmdb_payload=payload)
        status, body = responses[-1]
        self.assertEqual(status, 200)
        details = body["details"]
        self.assertEqual(details["total_episodes"], 20)
        season_numbers = [s["season_number"] for s in details["seasons"]]
        self.assertEqual(season_numbers, [1, 2], "season 0/specials must be excluded")

    def test_movie_total_episodes_and_seasons_are_null(self):
        responses, cur = self._run(
            mtype="movie", tmdb_payload={"title": "M", "number_of_episodes": 20})
        status, body = responses[-1]
        self.assertEqual(status, 200)
        details = body["details"]
        self.assertIsNone(details["total_episodes"])
        self.assertIsNone(details["seasons"])

    def test_watched_count_present_for_tv_zero_for_movie(self):
        responses_tv, _ = self._run(
            mtype="tv", tmdb_payload={"name": "S", "number_of_episodes": 20}, watched_count=7)
        self.assertEqual(responses_tv[-1][1]["details"]["watched_count"], 7)
        responses_movie, cur = self._run(mtype="movie", tmdb_payload={"title": "M"})
        self.assertEqual(responses_movie[-1][1]["details"]["watched_count"], 0)
        # No episode_progress query at all for a movie (BR-1 — movies have no episodes).
        self.assertEqual(cur.calls, [])

    def test_watched_count_query_scoped_by_user_and_tmdb_id(self):
        h, responses = make_handler(user_id="user-3", qs={"id": ["550"], "type": ["tv"]})
        h.path = "/api/details?id=550&type=tv"
        h._tmdb = lambda path, extra=None, ttl=None: {"name": "S", "number_of_episodes": 5}
        cur = FakeCursor(fetch_results=[{"n": 4}])
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False):
            with patch_db(cur):
                h._details()
        sql, params = cur.calls[0]
        self.assertTrue(sql.startswith("SELECT count(*) AS n FROM episode_progress"))
        self.assertEqual(params, ("user-3", 550))


# ── series-episode-progress: RTBF + orphan purge (AC-13) ──────────────────────


class EpisodeProgressPurgeUnit(unittest.TestCase):
    """AC-13: _delete_account deletes the user's episode_progress; do_DELETE
    purges a tv title's episodes (scoped to media_type='tv')."""

    def test_delete_account_purges_episode_progress(self):
        h, responses = make_handler(user_id="user-1", body={
            "password": "correct-password-123", "confirm_username": "usera"})
        h.headers = {"Authorization": "Bearer stub-token"}
        h.path = "/api/account/delete"
        h._supabase_verify_password = lambda e, p: True
        h._supabase_admin_delete_user = lambda uid: True
        cur = FakeCursor(fetch_results=[{"username": "usera"}])
        with mock.patch.object(server, "verify_jwt_identity", return_value=("user-1", "u@example.com")), \
             mock.patch.object(server, "rate_check", return_value=(True, 0)), \
             patch_db(cur):
            h._delete_account()
        self.assertEqual(responses[-1][0], 200)
        purges = [c for c in cur.calls
                  if c[0] == "DELETE FROM episode_progress WHERE user_id = %s"]
        self.assertEqual(len(purges), 1)
        self.assertEqual(purges[0][1], ("user-1",))

    def test_do_delete_purges_episode_progress_for_tv_title(self):
        h, responses = make_handler(user_id="user-1")
        h.path = "/api/movies/7"
        cur = FakeCursor(fetch_results=[{"media_type": "tv", "tmdb_id": 42}], rowcount=1)
        with patch_db(cur):
            h.do_DELETE()
        self.assertEqual(responses[-1][0], 200)
        purges = [c for c in cur.calls
                  if c[0].startswith("DELETE FROM episode_progress WHERE user_id")]
        self.assertEqual(len(purges), 1)
        self.assertEqual(purges[0][1], ("user-1", 42))

    def test_do_delete_does_not_purge_episode_progress_for_movie(self):
        h, responses = make_handler(user_id="user-1")
        h.path = "/api/movies/7"
        cur = FakeCursor(fetch_results=[{"media_type": "movie", "tmdb_id": 550}], rowcount=1)
        with patch_db(cur):
            h.do_DELETE()
        self.assertEqual(responses[-1][0], 200)
        purges = [c for c in cur.calls if c[0].startswith("DELETE FROM episode_progress")]
        self.assertEqual(purges, [], "a movie delete must never touch episode_progress")

    def test_do_delete_missing_title_returns_404_no_purge(self):
        h, responses = make_handler(user_id="user-1")
        h.path = "/api/movies/999"
        cur = FakeCursor(fetch_results=[None], rowcount=0)
        with patch_db(cur):
            h.do_DELETE()
        self.assertEqual(responses[-1][0], 404)
        purges = [c for c in cur.calls if c[0].startswith("DELETE FROM episode_progress")]
        self.assertEqual(purges, [])


if __name__ == "__main__":
    unittest.main()
