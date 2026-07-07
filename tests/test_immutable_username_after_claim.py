"""Unit tests for immutable-username-after-claim.

Covers every ### Tester scope backend row from the task DoD:
  - AC-4: already-set username + DIFFERENT value -> 400 immutability message,
    no INSERT INTO profiles UPSERT reaches the DB.
  - AC-3 / AC-7: first claim (stored username None) + valid value -> 200,
    username written.
  - AC-5: already-set username + SAME value -> 200, no audit line, unchanged.
  - AC-6: compound update (username == current + is_public/avatar change) -> 200,
    visibility/avatar columns written, username a no-op; and a username-absent
    visibility-only update is unaffected by the guard.

Reuses the stubbed-DB harness from tests/test_choose_username_at_registration.py
(`FakeCursor` + `make_handler` + `patch_db`, `tests/_harness.py`). No live DB.
"""

import io
import json
import unittest
from contextlib import redirect_stdout

from tests._harness import FakeCursor, make_handler, patch_db

_IMMUTABILITY_MESSAGE = "El nombre de usuario no se puede cambiar una vez elegido"


def _run_patch(body, *, user_id="user-test", current_row=None, capture_stdout=False):
    """Drive _patch_profile with a stubbed DB + given request body.

    `current_row` is the already-stored profile row (dict) or None (no row yet /
    no username). Returns (cur, responses) or (cur, responses, stdout) when
    `capture_stdout` is True.
    """
    cur = FakeCursor(fetch_results=[current_row])
    h, responses = make_handler(body=body, user_id=user_id)
    if capture_stdout:
        buf = io.StringIO()
        with redirect_stdout(buf):
            with patch_db(cur):
                h._patch_profile()
        return cur, responses, buf.getvalue()
    with patch_db(cur):
        h._patch_profile()
    return cur, responses


def _upsert_calls(cur):
    return [(sql, params) for sql, params in cur.calls if "INSERT INTO profiles" in sql]


class PatchProfileWriteOnceGuard(unittest.TestCase):
    """AC-4: an already-set username is immutable; a different value is rejected
    400 and nothing is written (threat model: server is the sole authority)."""

    def test_change_to_different_value_returns_400_with_message(self):
        """AC-4: already-set 'alice' + body {'username':'bob'} -> 400, exact message."""
        current_row = {
            "username": "alice", "is_public": False,
            "show_collection": False, "show_stats": False, "avatar_url": None,
        }
        _, responses = _run_patch({"username": "bob"}, current_row=current_row)
        self.assertEqual(responses[-1][0], 400)
        self.assertFalse(responses[-1][1]["ok"])
        self.assertEqual(responses[-1][1]["error"], _IMMUTABILITY_MESSAGE)

    def test_change_to_different_value_writes_nothing(self):
        """AC-4: the rejected change never reaches an INSERT INTO profiles UPSERT."""
        current_row = {
            "username": "alice", "is_public": False,
            "show_collection": False, "show_stats": False, "avatar_url": None,
        }
        cur, _ = _run_patch({"username": "bob"}, current_row=current_row)
        self.assertEqual(_upsert_calls(cur), [],
                          "No UPSERT must occur when a set username is changed")

    def test_change_to_different_value_emits_no_audit(self):
        """The rejection is a validation 400, not a state change -- no audit line
        (LO-*: the reused profile.username_set audit only fires on a real change)."""
        current_row = {
            "username": "alice", "is_public": False,
            "show_collection": False, "show_stats": False, "avatar_url": None,
        }
        _, _, stdout = _run_patch(
            {"username": "bob"}, current_row=current_row, capture_stdout=True)
        self.assertNotIn("profile.username_set", stdout,
                          "A rejected change must not emit the username_set audit line")


class PatchProfileFirstClaimUnblocked(unittest.TestCase):
    """AC-3 / AC-7: the guard never fires on the first claim (stored username
    empty) -- the registration / first-login claim path keeps working."""

    def test_first_claim_returns_200(self):
        """AC-3/AC-7: stored username None + valid value -> 200 (guard does not fire)."""
        _, responses = _run_patch({"username": "alice"}, current_row=None)
        self.assertEqual(responses[-1][0], 200)
        self.assertTrue(responses[-1][1]["ok"])

    def test_first_claim_writes_username(self):
        """AC-3/AC-7: the first-claim UPSERT carries the normalized username."""
        cur, responses = _run_patch({"username": "alice"}, current_row=None)
        upserts = _upsert_calls(cur)
        self.assertEqual(len(upserts), 1, "Exactly one UPSERT expected for a first claim")
        _, params = upserts[0]
        self.assertIn("alice", params)
        self.assertEqual(responses[-1][1]["profile"]["username"], "alice")

    def test_first_claim_emits_audit(self):
        """AC-3/AC-7 sanity: a genuine first claim still fires profile.username_set."""
        _, _, stdout = _run_patch(
            {"username": "alice"}, current_row=None, capture_stdout=True)
        self.assertIn("profile.username_set", stdout)


class PatchProfileSameValueIdempotent(unittest.TestCase):
    """AC-5: resubmitting the already-stored username is a no-op success, never
    an error -- keeps compound updates that echo the current handle harmless."""

    def _current_row(self):
        return {
            "username": "alice", "is_public": False,
            "show_collection": False, "show_stats": False, "avatar_url": None,
        }

    def test_same_value_returns_200(self):
        """AC-5: already-set 'alice' + body {'username':'alice'} -> 200, no error."""
        _, responses = _run_patch({"username": "alice"}, current_row=self._current_row())
        self.assertEqual(responses[-1][0], 200)
        self.assertTrue(responses[-1][1]["ok"])

    def test_same_value_emits_no_audit(self):
        """AC-5: same-value resubmit does not fire profile.username_set (username_changed
        stays False -- it is a no-op, not a state change)."""
        _, _, stdout = _run_patch(
            {"username": "alice"}, current_row=self._current_row(), capture_stdout=True)
        self.assertNotIn("profile.username_set", stdout,
                          "A same-value resubmit must not emit the username_set audit line")

    def test_same_value_username_unchanged_in_response(self):
        """AC-5: the returned profile still reflects the unchanged username 'alice'."""
        _, responses = _run_patch({"username": "alice"}, current_row=self._current_row())
        self.assertEqual(responses[-1][1]["profile"]["username"], "alice")


class PatchProfileCompoundUpdateUnaffected(unittest.TestCase):
    """AC-6: username == current + a visibility/avatar change succeeds and writes
    the visibility/avatar columns; the username lock never blocks unrelated
    fields. A visibility-only update with no username field at all is likewise
    unaffected by the guard (the username branch is skipped entirely)."""

    def _current_row(self, **overrides):
        row = {
            "username": "alice", "is_public": False,
            "show_collection": False, "show_stats": False, "avatar_url": None,
        }
        row.update(overrides)
        return row

    def test_compound_username_echo_plus_is_public_returns_200(self):
        """AC-6: username=='alice' (echoed) + is_public=True -> 200, both written."""
        cur, responses = _run_patch(
            {"username": "alice", "is_public": True},
            current_row=self._current_row(),
        )
        self.assertEqual(responses[-1][0], 200)
        self.assertTrue(responses[-1][1]["ok"])
        upserts = _upsert_calls(cur)
        self.assertEqual(len(upserts), 1)
        _, params = upserts[0]
        self.assertIn(True, params, "is_public=True must reach the UPSERT params")
        self.assertEqual(responses[-1][1]["profile"]["is_public"], True)
        self.assertEqual(responses[-1][1]["profile"]["username"], "alice")

    def test_compound_username_echo_plus_avatar_set_returns_200(self):
        """AC-6: username=='alice' (echoed) + an avatar change is not blocked by
        the username lock (the avatar branch runs independently, before the
        username branch, in _patch_profile)."""
        # is_public must already be True in current_row so the "publish without a
        # valid username" guard (server.py ~2508) is a non-issue here -- the
        # username IS valid/unchanged, this only isolates the avatar branch.
        cur, responses = _run_patch(
            {"username": "alice", "avatar": "remove"},
            current_row=self._current_row(),
        )
        self.assertEqual(responses[-1][0], 200)
        upserts = _upsert_calls(cur)
        self.assertEqual(len(upserts), 1)
        cols_sql, params = upserts[0]
        self.assertIn("avatar_url", cols_sql)
        self.assertIsNone(responses[-1][1]["profile"]["avatar_url"])

    def test_visibility_only_update_no_username_field_unaffected(self):
        """AC-6: an update with no 'username' key at all skips the guard entirely
        -- the username branch (and its 400) never evaluates."""
        cur, responses = _run_patch(
            {"is_public": True},
            current_row=self._current_row(),
        )
        self.assertEqual(responses[-1][0], 200)
        self.assertTrue(responses[-1][1]["ok"])
        upserts = _upsert_calls(cur)
        self.assertEqual(len(upserts), 1)
        cols_sql, _ = upserts[0]
        self.assertNotIn("username", cols_sql,
                          "username column must not be part of a visibility-only UPSERT")

    def test_visibility_only_update_emits_no_username_audit(self):
        """AC-6: a visibility-only update never emits profile.username_set."""
        _, _, stdout = _run_patch(
            {"is_public": True}, current_row=self._current_row(), capture_stdout=True)
        self.assertNotIn("profile.username_set", stdout)


if __name__ == "__main__":
    unittest.main()
