-- Migration 004 — series-episode-progress (ADR-017)
-- Additive / expand-only (DM-* / US-083): CREATE TABLE + index only. Does NOT alter movies.
-- Expand phase of the derive-and-sync lifecycle; no backfill (BR-9 — lazy adoption).
-- Apply in the Supabase SQL editor (project: CineBox) BEFORE the readers deploy. Reversible with the DOWN block.
-- Rollback: DROP TABLE episode_progress; movies untouched.

-- ===== UP =====

-- Per-user per-episode watched state. Keyed on (user_id, tmdb_id, season, episode).
-- No FK to movies: movies keys on an INTEGER id, not tmdb_id — orphan-freedom is guaranteed
-- instead by two explicit purges in server.py (the title-delete handler + _delete_account RTBF),
-- not by ON DELETE CASCADE (ADR-017 § Alternatives).
CREATE TABLE IF NOT EXISTS episode_progress (
  user_id     UUID        NOT NULL,                 -- Supabase auth user (JWT `sub`); per-user isolation (PS-001)
  tmdb_id     INTEGER     NOT NULL,                 -- the series' TMDB id (matches movies.tmdb_id)
  season      INTEGER     NOT NULL,
  episode     INTEGER     NOT NULL,
  watched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, tmdb_id, season, episode)   -- idempotent marks (ON CONFLICT DO NOTHING)
);
-- A user's marks for one show: the marks-merge read (_season) + derive-and-sync scan (_recompute_progress).
CREATE INDEX IF NOT EXISTS episode_progress_user_show_idx ON episode_progress (user_id, tmdb_id);

-- ===== DOWN (rollback) =====
-- DROP TABLE IF EXISTS episode_progress;
