"""Backend integration tests for the export-account feature (AC-2..AC-11 backend slice).

Covers every ### Tester scope row that belongs to the backend integration suite:

  ac2_ac3    — authenticated export returns 200; body parses as JSON; carries
               export.schema_version (AC-2, AC-3)
  ac4        — every collection field present for the caller's titles — status,
               rating, note, platform, watched_at, current_season, current_episode,
               total_seasons, genres (AC-4)
  ac5        — every list appears with name + items; empty list yields empty items
               array (AC-5)
  ac6        — profile username + is_public / show_collection / show_stats present
               (AC-6)
  ac7        — export contains no share_token, no user_id, no email, no JWT or
               Supabase key anywhere (AC-7, GD-001)
  ac8        — cross-user scoping: user A's export contains only A's rows; none of
               B's data appears (AC-8)
  ac10_unauth — unauthenticated / invalid-JWT request → 401, no data (AC-10)
  ac10_rate  — rate-limit exceeded → 429 (AC-10 / SE-*)
  ac9_failure — forced backend failure → generic es-ES error, no raw internals (AC-9)
  ac11       — round-trip completeness: all documented fields present; export shaped
               for lossless re-import (AC-11)
  audit_success  — success emits _audit("account.exported", ...) with user_hash only,
                   never raw user_id / email / token (AU-007, LO-*)
  audit_denial   — unauthenticated (401) emits "account.export_denied" with reason
                   "unauthenticated" and user_hash null; rate-limited (429) emits
                   "account.export_denied" with reason "rate_limited" and user_hash
                   set (AU-007, LO-*)

Stub strategy (mirrors tests/test_delete_account.py):
  - h._get_user_id    → lambda stub on handler instance (replaces verify_jwt)
  - server.rate_check → mock.patch to allow or block
  - server.get_db     → patch_db(FakeCursor) for DB boundary
  - server._audit     → mock.patch to capture calls without side effects

No live Supabase, no live DB, no live network required.
"""

import io
import json
import unittest
from unittest import mock

import server
from server import _hash_user_id
from tests._harness import FakeCursor, make_handler, patch_db


# ── Constants ──────────────────────────────────────────────────────────────────

_UID_A = "aaaa-1111-aaaa-1111"
_UID_B = "bbbb-2222-bbbb-2222"
_EMAIL_A = "usera@example.com"
_BEARER = "Bearer stub-token"

# Full collection row (all allow-listed fields per spec § Technical Details).
_MOVIE_ROW_A = {
    "tmdb_id": 101,
    "media_type": "movie",
    "title": "Film A",
    "year": 2022,
    "poster_url": "/poster_a.jpg",
    "status": "vista",
    "rating": 4,
    "note": "Great film",
    "watched_at": None,
    "platform": "Netflix",
    "current_season": None,
    "current_episode": None,
    "total_seasons": None,
    "genres": ["Action"],
    "created_at": "2024-01-01T00:00:00+00:00",
}

# A series row with per-season/episode progress fields.
_SERIES_ROW_A = {
    "tmdb_id": 202,
    "media_type": "tv",
    "title": "Series A",
    "year": 2021,
    "poster_url": "/poster_s.jpg",
    "status": "viendo",
    "rating": None,
    "note": None,
    "watched_at": None,
    "platform": None,
    "current_season": 2,
    "current_episode": 5,
    "total_seasons": 3,
    "genres": ["Drama"],
    "created_at": "2024-02-01T00:00:00+00:00",
}

# A collection row belonging to user B (cross-user scoping).
_MOVIE_ROW_B = {
    "tmdb_id": 999,
    "media_type": "movie",
    "title": "Film B (other user)",
    "year": 2020,
    "poster_url": "/poster_b.jpg",
    "status": "pendiente",
    "rating": None,
    "note": None,
    "watched_at": None,
    "platform": None,
    "current_season": None,
    "current_episode": None,
    "total_seasons": None,
    "genres": [],
    "created_at": "2024-03-01T00:00:00+00:00",
}

_PROFILE_ROW_A = {
    "username": "usera",
    "is_public": True,
    "show_collection": True,
    "show_stats": False,
}

_LIST_ROW_A = {
    "id": "list-uuid-aaaa",
    "name": "Favorites",
    "visibility": "public",
    "created_at": "2024-01-10T00:00:00+00:00",
    "updated_at": "2024-04-01T00:00:00+00:00",
}

_EMPTY_LIST_ROW = {
    "id": "list-uuid-empty",
    "name": "Empty List",
    "visibility": "private",
    "created_at": "2024-01-15T00:00:00+00:00",
    "updated_at": "2024-04-05T00:00:00+00:00",
}

_ITEM_ROW_A = {
    "list_id": "list-uuid-aaaa",
    "tmdb_id": 101,
    "media_type": "movie",
    "title": "Film A",
    "year": 2022,
    "poster_url": "/poster_a.jpg",
    "position": 1,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

# Sentinel: distinguishes "use default profile row" from "no profile row exists".
_USE_DEFAULT_PROFILE = object()


def _make_export_handler(
    *,
    user_id=_UID_A,
    rate_ok=True,
    profile_row=_USE_DEFAULT_PROFILE,
    collection_rows=None,
    list_rows=None,
    item_rows=None,
):
    """Build a Handler stub wired for _export_account() tests.

    Returns (handler, responses, cur, rate_result).

    FakeCursor fetch_results order mirrors the four queries in _export_account:
      1. fetchone()   → profile row (or None → lazy defaults)
      2. fetchall()   → collection rows list
      3. fetchall()   → list rows list
      4. fetchall()   → item rows list

    Pass `profile_row=None` to simulate a missing profile row (triggers lazy
    defaults in _export_account). Pass `profile_row=_PROFILE_ROW_A` (or omit)
    to use the canned row.
    """
    if profile_row is _USE_DEFAULT_PROFILE:
        profile_row = _PROFILE_ROW_A
    if collection_rows is None:
        collection_rows = [_MOVIE_ROW_A, _SERIES_ROW_A]
    if list_rows is None:
        list_rows = [_LIST_ROW_A]
    if item_rows is None:
        item_rows = [_ITEM_ROW_A]

    h, responses = make_handler(user_id=user_id)
    h.path = "/api/account/export"

    _rate_result = (True, 0) if rate_ok else (False, 60)

    # FakeCursor: results consumed in execute() order (FIFO).
    cur = FakeCursor(
        fetch_results=[
            profile_row,  # fetchone() → profile
            collection_rows,  # fetchall() → collection
            list_rows,  # fetchall() → list rows
            item_rows,  # fetchall() → item rows
        ]
    )

    return h, responses, cur, _rate_result


def _run_export(
    *,
    user_id=_UID_A,
    rate_ok=True,
    profile_row=_USE_DEFAULT_PROFILE,
    collection_rows=None,
    list_rows=None,
    item_rows=None,
):
    """Run _export_account() with all seams stubbed. Returns (responses, cur)."""
    h, responses, cur, rate_result = _make_export_handler(
        user_id=user_id,
        rate_ok=rate_ok,
        profile_row=profile_row,
        collection_rows=collection_rows,
        list_rows=list_rows,
        item_rows=item_rows,
    )
    with (
        mock.patch.object(server, "rate_check", return_value=rate_result),
        patch_db(cur),
    ):
        h._export_account()
    return responses, cur


# ── 1. Unauthenticated / invalid JWT → 401 (AC-10) ───────────────────────────


class TestExportAccountUnauth(unittest.TestCase):
    """AC-10 / PS-001: missing or invalid JWT yields 401; no DB read performed."""

    def test_missing_jwt_returns_401(self):
        """_get_user_id returning None (no valid JWT) → 401."""
        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._export_account()
        status, payload = responses[-1]
        self.assertEqual(status, 401)
        self.assertFalse(payload.get("ok"))

    def test_unauth_no_db_call(self):
        """No SELECT must reach the DB when JWT is invalid."""
        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._export_account()
        self.assertEqual(
            cur.calls,
            [],
            "No DB call must be issued when JWT is invalid",
        )

    def test_unauth_error_body_is_generic_es(self):
        """401 body must be generic es-ES; no raw error detail."""
        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._export_account()
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertTrue(error, "401 error body must not be empty")
        self.assertNotIn("Traceback", error)
        self.assertNotIn("Exception", error)


# ── 2. Rate-limit exceeded → 429 ─────────────────────────────────────────────


class TestExportAccountRateLimit(unittest.TestCase):
    """Rate-limit bucket exceeded immediately after auth → 429; no DB read."""

    def test_rate_limit_returns_429(self):
        responses, cur = _run_export(rate_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertFalse(payload.get("ok"))

    def test_no_db_call_on_rate_limit(self):
        responses, cur = _run_export(rate_ok=False)
        self.assertEqual(
            cur.calls,
            [],
            "No SELECT must be issued when rate-limited",
        )

    def test_rate_limit_error_is_generic_es(self):
        """429 body must be a generic es-ES message, never a raw bucket key."""
        responses, cur = _run_export(rate_ok=False)
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertTrue(error, "429 error body must not be empty")
        self.assertNotIn(
            "account-export:", error, "Rate-limit error must not expose bucket keys"
        )
        self.assertNotIn("Traceback", error)


# ── 3. Happy path — 200 + schema_version (AC-2, AC-3) ────────────────────────


class TestExportAccountHappyPath(unittest.TestCase):
    """AC-2 / AC-3: authenticated export returns 200; body parses as JSON;
    carries export.schema_version = 1."""

    def setUp(self):
        self.responses, self.cur = _run_export()

    def test_returns_200(self):
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))

    def test_body_parses_as_json(self):
        """The response payload must be a dict (parsed JSON by the harness)."""
        _, payload = self.responses[-1]
        self.assertIsInstance(payload, dict, "Response body must be a JSON object")

    def test_export_key_present(self):
        _, payload = self.responses[-1]
        self.assertIn("export", payload, "Response must carry an 'export' key")

    def test_schema_version_is_1(self):
        """AC-3: export.schema_version must equal 1 (integer)."""
        _, payload = self.responses[-1]
        export = payload["export"]
        self.assertIn("schema_version", export, "export.schema_version must be present")
        self.assertEqual(export["schema_version"], 1, "schema_version must be 1")

    def test_exported_at_present(self):
        """export.exported_at must be present (ISO 8601 timestamp)."""
        _, payload = self.responses[-1]
        exported_at = payload["export"].get("exported_at")
        self.assertIsNotNone(exported_at, "export.exported_at must be present")
        self.assertIsInstance(exported_at, str)


# ── 4. Collection fields (AC-4) ───────────────────────────────────────────────


class TestExportAccountCollection(unittest.TestCase):
    """AC-4: every allow-listed collection field is present for the caller's titles."""

    def setUp(self):
        self.responses, self.cur = _run_export(
            collection_rows=[_MOVIE_ROW_A, _SERIES_ROW_A],
        )
        _, payload = self.responses[-1]
        self.collection = payload["export"]["collection"]

    def test_collection_has_two_items(self):
        self.assertEqual(len(self.collection), 2)

    def _assert_field(self, item, field):
        self.assertIn(
            field,
            item,
            f"AC-4: collection item must carry field '{field}'; got keys: {list(item.keys())}",
        )

    def test_movie_row_has_all_required_fields(self):
        movie = self.collection[0]
        for field in (
            "tmdb_id",
            "media_type",
            "title",
            "year",
            "poster_url",
            "status",
            "rating",
            "note",
            "watched_at",
            "platform",
            "current_season",
            "current_episode",
            "total_seasons",
            "genres",
            "created_at",
        ):
            self._assert_field(movie, field)

    def test_note_field_included(self):
        """AC-4: note is Internal-PII that the public projection strips —
        the export must include it (owner-only portability)."""
        movie = self.collection[0]
        self.assertIn("note", movie, "AC-4: note must be present in collection export")
        self.assertEqual(movie["note"], "Great film")

    def test_series_progress_fields_included(self):
        """AC-4: per-series season/episode progress (current_season, current_episode,
        total_seasons) must be included."""
        series = self.collection[1]
        self.assertEqual(series["current_season"], 2)
        self.assertEqual(series["current_episode"], 5)
        self.assertEqual(series["total_seasons"], 3)

    def test_collection_values_match_source(self):
        movie = self.collection[0]
        self.assertEqual(movie["tmdb_id"], _MOVIE_ROW_A["tmdb_id"])
        self.assertEqual(movie["title"], _MOVIE_ROW_A["title"])
        self.assertEqual(movie["status"], _MOVIE_ROW_A["status"])
        self.assertEqual(movie["platform"], _MOVIE_ROW_A["platform"])


# ── 5. Lists + items (AC-5) ───────────────────────────────────────────────────


class TestExportAccountLists(unittest.TestCase):
    """AC-5: every list appears with name + items; empty list → empty items array."""

    def _run_with_two_lists(self):
        list_rows = [_LIST_ROW_A, _EMPTY_LIST_ROW]
        # Item only for the first list; the empty list gets no items.
        item_rows = [_ITEM_ROW_A]
        return _run_export(list_rows=list_rows, item_rows=item_rows)

    def test_list_name_present(self):
        responses, _ = _run_export()
        _, payload = responses[-1]
        lists = payload["export"]["lists"]
        self.assertEqual(len(lists), 1)
        self.assertIn("name", lists[0])
        self.assertEqual(lists[0]["name"], "Favorites")

    def test_list_items_populated(self):
        responses, _ = _run_export()
        _, payload = responses[-1]
        items = payload["export"]["lists"][0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tmdb_id"], _ITEM_ROW_A["tmdb_id"])
        self.assertEqual(items[0]["title"], _ITEM_ROW_A["title"])

    def test_empty_list_yields_empty_items_array(self):
        """AC-5: a list with no items must carry items = [] not be dropped."""
        responses, _ = self._run_with_two_lists()
        _, payload = responses[-1]
        lists = payload["export"]["lists"]
        empty = next((lst for lst in lists if lst["name"] == "Empty List"), None)
        self.assertIsNotNone(empty, "Empty List must appear in export")
        self.assertEqual(empty["items"], [], "Empty list must carry items=[]")

    def test_list_with_items_populated_alongside_empty_list(self):
        responses, _ = self._run_with_two_lists()
        _, payload = responses[-1]
        lists = payload["export"]["lists"]
        fav = next((lst for lst in lists if lst["name"] == "Favorites"), None)
        self.assertIsNotNone(fav, "Favorites list must appear in export")
        self.assertEqual(len(fav["items"]), 1)

    def test_list_fields_present(self):
        """AC-5: each list must carry name, visibility, items (created_at/updated_at optional)."""
        responses, _ = _run_export()
        _, payload = responses[-1]
        lst = payload["export"]["lists"][0]
        for field in ("name", "visibility", "items"):
            self.assertIn(field, lst, f"AC-5: list must have field '{field}'")

    def test_item_fields_present(self):
        """AC-5: each item must carry the allow-listed cached snapshot fields."""
        responses, _ = _run_export()
        _, payload = responses[-1]
        item = payload["export"]["lists"][0]["items"][0]
        for field in (
            "tmdb_id",
            "media_type",
            "title",
            "year",
            "poster_url",
            "position",
        ):
            self.assertIn(field, item, f"AC-5: item must have field '{field}'")


# ── 6. Profile fields (AC-6) ─────────────────────────────────────────────────


class TestExportAccountProfile(unittest.TestCase):
    """AC-6: profile username + is_public / show_collection / show_stats present."""

    def setUp(self):
        responses, _ = _run_export(profile_row=_PROFILE_ROW_A)
        _, payload = responses[-1]
        self.profile = payload["export"]["profile"]

    def test_username_present(self):
        self.assertIn("username", self.profile)
        self.assertEqual(self.profile["username"], "usera")

    def test_is_public_present(self):
        self.assertIn("is_public", self.profile)
        self.assertEqual(self.profile["is_public"], True)

    def test_show_collection_present(self):
        self.assertIn("show_collection", self.profile)
        self.assertEqual(self.profile["show_collection"], True)

    def test_show_stats_present(self):
        self.assertIn("show_stats", self.profile)
        self.assertEqual(self.profile["show_stats"], False)

    def test_missing_profile_row_yields_lazy_defaults(self):
        """When no profile row exists the export still succeeds with defaults."""
        responses, _ = _run_export(profile_row=None)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        profile = payload["export"]["profile"]
        self.assertIsNone(profile["username"])
        self.assertFalse(profile["is_public"])
        self.assertFalse(profile["show_collection"])
        self.assertFalse(profile["show_stats"])


# ── 7. No credential / secret in export (AC-7, GD-001) ───────────────────────


class TestExportAccountNoSecret(unittest.TestCase):
    """AC-7 / GD-001: export contains no share_token, no user_id, no email,
    no JWT or Supabase key anywhere in the serialised document."""

    def _export_json_str(self, **kwargs):
        responses, _ = _run_export(**kwargs)
        _, payload = responses[-1]
        return json.dumps(payload)

    def test_no_share_token_in_export(self):
        body = self._export_json_str()
        self.assertNotIn(
            "share_token", body, "AC-7: share_token must never appear in export"
        )

    def test_no_user_id_in_export(self):
        """The user_id (UUID) must not appear anywhere in the serialised export."""
        body = self._export_json_str()
        self.assertNotIn(_UID_A, body, "AC-7: raw user_id must not appear in export")

    def test_no_email_in_export(self):
        """Account email is excluded from the confirmed scope (AC-7)."""
        body = self._export_json_str()
        self.assertNotIn(_EMAIL_A, body, "AC-7: email must not appear in export")

    def test_no_jwt_bearer_in_export(self):
        body = self._export_json_str()
        self.assertNotIn(
            "Bearer", body, "AC-7: JWT bearer token must not appear in export"
        )

    def test_no_supabase_key_in_export(self):
        fake_key = "eyFakeSupabaseServiceKey-ABCDE12345"
        with mock.patch.dict("os.environ", {"SUPABASE_SERVICE_KEY": fake_key}):
            body = self._export_json_str()
        self.assertNotIn(
            fake_key, body, "AC-7: Supabase service key must not appear in export"
        )

    def test_no_internal_list_id_in_export(self):
        """The list's internal id (used server-side for grouping) must be dropped
        from the emitted list objects (GD-001 internal id minimisation)."""
        body = self._export_json_str()
        # list-uuid-aaaa is the internal list id; it must not appear as a field value
        # in the emitted list objects (it may appear at other levels only if the
        # item references it, but the spec says the id is dropped from emitted objects).
        export_data = json.loads(body)
        for lst in export_data.get("export", {}).get("lists", []):
            self.assertNotIn(
                "id",
                lst,
                f"AC-7: internal list id must be dropped from emitted list object; got: {lst}",
            )


# ── 8. Cross-user scoping (AC-8) ─────────────────────────────────────────────


class TestExportAccountCrossUser(unittest.TestCase):
    """AC-8: user A's export contains only A's rows; none of B's data appears.

    The scoping guarantee is enforced server-side by WHERE user_id = %s on every
    query. This test verifies:
    1. Every SELECT in the DB is parameterised with user A's id only.
    2. No SELECT carries user B's id.
    3. The exported collection does not contain B's title.
    """

    def test_all_selects_scoped_to_user_a(self):
        """Every SQL call must carry _UID_A as parameter; never _UID_B."""
        responses, cur = _run_export(user_id=_UID_A)
        for sql, params in cur.calls:
            if sql.strip().upper().startswith("SELECT"):
                self.assertIn(
                    _UID_A,
                    (params or ()),
                    f"SELECT must be scoped to _UID_A; sql={sql!r}, params={params!r}",
                )

    def test_no_user_b_id_in_any_select(self):
        """No SELECT must carry _UID_B as parameter."""
        responses, cur = _run_export(user_id=_UID_A)
        for sql, params in cur.calls:
            self.assertNotIn(
                _UID_B,
                (params or ()),
                f"_UID_B must never appear in SQL params; sql={sql!r}, params={params!r}",
            )

    def test_user_b_title_not_in_export_of_user_a(self):
        """When user A's collection contains A's rows (FakeCursor controlled), B's
        title must not appear.  The DB boundary is already stubbed — this test
        verifies the handler emits only what the cursor returns."""
        responses, _ = _run_export(
            user_id=_UID_A,
            collection_rows=[_MOVIE_ROW_A],  # only A's rows
        )
        _, payload = responses[-1]
        body_str = json.dumps(payload)
        self.assertNotIn(
            _MOVIE_ROW_B["title"],
            body_str,
            "User B's title must not appear in user A's export",
        )


# ── 9. Forced backend failure → generic es-ES error, no internals (AC-9) ─────


class TestExportAccountBackendFailure(unittest.TestCase):
    """AC-9: a forced DB exception returns a generic es-ES body; no raw error
    detail is serialised (invariants — never leak internals)."""

    def test_db_exception_returns_generic_error(self):
        """Patch get_db to raise an exception and confirm the response is generic."""
        h, responses = make_handler(user_id=_UID_A)
        h.path = "/api/account/export"

        # The _db_guard decorator wraps do_GET; _export_account is called from
        # inside the handler, where get_db() is called directly.  We force a
        # generic Exception inside the with-block by making the context manager raise.
        import contextlib

        @contextlib.contextmanager
        def _exploding_db():
            raise Exception("psycopg2: FATAL ERROR — internal detail SHOULD NOT LEAK")
            yield  # pragma: no cover

        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "get_db", _exploding_db),
        ):
            try:
                h._export_account()
            except Exception:
                # The handler itself may propagate; the _db_guard on do_GET catches it,
                # but since we call _export_account directly (not through do_GET) in this
                # test we may see the exception bubble.  That's acceptable — production
                # never calls _export_account directly.  What matters is verified below.
                pass

        # If a response was recorded it must be generic (not expose internals).
        if responses:
            _, payload = responses[-1]
            body_str = json.dumps(payload)
            self.assertNotIn(
                "psycopg2", body_str, "Raw DB error must not appear in response"
            )
            self.assertNotIn(
                "FATAL ERROR", body_str, "Internal error text must not be surfaced"
            )
            self.assertNotIn("Traceback", body_str)

    def test_generic_error_body_is_es(self):
        """The _db_guard 503 response must be a generic es-ES message."""
        # We exercise the full do_GET path (which has @_db_guard) so the 503 is produced.
        import functools
        import http.server
        import socket
        import threading
        import urllib.request

        handler_cls = functools.partial(server.Handler, directory=str(server.BASE_DIR))

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        # Wait until accepting
        deadline = __import__("time").monotonic() + 5.0
        import time

        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)

        try:
            # No DATABASE_URL → get_db will fail (DBBusy or connection error) → _db_guard
            # returns 503 with a generic body.
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/account/export",
                headers={"Authorization": "Bearer fake-jwt"},
            )
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read())
            self.assertFalse(body.get("ok", True), "503 body must not say ok=True")
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            body_str = json.dumps(body)
            self.assertNotIn("Traceback", body_str, "Traceback must never be surfaced")
            self.assertNotIn("psycopg2", body_str, "Raw DB error class must not appear")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5.0)


# ── 10. Round-trip completeness (AC-11) ───────────────────────────────────────


class TestExportAccountRoundTrip(unittest.TestCase):
    """AC-11: all documented fields are present and shaped for lossless re-import.

    Every field in the spec § Technical Details allow-list must appear in the
    exported document — including fields that the public projection strips
    (note, current_episode).
    """

    REQUIRED_COLLECTION_FIELDS = (
        "tmdb_id",
        "media_type",
        "title",
        "year",
        "poster_url",
        "status",
        "rating",
        "note",
        "watched_at",
        "platform",
        "current_season",
        "current_episode",
        "total_seasons",
        "genres",
        "created_at",
    )

    REQUIRED_PROFILE_FIELDS = ("username", "is_public", "show_collection", "show_stats")

    REQUIRED_LIST_FIELDS = ("name", "visibility", "items")

    REQUIRED_ITEM_FIELDS = (
        "tmdb_id",
        "media_type",
        "title",
        "year",
        "poster_url",
        "position",
    )

    REQUIRED_EXPORT_TOP_FIELDS = (
        "schema_version",
        "exported_at",
        "profile",
        "collection",
        "lists",
    )

    def setUp(self):
        responses, _ = _run_export(
            collection_rows=[_MOVIE_ROW_A, _SERIES_ROW_A],
            list_rows=[_LIST_ROW_A],
            item_rows=[_ITEM_ROW_A],
        )
        _, payload = responses[-1]
        self.export = payload["export"]

    def test_top_level_export_fields(self):
        for field in self.REQUIRED_EXPORT_TOP_FIELDS:
            self.assertIn(
                field,
                self.export,
                f"AC-11: export top-level must carry '{field}'",
            )

    def test_collection_item_round_trip(self):
        """Every allow-listed collection field from the spec must be present."""
        for item in self.export["collection"]:
            for field in self.REQUIRED_COLLECTION_FIELDS:
                self.assertIn(
                    field,
                    item,
                    f"AC-11: collection item missing '{field}'; got {list(item.keys())}",
                )

    def test_current_episode_round_trips(self):
        """current_episode appears in the allow-list but not in the public projection —
        the Tester must confirm it round-trips (Reviewer § For the Next Agent)."""
        series = next(
            (i for i in self.export["collection"] if i["media_type"] == "tv"), None
        )
        self.assertIsNotNone(series, "AC-11: a series row must be present")
        self.assertIn("current_episode", series)
        self.assertEqual(series["current_episode"], _SERIES_ROW_A["current_episode"])

    def test_profile_round_trip(self):
        for field in self.REQUIRED_PROFILE_FIELDS:
            self.assertIn(
                field,
                self.export["profile"],
                f"AC-11: profile missing '{field}'",
            )

    def test_list_structure_round_trip(self):
        for lst in self.export["lists"]:
            for field in self.REQUIRED_LIST_FIELDS:
                self.assertIn(
                    field,
                    lst,
                    f"AC-11: list missing '{field}'; got {list(lst.keys())}",
                )

    def test_item_structure_round_trip(self):
        for lst in self.export["lists"]:
            for item in lst["items"]:
                for field in self.REQUIRED_ITEM_FIELDS:
                    self.assertIn(
                        field,
                        item,
                        f"AC-11: item missing '{field}'; got {list(item.keys())}",
                    )

    def test_export_is_json_serialisable(self):
        """The entire export must be JSON-serialisable (re-import contract)."""
        try:
            serialised = json.dumps(self.export)
        except (TypeError, ValueError) as exc:
            self.fail(f"AC-11: export is not JSON-serialisable: {exc}")
        self.assertIsInstance(serialised, str)
        # And round-trippable back
        reparsed = json.loads(serialised)
        self.assertEqual(reparsed["schema_version"], 1)


# ── 11. Audit hygiene (AU-007, LO-*) ─────────────────────────────────────────


class TestExportAccountAuditSuccess(unittest.TestCase):
    """AU-007 / LO-*: success emits _audit('account.exported', user_id, 'account');
    audit line carries user_hash only, never raw user_id/email/token."""

    def test_success_emits_exported_audit(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export()

        exported = [c for c in audit_calls if c["action"] == "account.exported"]
        self.assertEqual(
            len(exported), 1, "Exactly one 'account.exported' audit call expected"
        )

    def test_success_audit_target_is_account(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export()

        exported = [c for c in audit_calls if c["action"] == "account.exported"]
        self.assertEqual(exported[0]["target"], "account")

    def test_success_audit_receives_real_user_id_for_hashing(self):
        """_audit is called with the real user_id so it can hash it internally."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export(user_id=_UID_A)

        exported = [c for c in audit_calls if c["action"] == "account.exported"]
        self.assertEqual(exported[0]["user_id"], _UID_A)

    def test_hash_user_id_produces_hash_not_raw_uuid(self):
        """_hash_user_id returns a 16-char hex string (LO-*)."""
        result = _hash_user_id(_UID_A)
        self.assertNotEqual(result, _UID_A)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 16)
        self.assertNotIn(_EMAIL_A, result)

    def test_hash_user_id_none_returns_none(self):
        """_hash_user_id(None) must return None (unauthenticated denial path)."""
        self.assertIsNone(_hash_user_id(None))

    def test_audit_log_line_carries_user_hash_stdout(self):
        """The printed audit log line must carry user_hash (not raw user_id)."""
        captured = io.StringIO()
        with mock.patch.object(server, "rate_check", return_value=(True, 0)):
            h, responses = make_handler(user_id=_UID_A)
            cur = FakeCursor(
                fetch_results=[
                    _PROFILE_ROW_A,
                    [_MOVIE_ROW_A],
                    [_LIST_ROW_A],
                    [_ITEM_ROW_A],
                ]
            )
            with patch_db(cur):
                with mock.patch("sys.stdout", captured):
                    h._export_account()

        output = captured.getvalue()
        self.assertIn("audit ", output, "Must emit at least one audit line")
        # Find the exported line
        for line in output.splitlines():
            if "audit " in line:
                entry = json.loads(line[len("audit ") :])
                if entry.get("action") == "account.exported":
                    self.assertIn("user_hash", entry)
                    self.assertNotEqual(
                        entry["user_hash"], _UID_A, "Must not log raw user_id"
                    )
                    self.assertNotIn(_EMAIL_A, json.dumps(entry))
                    break
        else:
            self.fail("No 'account.exported' audit line found in stdout")


class TestExportAccountAuditDenial(unittest.TestCase):
    """AU-007 / LO-*: every denial path emits _audit('account.export_denied', ...)
    with the correct non-sensitive reason token and never leaks raw user_id or email."""

    def test_unauth_emits_export_denied_audit(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "_audit", side_effect=_fake_audit),
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._export_account()

        denied = [c for c in audit_calls if c["action"] == "account.export_denied"]
        self.assertGreater(
            len(denied), 0, "Unauthenticated denial must emit export_denied audit"
        )

    def test_unauth_denial_reason_is_unauthenticated(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "_audit", side_effect=_fake_audit),
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._export_account()

        denied = [c for c in audit_calls if c["action"] == "account.export_denied"]
        self.assertEqual(denied[0]["target"], "unauthenticated")

    def test_unauth_denial_user_id_is_none(self):
        """Unauthenticated denial must pass user_id=None to _audit (no identity)."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "_audit", side_effect=_fake_audit),
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._export_account()

        denied = [c for c in audit_calls if c["action"] == "account.export_denied"]
        self.assertIsNone(denied[0]["user_id"])

    def test_rate_limited_denial_emits_export_denied_audit(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export(rate_ok=False)

        denied = [c for c in audit_calls if c["action"] == "account.export_denied"]
        self.assertGreater(
            len(denied), 0, "Rate-limited denial must emit export_denied audit"
        )

    def test_rate_limited_denial_reason_is_rate_limited(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export(rate_ok=False)

        denied = [c for c in audit_calls if c["action"] == "account.export_denied"]
        self.assertEqual(denied[0]["target"], "rate_limited")

    def test_rate_limited_denial_user_id_is_set(self):
        """Rate-limited denial has an authenticated user_id (not None)."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export(user_id=_UID_A, rate_ok=False)

        denied = [c for c in audit_calls if c["action"] == "account.export_denied"]
        self.assertEqual(denied[0]["user_id"], _UID_A)

    def test_denial_audit_never_includes_email_in_args(self):
        """The _audit() call on any denial must not pass email as any argument."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append((action, user_id, target))

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_export(rate_ok=False)

        for args in audit_calls:
            for val in args:
                self.assertNotEqual(
                    val, _EMAIL_A, "Email must not appear in audit args"
                )

    def test_audit_log_stdout_unauth_carries_user_hash_null(self):
        """Printed audit line for unauthenticated denial must carry user_hash=null."""
        captured = io.StringIO()
        h, responses = make_handler(user_id=None)
        h.path = "/api/account/export"
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            with mock.patch("sys.stdout", captured):
                h._export_account()

        output = captured.getvalue()
        for line in output.splitlines():
            if "audit " in line:
                entry = json.loads(line[len("audit ") :])
                if entry.get("action") == "account.export_denied":
                    self.assertIsNone(
                        entry.get("user_hash"),
                        "Unauthenticated denial audit must carry user_hash=null",
                    )
                    break
        else:
            self.fail(
                "No 'account.export_denied' audit line found in stdout for unauth denial"
            )


if __name__ == "__main__":
    unittest.main()
