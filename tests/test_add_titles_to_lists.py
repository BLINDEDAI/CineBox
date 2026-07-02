"""Regression tests for the add-titles-to-lists feature (AC-5, AC-8).

Covers every ### Tester scope row delegated to Python unittest:

  - AC-8: cross-user POST /api/lists/{id}/items against a non-owned list → 404,
    target list unchanged (cross-user ownership check).
  - AC-8: cross-user DELETE /api/lists/{id}/items/{item_id} against a non-owned
    list → 404.
  - AC-8: anonymous (no JWT) POST /api/lists/{id}/items → 401.
  - AC-8: anonymous (no JWT) DELETE /api/lists/{id}/items/{item_id} → 401.
  - AC-5: re-adding the same (tmdb_id, media_type) to a list → 409; item count
    unchanged (unique-constraint path via psycopg2.errors.UniqueViolation).

The anonymous-POST row is already covered by AnonymousOwnerMutationIntegration in
test_public_profiles_and_shared_lists.py (test_anonymous_add_list_item_401). That
test is cited in the Tester handoff DoD coverage as satisfying part of AC-8; this
module adds the missing rows that file does not cover.

DB boundary is stubbed with FakeCursor throughout (no live Supabase).
"""

import unittest

import psycopg2.errors

import server
from tests._harness import FakeCursor, make_handler, patch_db


# ── Helpers ───────────────────────────────────────────────────────────────────


def _valid_item_body():
    """Return a valid POST /api/lists/{id}/items request body."""
    return {
        "tmdb_id": 550,
        "media_type": "movie",
        "title": "Fight Club",
        "year": "1999",
        "poster_url": "https://image.tmdb.org/t/p/w342/poster.jpg",
    }


# ── AC-8: per-user isolation — POST /api/lists/{id}/items ─────────────────────


class AddListItemIsolationIntegration(unittest.TestCase):
    """AC-8: cross-user and anonymous POST /api/lists/{id}/items isolation."""

    def test_cross_user_post_returns_404(self):
        """AC-8: POST to another user's list → 404, list unchanged (IDOR guard).

        The handler does: SELECT 1 FROM lists WHERE id=%s AND user_id=%s.
        When the list belongs to a different user, fetchone() returns None → 404.
        """
        # fetchone() for the ownership SELECT returns None (not owner)
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(body=_valid_item_body(), user_id="attacker-uid")
        with patch_db(cur):
            h._add_list_item("victim-list-uuid")
        self.assertEqual(responses[-1][0], 404)
        # Only the ownership SELECT should have been executed — no INSERT.
        ownership_queries = [
            c for c in cur.calls
            if "SELECT" in c[0] and "lists" in c[0] and "user_id" in c[0]
        ]
        self.assertTrue(ownership_queries, "Ownership SELECT not executed")
        insert_queries = [c for c in cur.calls if "INSERT INTO list_items" in c[0]]
        self.assertEqual(insert_queries, [], "INSERT must not execute when ownership check fails")

    def test_cross_user_post_target_list_unchanged(self):
        """AC-8: 404 path executes no INSERT — list item count is not modified."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(body=_valid_item_body(), user_id="attacker-uid")
        with patch_db(cur):
            h._add_list_item("victim-list-uuid")
        # Confirm zero INSERT statements reached the DB stub.
        insert_calls = [c for c in cur.calls if "INSERT" in c[0]]
        self.assertEqual(insert_calls, [], "No INSERT should reach DB after 404")

    def test_anonymous_post_returns_401(self):
        """AC-8: anonymous POST /api/lists/{id}/items (no JWT) → 401.

        Note: also covered by test_anonymous_add_list_item_401 in
        test_public_profiles_and_shared_lists.py; included here for completeness
        of this feature's test surface.
        """
        h, responses = make_handler(body=_valid_item_body(), user_id=None)
        # No patch_db needed — 401 fires before any DB access.
        h._add_list_item("some-list-uuid")
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_post_no_db_access(self):
        """AC-8: anonymous 401 path executes zero DB statements (fast-fail)."""
        cur = FakeCursor()
        h, responses = make_handler(body=_valid_item_body(), user_id=None)
        with patch_db(cur):
            h._add_list_item("some-list-uuid")
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [], "No DB call should occur before auth check")


# ── AC-8: per-user isolation — DELETE /api/lists/{id}/items/{item_id} ─────────


class DeleteListItemIsolationIntegration(unittest.TestCase):
    """AC-8: cross-user and anonymous DELETE /api/lists/{id}/items/{item_id}."""

    def test_cross_user_delete_returns_404(self):
        """AC-8: DELETE item in another user's list → 404.

        The handler uses DELETE … RETURNING (or checks rowcount=0 after scoped
        DELETE JOIN). When rowcount=0 (item does not belong to the caller) → 404.
        """
        # rowcount=0 means the scoped DELETE matched no row (not owner).
        cur = FakeCursor(rowcount=0)
        h, responses = make_handler(user_id="attacker-uid")
        with patch_db(cur):
            h._delete_list_item("victim-list-uuid", "victim-item-uuid")
        self.assertEqual(responses[-1][0], 404)

    def test_cross_user_delete_target_unchanged(self):
        """AC-8: 404 path means the DELETE matched zero rows — no item removed."""
        cur = FakeCursor(rowcount=0)
        h, responses = make_handler(user_id="attacker-uid")
        with patch_db(cur):
            h._delete_list_item("victim-list-uuid", "victim-item-uuid")
        # The DELETE SQL must include user_id scoping (user_id in WHERE clause).
        delete_calls = [c for c in cur.calls if "DELETE" in c[0]]
        self.assertTrue(delete_calls, "DELETE statement not executed")
        delete_sql = delete_calls[0][0]
        self.assertIn("user_id", delete_sql,
                      "DELETE must scope by user_id for isolation")

    def test_own_delete_succeeds(self):
        """Sanity: DELETE own item (rowcount=1) → 200."""
        cur = FakeCursor(rowcount=1)
        h, responses = make_handler(user_id="owner-uid")
        with patch_db(cur):
            h._delete_list_item("my-list-uuid", "my-item-uuid")
        self.assertEqual(responses[-1][0], 200)

    def test_anonymous_delete_returns_401(self):
        """AC-8: anonymous DELETE /api/lists/{id}/items/{item_id} (no JWT) → 401."""
        h, responses = make_handler(user_id=None)
        h._delete_list_item("some-list-uuid", "some-item-uuid")
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_delete_no_db_access(self):
        """AC-8: anonymous 401 path executes zero DB statements."""
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        with patch_db(cur):
            h._delete_list_item("some-list-uuid", "some-item-uuid")
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [], "No DB call should occur before auth check")


# ── AC-5: duplicate detection — POST /api/lists/{id}/items ────────────────────


class DuplicateListItemIntegration(unittest.TestCase):
    """AC-5: re-adding the same (tmdb_id, media_type) to a list → 409."""

    def _run_add_duplicate(self):
        """Simulate _add_list_item where the DB UNIQUE constraint fires.

        fetch_results sequence for the handler path:
          1. fetchone() → ownership SELECT returns the row (caller owns the list).
          2. fetchone() → next_pos SELECT returns {"next_pos": 1}.
          Then the INSERT raises UniqueViolation (simulated via a custom cursor).
        """

        class UniqueViolationCursor(FakeCursor):
            """FakeCursor that raises UniqueViolation on the INSERT statement."""

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                if "INSERT INTO list_items" in sql:
                    raise psycopg2.errors.UniqueViolation(
                        "duplicate key value violates unique constraint "
                        '"list_items_list_id_tmdb_id_media_type_key"'
                    )

        cur = UniqueViolationCursor(
            fetch_results=[
                {"exists": 1, "visibility": "private"},   # ownership SELECT (now carries visibility)
                {"next_pos": 1},     # next_pos SELECT
            ]
        )
        h, responses = make_handler(
            body={
                "tmdb_id": 550,
                "media_type": "movie",
                "title": "Fight Club",
                "year": "1999",
                "poster_url": "https://image.tmdb.org/t/p/w342/poster.jpg",
            },
            user_id="owner-uid",
        )
        with patch_db(cur):
            h._add_list_item("my-list-uuid")
        return cur, responses

    def test_duplicate_returns_409(self):
        """AC-5: UniqueViolation from DB → handler returns 409."""
        _, responses = self._run_add_duplicate()
        self.assertEqual(responses[-1][0], 409)

    def test_duplicate_response_ok_false(self):
        """AC-5: 409 body has ok=False (non-blocking notice contract)."""
        _, responses = self._run_add_duplicate()
        self.assertFalse(responses[-1][1]["ok"])

    def test_duplicate_error_message(self):
        """AC-5: 409 body carries the Spanish duplicate notice text."""
        _, responses = self._run_add_duplicate()
        self.assertIn("error", responses[-1][1])
        self.assertEqual(responses[-1][1]["error"], "Ese título ya está en la lista")

    def test_duplicate_no_second_insert(self):
        """AC-5: after UniqueViolation, only one INSERT attempted (no retry)."""
        cur, _ = self._run_add_duplicate()
        insert_calls = [c for c in cur.calls if "INSERT INTO list_items" in c[0]]
        self.assertEqual(len(insert_calls), 1,
                         "Exactly one INSERT should have been attempted")

    def test_successful_add_returns_201(self):
        """Sanity: first add (no duplicate) → 201 with an item id."""
        ownership_row = {"exists": 1, "visibility": "private"}
        next_pos_row = {"next_pos": 0}
        new_id_row = {"id": "new-item-uuid"}
        cur = FakeCursor(
            fetch_results=[ownership_row, next_pos_row, new_id_row]
        )
        h, responses = make_handler(
            body=_valid_item_body(),
            user_id="owner-uid",
        )
        with patch_db(cur):
            h._add_list_item("my-list-uuid")
        self.assertEqual(responses[-1][0], 201)
        self.assertTrue(responses[-1][1]["ok"])
        self.assertIn("id", responses[-1][1])


if __name__ == "__main__":
    unittest.main()
