"""Shared test harness for driving server.Handler methods without a socket or DB.

The handlers are methods on a SimpleHTTPRequestHandler subclass; instantiating
the real class needs a live socket. We bypass __init__ with __new__ and inject
only the attributes the handler methods touch (captured JSON responses, stubbed
auth / rate-limit / body / query-string / TMDB). The DB boundary is replaced by
a FakeCursor that records every execute() call so a test can assert the exact
SQL + parameters reaching the parameterised INSERT/UPDATE.
"""

import contextlib
from unittest import mock

import server


class FakeCursor:
    """Records execute() calls and serves canned fetch results.

    `fetch_results` is a list consumed FIFO: each execute() that is followed by
    a fetchone()/fetchall() pops the next canned value. `rowcount` defaults to 1
    (a successful UPDATE) and can be overridden per instance.
    """

    def __init__(self, fetch_results=None, rowcount=1):
        self.calls = []  # list of (sql, params)
        self._fetch_results = list(fetch_results or [])
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._fetch_results.pop(0) if self._fetch_results else None

    def fetchall(self):
        return self._fetch_results.pop(0) if self._fetch_results else []


@contextlib.contextmanager
def fake_get_db(cursor):
    """Drop-in for server.get_db(): yields the supplied FakeCursor."""
    yield cursor


def make_handler(*, user_id="user-1", body=None, qs=None, rate_limited=False):
    """Build a server.Handler with the request boundary stubbed.

    Returns (handler, responses) where `responses` is a list the handler's
    _json() appends (status, payload) tuples to.
    """
    h = server.Handler.__new__(server.Handler)
    responses = []

    def _json(status, payload, extra_headers=None):
        responses.append((status, payload))

    h._json = _json
    h._get_user_id = lambda: user_id
    h._rate_limited = lambda uid: rate_limited
    h._read_json = lambda: body if body is not None else {}
    h._qs = lambda: qs or {}
    h.responses = responses
    return h, responses


def patch_db(cursor):
    """Patch server.get_db to yield the given FakeCursor."""
    return mock.patch.object(server, "get_db", lambda: fake_get_db(cursor))
