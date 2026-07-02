-- Migration 002 — social-follows-and-activity-feed (ADR-014)
-- Additive: CREATE TABLE only. Does NOT alter movies / profiles / lists / list_items.
-- Expand-only (DM-* / US-083); no backfill. Reversible with the DOWN block below.
-- Apply in the Supabase SQL editor (project: CineBox). pgcrypto's gen_random_uuid() is available on Supabase.

-- ===== UP =====

-- Social graph: a directed edge follower_id -> followed_id.
CREATE TABLE IF NOT EXISTS follows (
  follower_id  UUID NOT NULL,                       -- the user who follows (JWT sub)
  followed_id  UUID NOT NULL,                       -- the user being followed (JWT sub)
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (follower_id, followed_id),           -- idempotent follow (ON CONFLICT DO NOTHING)
  CHECK (follower_id <> followed_id)                -- no self-follow at the storage layer (AC-4)
);
CREATE INDEX IF NOT EXISTS follows_followed_idx ON follows (followed_id);  -- follower_id uses the PK prefix

-- Append-only activity log: one row per socially-relevant action.
CREATE TABLE IF NOT EXISTS activity (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL,                        -- actor (JWT sub)
  action      TEXT NOT NULL CHECK (action IN ('watched', 'rated', 'list_add')),
  tmdb_id     INTEGER,
  media_type  TEXT CHECK (media_type IN ('movie', 'tv')),
  title       TEXT NOT NULL,                        -- cached snapshot (feed renders without a join to movies)
  year        TEXT,
  poster_url  TEXT,                                 -- TMDB allow-list enforced at write (never rendered as arbitrary src)
  rating      INTEGER,                              -- only for action = 'rated'
  list_id     UUID REFERENCES lists (id) ON DELETE CASCADE,  -- only for 'list_add'; a deleted list drops its events (AC-14)
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activity_user_created_idx ON activity (user_id, created_at DESC);  -- feed reads by actor, newest first

-- ===== DOWN (rollback) =====
-- DROP TABLE IF EXISTS activity;
-- DROP TABLE IF EXISTS follows;
