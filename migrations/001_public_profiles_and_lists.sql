-- Migration 001 — public-profiles-and-shared-lists (ADR-005)
-- Additive: CREATE TABLE only. Does not alter `movies`. Reversible with the DOWN block below.
-- Apply in the Supabase SQL editor (project: Cinephora). pgcrypto's gen_random_uuid() is available on Supabase.

-- ===== UP =====
CREATE TABLE IF NOT EXISTS profiles (
  user_id          UUID PRIMARY KEY,                  -- Supabase auth user (JWT `sub`); no FK to auth.users (out of migration scope, mirrors movies.user_id)
  username         TEXT UNIQUE,                        -- stored lowercased at the app layer; NULL until the user picks one
  is_public        BOOLEAN NOT NULL DEFAULT false,
  show_collection  BOOLEAN NOT NULL DEFAULT false,
  show_stats       BOOLEAN NOT NULL DEFAULT false,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lists (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL,
  name         TEXT NOT NULL,
  visibility   TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'unlisted', 'public')),
  share_token  UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lists_user_id_idx ON lists (user_id);

CREATE TABLE IF NOT EXISTS list_items (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  list_id     UUID NOT NULL REFERENCES lists (id) ON DELETE CASCADE,
  tmdb_id     INTEGER NOT NULL,
  media_type  TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
  title       TEXT NOT NULL,
  year        TEXT,
  poster_url  TEXT,
  position    INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (list_id, tmdb_id, media_type)
);
CREATE INDEX IF NOT EXISTS list_items_list_id_idx ON list_items (list_id, position);

-- ===== DOWN (rollback) =====
-- DROP TABLE IF EXISTS list_items;
-- DROP TABLE IF EXISTS lists;
-- DROP TABLE IF EXISTS profiles;
