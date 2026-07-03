"""Static (no-live-DB) assertions for migrations/004_series_episode_progress.sql.

Covers the ### Tester scope "Static test" row of the series-episode-progress
task DoD (AC-10): the migration is additive/expand-only — CREATE TABLE + index
only, no ALTER/DROP of any EXISTING object — and carries a DOWN rollback block.

No Postgres connection is used or required; these are text/regex assertions
over the committed .sql file, matching the "Integration Tests" row of the task
("Migration 004 is additive ... static assertion — no live DB in the test env").
"""

import re
import unittest
from pathlib import Path

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "migrations" / "004_series_episode_progress.sql"
)


class Migration004IsAdditive(unittest.TestCase):
    """AC-10: the migration only adds a new table + index; it never touches the
    pre-existing `movies` (or any other already-shipped) table, and is
    reversible via a DOWN block."""

    @classmethod
    def setUpClass(cls):
        cls.sql = _MIGRATION_PATH.read_text(encoding="utf-8")

    def test_migration_file_exists(self):
        self.assertTrue(_MIGRATION_PATH.exists(), f"{_MIGRATION_PATH} must exist")

    def test_creates_episode_progress_table_if_not_exists(self):
        self.assertRegex(
            self.sql,
            r"CREATE TABLE IF NOT EXISTS\s+episode_progress",
            "must additively create the new table",
        )

    def test_creates_index_if_not_exists(self):
        self.assertRegex(
            self.sql,
            r"CREATE INDEX IF NOT EXISTS\s+episode_progress_user_show_idx\s+ON\s+episode_progress",
            "must additively create the lookup index",
        )

    def test_no_alter_statement_anywhere(self):
        self.assertNotRegex(
            self.sql, r"\bALTER\s+TABLE\b", "an additive migration must never ALTER an existing table"
        )

    def test_no_drop_in_the_up_section(self):
        """DROP is only allowed inside the commented-out DOWN block, never live in UP."""
        up_section = self.sql.split("===== DOWN")[0]
        # Strip SQL line comments before scanning for a live (non-comment) DROP.
        code_only = "\n".join(
            line for line in up_section.splitlines() if not line.strip().startswith("--")
        )
        self.assertNotRegex(
            code_only, r"\bDROP\b", "the UP section must not contain a live DROP statement"
        )

    def test_does_not_touch_movies_table(self):
        """The migration must contain no executable DDL statement targeting the
        pre-existing `movies` table (no ALTER/column add/FK on it) — orphan-
        freedom is handled at the app layer, not by a schema change here.
        (Prose comments explaining the no-FK decision are expected to mention
        `movies` by name and are excluded from this check.)"""
        code_only = "\n".join(line.split("--", 1)[0] for line in self.sql.splitlines())
        self.assertNotRegex(
            code_only,
            r"\bmovies\b",
            "migration 004 must contain no live DDL statement referencing movies",
        )

    def test_has_up_and_down_section_markers(self):
        self.assertIn("===== UP", self.sql)
        self.assertIn("===== DOWN", self.sql)
        down_index = self.sql.index("===== DOWN")
        up_index = self.sql.index("===== UP")
        self.assertLess(up_index, down_index, "UP section must precede DOWN section")

    def test_down_block_drops_the_new_table(self):
        down_section = self.sql.split("===== DOWN", 1)[1]
        self.assertRegex(
            down_section,
            r"DROP\s+TABLE\s+IF\s+EXISTS\s+episode_progress",
            "the DOWN block must reverse the UP block by dropping the new table",
        )

    def test_primary_key_is_composite_user_show_season_episode(self):
        self.assertRegex(
            self.sql,
            r"PRIMARY KEY\s*\(\s*user_id,\s*tmdb_id,\s*season,\s*episode\s*\)",
            "PK shape backs the idempotent ON CONFLICT DO NOTHING upsert",
        )

    def test_no_not_null_default_backfill_of_existing_rows(self):
        """An additive/no-backfill migration (BR-9) must not carry a DEFAULT
        clause that would implicitly backfill pre-existing data on any existing
        table — only the new table's own watched_at default is expected."""
        # The only DEFAULT in the file must be the new table's watched_at column.
        defaults = re.findall(r"(\w+)\s+\w[\w()]*\s+NOT NULL\s+DEFAULT", self.sql)
        self.assertEqual(defaults, ["watched_at"])


if __name__ == "__main__":
    unittest.main()
