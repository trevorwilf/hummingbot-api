"""Schema-version state: is this database at the alembic head? (CDX-013)

Before this module the API built its schema at every startup with
`Base.metadata.create_all` plus a list of hand-written `ALTER TABLE`
statements whose failures were logged and swallowed. That is fail-open: a
migration could silently not happen and the API would serve anyway, against a
schema it only assumed. Schema state is now versioned by alembic and verified
at startup; anything other than "at head" is fatal.

The check is deliberately dumb — it compares the database's alembic_version
against the head revision on disk. It does not inspect columns and it cannot
repair anything: repair is a human runbook step, because on a live trading
database the right repair (stamp vs upgrade) depends on facts only a human
knows.
"""
import logging
from pathlib import Path
from typing import Optional

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

logger = logging.getLogger(__name__)

# Repo root: this file is <root>/database/migration_state.py.
_REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI_PATH = _REPO_ROOT / "alembic.ini"

# The baseline revision — the schema the pre-CDX-013 image built. A database
# that predates alembic has exactly this schema, so it is the revision a human
# stamps before upgrading. Named here so the runbook error message can quote
# the exact command instead of making an operator go and look it up.
BASELINE_REVISION = "0001_baseline"


class MigrationStateError(RuntimeError):
    """The database's schema version is absent, behind, or unverifiable.

    Fatal at startup by design: serving against an unknown schema is how
    silently-missing columns turn into corrupted money data.
    """


def alembic_config(database_url: Optional[str] = None) -> Config:
    """Load alembic.ini.

    database_url overrides the URL env.py would otherwise resolve from the
    application settings — used by tests, and by a human targeting a specific
    database. (Passing a URL containing a literal '%' would hit ConfigParser
    interpolation; the app path leaves it unset and resolves via settings.)
    """
    config = Config(str(ALEMBIC_INI_PATH))
    if database_url is not None:
        config.set_main_option("sqlalchemy.url", database_url)
    return config


def get_head_revision() -> str:
    """The single head revision defined by the scaffold on disk."""
    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    if len(heads) != 1:
        raise MigrationStateError(
            f"Expected exactly one alembic head revision, found {len(heads)}: "
            f"{heads or '(none)'}. The migration scaffold in "
            f"{_REPO_ROOT / 'alembic' / 'versions'} is broken or has branched; "
            "resolve it before starting the API."
        )
    return heads[0]


def get_current_revision(connection) -> Optional[str]:
    """The revision this database reports, or None if it has no version table.

    `connection` is a synchronous SQLAlchemy Connection (async callers reach
    this via `AsyncConnection.run_sync`).
    """
    heads = MigrationContext.configure(connection).get_current_heads()
    if not heads:
        return None
    if len(heads) > 1:
        raise MigrationStateError(
            f"Database reports multiple alembic head revisions {heads}; expected one. "
            f"{_runbook(get_head_revision())}"
        )
    return heads[0]


def _runbook(head: str) -> str:
    return (
        "RUNBOOK — run ONE of these against this database, then restart:\n"
        "  * Database created BEFORE this release (it has tables but no "
        "alembic_version table) — record the schema it already has, then apply "
        "everything newer:\n"
        f"      alembic stamp {BASELINE_REVISION}\n"
        "      alembic upgrade head\n"
        "  * Empty/new database — build the whole schema:\n"
        "      alembic upgrade head\n"
        f"    (run from the application root, {_REPO_ROOT}, with DATABASE_URL set "
        "to this database)\n"
        f"  Expected head revision: {head}\n"
        f"  Do NOT stamp {head} on a database created before this release: the "
        f"phase 7/8 columns (bot_runs.retirement_status/retirement_evidence, "
        "trades.account_name/connector_name/exchange_trade_id and the "
        "uq_trade_scoped_exchange_trade_id dedup index) would be missing while "
        "the database claims to be up to date."
    )


def check_schema_at_head(connection) -> str:
    """Verify this database is at the head revision; raise if it is not.

    Returns the revision on success. Raises MigrationStateError when the
    version table is absent or the database is not at head — never returns
    normally in those cases, and never repairs anything itself.
    """
    head = get_head_revision()
    current = get_current_revision(connection)

    if current is None:
        raise MigrationStateError(
            "Database has no alembic_version table, so its schema version is "
            "unknown and this API refuses to serve against it. "
            + _runbook(head)
        )
    if current != head:
        raise MigrationStateError(
            f"Database schema is at alembic revision '{current}' but this code "
            f"requires head '{head}'. Pending migrations must be applied before "
            "the API can serve. " + _runbook(head)
        )
    return current
