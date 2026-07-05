"""Redaction + benign-filter + throttle tests for the Discord error-alert path.

`ErrorNotifyingServer.handle_error` posts a REDACTED traceback to
`DISCORD_WEBHOOK_ERRORS` when an unhandled exception escapes the request handler.
These tests guard the load-bearing invariants:

- client-disconnect exceptions never alert (noise, not application bugs);
- an upstream TimeoutError DOES alert (a TMDB/Supabase outage is a real bug);
- every alerted traceback has its secrets stripped before it leaves the process;
- a crash-looping endpoint does not spam Discord (same traceback -> one alert/window).
"""
import time
import unittest
from unittest import mock

import server


def _server():
    # Build an ErrorNotifyingServer without __init__ so no socket is bound.
    return object.__new__(server.ErrorNotifyingServer)


def _raise(exc):
    raise exc


class ErrorAlertTests(unittest.TestCase):
    def setUp(self):
        server._ALERT_LAST.clear()   # fresh throttle state per test

    def test_benign_disconnect_does_not_alert(self):
        captured = []
        with mock.patch.object(server, "_send_discord",
                               side_effect=lambda url, p: captured.append(p)), \
             mock.patch.dict(server.os.environ,
                             {"DISCORD_WEBHOOK_ERRORS": "https://x.test/wh"}):
            try:
                raise ConnectionResetError("client gone")
            except Exception:
                _server().handle_error(None, ("1.2.3.4", 0))
        self.assertEqual(captured, [], "a client disconnect must not alert")

    def test_timeout_is_not_benign_and_alerts(self):
        captured = []
        with mock.patch.object(server, "_send_discord",
                               side_effect=lambda url, p: captured.append(p)), \
             mock.patch.dict(server.os.environ,
                             {"DISCORD_WEBHOOK_ERRORS": "https://x.test/wh"}):
            try:
                raise TimeoutError("upstream TMDB slow")
            except Exception:
                _server().handle_error(None, ("1.2.3.4", 0))
        time.sleep(0.3)
        self.assertTrue(captured, "an upstream timeout must alert (not treated as benign)")

    def test_unhandled_error_alerts_with_secrets_redacted(self):
        captured = []
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ."
               "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
        env = {"DISCORD_WEBHOOK_ERRORS": "https://x.test/wh",
               "SUPABASE_SERVICE_KEY": "sb_secret_LIVEVALUE1234567890"}
        with mock.patch.object(server, "_send_discord",
                               side_effect=lambda url, p: captured.append(p)), \
             mock.patch.dict(server.os.environ, env):
            try:
                raise ValueError(
                    "postgres://u:mypassword@h/db Bearer eyJaaa.bbbb.cccc "
                    "Bearer abc123deadbeef token=t0psecret password=hunter2 "
                    "Authorization: Basic dXNlcjpzdXBlcnNlY3JldA== "
                    "sb_secret_LIVEVALUE1234567890 sb_secret_OTHERROTATEDKEY99 " + jwt)
            except Exception:
                _server().handle_error(None, ("1.2.3.4", 0))
        time.sleep(0.3)
        self.assertTrue(captured, "an unhandled error must alert")
        body = captured[-1].decode("utf-8")
        for leak in ("mypassword", "eyJaaa.bbbb.cccc", "abc123deadbeef", "t0psecret",
                     "hunter2", "dXNlcjpzdXBlcnNlY3JldA==",     # Basic auth base64
                     "LIVEVALUE1234567890", "OTHERROTATEDKEY99",  # both Supabase keys
                     jwt):
            self.assertNotIn(leak, body, "secret leaked to Discord: " + leak)
        self.assertIn("[REDACTED", body)
        self.assertIn("ValueError", body)  # useful context is preserved

    def test_repeated_error_is_throttled_to_one_alert(self):
        captured = []
        with mock.patch.object(server, "_send_discord",
                               side_effect=lambda url, p: captured.append(p)), \
             mock.patch.dict(server.os.environ,
                             {"DISCORD_WEBHOOK_ERRORS": "https://x.test/wh"}):
            for _ in range(5):
                try:
                    _raise(RuntimeError("crash loop"))   # same site -> same signature
                except Exception:
                    _server().handle_error(None, ("1.2.3.4", 0))
        time.sleep(0.3)
        self.assertEqual(len(captured), 1,
                         "a crash-looping endpoint must alert once per window, not per request")

    def test_no_webhook_configured_is_noop(self):
        captured = []
        with mock.patch.object(server, "_send_discord",
                               side_effect=lambda url, p: captured.append(p)), \
             mock.patch.dict(server.os.environ, {}, clear=False):
            server.os.environ.pop("DISCORD_WEBHOOK_ERRORS", None)
            server.notify_error("boom password=secret")
        self.assertEqual(captured, [], "no webhook -> no network call")

    def test_redact_masks_common_secrets(self):
        with mock.patch.dict(server.os.environ, {"TMDB_API_KEY": "tmdbSECRETKEY12345"}):
            out = server._redact("k tmdbSECRETKEY12345 token: abc password=xyz "
                                 "postgres://u:pw@h/db Authorization: Basic YWJjOmRlZg==")
        self.assertNotIn("tmdbSECRETKEY12345", out)
        self.assertNotIn("xyz", out)
        self.assertNotIn(":pw@", out)
        self.assertNotIn("YWJjOmRlZg==", out)


if __name__ == "__main__":
    unittest.main()
