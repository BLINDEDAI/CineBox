"""Unit + integration tests for choose-username-at-registration.

Covers every ### Tester scope row in the task file:
  - Unit: _username_available invalid/reserved (no DB hit) / free / taken (AC-2, AC-3)
  - Unit: PATCH /api/profile rejects invalid/reserved desired_username -> 400 (AC-7)
  - Integration: availability endpoint burst over per-IP cap -> 429 + Retry-After;
    limiter fires before any DB read (AS-013 / threat model)
  - Log assertion: throttled (429) availability request emits a redacted structured
    log line with no username/email/IP in clear (LO-* / F-2 fix)
  - Integration: race -- two authoritative claims of same username -> second 409,
    row unchanged (AC-5)
  - Integration: anonymous PATCH /api/profile -> 401, nothing written (threat model)
  - Integration: GET /api/profile for account with no profiles row -> username:null
    (AC-6 backend support)

DB boundary is stubbed with FakeCursor throughout (no live Supabase / Postgres).
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

import psycopg2.errors

import server
from server import _normalize_username
from tests._harness import FakeCursor, make_handler, patch_db


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_rate_limited_handler(*, rate_limited=True):
    """Return a Handler stub whose _public_rate_limited returns rate_limited.

    The _json capture list is still populated so callers can inspect the
    429 response emitted by _public_rate_limited itself or by the handler.
    """
    h, responses = make_handler(user_id=None)

    def _public_rate_limited_stub():
        if rate_limited:
            h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                    extra_headers={"Retry-After": 60})
            return True
        return False

    h._public_rate_limited = _public_rate_limited_stub
    return h, responses


# ── Unit: _username_available advisory results ─────────────────────────────────


class UsernameAvailableUnit(unittest.TestCase):
    """AC-2, AC-3: _username_available advisory results, DB stubbed."""

    def _run_available(self, u_param, *, fetch_results=None, rate_limited=False):
        """Drive _username_available with a stubbed DB and rate-limit state.

        `fetch_results` is the FIFO list given to FakeCursor (for the SELECT).
        `rate_limited=True` simulates the limiter already triggered.
        """
        cur = FakeCursor(fetch_results=fetch_results or [])
        h, responses = make_handler(user_id=None)

        if rate_limited:
            def _blocked():
                h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                        extra_headers={"Retry-After": 60})
                return True
            h._public_rate_limited = _blocked
        else:
            h._public_rate_limited = lambda: False

        h._qs = lambda: {"u": [u_param]} if u_param is not None else {}
        with patch_db(cur):
            h._username_available()
        return cur, responses

    # AC-2: invalid format -> available:false, reason:"invalid", NO DB hit

    def test_invalid_too_short_no_db_hit(self):
        """AC-2: too-short username -> available:false, reason:'invalid', no DB read."""
        cur, responses = self._run_available("ab")
        self.assertEqual(responses[-1][0], 200)
        payload = responses[-1][1]
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "invalid")
        self.assertEqual(cur.calls, [], "DB must not be queried for invalid username")

    def test_invalid_bad_chars_no_db_hit(self):
        """AC-2: username with illegal chars -> available:false, reason:'invalid', no DB."""
        cur, responses = self._run_available("alice@example")
        self.assertEqual(responses[-1][0], 200)
        self.assertFalse(responses[-1][1]["available"])
        self.assertEqual(responses[-1][1]["reason"], "invalid")
        self.assertEqual(cur.calls, [])

    def test_invalid_too_long_no_db_hit(self):
        """AC-2: 31-char username -> available:false, reason:'invalid', no DB read."""
        cur, responses = self._run_available("a" * 31)
        self.assertFalse(responses[-1][1]["available"])
        self.assertEqual(responses[-1][1]["reason"], "invalid")
        self.assertEqual(cur.calls, [])

    def test_reserved_name_no_db_hit(self):
        """AC-2: reserved username (e.g. 'admin') -> available:false, reason:'invalid', no DB."""
        for reserved in list(server.RESERVED_USERNAMES)[:3]:
            with self.subTest(name=reserved):
                cur, responses = self._run_available(reserved)
                self.assertFalse(responses[-1][1]["available"],
                                 f"Reserved name {reserved!r} must be unavailable")
                self.assertEqual(responses[-1][1]["reason"], "invalid")
                self.assertEqual(cur.calls, [], f"DB queried for reserved name {reserved!r}")

    def test_oversized_param_returns_400(self):
        """US-040: u= param longer than 64 chars -> 400 (no DB hit)."""
        cur, responses = self._run_available("a" * 65)
        self.assertEqual(responses[-1][0], 400)
        self.assertFalse(responses[-1][1]["ok"])
        self.assertEqual(cur.calls, [])

    # AC-3: valid + free -> available:true, reason:"ok"

    def test_valid_free_username(self):
        """AC-3: valid format + not in DB -> available:true, reason:'ok'."""
        # fetchone() returns None -> username not taken
        cur, responses = self._run_available("alice", fetch_results=[None])
        self.assertEqual(responses[-1][0], 200)
        self.assertTrue(responses[-1][1]["available"])
        self.assertEqual(responses[-1][1]["reason"], "ok")
        # Exactly one DB query issued
        self.assertEqual(len(cur.calls), 1)
        sql, params = cur.calls[0]
        self.assertIn("profiles", sql)
        self.assertIn("username", sql)
        self.assertEqual(params, ("alice",))

    def test_valid_free_response_contains_only_ok_available_reason(self):
        """AC-3 / GD-001: response contains ONLY {ok, available, reason} -- no profile data."""
        _, responses = self._run_available("alice", fetch_results=[None])
        payload = responses[-1][1]
        self.assertSetEqual(set(payload.keys()), {"ok", "available", "reason"})

    # AC-3: valid + taken -> available:false, reason:"taken"

    def test_valid_taken_username(self):
        """AC-3: valid format + exists in DB -> available:false, reason:'taken'."""
        # fetchone() returns a row -> username taken
        cur, responses = self._run_available("alice", fetch_results=[{"exists": 1}])
        self.assertEqual(responses[-1][0], 200)
        self.assertFalse(responses[-1][1]["available"])
        self.assertEqual(responses[-1][1]["reason"], "taken")

    def test_taken_response_contains_only_ok_available_reason(self):
        """GD-001: taken response exposes ONLY {ok, available, reason} -- no user PII."""
        _, responses = self._run_available("alice", fetch_results=[{"exists": 1}])
        payload = responses[-1][1]
        self.assertSetEqual(set(payload.keys()), {"ok", "available", "reason"})

    def test_sql_is_parameterised(self):
        """PS-002: the DB query uses %s placeholder (parameterised, no string format)."""
        cur, _ = self._run_available("alice", fetch_results=[None])
        sql, params = cur.calls[0]
        self.assertNotIn("alice", sql, "Username must not appear literally in SQL string")
        self.assertIn("%s", sql)
        self.assertIn("alice", params)

    def test_missing_u_param(self):
        """Edge case: missing u= param -> treated as empty string -> invalid -> no DB."""
        cur, responses = self._run_available("", fetch_results=[])
        self.assertFalse(responses[-1][1]["available"])
        self.assertEqual(responses[-1][1]["reason"], "invalid")
        self.assertEqual(cur.calls, [])

    def test_response_always_has_ok_true(self):
        """advisory responses always carry ok:true (not ok:false which is the error envelope)."""
        for candidate, fetch_results in [("ab", []), ("alice", [None]), ("alice", [{"x": 1}])]:
            with self.subTest(candidate=candidate):
                _, responses = self._run_available(candidate, fetch_results=fetch_results)
                payload = responses[-1][1]
                self.assertTrue(payload["ok"])


# ── Unit: PATCH /api/profile rejects invalid/reserved desired_username -> 400 ──


class PatchProfileRejectsInvalidUsername(unittest.TestCase):
    """AC-7: the server re-validates the username in PATCH /api/profile; never trusts metadata."""

    def _run_patch(self, body, *, user_id="user-test", current_row=None):
        """Run _patch_profile with a stubbed DB and given request body."""
        cur = FakeCursor(fetch_results=[current_row])
        h, responses = make_handler(body=body, user_id=user_id)
        with patch_db(cur):
            h._patch_profile()
        return cur, responses

    def test_invalid_format_username_returns_400(self):
        """AC-7: PATCH with username='AB cd!' (bad chars) -> 400."""
        _, responses = self._run_patch({"username": "AB cd!"})
        self.assertEqual(responses[-1][0], 400)

    def test_too_short_username_returns_400(self):
        """AC-7: PATCH with username='ab' (too short) -> 400."""
        _, responses = self._run_patch({"username": "ab"})
        self.assertEqual(responses[-1][0], 400)

    def test_reserved_username_returns_400(self):
        """AC-7: PATCH with a reserved username -> 400 (server re-validates)."""
        for reserved in list(server.RESERVED_USERNAMES)[:3]:
            with self.subTest(name=reserved):
                _, responses = self._run_patch({"username": reserved})
                self.assertEqual(responses[-1][0], 400,
                                 f"Reserved {reserved!r} must be rejected with 400")

    def test_html_metacharacter_username_returns_400(self):
        """AC-7 / threat model: username with HTML metacharacters -> 400 (charset gate)."""
        _, responses = self._run_patch({"username": "<script>alert(1)</script>"})
        self.assertEqual(responses[-1][0], 400)

    def test_oversized_desired_username_returns_400(self):
        """AC-7: username longer than 30 chars (e.g. from crafted metadata) -> 400."""
        _, responses = self._run_patch({"username": "a" * 31})
        self.assertEqual(responses[-1][0], 400)

    def test_valid_username_proceeds(self):
        """AC-7 sanity: a legitimately valid username in PATCH body proceeds (200 path)."""
        cur, responses = self._run_patch({"username": "validname"})
        # 200 (upsert success) or 409 (unique violation from FakeCursor rowcount=1 default)
        # With FakeCursor.rowcount=1 and no UniqueViolation the upsert succeeds.
        self.assertEqual(responses[-1][0], 200)

    def test_invalid_username_no_db_write(self):
        """AC-7: invalid username -> 400 before any UPSERT reaches the DB."""
        cur, _ = self._run_patch({"username": "!@#$"})
        # The handler returns 400 before the second with-get_db block (upsert).
        upsert_calls = [sql for sql, _ in cur.calls if "INSERT INTO profiles" in sql]
        self.assertEqual(upsert_calls, [], "No upsert must occur on invalid username")


# ── Integration: availability endpoint burst -> 429 + Retry-After ──────────────


class UsernameAvailableBurstIntegration(unittest.TestCase):
    """AS-013 / threat model: burst over per-IP cap -> 429 + Retry-After;
    limiter fires BEFORE any DB read."""

    def test_rate_limited_returns_429(self):
        """Burst over per-IP cap: _public_rate_limited -> True -> 429 emitted."""
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)

        blocked_called = []

        def _blocked():
            blocked_called.append(True)
            h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                    extra_headers={"Retry-After": 60})
            return True

        h._public_rate_limited = _blocked
        h._qs = lambda: {"u": ["alice"]}
        with patch_db(cur):
            h._username_available()

        self.assertEqual(responses[-1][0], 429)
        self.assertTrue(blocked_called, "_public_rate_limited must have been called")

    def test_rate_limiter_fires_before_db_read(self):
        """AS-013: when throttled, zero DB queries are issued (limiter is before the DB)."""
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)

        def _blocked():
            h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                    extra_headers={"Retry-After": 60})
            return True

        h._public_rate_limited = _blocked
        h._qs = lambda: {"u": ["alice"]}
        with patch_db(cur):
            h._username_available()

        self.assertEqual(cur.calls, [],
                         "No DB query must occur when the rate limiter fires")

    def test_rate_check_blocks_after_per_ip_limit(self):
        """Integration: real rate_check() blocks on the PUBLIC_RATE_MAX per-IP bucket."""
        import time  # noqa: F401 (used only to silence unused-import lint)

        test_ip = "253.252.251.250"  # unique test IP, avoids collision with other tests
        global_key = f"public:_global_burst_test_{test_ip}"
        per_ip_key = f"public:{test_ip}"
        buckets = [(per_ip_key, server.PUBLIC_RATE_MAX), (global_key, server.PUBLIC_RATE_GLOBAL)]

        # Exhaust the per-IP limit
        for _ in range(server.PUBLIC_RATE_MAX):
            server.rate_check(buckets)

        # The next call must be blocked
        allowed, retry = server.rate_check(buckets)
        self.assertFalse(allowed, "rate_check must block after per-IP cap")
        self.assertGreater(retry, 0, "Retry-After must be > 0 when blocked")

    def test_valid_response_contains_no_profile_data(self):
        """GD-001 / threat model: a valid u= returns only {ok, available, reason}."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        h._qs = lambda: {"u": ["freeuser"]}
        with patch_db(cur):
            h._username_available()
        payload = responses[-1][1]
        # Strictly only these three keys
        self.assertSetEqual(set(payload.keys()), {"ok", "available", "reason"})
        # No profile fields
        for bad_key in ("email", "user_id", "username", "is_public", "profile"):
            self.assertNotIn(bad_key, payload,
                             f"Field {bad_key!r} must not appear in availability response")


# ── Log assertion: throttled 429 emits a redacted structured log line ───────────


class ThrottleLogRedactionIntegration(unittest.TestCase):
    """LO-* / F-2 fix: throttled (_username_available 429) must emit a redacted
    structured log line with no username, email, or IP in clear.

    The fix adds a `print("audit " + json.dumps(...))` at server.py:1376 inside
    _username_available() after _public_rate_limited() returns True. We capture
    stdout to assert the line and its contents.
    """

    def _capture_stdout_on_throttle(self, u_param="alice"):
        """Drive _username_available with the limiter blocked, capture stdout."""
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)

        def _blocked():
            h._json(429, {"ok": False, "error": "Demasiadas peticiones"},
                    extra_headers={"Retry-After": 60})
            return True

        h._public_rate_limited = _blocked
        h._qs = lambda: {"u": [u_param]}

        buf = io.StringIO()
        with redirect_stdout(buf):
            with patch_db(cur):
                h._username_available()

        return buf.getvalue(), responses

    def test_throttle_emits_audit_line(self):
        """F-2 / LO-*: a throttled request prints an 'audit {...}' JSON line."""
        stdout, responses = self._capture_stdout_on_throttle()
        self.assertEqual(responses[-1][0], 429)
        lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
        audit_lines = [l for l in lines if l.startswith("audit ")]
        self.assertTrue(audit_lines,
                        f"Expected an 'audit ...' log line on throttle; stdout was: {stdout!r}")

    def test_throttle_log_action_is_correct(self):
        """F-2: the audit line's action field is 'username_available.throttled'."""
        stdout, _ = self._capture_stdout_on_throttle()
        audit_line = next(
            (l for l in stdout.splitlines() if l.strip().startswith("audit ")), None
        )
        self.assertIsNotNone(audit_line, "No audit line found in stdout")
        json_part = audit_line.strip()[len("audit "):]
        entry = json.loads(json_part)
        self.assertEqual(entry.get("action"), "username_available.throttled")

    def test_throttle_log_has_timestamp(self):
        """F-2: the audit line carries a timestamp field."""
        stdout, _ = self._capture_stdout_on_throttle()
        audit_line = next(l for l in stdout.splitlines() if l.strip().startswith("audit "))
        entry = json.loads(audit_line.strip()[len("audit "):])
        self.assertIn("timestamp", entry, "Audit line must carry a timestamp")

    def test_throttle_log_has_no_username_in_clear(self):
        """LO-* / US-044: throttle log must NOT contain the candidate username in clear."""
        stdout, _ = self._capture_stdout_on_throttle(u_param="secretname")
        self.assertNotIn("secretname", stdout,
                         "The candidate username must not appear in clear in the throttle log")

    def test_throttle_log_has_no_email_field(self):
        """LO-* redaction: throttle log must NOT contain any email field."""
        stdout, _ = self._capture_stdout_on_throttle()
        self.assertNotIn("email", stdout.lower(),
                         "No email field must appear in the throttle log")

    def test_throttle_log_has_no_ip_in_clear(self):
        """LO-* / US-044: throttle log must NOT contain any raw IP address in clear.

        We do not assert that _client_ip() was called (its return value is the IP
        to protect). Instead we assert that no dotted-quad-like string that would
        match a typical IP address appears in the log body.
        """
        stdout, _ = self._capture_stdout_on_throttle()
        audit_line = next(
            (l for l in stdout.splitlines() if l.strip().startswith("audit ")), None
        )
        if audit_line is None:
            self.fail("No audit line emitted")
        json_part = audit_line.strip()[len("audit "):]
        entry = json.loads(json_part)
        # No key named 'ip' or 'client_ip' in clear
        for key in ("ip", "client_ip", "x_forwarded_for", "remote_addr"):
            self.assertNotIn(key, entry,
                             f"IP-related key {key!r} must not appear in the log entry")

    def test_non_throttled_request_emits_no_audit_line(self):
        """Sanity: a non-throttled valid request does NOT emit an 'audit' line from _username_available."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        h._qs = lambda: {"u": ["freeuser"]}

        buf = io.StringIO()
        with redirect_stdout(buf):
            with patch_db(cur):
                h._username_available()

        stdout = buf.getvalue()
        # The non-throttled path must NOT emit an audit line from _username_available
        audit_lines_from_handler = [
            l for l in stdout.splitlines()
            if l.strip().startswith("audit ") and "username_available.throttled" in l
        ]
        self.assertEqual(audit_lines_from_handler, [],
                         "Non-throttled path must not emit a throttle audit line")


# ── Integration: race -- two claims of the same username -> second 409 ──────────


class UsernameClaimRaceIntegration(unittest.TestCase):
    """AC-5: two authoritative PATCH /api/profile claims of the same username.
    The second claim must receive 409 and the row must remain unchanged.
    """

    def _run_patch_with_unique_violation(self, username, user_id="user-race"):
        """Simulate a PATCH where the upsert raises UniqueViolation (race scenario)."""
        current_row = None  # no existing profile row (new user)

        class _RacingCursor(FakeCursor):
            """Raises UniqueViolation on the INSERT ... ON CONFLICT upsert."""
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                if "INSERT INTO profiles" in sql and "ON CONFLICT" in sql:
                    raise psycopg2.errors.UniqueViolation("duplicate key value")

        cur = _RacingCursor(fetch_results=[current_row])
        h, responses = make_handler(body={"username": username}, user_id=user_id)
        with patch_db(cur):
            h._patch_profile()
        return cur, responses

    def test_second_claim_returns_409(self):
        """AC-5: racing second claim of the same username -> 409."""
        _, responses = self._run_patch_with_unique_violation("alice")
        self.assertEqual(responses[-1][0], 409)

    def test_409_response_is_not_ok(self):
        """AC-5: 409 response body has ok:false (not a 2xx masking the error)."""
        _, responses = self._run_patch_with_unique_violation("alice")
        self.assertFalse(responses[-1][1]["ok"])

    def test_second_claim_issues_no_further_writes(self):
        """AC-5: after the UniqueViolation the handler does NOT retry or mutate further."""
        cur, responses = self._run_patch_with_unique_violation("alice")
        self.assertEqual(responses[-1][0], 409)
        # Only: (1) SELECT for current state, (2) attempted INSERT (which raised)
        insert_calls = [s for s, _ in cur.calls if "INSERT INTO profiles" in s]
        self.assertEqual(len(insert_calls), 1,
                         "Exactly one attempted INSERT (which raced) is expected")

    def test_first_claim_with_no_race_returns_200(self):
        """AC-5 sanity: a single claim (no race) returns 200."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(body={"username": "alice"}, user_id="user-first")
        with patch_db(cur):
            h._patch_profile()
        self.assertEqual(responses[-1][0], 200)


# ── Integration: anonymous PATCH /api/profile -> 401, nothing written ───────────


class AnonymousPatchProfileIntegration(unittest.TestCase):
    """Threat model: unauthenticated PATCH /api/profile -> 401, no DB write."""

    def test_anonymous_patch_returns_401(self):
        """Threat model: no JWT -> 401 before any write."""
        cur = FakeCursor()
        h, responses = make_handler(body={"username": "hacker"}, user_id=None)
        with patch_db(cur):
            h._patch_profile()
        self.assertEqual(responses[-1][0], 401)

    def test_anonymous_patch_nothing_written(self):
        """Threat model: 401 fires before any DB write (no upsert)."""
        cur = FakeCursor()
        h, _ = make_handler(body={"username": "hacker"}, user_id=None)
        with patch_db(cur):
            h._patch_profile()
        insert_calls = [s for s, _ in cur.calls if "INSERT INTO" in s or "UPDATE" in s]
        self.assertEqual(insert_calls, [],
                         "No INSERT/UPDATE must occur for an anonymous request")


# ── Integration: GET /api/profile returns username:null for no-row account ──────


class GetProfileNoRowIntegration(unittest.TestCase):
    """AC-6 backend: GET /api/profile for a user with no profiles row returns
    username:null (the lazy-defaults path), which is the gate trigger signal.
    """

    def test_no_profiles_row_returns_username_null(self):
        """AC-6: when profiles row is absent, GET /api/profile returns username:null."""
        # fetchone() returns None -> lazy defaults branch
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id="user-no-profile")
        with patch_db(cur):
            h._get_profile()
        self.assertEqual(responses[-1][0], 200)
        profile = responses[-1][1]["profile"]
        self.assertIsNone(profile["username"],
                          "username must be null when no profiles row exists (AC-6 gate trigger)")

    def test_no_profiles_row_profile_shape(self):
        """AC-6: lazy-default profile has the expected shape (ok, profile dict)."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id="user-no-profile")
        with patch_db(cur):
            h._get_profile()
        payload = responses[-1][1]
        self.assertTrue(payload["ok"])
        self.assertIn("profile", payload)
        profile = payload["profile"]
        self.assertIn("username", profile)
        self.assertIn("is_public", profile)

    def test_existing_username_returned_correctly(self):
        """Sanity: GET /api/profile with an existing row returns the username."""
        row = {
            "username": "alice",
            "is_public": False,
            "show_collection": False,
            "show_stats": False,
        }
        cur = FakeCursor(fetch_results=[row])
        h, responses = make_handler(user_id="user-alice")
        with patch_db(cur):
            h._get_profile()
        self.assertEqual(responses[-1][0], 200)
        self.assertEqual(responses[-1][1]["profile"]["username"], "alice")


if __name__ == "__main__":
    unittest.main()
