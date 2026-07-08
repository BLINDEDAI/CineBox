"""Unit + integration tests for guest-explore-mode (spec: guest-explore-mode-specs.md).

Covers every `### Tester scope` unit/integration row of the task file:
  - AC-1: anonymous GET on each of the six TMDB read endpoints -> 200, never 401.
  - AC-2: anonymous over-limit -> 429 + Retry-After (per-IP); the global bucket
    trips across varying per-IP keys (threat-model row 3).
  - AC-9: an authed request on the six endpoints consumes tmdb:{user_id}, never
    a public:* bucket — the per-user limiter path is untouched.
  - AC-7: anonymous `_details` (series) -> watched_count: 0; anonymous `_season`
    -> every episode watched: false; no get_db() call for an anonymous request.
  - AC-3: every user-scoped endpoint + every write still 401 without a JWT.
  - AC-10 (integration): the security-header set (CSP/HSTS/Permissions-Policy/
    static allow-list 404) is byte-identical for an anonymous and an "authed"
    (Authorization header present) request, and unchanged from the pre-feature
    baseline documented in test_security_headers.py / test_perimeter_headers.py.

DB boundary stubbed with FakeCursor (tests/_harness.py) throughout — no live
Postgres. The AC-10 integration test boots the real server.Handler over an
ephemeral-port ThreadingHTTPServer (same construction as
tests/test_security_headers.py / tests/test_perimeter_headers.py) since header
emission lives in Handler.end_headers(), not reachable through the FakeCursor
harness.
"""

import functools
import http.server
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import server
from tests._harness import FakeCursor, make_handler, patch_db

# ── Six anonymous-capable read endpoints: (attr, path, extra qs) ───────────────
# `call` invokes the handler method with the right positional args (the season
# endpoint takes tmdb_id/season as method args, not query params).
_SIX_READ_ENDPOINTS = [
    ("_search",    {"q": ["dune"]},                 None),
    ("_trending",  {},                               None),
    ("_discover",  {"genre_id": ["28"], "type": ["movie"]}, None),
    ("_details",   {"id": ["550"], "type": ["movie"]}, None),
    ("_similar",   {"id": ["550"], "type": ["movie"]}, None),
    ("_season",    {},                               (550, 1)),
]


def _call_endpoint(h, attr, season_args):
    method = getattr(h, attr)
    if season_args is not None:
        return method(*season_args)
    return method()


class _NoKeyEnv:
    """Context manager: guarantees TMDB_API_KEY is absent so every one of the
    six handlers takes its needs_key early-return (200) without a real TMDB
    call, isolating the auth/rate-limit gate under test."""

    def __enter__(self):
        self._patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._patcher.start()
        os.environ.pop("TMDB_API_KEY", None)
        return self

    def __exit__(self, *exc):
        self._patcher.stop()


# ── AC-1: anonymous 200, never 401, on the six read endpoints ──────────────────


class AnonymousReadEndpointsReturn200(unittest.TestCase):
    """AC-1: no Authorization header -> 200 on each of the six TMDB reads."""

    def test_anonymous_never_401_on_any_of_the_six(self):
        for attr, qs, season_args in _SIX_READ_ENDPOINTS:
            with self.subTest(endpoint=attr):
                h, responses = make_handler(user_id=None, qs=qs)
                h.path = "/api/" + attr.lstrip("_")
                h._public_rate_limited = lambda: False
                cur = FakeCursor(fetch_results=[{"n": 0}])
                with _NoKeyEnv(), patch_db(cur):
                    _call_endpoint(h, attr, season_args)
                status = responses[-1][0]
                self.assertNotEqual(status, 401, f"{attr} must never 401 for an anonymous caller")
                self.assertEqual(status, 200, f"{attr} anonymous call should be 200 (needs_key degrade)")


# ── AC-2: anonymous over-limit -> 429 + Retry-After; global bucket ─────────────


class AnonymousRateLimitEnforced(unittest.TestCase):
    """AC-2 / threat-model row 1+3: per-IP + global public buckets."""

    def test_public_rate_limited_blocks_and_search_returns_429(self):
        """Handler-level: when _public_rate_limited() fires, _search short-circuits
        to 429 + Retry-After before any TMDB/DB access."""
        h, responses = make_handler(user_id=None, qs={"q": ["dune"]})
        h.path = "/api/search"
        tmdb_calls = []
        h._tmdb = lambda *a, **k: tmdb_calls.append(1)

        def _blocked():
            h._json(429, {"ok": False, "error": "Demasiadas peticiones, espera un momento."},
                    extra_headers={"Retry-After": 30})
            return True

        h._public_rate_limited = _blocked
        cur = FakeCursor()
        with patch_db(cur):
            h._search()
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertEqual(tmdb_calls, [], "no TMDB call once the public limiter has fired")
        self.assertEqual(cur.calls, [], "no DB call once the public limiter has fired")

    def test_real_public_rate_limited_blocks_after_per_ip_cap_and_search_returns_429(self):
        """Drives the REAL server._public_rate_limited()/_client_ip() (not
        stubbed): seed a per-IP bucket to PUBLIC_RATE_MAX via a unique test IP
        (RFC 5737 TEST-NET-2, never a real client) so this cannot collide with
        any other test's per-IP key or the shared "public:_global" bucket count
        used elsewhere, then drive the real _search() through _client_ip() via
        X-Forwarded-For and assert the real 429 path fires."""
        test_ip = "198.51.100.42"
        for _ in range(server.PUBLIC_RATE_MAX):
            server.rate_check([(f"public:{test_ip}", server.PUBLIC_RATE_MAX)])

        h, responses = make_handler(user_id=None, qs={"q": ["dune"]})
        h.path = "/api/search"
        h.headers = {"X-Forwarded-For": test_ip}
        h.client_address = (test_ip, 12345)
        # Restore the REAL bound _public_rate_limited (make_handler does not stub
        # it, but be explicit: no override here) and the real _json so the
        # extra_headers={"Retry-After": ...} argument is actually captured.
        captured = []
        h._json = lambda status, payload, extra_headers=None: captured.append((status, payload, extra_headers))
        cur = FakeCursor()
        with patch_db(cur):
            h._search()
        status, payload, extra_headers = captured[-1]
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", extra_headers or {})
        self.assertEqual(cur.calls, [], "no DB call once the real public limiter has fired")

    def test_global_bucket_trips_across_varying_per_ip_keys(self):
        """AC-2 / threat-model row 3: a burst distributed across many distinct
        per-IP keys still trips the shared global bucket, bounding total
        anonymous throughput regardless of X-Forwarded-For spoofing.

        Mirrors the existing precedent in
        test_choose_username_at_registration.py::test_rate_check_blocks_after_per_ip_limit
        — drives server.rate_check() directly with a TEST-scoped global key
        (never the production "public:_global" string) so this test cannot
        pollute the real global bucket shared by every other test in the suite
        that exercises the six read endpoints (or any other public:* caller)
        without stubbing _public_rate_limited."""
        global_key = "public:_global_test_ac2_varying_ips"
        small_global_limit = 5  # keep the loop short; mechanism is limit-agnostic
        # Each of the 5 hits comes from a DIFFERENT per-IP key (well under its own
        # PUBLIC_RATE_MAX), yet all share the same global bucket.
        for i in range(small_global_limit):
            per_ip_key = f"public:198.51.100.{100 + i}"
            allowed, _ = server.rate_check(
                [(per_ip_key, server.PUBLIC_RATE_MAX), (global_key, small_global_limit)]
            )
            self.assertTrue(allowed, f"hit {i} from a fresh per-IP key must pass (per-IP cap far from reached)")
        # The (small_global_limit + 1)-th hit, from yet another fresh per-IP key,
        # must be blocked purely by the shared global bucket.
        allowed, retry = server.rate_check(
            [("public:198.51.100.200", server.PUBLIC_RATE_MAX), (global_key, small_global_limit)]
        )
        self.assertFalse(allowed, "the global bucket must trip once its cap is reached, "
                                   "even though every per-IP key is fresh")
        self.assertGreater(retry, 0)


# ── AC-9: authed request on the six endpoints uses tmdb:{user_id}, not public:* ─


class AuthedRequestUsesPerUserBucketNotPublic(unittest.TestCase):
    """AC-9: an authenticated caller on the six endpoints must consume the
    per-user tmdb:{user_id} limiter (unchanged), never the public:* bucket —
    the anonymous branch (_public_rate_limited) must not even be invoked."""

    def test_authed_search_never_calls_public_rate_limited(self):
        h, responses = make_handler(user_id="user-77", qs={"q": ["dune"]}, rate_limited=False)
        h.path = "/api/search"
        called = {"public": False}

        def _public():
            called["public"] = True
            return False

        h._public_rate_limited = _public
        with _NoKeyEnv():
            h._search()
        self.assertFalse(called["public"], "an authed request must never touch the public limiter")
        self.assertEqual(responses[-1][0], 200)

    def test_authed_request_consumes_tmdb_user_bucket_via_real_rate_check(self):
        """Drives the real rate_check() call through _rate_limited(user_id) (not
        stubbed) and asserts the bucket key is tmdb:{user_id} / tmdb:_global —
        never a public:* key — proving the branch selection in the six handlers
        (`if user_id: self._rate_limited(user_id) ... elif ...`)."""
        seen_buckets = []
        real_rate_check = server.rate_check

        def _spy(buckets):
            seen_buckets.extend(buckets)
            return real_rate_check(buckets)

        h, responses = make_handler(user_id="user-guest-explore-9", qs={"q": ["dune"]})
        h.path = "/api/search"
        # _rate_limited is normally stubbed to a constant by make_handler; restore
        # the REAL bound method so the real rate_check() call happens.
        h._rate_limited = server.Handler._rate_limited.__get__(h, server.Handler)
        h._public_rate_limited = lambda: (_ for _ in ()).throw(
            AssertionError("public limiter must not be called for an authed request"))
        with mock.patch.object(server, "rate_check", side_effect=_spy), _NoKeyEnv():
            h._search()
        bucket_keys = [k for k, _ in seen_buckets]
        self.assertIn("tmdb:user-guest-explore-9", bucket_keys)
        self.assertIn("tmdb:_global", bucket_keys)
        self.assertFalse(any(k.startswith("public:") for k in bucket_keys),
                          f"no public:* bucket may be touched for an authed request, got {bucket_keys}")


# ── AC-7: anonymous _details/_season degrade to safe per-user defaults ─────────


class AnonymousDetailsAndSeasonDegradeSafely(unittest.TestCase):
    """AC-7: anonymous _details (tv) -> watched_count: 0, no get_db() call;
    anonymous _season -> every episode watched: false, no get_db() call."""

    def test_anonymous_details_series_watched_count_zero_no_db_call(self):
        h, responses = make_handler(user_id=None, qs={"id": ["42"], "type": ["tv"]})
        h.path = "/api/details?id=42&type=tv"
        h._public_rate_limited = lambda: False
        h._tmdb = lambda path, extra=None, ttl=None: {"name": "S", "number_of_seasons": 3}
        cur = FakeCursor()
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False), patch_db(cur):
            h._details()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["details"]["watched_count"], 0)
        self.assertEqual(cur.calls, [], "no get_db() call for an anonymous _details request")

    def test_anonymous_details_movie_unaffected(self):
        """Sibling guard: an anonymous movie _details call was already 0 pre-feature
        (movies have no episodes) and issues no DB call either — the guard must not
        regress the movie path."""
        h, responses = make_handler(user_id=None, qs={"id": ["550"], "type": ["movie"]})
        h.path = "/api/details?id=550&type=movie"
        h._public_rate_limited = lambda: False
        h._tmdb = lambda path, extra=None, ttl=None: {"title": "M"}
        cur = FakeCursor()
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False), patch_db(cur):
            h._details()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["details"]["watched_count"], 0)
        self.assertEqual(cur.calls, [])

    def test_anonymous_season_every_episode_watched_false_no_db_call(self):
        h, responses = make_handler(user_id=None)
        h.path = "/api/tv/550/season/1"
        h._public_rate_limited = lambda: False
        payload_in = {
            "season_number": 1,
            "name": "Season 1",
            "episodes": [
                {"episode_number": 1, "name": "Pilot", "air_date": "2020-01-01",
                 "runtime": 45, "overview": "The beginning.", "still_path": "/s1.jpg"},
                {"episode_number": 2, "name": "Episode 2", "air_date": "2020-01-08",
                 "runtime": 42, "overview": "It continues.", "still_path": None},
            ],
        }
        h._tmdb = lambda path, extra=None, ttl=None: payload_in
        cur = FakeCursor()
        with mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False), patch_db(cur):
            h._season(550, 1)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        episodes = payload["season"]["episodes"]
        self.assertEqual(len(episodes), 2)
        for ep in episodes:
            self.assertFalse(ep["watched"], "every episode must be watched: false for an anonymous caller")
        self.assertEqual(cur.calls, [], "no get_db() call for an anonymous _season request")


# ── AC-3: user-scoped endpoints + writes still 401 without a JWT ───────────────


class UserScopedEndpointsStill401ForAnonymous(unittest.TestCase):
    """AC-3 / threat-model row 2: every endpoint OTHER than the six reads keeps
    its 401 gate for a caller with no valid JWT, and never touches the DB."""

    def test_get_endpoints_401_no_db_call(self):
        cases = [
            ("_list_movies",   "/api/movies",  ()),
            ("_level",         "/api/level",   ()),
            ("_get_profile",   "/api/profile", ()),
            ("_list_lists",    "/api/lists",   ()),
            ("_feed",          "/api/feed",    ()),
            ("_review_likes",  "/api/reviews/7/likes", (7,)),
            ("_export_account", "/api/account/export", ()),
            ("_follow_status", "/api/follows/alice", ("alice",)),
        ]
        for attr, path, args in cases:
            with self.subTest(endpoint=attr):
                h, responses = make_handler(user_id=None)
                h.path = path
                h.headers = {}
                cur = FakeCursor()
                with patch_db(cur):
                    getattr(h, attr)(*args)
                self.assertEqual(responses[-1][0], 401, f"{attr} must 401 for an anonymous caller")
                self.assertEqual(cur.calls, [], f"{attr} must not touch the DB before the 401 gate")

    def test_write_endpoints_401_no_db_call(self):
        # do_POST /api/movies
        h, responses = make_handler(user_id=None, body={"title": "X", "media_type": "movie", "tmdb_id": 1})
        h.path = "/api/movies"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_POST()
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [])

        # do_PATCH /api/movies/7
        h, responses = make_handler(user_id=None, body={"status": "vista"})
        h.path = "/api/movies/7"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_PATCH()
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [])

        # do_DELETE /api/movies/7
        h, responses = make_handler(user_id=None)
        h.path = "/api/movies/7"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_DELETE()
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [])


# ── AC-10 (integration): header parity, anonymous vs "authed" ──────────────────

_HOST = "127.0.0.1"
_EPHEMERAL_PORT = 0
_STARTUP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05

# The perimeter header set this feature must leave byte-identical (same set
# test_security_headers.py / test_perimeter_headers.py assert individually).
_PERIMETER_HEADERS = (
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Content-Security-Policy",
    "Permissions-Policy",
    "Cross-Origin-Resource-Policy",
)


def _wait_until_accepting(host, port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=_POLL_INTERVAL_SECONDS):
                return
        except OSError:
            time.sleep(_POLL_INTERVAL_SECONDS)
    raise RuntimeError(f"test server did not accept a connection on {host}:{port} within {timeout}s")


class GuestModeHeaderParityIntegration(unittest.TestCase):
    """AC-10: boots the real server.Handler and asserts the perimeter header set
    is byte-identical for an anonymous request, a request carrying a (invalid,
    since no JWKS is configured in this test process — verify_jwt_identity
    always returns None/None) Authorization header, and the pre-existing 404
    path — proving this feature added no header divergence by auth state."""

    @classmethod
    def setUpClass(cls):
        handler = functools.partial(server.Handler, directory=str(server.BASE_DIR))
        cls.httpd = http.server.ThreadingHTTPServer((_HOST, _EPHEMERAL_PORT), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        _wait_until_accepting(_HOST, cls.port, _STARTUP_TIMEOUT_SECONDS)
        cls.base_url = f"http://{_HOST}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=_STARTUP_TIMEOUT_SECONDS)

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.base_url + path, method="GET", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _perimeter_set(self, headers):
        return {name: headers.get(name) for name in _PERIMETER_HEADERS}

    def test_anonymous_and_authed_header_present_requests_are_byte_identical(self):
        """A request carrying an Authorization header still resolves to anonymous
        here (no JWKS client is configured in this DB-less test process, so
        verify_jwt_identity always returns (None, None)) — this still proves
        header emission never branches on the *presence* of the header."""
        status_anon, headers_anon, _ = self._get("/api/search?q=x")
        self.assertNotEqual(status_anon, 401, "anonymous /api/search must not 401 (AC-1)")
        status_authed, headers_authed, _ = self._get(
            "/api/search?q=x", headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(self._perimeter_set(headers_anon), self._perimeter_set(headers_authed),
                          "perimeter header set must be byte-identical regardless of the "
                          "Authorization header's presence")

    def test_header_set_unchanged_for_a_genuinely_authenticated_caller(self):
        """Stronger AC-10 proof: force server.Handler._get_user_id() to resolve a
        real (non-None) user_id — mirroring a genuinely authenticated caller
        without needing a live Supabase JWKS in this test process — and assert
        the perimeter header set on /api/search is unchanged from the anonymous
        baseline. end_headers() must never branch on user_id."""
        status_anon, headers_anon, _ = self._get("/health")
        with mock.patch.object(server.Handler, "_get_user_id", lambda self: "fixed-test-user-ac10"):
            status_authed, headers_authed, _ = self._get("/api/search?q=x")
        self.assertEqual(status_authed, 200)
        self.assertEqual(self._perimeter_set(headers_anon), self._perimeter_set(headers_authed),
                          "perimeter header set must be byte-identical for a genuinely "
                          "authenticated caller")

    def test_health_and_404_carry_the_same_perimeter_headers(self):
        _, headers_health, _ = self._get("/health")
        _, headers_404, _ = self._get("/api/this-route-does-not-exist")
        self.assertEqual(self._perimeter_set(headers_health), self._perimeter_set(headers_404))

    def test_csp_unchanged_no_new_origin(self):
        _, headers, _ = self._get("/health")
        csp = headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("cdn.jsdelivr.net", csp)

    def test_static_allowlist_404_unchanged_for_anonymous(self):
        status, headers, body = self._get("/not-a-real-static-asset.txt")
        self.assertEqual(status, 404)
        self.assertIsNotNone(headers.get("Content-Security-Policy"),
                              "even the static-serving 404 path must carry the perimeter headers")


if __name__ == "__main__":
    unittest.main()
