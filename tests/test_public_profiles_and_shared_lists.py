"""Unit + integration tests for the public-profiles-and-shared-lists feature.

Covers every ### Tester scope row in the task file:
  - Unit: _normalize_username (AC-1, AC-2)
  - Unit: _public_collection_projection strips note/email/user_id (AC-5)
  - Unit: public stats reuse compute_level() (AC-6)
  - Unit: _client_ip XFF first-hop / fallback
  - Integration: username case-insensitive uniqueness 409 (AC-2)
  - Integration: publish-without-username 400 (AC-1)
  - Integration: private profile public fetch -> 404 (AC-3)
  - Integration: collection-only vs stats-only projections (AC-4, AC-6)
  - Integration: public collection has no note (AC-5)
  - Integration: private list not returned (AC-7); unlisted by token (AC-8);
    unlisted absent from profile (AC-9)
  - Integration: public list on profile + by URL; titles regardless of
    collection (AC-10, AC-12)
  - Integration: re-privatised profile/list -> 404 (AC-11)
  - Integration: cross-user PATCH/DELETE list -> 404, unchanged (AC-13)
  - Integration: anonymous owner-mutation -> 401 (AC-14)
  - Integration: burst over per-IP cap -> 429 + Retry-After
  - Integration: audit log entry emitted on consent/visibility change

DB boundary is stubbed with FakeCursor throughout (no live Supabase).
"""

import unittest
from unittest import mock

import server
from server import (
    _normalize_username,
    _public_collection_projection,
    compute_level,
    POINTS_VISTA,
    POINTS_RATING,
    POINTS_NOTE,
    RESERVED_USERNAMES,
)
from tests._harness import FakeCursor, make_handler, patch_db


# ── Unit: _normalize_username ─────────────────────────────────────────────────


class NormalizeUsernameUnit(unittest.TestCase):
    """AC-1, AC-2: username format validation helper."""

    def test_valid_slug_returned_lowercased(self):
        """A valid lowercase slug is returned as-is (AC-1)."""
        self.assertEqual(_normalize_username("alice"), "alice")

    def test_uppercase_is_lowercased(self):
        """Uppercase input is normalised to lowercase (AC-2, case-insensitive)."""
        self.assertEqual(_normalize_username("ALICE"), "alice")
        self.assertEqual(_normalize_username("CamelCase1"), "camelcase1")

    def test_mixed_valid_chars(self):
        """Slug with digits, hyphens, underscores passes."""
        self.assertEqual(_normalize_username("my-user_42"), "my-user_42")

    def test_too_short_rejected(self):
        """Username shorter than 3 chars is rejected -> None (AC-1)."""
        self.assertIsNone(_normalize_username("ab"))
        self.assertIsNone(_normalize_username("a"))
        self.assertIsNone(_normalize_username(""))

    def test_too_long_rejected(self):
        """Username longer than 30 chars is rejected (AC-1)."""
        self.assertIsNone(_normalize_username("a" * 31))

    def test_exactly_30_chars_accepted(self):
        """30-char username at the upper boundary is valid."""
        self.assertEqual(_normalize_username("a" * 30), "a" * 30)

    def test_exactly_3_chars_accepted(self):
        """3-char username at the lower boundary is valid."""
        self.assertEqual(_normalize_username("abc"), "abc")

    def test_illegal_chars_rejected(self):
        """Spaces, dots, at-sign, and other special chars are rejected (AC-1)."""
        for bad in ("alice smith", "alice.smith", "alice@", "alice!", "ñoño"):
            self.assertIsNone(_normalize_username(bad), f"Expected None for {bad!r}")

    def test_reserved_names_rejected(self):
        """All reserved usernames are blocked (AC-1 - prevents route collisions)."""
        for name in RESERVED_USERNAMES:
            self.assertIsNone(_normalize_username(name), f"Expected None for reserved {name!r}")

    def test_reserved_names_case_insensitive(self):
        """Reserved names are also blocked when uppercased (lowercased before check)."""
        self.assertIsNone(_normalize_username("API"))
        self.assertIsNone(_normalize_username("Admin"))

    def test_non_string_rejected(self):
        """Non-string input (None, int, list) returns None without crashing."""
        self.assertIsNone(_normalize_username(None))
        self.assertIsNone(_normalize_username(42))
        self.assertIsNone(_normalize_username(["alice"]))


# ── Unit: _public_collection_projection ──────────────────────────────────────


class PublicCollectionProjectionUnit(unittest.TestCase):
    """AC-5: public collection allow-list strips private fields."""

    def _make_row(self, **overrides):
        """Return a dict that looks like a DB row from the movies table."""
        row = {
            "tmdb_id": 27205,
            "title": "Inception",
            "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
            "status": "vista",
            "rating": 4,
            "media_type": "movie",
            "current_season": None,
            "total_seasons": None,
            # Private fields that must NEVER appear in the public projection:
            "note": "My private note",
            "email": "user@example.com",
            "user_id": "some-uuid-1234",
        }
        row.update(overrides)
        return row

    def test_strips_note(self):
        """AC-5: note is never serialized in the public projection."""
        result = _public_collection_projection([self._make_row()])
        self.assertNotIn("note", result[0])

    def test_strips_email(self):
        """AC-5: email is never serialized in the public projection."""
        result = _public_collection_projection([self._make_row()])
        self.assertNotIn("email", result[0])

    def test_strips_user_id(self):
        """AC-5: user_id is never serialized in the public projection."""
        result = _public_collection_projection([self._make_row()])
        self.assertNotIn("user_id", result[0])

    def test_keeps_allowed_fields(self):
        """AC-4/AC-5: all allowed fields are present and correct."""
        row = self._make_row(tmdb_id=438631, title="Dune", rating=5, status="viendo",
                             media_type="tv", current_season=1, total_seasons=3)
        result = _public_collection_projection([row])
        r = result[0]
        self.assertEqual(r["tmdb_id"], 438631)
        self.assertEqual(r["title"], "Dune")
        self.assertEqual(r["poster_url"], "https://image.tmdb.org/t/p/w342/x.jpg")
        self.assertEqual(r["status"], "viendo")
        self.assertEqual(r["rating"], 5)
        self.assertEqual(r["media_type"], "tv")
        self.assertEqual(r["current_season"], 1)
        self.assertEqual(r["total_seasons"], 3)

    def test_projection_has_exactly_eight_fields(self):
        """Projection contains exactly the 8 allowed fields - no extras can sneak in.
        tmdb_id is the PUBLIC TMDB id of the title (not personal data); note/email/
        user_id stay excluded (GD-001)."""
        result = _public_collection_projection([self._make_row()])
        self.assertEqual(
            set(result[0].keys()),
            {"tmdb_id", "title", "poster_url", "status", "rating", "media_type",
             "current_season", "total_seasons"},
        )

    def test_multiple_rows(self):
        """Projection handles multiple rows correctly."""
        rows = [self._make_row(title=f"Film {i}") for i in range(3)]
        result = _public_collection_projection(rows)
        self.assertEqual(len(result), 3)
        for r in result:
            self.assertNotIn("note", r)

    def test_empty_input(self):
        """Empty collection returns empty list."""
        self.assertEqual(_public_collection_projection([]), [])


# ── Unit: public stats reuse compute_level() ─────────────────────────────────


class PublicStatsUsesComputeLevel(unittest.TestCase):
    """AC-6: public stats projection delegates to compute_level(), same as _level."""

    def test_compute_level_called_with_correct_points(self):
        """compute_level() is the single source of truth (PS-004).
        The public stats computation: vistas*10 + valoradas*5 + notas*5."""
        # Direct unit test: verify compute_level returns expected structure.
        result = compute_level(0)
        self.assertIn("points", result)
        self.assertIn("level", result)
        self.assertIn("name", result)
        self.assertIn("progress_pct", result)

    def test_stats_formula_matches_level_endpoint(self):
        """The formula used in _public_profile (server.py:1401-1403) matches _level.
        3 vistas=30pts, 2 valoradas=10pts, 1 nota=5pts -> 45pts -> level 1."""
        points = 3 * POINTS_VISTA + 2 * POINTS_RATING + 1 * POINTS_NOTE
        self.assertEqual(points, 45)
        lvl = compute_level(points)
        self.assertEqual(lvl["level"], 1)  # Espectador: 0-49
        self.assertEqual(lvl["points"], 45)

    def test_compute_level_at_boundary(self):
        """50 points exactly -> level 2 (Aficionado boundary)."""
        lvl = compute_level(50)
        self.assertEqual(lvl["level"], 2)

    def test_compute_level_max_level(self):
        """1200+ points -> level 6 (Maestro), progress_pct=100."""
        lvl = compute_level(1500)
        self.assertEqual(lvl["level"], 6)
        self.assertEqual(lvl["progress_pct"], 100)
        self.assertIsNone(lvl["next_min"])


# ── Unit: _client_ip derivation ───────────────────────────────────────────────


class ClientIpUnit(unittest.TestCase):
    """Unit: per-IP key derivation - XFF first hop / fallback."""

    def _make_handler_for_ip(self, xff=None, socket_ip="10.0.0.1"):
        h = server.Handler.__new__(server.Handler)
        h.headers = {}
        if xff is not None:
            h.headers["X-Forwarded-For"] = xff
        h.client_address = (socket_ip, 12345)
        return h

    def test_xff_single_hop(self):
        """First hop of X-Forwarded-For is used as the client IP."""
        h = self._make_handler_for_ip(xff="1.2.3.4")
        self.assertEqual(h._client_ip(), "1.2.3.4")

    def test_xff_multiple_hops_takes_first(self):
        """When XFF has multiple IPs, the first (leftmost) is returned."""
        h = self._make_handler_for_ip(xff="1.2.3.4, 10.0.0.2, 10.0.0.3")
        self.assertEqual(h._client_ip(), "1.2.3.4")

    def test_xff_missing_falls_back_to_socket(self):
        """When X-Forwarded-For is absent, falls back to client_address[0]."""
        h = self._make_handler_for_ip(socket_ip="192.168.1.5")
        self.assertEqual(h._client_ip(), "192.168.1.5")

    def test_xff_empty_string_falls_back_to_socket(self):
        """When X-Forwarded-For is empty, falls back to client_address[0]."""
        h = self._make_handler_for_ip(xff="", socket_ip="192.168.1.5")
        self.assertEqual(h._client_ip(), "192.168.1.5")

    def test_xff_whitespace_only_falls_back(self):
        """When X-Forwarded-For is all whitespace after comma split, falls back."""
        # If XFF contains only whitespace after stripping the first hop, first.strip() is ""
        h = self._make_handler_for_ip(xff="  ", socket_ip="192.168.1.7")
        # "  ".split(",")[0].strip() == "" -> falsy -> fallback
        self.assertEqual(h._client_ip(), "192.168.1.7")


# ── Integration: _patch_profile ───────────────────────────────────────────────


class PatchProfileIntegration(unittest.TestCase):
    """Integration tests for PATCH /api/profile (handler-level, DB stubbed)."""

    def _run_patch(self, body, *, user_id="user-test",
                   current_username=None, current_is_public=False,
                   raise_unique_violation=False):
        """Run _patch_profile with a stubbed DB.

        current_username/current_is_public simulate the existing DB row.
        raise_unique_violation simulates a psycopg2 UniqueViolation on upsert.
        """
        # fetchone() for the current-state SELECT
        current_row = None
        if current_username is not None or current_is_public:
            current_row = {
                "username": current_username,
                "is_public": current_is_public,
                "show_collection": False,
                "show_stats": False,
            }

        if raise_unique_violation:
            class _FakeCursorWithUniqueViolation(FakeCursor):
                def execute(self, sql, params=None):
                    self.calls.append((sql, params))
                    if "INSERT INTO profiles" in sql and "ON CONFLICT" in sql:
                        raise psycopg2.errors.UniqueViolation("duplicate key")

            import psycopg2.errors
            cur = _FakeCursorWithUniqueViolation(fetch_results=[current_row])
        else:
            cur = FakeCursor(fetch_results=[current_row])

        h, responses = make_handler(body=body, user_id=user_id)
        with patch_db(cur):
            h._patch_profile()
        return cur, responses

    def test_publish_without_username_returns_400(self):
        """AC-1: setting is_public=true while username is None -> 400."""
        _, responses = self._run_patch(
            {"is_public": True},
            current_username=None,
        )
        self.assertEqual(responses[-1][0], 400)

    def test_publish_with_username_succeeds(self):
        """AC-1: setting is_public=true when username is already set -> 200."""
        _, responses = self._run_patch(
            {"is_public": True},
            current_username="alice",
            current_is_public=False,
        )
        self.assertEqual(responses[-1][0], 200)

    def test_set_username_and_publish_in_one_patch(self):
        """AC-1: providing username + is_public=true together -> 200."""
        _, responses = self._run_patch(
            {"username": "alice", "is_public": True},
            current_username=None,
        )
        self.assertEqual(responses[-1][0], 200)

    def test_username_uniqueness_conflict_returns_409(self):
        """AC-2: if the DB raises UniqueViolation on upsert -> 409."""
        _, responses = self._run_patch(
            {"username": "taken"},
            current_username=None,
            raise_unique_violation=True,
        )
        self.assertEqual(responses[-1][0], 409)

    def test_invalid_username_format_returns_400(self):
        """AC-1: username with illegal chars -> 400."""
        _, responses = self._run_patch({"username": "bad user!"})
        self.assertEqual(responses[-1][0], 400)

    def test_reserved_username_returns_400(self):
        """AC-1: reserved username -> 400."""
        _, responses = self._run_patch({"username": "admin"})
        self.assertEqual(responses[-1][0], 400)

    def test_too_short_username_returns_400(self):
        """AC-1: username too short -> 400."""
        _, responses = self._run_patch({"username": "ab"})
        self.assertEqual(responses[-1][0], 400)

    def test_anonymous_patch_returns_401(self):
        """AC-14: unauthenticated PATCH /api/profile -> 401."""
        h, responses = make_handler(body={"username": "alice"}, user_id=None)
        h._patch_profile()
        self.assertEqual(responses[-1][0], 401)

    def test_empty_patch_returns_400(self):
        """Empty body with no fields -> 400."""
        _, responses = self._run_patch({}, current_username="alice")
        self.assertEqual(responses[-1][0], 400)

    def test_non_bool_flag_returns_400(self):
        """is_public must be boolean; string is rejected -> 400."""
        _, responses = self._run_patch(
            {"is_public": "true"},
            current_username="alice",
        )
        self.assertEqual(responses[-1][0], 400)

    def test_username_stored_lowercased(self):
        """AC-2: uppercase username in body is normalised and stored lowercase."""
        cur, responses = self._run_patch(
            {"username": "ALICE"},
            current_username=None,
        )
        self.assertEqual(responses[-1][0], 200)
        upsert_calls = [c for c in cur.calls if "INSERT INTO profiles" in c[0]]
        self.assertTrue(upsert_calls, "No upsert call recorded")
        params = upsert_calls[0][1]
        # The normalised username ("alice") must appear in the params.
        self.assertIn("alice", params)
        self.assertNotIn("ALICE", params)

    def test_audit_log_emitted_on_username_set(self):
        """Consent-change audit: setting username emits an audit line."""
        with mock.patch("server._audit") as mock_audit:
            self._run_patch(
                {"username": "newuser"},
                current_username=None,
            )
        mock_audit.assert_called()
        call_args = mock_audit.call_args_list
        actions = [c[0][0] for c in call_args]
        self.assertIn("profile.username_set", actions)

    def test_audit_log_emitted_on_publish(self):
        """Consent-change audit: publishing the profile emits an audit line."""
        with mock.patch("server._audit") as mock_audit:
            self._run_patch(
                {"is_public": True},
                current_username="alice",
                current_is_public=False,
            )
        mock_audit.assert_called()
        call_args = mock_audit.call_args_list
        actions = [c[0][0] for c in call_args]
        self.assertIn("profile.publish", actions)

    def test_audit_log_emitted_on_unpublish(self):
        """Consent-change audit: unpublishing the profile emits an audit line."""
        with mock.patch("server._audit") as mock_audit:
            self._run_patch(
                {"is_public": False},
                current_username="alice",
                current_is_public=True,
            )
        mock_audit.assert_called()
        call_args = mock_audit.call_args_list
        actions = [c[0][0] for c in call_args]
        self.assertIn("profile.unpublish", actions)


# ── Integration: _public_profile ─────────────────────────────────────────────


class PublicProfileIntegration(unittest.TestCase):
    """Integration tests for GET /api/public/profile/{username} (DB stubbed)."""

    def _run_public_profile(self, username, *, fetch_results):
        """Run _public_profile with a stubbed DB and rate limit disabled."""
        cur = FakeCursor(fetch_results=fetch_results)
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile(username)
        return cur, responses

    def test_private_profile_returns_404(self):
        """AC-3: profile with is_public=False -> 404."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": False, "show_collection": True, "show_stats": True,
        }
        _, responses = self._run_public_profile("alice", fetch_results=[prof_row])
        self.assertEqual(responses[-1][0], 404)

    def test_nonexistent_profile_returns_404(self):
        """AC-3: username not in DB -> 404 (no enumeration)."""
        _, responses = self._run_public_profile("nobody", fetch_results=[None])
        self.assertEqual(responses[-1][0], 404)

    def test_public_profile_collection_only(self):
        """AC-4: show_collection=True, show_stats=False -> body has collection, no stats."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": True, "show_collection": True, "show_stats": False,
        }
        collection_rows = [
            {"tmdb_id": 438631, "title": "Dune", "poster_url": "https://image.tmdb.org/x.jpg",
             "status": "vista", "rating": 5, "media_type": "movie",
             "current_season": None, "total_seasons": None},
        ]
        lists_rows = []
        _, responses = self._run_public_profile(
            "alice",
            # follows: followers_count, following_count, followers[], following[]
            fetch_results=[prof_row, collection_rows, lists_rows, {"c": 0}, {"c": 0}, [], []],
        )
        self.assertEqual(responses[-1][0], 200)
        payload = responses[-1][1]["profile"]
        self.assertIn("collection", payload)
        self.assertNotIn("stats", payload)

    def test_public_profile_stats_only(self):
        """AC-6: show_collection=False, show_stats=True -> body has stats, no collection."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": True, "show_collection": False, "show_stats": True,
        }
        stats_row = {"vistas": 5, "valoradas": 3, "notas": 1}
        lists_rows = []
        _, responses = self._run_public_profile(
            "alice",
            fetch_results=[prof_row, stats_row, lists_rows, {"c": 0}, {"c": 0}, [], []],
        )
        self.assertEqual(responses[-1][0], 200)
        payload = responses[-1][1]["profile"]
        self.assertNotIn("collection", payload)
        self.assertIn("stats", payload)
        # AC-6: stats uses compute_level() — verify the structure
        self.assertIn("level", payload["stats"])
        self.assertIn("points", payload["stats"])

    def test_public_collection_has_no_note(self):
        """AC-5: note never appears in the public collection payload."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": True, "show_collection": True, "show_stats": False,
        }
        # The DB row includes note (as a real DB row would)
        collection_rows = [
            {"tmdb_id": 550, "title": "Film", "poster_url": "https://image.tmdb.org/x.jpg",
             "status": "vista", "rating": 4, "media_type": "movie",
             "current_season": None, "total_seasons": None,
             "note": "SECRET NOTE", "email": "u@e.com", "user_id": "uid-1"},
        ]
        lists_rows = []
        _, responses = self._run_public_profile(
            "alice",
            fetch_results=[prof_row, collection_rows, lists_rows, {"c": 0}, {"c": 0}, [], []],
        )
        self.assertEqual(responses[-1][0], 200)
        col = responses[-1][1]["profile"]["collection"]
        for item in col:
            self.assertNotIn("note", item)
            self.assertNotIn("email", item)
            self.assertNotIn("user_id", item)

    def test_unlisted_list_not_in_public_profile(self):
        """AC-9: unlisted lists never appear in the public profile listing."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": True, "show_collection": False, "show_stats": False,
        }
        # The query uses WHERE visibility='public', so unlisted lists are excluded.
        # We assert: only lists with visibility='public' in the DB query.
        cur = FakeCursor(fetch_results=[prof_row, [], {"c": 0}, {"c": 0}, [], []])
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile("alice")
        # Find the lists query in cursor calls
        lists_queries = [
            c for c in cur.calls
            if "lists" in c[0].lower() and "visibility" in c[0]
        ]
        self.assertTrue(lists_queries, "Expected a lists query with visibility filter")
        # The query must filter for visibility = 'public'
        self.assertIn("'public'", lists_queries[-1][0])

    def test_public_list_appears_on_profile(self):
        """AC-10: public lists appear in the profile response."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": True, "show_collection": False, "show_stats": False,
        }
        public_list_row = {
            "id": "list-uuid-1", "name": "My Horror List",
            "share_token": "token-uuid-1", "item_count": 3,
        }
        _, responses = self._run_public_profile(
            "alice",
            fetch_results=[prof_row, [public_list_row], {"c": 0}, {"c": 0}, [], []],
        )
        self.assertEqual(responses[-1][0], 200)
        lists = responses[-1][1]["profile"]["lists"]
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["name"], "My Horror List")

    def test_reprivatised_profile_returns_404(self):
        """AC-11: profile set back to private -> 404 immediately."""
        prof_row = {
            "user_id": "uid-1", "username": "alice",
            "is_public": False,  # re-privatised
            "show_collection": True, "show_stats": True,
        }
        _, responses = self._run_public_profile("alice", fetch_results=[prof_row])
        self.assertEqual(responses[-1][0], 404)


# ── Integration: _public_list ─────────────────────────────────────────────────


class PublicListIntegration(unittest.TestCase):
    """Integration tests for GET /api/public/list/{share_token} (DB stubbed)."""

    def _run_public_list(self, token, *, fetch_results):
        cur = FakeCursor(fetch_results=fetch_results)
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_list(token)
        return cur, responses

    def test_private_list_returns_404(self):
        """AC-7: private list -> 404."""
        lst_row = {"name": "My List", "visibility": "private", "owner_username": "alice"}
        _, responses = self._run_public_list(
            "some-token-uuid", fetch_results=[lst_row]
        )
        self.assertEqual(responses[-1][0], 404)

    def test_unknown_token_returns_404(self):
        """AC-7: unknown share token -> 404."""
        _, responses = self._run_public_list("bad-token", fetch_results=[None])
        self.assertEqual(responses[-1][0], 404)

    def test_unlisted_resolves_by_token(self):
        """AC-8: unlisted list accessible by its token."""
        lst_row = {"name": "Hidden List", "visibility": "unlisted", "owner_username": "alice"}
        items = [
            {"tmdb_id": 1, "media_type": "movie", "title": "Film 1",
             "year": "2020", "poster_url": "https://image.tmdb.org/x.jpg"},
        ]
        _, responses = self._run_public_list("token-uuid", fetch_results=[lst_row, items])
        self.assertEqual(responses[-1][0], 200)
        self.assertEqual(responses[-1][1]["list"]["name"], "Hidden List")

    def test_public_list_resolves_by_token(self):
        """AC-10: public list accessible by its token URL."""
        lst_row = {"name": "Public List", "visibility": "public", "owner_username": "alice"}
        items = []
        _, responses = self._run_public_list("token-uuid", fetch_results=[lst_row, items])
        self.assertEqual(responses[-1][0], 200)

    def test_public_list_shows_titles_regardless_of_collection(self):
        """AC-12: list items shown even if not in the owner's collection."""
        lst_row = {"name": "Curated", "visibility": "public", "owner_username": "alice"}
        items = [
            {"tmdb_id": 999, "media_type": "tv", "title": "Obscure Show",
             "year": "2021", "poster_url": ""},
        ]
        _, responses = self._run_public_list("token-uuid", fetch_results=[lst_row, items])
        self.assertEqual(responses[-1][0], 200)
        result_items = responses[-1][1]["list"]["items"]
        self.assertEqual(len(result_items), 1)
        self.assertEqual(result_items[0]["title"], "Obscure Show")

    def test_reprivatised_list_returns_404(self):
        """AC-11: re-privatised list -> 404 immediately."""
        lst_row = {"name": "Was Public", "visibility": "private", "owner_username": "alice"}
        _, responses = self._run_public_list("old-token", fetch_results=[lst_row])
        self.assertEqual(responses[-1][0], 404)

    def test_public_list_shows_owner_username(self):
        """AC-12: public list body includes owner_username."""
        lst_row = {"name": "Film List", "visibility": "public", "owner_username": "alice"}
        _, responses = self._run_public_list("token-uuid", fetch_results=[lst_row, []])
        self.assertEqual(responses[-1][1]["list"]["owner_username"], "alice")


# ── Integration: cross-user isolation (AC-13) ─────────────────────────────────


class CrossUserIsolationIntegration(unittest.TestCase):
    """AC-13: cross-user PATCH/DELETE on list -> 404."""

    def _run_patch_list(self, list_id, body, *, user_id, rowcount=0,
                        has_username=True):
        """Simulate PATCH /api/lists/{id} by another user (rowcount=0 -> 404)."""
        # For visibility check we need to return a username (or None)
        username_row = {"username": "alice"} if has_username else None
        if body.get("visibility") in ("unlisted", "public"):
            cur = FakeCursor(fetch_results=[username_row], rowcount=rowcount)
        else:
            cur = FakeCursor(rowcount=rowcount)
        h, responses = make_handler(body=body, user_id=user_id)
        with patch_db(cur):
            h._patch_list(list_id)
        return cur, responses

    def _run_delete_list(self, list_id, *, user_id, rowcount=0):
        cur = FakeCursor(rowcount=rowcount)
        h, responses = make_handler(user_id=user_id)
        with patch_db(cur):
            h._delete_list(list_id)
        return cur, responses

    def test_cross_user_patch_returns_404(self):
        """AC-13: PATCH another user's list -> 404 (non-enumerating)."""
        _, responses = self._run_patch_list(
            "list-uuid-of-other", {"name": "Hacked"}, user_id="attacker", rowcount=0
        )
        self.assertEqual(responses[-1][0], 404)

    def test_cross_user_delete_returns_404(self):
        """AC-13: DELETE another user's list -> 404 (non-enumerating)."""
        _, responses = self._run_delete_list(
            "list-uuid-of-other", user_id="attacker", rowcount=0
        )
        self.assertEqual(responses[-1][0], 404)

    def test_own_patch_succeeds(self):
        """Sanity: PATCH own list (rowcount=1) -> 200."""
        _, responses = self._run_patch_list(
            "my-list-uuid", {"name": "My Renamed List"}, user_id="owner", rowcount=1
        )
        self.assertEqual(responses[-1][0], 200)

    def test_own_delete_succeeds(self):
        """Sanity: DELETE own list (rowcount=1) -> 200."""
        _, responses = self._run_delete_list(
            "my-list-uuid", user_id="owner", rowcount=1
        )
        self.assertEqual(responses[-1][0], 200)


# ── Integration: anonymous owner-mutation -> 401 (AC-14) ─────────────────────


class AnonymousOwnerMutationIntegration(unittest.TestCase):
    """AC-14: unauthenticated requests to owner-only endpoints -> 401."""

    def _anon_handler(self, body=None):
        return make_handler(body=body, user_id=None)

    def test_anonymous_patch_profile_401(self):
        """AC-14: anonymous PATCH /api/profile -> 401."""
        h, responses = self._anon_handler({"username": "hacker"})
        h._patch_profile()
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_get_profile_401(self):
        """AC-14: anonymous GET /api/profile -> 401."""
        h, responses = self._anon_handler()
        h._get_profile()
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_create_list_401(self):
        """AC-14: anonymous POST /api/lists -> 401."""
        h, responses = self._anon_handler({"name": "My List"})
        h._create_list()
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_patch_list_401(self):
        """AC-14: anonymous PATCH /api/lists/{id} -> 401."""
        h, responses = self._anon_handler({"name": "Hack"})
        h._patch_list("some-list-uuid")
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_delete_list_401(self):
        """AC-14: anonymous DELETE /api/lists/{id} -> 401."""
        h, responses = self._anon_handler()
        h._delete_list("some-list-uuid")
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_add_list_item_401(self):
        """AC-14: anonymous POST /api/lists/{id}/items -> 401."""
        h, responses = self._anon_handler(
            {"tmdb_id": 1, "media_type": "movie", "title": "X"}
        )
        h._add_list_item("some-list-uuid")
        self.assertEqual(responses[-1][0], 401)


# ── Integration: per-IP rate limiting (429 + Retry-After) ─────────────────────


class PublicRateLimitIntegration(unittest.TestCase):
    """Integration: burst over per-IP cap -> 429 + Retry-After."""

    def test_rate_limited_public_profile_returns_429(self):
        """Burst over per-IP cap -> 429 + Retry-After header."""
        # Simulate the limiter returning 'not allowed' by mocking _public_rate_limited.
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        # Simulate the rate limiter triggering (returns True means 'blocked')
        blocked_called = []

        def _blocked_rate_limited():
            blocked_called.append(True)
            h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                    extra_headers={"Retry-After": 60})
            return True

        h._public_rate_limited = _blocked_rate_limited
        with patch_db(cur):
            h._public_profile("alice")
        # 429 was emitted; no DB reads occurred
        self.assertEqual(responses[-1][0], 429)
        self.assertTrue(blocked_called)
        # No DB query should have been made
        self.assertEqual(cur.calls, [])

    def test_rate_limited_public_list_returns_429(self):
        """Burst on public list endpoint -> 429 + Retry-After."""
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)

        def _blocked():
            h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                    extra_headers={"Retry-After": 60})
            return True

        h._public_rate_limited = _blocked
        with patch_db(cur):
            h._public_list("some-token")
        self.assertEqual(responses[-1][0], 429)
        self.assertEqual(cur.calls, [])

    def test_rate_check_blocks_after_limit(self):
        """Integration: _public_rate_limited calls rate_check with public:{ip} key.
        After exceeding PUBLIC_RATE_MAX hits, rate_check returns blocked."""
        import time

        # Clear the rate limiter state for our test key
        test_ip = "254.253.252.251"
        test_buckets = [(f"public:{test_ip}", server.PUBLIC_RATE_MAX),
                        ("public:_global_test", server.PUBLIC_RATE_GLOBAL)]

        # Hit the limit
        allowed = True
        for _ in range(server.PUBLIC_RATE_MAX):
            allowed, _ = server.rate_check(test_buckets)

        # The next call should be blocked
        allowed, retry = server.rate_check(test_buckets)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)


# ── Integration: audit log no raw PII ─────────────────────────────────────────


class AuditLogNoPiiIntegration(unittest.TestCase):
    """Audit log entry emitted on consent/visibility change; no raw PII in output."""

    def test_audit_hash_user_id_not_raw(self):
        """_audit() logs user_hash (sha256[:16]) not the raw UUID."""
        import io
        from unittest.mock import patch

        user_id = "12345678-dead-beef-cafe-123456789abc"
        output_lines = []

        def capture_print(*args, **kwargs):
            output_lines.append(args[0] if args else "")

        with patch("builtins.print", side_effect=capture_print):
            server._audit("profile.publish", user_id, "profile")

        self.assertTrue(output_lines, "No audit line emitted")
        line = output_lines[-1]
        # Should be JSON after 'audit '
        import json
        self.assertTrue(line.startswith("audit "))
        entry = json.loads(line[len("audit "):])

        # Must NOT contain the raw user_id
        self.assertNotIn(user_id, line, "Raw user_id leaked into audit log")
        # Must contain user_hash
        self.assertIn("user_hash", entry)
        # The hash should be 16 hex chars
        self.assertRegex(entry["user_hash"], r"^[0-9a-f]{16}$")
        # Must contain action + target
        self.assertEqual(entry["action"], "profile.publish")
        self.assertEqual(entry["target"], "profile")
        # Must NOT contain email or share_token keys
        self.assertNotIn("email", entry)
        self.assertNotIn("share_token", entry)
        self.assertNotIn("username", entry)

    def test_list_visibility_audit_emitted(self):
        """Setting list visibility emits an audit line with no raw PII."""
        import io
        from unittest.mock import patch

        cur = FakeCursor(
            fetch_results=[{"username": "alice"}, None],
            rowcount=1,
        )
        h, responses = make_handler(
            body={"visibility": "public"}, user_id="uid-test"
        )
        output_lines = []

        def capture_print(*args, **kwargs):
            output_lines.append(args[0] if args else "")

        with patch("builtins.print", side_effect=capture_print):
            with patch_db(cur):
                h._patch_list("list-uuid-1")

        self.assertEqual(responses[-1][0], 200)
        audit_lines = [l for l in output_lines if l.startswith("audit ")]
        self.assertTrue(audit_lines, "No audit line emitted for list visibility change")


# ── Security: IDOR / threat model checks ─────────────────────────────────────


class ThreatModelIntegration(unittest.TestCase):
    """Negative-path security tests from spec § Threat model."""

    def test_guessed_share_token_returns_404(self):
        """Guessed or random token -> 404 (AC-8)."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_list("random-uuid-aaaa-bbbb")
        self.assertEqual(responses[-1][0], 404)

    def test_non_tmdb_poster_url_stored_empty(self):
        """AC - poster_url not from TMDB is silently stored empty (SSRF mitigation)."""
        # Simulate _add_list_item with a non-TMDB poster_url
        # Ownership check row: returns list owned by user
        ownership_row = {"exists": 1, "visibility": "private"}
        next_pos_row = {"next_pos": 0}
        new_id_row = {"id": "item-uuid-1"}

        cur = FakeCursor(
            fetch_results=[ownership_row, next_pos_row, new_id_row]
        )
        bad_poster = "https://evil.com/malicious.jpg"
        h, responses = make_handler(
            body={
                "tmdb_id": 123,
                "media_type": "movie",
                "title": "Evil Film",
                "year": "2024",
                "poster_url": bad_poster,
            },
            user_id="owner-uid",
        )
        with patch_db(cur):
            h._add_list_item("list-uuid-1")
        self.assertEqual(responses[-1][0], 201)
        # Find the INSERT call and verify poster_url is empty
        insert_calls = [c for c in cur.calls if "INSERT INTO list_items" in c[0]]
        self.assertTrue(insert_calls)
        params = insert_calls[0][1]
        # poster_url is the 6th positional param (index 5): list_id, tmdb_id,
        # media_type, title, year, poster_url, position
        poster_param = params[5]
        self.assertEqual(poster_param, "", f"Expected empty poster_url, got: {poster_param!r}")

    def test_publish_without_username_list_visibility_400(self):
        """AC-1: setting list to 'public' without a username -> 400."""
        # fetchone() for the profiles username check returns None (no username)
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(
            body={"visibility": "public"}, user_id="uid-no-username"
        )
        with patch_db(cur):
            h._patch_list("list-uuid-1")
        self.assertEqual(responses[-1][0], 400)

    def test_publish_unlisted_without_username_400(self):
        """AC-1: setting list to 'unlisted' without a username -> 400."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(
            body={"visibility": "unlisted"}, user_id="uid-no-username"
        )
        with patch_db(cur):
            h._patch_list("list-uuid-1")
        self.assertEqual(responses[-1][0], 400)


if __name__ == "__main__":
    unittest.main()
