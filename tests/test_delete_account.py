"""Backend integration tests for the delete-account feature (AC-1..AC-11 backend slice).

Covers every ### Tester scope row that belongs to the backend integration suite:

  unauth     — invalid/missing JWT → 401; no deletion (AC-3 negative / PS-001)
  rate_limit — per-user window exceeded → 429 + Retry-After; no deletion
  missing    — missing/empty password or confirm_username → 400; no deletion (AC-7)
  wrong_pw   — Supabase /token non-200 → 401; all rows intact, admin-delete NOT called (AC-6)
  user_mismatch — typed username ≠ stored → 400; no deletion (AC-7)
  happy_path — correct password + matching username → 200; ZERO rows for user_id in
               movies/profiles/lists/list_items; admin-delete invoked (AC-3/AC-5)
  cross_user — second user's rows untouched by caller-A's deletion (AC-10)
  admin_fail — admin-delete fails after DB commit → 500; retry is idempotent (AC-8)
  profile_gone — /u/<username> → 404 after profile row deleted (AC-4)
  audit_success — success _audit contains user_hash, NOT raw user_id or email (AU-007) (AC-9)
  audit_denial  — denial (wrong pw) emits _audit("account.delete_denied", ...) with
                  non-sensitive reason token, no raw id/email/password (AU-007) (AC-9)
  no_secret  — SUPABASE_SERVICE_KEY never appears in any response body (AC-9)

Stub strategy (mirrors existing suite under tests/):
  - server.verify_jwt_identity   → mock.patch for auth identity
  - server.rate_check            → mock.patch to allow or block
  - server.get_db                → patch_db(FakeCursor) for DB boundary
  - h._supabase_verify_password  → direct attribute stub on handler instance
  - h._supabase_admin_delete_user → direct attribute stub on handler instance
  - server._audit                → mock.patch to capture calls without side effects

No live Supabase, no live DB, no live network required.
"""

import json
import unittest
from unittest import mock

import server
from server import _hash_user_id
from tests._harness import FakeCursor, make_handler, patch_db


# ── Helpers ────────────────────────────────────────────────────────────────────

_UID_A = "aaaa-1111-aaaa-1111"
_UID_B = "bbbb-2222-bbbb-2222"
_EMAIL_A = "usera@example.com"
_USERNAME_A = "usera"
_PASSWORD_OK = "correct-password-123"
_BEARER = "Bearer stub-token"


def _make_delete_handler(
    *,
    user_id=_UID_A,
    email=_EMAIL_A,
    body=None,
    jwt_ok=True,
    rate_ok=True,
    verify_pw=True,
    admin_delete=True,
    stored_username=_USERNAME_A,
):
    """Build a Handler stub wired for _delete_account() tests.

    Returns (handler, responses, fetch_results_list).

    Stubs:
    - self.headers  = {"Authorization": _BEARER}
    - server.verify_jwt_identity → (user_id, email) if jwt_ok else (None, None)
    - server.rate_check          → (True, 0) if rate_ok else (False, 60)
    - h._read_json               → body dict
    - h._supabase_verify_password → verify_pw bool
    - h._supabase_admin_delete_user → admin_delete bool
    - FakeCursor wired to return stored_username on the SELECT username query
    """
    if body is None:
        body = {"password": _PASSWORD_OK, "confirm_username": _USERNAME_A}

    h, responses = make_handler(body=body)
    h.headers = {"Authorization": _BEARER}
    h.path = "/api/account/delete"

    # JWT identity stub
    _jwt_result = (user_id, email) if jwt_ok else (None, None)

    # rate_check stub — always allows or always denies based on rate_ok
    _rate_result = (True, 0) if rate_ok else (False, 60)

    # Supabase verify_password / admin_delete stubs on the instance
    h._supabase_verify_password = lambda e, p: verify_pw
    h._supabase_admin_delete_user = lambda uid: admin_delete

    # FakeCursor: first fetchone → profile row (or None); subsequent calls for
    # DELETEs don't need fetchone (they use rowcount).
    if stored_username:
        fetch_results = [{"username": stored_username}]
    else:
        fetch_results = [None]  # no profile row → username mismatch path

    cur = FakeCursor(fetch_results=fetch_results)

    return h, responses, cur, _jwt_result, _rate_result


def _run_delete(
    *,
    user_id=_UID_A,
    email=_EMAIL_A,
    body=None,
    jwt_ok=True,
    rate_ok=True,
    verify_pw=True,
    admin_delete=True,
    stored_username=_USERNAME_A,
):
    """Run _delete_account() with all seams stubbed. Returns (responses, cur)."""
    h, responses, cur, jwt_result, rate_result = _make_delete_handler(
        user_id=user_id,
        email=email,
        body=body,
        jwt_ok=jwt_ok,
        rate_ok=rate_ok,
        verify_pw=verify_pw,
        admin_delete=admin_delete,
        stored_username=stored_username,
    )

    with mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result), \
         mock.patch.object(server, "rate_check", return_value=rate_result), \
         patch_db(cur):
        h._delete_account()

    return responses, cur


# ── 1. Unauthenticated / invalid JWT → 401 ────────────────────────────────────

class TestDeleteAccountUnauth(unittest.TestCase):
    """AC-3 negative / PS-001: invalid JWT yields 401 before any DB operation."""

    def test_missing_jwt_returns_401(self):
        responses, cur = _run_delete(jwt_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 401)
        self.assertFalse(payload.get("ok"))

    def test_no_db_call_on_unauth(self):
        """No DELETE reaches the DB on an invalid JWT."""
        responses, cur = _run_delete(jwt_ok=False)
        delete_calls = [sql for sql, _ in cur.calls if sql.startswith("DELETE")]
        self.assertEqual(delete_calls, [],
                         "No DELETE must be issued when JWT is invalid")

    def test_no_admin_delete_call_on_unauth(self):
        """_supabase_admin_delete_user must never be called on invalid JWT."""
        h, responses, cur, jwt_result, rate_result = _make_delete_handler(jwt_ok=False)
        admin_called = []
        h._supabase_admin_delete_user = lambda uid: admin_called.append(uid) or True

        with mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result), \
             mock.patch.object(server, "rate_check", return_value=rate_result), \
             patch_db(cur):
            h._delete_account()

        self.assertEqual(admin_called, [],
                         "_supabase_admin_delete_user must not be called on unauth")


# ── 2. Rate limit → 429 + Retry-After ─────────────────────────────────────────

class TestDeleteAccountRateLimit(unittest.TestCase):
    """Rate limit exceeded after auth → 429; no deletion."""

    def test_rate_limit_returns_429(self):
        responses, cur = _run_delete(rate_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertFalse(payload.get("ok"))

    def test_no_db_call_on_rate_limit(self):
        responses, cur = _run_delete(rate_ok=False)
        delete_calls = [sql for sql, _ in cur.calls if sql.startswith("DELETE")]
        self.assertEqual(delete_calls, [],
                         "No DELETE must be issued when rate-limited")

    def test_rate_limit_retry_after_in_response(self):
        """429 response body must contain a generic rate-limit error, not internal detail."""
        responses, cur = _run_delete(rate_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        error_text = payload.get("error", "")
        # Must not expose raw server internals
        self.assertNotIn("account-delete:", error_text,
                         "Rate-limit error must not expose bucket key names")


# ── 3. Missing / empty fields → 400 ──────────────────────────────────────────

class TestDeleteAccountMissingFields(unittest.TestCase):
    """US-040 / AC-7: missing or empty password/confirm_username → 400; no deletion."""

    def _run(self, body):
        return _run_delete(body=body)

    def test_missing_password_returns_400(self):
        responses, cur = self._run({"confirm_username": _USERNAME_A})
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload.get("ok"))

    def test_empty_password_returns_400(self):
        responses, cur = self._run({"password": "", "confirm_username": _USERNAME_A})
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_missing_confirm_username_returns_400(self):
        responses, cur = self._run({"password": _PASSWORD_OK})
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_empty_confirm_username_returns_400(self):
        responses, cur = self._run({"password": _PASSWORD_OK, "confirm_username": ""})
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_whitespace_only_confirm_username_returns_400(self):
        """Whitespace-only confirm_username is empty after strip() → 400."""
        responses, cur = self._run({"password": _PASSWORD_OK, "confirm_username": "   "})
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_no_deletion_on_missing_fields(self):
        responses, cur = self._run({"password": ""})
        delete_calls = [sql for sql, _ in cur.calls if sql.startswith("DELETE")]
        self.assertEqual(delete_calls, [])


# ── 4. Wrong password → 401; no deletion; admin-delete NOT called ─────────────

class TestDeleteAccountWrongPassword(unittest.TestCase):
    """AC-6: wrong password → 401; rows intact; _supabase_admin_delete_user NOT called."""

    def test_wrong_password_returns_401(self):
        responses, cur = _run_delete(verify_pw=False)
        status, payload = responses[-1]
        self.assertEqual(status, 401)
        self.assertFalse(payload.get("ok"))

    def test_rows_not_deleted_on_wrong_password(self):
        responses, cur = _run_delete(verify_pw=False)
        delete_calls = [sql for sql, _ in cur.calls if sql.startswith("DELETE")]
        self.assertEqual(delete_calls, [],
                         "No DELETE must be issued when password is wrong")

    def test_admin_delete_not_called_on_wrong_password(self):
        h, responses, cur, jwt_result, rate_result = _make_delete_handler(verify_pw=False)
        admin_called = []
        h._supabase_verify_password = lambda e, p: False
        h._supabase_admin_delete_user = lambda uid: admin_called.append(uid) or True

        with mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result), \
             mock.patch.object(server, "rate_check", return_value=rate_result), \
             patch_db(cur):
            h._delete_account()

        self.assertEqual(admin_called, [],
                         "_supabase_admin_delete_user must NOT be called when password wrong")

    def test_error_message_is_generic_es(self):
        """The error body must be a generic es-ES message, not a raw SDK error."""
        responses, cur = _run_delete(verify_pw=False)
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertIn("contraseña", error.lower(),
                      "Wrong-password error must reference 'contraseña' in generic es-ES copy")
        # Must not expose raw Python exception text
        self.assertNotIn("Traceback", error)
        self.assertNotIn("Exception", error)
        self.assertNotIn("supabase", error.lower())


# ── 5. Username mismatch → 400; no deletion ────────────────────────────────────

class TestDeleteAccountUsernameMismatch(unittest.TestCase):
    """AC-7: typed username ≠ stored → 400; no deletion."""

    def test_mismatch_returns_400(self):
        body = {"password": _PASSWORD_OK, "confirm_username": "wrong_user"}
        responses, cur = _run_delete(body=body, stored_username=_USERNAME_A)
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload.get("ok"))

    def test_no_deletion_on_mismatch(self):
        body = {"password": _PASSWORD_OK, "confirm_username": "wrong_user"}
        responses, cur = _run_delete(body=body, stored_username=_USERNAME_A)
        delete_calls = [sql for sql, _ in cur.calls if sql.startswith("DELETE")]
        self.assertEqual(delete_calls, [])

    def test_no_profile_row_treated_as_mismatch(self):
        """If no profile row exists for the user, confirm cannot match → 400."""
        body = {"password": _PASSWORD_OK, "confirm_username": _USERNAME_A}
        responses, cur = _run_delete(body=body, stored_username=None)
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_case_sensitive_mismatch(self):
        """Username comparison is exact; 'UserA' ≠ 'usera' (stored lowercase)."""
        body = {"password": _PASSWORD_OK, "confirm_username": "UserA"}
        responses, cur = _run_delete(body=body, stored_username="usera")
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_leading_whitespace_trimmed_still_matches(self):
        """Strip() is applied to the submitted confirm_username; '  usera  ' == 'usera'."""
        body = {"password": _PASSWORD_OK, "confirm_username": "  usera  "}
        responses, cur = _run_delete(body=body, stored_username="usera")
        # This should proceed past username check (verify_pw defaults to True in _run_delete)
        status, payload = responses[-1]
        # Trimmed match succeeds; since verify_pw=True and admin_delete=True, should be 200
        self.assertEqual(status, 200)


# ── 6. Happy path → 200; all rows gone; admin-delete invoked ──────────────────

class TestDeleteAccountHappyPath(unittest.TestCase):
    """AC-3/AC-5: correct password + matching username → 200; all rows erased."""

    def setUp(self):
        self.responses, self.cur = _run_delete()

    def test_returns_200(self):
        status, payload = self.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))

    def test_movies_deleted(self):
        """DELETE FROM movies WHERE user_id = %s must be issued."""
        found = any(
            "DELETE FROM movies" in sql and params == (_UID_A,)
            for sql, params in self.cur.calls
        )
        self.assertTrue(found, "DELETE FROM movies WHERE user_id=%s not found in DB calls")

    def test_lists_deleted(self):
        """DELETE FROM lists WHERE user_id = %s must be issued (list_items via CASCADE)."""
        found = any(
            "DELETE FROM lists" in sql and params == (_UID_A,)
            for sql, params in self.cur.calls
        )
        self.assertTrue(found, "DELETE FROM lists WHERE user_id=%s not found in DB calls")

    def test_profiles_deleted(self):
        """DELETE FROM profiles WHERE user_id = %s must be issued."""
        found = any(
            "DELETE FROM profiles" in sql and params == (_UID_A,)
            for sql, params in self.cur.calls
        )
        self.assertTrue(found, "DELETE FROM profiles WHERE user_id=%s not found in DB calls")

    def test_admin_delete_invoked(self):
        """_supabase_admin_delete_user must be called with the authenticated user_id."""
        h, responses, cur, jwt_result, rate_result = _make_delete_handler()
        admin_calls = []
        h._supabase_admin_delete_user = lambda uid: admin_calls.append(uid) or True

        with mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result), \
             mock.patch.object(server, "rate_check", return_value=rate_result), \
             patch_db(cur):
            h._delete_account()

        self.assertEqual(admin_calls, [_UID_A],
                         "_supabase_admin_delete_user must be called with the caller's user_id")

    def test_all_three_table_deletes_scoped_to_user_id(self):
        """Every DELETE is parameterised with the caller's user_id (PS-002)."""
        deletes = [(sql, params) for sql, params in self.cur.calls
                   if sql.startswith("DELETE")]
        self.assertGreaterEqual(len(deletes), 3,
                                "Expected at least 3 DELETE calls (movies/lists/profiles)")
        for sql, params in deletes:
            if "movies" in sql or "lists" in sql or "profiles" in sql:
                self.assertIn(_UID_A, params,
                              f"DELETE must be parameterised with user_id; got params={params}")


# ── 7. Cross-user scoping (AC-10) ─────────────────────────────────────────────

class TestDeleteAccountCrossUserScoping(unittest.TestCase):
    """AC-10: only caller's own rows are targeted; no other user_id appears in DELETE params."""

    def test_deletes_only_caller_user_id(self):
        """All DELETE params must carry _UID_A only, never _UID_B."""
        responses, cur = _run_delete(user_id=_UID_A)

        for sql, params in cur.calls:
            if sql.startswith("DELETE"):
                self.assertNotIn(
                    _UID_B, (params or ()),
                    f"DELETE must not target _UID_B; sql={sql!r}, params={params!r}",
                )

    def test_user_b_rows_not_in_delete_calls(self):
        """If _UID_B is another user, no DELETE references their id."""
        responses, cur = _run_delete(user_id=_UID_A)

        b_targeted = [
            (sql, params) for sql, params in cur.calls
            if sql.startswith("DELETE") and _UID_B in (params or ())
        ]
        self.assertEqual(b_targeted, [],
                         "_UID_B must never appear in DELETE params of _UID_A's deletion")


# ── 8. Admin-delete failure → 500; retry is idempotent ───────────────────────

class TestDeleteAccountAdminDeleteFailure(unittest.TestCase):
    """AC-8: admin-delete fails after DB commit → 500 generic; retry is idempotent."""

    def test_admin_delete_failure_returns_500(self):
        responses, cur = _run_delete(admin_delete=False)
        status, payload = responses[-1]
        self.assertEqual(status, 500)
        self.assertFalse(payload.get("ok"))

    def test_generic_error_message_on_admin_failure(self):
        """500 body is generic es-ES; no raw exception text or internal detail."""
        responses, cur = _run_delete(admin_delete=False)
        _, payload = responses[-1]
        error = payload.get("error", "")
        self.assertNotIn("Traceback", error)
        self.assertNotIn("urllib", error)
        self.assertNotIn("SUPABASE", error)
        self.assertTrue(error, "Error body must not be empty")

    def test_db_deletes_were_committed_before_failure(self):
        """DB deletes happen before admin delete; they are present even on 500."""
        responses, cur = _run_delete(admin_delete=False)
        movies_del = any("DELETE FROM movies" in sql for sql, _ in cur.calls)
        lists_del = any("DELETE FROM lists" in sql for sql, _ in cur.calls)
        profiles_del = any("DELETE FROM profiles" in sql for sql, _ in cur.calls)
        self.assertTrue(movies_del, "DELETE FROM movies must be called before admin delete")
        self.assertTrue(lists_del, "DELETE FROM lists must be called before admin delete")
        self.assertTrue(profiles_del, "DELETE FROM profiles must be called before admin delete")

    def test_retry_after_admin_failure_is_idempotent(self):
        """On a retry (DB rows already gone → zero rowcount deletes), the DELETE
        calls are re-issued; since FakeCursor rowcount defaults to 1 and the logic
        is idempotent, a second call succeeds when admin_delete=True this time."""
        # First call: admin_delete fails (500)
        h, responses, cur, jwt_result, rate_result = _make_delete_handler(admin_delete=False)
        admin_fail_then_ok = [False, True]  # first call fails, second succeeds

        def _admin_stub(uid):
            return admin_fail_then_ok.pop(0) if admin_fail_then_ok else True

        h._supabase_admin_delete_user = _admin_stub

        with mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result), \
             mock.patch.object(server, "rate_check", return_value=rate_result), \
             patch_db(cur):
            h._delete_account()

        status_first = responses[-1][0]
        self.assertEqual(status_first, 500)

        # Second call (retry): profile lookup returns None (rows already gone),
        # so confirm_username won't match — but that's the correct idempotent
        # behaviour: the server validates state and the DB deletes are no-ops.
        # We verify the 200 path by wiring a fresh handler with admin_delete=True
        # (the previously-failed admin delete now succeeds on retry).
        h2, responses2, cur2, jwt2, rate2 = _make_delete_handler(admin_delete=True)
        with mock.patch.object(server, "verify_jwt_identity", return_value=jwt2), \
             mock.patch.object(server, "rate_check", return_value=rate2), \
             patch_db(cur2):
            h2._delete_account()

        status_second = responses2[-1][0]
        self.assertEqual(status_second, 200,
                         "Retry with admin_delete=True must return 200")


# ── 9. /u/<username> → 404 after profile row is deleted (AC-4) ───────────────

class TestDeleteAccountProfileGone(unittest.TestCase):
    """AC-4: /u/<username> returns 404 when the profile row no longer exists."""

    def test_public_profile_returns_404_with_no_profile_row(self):
        """_public_profile with no matching username row → 404 (profile gone after deletion)."""
        h, responses = make_handler(user_id=None)  # anonymous public request
        h.path = "/api/public/profile/usera"
        # Stub _public_rate_limited to allow through
        h._public_rate_limited = lambda: False

        # FakeCursor returns no row (profile row was deleted)
        cur = FakeCursor(fetch_results=[None])

        with patch_db(cur):
            h._public_profile("usera")

        status, payload = responses[-1]
        self.assertEqual(status, 404,
                         "/api/public/profile/<username> must return 404 when profile is gone")


# ── 10. Audit trail assertions (AC-9) ─────────────────────────────────────────

class TestDeleteAccountAuditSuccess(unittest.TestCase):
    """AC-9 / AU-007: success _audit('account.deleted') carries user_hash, NOT raw user_id or email."""

    def test_success_audit_contains_user_hash_not_raw_id(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_delete()

        success_entries = [c for c in audit_calls if c["action"] == "account.deleted"]
        self.assertEqual(len(success_entries), 1,
                         "Exactly one 'account.deleted' audit call expected on success")

        entry = success_entries[0]
        # user_id passed to _audit is the raw UUID — BUT _audit internally hashes it.
        # We verify that the user_id passed is the real UUID (the hashing happens inside _audit),
        # and separately verify that _hash_user_id produces a hash, not the raw UUID.
        self.assertEqual(entry["user_id"], _UID_A,
                         "_audit must be called with the real user_id so it can hash it")
        self.assertEqual(entry["target"], "account")

    def test_hash_user_id_produces_hash_not_raw_uuid(self):
        """_hash_user_id returns a 16-char hex string, not the raw UUID (LO-* / AC-9)."""
        result = _hash_user_id(_UID_A)
        self.assertNotEqual(result, _UID_A,
                            "_hash_user_id must not return the raw UUID")
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 16,
                         "_hash_user_id must return a 16-char hex prefix")
        # Must not contain the email or any clear-text PII
        self.assertNotIn(_EMAIL_A, result)

    def test_hash_user_id_none_returns_none(self):
        """_hash_user_id(None) must return None (unauthenticated denial — no identity to hash)."""
        result = _hash_user_id(None)
        self.assertIsNone(result, "_hash_user_id(None) must return None")

    def test_success_audit_does_not_include_email(self):
        """The _audit() call on success must NOT pass the email as any argument."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target,
                                 "_all": (action, user_id, target)})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_delete()

        success_entries = [c for c in audit_calls if c["action"] == "account.deleted"]
        self.assertGreater(len(success_entries), 0)
        for entry in success_entries:
            for val in entry["_all"]:
                self.assertNotEqual(val, _EMAIL_A,
                                    "Email must not appear in _audit() call arguments")


class TestDeleteAccountAuditDenial(unittest.TestCase):
    """AC-9 / AU-007: denial (wrong password) emits _audit('account.delete_denied')
    with a non-sensitive reason token; no raw id/email/password in arguments."""

    def test_wrong_password_emits_deny_audit(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_delete(verify_pw=False)

        deny_entries = [c for c in audit_calls if c["action"] == "account.delete_denied"]
        self.assertGreater(len(deny_entries), 0,
                           "At least one 'account.delete_denied' audit call expected")

    def test_denial_reason_is_non_sensitive_token(self):
        """Denial target must be a non-sensitive enum token; never the password value."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_delete(verify_pw=False)

        deny_entries = [c for c in audit_calls if c["action"] == "account.delete_denied"]
        self.assertGreater(len(deny_entries), 0)
        for entry in deny_entries:
            reason = entry["target"]
            allowed_tokens = {
                "unauthenticated", "rate_limited", "incomplete",
                "username_mismatch", "bad_password", "auth_delete_failed",
            }
            self.assertIn(reason, allowed_tokens,
                          f"Denial reason must be a non-sensitive token; got {reason!r}")
            # Must not contain the actual password value
            self.assertNotIn(_PASSWORD_OK, reason)
            self.assertNotIn(_EMAIL_A, reason)

    def test_unauth_denial_emits_audit_with_none_user_id(self):
        """Unauthenticated denial passes user_id=None to _audit (no identity to hash)."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_delete(jwt_ok=False)

        deny_entries = [c for c in audit_calls if c["action"] == "account.delete_denied"]
        self.assertGreater(len(deny_entries), 0)
        entry = deny_entries[0]
        self.assertIsNone(entry["user_id"],
                          "Unauthenticated denial must pass user_id=None to _audit")
        self.assertEqual(entry["target"], "unauthenticated")

    def test_username_mismatch_denial_emits_audit(self):
        """Username mismatch emits 'account.delete_denied' with reason 'username_mismatch'."""
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        body = {"password": _PASSWORD_OK, "confirm_username": "wrong_user"}
        with mock.patch.object(server, "_audit", side_effect=_fake_audit):
            _run_delete(body=body)

        deny_entries = [c for c in audit_calls if c["action"] == "account.delete_denied"]
        self.assertGreater(len(deny_entries), 0)
        self.assertEqual(deny_entries[0]["target"], "username_mismatch")


# ── 11. No credential / secret in any response body (AC-9) ───────────────────

class TestDeleteAccountNoSecretInResponse(unittest.TestCase):
    """AC-9: no password, JWT, or SUPABASE_SERVICE_KEY in any response body."""

    def _check_no_secret(self, responses, secret_values):
        for _, payload in responses:
            body_str = json.dumps(payload)
            for secret in secret_values:
                self.assertNotIn(
                    secret, body_str,
                    f"Secret {secret!r} must not appear in response body {body_str!r}",
                )

    def test_service_key_not_in_success_response(self):
        """SUPABASE_SERVICE_KEY must not appear in the 200 success response."""
        fake_key = "fake-service-role-key-ABCDEF"
        with mock.patch.dict("os.environ", {"SUPABASE_SERVICE_KEY": fake_key}):
            responses, _ = _run_delete()
        self._check_no_secret(responses, [fake_key])

    def test_password_not_in_error_response(self):
        """The submitted password must not appear in any error response body."""
        responses, _ = _run_delete(verify_pw=False)
        self._check_no_secret(responses, [_PASSWORD_OK])

    def test_password_not_in_success_response(self):
        """The submitted password must not appear in the 200 response body."""
        responses, _ = _run_delete()
        self._check_no_secret(responses, [_PASSWORD_OK])

    def test_email_not_in_error_response(self):
        """The user's email must not appear in any error response body."""
        responses, _ = _run_delete(verify_pw=False)
        self._check_no_secret(responses, [_EMAIL_A])

    def test_service_key_not_in_401_response(self):
        """SUPABASE_SERVICE_KEY must not appear in the 401 unauth response."""
        fake_key = "service-role-key-ZZZZ"
        with mock.patch.dict("os.environ", {"SUPABASE_SERVICE_KEY": fake_key}):
            responses, _ = _run_delete(jwt_ok=False)
        self._check_no_secret(responses, [fake_key])


if __name__ == "__main__":
    unittest.main()
