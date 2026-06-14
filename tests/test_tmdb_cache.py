"""Tests for the in-memory TTL cache on the TMDB layer (server._tmdb).

The cache is transparent: it sits inside _tmdb(), keyed on (path, params minus
api_key). These tests drive the real _tmdb() with urllib.request.urlopen stubbed
so no network is hit, asserting hit/miss/expiry/bypass behaviour and that errors
are never cached.
"""

import json
import os
import unittest
import urllib.error
from contextlib import contextmanager
from unittest import mock

import server


class _FakeResp:
    """Minimal context-manager stand-in for the urlopen() response object."""

    def __init__(self, payload):
        self._bytes = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._bytes


@contextmanager
def _patched(*, ttl=900, monotonic=None, urlopen=None):
    """Reset the module cache and patch TTL / clock / urlopen for one test."""
    server._tmdb_cache.clear()
    with (
        mock.patch.object(server, "TMDB_CACHE_TTL", ttl),
        mock.patch.dict(os.environ, {"TMDB_API_KEY": "k"}, clear=False),
    ):
        stack = []
        if monotonic is not None:
            stack.append(mock.patch.object(server.time, "monotonic", monotonic))
        if urlopen is not None:
            stack.append(mock.patch.object(server.urllib.request, "urlopen", urlopen))
        for p in stack:
            p.start()
        try:
            yield
        finally:
            for p in stack:
                p.stop()


def _handler():
    return server.Handler.__new__(server.Handler)


class TmdbCache(unittest.TestCase):
    def test_hit_avoids_second_network_call(self):
        """Two identical _tmdb calls within the TTL → urlopen hit exactly once."""
        calls = {"n": 0}

        def fake_urlopen(url, timeout=None):
            calls["n"] += 1
            return _FakeResp({"results": [{"id": 1}]})

        with _patched(urlopen=fake_urlopen):
            h = _handler()
            a = h._tmdb("/trending/all/week")
            b = h._tmdb("/trending/all/week")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(a, b)
        self.assertEqual(a["results"][0]["id"], 1)

    def test_different_params_are_separate_entries(self):
        """A different `extra` is a cache miss → a second network call."""
        calls = {"n": 0}

        def fake_urlopen(url, timeout=None):
            calls["n"] += 1
            return _FakeResp({"page": calls["n"]})

        with _patched(urlopen=fake_urlopen):
            h = _handler()
            h._tmdb("/discover/movie", {"with_genres": "28"})
            h._tmdb("/discover/movie", {"with_genres": "35"})
        self.assertEqual(calls["n"], 2)

    def test_entry_expires_after_ttl(self):
        """Past the TTL the cached value is stale → a fresh network call."""
        clock = {"t": 1000.0}
        calls = {"n": 0}

        def fake_urlopen(url, timeout=None):
            calls["n"] += 1
            return _FakeResp({"n": calls["n"]})

        with _patched(ttl=900, monotonic=lambda: clock["t"], urlopen=fake_urlopen):
            h = _handler()
            first = h._tmdb("/trending/all/week")
            clock["t"] += 901  # advance past the TTL
            second = h._tmdb("/trending/all/week")
        self.assertEqual(calls["n"], 2)
        self.assertNotEqual(first, second)

    def test_ttl_zero_disables_cache(self):
        """TTL=0 → every call goes to the network (cache bypassed, nothing stored)."""
        calls = {"n": 0}

        def fake_urlopen(url, timeout=None):
            calls["n"] += 1
            return _FakeResp({"n": calls["n"]})

        with _patched(ttl=0, urlopen=fake_urlopen):
            h = _handler()
            h._tmdb("/trending/all/week")
            h._tmdb("/trending/all/week")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(len(server._tmdb_cache), 0)

    def test_no_key_returns_none_and_does_not_cache(self):
        """No TMDB key → None, no network call, nothing cached."""
        server._tmdb_cache.clear()
        called = {"n": 0}

        def fake_urlopen(url, timeout=None):
            called["n"] += 1
            return _FakeResp({})

        with (
            mock.patch.object(server, "TMDB_CACHE_TTL", 900),
            mock.patch.object(server.urllib.request, "urlopen", fake_urlopen),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("TMDB_API_KEY", None)
            h = _handler()
            self.assertIsNone(h._tmdb("/trending/all/week"))
        self.assertEqual(called["n"], 0)
        self.assertEqual(len(server._tmdb_cache), 0)

    def test_network_error_is_not_cached(self):
        """A urlopen failure propagates and leaves the cache empty (no poisoning)."""

        def boom(url, timeout=None):
            raise urllib.error.URLError("down")

        with _patched(urlopen=boom):
            h = _handler()
            with self.assertRaises(urllib.error.URLError):
                h._tmdb("/trending/all/week")
        self.assertEqual(len(server._tmdb_cache), 0)


if __name__ == "__main__":
    unittest.main()
