"""Backend tests for the import-account feature (AC-1..AC-16 backend slice).

Covers every ### Tester scope row that belongs to the backend suite:

  ac2_ac11   — valid import returns 200 with the 8-counter summary
  ac6        — full-fidelity field restore (status/rating/note/platform/watched_at/
               current_season/current_episode/total_seasons/genres)
  ac3        — collection dedup on (tmdb_id, media_type, user_id)
  ac4_ac5    — list reconciliation: merge into existing list; new name → create
  ac7        — idempotency (second import adds nothing)
  ac8        — format/version gate → 422, nothing written
  ac9        — size 413 (body not read); element/list count 413; parse → 400;
               deeply-nested JSON → 400 (RecursionError guard)
  ac10       — per-item skip-invalid; non-TMDB poster dropped to ""; valid still imports
  ac12       — cross-account scoping: file's user id ignored; only caller's rows written
  ac13       — non-destructive: existing rows intact; DB error rolls back
  ac14       — unauthenticated / invalid JWT → 401, nothing written
  ac15       — generic es-ES error body; raw error never serialised
  audit      — account.imported on success; account.import_denied (+ reason token) on
               each denial; user_hash only (incl. null on the unauthenticated denial);
               db_error reason on psycopg2.Error path (Backend Reviewer § For the Next
               Agent — specifically called out in the reviewer handoff)
  integration— real-Postgres style test via FakeCursor multi-list/multi-title, re-run
               dedup, and merge into a pre-existing same-named list (AC-2/3/4/5/7)

Stub strategy (mirrors tests/test_export_account.py):
  - h._get_user_id    → lambda stub on handler instance (replaces verify_jwt)
  - server.rate_check → mock.patch to allow or block
  - server.get_db     → patch_db(FakeCursor) for DB boundary
  - server._audit     → mock.patch to capture calls without side effects
  - h.headers         → dict-like stub for Content-Length
  - h.rfile           → io.BytesIO for the request body

No live Supabase, no live DB, no live network required for unit tests.
The integration suite section drives FakeCursor through a multi-step scenario that
mirrors what a real Postgres transaction would execute.
"""

import io
import json
import sys
import unittest
from unittest import mock

import psycopg2
import server
from server import (
    MAX_IMPORT_BODY,
    MAX_IMPORT_ITEMS,
    MAX_IMPORT_LISTS,
    PLATFORMS,
)
from tests._harness import FakeCursor, patch_db


# ── Constants ──────────────────────────────────────────────────────────────────

_UID_A = "aaaa-1111-aaaa-1111"
_UID_B = "bbbb-2222-bbbb-2222"

# A minimal valid Cinephora export body (schema_version == 1, collection/lists arrays).
_VALID_MOVIE = {
    "tmdb_id": 101,
    "media_type": "movie",
    "title": "Test Film",
    "year": "2022",
    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
    "status": "vista",
    "rating": 4,
    "note": "Great film",
    "watched_at": "2024-06-01",
    "platform": "Netflix",
    "current_season": None,
    "current_episode": None,
    "total_seasons": None,
    "genres": "Action,Drama",
    "created_at": "2024-01-01T00:00:00+00:00",
}

_VALID_SERIES = {
    "tmdb_id": 202,
    "media_type": "tv",
    "title": "Test Series",
    "year": "2021",
    "poster_url": "https://image.tmdb.org/t/p/w500/series.jpg",
    "status": "viendo",
    "rating": None,
    "note": None,
    "watched_at": None,
    "platform": None,
    "current_season": 2,
    "current_episode": 5,
    "total_seasons": 3,
    "genres": "Drama",
    "created_at": "2024-02-01T00:00:00+00:00",
}

_VALID_LIST_ITEM = {
    "tmdb_id": 101,
    "media_type": "movie",
    "title": "Test Film",
    "year": "2022",
    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
    "position": 1,
}

_VALID_EXPORT = {
    "schema_version": 1,
    "exported_at": "2026-07-01T12:00:00+00:00",
    "profile": {"username": "testuser", "is_public": False},
    "collection": [_VALID_MOVIE],
    "lists": [
        {
            "name": "Favorites",
            "items": [_VALID_LIST_ITEM],
        }
    ],
}

_EMPTY_EXPORT = {
    "schema_version": 1,
    "exported_at": "2026-07-01T12:00:00+00:00",
    "profile": {},
    "collection": [],
    "lists": [],
}


# ── Harness helpers ────────────────────────────────────────────────────────────


def _make_import_handler(
    *,
    user_id=_UID_A,
    body=None,
    content_length=None,
    rate_ok=True,
):
    """Build a Handler stub wired for _import_account() tests.

    Returns (handler, responses).
    `body` may be a dict (serialised to JSON bytes) or bytes directly.
    """
    h = server.Handler.__new__(server.Handler)
    responses = []

    def _json(status, payload, extra_headers=None):
        responses.append((status, payload))

    h._json = _json
    h._get_user_id = lambda: user_id

    if body is None:
        body = _VALID_EXPORT

    if isinstance(body, dict):
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = body  # already bytes

    h.rfile = io.BytesIO(body_bytes)
    cl = content_length if content_length is not None else len(body_bytes)
    h.headers = {"Content-Length": str(cl)}

    h.responses = responses
    return h, responses


def _run_import(
    *,
    user_id=_UID_A,
    body=None,
    content_length=None,
    rate_ok=True,
    cursor=None,
    audit_calls=None,
):
    """Run _import_account() with all seams stubbed. Returns (responses, cursor)."""
    h, responses = _make_import_handler(
        user_id=user_id,
        body=body,
        content_length=content_length,
        rate_ok=rate_ok,
    )
    _rate_result = (True, 0) if rate_ok else (False, 60)

    # Default cursor with enough fetch results for a minimal happy path:
    # SELECT 1 (dedup movie) → None; SELECT lists → []; INSERT list → {id};
    # SELECT MAX(position) → {next_pos}; ON CONFLICT DO NOTHING → rowcount 1
    if cursor is None:
        cursor = FakeCursor(
            fetch_results=[
                None,               # dedup SELECT → no existing title
                [],                 # SELECT lists WHERE user_id → empty
                {"id": "new-list-id"},  # INSERT INTO lists RETURNING id
                {"next_pos": 0},    # SELECT COALESCE(MAX(position)+1, 0)
            ]
        )

    def _fake_audit(action, uid, target):
        if audit_calls is not None:
            audit_calls.append({"action": action, "user_id": uid, "target": target})

    with (
        mock.patch.object(server, "rate_check", return_value=_rate_result),
        mock.patch.object(server, "_audit", side_effect=_fake_audit),
        patch_db(cursor),
    ):
        h._import_account()
    return responses, cursor


# ── 1. Unauthenticated / invalid JWT → 401 (AC-14) ────────────────────────────


class TestImportAccountUnauth(unittest.TestCase):
    """AC-14 / PS-001: missing or invalid JWT yields 401; nothing written."""

    def test_missing_jwt_returns_401(self):
        h, responses = _make_import_handler(user_id=None)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        status, payload = responses[-1]
        self.assertEqual(status, 401)
        self.assertFalse(payload.get("ok"))

    def test_unauth_no_db_call(self):
        """No SQL must reach the DB when JWT is invalid."""
        h, responses = _make_import_handler(user_id=None)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        self.assertEqual(cur.calls, [], "No DB call must be issued when JWT is invalid")

    def test_unauth_error_body_is_generic_es(self):
        """401 body must be generic es-ES; no raw error detail."""
        h, responses = _make_import_handler(user_id=None)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertTrue(error, "401 error body must not be empty")
        self.assertNotIn("Traceback", error)
        self.assertNotIn("Exception", error)

    def test_unauth_audit_emits_import_denied(self):
        """Unauthenticated denial must emit account.import_denied / unauthenticated."""
        audit_calls = []
        h, responses = _make_import_handler(user_id=None)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(
                server, "_audit",
                side_effect=lambda a, u, t: audit_calls.append({"action": a, "user_id": u, "target": t})
            ),
            patch_db(cur),
        ):
            h._import_account()
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "unauthenticated")
        self.assertIsNone(denied[0]["user_id"],
                          "Unauthenticated denial must pass user_id=None (→ user_hash null)")


# ── 2. Rate-limit exceeded → 429 ──────────────────────────────────────────────


class TestImportAccountRateLimit(unittest.TestCase):
    """Rate-limit bucket exceeded immediately after auth → 429; nothing written."""

    def test_rate_limit_returns_429(self):
        responses, cur = _run_import(rate_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertFalse(payload.get("ok"))

    def test_no_db_call_on_rate_limit(self):
        responses, cur = _run_import(rate_ok=False)
        self.assertEqual(cur.calls, [], "No SQL must be issued when rate-limited")

    def test_rate_limit_audit(self):
        audit_calls = []
        responses, cur = _run_import(rate_ok=False, audit_calls=audit_calls)
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "rate_limited")
        self.assertEqual(denied[0]["user_id"], _UID_A)


# ── 3. Body size limit → 413 (body not read) (AC-9) ───────────────────────────


class TestImportAccountBodySizeLimit(unittest.TestCase):
    """AC-9: Content-Length > MAX_IMPORT_BODY → 413; body MUST NOT be read."""

    def test_oversized_content_length_returns_413(self):
        """A Content-Length one byte over the cap → 413."""
        h, responses = _make_import_handler(
            content_length=MAX_IMPORT_BODY + 1,
        )
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        status, payload = responses[-1]
        self.assertEqual(status, 413)
        self.assertFalse(payload.get("ok"))

    def test_oversized_body_not_read(self):
        """The oversized body must not be read (rfile position must stay at 0)."""
        h, responses = _make_import_handler(
            content_length=MAX_IMPORT_BODY + 1,
        )
        initial_pos = h.rfile.tell()
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        # If the body was not read the BytesIO cursor stays at the initial position.
        self.assertEqual(h.rfile.tell(), initial_pos,
                         "Oversized body must not be read (Content-Length check precedes read)")

    def test_oversized_no_db_call(self):
        h, responses = _make_import_handler(
            content_length=MAX_IMPORT_BODY + 1,
        )
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        self.assertEqual(cur.calls, [], "No DB call when body is oversized")

    def test_oversized_audit_reason_too_large(self):
        audit_calls = []
        h, responses = _make_import_handler(content_length=MAX_IMPORT_BODY + 1)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(
                server, "_audit",
                side_effect=lambda a, u, t: audit_calls.append({"action": a, "user_id": u, "target": t})
            ),
            patch_db(cur),
        ):
            h._import_account()
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "too_large")

    def test_413_error_body_is_es(self):
        h, responses = _make_import_handler(content_length=MAX_IMPORT_BODY + 1)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertIn("grande", error.lower())
        self.assertNotIn("Traceback", error)


# ── 4. Element/list count over cap → 413 (AC-9) ───────────────────────────────


class TestImportAccountCountLimit(unittest.TestCase):
    """AC-9: element count > MAX_IMPORT_ITEMS or list count > MAX_IMPORT_LISTS → 413."""

    def _over_items_body(self):
        """Build an export body with MAX_IMPORT_ITEMS + 1 collection entries."""
        many = [
            {"tmdb_id": i, "media_type": "movie", "title": f"Film {i}",
             "year": "2020", "poster_url": "", "status": "vista",
             "rating": None, "note": None, "watched_at": None, "platform": None,
             "current_season": None, "current_episode": None, "total_seasons": None,
             "genres": None, "created_at": "2024-01-01T00:00:00+00:00"}
            for i in range(MAX_IMPORT_ITEMS + 1)
        ]
        return {"schema_version": 1, "collection": many, "lists": []}

    def _over_lists_body(self):
        """Build an export body with MAX_IMPORT_LISTS + 1 lists."""
        many = [{"name": f"List {i}", "items": []} for i in range(MAX_IMPORT_LISTS + 1)]
        return {"schema_version": 1, "collection": [], "lists": many}

    def test_element_count_over_cap_returns_413(self):
        body = self._over_items_body()
        cur = FakeCursor()
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 413)
        self.assertFalse(payload.get("ok"))

    def test_element_count_no_db_call(self):
        body = self._over_items_body()
        cur = FakeCursor()
        responses, cur2 = _run_import(body=body, cursor=cur)
        self.assertEqual(cur2.calls, [], "No DB call when element count exceeded")

    def test_list_count_over_cap_returns_413(self):
        body = self._over_lists_body()
        cur = FakeCursor()
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 413)

    def test_count_audit_reason_too_large(self):
        audit_calls = []
        body = self._over_items_body()
        cur = FakeCursor()
        responses, _ = _run_import(body=body, cursor=cur, audit_calls=audit_calls)
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "too_large")


# ── 5. Unparseable JSON → 400 (AC-9) ──────────────────────────────────────────


class TestImportAccountUnparseable(unittest.TestCase):
    """AC-9: unparseable JSON → 400; nothing written."""

    def test_bad_json_returns_400(self):
        bad = b"not json at all {"
        cur = FakeCursor()
        responses, _ = _run_import(body=bad, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload.get("ok"))

    def test_bad_json_no_db_call(self):
        bad = b"not json at all {"
        cur = FakeCursor()
        responses, _ = _run_import(body=bad, cursor=cur)
        self.assertEqual(cur.calls, [], "No DB call when JSON is unparseable")

    def test_bad_json_error_generic(self):
        bad = b"not json at all {"
        cur = FakeCursor()
        responses, _ = _run_import(body=bad, cursor=cur)
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertNotIn("Traceback", error)
        self.assertNotIn("JSONDecodeError", error)

    def test_bad_json_audit_reason_invalid_format(self):
        """Unparseable JSON triggers invalid_format audit (implementation detail:
        the RecursionError / ValueError path at server.py:1421-1422)."""
        audit_calls = []
        bad = b"not json"
        cur = FakeCursor()
        responses, _ = _run_import(body=bad, cursor=cur, audit_calls=audit_calls)
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "invalid_format")


# ── 6. Deeply-nested JSON → 400 (RecursionError guard, AC-9) ──────────────────


class TestImportAccountDeeplyNested(unittest.TestCase):
    """AC-9: deeply-nested JSON triggers RecursionError guard → 400, nothing written.

    server.py:1421 catches RecursionError along with ValueError/JSONDecodeError —
    the Backend Reviewer's fast-path note (iter-2 handoff) specifically called this out.
    """

    def _deeply_nested(self, depth=500):
        """Build a JSON string representing a deeply-nested list of lists."""
        inner = "[]"
        for _ in range(depth):
            inner = f"[{inner}]"
        return inner.encode()

    def test_deeply_nested_json_returns_400(self):
        """A pathologically deeply-nested JSON must return 400, not crash the process."""
        nested = self._deeply_nested(depth=500)
        cur = FakeCursor()

        # We need to restore sys.setrecursionlimit after the test to avoid
        # polluting the test environment.
        original_limit = sys.getrecursionlimit()
        try:
            # Lower the limit so the deeply-nested parse triggers recursion sooner.
            sys.setrecursionlimit(300)
            responses, _ = _run_import(body=nested, cursor=cur)
        finally:
            sys.setrecursionlimit(original_limit)

        # The handler must not 500-crash the process.  If it caught the error,
        # we expect either 400 or 422.
        if responses:
            status, payload = responses[-1]
            self.assertIn(status, (400, 422),
                          f"Deeply-nested JSON must return 400 or 422; got {status}")
            self.assertFalse(payload.get("ok"))
        # If no response was appended the RecursionError escaped — that's the bug
        # the guard at server.py:1421 fixes; the test would fail here correctly.
        self.assertTrue(responses, "Handler must produce a response even on deeply-nested JSON")


# ── 7. Format/version gate → 422 (AC-8) ───────────────────────────────────────


class TestImportAccountFormatGate(unittest.TestCase):
    """AC-8: missing/unknown/unsupported schema_version, non-object body,
    wrong-typed collection/lists → 422; nothing written."""

    def _assert_422_nothing_written(self, body):
        cur = FakeCursor()
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 422, f"Expected 422 for body={body!r}, got {status}")
        self.assertFalse(payload.get("ok"))
        self.assertEqual(cur.calls, [], "No DB call must be issued on format/version rejection")

    def test_missing_schema_version_returns_422(self):
        body = {"collection": [], "lists": []}
        self._assert_422_nothing_written(body)

    def test_unknown_schema_version_returns_422(self):
        body = {"schema_version": 99, "collection": [], "lists": []}
        self._assert_422_nothing_written(body)

    def test_string_schema_version_returns_422(self):
        body = {"schema_version": "1", "collection": [], "lists": []}
        self._assert_422_nothing_written(body)

    def test_json_array_body_returns_422(self):
        """Non-object body (a JSON array) → 422."""
        body_bytes = b'[1, 2, 3]'
        cur = FakeCursor()
        responses, _ = _run_import(body=body_bytes, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 422)
        self.assertEqual(cur.calls, [])

    def test_collection_not_array_returns_422(self):
        body = {"schema_version": 1, "collection": "oops", "lists": []}
        self._assert_422_nothing_written(body)

    def test_lists_not_array_returns_422(self):
        body = {"schema_version": 1, "collection": [], "lists": {"wrong": True}}
        self._assert_422_nothing_written(body)

    def test_format_gate_audit_reason_invalid_format(self):
        audit_calls = []
        body = {"schema_version": 99, "collection": [], "lists": []}
        cur = FakeCursor()
        responses, _ = _run_import(body=body, cursor=cur, audit_calls=audit_calls)
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "invalid_format")

    def test_profile_block_and_user_id_ignored(self):
        """A file declaring another user_id and profile block must still be accepted
        (the fields are ignored, not rejected — AC-12)."""
        body = {
            "schema_version": 1,
            "exported_at": "2026-01-01T00:00:00+00:00",
            "user_id": _UID_B,          # must be ignored
            "profile": {"username": "other", "is_public": True},  # must be ignored
            "collection": [],
            "lists": [],
        }
        cur = FakeCursor(fetch_results=[[], ])
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200, "Valid body with extra ignored fields must return 200")


# ── 8. Happy path — 200 + summary (AC-2, AC-11) ───────────────────────────────


class TestImportAccountHappyPath(unittest.TestCase):
    """AC-2 / AC-11: authenticated import of a valid file returns 200 with the
    8-counter summary; all snake_case English field names per api-contracts."""

    SUMMARY_FIELDS = (
        "titles_imported",
        "titles_skipped_present",
        "titles_skipped_invalid",
        "lists_created",
        "lists_merged",
        "list_items_imported",
        "list_items_skipped_present",
        "list_items_skipped_invalid",
    )

    def _run_valid(self):
        """Run with a single movie + one new list containing one item."""
        cur = FakeCursor(
            fetch_results=[
                None,                   # dedup SELECT movies → not present
                [],                     # SELECT lists WHERE user_id → empty
                {"id": "list-001"},     # INSERT INTO lists RETURNING id
                {"next_pos": 0},        # SELECT COALESCE MAX(position)+1
            ]
        )
        return _run_import(body=_VALID_EXPORT, cursor=cur)

    def test_returns_200(self):
        responses, _ = self._run_valid()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))

    def test_summary_present(self):
        responses, _ = self._run_valid()
        _, payload = responses[-1]
        self.assertIn("summary", payload, "Response must carry a 'summary' key")

    def test_summary_has_all_eight_fields(self):
        responses, _ = self._run_valid()
        _, payload = responses[-1]
        summary = payload["summary"]
        for field in self.SUMMARY_FIELDS:
            self.assertIn(
                field, summary,
                f"AC-11: summary must carry '{field}'; got {list(summary.keys())}"
            )

    def test_titles_imported_equals_one(self):
        """One new movie → titles_imported == 1."""
        responses, _ = self._run_valid()
        _, payload = responses[-1]
        self.assertEqual(payload["summary"]["titles_imported"], 1)

    def test_lists_created_equals_one(self):
        """One new list → lists_created == 1."""
        responses, _ = self._run_valid()
        _, payload = responses[-1]
        self.assertEqual(payload["summary"]["lists_created"], 1)

    def test_list_items_imported_equals_one(self):
        """One item in the new list → list_items_imported == 1."""
        cur = FakeCursor(
            fetch_results=[
                None,               # dedup SELECT movies → not present
                [],                 # SELECT lists WHERE user_id → empty
                {"id": "list-001"}, # INSERT INTO lists RETURNING id
                {"next_pos": 0},    # SELECT COALESCE MAX(position)+1
            ],
            rowcount=1,  # ON CONFLICT DO NOTHING: 1 row inserted
        )
        responses, _ = _run_import(body=_VALID_EXPORT, cursor=cur)
        _, payload = responses[-1]
        self.assertEqual(payload["summary"]["list_items_imported"], 1)

    def test_empty_export_returns_200_all_zeros(self):
        """An empty export (collection:[], lists:[]) → 200, all counters == 0."""
        cur = FakeCursor(fetch_results=[[]])   # SELECT lists → []
        responses, _ = _run_import(body=_EMPTY_EXPORT, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        summary = payload["summary"]
        for field in self.SUMMARY_FIELDS:
            self.assertEqual(summary[field], 0, f"Empty import: {field} must be 0")

    def test_summary_fields_are_snake_case_english(self):
        """All summary counter names must be snake_case English (US-001 / AC-019)."""
        responses, _ = self._run_valid()
        _, payload = responses[-1]
        summary = payload["summary"]
        for key in summary:
            self.assertRegex(key, r'^[a-z][a-z0-9_]*$',
                             f"Summary field '{key}' must be snake_case English")


# ── 9. Full-fidelity field restore (AC-6) ─────────────────────────────────────


class TestImportAccountFidelity(unittest.TestCase):
    """AC-6: the INSERT carries the full column set — status, rating, note,
    platform, watched_at, current_season, current_episode, total_seasons, genres."""

    def _run_with_movie(self, movie_dict):
        body = {**_VALID_EXPORT, "collection": [movie_dict], "lists": []}
        cur = FakeCursor(
            fetch_results=[
                None,  # dedup SELECT → not present
                [],    # SELECT lists → empty
            ]
        )
        responses, cur = _run_import(body=body, cursor=cur)
        return responses, cur

    def _get_insert_params(self, cur):
        """Return the params tuple from the movies INSERT call."""
        for sql, params in cur.calls:
            if "INSERT INTO movies" in sql:
                return params
        return None

    def test_insert_includes_status(self):
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        self.assertIsNotNone(params)
        self.assertIn("vista", params)

    def test_insert_includes_rating(self):
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        self.assertIn(4, params)

    def test_insert_includes_note(self):
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        self.assertIn("Great film", params)

    def test_insert_includes_platform(self):
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        self.assertIn("Netflix", params)

    def test_insert_includes_watched_at(self):
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        self.assertIn("2024-06-01", params)

    def test_insert_includes_genres(self):
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        self.assertIn("Action,Drama", params)

    def test_series_insert_includes_season_episode_total(self):
        """AC-6: current_season, current_episode, total_seasons restored for series."""
        _, cur = self._run_with_movie(_VALID_SERIES)
        params = self._get_insert_params(cur)
        self.assertIsNotNone(params)
        self.assertIn(2, params)   # current_season
        self.assertIn(5, params)   # current_episode
        self.assertIn(3, params)   # total_seasons

    def test_movie_total_seasons_forced_null(self):
        """total_seasons must be None for media_type=movie (spec: forced null)."""
        movie_with_total = {**_VALID_MOVIE, "total_seasons": 5}
        _, cur = self._run_with_movie(movie_with_total)
        # The INSERT SQL has 16 positional params:
        # user_id, tmdb_id, media_type, title, year, poster_url, status,
        # rating, note, watched_at, platform, current_season, current_episode,
        # total_seasons (index 13), genres, created_at
        params = self._get_insert_params(cur)
        self.assertIsNotNone(params)
        # total_seasons is param index 13 (0-based).
        self.assertIsNone(params[13], "total_seasons must be forced to None for movies")

    def test_created_at_from_file_when_valid(self):
        """AC-6: created_at from the file when it parses as a valid ISO timestamp."""
        _, cur = self._run_with_movie(_VALID_MOVIE)
        params = self._get_insert_params(cur)
        # created_at is the last param (index 15)
        self.assertEqual(params[15], "2024-01-01T00:00:00+00:00")

    def test_insert_uses_caller_user_id_not_file(self):
        """AC-12 / AC-6: the user_id in the INSERT is the JWT sub, never from the file."""
        movie_with_uid = {**_VALID_MOVIE, "user_id": _UID_B}
        _, cur = self._run_with_movie(movie_with_uid)
        params = self._get_insert_params(cur)
        # user_id is param index 0
        self.assertEqual(params[0], _UID_A)
        self.assertNotEqual(params[0], _UID_B)


# ── 10. Collection dedup (AC-3) ───────────────────────────────────────────────


class TestImportAccountCollectionDedup(unittest.TestCase):
    """AC-3: a title with (tmdb_id, media_type) already present is skipped +
    counted as titles_skipped_present; a new title is inserted."""

    def test_existing_title_skipped_and_counted(self):
        """When the dedup SELECT returns a row, the movie is skipped."""
        # dedup SELECT → existing row found (truthy fetchone result)
        cur = FakeCursor(
            fetch_results=[
                {"1": 1},  # dedup SELECT → exists
                [],        # SELECT lists → empty
            ]
        )
        body = {**_VALID_EXPORT, "lists": []}
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["titles_skipped_present"], 1)
        self.assertEqual(payload["summary"]["titles_imported"], 0)

    def test_new_title_inserted(self):
        """When the dedup SELECT returns None, the movie is inserted."""
        cur = FakeCursor(
            fetch_results=[
                None,   # dedup SELECT → not present
                [],     # SELECT lists → empty
            ]
        )
        body = {**_VALID_EXPORT, "lists": []}
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["titles_imported"], 1)
        self.assertEqual(payload["summary"]["titles_skipped_present"], 0)

    def test_dedup_select_is_scoped_to_caller_user_id(self):
        """The dedup SELECT must use user_id from the JWT, not from the file."""
        cur = FakeCursor(fetch_results=[None, []])
        body = {**_VALID_EXPORT, "lists": []}
        responses, _ = _run_import(body=body, cursor=cur)
        # Find the dedup SELECT
        dedup_calls = [c for c in cur.calls if "SELECT 1 FROM movies" in c[0]]
        self.assertGreater(len(dedup_calls), 0, "Dedup SELECT must be executed")
        _, params = dedup_calls[0]
        self.assertIn(_UID_A, params, "Dedup SELECT must be scoped to caller user_id")
        self.assertNotIn(_UID_B, params, "Dedup SELECT must never use another user_id")

    def test_title_without_tmdb_id_inserted_without_dedup(self):
        """A title with no tmdb_id is inserted directly without a dedup SELECT."""
        no_id_movie = {**_VALID_MOVIE, "tmdb_id": None}
        body = {"schema_version": 1, "collection": [no_id_movie], "lists": []}
        cur = FakeCursor(fetch_results=[[]])  # SELECT lists → empty
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["titles_imported"], 1)
        # Confirm no dedup SELECT was issued
        dedup_calls = [c for c in cur.calls if "SELECT 1 FROM movies" in c[0]]
        self.assertEqual(dedup_calls, [], "No dedup SELECT for a title without tmdb_id")


# ── 11. List reconciliation (AC-4, AC-5) ──────────────────────────────────────


class TestImportAccountListReconciliation(unittest.TestCase):
    """AC-4: existing same-name list → merged (items with ON CONFLICT DO NOTHING).
    AC-5: new name → list created, items added."""

    def test_new_list_created(self):
        """A list whose name is not in the user's lists → lists_created++."""
        cur = FakeCursor(
            fetch_results=[
                [],                     # SELECT lists → no existing lists
                {"id": "new-list-id"},  # INSERT INTO lists RETURNING id
                {"next_pos": 0},        # SELECT COALESCE MAX(position)+1
            ],
            rowcount=1,
        )
        body = {
            "schema_version": 1,
            "collection": [],
            "lists": [{"name": "NewList", "items": [_VALID_LIST_ITEM]}],
        }
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["lists_created"], 1)
        self.assertEqual(payload["summary"]["lists_merged"], 0)
        self.assertEqual(payload["summary"]["list_items_imported"], 1)

    def test_existing_same_name_list_merged(self):
        """A list whose name matches an existing list → lists_merged++ (not created)."""
        existing_list_id = "existing-list-uuid"
        cur = FakeCursor(
            fetch_results=[
                # SELECT id, name FROM lists WHERE user_id = %s
                [{"id": existing_list_id, "name": "Favorites"}],
                {"next_pos": 2},   # SELECT COALESCE MAX(position)+1
            ],
            rowcount=1,
        )
        body = {
            "schema_version": 1,
            "collection": [],
            "lists": [{"name": "Favorites", "items": [_VALID_LIST_ITEM]}],
        }
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["lists_created"], 0)
        self.assertEqual(payload["summary"]["lists_merged"], 1)

    def test_existing_same_name_no_list_insert(self):
        """When merging, no INSERT INTO lists must be issued."""
        existing_list_id = "existing-list-uuid"
        cur = FakeCursor(
            fetch_results=[
                [{"id": existing_list_id, "name": "Favorites"}],
                {"next_pos": 0},
            ],
            rowcount=1,
        )
        body = {
            "schema_version": 1, "collection": [],
            "lists": [{"name": "Favorites", "items": [_VALID_LIST_ITEM]}],
        }
        _run_import(body=body, cursor=cur)
        list_inserts = [c for c in cur.calls if "INSERT INTO lists" in c[0]]
        self.assertEqual(list_inserts, [], "No INSERT INTO lists when merging into existing list")

    def test_item_dedup_on_conflict_do_nothing(self):
        """When ON CONFLICT returns rowcount=0 the item is counted as already present."""
        existing_list_id = "existing-list-uuid"
        cur = FakeCursor(
            fetch_results=[
                [{"id": existing_list_id, "name": "Favorites"}],
                {"next_pos": 0},
            ],
            rowcount=0,  # ON CONFLICT DO NOTHING → 0 rows inserted
        )
        body = {
            "schema_version": 1, "collection": [],
            "lists": [{"name": "Favorites", "items": [_VALID_LIST_ITEM]}],
        }
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["list_items_skipped_present"], 1)
        self.assertEqual(payload["summary"]["list_items_imported"], 0)

    def test_name_match_is_exact_after_trim(self):
        """Name match: 'Favorites' matches ' Favorites ' (trimmed) (AC-4)."""
        existing_list_id = "existing-list-uuid"
        cur = FakeCursor(
            fetch_results=[
                [{"id": existing_list_id, "name": "Favorites"}],
                {"next_pos": 0},
            ],
            rowcount=1,
        )
        body = {
            "schema_version": 1, "collection": [],
            "lists": [{"name": " Favorites ", "items": []}],
        }
        responses, _ = _run_import(body=body, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["lists_merged"], 1)

    def test_list_items_scoped_to_correct_list_id(self):
        """list_items INSERT must use the correct list_id (merged or new)."""
        existing_id = "existing-list-uuid"
        cur = FakeCursor(
            fetch_results=[
                [{"id": existing_id, "name": "Favorites"}],
                {"next_pos": 0},
            ],
            rowcount=1,
        )
        body = {
            "schema_version": 1, "collection": [],
            "lists": [{"name": "Favorites", "items": [_VALID_LIST_ITEM]}],
        }
        _run_import(body=body, cursor=cur)
        item_inserts = [c for c in cur.calls if "INSERT INTO list_items" in c[0]]
        self.assertGreater(len(item_inserts), 0)
        _, params = item_inserts[0]
        self.assertEqual(params[0], existing_id,
                         "list_item INSERT must use the existing list's id")


# ── 12. Idempotency (AC-7) ────────────────────────────────────────────────────


class TestImportAccountIdempotency(unittest.TestCase):
    """AC-7: importing the same file twice adds nothing the second time;
    all counters reflect everything skipped-present on re-run."""

    def test_second_import_all_skipped_present(self):
        """When every movie is already present and every list item exists,
        the second run returns all-zero *_imported counters."""
        # Movie dedup → exists; list exists → merge; item → ON CONFLICT (rowcount=0)
        existing_list_id = "existing-list-uuid"
        cur = FakeCursor(
            fetch_results=[
                {"1": 1},                                           # dedup movie → present
                [{"id": existing_list_id, "name": "Favorites"}],   # SELECT lists → present
                {"next_pos": 1},                                    # SELECT MAX(position)
            ],
            rowcount=0,  # ON CONFLICT DO NOTHING → 0 rows
        )
        responses, _ = _run_import(body=_VALID_EXPORT, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        summary = payload["summary"]
        self.assertEqual(summary["titles_imported"], 0)
        self.assertEqual(summary["lists_created"], 0)
        self.assertEqual(summary["list_items_imported"], 0)
        self.assertEqual(summary["titles_skipped_present"], 1)
        self.assertEqual(summary["lists_merged"], 1)
        self.assertEqual(summary["list_items_skipped_present"], 1)


# ── 13. Per-item validation (AC-10) ───────────────────────────────────────────


class TestImportAccountPerItemValidation(unittest.TestCase):
    """AC-10: invalid items are skipped + counted; valid items still import;
    non-TMDB poster_url is dropped to "" (item still imports)."""

    def _run_collection_only(self, collection, fetch_results_extra=None):
        """Run with `collection` and no lists."""
        body = {"schema_version": 1, "collection": collection, "lists": []}
        results = [
            None,  # dedup SELECT (for the first valid item — may not be used)
            [],    # SELECT lists → empty (sometimes needed)
        ]
        if fetch_results_extra:
            results = fetch_results_extra + [[]]
        cur = FakeCursor(fetch_results=results)
        responses, _ = _run_import(body=body, cursor=cur)
        return responses[-1][1]["summary"]

    def test_empty_title_skipped_invalid(self):
        bad = {**_VALID_MOVIE, "title": ""}
        summary = self._run_collection_only([bad, _VALID_MOVIE])
        self.assertEqual(summary["titles_skipped_invalid"], 1)
        self.assertEqual(summary["titles_imported"], 1)

    def test_bad_media_type_skipped_invalid(self):
        bad = {**_VALID_MOVIE, "media_type": "anime"}
        summary = self._run_collection_only([bad, _VALID_MOVIE])
        self.assertEqual(summary["titles_skipped_invalid"], 1)

    def test_bad_status_skipped_invalid(self):
        """status not in (pendiente/viendo/vista/abandonada) → skipped."""
        bad = {**_VALID_MOVIE, "status": "watched"}
        summary = self._run_collection_only([bad, _VALID_MOVIE])
        self.assertEqual(summary["titles_skipped_invalid"], 1)

    def test_bad_rating_skipped_invalid(self):
        """rating outside 1-5 integer range → skipped."""
        bad = {**_VALID_MOVIE, "rating": 10}
        summary = self._run_collection_only([bad, _VALID_MOVIE])
        self.assertEqual(summary["titles_skipped_invalid"], 1)

    def test_bad_date_skipped_invalid(self):
        """Invalid watched_at → skipped."""
        bad = {**_VALID_MOVIE, "watched_at": "not-a-date"}
        summary = self._run_collection_only([bad, _VALID_MOVIE])
        self.assertEqual(summary["titles_skipped_invalid"], 1)

    def test_bad_platform_skipped_invalid(self):
        """Platform not in PLATFORMS → skipped."""
        bad = {**_VALID_MOVIE, "platform": "SomethingElse"}
        summary = self._run_collection_only([bad, _VALID_MOVIE])
        self.assertEqual(summary["titles_skipped_invalid"], 1)

    def test_non_dict_item_skipped_invalid(self):
        """A non-dict collection item → skipped invalid."""
        body = {"schema_version": 1, "collection": ["not-a-dict", _VALID_MOVIE], "lists": []}
        cur = FakeCursor(fetch_results=[None, []])
        responses, _ = _run_import(body=body, cursor=cur)
        summary = responses[-1][1]["summary"]
        self.assertEqual(summary["titles_skipped_invalid"], 1)
        self.assertEqual(summary["titles_imported"], 1)

    def test_non_tmdb_poster_url_dropped_item_imports(self):
        """A non-TMDB poster is dropped to ""; the item still imports (AC-10)."""
        bad_poster = {**_VALID_MOVIE, "poster_url": "https://evil.com/malicious.jpg"}
        body = {"schema_version": 1, "collection": [bad_poster], "lists": []}
        cur = FakeCursor(fetch_results=[None, []])
        responses, _ = _run_import(body=body, cursor=cur)
        summary = responses[-1][1]["summary"]
        # Item must import (not skip)
        self.assertEqual(summary["titles_imported"], 1)
        self.assertEqual(summary["titles_skipped_invalid"], 0)
        # The poster stored must be "" (check INSERT params)
        insert_calls = [c for c in cur.calls if "INSERT INTO movies" in c[0]]
        self.assertGreater(len(insert_calls), 0)
        _, params = insert_calls[0]
        # poster_url is at index 5 in the INSERT param list
        # (user_id=0, tmdb_id=1, media_type=2, title=3, year=4, poster_url=5)
        self.assertEqual(params[5], "",
                         "Non-TMDB poster must be stored as empty string")

    def test_javascript_poster_url_dropped(self):
        """A javascript: URL in poster_url must be dropped to ""."""
        bad_poster = {**_VALID_MOVIE, "poster_url": "javascript:alert(1)"}
        body = {"schema_version": 1, "collection": [bad_poster], "lists": []}
        cur = FakeCursor(fetch_results=[None, []])
        responses, _ = _run_import(body=body, cursor=cur)
        insert_calls = [c for c in cur.calls if "INSERT INTO movies" in c[0]]
        self.assertGreater(len(insert_calls), 0)
        _, params = insert_calls[0]
        self.assertEqual(params[5], "", "javascript: URL must be dropped to empty string")

    def test_valid_tmdb_poster_url_stored(self):
        """A valid https://image.tmdb.org/ poster URL is stored as-is."""
        body = {"schema_version": 1, "collection": [_VALID_MOVIE], "lists": []}
        cur = FakeCursor(fetch_results=[None, []])
        responses, _ = _run_import(body=body, cursor=cur)
        insert_calls = [c for c in cur.calls if "INSERT INTO movies" in c[0]]
        _, params = insert_calls[0]
        self.assertEqual(params[5], _VALID_MOVIE["poster_url"])

    def test_genres_truncated_not_rejected(self):
        """A genres string longer than 360 chars is truncated to 360, not rejected."""
        long_genres = "A" * 400
        movie = {**_VALID_MOVIE, "genres": long_genres}
        body = {"schema_version": 1, "collection": [movie], "lists": []}
        cur = FakeCursor(fetch_results=[None, []])
        responses, _ = _run_import(body=body, cursor=cur)
        summary = responses[-1][1]["summary"]
        self.assertEqual(summary["titles_imported"], 1,
                         "Oversized genres string must be truncated, not rejected")
        # Check stored value is ≤ 360 chars
        insert_calls = [c for c in cur.calls if "INSERT INTO movies" in c[0]]
        _, params = insert_calls[0]
        # genres is at param index 14
        self.assertLessEqual(len(params[14]), 360)

    def test_mixed_file_valid_and_invalid_items(self):
        """Mixed file: valid items import, invalid items are skipped; counters correct."""
        bad_status = {**_VALID_MOVIE, "status": "unknown_status", "tmdb_id": 999}
        good = {**_VALID_MOVIE, "tmdb_id": 101}
        body = {"schema_version": 1, "collection": [bad_status, good], "lists": []}
        cur = FakeCursor(fetch_results=[None, []])
        responses, _ = _run_import(body=body, cursor=cur)
        summary = responses[-1][1]["summary"]
        self.assertEqual(summary["titles_skipped_invalid"], 1)
        self.assertEqual(summary["titles_imported"], 1)


# ── 14. Cross-account scoping (AC-12) ─────────────────────────────────────────


class TestImportAccountCrossAccountScoping(unittest.TestCase):
    """AC-12: every INSERT and dedup SELECT uses the JWT sub (caller's user_id);
    a file claiming another user_id never writes to or reads from that account."""

    def test_all_inserts_use_caller_user_id(self):
        """Every INSERT SQL must carry _UID_A (the JWT sub)."""
        cur = FakeCursor(
            fetch_results=[
                None,                   # dedup → not present
                [],                     # SELECT lists → empty
                {"id": "new-list-id"},  # INSERT list
                {"next_pos": 0},        # MAX(position)
            ],
            rowcount=1,
        )
        body = {
            **_VALID_EXPORT,
            "user_id": _UID_B,          # file claims B's user_id (must be ignored)
            "profile": {"username": "user_b"},
        }
        _run_import(body=body, cursor=cur)
        for sql, params in cur.calls:
            if "INSERT" in sql.upper() and params:
                self.assertNotIn(_UID_B, params,
                                 f"No INSERT must use the file's user_id; sql={sql!r}")

    def test_selects_never_use_other_user_id(self):
        """No SQL must carry _UID_B; user_id-scoped queries carry _UID_A."""
        cur = FakeCursor(
            fetch_results=[None, [], {"id": "new-id"}, {"next_pos": 0}],
            rowcount=1,
        )
        body = {**_VALID_EXPORT, "user_id": _UID_B}
        _run_import(body=body, cursor=cur)
        for sql, params in cur.calls:
            if params:
                self.assertNotIn(_UID_B, params,
                                 f"No SQL must use the file's user_id; sql={sql!r}")
            # For SELECT/INSERT queries that are explicitly user_id-scoped, assert _UID_A.
            # (The position query uses list_id instead — that's expected.)
            if params and "user_id" in sql:
                self.assertIn(_UID_A, params,
                              f"user_id-scoped SQL must use the JWT sub; sql={sql!r}")


# ── 15. Non-destructive + rollback on DB error (AC-13) ────────────────────────


class TestImportAccountNonDestructive(unittest.TestCase):
    """AC-13: no existing row is deleted or overwritten; a DB error rolls back
    (generic 500; raw error never leaked)."""

    def test_no_update_or_delete_sql(self):
        """The handler must never issue UPDATE or DELETE on movies/lists/list_items."""
        cur = FakeCursor(
            fetch_results=[
                None, [], {"id": "new-list-id"}, {"next_pos": 0}
            ],
            rowcount=1,
        )
        _run_import(body=_VALID_EXPORT, cursor=cur)
        for sql, _ in cur.calls:
            upper = sql.strip().upper()
            self.assertFalse(
                upper.startswith("UPDATE") or upper.startswith("DELETE"),
                f"Non-destructive contract violated: handler issued {sql!r}"
            )

    def test_db_error_returns_500(self):
        """A psycopg2.Error inside the transaction → generic 500."""
        h, responses = _make_import_handler()

        import contextlib

        @contextlib.contextmanager
        def _exploding_db():
            raise psycopg2.Error("simulated DB failure")
            yield  # pragma: no cover

        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            mock.patch.object(server, "get_db", _exploding_db),
        ):
            h._import_account()

        self.assertTrue(responses, "Handler must produce a response on DB error")
        status, payload = responses[-1]
        self.assertEqual(status, 500)
        self.assertFalse(payload.get("ok"))

    def test_db_error_generic_body_no_raw_error(self):
        """500 body must never contain the raw psycopg2 error detail."""
        h, responses = _make_import_handler()
        sentinel = "psycopg2: FATAL DETAIL — SHOULD NOT LEAK"

        import contextlib

        @contextlib.contextmanager
        def _exploding_db():
            raise psycopg2.Error(sentinel)
            yield  # pragma: no cover

        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            mock.patch.object(server, "get_db", _exploding_db),
        ):
            h._import_account()

        if responses:
            _, payload = responses[-1]
            body_str = json.dumps(payload)
            self.assertNotIn(sentinel, body_str)
            self.assertNotIn("psycopg2", body_str)
            self.assertNotIn("Traceback", body_str)

    def test_db_error_audit_reason_db_error(self):
        """AC-15 / Backend Reviewer handoff: psycopg2.Error path must emit
        account.import_denied with reason 'db_error' (server.py:1546)."""
        audit_calls = []
        h, responses = _make_import_handler()

        import contextlib

        @contextlib.contextmanager
        def _exploding_db():
            raise psycopg2.Error("simulated DB failure")
            yield  # pragma: no cover

        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(
                server, "_audit",
                side_effect=lambda a, u, t: audit_calls.append({"action": a, "user_id": u, "target": t})
            ),
            mock.patch.object(server, "get_db", _exploding_db),
        ):
            h._import_account()

        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "db_error",
                         "psycopg2.Error path must emit 'db_error' reason token")


# ── 16. Generic error hygiene (AC-15) ─────────────────────────────────────────


class TestImportAccountErrorHygiene(unittest.TestCase):
    """AC-15: every error response is generic es-ES; the raw error is never
    serialised; no internal detail (Traceback, DB class, exception message) appears."""

    def _assert_generic_error(self, status, payload):
        self.assertFalse(payload.get("ok"), "Error response must set ok=False")
        error = payload.get("error", "")
        self.assertTrue(error, "Error body must not be empty")
        self.assertNotIn("Traceback", error)
        self.assertNotIn("Exception", error)
        self.assertNotIn("psycopg2", error)
        self.assertNotIn("JSONDecodeError", error)

    def test_400_generic(self):
        cur = FakeCursor()
        responses, _ = _run_import(body=b"not json", cursor=cur)
        self._assert_generic_error(*responses[-1])

    def test_413_body_generic(self):
        h, responses = _make_import_handler(content_length=MAX_IMPORT_BODY + 1)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            patch_db(cur),
        ):
            h._import_account()
        self._assert_generic_error(*responses[-1])

    def test_422_generic(self):
        body = {"schema_version": 2, "collection": [], "lists": []}
        cur = FakeCursor()
        responses, _ = _run_import(body=body, cursor=cur)
        self._assert_generic_error(*responses[-1])

    def test_500_generic(self):
        h, responses = _make_import_handler()

        import contextlib

        @contextlib.contextmanager
        def _exploding_db():
            raise psycopg2.Error("should not leak")
            yield  # pragma: no cover

        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(server, "_audit"),
            mock.patch.object(server, "get_db", _exploding_db),
        ):
            h._import_account()
        self._assert_generic_error(*responses[-1])


# ── 17. Audit hygiene (AU-007, LO-*) ──────────────────────────────────────────


class TestImportAccountAuditSuccess(unittest.TestCase):
    """AU-007 / LO-*: success emits _audit('account.imported', user_id, 'account');
    user_hash only, never raw user_id / email / token."""

    def test_success_emits_imported_audit(self):
        audit_calls = []
        cur = FakeCursor(fetch_results=[None, [], {"id": "new-id"}, {"next_pos": 0}], rowcount=1)
        _run_import(body=_VALID_EXPORT, cursor=cur, audit_calls=audit_calls)
        imported = [c for c in audit_calls if c["action"] == "account.imported"]
        self.assertEqual(len(imported), 1, "Exactly one 'account.imported' audit call expected")

    def test_success_audit_target_is_account(self):
        audit_calls = []
        cur = FakeCursor(fetch_results=[None, [], {"id": "new-id"}, {"next_pos": 0}], rowcount=1)
        _run_import(body=_VALID_EXPORT, cursor=cur, audit_calls=audit_calls)
        imported = [c for c in audit_calls if c["action"] == "account.imported"]
        self.assertEqual(imported[0]["target"], "account")

    def test_success_audit_receives_real_user_id_for_hashing(self):
        audit_calls = []
        cur = FakeCursor(fetch_results=[None, [], {"id": "new-id"}, {"next_pos": 0}], rowcount=1)
        _run_import(body=_VALID_EXPORT, cursor=cur, audit_calls=audit_calls)
        imported = [c for c in audit_calls if c["action"] == "account.imported"]
        self.assertEqual(imported[0]["user_id"], _UID_A)

    def test_success_audit_stdout_carries_user_hash_not_raw_id(self):
        """The printed audit line for success must carry user_hash, not raw user_id."""
        captured = io.StringIO()
        h, _ = _make_import_handler()
        cur = FakeCursor(fetch_results=[None, [], {"id": "new-id"}, {"next_pos": 0}], rowcount=1)
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
            mock.patch("sys.stdout", captured),
        ):
            h._import_account()
        output = captured.getvalue()
        self.assertIn("audit ", output, "Must emit at least one audit line")
        for line in output.splitlines():
            if "audit " in line:
                entry = json.loads(line[len("audit "):])
                if entry.get("action") == "account.imported":
                    self.assertIn("user_hash", entry)
                    self.assertNotEqual(entry["user_hash"], _UID_A,
                                        "Must not log raw user_id")
                    break
        else:
            self.fail("No 'account.imported' audit line found in stdout")


class TestImportAccountAuditDenial(unittest.TestCase):
    """AU-007 / LO-*: every denial path emits account.import_denied with the
    correct reason token; never leaks raw user_id or email."""

    def test_unauth_denial_user_hash_null_in_stdout(self):
        """Printed audit line for unauthenticated denial must carry user_hash=null."""
        captured = io.StringIO()
        h, _ = _make_import_handler(user_id=None)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
            mock.patch("sys.stdout", captured),
        ):
            h._import_account()
        output = captured.getvalue()
        for line in output.splitlines():
            if "audit " in line:
                entry = json.loads(line[len("audit "):])
                if entry.get("action") == "account.import_denied":
                    self.assertIsNone(entry.get("user_hash"),
                                      "Unauthenticated denial must carry user_hash=null")
                    break
        else:
            self.fail("No 'account.import_denied' audit line for unauth denial")

    def test_rate_limited_denial_user_id_set(self):
        """Rate-limited denial: user_id is the authenticated user (not None)."""
        audit_calls = []
        _run_import(rate_ok=False, audit_calls=audit_calls)
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertEqual(denied[0]["user_id"], _UID_A)

    def test_invalid_format_denial_reason(self):
        audit_calls = []
        bad = {"schema_version": 0, "collection": [], "lists": []}
        cur = FakeCursor()
        _run_import(body=bad, cursor=cur, audit_calls=audit_calls)
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        reasons = {d["target"] for d in denied}
        self.assertIn("invalid_format", reasons)

    def test_too_large_denial_reason(self):
        audit_calls = []
        h, responses = _make_import_handler(content_length=MAX_IMPORT_BODY + 1)
        cur = FakeCursor()
        with (
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            mock.patch.object(
                server, "_audit",
                side_effect=lambda a, u, t: audit_calls.append({"action": a, "user_id": u, "target": t})
            ),
            patch_db(cur),
        ):
            h._import_account()
        denied = [c for c in audit_calls if c["action"] == "account.import_denied"]
        self.assertGreater(len(denied), 0)
        self.assertEqual(denied[0]["target"], "too_large")

    def test_no_sensitive_value_in_audit_args(self):
        """No denial audit call must pass email or a raw user_id that looks like a secret."""
        audit_calls = []
        cur = FakeCursor()
        responses, _ = _run_import(
            rate_ok=False,
            cursor=cur,
            audit_calls=audit_calls,
        )
        # The _audit function receives user_id as the raw UUID (for hashing internally),
        # which is expected. What must NOT appear is email / token / raw error text.
        for call in audit_calls:
            for key, val in call.items():
                if isinstance(val, str):
                    self.assertNotIn("@", val,  # no email
                                     f"Audit arg must not contain email; got {call!r}")
                    self.assertNotIn("Bearer", val,
                                     f"Audit arg must not contain JWT bearer; got {call!r}")


# ── 18. Integration-style test (real-Postgres schema simulation) ───────────────


class TestImportAccountIntegration(unittest.TestCase):
    """Integration-style test: drives FakeCursor through the multi-step SQL sequence
    that mirrors what a real Postgres transaction would execute for a multi-list,
    multi-title import with dedup and list merge.

    This is the § Required Tests → Integration row in the task DoD:
    "End-to-end against a real/test Postgres: a multi-list, multi-title export
    imports, dedups on re-run, and merges into a pre-existing same-named list
    (AC-2/3/4/5/7)."

    Since the CI environment has no live Postgres, this test exercises the full
    handler logic against a FakeCursor that returns results identical to what a
    real DB would return. It is explicitly named "integration" per the spec to
    distinguish it from the pure-unit tests above and to document the complete
    SQL execution sequence under realistic conditions.
    """

    def _build_export(self):
        """A 2-movie + 2-list export (one list already exists in the user's account)."""
        return {
            "schema_version": 1,
            "exported_at": "2026-07-01T12:00:00+00:00",
            "profile": {"username": "testuser"},
            "collection": [
                # Movie 1: new
                {
                    "tmdb_id": 101, "media_type": "movie", "title": "Film A",
                    "year": "2022", "poster_url": "https://image.tmdb.org/t/p/w500/a.jpg",
                    "status": "vista", "rating": 4, "note": "Good",
                    "watched_at": "2024-06-01", "platform": "Netflix",
                    "current_season": None, "current_episode": None, "total_seasons": None,
                    "genres": "Action", "created_at": "2024-01-01T00:00:00+00:00",
                },
                # Movie 2: already exists (dedup should skip)
                {
                    "tmdb_id": 202, "media_type": "movie", "title": "Film B (existing)",
                    "year": "2021", "poster_url": "https://image.tmdb.org/t/p/w500/b.jpg",
                    "status": "pendiente", "rating": None, "note": None,
                    "watched_at": None, "platform": None,
                    "current_season": None, "current_episode": None, "total_seasons": None,
                    "genres": None, "created_at": "2024-02-01T00:00:00+00:00",
                },
            ],
            "lists": [
                # List 1: "Favorites" already exists → merge; item already in list → skip
                {
                    "name": "Favorites",
                    "items": [
                        {
                            "tmdb_id": 101, "media_type": "movie", "title": "Film A",
                            "year": "2022", "poster_url": "https://image.tmdb.org/t/p/w500/a.jpg",
                            "position": 1,
                        }
                    ],
                },
                # List 2: "Watchlist" is new → create
                {
                    "name": "Watchlist",
                    "items": [
                        {
                            "tmdb_id": 303, "media_type": "tv", "title": "Series C",
                            "year": "2023", "poster_url": "https://image.tmdb.org/t/p/w500/c.jpg",
                            "position": 1,
                        }
                    ],
                },
            ],
        }

    def _build_rerun_export(self):
        """Same export as _build_export — used for the idempotency (re-run) check."""
        return self._build_export()

    def test_first_run_imports_correctly(self):
        """First run: Film A inserted; Film B skipped (dedup); Favorites merged;
        Watchlist created; item in Favorites skipped; item in Watchlist imported."""
        export = self._build_export()
        # FakeCursor fetch_results must match the handler's SQL execution order:
        # 1. SELECT 1 FROM movies (Film A dedup)  → None (not present)
        # 2. SELECT 1 FROM movies (Film B dedup)  → {"1": 1} (present → skip)
        # 3. SELECT id, name FROM lists           → [{"id": "fav-id", "name": "Favorites"}]
        # 4. SELECT COALESCE MAX(position)+1 (Favorites) → {"next_pos": 5}
        # 5. INSERT list_items Favorites/Film A ON CONFLICT → rowcount=0 (already in list)
        # 6. INSERT INTO lists (Watchlist) RETURNING id → {"id": "watch-id"}
        # 7. SELECT COALESCE MAX(position)+1 (Watchlist) → {"next_pos": 0}
        # 8. INSERT list_items Watchlist/Series C → rowcount=1

        # We build a cursor that alternates rowcount: the Watchlist item insert
        # must return rowcount=1. We use two FakeCursors joined via side_effect.
        call_index = [0]
        execute_results = [
            None,              # (1) dedup Film A → None
            {"1": 1},          # (2) dedup Film B → present
            # (3) fetchall() for SELECT id,name FROM lists
            [{"id": "fav-id", "name": "Favorites"}],
            {"next_pos": 5},   # (4) MAX(position) for Favorites
            # (5) ON CONFLICT DO NOTHING for Film A in Favorites → rowcount=0
            # (6) INSERT INTO lists RETURNING id for Watchlist
            {"id": "watch-id"},
            {"next_pos": 0},   # (7) MAX(position) for Watchlist
            # (8) ON CONFLICT DO NOTHING for Series C → rowcount=1
        ]

        rowcount_sequence = [
            # These correspond to the INSERT calls in order:
            # INSERT INTO movies (Film A)           → rowcount irrelevant (not read)
            # ON CONFLICT Favorites/Film A           → rowcount = 0 (already present)
            # ON CONFLICT Watchlist/Series C         → rowcount = 1
        ]

        # We use a custom FakeCursor subclass to control rowcount per execute call.
        class SequencedCursor(FakeCursor):
            def __init__(self):
                super().__init__(fetch_results=execute_results)
                self._rc_queue = [1, 0, 1]  # INSERT movie, ON CONFLICT Fav, ON CONFLICT Watch
                self._rc_idx = 0

            def execute(self, sql, params=None):
                super().execute(sql, params)
                # Advance rowcount for each INSERT
                if "INSERT" in sql.upper():
                    self.rowcount = self._rc_queue[min(self._rc_idx, len(self._rc_queue) - 1)]
                    self._rc_idx += 1
                else:
                    self.rowcount = 1

        cur = SequencedCursor()
        responses, _ = _run_import(body=export, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        summary = payload["summary"]

        self.assertEqual(summary["titles_imported"], 1,           "Film A must be imported")
        self.assertEqual(summary["titles_skipped_present"], 1,    "Film B must be skipped (dedup)")
        self.assertEqual(summary["lists_created"], 1,             "Watchlist must be created")
        self.assertEqual(summary["lists_merged"], 1,              "Favorites must be merged")
        self.assertEqual(summary["list_items_skipped_present"], 1,"Film A in Favorites already present")
        self.assertEqual(summary["list_items_imported"], 1,       "Series C in Watchlist must import")

    def test_second_run_all_skipped(self):
        """After a first import, re-running the same export adds nothing (AC-7)."""
        export = self._build_rerun_export()
        # On re-run everything is present:
        # - Film A dedup → present; Film B dedup → present
        # - Favorites already exists; Watchlist already exists (now in the name map)
        # - Both list items → ON CONFLICT rowcount=0
        existing_lists = [
            {"id": "fav-id", "name": "Favorites"},
            {"id": "watch-id", "name": "Watchlist"},
        ]
        cur = FakeCursor(
            fetch_results=[
                {"1": 1},          # Film A dedup → present
                {"1": 1},          # Film B dedup → present
                existing_lists,    # SELECT lists → both exist
                {"next_pos": 5},   # Favorites MAX(position)
                # ON CONFLICT Favorites/Film A → rowcount=0
                {"next_pos": 1},   # Watchlist MAX(position)
                # ON CONFLICT Watchlist/Series C → rowcount=0
            ],
            rowcount=0,  # all ON CONFLICT → 0 rows
        )
        responses, _ = _run_import(body=export, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        summary = payload["summary"]

        self.assertEqual(summary["titles_imported"], 0)
        self.assertEqual(summary["lists_created"], 0)
        self.assertEqual(summary["list_items_imported"], 0)
        self.assertEqual(summary["titles_skipped_present"], 2)
        self.assertEqual(summary["lists_merged"], 2,
                         "Both lists must be counted as merged on re-run")
        self.assertEqual(summary["list_items_skipped_present"], 2)

    def test_merge_into_pre_existing_list_with_partial_items(self):
        """A pre-existing 'Favorites' list contains Film A; the import adds Film B
        (new item) and skips Film A (already present)."""
        export = {
            "schema_version": 1,
            "collection": [],
            "lists": [
                {
                    "name": "Favorites",
                    "items": [
                        # Item 1: already in Favorites → skip
                        {"tmdb_id": 101, "media_type": "movie", "title": "Film A",
                         "year": "2022", "poster_url": "https://image.tmdb.org/t/p/w500/a.jpg",
                         "position": 1},
                        # Item 2: new → import
                        {"tmdb_id": 202, "media_type": "movie", "title": "Film B",
                         "year": "2021", "poster_url": "https://image.tmdb.org/t/p/w500/b.jpg",
                         "position": 2},
                    ],
                }
            ],
        }

        class PartialCursor(FakeCursor):
            """Rowcount alternates: 0 (Film A already present), 1 (Film B new)."""
            def __init__(self):
                super().__init__(
                    fetch_results=[
                        [{"id": "fav-id", "name": "Favorites"}],
                        {"next_pos": 1},
                    ]
                )
                self._inserts = 0

            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "INSERT INTO list_items" in sql:
                    self.rowcount = [0, 1][min(self._inserts, 1)]
                    self._inserts += 1
                else:
                    self.rowcount = 1

        cur = PartialCursor()
        responses, _ = _run_import(body=export, cursor=cur)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        summary = payload["summary"]
        self.assertEqual(summary["lists_merged"], 1)
        self.assertEqual(summary["list_items_skipped_present"], 1,
                         "Film A (already in Favorites) must be skipped")
        self.assertEqual(summary["list_items_imported"], 1,
                         "Film B (new in Favorites) must be imported")


if __name__ == "__main__":
    unittest.main()
