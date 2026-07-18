#!/usr/bin/env bash
#
# run-migrations.sh -- database migration entrypoint for the hummingbot-api-migrate
# init container. It ships INSIDE the image (Dockerfile: COPY scripts ./scripts) so it is
# versioned with the alembic revisions it drives and is identical across every stack (VPN
# and no-VPN both just call `bash scripts/run-migrations.sh`).
#
# It handles all three database states safely and idempotently:
#
#   * fresh    (empty DB, no tables)          -> `alembic upgrade head` builds the whole schema.
#   * legacy   (deployed before this release: -> `alembic stamp 0001_baseline` adopts the
#               tables exist, no alembic_version)   existing schema, THEN `alembic upgrade head`
#                                                   applies the phase 7/8 delta (revision 0002).
#   * migrated (alembic_version already set)   -> `alembic upgrade head` is a no-op.
#
# The stamp branch is self-disabling: once a legacy DB is adopted, alembic_version exists, so
# every later run takes the plain upgrade path. A stamp NEVER runs against a DB alembic already
# tracks, and never against a fresh empty DB -- only against a pre-existing, untracked schema.
#
# Assumption for the adopt path: a legacy DB's schema equals revision 0001_baseline (the schema
# the pre-release image deployed). That is the exact contract 0001_baseline was written to
# reproduce; a database older than that baseline would need a different adoption point.
set -euo pipefail

# The baseline revision a legacy (pre-release) database is stamped at before the delta applies.
# Must match alembic/versions/0001_baseline_*.py. Kept next to that file, in the same image.
BASELINE_REVISION="0001_baseline"

# A table the baseline schema always contains. Its presence WITHOUT an alembic_version table is
# exactly what distinguishes a legacy deployed DB (adopt) from a fresh empty one (build).
SENTINEL_TABLE="account_states"

log() { echo "[migrate] $*"; }

# The alembic + asyncpg dependencies live in the hummingbot-api conda env. The image already
# puts that env on PATH, so this activation is belt-and-suspenders -- and lets the script run
# unchanged in a plain shell where the env is not yet active.
if [ -f /opt/conda/etc/profile.d/conda.sh ]; then
  # shellcheck disable=SC1091
  . /opt/conda/etc/profile.d/conda.sh
  conda activate hummingbot-api 2>/dev/null || true
fi

# App dir. HBOT_API_DIR is an optional override used only by the test harness; production
# leaves it unset and falls back to the image's install path.
if [ -n "${HBOT_API_DIR:-}" ]; then
  cd "$HBOT_API_DIR" || { log "app dir '$HBOT_API_DIR' not found -- skipping."; exit 0; }
else
  cd /hummingbot-api 2>/dev/null || cd /app 2>/dev/null || { log "app dir not found -- skipping."; exit 0; }
fi

if [ ! -f alembic.ini ]; then
  log "No alembic.ini in image -- skipping (scaffold Alembic to enable)."
  exit 0
fi

log "alembic.ini found -- detecting database state..."

# Probe with the same driver the app uses (asyncpg). to_regclass() returns NULL for an absent
# relation, so neither check raises on a missing table. Prints exactly "yes" or "no".
if ! NEED_STAMP="$(
python - "$SENTINEL_TABLE" <<'PY'
import asyncio
import os
import sys

import asyncpg

sentinel = "public." + sys.argv[1]
# SQLAlchemy's async URL (postgresql+asyncpg://...) -> the plain libpq URL asyncpg.connect wants.
url = os.environ["DATABASE_URL"].replace("+asyncpg", "")


async def probe():
    conn = await asyncpg.connect(url)
    try:
        has_version = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        has_schema = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", sentinel)
    finally:
        await conn.close()
    # Stamp only when the schema exists but alembic is not yet tracking it (the legacy case).
    print("yes" if (not has_version and has_schema) else "no")


asyncio.run(probe())
PY
)"; then
  log "ERROR: database state probe failed. Is the database reachable and DATABASE_URL set correctly?" >&2
  exit 1
fi

if [ "$NEED_STAMP" = "yes" ]; then
  log "pre-existing schema without alembic_version -- stamping $BASELINE_REVISION for one-time adoption."
  alembic stamp "$BASELINE_REVISION"
fi

log "running: alembic upgrade head"
alembic upgrade head
log "Alembic migrations applied."
