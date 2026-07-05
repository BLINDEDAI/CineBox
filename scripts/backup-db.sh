#!/usr/bin/env bash
# Manual PostgreSQL backup for Cinephora — a free-tier safety net.
# (Supabase Pro has automatic daily backups + PITR; this is the substitute while
# you stay on Free. Run it periodically — e.g. weekly, or before risky changes.)
#
# Requires:
#   - Docker running (uses the postgres:17 image, so no local pg_dump install or
#     server-version mismatch to worry about).
#   - A SESSION-mode connection string. IMPORTANT: pg_dump does NOT work against
#     Supabase's TRANSACTION pooler (port 6543 — what the app's DATABASE_URL uses).
#     Use the SESSION connection (same host, port 5432): Supabase dashboard →
#     Settings → Database → "Connection string" (Session pooler / URI). Put it in
#     .env as:
#         BACKUP_DATABASE_URL=postgresql://postgres.<ref>:<pwd>@<host>:5432/postgres
#     If unset, this script derives it from DATABASE_URL by swapping 6543 -> 5432.
#
# Usage:
#   ./scripts/backup-db.sh                          # make a backup
#   ./scripts/backup-db.sh --restore <file.dump>    # DESTRUCTIVE restore
#
# Output: backups/cinephora-YYYYMMDD-HHMMSS.dump  (pg custom format, restorable)
# The connection string never leaves your machine; it is passed to the container
# via an env var (not on the command line) and only reaches your own Supabase DB.
set -euo pipefail
cd "$(dirname "$0")/.."

IMG="postgres:17"

get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r"'; }

URL="$(get_env BACKUP_DATABASE_URL)"
if [ -z "$URL" ]; then
  URL="$(get_env DATABASE_URL | sed -E 's/:6543\//:5432\//')"
  [ -n "$URL" ] && echo "note: using session URL derived from DATABASE_URL (6543->5432)." >&2
fi
[ -z "$URL" ] && { echo "error: no connection string. Set BACKUP_DATABASE_URL (session mode, port 5432) in .env." >&2; exit 1; }

docker info >/dev/null 2>&1 || { echo "error: Docker daemon not running — start Docker Desktop first." >&2; exit 1; }

# ── Restore path (destructive) ────────────────────────────────────────────────
if [ "${1:-}" = "--restore" ]; then
  FILE="${2:-}"
  [ -f "$FILE" ] || { echo "error: dump file not found: ${FILE:-<none>}" >&2; exit 1; }
  echo "⚠️  RESTORE is DESTRUCTIVE: it runs pg_restore --clean and overwrites the target DB."
  printf "Type YES to proceed: "
  read -r confirm
  [ "$confirm" = "YES" ] || { echo "aborted."; exit 1; }
  docker run --rm -i -e U="$URL" "$IMG" sh -c 'pg_restore --clean --if-exists --no-owner -d "$U"' < "$FILE"
  echo "✅ Restore complete."
  exit 0
fi

# ── Backup path ───────────────────────────────────────────────────────────────
mkdir -p backups
TS="$(date +%Y%m%d-%H%M%S)"
OUT="backups/cinephora-$TS.dump"
echo "Backing up Cinephora DB → $OUT ..."
# pg_dump writes the custom-format dump to stdout inside the container; we pipe it
# straight to the host file — no volume mount (dodges Windows/Docker mount quirks).
docker run --rm -e PGCONN="$URL" "$IMG" sh -c 'pg_dump "$PGCONN" -Fc --no-owner' > "$OUT"

[ -s "$OUT" ] || { echo "error: dump is empty — check BACKUP_DATABASE_URL / connectivity." >&2; rm -f "$OUT"; exit 1; }
echo "✅ Done: $OUT ($(du -h "$OUT" | cut -f1))"

# Keep the last 14 days of dumps.
find backups -name 'cinephora-*.dump' -type f -mtime +14 -delete 2>/dev/null || true
echo "Backups on disk: $(ls backups/cinephora-*.dump 2>/dev/null | wc -l)"
