"""Unit tests for the DB connection-pool liveness check (`_checkout_live`) and
the semaphore contract of `get_db`.

Background: in production (Render + Supabase) an idle connection silently dropped
by the pooler / NAT was handed out by the pool, and the first `execute` failed
with `could not send data to server: Connection timed out` → 502. The fix adds
TCP keepalives plus `_checkout_live`, which probes each connection with `SELECT 1`
before handing it out, discarding and retrying the dead ones.

These tests drive `_checkout_live` / `get_db` directly against a mocked pool and
semaphore (the `_harness` mock replaces `get_db` wholesale, so it does not reach
this code path — hence a separate suite that patches `server._db_pool` / `_db_sem`).
"""

import threading
import unittest
from unittest import mock

import psycopg2

import server


def _make_con(*, dead=False):
    """A MagicMock connection whose `with con.cursor() as cur: cur.execute(...)`
    raises OperationalError when `dead=True`, else records the call."""
    con = mock.MagicMock()
    if dead:
        con.cursor.return_value.__enter__.return_value.execute.side_effect = (
            psycopg2.OperationalError("could not send data to server")
        )
    return con


class CheckoutLiveTest(unittest.TestCase):
    def test_returns_live_connection_on_first_try(self):
        """Happy path: probe succeeds → rollback() closes the probe txn → return."""
        con = _make_con()
        pool = mock.MagicMock()
        pool.getconn.return_value = con

        with mock.patch.object(server, "_db_pool", pool):
            result = server._checkout_live()

        pool.getconn.assert_called_once()
        con.cursor.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")
        con.rollback.assert_called_once()
        pool.putconn.assert_not_called()
        self.assertIs(result, con)

    def test_dead_connection_is_discarded_then_retried(self):
        """First connection is dead → putconn(close=True) → next is live → return."""
        dead, live = _make_con(dead=True), _make_con()
        pool = mock.MagicMock()
        pool.getconn.side_effect = [dead, live]

        with mock.patch.object(server, "_db_pool", pool):
            result = server._checkout_live()

        pool.putconn.assert_called_once_with(dead, close=True)
        self.assertEqual(pool.getconn.call_count, 2)
        self.assertIs(result, live)

    def test_all_dead_propagates_last_exception_bounded_by_tries(self):
        """Every connection dead → raise after exactly DB_CHECKOUT_TRIES attempts."""
        pool = mock.MagicMock()
        pool.getconn.side_effect = lambda: _make_con(dead=True)

        with mock.patch.object(server, "_db_pool", pool):
            with self.assertRaises(psycopg2.OperationalError):
                server._checkout_live()

        self.assertEqual(pool.getconn.call_count, server.DB_CHECKOUT_TRIES)
        self.assertEqual(pool.putconn.call_count, server.DB_CHECKOUT_TRIES)

    def test_putconn_failure_does_not_break_retry_loop(self):
        """If discarding a dead connection raises, the loop still retries."""
        dead, live = _make_con(dead=True), _make_con()
        pool = mock.MagicMock()
        pool.getconn.side_effect = [dead, live]
        pool.putconn.side_effect = psycopg2.OperationalError("already returned")

        with mock.patch.object(server, "_db_pool", pool):
            result = server._checkout_live()

        self.assertIs(result, live)


class GetDbSemaphoreTest(unittest.TestCase):
    def test_semaphore_released_when_checkout_live_raises(self):
        """get_db acquires the semaphore; if _checkout_live fails the nested
        finally must still release it (no slot leak)."""
        sem = threading.BoundedSemaphore(1)
        with (mock.patch.object(server, "_db_sem", sem),
              mock.patch.object(server, "_checkout_live",
                                side_effect=psycopg2.OperationalError("all dead"))):
            with self.assertRaises(psycopg2.OperationalError):
                with server.get_db():
                    pass
        # Slot must be reacquirable — i.e. it was released, not leaked.
        self.assertTrue(sem.acquire(blocking=False))
        sem.release()

    def test_saturated_semaphore_raises_dbbusy(self):
        """No slot within DB_WAIT_TIMEOUT → DBBusy (which _db_guard maps to 503)."""
        sem = threading.BoundedSemaphore(1)
        sem.acquire()  # exhaust the only slot
        with (mock.patch.object(server, "_db_sem", sem),
              mock.patch.object(server, "DB_WAIT_TIMEOUT", 0)):
            with self.assertRaises(server.DBBusy):
                with server.get_db():
                    pass


if __name__ == "__main__":
    unittest.main()
