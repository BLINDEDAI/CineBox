"""Pytest fixtures for the CineBox browser E2E harness (ADR-003).

Boots the *real* ``server.Handler`` over an ephemeral-port
``ThreadingHTTPServer`` in a daemon thread — mirroring the construction in
``server.main()`` (server.py:922) — but **without** calling ``server.main()``,
``init_pool()``, or ``init_db()``. The system-under-test for the supply-chain
checks (static ``do_GET`` file serving + the CSP emitted by
``Handler.end_headers()``) is DB-independent, so no ``DATABASE_URL`` and no
Postgres are required (spec § "Server under test").

The ``base_url`` fixture is the same-origin SUT the Tester's E2E cases drive a
headless browser against; pytest-playwright supplies the browser fixtures.
"""

import functools
import http.server
import socket
import threading
import time

import pytest

import server

# Bind to an ephemeral port on loopback only: 0 lets the OS assign a free port,
# avoiding collision with a dev server on 8000 and with parallel CI runners
# (spec § Edge Cases "Ephemeral port discovery").
_HOST = "127.0.0.1"
_EPHEMERAL_PORT = 0
# Max seconds to wait for the server thread to accept a TCP connection before
# the first navigation, so the first page load never races server startup
# (spec § Edge Cases "Server-start race").
_STARTUP_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05


def _wait_until_accepting(host, port, timeout):
    """Block until ``(host, port)`` accepts a TCP connection, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=_POLL_INTERVAL_SECONDS):
                return
        except OSError:
            time.sleep(_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"CineBox test server did not accept a connection on {host}:{port} "
        f"within {timeout}s"
    )


@pytest.fixture(scope="session")
def base_url():
    """Yield the base URL of a running CineBox server backed by the real Handler.

    Serves the repository root (``server.BASE_DIR``) — the same directory
    ``server.main()`` passes — so ``index.html``, the seven JS modules, and the
    vendored ``vendor/supabase-js/...`` bundle are served exactly as in
    production. Tears the server down on teardown so no process leaks (AC-1).
    """
    handler = functools.partial(server.Handler, directory=str(server.BASE_DIR))
    httpd = http.server.ThreadingHTTPServer((_HOST, _EPHEMERAL_PORT), handler)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    try:
        _wait_until_accepting(_HOST, port, _STARTUP_TIMEOUT_SECONDS)
        yield f"http://{_HOST}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=_STARTUP_TIMEOUT_SECONDS)
