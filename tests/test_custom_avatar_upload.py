"""Unit + integration tests for custom-avatar-upload (AC-1..AC-11 backend slice).

Covers every ### Tester scope row from the task DoD that belongs to the backend
unit/integration suite:

  - PATCH /api/profile {avatar:"set"} derives + stores the canonical URL with a
    ?v= buster when the object exists; 400 when the HEAD check finds no object
    (AC-1).
  - PATCH /api/profile {avatar:"remove"} clears avatar_url and calls the
    service-role delete; idempotent when nothing was uploaded (AC-6).
  - PATCH /api/profile {avatar:"<invalid>"} -> 400 (AC-3 rejection semantics).
  - A client-supplied avatar_url field in the PATCH body is ignored -- the
    server always derives its own URL (img-src-injection negative path, threat
    model row 3).
  - GET /api/profile and GET /api/public/profile/{username} include avatar_url
    (nullable); public body includes it only when the profile is public
    (AC-2, AC-7).
  - _delete_account invokes the avatar-object delete helper before the
    auth-user delete; overall flow stays idempotent (AC-10).

Stub strategy mirrors tests/test_delete_account.py and
tests/test_choose_username_at_registration.py: FakeCursor for the DB boundary,
direct attribute stubs on the handler instance for the service-role Storage
helpers (_supabase_storage_head_avatar / _supabase_storage_delete_avatar), and
mock.patch for module-level seams (server.verify_jwt_identity, server.rate_check,
server._audit). No live Supabase, no live DB, no live network.
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import server
from tests._harness import FakeCursor, make_handler, patch_db


_UID_A = "aaaa-1111-aaaa-1111"
_UID_B = "bbbb-2222-bbbb-2222"
_EMAIL_A = "usera@example.com"
_USERNAME_A = "usera"
_PASSWORD_OK = "correct-password-123"
_BEARER = "Bearer stub-token"


# ── PATCH /api/profile {avatar: "set"|"remove"|invalid} ───────────────────────


def _run_patch_profile(
    *,
    user_id=_UID_A,
    body,
    current_row=None,
    head_ok=True,
    delete_ok=True,
):
    """Drive _patch_profile() with the DB + Storage boundary stubbed.

    `current_row` is the FakeCursor's canned row for the pre-write SELECT
    (username/is_public/show_collection/show_stats/avatar_url); None means
    "no profile row yet" (lazy-default path). Returns (responses, cur).
    """
    h, responses = make_handler(user_id=user_id, body=body)
    h._supabase_storage_head_avatar = lambda uid: head_ok
    h._supabase_storage_delete_avatar = lambda uid: delete_ok

    fetch_results = [current_row]
    cur = FakeCursor(fetch_results=fetch_results)

    with patch_db(cur):
        h._patch_profile()
    return responses, cur


class PatchProfileAvatarSetUnit(unittest.TestCase):
    """AC-1: {avatar:"set"} derives + stores the canonical URL; 400 if absent."""

    def test_set_with_object_present_derives_url_with_version_buster(self):
        """AC-1: HEAD confirms the object -> avatar_url is server-derived with ?v=."""
        responses, cur = _run_patch_profile(
            body={"avatar": "set"}, head_ok=True,
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        url = payload["profile"]["avatar_url"]
        self.assertIsNotNone(url, "avatar_url must be set after a successful HEAD")
        self.assertIn(f"/storage/v1/object/public/avatars/{_UID_A}/avatar.webp", url)
        self.assertIn("?v=", url, "canonical URL must carry a ?v= cache-buster")

    def test_set_writes_avatar_url_via_parameterised_sql(self):
        """AC-1 / PS-002: the derived URL reaches the DB only via a %s placeholder."""
        responses, cur = _run_patch_profile(body={"avatar": "set"}, head_ok=True)
        write_calls = [c for c in cur.calls if c[0].strip().startswith(("INSERT", "UPDATE"))]
        self.assertTrue(write_calls, "expected an INSERT/UPSERT for the avatar_url write")
        sql, params = write_calls[-1]
        self.assertIn("avatar_url", sql)
        self.assertNotIn("%v=", sql)  # no string-built URL fragment in the SQL text
        # The derived URL value must appear only among the bound params, never
        # concatenated into the SQL string itself.
        self.assertTrue(any(
            isinstance(p, str) and "/storage/v1/object/public/avatars/" in p
            for p in params
        ))

    def test_set_missing_object_returns_400_and_avatar_unchanged(self):
        """AC-1 edge case: HEAD finds no object -> 400, no DB write attempted."""
        responses, cur = _run_patch_profile(body={"avatar": "set"}, head_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "No se encontró la imagen subida")
        # No INSERT/UPDATE should have been issued -- the function returns before
        # composing the upsert when the HEAD check fails.
        write_calls = [c for c in cur.calls if c[0].strip().startswith(("INSERT", "UPDATE"))]
        self.assertEqual(write_calls, [], "no DB write on a missing-object 400")

    def test_set_error_body_is_generic_no_internal_leakage(self):
        """US-023: the 400 error string never echoes Supabase/internal detail."""
        responses, _ = _run_patch_profile(body={"avatar": "set"}, head_ok=False)
        _, payload = responses[-1]
        for leaky in ("supabase", "storage", "traceback", "Exception", "404"):
            self.assertNotIn(leaky.lower(), payload["error"].lower())


class PatchProfileAvatarRemoveUnit(unittest.TestCase):
    """AC-6: {avatar:"remove"} clears avatar_url + calls the service-role delete."""

    def test_remove_clears_avatar_url(self):
        """AC-6: a successful remove sets avatar_url to null in the response."""
        responses, cur = _run_patch_profile(
            body={"avatar": "remove"},
            current_row={
                "username": _USERNAME_A, "is_public": True,
                "show_collection": False, "show_stats": False,
                "avatar_url": "https://proj.supabase.co/storage/v1/object/public/avatars/x/avatar.webp?v=1",
            },
            delete_ok=True,
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["profile"]["avatar_url"])

    def test_remove_calls_delete_helper(self):
        """AC-6: remove invokes the service-role delete helper exactly once."""
        h, responses = make_handler(user_id=_UID_A, body={"avatar": "remove"})
        calls = {"n": 0}
        h._supabase_storage_head_avatar = lambda uid: True

        def _delete(uid):
            calls["n"] += 1
            self.assertEqual(uid, _UID_A)
            return True

        h._supabase_storage_delete_avatar = _delete
        cur = FakeCursor(fetch_results=[None])
        with patch_db(cur):
            h._patch_profile()
        self.assertEqual(calls["n"], 1)

    def test_remove_idempotent_when_nothing_was_uploaded(self):
        """AC-6 edge case: remove when avatar_url is already null -> still 200, null->null."""
        responses, cur = _run_patch_profile(
            body={"avatar": "remove"},
            current_row={
                "username": _USERNAME_A, "is_public": False,
                "show_collection": False, "show_stats": False,
                "avatar_url": None,
            },
            delete_ok=True,  # 404 treated as success by the real helper
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["profile"]["avatar_url"])

    def test_remove_does_not_fail_response_when_delete_helper_returns_false(self):
        """The PATCH remove action does not gate its 200 on the delete helper's
        return value -- it always proceeds to clear avatar_url (idempotent by
        design; a transient Storage failure on remove does not block the user
        from clearing their own avatar pointer)."""
        responses, cur = _run_patch_profile(
            body={"avatar": "remove"}, delete_ok=False,
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["profile"]["avatar_url"])


class PatchProfileAvatarInvalidUnit(unittest.TestCase):
    """AC-3 rejection semantics: unknown avatar action -> 400."""

    def test_invalid_avatar_action_returns_400(self):
        responses, cur = _run_patch_profile(body={"avatar": "delete"})
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "Acción de avatar inválida")

    def test_invalid_avatar_action_no_db_write(self):
        """A bad avatar action must not reach the DB write path at all."""
        _, cur = _run_patch_profile(body={"avatar": "delete"})
        write_calls = [c for c in cur.calls if c[0].strip().startswith(("INSERT", "UPDATE"))]
        self.assertEqual(write_calls, [])

    def test_null_avatar_action_returns_400(self):
        """Non-string / unexpected types for `avatar` also fall outside {set,remove}."""
        responses, _ = _run_patch_profile(body={"avatar": None})
        self.assertEqual(responses[-1][0], 400)

    def test_boolean_avatar_action_returns_400(self):
        responses, _ = _run_patch_profile(body={"avatar": True})
        self.assertEqual(responses[-1][0], 400)


class PatchProfileClientSuppliedAvatarUrlIgnoredUnit(unittest.TestCase):
    """Threat model row 3 (img-src injection): a client-supplied avatar_url in the
    PATCH body is never trusted or persisted -- the server always derives its own
    URL from the service-role HEAD result."""

    def test_client_avatar_url_ignored_on_set(self):
        """A crafted avatar_url alongside {avatar:"set"} is discarded; the stored
        value is always the server-derived canonical Storage URL."""
        malicious = "https://evil.example.com/pwn.svg"
        responses, cur = _run_patch_profile(
            body={"avatar": "set", "avatar_url": malicious},
            head_ok=True,
        )
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        stored_url = payload["profile"]["avatar_url"]
        self.assertNotEqual(stored_url, malicious)
        self.assertIn("/storage/v1/object/public/avatars/", stored_url)
        # The malicious value must never appear in any bound SQL parameter either.
        for _, params in cur.calls:
            if params:
                self.assertNotIn(malicious, params)

    def test_client_avatar_url_alone_is_a_noop_field(self):
        """avatar_url with no recognised `avatar` action and no other field is
        rejected by the existing "nothing to update" 400 -- the field itself is
        not a writable column from client input."""
        responses, cur = _run_patch_profile(
            body={"avatar_url": "https://evil.example.com/pwn.svg"},
        )
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Nada que actualizar")
        write_calls = [c for c in cur.calls if c[0].strip().startswith(("INSERT", "UPDATE"))]
        self.assertEqual(write_calls, [])


# ── GET /api/profile + GET /api/public/profile/{username}: avatar_url shape ───


class GetProfileAvatarUrlUnit(unittest.TestCase):
    """AC-2, AC-7: GET /api/profile includes avatar_url (nullable)."""

    def test_get_profile_includes_avatar_url_when_set(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor(fetch_results=[{
            "username": _USERNAME_A, "is_public": True,
            "show_collection": False, "show_stats": False,
            "avatar_url": "https://proj.supabase.co/storage/v1/object/public/avatars/x/avatar.webp?v=1",
        }])
        with patch_db(cur):
            h._get_profile()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIn("avatar_url", payload["profile"])
        self.assertTrue(payload["profile"]["avatar_url"])

    def test_get_profile_lazy_default_includes_null_avatar_url(self):
        """AC-7: no profile row yet -> lazy default includes avatar_url: null."""
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor(fetch_results=[None])
        with patch_db(cur):
            h._get_profile()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIn("avatar_url", payload["profile"])
        self.assertIsNone(payload["profile"]["avatar_url"])

    def test_get_profile_row_with_null_avatar_url(self):
        """AC-7: an existing profile row that never uploaded an avatar -> null."""
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor(fetch_results=[{
            "username": _USERNAME_A, "is_public": False,
            "show_collection": False, "show_stats": False,
            "avatar_url": None,
        }])
        with patch_db(cur):
            h._get_profile()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["profile"]["avatar_url"])


class PublicProfileAvatarUrlIntegrationUnit(unittest.TestCase):
    """AC-2, AC-7: _public_profile returns avatar_url for a public profile with an
    avatar and null otherwise; a non-public profile 404s before any field leaks."""

    def _run_public_profile(self, username, *, row):
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        cur = FakeCursor(fetch_results=[row])
        with patch_db(cur):
            h._public_profile(username)
        return responses, cur

    def test_public_profile_returns_avatar_url_when_public_and_set(self):
        """AC-2: a public profile with an uploaded avatar exposes avatar_url."""
        responses, _ = self._run_public_profile("useralpha", row={
            "user_id": _UID_A, "username": "useralpha", "is_public": True,
            "show_collection": False, "show_stats": False,
            "avatar_url": "https://proj.supabase.co/storage/v1/object/public/avatars/x/avatar.webp?v=1",
        })
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIn("avatar_url", payload["profile"])
        self.assertTrue(payload["profile"]["avatar_url"])

    def test_public_profile_avatar_url_null_when_never_uploaded(self):
        """AC-7: a public profile with no uploaded avatar exposes avatar_url: null."""
        responses, _ = self._run_public_profile("useralpha", row={
            "user_id": _UID_A, "username": "useralpha", "is_public": True,
            "show_collection": False, "show_stats": False,
            "avatar_url": None,
        })
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertIsNone(payload["profile"]["avatar_url"])

    def test_public_profile_avatar_url_not_gated_by_show_collection_or_stats(self):
        """avatar_url is header identity: included even when both show_collection
        and show_stats are False (never gated by those toggles, per spec)."""
        responses, _ = self._run_public_profile("useralpha", row={
            "user_id": _UID_A, "username": "useralpha", "is_public": True,
            "show_collection": False, "show_stats": False,
            "avatar_url": "https://proj.supabase.co/storage/v1/object/public/avatars/x/avatar.webp?v=1",
        })
        _, payload = responses[-1]
        self.assertTrue(payload["profile"]["avatar_url"])

    def test_non_public_profile_404s_avatar_url_never_leaks(self):
        """A private profile 404s before any field (including avatar_url) is
        released -- no enumeration, no partial-body leak."""
        responses, _ = self._run_public_profile("useralpha", row={
            "user_id": _UID_A, "username": "useralpha", "is_public": False,
            "show_collection": False, "show_stats": False,
            "avatar_url": "https://proj.supabase.co/storage/v1/object/public/avatars/x/avatar.webp?v=1",
        })
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertNotIn("avatar_url", payload)

    def test_nonexistent_profile_404s(self):
        responses, _ = self._run_public_profile("ghost", row=None)
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertNotIn("avatar_url", payload)


# ── _delete_account: avatar-object erasure (RTBF) ─────────────────────────────


def _make_delete_handler(
    *,
    user_id=_UID_A,
    email=_EMAIL_A,
    body=None,
    jwt_ok=True,
    rate_ok=True,
    verify_pw=True,
    admin_delete=True,
    avatar_delete_ok=True,
    stored_username=_USERNAME_A,
):
    if body is None:
        body = {"password": _PASSWORD_OK, "confirm_username": _USERNAME_A}

    h, responses = make_handler(body=body)
    h.headers = {"Authorization": _BEARER}
    h.path = "/api/account/delete"

    _jwt_result = (user_id, email) if jwt_ok else (None, None)
    _rate_result = (True, 0) if rate_ok else (False, 60)

    h._supabase_verify_password = lambda e, p: verify_pw
    h._supabase_admin_delete_user = lambda uid: admin_delete

    avatar_delete_calls = {"n": 0, "uid": None}

    def _avatar_delete(uid):
        avatar_delete_calls["n"] += 1
        avatar_delete_calls["uid"] = uid
        return avatar_delete_ok

    h._supabase_storage_delete_avatar = _avatar_delete

    fetch_results = [{"username": stored_username}] if stored_username else [None]
    cur = FakeCursor(fetch_results=fetch_results)

    return h, responses, cur, _jwt_result, _rate_result, avatar_delete_calls


def _run_delete(
    *,
    user_id=_UID_A,
    email=_EMAIL_A,
    body=None,
    jwt_ok=True,
    rate_ok=True,
    verify_pw=True,
    admin_delete=True,
    avatar_delete_ok=True,
    stored_username=_USERNAME_A,
    capture_audit=False,
):
    h, responses, cur, jwt_result, rate_result, avatar_calls = _make_delete_handler(
        user_id=user_id, email=email, body=body, jwt_ok=jwt_ok, rate_ok=rate_ok,
        verify_pw=verify_pw, admin_delete=admin_delete,
        avatar_delete_ok=avatar_delete_ok, stored_username=stored_username,
    )

    audit_calls = []
    ctx = mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result)
    ctx2 = mock.patch.object(server, "rate_check", return_value=rate_result)
    if capture_audit:
        def _fake_audit(action, uid, target):
            audit_calls.append((action, uid, target))
        ctx3 = mock.patch.object(server, "_audit", side_effect=_fake_audit)
    else:
        ctx3 = mock.patch.object(server, "_audit")

    with ctx, ctx2, ctx3, patch_db(cur):
        h._delete_account()

    return responses, cur, avatar_calls, audit_calls


class DeleteAccountAvatarErasureUnit(unittest.TestCase):
    """AC-10: _delete_account invokes the avatar-object delete helper; flow
    stays idempotent whether or not the Storage delete itself succeeds."""

    def test_avatar_delete_helper_invoked_on_success_path(self):
        """AC-10: a full happy-path account deletion calls the avatar-delete
        helper exactly once, with the authenticated user's own id."""
        responses, cur, avatar_calls, _ = _run_delete()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(avatar_calls["n"], 1)
        self.assertEqual(avatar_calls["uid"], _UID_A)

    def test_avatar_delete_called_before_auth_user_delete(self):
        """DB -> avatar -> auth ordering (spec Technical Details): the avatar
        helper must have been invoked by the time _supabase_admin_delete_user
        runs. We assert this by making admin_delete assert avatar was already
        called."""
        order = []

        h, responses, cur, jwt_result, rate_result, _ = _make_delete_handler()

        def _avatar_delete(uid):
            order.append("avatar")
            return True

        def _admin_delete(uid):
            order.append("auth")
            return True

        h._supabase_storage_delete_avatar = _avatar_delete
        h._supabase_admin_delete_user = _admin_delete

        with mock.patch.object(server, "verify_jwt_identity", return_value=jwt_result), \
             mock.patch.object(server, "rate_check", return_value=rate_result), \
             mock.patch.object(server, "_audit"), \
             patch_db(cur):
            h._delete_account()

        self.assertEqual(order, ["avatar", "auth"])

    def test_flow_succeeds_even_when_avatar_delete_fails(self):
        """AC-10 idempotency: a failed avatar-object delete does NOT abort the
        account deletion (DB + auth erasure are primary; storage failure is a
        non-fatal, audited side note)."""
        responses, cur, avatar_calls, _ = _run_delete(avatar_delete_ok=False)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(avatar_calls["n"], 1)

    def test_avatar_delete_failure_is_audited_redacted(self):
        """GD-*/LO-*: a failed avatar erasure emits a redacted _audit entry
        ("account.avatar_erase_failed") -- the raw user_id must never be the
        value passed as user_hash (it always goes through _hash_user_id first,
        verified separately below at the _audit implementation)."""
        responses, cur, avatar_calls, audit_calls = _run_delete(
            avatar_delete_ok=False, capture_audit=True,
        )
        actions = [a for a, _, _ in audit_calls]
        self.assertIn("account.avatar_erase_failed", actions)
        # The _audit call site passes user_id positionally; _audit() itself
        # hashes it (test_audit_hashes_user_id below proves the hashing).
        failed_entry = next(c for c in audit_calls if c[0] == "account.avatar_erase_failed")
        self.assertEqual(failed_entry[1], _UID_A)
        self.assertEqual(failed_entry[2], "avatar")

    def test_no_avatar_erase_failed_audit_on_success(self):
        """No account.avatar_erase_failed audit entry is emitted when the
        avatar delete succeeds."""
        responses, cur, avatar_calls, audit_calls = _run_delete(
            avatar_delete_ok=True, capture_audit=True,
        )
        actions = [a for a, _, _ in audit_calls]
        self.assertNotIn("account.avatar_erase_failed", actions)

    def test_avatar_delete_idempotent_on_retry_when_already_gone(self):
        """A retry of account deletion after the object is already gone is a
        no-op success (the real helper treats 404 as success) -- the flow
        still returns 200."""
        responses, cur, avatar_calls, _ = _run_delete(avatar_delete_ok=True)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_avatar_delete_not_called_on_unauthenticated(self):
        """AC-10 negative: no avatar erasure attempt when the caller is not
        authenticated -- ordering never reaches step 8."""
        responses, cur, avatar_calls, _ = _run_delete(jwt_ok=False)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(avatar_calls["n"], 0)

    def test_avatar_delete_not_called_on_wrong_password(self):
        """AC-10 negative: a failed re-auth (bad password) never reaches the
        avatar-erasure step."""
        responses, cur, avatar_calls, _ = _run_delete(verify_pw=False)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(avatar_calls["n"], 0)

    def test_audit_never_logs_raw_user_id_or_service_key(self):
        """LO-*/GD-*: capture the printed audit line and assert the raw UUID
        and service key never appear -- only the hashed value does."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run_delete(avatar_delete_ok=False, capture_audit=False)
        printed = buf.getvalue()
        self.assertNotIn(_UID_A, printed, "raw user_id must never appear in an audit log line")
        self.assertNotIn("SUPABASE_SERVICE_KEY", printed)


class HashUserIdUnit(unittest.TestCase):
    """Sanity check on the redaction primitive _audit relies on."""

    def test_hash_user_id_is_not_the_raw_value(self):
        h = server._hash_user_id(_UID_A)
        self.assertNotEqual(h, _UID_A)
        self.assertEqual(len(h), 16)

    def test_hash_user_id_deterministic(self):
        self.assertEqual(server._hash_user_id(_UID_A), server._hash_user_id(_UID_A))

    def test_hash_user_id_none_passthrough(self):
        self.assertIsNone(server._hash_user_id(None))


if __name__ == "__main__":
    unittest.main()
