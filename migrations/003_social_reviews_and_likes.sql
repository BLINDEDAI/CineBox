-- Migration 003 — social-reviews-and-likes (ADR-015)
-- Additive/expand-only (DM-* / US-083): ADD COLUMN, widen a CHECK, CREATE TABLE.
-- No destructive change; no backfill (note_public defaults false → every existing note stays private).
-- Apply in the Supabase SQL editor (project: Cinephora) BEFORE the readers deploy. Reversible with the DOWN block.

-- ===== UP =====

-- 1) Per-title publish opt-in on the existing movies table (additive; default private).
ALTER TABLE movies ADD COLUMN IF NOT EXISTS note_public BOOLEAN NOT NULL DEFAULT false;
-- Partial index: a user's currently-published reviews (profile reviews area / feed gate).
CREATE INDEX IF NOT EXISTS movies_note_public_idx ON movies (user_id) WHERE note_public = true;

-- 2) The 'reviewed' feed event links to the reviewed movie so the feed reads the CURRENT note + gate
--    (read-time visibility, never a write-time snapshot). Widen the action CHECK to admit 'reviewed'.
ALTER TABLE activity ADD COLUMN IF NOT EXISTS movie_id INTEGER REFERENCES movies (id) ON DELETE CASCADE;
ALTER TABLE activity DROP CONSTRAINT IF EXISTS activity_action_check;   -- auto-name from migration 002's inline CHECK
ALTER TABLE activity ADD  CONSTRAINT activity_action_check
  CHECK (action IN ('watched', 'rated', 'list_add', 'reviewed'));

-- 3) Likes: idempotent (liker_id, movie_id) edge; a like dies with the reviewed title (AC-15 / AC-17).
CREATE TABLE IF NOT EXISTS likes (
  liker_id    UUID    NOT NULL,                                     -- the user who liked (JWT sub)
  movie_id    INTEGER NOT NULL REFERENCES movies (id) ON DELETE CASCADE,  -- the reviewed title
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (liker_id, movie_id)                                  -- idempotent like (ON CONFLICT DO NOTHING)
);
CREATE INDEX IF NOT EXISTS likes_movie_idx ON likes (movie_id);     -- count/list likes for a review; liker_id uses the PK prefix

-- ===== DOWN (rollback) =====
-- DROP TABLE IF EXISTS likes;
-- ALTER TABLE activity DROP CONSTRAINT IF EXISTS activity_action_check;
-- ALTER TABLE activity ADD  CONSTRAINT activity_action_check
--   CHECK (action IN ('watched', 'rated', 'list_add'));
-- ALTER TABLE activity DROP COLUMN IF EXISTS movie_id;
-- DROP INDEX IF EXISTS movies_note_public_idx;
-- ALTER TABLE movies DROP COLUMN IF EXISTS note_public;
