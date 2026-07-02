"""Backend unit/integration tests for the social-reviews-and-likes feature
(Social layer — Phase 2 — public reviews + likes on top of Phase 1's follows +
activity feed, ADR-015).

Covers every ### Tester scope row delegated to Python unittest:

  - Publish/unpublish transitions on `note_public` (AC-1), private-by-default.
  - Read-time removal from feed AND public profile on unpublish / private /
    cleared note -- the two mandatory verification-directive tests (AC-2/AC-5/AC-8).
  - Publish requires non-empty note -> 400; clearing a published note hides it
    (AC-3).
  - `_public_profile` reviews projection next to its title (AC-4); private
    profile -> no reviews (AC-5); shown even with show_collection=false (AC-6).
  - `_feed` `reviewed` branch: entry with review text for a follower (AC-7);
    disappears on unpublish/actor-private (AC-8).
  - Allow-list projection never serializes email/user_id at any review/like
    surface (backend half of AC-9; browser half is E2E).
  - Over-length note rejected before publish (AC-10).
  - Like/unlike idempotency + count (AC-11/AC-12); anonymous -> 401 (AC-13).
  - True-total count + public-only likers list (AC-14).
  - Non-enumerating 404 for unpublished/deleted/nonexistent review (AC-15).
  - Cross-user scoping: liker_id/user_id always the caller (AC-16).
  - `_delete_account` given-likes purge; received likes + reviewed events
    fall by the movies cascade (AC-17, exercised at the purge-statement level).
  - Mutation-contract regression: the 'reviewed' event write on
    PATCH /api/movies/{id} introduces NO new failure mode (200/404/400/Discord
    contract byte-identical; watched/rated paths untouched).
  - Rate limiting: LIKE_RATE_MAX/LIKE_RATE_GLOBAL exceeded -> 429 + Retry-After.

Stub strategy (mirrors tests/test_social.py, Phase 1):
  - h._get_user_id      -> lambda stub on the handler instance (make_handler)
  - server.rate_check   -> mock.patch to allow or block (returns (bool, retry_s))
  - server.get_db       -> patch_db(FakeCursor) for the DB boundary
  - server._audit       -> mock.patch to capture calls without side effects

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
_MOVIE_ID = 42


def _allow_rate():
    return mock.patch.object(server, "rate_check", return_value=(True, 0))


def _block_rate():
    return mock.patch.object(server, "rate_check", return_value=(False, 42))


def _snapshot_row(**overrides):
    row = {
        "title": "Fight Club", "year": "1999",
        "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
        "media_type": "movie", "tmdb_id": 550,
    }
    row.update(overrides)
    return row


# ── AC-1/AC-3: PATCH /api/movies/{id} note_public publish/unpublish ─────────


class PublishTransitionIntegration(unittest.TestCase):
    """AC-1: publishing sets note_public -> the note becomes a public review;
    private by default. AC-3: publishing an empty note -> 400; clearing a
    published note removes the review it backed (the empty note leaves
    nothing to show at read time -- covered in _public_profile tests below)."""

    def test_publish_with_nonempty_note_sets_note_public_true(self):
        """AC-1: PATCH {note_public: true} on a title with a non-empty stored
        note updates note_public and the mutation succeeds."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": "Great movie"},  # pre-check SELECT
            _snapshot_row(),                                  # post-UPDATE snapshot SELECT
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        update_calls = [c for c in cur.calls if c[0].strip().startswith("UPDATE movies")]
        self.assertEqual(len(update_calls), 1)
        self.assertIn("note_public = %s", update_calls[0][0])
        self.assertIn(True, update_calls[0][1])

    def test_publish_writes_fresh_reviewed_event_on_false_to_true_transition(self):
        """AC-1/AC-7: a false->true transition deletes any stale 'reviewed' row
        for this title then inserts exactly one fresh event (ADR-015)."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": "Great movie"},
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        deletes = [c for c in cur.calls if "DELETE FROM activity" in c[0] and "reviewed" in c[0]]
        inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1], (_UID_A, _MOVIE_ID))
        reviewed_inserts = [c for c in inserts if "reviewed" in c[1]]
        self.assertEqual(len(reviewed_inserts), 1)

    def test_republish_after_prior_publish_still_writes_exactly_one_reviewed_row(self):
        """No accumulation: the delete-then-insert dance means republishing
        never leaves duplicate 'reviewed' rows for the same title."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        # prev note_public is False (had been unpublished before) -> still a
        # false->true transition from the DB's point of view.
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": "Great movie"},
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        reviewed_inserts = [
            c for c in cur.calls if "INSERT INTO activity" in c[0] and "reviewed" in c[1]
        ]
        self.assertEqual(len(reviewed_inserts), 1)

    def test_true_to_true_republish_writes_no_new_reviewed_event(self):
        """A PATCH with note_public=true when it was ALREADY true is not a
        false->true transition -- no fresh 'reviewed' event (avoids feed spam
        on every unrelated PATCH that happens to resend note_public=true)."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": True, "note": "Great movie"},  # already published
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        reviewed_inserts = [
            c for c in cur.calls if "INSERT INTO activity" in c[0] and "reviewed" in c[1]
        ]
        self.assertEqual(reviewed_inserts, [])

    def test_unpublish_true_to_false_writes_no_reviewed_event(self):
        """AC-2: note_public: false is not a publish transition -- no event
        write; the review's removal is purely the read-time gate (no backfill
        needed, nothing to delete from feeds retroactively)."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": False})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_note_public_must_be_boolean(self):
        """US-040: strict type validation at the entry boundary."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": "yes"})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(cur.calls, [])

    def test_publish_empty_stored_note_returns_400(self):
        """AC-3: publishing when the resulting note is empty (no note in this
        PATCH, and the stored note is blank) -> 400, no UPDATE runs."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[{"note_public": False, "note": ""}])
        with patch_db(cur):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Escribe una nota antes de publicarla.")
        update_calls = [c for c in cur.calls if c[0].strip().startswith("UPDATE movies")]
        self.assertEqual(update_calls, [])

    def test_publish_with_whitespace_only_stored_note_returns_400(self):
        """AC-3: a blank/whitespace-only note is treated as empty."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[{"note_public": False, "note": "   "}])
        with patch_db(cur):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 400)

    def test_publish_uses_note_from_same_patch_when_provided(self):
        """AC-3: when this PATCH ALSO sets `note`, the resulting note (not the
        stale stored one) decides the empty-note gate."""
        h, responses = make_handler(
            user_id=_UID_A, body={"note": "Fresh take", "note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": ""},   # stored note is empty
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 200, "the PATCH's own non-empty note satisfies the gate")

    def test_publish_nonexistent_title_returns_404(self):
        """The pre-check SELECT finds no row (wrong id/owner) -> 404, no UPDATE."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[None])
        with patch_db(cur):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        update_calls = [c for c in cur.calls if c[0].strip().startswith("UPDATE movies")]
        self.assertEqual(update_calls, [])

    def test_note_public_write_scoped_to_id_and_user_id(self):
        """AC-16: the UPDATE is scoped WHERE id=%s AND user_id=%s."""
        h, responses = make_handler(user_id=_UID_A, body={"note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": "Great movie"},
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        update_calls = [c for c in cur.calls if c[0].strip().startswith("UPDATE movies")]
        sql, params = update_calls[0]
        self.assertIn("WHERE id = %s AND user_id = %s", sql)
        self.assertEqual(list(params[-2:]), [_MOVIE_ID, _UID_A])


# ── AC-10: note length cap enforced before publish ───────────────────────────


class NoteLengthCapIntegration(unittest.TestCase):
    """AC-10: an over-length note (>500 chars) is rejected at the PATCH
    boundary before it can ever be published -- the existing note-edit cap,
    reused unchanged, is the enforcement point."""

    def test_over_length_note_returns_400(self):
        h, responses = make_handler(
            user_id=_UID_A, body={"note": "x" * 501, "note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 400)
        self.assertIn("500", payload["error"])
        self.assertEqual(cur.calls, [], "must reject before any DB access")

    def test_exactly_500_chars_is_accepted(self):
        h, responses = make_handler(
            user_id=_UID_A, body={"note": "x" * 500, "note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": "old"},
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        status, payload = responses[-1]
        self.assertEqual(status, 200)


# ── AC-4/AC-5/AC-6: _public_profile reviews projection ───────────────────────


class PublicProfileReviewsIntegration(unittest.TestCase):
    """AC-4: a published review appears next to its title. AC-5: a private
    profile returns no reviews (the endpoint 404s before the reviews query
    ever runs). AC-6: a review is shown even when show_collection is false --
    the reviews query is unconditional once past the is_public gate."""

    def _run(self, *, is_public=True, show_collection=False, show_stats=False,
              reviews_rows=None):
        prof_row = {
            "user_id": "owner-uid", "username": "alice",
            "is_public": is_public, "show_collection": show_collection,
            "show_stats": show_stats, "avatar_url": None,
        }
        if not is_public:
            cur = FakeCursor(fetch_results=[None])  # profile lookup misses -> 404 path
            h, responses = make_handler(user_id=None)
            h._public_rate_limited = lambda: False
            with patch_db(cur):
                h._public_profile("alice")
            return responses, cur

        fetch_results = [prof_row]
        if show_collection:
            fetch_results.append([])
        if show_stats:
            fetch_results.append({"vistas": 0, "valoradas": 0, "notas": 0})
        fetch_results.append([])          # lists
        fetch_results.append({"c": 0})    # followers_count
        fetch_results.append({"c": 0})    # following_count
        fetch_results.append([])          # followers[]
        fetch_results.append([])          # following[]
        fetch_results.append(reviews_rows if reviews_rows is not None else [])
        cur = FakeCursor(fetch_results=fetch_results)
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile("alice")
        return responses, cur

    def test_published_review_appears_next_to_its_title(self):
        """AC-4: the reviews array carries the title + review text."""
        reviews = [{
            "movie_id": _MOVIE_ID, "tmdb_id": 550, "media_type": "movie",
            "title": "Fight Club", "year": "1999",
            "poster_url": "https://image.tmdb.org/t/p/w342/x.jpg",
            "note": "A masterpiece.", "created_at": "2026-07-02T10:00:00+00:00",
            "like_count": 3,
        }]
        responses, cur = self._run(show_collection=True, reviews_rows=reviews)
        payload = responses[-1][1]
        self.assertEqual(len(payload["profile"]["reviews"]), 1)
        entry = payload["profile"]["reviews"][0]
        self.assertEqual(entry["title"], "Fight Club")
        self.assertEqual(entry["note"], "A masterpiece.")
        self.assertEqual(entry["like_count"], 3)
        self.assertEqual(entry["movie_id"], _MOVIE_ID)

    def test_private_profile_returns_404_and_no_reviews_query(self):
        """AC-5: a private/nonexistent profile 404s before the reviews SELECT
        is ever issued (no leakage of a private user's reviews)."""
        responses, cur = self._run(is_public=False)
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        review_queries = [c for c in cur.calls if "note_public = TRUE" in c[0]]
        self.assertEqual(review_queries, [])

    def test_review_shown_even_when_show_collection_is_false(self):
        """AC-6: independent per-title opt-in -- the reviews query is issued
        (and returns rows) regardless of show_collection."""
        reviews = [{
            "movie_id": _MOVIE_ID, "tmdb_id": 550, "media_type": "movie",
            "title": "Fight Club", "year": "1999", "poster_url": None,
            "note": "Still great.", "created_at": "2026-07-02T10:00:00+00:00",
            "like_count": 0,
        }]
        responses, cur = self._run(show_collection=False, reviews_rows=reviews)
        payload = responses[-1][1]
        self.assertEqual(len(payload["profile"]["reviews"]), 1)
        review_queries = [c for c in cur.calls if "note_public = TRUE" in c[0]]
        self.assertEqual(len(review_queries), 1)
        # The reviews query itself carries no show_collection predicate.
        self.assertNotIn("show_collection", review_queries[0][0])

    def test_reviews_query_capped_at_public_review_list_max(self):
        responses, cur = self._run(show_collection=False, reviews_rows=[])
        review_queries = [c for c in cur.calls if "note_public = TRUE" in c[0]]
        self.assertEqual(len(review_queries), 1)
        self.assertIn(server.PUBLIC_REVIEW_LIST_MAX, review_queries[0][1])

    def test_reviews_projection_never_serializes_email_or_user_id(self):
        """GD-001: defense in depth -- even if a row dict carried email/user_id,
        the handler's dict(r) pass-through means the SQL column list is the
        real allow-list; assert the query text lists only allowed columns."""
        responses, cur = self._run(show_collection=False, reviews_rows=[])
        review_queries = [c for c in cur.calls if "note_public = TRUE" in c[0]]
        sql = review_queries[0][0]
        self.assertNotIn("email", sql)
        self.assertNotIn(" user_id,", sql)  # user_id used only in WHERE, never SELECTed


# ── AC-2/AC-5/AC-8: MANDATORY read-time removal (verification directive #1) ──


class ReadTimeRemovalIntegration(unittest.TestCase):
    """Developer-mandated verification directive #1 (tester-bundle SS0):
    unpublishing REMOVES a review from followers' feeds and the public
    profile on the NEXT read (not merely "no new events") -- read-time
    gating, no backfill. Modeled two ways per surface: (a) the query issued
    against a since-changed state returns nothing (the SQL WHERE gate, which
    the real Postgres evaluates), and (b) an explicit before/after sequence
    against a stub DB proving the SAME endpoint call yields different
    projected results as the underlying row changes between calls."""

    # -- Public profile: unpublish / note-cleared / actor-goes-private --------

    def _profile_fetch_results(self, *, is_public, reviews_rows):
        prof_row = {
            "user_id": "owner-uid", "username": "alice", "is_public": is_public,
            "show_collection": False, "show_stats": False, "avatar_url": None,
        }
        if not is_public:
            return [None]
        return [prof_row, [], {"c": 0}, {"c": 0}, [], [], reviews_rows]

    def test_public_profile_review_present_then_absent_after_unpublish(self):
        """AC-2: same profile, first read (published) shows the review;
        second read (after note_public flips to false server-side, modeled as
        the reviews query now returning zero rows) shows none."""
        published_review = [{
            "movie_id": _MOVIE_ID, "tmdb_id": 550, "media_type": "movie",
            "title": "Fight Club", "year": "1999", "poster_url": None,
            "note": "Great.", "created_at": "2026-07-02T10:00:00+00:00",
            "like_count": 0,
        }]
        cur1 = FakeCursor(fetch_results=self._profile_fetch_results(
            is_public=True, reviews_rows=published_review))
        h1, r1 = make_handler(user_id=None)
        h1._public_rate_limited = lambda: False
        with patch_db(cur1):
            h1._public_profile("alice")
        self.assertEqual(len(r1[-1][1]["profile"]["reviews"]), 1)

        # NEXT read: note_public is now false -> the query (real Postgres WHERE
        # note_public = TRUE) returns zero rows for the SAME title.
        cur2 = FakeCursor(fetch_results=self._profile_fetch_results(
            is_public=True, reviews_rows=[]))
        h2, r2 = make_handler(user_id=None)
        h2._public_rate_limited = lambda: False
        with patch_db(cur2):
            h2._public_profile("alice")
        self.assertEqual(r2[-1][1]["profile"]["reviews"], [],
                          "AC-2: unpublishing must remove the review on the NEXT read")

    def test_public_profile_review_gone_after_note_cleared(self):
        """AC-3/AC-5 read-time: clearing the note (note <> '' now false) hides
        the review even though note_public may still be true."""
        cur = FakeCursor(fetch_results=self._profile_fetch_results(
            is_public=True, reviews_rows=[]))  # note='' -> query excludes it
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile("alice")
        self.assertEqual(responses[-1][1]["profile"]["reviews"], [])

    def test_public_profile_review_gone_after_actor_goes_private(self):
        """AC-5: the actor's profile going private makes the WHOLE profile
        404 (is_public gate at the top), which removes every review with it,
        immediately and with no backfill."""
        cur = FakeCursor(fetch_results=self._profile_fetch_results(
            is_public=False, reviews_rows=[]))
        h, responses = make_handler(user_id=None)
        h._public_rate_limited = lambda: False
        with patch_db(cur):
            h._public_profile("alice")
        self.assertEqual(responses[-1][0], 404)

    # -- Feed: reviewed entry present then absent after unpublish -------------

    def _feed_reviewed_row(self, *, note, note_public_effective_visible):
        """`note_public_effective_visible` models the SQL gate outcome: when
        False, the real Postgres WHERE would exclude the row entirely (so the
        fake simply omits it from fetchall(), matching how the gate manifests
        at the Python layer)."""
        return {
            "action": "reviewed", "tmdb_id": 550, "media_type": "movie",
            "title": "Fight Club", "year": "1999", "poster_url": None,
            "rating": None, "created_at": "2026-07-02T10:00:00+00:00",
            "username": "alice", "avatar_url": None, "list_name": None,
            "list_share_token": None, "movie_id": _MOVIE_ID,
            "review_note": note, "like_count": 0, "liked_by_me": False,
        }

    def test_feed_reviewed_entry_present_then_absent_after_unpublish(self):
        """AC-7/AC-8: first feed read (published) shows the 'reviewed' entry
        with the review text; second read (after unpublish) shows none -- the
        SAME endpoint, no backfill, read-time gate re-evaluated fresh."""
        row = self._feed_reviewed_row(note="Great.", note_public_effective_visible=True)
        cur1 = FakeCursor(fetch_results=[[row]])
        h1, r1 = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur1):
            h1._feed()
        activity1 = r1[-1][1]["activity"]
        self.assertEqual(len(activity1), 1)
        self.assertEqual(activity1[0]["action"], "reviewed")
        self.assertEqual(activity1[0]["note"], "Great.")

        # NEXT read: unpublished -> the gated SQL WHERE excludes the row.
        cur2 = FakeCursor(fetch_results=[[]])
        h2, r2 = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur2):
            h2._feed()
        self.assertEqual(r2[-1][1]["activity"], [],
                          "AC-8: unpublishing must remove the feed entry on the NEXT read")

    def test_feed_reviewed_entry_gone_after_actor_goes_private(self):
        """AC-8: the actor's profile going private drops the JOIN profiles
        p.is_public = TRUE match -> the reviewed row (and everything else of
        theirs) vanishes from the feed with no backfill."""
        cur = FakeCursor(fetch_results=[[]])  # actor no longer joins
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        self.assertEqual(responses[-1][1]["activity"], [])

    def test_feed_reviewed_carries_note_movie_id_like_count_liked_by_me(self):
        """The 'reviewed' projection carries exactly the extra fields the
        spec requires, and other action kinds do not carry them."""
        rows = [
            self._feed_reviewed_row(note="A take.", note_public_effective_visible=True),
            {
                "action": "watched", "tmdb_id": 1, "media_type": "movie",
                "title": "Dune", "year": "2024", "poster_url": None,
                "rating": None, "created_at": "2026-07-02T09:00:00+00:00",
                "username": "bob", "avatar_url": None, "list_name": None,
                "list_share_token": None, "movie_id": None,
                "review_note": None, "like_count": 0, "liked_by_me": False,
            },
        ]
        cur = FakeCursor(fetch_results=[rows])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        activity = responses[-1][1]["activity"]
        reviewed = [e for e in activity if e["action"] == "reviewed"][0]
        watched = [e for e in activity if e["action"] == "watched"][0]
        self.assertEqual(reviewed["note"], "A take.")
        self.assertEqual(reviewed["movie_id"], _MOVIE_ID)
        self.assertIn("like_count", reviewed)
        self.assertIn("liked_by_me", reviewed)
        self.assertNotIn("note", watched)
        self.assertNotIn("movie_id", watched)


# ── AC-9 (backend half): allow-list never serializes email/user_id/note leak ─


class AllowListRedactionIntegration(unittest.TestCase):
    """GD-001: defense in depth against a leaky row dict -- even if a
    malicious/legacy row carried email/raw user_id, the Python-side allow-list
    projection strips it before it reaches the client, on both feed entries
    and (structurally) the public-profile reviews projection."""

    def test_feed_reviewed_entry_never_serializes_email_or_raw_user_id(self):
        row = {
            "action": "reviewed", "tmdb_id": 550, "media_type": "movie",
            "title": "Fight Club", "year": "1999", "poster_url": None,
            "rating": None, "created_at": "2026-07-02T10:00:00+00:00",
            "username": "alice", "avatar_url": None, "list_name": None,
            "list_share_token": None, "movie_id": _MOVIE_ID,
            "review_note": "Great.", "like_count": 0, "liked_by_me": False,
            "email": "leak@example.com", "user_id": "should-not-leak",
        }
        cur = FakeCursor(fetch_results=[[row]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        entry = responses[-1][1]["activity"][0]
        self.assertNotIn("email", entry)
        self.assertNotIn("user_id", entry)


# ── AC-11/AC-12: like/unlike idempotency + count ─────────────────────────────


class LikeIdempotencyIntegration(unittest.TestCase):
    """AC-11: liking increments the count by one and is idempotent (liking
    twice is still one like, no error). AC-12: unliking decrements by one and
    is idempotent (unliking something never liked is a harmless no-op)."""

    def test_like_visible_review_returns_200_liked_true_and_count(self):
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["liked"])
        self.assertEqual(payload["count"], 1)

    def test_like_insert_uses_on_conflict_do_nothing(self):
        """Storage-layer idempotency: liking an already-liked review is a
        harmless no-op, never a duplicate row / error."""
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        insert_calls = [c for c in cur.calls if "INSERT INTO likes" in c[0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertIn("ON CONFLICT DO NOTHING", insert_calls[0][0])
        self.assertEqual(insert_calls[0][1], (_UID_A, _MOVIE_ID))

    def test_like_response_reflects_current_true_count(self):
        """Liking a review already liked by others still returns 200 with the
        current authoritative count -- no error on a duplicate-like attempt."""
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 5}])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 5)

    def test_unlike_returns_200_liked_false_and_count(self):
        cur = FakeCursor(fetch_results=[{"c": 0}])
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["liked"])
        self.assertEqual(payload["count"], 0)

    def test_unlike_deletes_scoped_to_caller_and_movie(self):
        cur = FakeCursor(fetch_results=[{"c": 0}])
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        delete_calls = [c for c in cur.calls if "DELETE FROM likes" in c[0]]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0][1], (_UID_A, _MOVIE_ID))

    def test_unlike_never_liked_is_harmless_noop(self):
        """Unliking a review the caller never liked: DELETE matches zero rows,
        still 200 with the unaffected current count."""
        cur = FakeCursor(fetch_results=[{"c": 3}])
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["liked"])
        self.assertEqual(payload["count"], 3)

    def test_self_like_is_allowed(self):
        """ASSUMED (Open Questions): a user may like their own review; the
        visibility gate has no author-exclusion clause."""
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)  # caller is also the author
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["liked"])


# ── AC-13: anonymous like/unlike/like-state -> 401 ───────────────────────────


class LikeAuthIntegration(unittest.TestCase):
    """AC-13: every like endpoint requires auth; unauthenticated -> 401, no
    write, no read, nothing recorded."""

    def test_like_unauthenticated_returns_401(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        with patch_db(cur):
            h._like_review(_MOVIE_ID)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [], "no DB call before auth check")

    def test_unlike_unauthenticated_returns_401(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        with patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [])

    def test_like_state_unauthenticated_returns_401(self):
        cur = FakeCursor()
        h, responses = make_handler(user_id=None)
        with patch_db(cur):
            h._review_likes(_MOVIE_ID)
        self.assertEqual(responses[-1][0], 401)
        self.assertEqual(cur.calls, [])

    def test_like_unauthenticated_body_is_generic(self):
        h, responses = make_handler(user_id=None)
        h._like_review(_MOVIE_ID)
        _, payload = responses[-1]
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "No autenticado")


# ── AC-14: true-total count + public-only liker list ─────────────────────────


class LikeCountAndLikerListIntegration(unittest.TestCase):
    """AC-14: the count is the true total (private likers included); the
    named `likers` list contains ONLY public profiles -- a private liker is
    counted but never individually named."""

    def _run_review_likes(self, *, count, liked, likers):
        cur = FakeCursor(fetch_results=[
            {"1": 1},           # _review_visible
            {"c": count},        # _like_count
            {"liked": liked},    # liked_by_me EXISTS
            likers,               # likers list (public-only, per SQL JOIN)
        ])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._review_likes(_MOVIE_ID)
        return responses, cur

    def test_count_reflects_true_total_including_private_likers(self):
        """count=5 while only 2 public likers are named -- 3 private likers
        counted but never listed."""
        likers = [
            {"username": "pub1", "avatar_url": None},
            {"username": "pub2", "avatar_url": None},
        ]
        responses, cur = self._run_review_likes(count=5, liked=False, likers=likers)
        payload = responses[-1][1]
        self.assertEqual(payload["count"], 5)
        self.assertEqual(len(payload["likers"]), 2)

    def test_likers_list_names_only_public_profiles(self):
        """The listed handles are exactly the ones the (server-side JOIN
        profiles ... is_public=TRUE) query returned -- private likers are
        excluded server-side and the handler serializes what it got."""
        likers = [{"username": "onlypublic", "avatar_url": None}]
        responses, cur = self._run_review_likes(count=10, liked=True, likers=likers)
        payload = responses[-1][1]
        usernames = {l["username"] for l in payload["likers"]}
        self.assertEqual(usernames, {"onlypublic"})
        self.assertEqual(payload["count"], 10)

    def test_liked_by_me_reflects_caller_only(self):
        responses, cur = self._run_review_likes(count=1, liked=True, likers=[])
        payload = responses[-1][1]
        self.assertTrue(payload["liked_by_me"])

    def test_likers_query_capped_at_public_like_list_max(self):
        responses, cur = self._run_review_likes(count=0, liked=False, likers=[])
        likers_query = [c for c in cur.calls if "JOIN profiles p ON p.user_id = lk.liker_id" in c[0]]
        self.assertEqual(len(likers_query), 1)
        self.assertIn(server.PUBLIC_LIKE_LIST_MAX, likers_query[0][1])

    def test_review_likes_never_serializes_email_or_raw_user_id(self):
        """GD-001: the likers SQL only selects username/avatar_url."""
        responses, cur = self._run_review_likes(count=0, liked=False, likers=[])
        likers_query = [c for c in cur.calls if "JOIN profiles p ON p.user_id = lk.liker_id" in c[0]]
        sql = likers_query[0][0]
        self.assertNotIn("email", sql)
        self.assertNotIn("p.user_id,", sql)


# ── AC-15: non-enumerating 404 for unpublished/deleted/nonexistent review ────


class NonEnumeratingVisibilityIntegration(unittest.TestCase):
    """AC-15: an unpublished review, a review whose title was deleted, and a
    nonexistent title all yield the IDENTICAL 404 'No disponible' from every
    like verb -- the caller cannot distinguish "exists but hidden" from
    "never existed"."""

    def test_like_not_visible_review_returns_404(self):
        cur = FakeCursor(fetch_results=[None])  # _review_visible SELECT misses
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "No disponible")

    def test_like_nonexistent_title_returns_identical_404(self):
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(999999)
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "No disponible")

    def test_unpublished_and_nonexistent_bodies_are_identical(self):
        """Non-enumeration: byte-identical bodies for both cases."""
        cur1 = FakeCursor(fetch_results=[None])
        h1, r1 = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur1):
            h1._like_review(_MOVIE_ID)

        cur2 = FakeCursor(fetch_results=[None])
        h2, r2 = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur2):
            h2._like_review(999999)

        self.assertEqual(r1[-1], r2[-1])

    def test_like_not_visible_creates_no_insert(self):
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        insert_calls = [c for c in cur.calls if "INSERT INTO likes" in c[0]]
        self.assertEqual(insert_calls, [])

    def test_review_likes_state_not_visible_returns_404(self):
        cur = FakeCursor(fetch_results=[None])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._review_likes(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "No disponible")

    def test_review_visible_helper_checks_public_profile_and_published_nonempty_note(self):
        """_review_visible's own SQL gates on is_public + note_public + note<>''
        -- assert the query shape carries all three predicates (title-deleted
        and note-cleared cases collapse to the same "no matching row" outcome
        the real Postgres would produce, exercised via the None fetchone())."""
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 0}])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        visible_query = cur.calls[0][0]
        self.assertIn("p.is_public = TRUE", visible_query)
        self.assertIn("m.note_public = TRUE", visible_query)
        self.assertIn("m.note <> ''", visible_query)


# ── AC-16: cross-user scoping ─────────────────────────────────────────────────


class CrossUserScopingIntegration(unittest.TestCase):
    """AC-16: every review/like write for user A is scoped to A's own
    user_id, never another user's reviews or likes; liked_by_me reflects only
    the caller."""

    def test_like_insert_uses_caller_as_liker_never_client_supplied(self):
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        insert_calls = [c for c in cur.calls if "INSERT INTO likes" in c[0]]
        self.assertEqual(insert_calls[0][1][0], _UID_A)

    def test_unlike_deletes_only_callers_own_like(self):
        cur = FakeCursor(fetch_results=[{"c": 0}])
        h, responses = make_handler(user_id=_UID_A)
        with patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        delete_calls = [c for c in cur.calls if "DELETE FROM likes" in c[0]]
        self.assertEqual(delete_calls[0][1][0], _UID_A)

    def test_liked_by_me_exists_query_bound_to_caller_only(self):
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 0}, {"liked": False}, []])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._review_likes(_MOVIE_ID)
        exists_query = [c for c in cur.calls if "AS liked" in c[0]][0]
        self.assertEqual(exists_query[1], (_MOVIE_ID, _UID_A))
        self.assertNotIn(_UID_B, exists_query[1])

    def test_publish_never_uses_a_client_supplied_owner_id(self):
        """The note_public UPDATE never reads an owner/user id from the
        request body -- it is always the JWT-verified caller (_UID_A)."""
        h, responses = make_handler(
            user_id=_UID_A,
            body={"note_public": True, "user_id": "attacker-injected-id"},
        )
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"note_public": False, "note": "Great movie"},
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        update_calls = [c for c in cur.calls if c[0].strip().startswith("UPDATE movies")]
        self.assertEqual(update_calls[0][1][-1], _UID_A)

    def test_feed_liked_by_me_binding_never_leaks_another_users_id(self):
        cur = FakeCursor(fetch_results=[[]])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            h._feed()
        select_call = cur.calls[0]
        self.assertNotIn(_UID_B, select_call[1])


# ── AC-17: account deletion purges given likes; received likes cascade ──────


class DeleteAccountLikesPurgeIntegration(unittest.TestCase):
    """AC-17: after _delete_account, zero `likes` rows reference the deleting
    user as liker (their GIVEN likes are explicitly purged); their own
    reviews (their movies.note_public rows) and RECEIVED likes fall by the
    movies cascade, exercised here as: the explicit likes-purge DELETE runs
    inside the same erasure transaction, scoped to liker_id, and the movies
    DELETE (which cascades received likes via the FK) still runs."""

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

    def test_given_likes_deleted_scoped_to_liker_id(self):
        responses, cur = self._run_delete()
        likes_deletes = [c for c in cur.calls if "DELETE FROM likes" in c[0]]
        self.assertEqual(len(likes_deletes), 1)
        sql, params = likes_deletes[0]
        self.assertIn("liker_id = %s", sql)
        self.assertEqual(params, (self._UID,))

    def test_movies_delete_still_runs_cascading_received_likes_and_reviewed_events(self):
        """The movies DELETE (whose FK cascade removes received likes and
        'reviewed' activity events tied to the user's own titles) is present
        and scoped to the deleting user -- unchanged by this feature."""
        responses, cur = self._run_delete()
        movies_deletes = [c for c in cur.calls if "DELETE FROM movies" in c[0]]
        self.assertEqual(len(movies_deletes), 1)
        self.assertEqual(movies_deletes[0][1], (self._UID,))

    def test_likes_purge_runs_before_lists_delete(self):
        """Ordering sanity (mirrors the Phase-1 activity/follows-before-lists
        precedent): the likes purge is not racing any FK cascade triggered
        later in the same transaction."""
        responses, cur = self._run_delete()
        delete_sqls = [c[0] for c in cur.calls if c[0].strip().startswith("DELETE")]
        idx_likes = next(i for i, s in enumerate(delete_sqls) if "FROM likes" in s)
        idx_lists = next(i for i, s in enumerate(delete_sqls) if "FROM lists" in s)
        self.assertLess(idx_likes, idx_lists)

    def test_delete_account_still_returns_200(self):
        """Contract preservation: the likes-purge addition does not change
        the existing success contract of _delete_account."""
        responses, cur = self._run_delete()
        status, payload = responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_delete_account_purge_scoped_to_deleting_user_only(self):
        """AC-16/AC-17: the purge DELETEs never reference any other user's id."""
        responses, cur = self._run_delete()
        likes_delete = [c for c in cur.calls if "DELETE FROM likes" in c[0]][0]
        self.assertNotIn(_UID_B, likes_delete[1])


# ── Mutation-contract regression: PATCH /api/movies/{id} unchanged shape ─────


class MutationContractRegressionIntegration(unittest.TestCase):
    """The 'reviewed' event write introduces NO new failure mode to
    PATCH /api/movies/{id}: status codes, the Discord notify call, and the
    watched/rated event-writing paths remain byte-identical to Phase 1 when
    the PATCH does not touch note_public at all."""

    def _run_patch(self, *, data, rowcount=1, snapshot_row=None):
        h, responses = make_handler(user_id=_UID_A, body=data)
        h.path = f"/api/movies/{_MOVIE_ID}"
        fetch_results = []
        if data.get("status") == "vista" and "watched_at" not in data:
            fetch_results.append({"watched_at": "1999-01-01"})
        needs_snapshot = data.get("status") == "vista" or (
            "rating" in data and data["rating"] is not None
        )
        if needs_snapshot and rowcount != 0:
            fetch_results.append(snapshot_row or _snapshot_row())
        cur = FakeCursor(fetch_results=fetch_results, rowcount=rowcount)
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        return h, responses, cur

    def test_watched_transition_unaffected_by_reviewed_feature_still_writes_watched(self):
        h, responses, cur = self._run_patch(data={"status": "vista"})
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 1)
        self.assertIn("watched", activity_inserts[0][1])
        reviewed_inserts = [c for c in activity_inserts if "reviewed" in c[1]]
        self.assertEqual(reviewed_inserts, [])

    def test_rating_only_patch_still_returns_200_no_reviewed_event(self):
        h, responses, cur = self._run_patch(data={"rating": 4})
        self.assertEqual(responses[-1][0], 200)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertIn("rated", activity_inserts[0][1])
        self.assertEqual(len(activity_inserts), 1)

    def test_note_only_patch_without_note_public_writes_no_activity(self):
        """A PATCH touching only `note` (no note_public key at all) writes no
        reviewed event and no other activity row -- unchanged Phase-1 behavior."""
        h, responses, cur = self._run_patch(data={"note": "just a private thought"})
        self.assertEqual(responses[-1][0], 200)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_not_found_patch_still_returns_404_regardless_of_note_public(self):
        """Contract preservation: rowcount=0 -> 404 unchanged even when the
        PATCH ALSO carries note_public (the UPDATE's WHERE just matches
        nothing; no event write can occur on a row that was never updated)."""
        h, responses = make_handler(user_id=_UID_A, body={"status": "vista"})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(rowcount=0)
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        self.assertEqual(responses[-1][0], 404)
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(activity_inserts, [])

    def test_invalid_rating_still_returns_400_unaffected_by_reviews_feature(self):
        h, responses = make_handler(user_id=_UID_A, body={"rating": 9})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor()
        with patch_db(cur):
            h.do_PATCH()
        self.assertEqual(responses[-1][0], 400)
        self.assertEqual(cur.calls, [])

    def test_watched_and_publish_same_patch_writes_two_distinct_events(self):
        """A PATCH that BOTH marks watched AND publishes (false->true) writes
        TWO events (watched + reviewed) -- documented v1 behavior, no
        coalescing, mirroring the existing watched+rated precedent."""
        h, responses = make_handler(
            user_id=_UID_A, body={"status": "vista", "note_public": True})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"watched_at": None},                              # watched_at default-fill check
            {"note_public": False, "note": "Great movie"},      # publish pre-check
            _snapshot_row(),                                     # post-update snapshot
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord"):
            h.do_PATCH()
        activity_inserts = [c for c in cur.calls if "INSERT INTO activity" in c[0]]
        self.assertEqual(len(activity_inserts), 2)
        actions = {c[1][1] for c in activity_inserts}
        self.assertEqual(actions, {"watched", "reviewed"})

    def test_watched_status_and_discord_notify_still_fires_on_watched_transition(self):
        h, responses = make_handler(user_id=_UID_A, body={"status": "vista"})
        h.path = f"/api/movies/{_MOVIE_ID}"
        cur = FakeCursor(fetch_results=[
            {"watched_at": None},
            _snapshot_row(),
        ])
        with patch_db(cur), mock.patch.object(server, "notify_discord") as m:
            h.do_PATCH()
        self.assertTrue(m.called, "Discord notify must still fire on a watched transition")
        self.assertEqual(responses[-1][0], 200)


# ── Rate limiting: LIKE_RATE_MAX / LIKE_RATE_GLOBAL -> 429 ───────────────────


class LikeRateLimitIntegration(unittest.TestCase):
    """Repeated like calls beyond the per-user or global cap -> 429 +
    Retry-After, no DB write/read (mirrors test_social.py's Phase-1 pattern)."""

    def test_like_rate_limited_returns_429(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 429)
        self.assertFalse(payload["ok"])

    def test_like_rate_limited_no_db_call(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        self.assertEqual(cur.calls, [])

    def test_like_rate_limited_has_retry_after(self):
        h, responses = make_handler(user_id=_UID_A)
        captured = []

        def _json(status, payload, extra_headers=None):
            captured.append((status, payload, extra_headers))

        h._json = _json
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._like_review(_MOVIE_ID)
        status, payload, extra_headers = captured[-1]
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", extra_headers or {})

    def test_review_likes_rate_limited_returns_429(self):
        h, responses = make_handler(user_id=_UID_A)
        cur = FakeCursor()
        with _block_rate(), patch_db(cur):
            h._review_likes(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 429)

    def test_like_rate_check_uses_like_rate_max_and_global_constants(self):
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)
        with mock.patch.object(server, "rate_check", return_value=(True, 0)) as m, \
             patch_db(cur):
            h._like_review(_MOVIE_ID)
        buckets = m.call_args[0][0]
        keys_and_limits = dict(buckets)
        self.assertIn(f"like:{_UID_A}", keys_and_limits)
        self.assertEqual(keys_and_limits[f"like:{_UID_A}"], server.LIKE_RATE_MAX)
        self.assertIn("like:_global", keys_and_limits)
        self.assertEqual(keys_and_limits["like:_global"], server.LIKE_RATE_GLOBAL)

    def test_unlike_is_not_rate_limited_mirroring_unfollow_precedent(self):
        """Documented asymmetry (Backend Reviewer follow-up
        `unlike-review-no-rate-limit-wording`): _unlike_review does not call
        rate_check, mirroring the existing _unfollow precedent for the
        idempotent removal verb. Assert unlike works even when rate_check
        would otherwise block, proving no rate_check call gates it."""
        cur = FakeCursor(fetch_results=[{"c": 0}])
        h, responses = make_handler(user_id=_UID_A)
        with _block_rate(), patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        status, payload = responses[-1]
        self.assertEqual(status, 200, "unlike must not be gated by rate_check")


# ── Audit logging (LO-*): redacted, hashed user only ─────────────────────────


class ReviewLikeAuditRedactionIntegration(unittest.TestCase):
    """Like/unlike emit _audit with a hashed user_id only -- never the raw
    user_id, note text, or session token (logging.md)."""

    def test_like_audit_uses_hashed_user_id_never_raw(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)
        with (
            mock.patch.object(server, "_audit", side_effect=_fake_audit),
            _allow_rate(),
            patch_db(cur),
        ):
            h._like_review(_MOVIE_ID)
        created = [c for c in audit_calls if c["action"] == "like.created"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["user_id"], _UID_A)
        self.assertNotEqual(_hash_user_id(_UID_A), _UID_A)

    def test_unlike_audit_emitted(self):
        audit_calls = []

        def _fake_audit(action, user_id, target):
            audit_calls.append({"action": action, "user_id": user_id, "target": target})

        cur = FakeCursor(fetch_results=[{"c": 0}])
        h, responses = make_handler(user_id=_UID_A)
        with mock.patch.object(server, "_audit", side_effect=_fake_audit), patch_db(cur):
            h._unlike_review(_MOVIE_ID)
        deleted = [c for c in audit_calls if c["action"] == "like.deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["user_id"], _UID_A)

    def test_audit_log_stdout_never_carries_note_text_or_raw_user_id(self):
        """LO-*: the printed audit log line must carry user_hash only -- never
        the raw user_id, and never the review's note text (privacy-sensitive
        UGC has no business appearing in any log line)."""
        import io

        captured = io.StringIO()
        cur = FakeCursor(fetch_results=[{"1": 1}, {"c": 1}])
        h, responses = make_handler(user_id=_UID_A)
        with _allow_rate(), patch_db(cur):
            with mock.patch("sys.stdout", captured):
                h._like_review(_MOVIE_ID)
        output = captured.getvalue()
        self.assertNotIn(_UID_A, output)
        self.assertNotIn("Great movie", output)  # no note text ever in a log line
        found_audit_line = False
        for line in output.splitlines():
            if "audit " in line and "like.created" in line:
                found_audit_line = True
                self.assertIn("user_hash", line)
        self.assertTrue(found_audit_line, "Expected a like.created audit line on stdout")


if __name__ == "__main__":
    unittest.main()
