"""Runtime key/value config stored in `<catalog>.<gold>.app_config`.

Used by the Setup Wizard to persist runtime decisions (Genie space ID,
post-bootstrap state, etc.) that need to survive app restarts but shouldn't
require a bundle re-deploy to update. Reads use the same SQL Statement
Execution path as every other DB op (so the app's service principal needs
the usual SELECT/MODIFY on the gold schema).

Read at request time (no caching). Lookups are tiny single-row queries; the
extra ~50ms is invisible against the operations that consume these values
(Genie chat, schema enrichment, etc.).
"""

from __future__ import annotations

import logging

from .db import execute_query, execute_update, get_catalog, get_gold_schema

logger = logging.getLogger(__name__)

TABLE_DDL = """CREATE TABLE IF NOT EXISTS {fqn} (
    key STRING NOT NULL,
    value STRING,
    updated_at TIMESTAMP,
    updated_by STRING
) USING DELTA COMMENT 'Runtime key/value config written by the in-app Setup Wizard'"""


def _fqn() -> str:
    return f"`{get_catalog()}`.`{get_gold_schema()}`.app_config"


def get_config_value(key: str) -> str | None:
    """Return the value for `key`, or None if missing or table not yet created.

    Failures (table missing, SQL error) are swallowed and logged at DEBUG so
    callers in the chatbot path don't have to wrap every read in try/except.
    """
    try:
        rows = execute_query(
            f"SELECT value FROM {_fqn()} WHERE key = '{_escape(key)}' LIMIT 1",
            tag_overrides={"submodule": "app_config_get"},
        )
    except Exception as e:
        logger.debug("app_config get(%s) failed (table may not exist yet): %s", key, e)
        return None
    if rows and rows[0].get("value") is not None:
        return str(rows[0]["value"])
    return None


def set_config_value(key: str, value: str, principal: str = "") -> None:
    """Idempotent upsert of a key/value pair.

    Caller is responsible for surfacing exceptions; this is the write path
    used by privileged endpoints (Setup Wizard) where a clean error matters.
    """
    safe_key = _escape(key)
    safe_val = _escape(value)
    safe_who = _escape(principal or "app")
    execute_update(
        f"""
        MERGE INTO {_fqn()} t
        USING (SELECT '{safe_key}' AS key, '{safe_val}' AS value,
                      current_timestamp() AS updated_at,
                      '{safe_who}' AS updated_by) s
        ON t.key = s.key
        WHEN MATCHED THEN UPDATE SET t.value = s.value,
                                     t.updated_at = s.updated_at,
                                     t.updated_by = s.updated_by
        WHEN NOT MATCHED THEN INSERT (key, value, updated_at, updated_by)
                              VALUES (s.key, s.value, s.updated_at, s.updated_by)
        """,
        tag_overrides={"submodule": "app_config_set"},
    )


def _escape(s: str) -> str:
    """Single-quote escape for inline SQL literals on this trusted, server-only path."""
    return (s or "").replace("'", "''")
