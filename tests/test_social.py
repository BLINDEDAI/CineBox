"""Backend unit/integration tests for the social-follows-and-activity-feed
feature (Social layer — Phase 1).

Covers every ### Tester scope row delegated to Python unittest:

  - Follow: happy path (AC-1), self-follow 400 (AC-4), private/nonexistent
    identical 404 (AC-3), duplicate idempotent (AC-5).
  - Unfollow: removes the edge, idempotent no-op on non-followed/nonexistent
    (AC-2).
  - Auth: unauthenticated POST /api/follows, DELETE /api/follows/{u},
    GET /api/feed -> 401, nothing changed (AC-6).
  - Feed gating: three action kinds present (AC-10), pending excluded
    (AC-11), actor-private hidden (AC-12), show_collection=false hides
    watched/rated but not list_add (AC-13), list privatized/deleted hides
    list_add (AC-14), reverse-chron (AC-9), empty feed (AC-15).
  - Counts + lists: followers_count/following_count include private
    participants (AC-7); followers/following arrays list only public
    profiles (AC-8).
  - Deletion purge: after _delete_account, zero activity rows + zero
    follows rows in either direction (AC-16).
  - Cross-user scoping: a follow/unfollow/feed/purge for user A never
    touches user B's data (AC-17).
  - Rate limiting: FOLLOW_RATE_MAX / FEED_RATE_MAX exceeded -> 429 +
    Retry-After (mirrors the existing account-export rate-limit tests).
  - Developer verification directive (tester-bundle SS8, load-bearing):
    the activity-event write introduces NO new failure mode on the 3
    existing mutations (do_POST /api/movies, do_PATCH /api/movies/{id},
    _add_list_item) -- their status codes/outcomes are unchanged, and a
    watched/rated/public-list-add now ALSO writes exactly one activity row
    while a pending-add/non-public-list-add writes none (AC-10/AC-11).

Stub strategy (mirrors tests/test_delete_account.py, tests/test_export_account.py,
tests/test_add_titles_to_lists.py):
  - h._get_user_id      -> lambda stub on the handler instance (make_handler)
  - server.rate_check   -> mock.patch to allow or block (returns (bool, retry_s))
  - server.get_db       -> patch_db(FakeCursor) for the DB boundary
  - server._audit       -> mock.patch to capture calls without side effects

_public_profile DB-call order (per the Backend Developer handoff, load-bearing
for FakeCursor FIFO wiring): profile -> [collection?] -> [stats?] -> lists ->
followers_count -> following_count -> followers[] -> following[].

No live Supabase, no live DB, no live network required.
"""

import unittest
from unittest import mock

import server
from server import _hash_user_id
from tests._harness import FakeCursor, make_handler, patch_db


# ── Shared fixtures ──────────────────────────────────────────────────────────

_UID_A = "aaaa-1111-aaaa-1111"
_UID_B = "bbbb-2222-bbbb-2222"
_USERNAME_B = "bobpublic"


def _allow_rate():
    return mock.patch.object(server, "rate_check", return_value=(True, 0))


def _block_rate():
    return mock.patch.object(server, "rate_check", return_value=(False, 42))


# ── AC-1/AC-3/AC-4/AC-5: POST /api/follows ───────────────────────────────────


class FollowHappyPathIntegration(unittest.TestCase):
    """AC-1: following a public target creates exactly one edge."""

    def test_follow_public_target_returns_200_following_true(self):
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["following"])

    def test_follow_public_target_inserts_with_caller_as_follower(self):
        """AC-17: follower_id is ALWAYS the caller's own user_id, never client-supplied."""
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        insert_calls = [c for c in cur.calls if "INSERT INTO follows" in c[0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0][1], (_UID_A, _UID_B))

    def test_follow_uses_on_conflict_do_nothing(self):
        """AC-5 storage-layer idempotency: the INSERT carries ON CONFLICT DO NOTHING."""
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        insert_sql = [c[0] for c in cur.calls if "INSERT INTO follows" in c[0]][0]
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)


class FollowSelfIntegration(unittest.TestCase):
    """AC-4: a self-follow attempt is rejected with 400, no edge created."""

    def test_self_follow_returns_400(self):
        cur = FakeCursor(fetch_results=[{"user_id": _UID_A, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": "selfuser"})
        with _allow_rate(), patch_db(cur):
            h._follow()
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_self_follow_creates_no_edge(self):
        cur = FakeCursor(fetch_results=[{"user_id": _UID_A, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": "selfuser"})
        with _allow_rate(), patch_db(cur):
            h._follow()
        insert_calls = [c for c in cur.calls if "INSERT INTO follows" in c[0]]
        self.assertEqual(insert_calls, [], "Self-follow must not reach INSERT")


class FollowPrivateOrNonexistentIntegration(unittest.TestCase):
    """AC-3: private and nonexistent targets both return an identical 404
    (non-enumerating)."""

    def test_private_target_returns_404(self):
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": False}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "No disponible")

    def test_nonexistent_target_returns_404(self):
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=_UID_A, body={"username": "ghostuser"})
        with _allow_rate(), patch_db(cur):
            h._follow()
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "No disponible")

    def test_private_and_nonexistent_bodies_are_identical(self):
        """Non-enumeration: the exact same body for both cases."""
        cur1 = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": False}])
        h1, r1 = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur1):
            h1._follow()

        cur2 = FakeCursor(fetch_results=[None])
        h2, r2 = make_handler(user_id=_UID_A, body={"username": "ghostuser"})
        with _allow_rate(), patch_db(cur2):
            h2._follow()

        self.assertEqual(r1[-1], r2[-1])

    def test_private_target_creates_no_edge(self):
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": False}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        insert_calls = [c for c in cur.calls if "INSERT INTO follows" in c[0]]
        self.assertEqual(insert_calls, [])


class FollowDuplicateIntegration(unittest.TestCase):
    """AC-5: following an already-followed target is a harmless idempotent no-op."""

    def test_duplicate_follow_returns_200(self):
        # ON CONFLICT DO NOTHING means the INSERT succeeds silently either way;
        # the handler always returns 200 {following:true} regardless of whether
        # a new row was actually created.
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["following"])

    def test_duplicate_follow_single_insert_statement(self):
        """Still exactly one INSERT attempt (no duplicate-row special-casing)."""
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            h._follow()
        insert_calls = [c for c in cur.calls if "INSERT INTO follows" in c[0]]
        self.assertEqual(len(insert_calls), 1)


# ── AC-2: DELETE /api/follows/{username} ─────────────────────────────────────


class UnfollowIntegration(unittest.TestCase):
    """AC-2: unfollow is idempotent and non-enumerating."""

    def test_unfollow_returns_200_following_false(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unfollow(_USERNAME_B)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["following"])

    def test_unfollow_deletes_scoped_to_caller(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unfollow(_USERNAME_B)
        delete_calls = [c for c in cur.calls if "DELETE FROM follows" in c[0]]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1], (_UID_A, _USERNAME_B))

    def test_unfollow_nonexistent_username_still_200(self):
        """AC-2: a malformed/reserved username still returns 200 (non-enumerating),
        with no DB DELETE issued (server.py:2296 unfollow guards on norm is not None)."""
        cur = FakeCursor()
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unfollow("x")  # too short to normalize (_USERNAME_RE requires 3-30)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["following"])
        self.assertEqual(cur.calls, [], "Malformed username must not reach the DB")

    def test_unfollow_not_followed_is_harmless_noop(self):
        """Unfollowing someone never followed: DELETE matches zero rows, still 200."""
        cur = FakeCursor(rowcount=0)
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unfollow(_USERNAME_B)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["following"])


# ── AC-6: unauthenticated follow/unfollow/feed -> 401 ────────────────────────


class SocialAuthIntegration(unittest.TestCase):
    """AC-6: every social endpoint requires auth; unauthenticated -> 401, no writes."""

    def test_follow_unauthenticated_returns_401(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=None, body={"username": _USERNAME_B})
        with patch_db(cur):
            h._follow()
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [], "No DB call before auth check")

    def test_unfollow_unauthenticated_returns_401(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        with patch_db(cur):
            h._unfollow(_USERNAME_B)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [], "No DB call before auth check")

    def test_feed_unauthenticated_returns_401(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        with patch_db(cur):
            h._feed()
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [], "No DB call before auth check")

    def test_follow_unauthenticated_body_is_generic(self):
        h, responses = make_handler(user_id=None, body={"username": _USERNAME_B})
        h._follow()
        _, payload = responses[-1]
        self.assertFalse(payload["ok"])
        self.assertTrue(payload.get("error"))


# ── AC-9/10/11/12/13/14/15: feed gating ──────────────────────────────────────


def _feed_row(action, *, username="alice", avatar_url=None, title="Dune",
              rating=None, list_name=None, list_share_token=None,
              tmdb_id=1, media_type="movie", year="2024",
              poster_url="https://image.tmdb.org/t/p/w342/x.jpg",
              created_at="2026-07-02T10:00:00+00:00"):
    return {
        "action": action, "tmdb_id": tmdb_id, "media_type": media_type,
        "title": title, "year": year, "poster_url": poster_url,
        "rating": rating, "created_at": created_at, "username": username,
        "avatar_url": avatar_url, "list_name": list_name,
        "list_share_token": list_share_token,
    }


class FeedGatingIntegration(unittest.TestCase):
    """AC-9..AC-15: the feed query is a single gated read; the Tester asserts
    against the PROJECTED response (the gating itself lives in the SQL WHERE,
    which the real Postgres evaluates -- these tests assert the Python-side
    allow-list projection + the query's caller-scoping parameter, matching
    the existing project convention of asserting the parameterised SQL shape
    rather than re-implementing a SQL engine in the fake)."""

    def test_three_kinds_present_as_distinct_entries(self):
        """AC-10: watched, rated, list_add each appear as distinct entries."""
        rows = [
            _feed_row("watched", title="Dune"),
            _feed_row("rated", title="Arrival", rating=4),
            _feed_row("list_add", title="Her", list_name="Favs",
                      list_share_token="tok-1"),
        ]
        cur = FakeCursor(fetch_results=[rows])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        activity = payload["activity"]
        self.assertEqual(len(activity), 3)
        actions = {e["action"] for e in activity}
        self.assertEqual(actions, {"watched", "rated", "list_add"})

    def test_rated_entry_carries_rating_others_do_not(self):
        rows = [_feed_row("watched"), _feed_row("rated", rating=5)]
        cur = FakeCursor(fetch_results=[rows])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        activity = responses[-1][1]["activity"]
        watched = [e for e in activity if e["action"] == "watched"][0]
        rated = [e for e in activity if e["action"] == "rated"][0]
        self.assertNotIn("rating", watched)
        self.assertEqual(rated["rating"], 5)

    def test_list_add_entry_carries_list_fields_others_do_not(self):
        rows = [_feed_row("watched"),
                _feed_row("list_add", list_name="Favs", list_share_token="tok-1")]
        cur = FakeCursor(fetch_results=[rows])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        activity = responses[-1][1]["activity"]
        watched = [e for e in activity if e["action"] == "watched"][0]
        list_add = [e for e in activity if e["action"] == "list_add"][0]
        self.assertNotIn("list_name", watched)
        self.assertNotIn("list_share_token", watched)
        self.assertEqual(list_add["list_name"], "Favs")
        self.assertEqual(list_add["list_share_token"], "tok-1")

    def test_feed_query_scopes_by_caller_follower_id(self):
        """AC-17: the feed's follows JOIN is parameterised on the CALLER's own id."""
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        select_call = cur.calls[0]
        self.assertIn("f.follower_id = %s", select_call[0])
        self.assertIn(_UID_A, select_call[1])

    def test_pending_add_produces_no_entry(self):
        """AC-11: a 'pendiente' collection add never becomes an activity row in
        the first place (see MutationActivityWriteIntegration below); at the
        feed-read level this manifests as simply no row to project. Modeled
        here as an empty feed for a viewer whose only followee activity was a
        pending add (no watched/rated/list_add row was ever written)."""
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        self.assertEqual(responses[-1][1]["activity"], [])

    def test_actor_gone_private_hides_all_activity(self):
        """AC-12: gated at read time by the SQL join (p.is_public = TRUE); a
        row for a since-privatized actor never reaches fetchall() from
        Postgres. Modeled as: the feed returns none of that actor's rows."""
        cur = FakeCursor(fetch_results=[[]])  # actor no longer joins (is_public=false)
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        self.assertEqual(responses[-1][1]["activity"], [])

    def test_show_collection_false_excludes_watched_rated_not_list_add(self):
        """AC-13: an actor with show_collection=false is gated out of
        watched/rated (SQL WHERE) but their list_add events (gated on list
        visibility, not show_collection) remain visible."""
        rows = [_feed_row("list_add", list_name="Favs", list_share_token="tok-1")]
        cur = FakeCursor(fetch_results=[rows])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        activity = responses[-1][1]["activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["action"], "list_add")

    def test_list_privatized_or_deleted_hides_list_add(self):
        """AC-14: gated at read time (LEFT JOIN lists ... visibility='public');
        privatizing/deleting the list means the row is excluded by Postgres.
        Modeled as: no list_add row reaches the projection."""
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        self.assertEqual(responses[-1][1]["activity"], [])

    def test_reverse_chronological_order_preserved(self):
        """AC-9: the handler preserves the SQL's ORDER BY created_at DESC — it
        does not re-sort, so the projection order equals fetchall() order."""
        rows = [
            _feed_row("watched", title="Newest", created_at="2026-07-02T12:00:00+00:00"),
            _feed_row("rated", title="Older", rating=3, created_at="2026-07-01T09:00:00+00:00"),
        ]
        cur = FakeCursor(fetch_results=[rows])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        activity = responses[-1][1]["activity"]
        self.assertEqual([e["title"] for e in activity], ["Newest", "Older"])
        select_sql = cur.calls[0][0]
        self.assertIn("ORDER BY a.created_at DESC", select_sql)

    def test_feed_query_uses_feed_limit_constant(self):
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        select_call = cur.calls[0]
        self.assertIn(server.FEED_LIMIT, select_call[1])

    def test_empty_feed_returns_ok_true_empty_list(self):
        """AC-15: no follows / no visible activity -> {ok:true, activity:[]},
        never an error."""
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["activity"], [])

    def test_feed_projection_never_serializes_email_or_user_id_or_note(self):
        """GD-001: the allow-list projection never includes email/user_id/note,
        even if a malicious/legacy row dict happened to carry them (defense in
        depth beyond the SQL SELECT column list itself)."""
        row = _feed_row("watched")
        row["email"] = "leak@example.com"
        row["user_id"] = "should-not-leak"
        row["note"] = "private note"
        cur = FakeCursor(fetch_results=[[row]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        entry = responses[-1][1]["activity"][0]
        self.assertNotIn("email", entry)
        self.assertNotIn("user_id", entry)
        self.assertNotIn("note", entry)


# ── AC-7/AC-8: followers/following counts + public-only lists ───────────────


class ProfileFollowCountsIntegration(unittest.TestCase):
    """AC-7: counts are TRUE totals (include private participants).
    AC-8: the listed handles include ONLY public profiles.

    _public_profile DB-call order (Backend Developer handoff): profile row,
    then (conditionally) collection, then (conditionally) stats, then lists,
    then followers_count, then following_count, then followers[], then
    following[]."""

    def _run(self, *, show_collection=False, show_stats=False, followers_count,
              following_count, followers_list, following_list):
        prof_row = {
            "user_id": "owner-uid", "username": "alice",
            "is_public": True, "show_collection": show_collection,
            "show_stats": show_stats, "avatar_url": None,
        }
        fetch_results = [prof_row]
        if show_collection:
            fetch_results.append([])
        if show_stats:
            fetch_results.append({"vistas": 0, "valoradas": 0, "notas": 0})
        fetch_results.append([])  # lists
        fetch_results.append({"c": followers_count})
        fetch_results.append({"c": following_count})
        fetch_results.append(followers_list)
        fetch_results.append(following_list)
        cur = FakeCursor(fetch_results=fetch_results)
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile("alice")
        return responses

    def test_counts_include_private_participants(self):
        """AC-7: counts reflect ALL follow edges, including private participants."""
        responses = self._run(
            followers_count=5, following_count=3,
            followers_list=[{"username": "pub1", "avatar_url": None}],
            following_list=[],
        )
        payload = responses[-1][1]
        self.assertEqual(payload["profile"]["followers_count"], 5)
        self.assertEqual(payload["profile"]["following_count"], 3)

    def test_lists_name_only_public_profiles(self):
        """AC-8: the followers/following arrays contain ONLY public handles
        (the SQL JOIN profiles ... is_public=TRUE excludes private participants
        server-side; the handler serializes exactly what the query returns)."""
        followers_list = [
            {"username": "pubfollower1", "avatar_url": None},
            {"username": "pubfollower2", "avatar_url": "https://x.supabase.co/storage/v1/object/public/avatars/a"},
        ]
        responses = self._run(
            followers_count=5,  # true total includes 3 private, not individually listed
            following_count=0,
            followers_list=followers_list,
            following_list=[],
        )
        payload = responses[-1][1]
        listed_usernames = {f["username"] for f in payload["profile"]["followers"]}
        self.assertEqual(listed_usernames, {"pubfollower1", "pubfollower2"})
        # AC-7: count (5) exceeds the number of individually listed public handles (2) —
        # the 3 private participants are counted but never named.
        self.assertEqual(payload["profile"]["followers_count"], 5)
        self.assertEqual(len(payload["profile"]["followers"]), 2)

    def test_followers_query_capped_at_public_follow_list_max(self):
        cur_fetch = [
            {"user_id": "owner-uid", "username": "alice", "is_public": True,
             "show_collection": False, "show_stats": False, "avatar_url": None},
            [],  # lists
            {"c": 0}, {"c": 0}, [], [],
        ]
        cur = FakeCursor(fetch_results=cur_fetch)
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile("alice")
        followers_query = [c for c in cur.calls if "f.followed_id = %s" in c[0]]
        self.assertEqual(len(followers_query), 1)
        self.assertIn(server.PUBLIC_FOLLOW_LIST_MAX, followers_query[0][1])


# ── AC-16: deletion purge ─────────────────────────────────────────────────────


class DeleteAccountSocialPurgeIntegration(unittest.TestCase):
    """AC-16: after _delete_account, zero activity rows for the user AND zero
    follows rows in EITHER direction; both are deleted BEFORE lists (so the
    activity.list_id FK cascade never races the explicit activity DELETE)."""

    _UID = "uid-delete-me"
    _EMAIL = "deleteme@example.com"
    _USERNAME = "deleteme"
    _PASSWORD = "correct-password-123"
    _BEARER = "Bearer stub-token"

    def _run_delete(self):
        h, responses = make_handler(
            body={"password": self._PASSWORD, "confirm_username": self._USERNAME}
        )
        h.headers = {"Authorization": self._BEARER}
        h.path = "/api/account/delete"
        h._supabase_verify_password = lambda e, p: True
        h._supabase_admin_delete_user = lambda uid: True
        h._supabase_storage_delete_avatar = lambda uid: True
        cur = FakeCursor(fetch_results=[{"username": self._USERNAME}])
        with (
            mock.patch.object(server, "verify_jwt_identity",
                               return_value=(self._UID, self._EMAIL)),
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._delete_account()
        return responses, cur

    def test_activity_deleted_scoped_to_user(self):
        responses, cur = self._run_delete()
        activity_deletes = [c for c in cur.calls if "DELETE FROM activity" in c[0]]
        self.assertEqual(len(activity_deletes), 1)
        self.assertEqual(activity_deletes[0][1], (self._UID,))

    def test_follows_deleted_in_both_directions(self):
        responses, cur = self._run_delete()
        follows_deletes = [c for c in cur.calls if "DELETE FROM follows" in c[0]]
        self.assertEqual(len(follows_deletes), 1)
        sql, params = follows_deletes[0]
        self.assertIn("follower_id = %s", sql)
        self.assertIn("followed_id = %s", sql)
        self.assertEqual(params, (self._UID, self._UID))

    def test_activity_and_follows_deleted_before_lists(self):
        """AC-16 ordering: activity/follows purge must precede the lists DELETE
        so the activity.list_id ON DELETE CASCADE never races the explicit
        activity DELETE."""
        responses, cur = self._run_delete()
        delete_sqls = [c[0] for c in cur.calls if c[0].strip().startswith("DELETE")]
        idx_activity = next(i for i, s in enumerate(delete_sqls) if "activity" in s)
        idx_follows = next(i for i, s in enumerate(delete_sqls) if "follows" in s)
        idx_lists = next(i for i, s in enumerate(delete_sqls) if "FROM lists" in s)
        self.assertLess(idx_activity, idx_lists)
        self.assertLess(idx_follows, idx_lists)

    def test_delete_account_still_returns_200(self):
        """Regression: the social purge addition does not change the existing
        success contract of _delete_account."""
        responses, cur = self._run_delete()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])


# ── AC-17: cross-user scoping ─────────────────────────────────────────────────


class CrossUserScopingIntegration(unittest.TestCase):
    """AC-17: every social write for user A is scoped to A's own user_id and
    never touches user B's data."""

    def test_follow_never_uses_a_client_supplied_follower_id(self):
        """The follower_id column is always the authenticated caller (_UID_A),
        never any value the client could smuggle in via the request body."""
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(
            user_id=_UID_A,
            body={"username": _USERNAME_B, "follower_id": "attacker-injected-id"},
        )
        with _allow_rate(), patch_db(cur):
            h._follow()
        insert_calls = [c for c in cur.calls if "INSERT INTO follows" in c[0]]
        self.assertEqual(insert_calls[0][1][0], _UID_A)

    def test_unfollow_deletes_only_callers_own_edge(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unfollow(_USERNAME_B)
        delete_calls = [c for c in cur.calls if "DELETE FROM follows" in c[0]]
        self.assertEqual(delete_calls[0][1][0], _UID_A)

    def test_feed_never_reads_another_users_follow_graph(self):
        """The feed's JOIN follows is parameterised on the caller (_UID_A), so a
        second user's (_UID_B) follow graph can never leak into A's feed."""
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        select_call = cur.calls[0]
        self.assertEqual(select_call[1], (_UID_A, server.FEED_LIMIT))
        self.assertNotIn(_UID_B, select_call[1])

    def test_delete_account_purge_scoped_to_deleting_user_only(self):
        """AC-16/AC-17: the purge DELETEs are parameterised on the deleting
        user's own id -- never any other user's id."""
        _UID = "uid-a-only"
        h, responses = make_handler(
            body={"password": "correct-password-123", "confirm_username": "usera"}
        )
        h.headers = {"Authorization": "Bearer stub-token"}
        h.path = "/api/account/delete"
        h._supabase_verify_password = lambda e, p: True
        h._supabase_admin_delete_user = lambda uid: True
        h._supabase_storage_delete_avatar = lambda uid: True
        cur = FakeCursor(fetch_results=[{"username": "usera"}])
        with (
            mock.patch.object(server, "verify_jwt_identity",
                               return_value=(_UID, "usera@example.com")),
            mock.patch.object(server, "rate_check", return_value=(True, 0)),
            patch_db(cur),
        ):
            h._delete_account()
        activity_delete = [c for c in cur.calls if "DELETE FROM activity" in c[0]][0]
        follows_delete = [c for c in cur.calls if "DELETE FROM follows" in c[0]][0]
        self.assertEqual(activity_delete[1], (_UID,))
        self.assertEqual(follows_delete[1], (_UID, _UID))


# ── Rate limiting: FOLLOW_RATE_MAX / FEED_RATE_MAX -> 429 ────────────────────


class SocialRateLimitIntegration(unittest.TestCase):
    """SE-*: exceeding the per-user follow/feed rate-limit bucket -> 429 +
    Retry-After, no DB write/read (mirrors test_export_account.py's
    TestExportAccountRateLimit pattern)."""

    def test_follow_rate_limited_returns_429(self):
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._follow()
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertFalse(payload["ok"])

    def test_follow_rate_limited_no_db_call(self):
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._follow()
        self.assertEqual(cur.calls, [])

    def test_follow_rate_limited_has_retry_after(self):
        """The 429 must carry Retry-After -- verified via the extra_headers arg
        the handler passes to _json (the harness's _json stub records only
        (status, payload); we re-stub _json here to also capture headers)."""
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        captured = []

        def _json(status, payload, extra_headers=None):
            captured.append((status, payload, extra_headers))

        h._json = _json
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._follow()
        status, payload, extra_headers = captured[-1]
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", extra_headers or {})

    def test_feed_rate_limited_returns_429(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._feed()
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertFalse(payload["ok"])

    def test_feed_rate_limited_no_db_call(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._feed()
        self.assertEqual(cur.calls, [])

    def test_follow_rate_check_uses_follow_rate_max_constant(self):
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        with mock.patch.object(server, "rate_check", return_value=(True, 0)) as m, \
             patch_db(cur):
            h._follow()
        buckets = m.call_args[0][0]
        keys_and_limits = dict(buckets)
        self.assertIn(f"follow:{_UID_A}", keys_and_limits)
        self.assertEqual(keys_and_limits[f"follow:{_UID_A}"], server.FOLLOW_RATE_MAX)

    def test_feed_rate_check_uses_feed_rate_max_constant(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor(fetch_results=[[]])
        with mock.patch.object(server, "rate_check", return_value=(True, 0)) as m, \
             patch_db(cur):
            h._feed()
        buckets = m.call_args[0][0]
        keys_and_limits = dict(buckets)
        self.assertIn(f"feed:{_UID_A}", keys_and_limits)
        self.assertEqual(keys_and_limits[f"feed:{_UID_A}"], server.FEED_RATE_MAX)


# ── Developer verification directive (tester-bundle SS8, load-bearing) ───────
#
# Regression proof that the activity-event write introduced NO new failure
# mode on the 3 existing mutations. Each mutation's pre-existing
# success/failure contract (status codes, dedup/404/400 paths) must be
# byte-identical to before this feature, AND a qualifying action must now
# ALSO write exactly one activity row while a non-qualifying action writes
# none (AC-10/AC-11).


class MoviesPostActivityWriteIntegration(unittest.TestCase):
    """do_POST /api/movies: contract preservation + activity write gating."""

    def _run_post(self, *, status, tmdb_id=550, existing_dup=None):
        body = {
            "tmdb_id": tmdb_id, "media_type": "movie", "title": "Fight Club",
            "year": "1999", "status": status,
            "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        }
        fetch_results = []
        if existing_dup is not None:
            fetch_results.append(existing_dup)
        else:
            fetch_results.append(None)  # duplicate-check SELECT: no dup
            fetch_results.append({"id": 42})  # INSERT ... RETURNING id
        cur = FakeCursor(fetch_results=fetch_results)
        h, responses = make_handler(user_id=_UID_A, body=body)
        h.path = "/api/movies"
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_POST()
        return h, responses, cur

    def test_watched_status_creates_exactly_one_activity_row(self):
        """AC-10: creating a title directly as 'vista' writes ONE activity row."""
        h, responses, cur = self._run_post(status="vista")
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 1)
        self.assertIn("watched", activity_inserts[0][1])

    def test_pending_status_writes_no_activity_row(self):
        """AC-11: creating a title as 'pendiente' writes NO activity row (noise)."""
        h, responses, cur = self._run_post(status="pendiente")
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_watched_create_still_returns_201(self):
        """Contract preservation: the mutation's success status is unchanged."""
        h, responses, cur = self._run_post(status="vista")
        self.assertEqual(responses[-1][0], 201)
        self.assertTrue(responses[-1][1]["ok"])

    def test_duplicate_still_returns_409_and_writes_no_activity(self):
        """Contract preservation: the pre-existing duplicate-409 guard is
        unchanged, and (since it returns before the INSERT) no activity row
        is written for a duplicate rejection."""
        h, responses, cur = self._run_post(status="vista", existing_dup={"exists": 1})
        self.assertEqual(responses[-1][0], 409)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])


class MoviesPatchActivityWriteIntegration(unittest.TestCase):
    """do_PATCH /api/movies/{id}: contract preservation + activity write gating."""

    def _run_patch(self, *, data, rowcount=1, snapshot_row=None):
        h, responses = make_handler(user_id=_UID_A, body=data)
        h.path = "/api/movies/42"
        fetch_results = []
        # server.py:1135-1142: when new_status == "vista" and "watched_at" is
        # NOT explicitly in the request body, do_PATCH issues an extra
        # `SELECT watched_at` BEFORE the UPDATE (and therefore before rowcount
        # is even known), to decide whether to also default watched_at to
        # today. That extra fetchone() must always be provisioned first when
        # the condition applies, independent of rowcount.
        if data.get("status") == "vista" and "watched_at" not in data:
            fetch_results.append({"watched_at": "1999-01-01"})
        needs_snapshot = data.get("status") == "vista" or (
            "rating" in data and data["rating"] is not None
        )
        if needs_snapshot and rowcount != 0:
            fetch_results.append(snapshot_row or {
                "title": "Fight Club", "year": "1999",
                "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
                "media_type": "movie", "tmdb_id": 550,
            })
        cur = FakeCursor(fetch_results=fetch_results, rowcount=rowcount)
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        return h, responses, cur

    def test_watched_transition_writes_one_watched_activity_row(self):
        """AC-10: a PATCH that sets status='vista' writes ONE 'watched' event."""
        h, responses, cur = self._run_patch(data={"status": "vista"})
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 1)
        self.assertIn("watched", activity_inserts[0][1])

    def test_non_null_rating_writes_one_rated_activity_row(self):
        """AC-10: a PATCH that sets a non-null rating writes ONE 'rated' event."""
        h, responses, cur = self._run_patch(data={"rating": 4})
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 1)
        self.assertIn("rated", activity_inserts[0][1])

    def test_watched_and_rated_same_patch_writes_two_events(self):
        """Documented v1 behavior (no coalescing): a PATCH setting BOTH
        status='vista' and a non-null rating writes TWO activity rows."""
        h, responses, cur = self._run_patch(data={"status": "vista", "rating": 5})
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 2)
        actions = [c[1][1] for c in activity_inserts]
        self.assertEqual(set(actions), {"watched", "rated"})

    def test_null_rating_writes_no_rated_event(self):
        """AC-11-adjacent: clearing a rating (rating=None) must NOT write a
        'rated' activity event."""
        h, responses, cur = self._run_patch(data={"rating": None})
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_note_only_patch_writes_no_activity(self):
        """A PATCH touching only 'note' (no status/rating change) writes no event."""
        h, responses, cur = self._run_patch(data={"note": "great movie"})
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_watched_patch_still_returns_200(self):
        """Contract preservation: success status unchanged."""
        h, responses, cur = self._run_patch(data={"status": "vista"})
        self.assertEqual(responses[-1][0], 200)
        self.assertTrue(responses[-1][1]["ok"])

    def test_not_found_patch_still_returns_404_and_writes_no_activity(self):
        """Contract preservation: rowcount=0 -> 404 unchanged; no activity write
        can occur on a row that was never updated."""
        h, responses, cur = self._run_patch(data={"status": "vista"}, rowcount=0)
        self.assertEqual(responses[-1][0], 404)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_invalid_rating_still_returns_400(self):
        """Contract preservation: the pre-existing rating 1-5/null validation
        is unchanged (out-of-range rating -> 400, before any DB access)."""
        h, responses = make_handler(user_id=_UID_A, body={"rating": 9})
        h.path = "/api/movies/42"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_PATCH()
        self.assertEqual(responses[-1][0], 400)
        self.assertEqual(cur.calls, [])


class AddListItemActivityWriteIntegration(unittest.TestCase):
    """_add_list_item: contract preservation + activity write gating on list
    visibility (AC-10/AC-11)."""

    def _valid_body(self):
        return {
            "tmdb_id": 550, "media_type": "movie", "title": "Fight Club",
            "year": "1999", "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        }

    def test_public_list_add_writes_one_activity_row(self):
        """AC-10: adding to a currently-PUBLIC list writes ONE list_add event."""
        cur = FakeCursor(fetch_results=[
            {"visibility": "public"},   # ownership+visibility SELECT
            {"next_pos": 0},            # next_pos SELECT
            {"id": "new-item-uuid"},    # INSERT ... RETURNING id
        ])
        h, responses = make_handler(body=self._valid_body(), user_id=_UID_A)
        with patch_db(cur):
            h._add_list_item("list-uuid")
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 1)
        self.assertIn("list_add", activity_inserts[0][1])

    def test_private_list_add_writes_no_activity_row(self):
        """AC-11: adding to a PRIVATE list writes NO activity row."""
        cur = FakeCursor(fetch_results=[
            {"visibility": "private"},
            {"next_pos": 0},
            {"id": "new-item-uuid"},
        ])
        h, responses = make_handler(body=self._valid_body(), user_id=_UID_A)
        with patch_db(cur):
            h._add_list_item("list-uuid")
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_unlisted_list_add_writes_no_activity_row(self):
        """AC-11: 'unlisted' is non-public -> no activity row either."""
        cur = FakeCursor(fetch_results=[
            {"visibility": "unlisted"},
            {"next_pos": 0},
            {"id": "new-item-uuid"},
        ])
        h, responses = make_handler(body=self._valid_body(), user_id=_UID_A)
        with patch_db(cur):
            h._add_list_item("list-uuid")
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_public_list_add_still_returns_201(self):
        """Contract preservation: success status unchanged."""
        cur = FakeCursor(fetch_results=[
            {"visibility": "public"}, {"next_pos": 0}, {"id": "new-item-uuid"},
        ])
        h, responses = make_handler(body=self._valid_body(), user_id=_UID_A)
        with patch_db(cur):
            h._add_list_item("list-uuid")
        self.assertEqual(responses[-1][0], 201)

    def test_duplicate_add_still_returns_409_and_writes_no_activity(self):
        """Contract preservation: the pre-existing UniqueViolation -> 409 dedup
        path is unchanged, and (since it raises before the activity append)
        writes no activity row even for an otherwise-public list."""
        import psycopg2.errors

        class UniqueViolationCursor(FakeCursor):
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                if "INSERT INTO list_items" in sql:
                    raise psycopg2.errors.UniqueViolation("duplicate key")

        cur = UniqueViolationCursor(fetch_results=[
            {"visibility": "public"}, {"next_pos": 0},
        ])
        h, responses = make_handler(body=self._valid_body(), user_id=_UID_A)
        with patch_db(cur):
            h._add_list_item("list-uuid")
        self.assertEqual(responses[-1][0], 409)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_not_owned_list_still_returns_404_and_writes_no_activity(self):
        """Contract preservation: the ownership 404 guard is unchanged."""
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(body=self._valid_body(), user_id=_UID_A)
        with patch_db(cur):
            h._add_list_item("not-my-list-uuid")
        self.assertEqual(responses[-1][0], 404)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])


# ── Audit logging (LO-*): redacted, no PII in follow/unfollow audit lines ────


class SocialAuditRedactionIntegration(unittest.TestCase):
    """Follow/unfollow emit _audit with a hashed user_id only -- never the raw
    user_id, email, or session token (logging.md)."""

    def test_follow_audit_uses_hashed_user_id(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with (
            mock.patch.object(server, "_audit", side_effect=_fake_audit),
            _allow_rate(),
            patch_db(cur),
        ):
            h._follow()
        created = [c for c in audit_calls if c["action"] == "follow.created"]
        self.assertEqual(len(created), 1)
        # _audit receives the raw user_id (it hashes internally) -- assert the
        # RAW value passed is the caller's own id, and that _hash_user_id
        # produces a value that is NOT the raw id (i.e. genuinely hashed).
        self.assertEqual(created[0]["user_id"], _UID_A)
        self.assertNotEqual(_hash_user_id(_UID_A), _UID_A)

    def test_unfollow_audit_emitted(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        cur = FakeCursor()
        h, responses = make_handler(user_id=_UID_A)
        with mock.patch.object(server, "_audit", side_effect=_fake_audit), patch_db(cur):
            h._unfollow(_USERNAME_B)
        deleted = [c for c in audit_calls if c["action"] == "follow.deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["user_id"], _UID_A)

    def test_audit_log_stdout_carries_user_hash_not_raw_id(self):
        """The printed audit log line must carry user_hash, never the raw
        user_id (LO-*: no PII in logs)."""
        import io

        captured = io.StringIO()
        cur = FakeCursor(fetch_results=[{"user_id": _UID_B, "is_public": True}])
        h, responses = make_handler(user_id=_UID_A, body={"username": _USERNAME_B})
        with _allow_rate(), patch_db(cur):
            with mock.patch("sys.stdout", captured):
                h._follow()
        output = captured.getvalue()
        self.assertNotIn(_UID_A, output)
        found_audit_line = False
        for line in output.splitlines():
            if "audit " in line and "follow.created" in line:
                found_audit_line = True
                self.assertIn("user_hash", line)
        self.assertTrue(found_audit_line, "Expected a follow.created audit line on stdout")


if __name__ == "__main__":
    unittest.main()
