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
    def _run_details(self, *, mtype, tmdb_payload, has_key=True):
        h, responses = make_handler(qs={"id": ["42"], "type": [mtype]})
        h.path = f"/api/details?id=42&type={mtype}"
        env = {"TMDB_API_KEY": "k"} if has_key else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not has_key:
                os.environ.pop("TMDB_API_KEY", None)
            h._tmdb = lambda path, extra=None: tmdb_payload
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


if __name__ == "__main__":
    unittest.main()
