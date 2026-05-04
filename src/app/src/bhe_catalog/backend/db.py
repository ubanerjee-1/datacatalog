"""
Database service for querying Databricks SQL warehouse.
Uses the SQL Statement Execution REST API via requests.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from ._query_tag import with_overrides

logger = logging.getLogger(__name__)

# Required runtime env (set in src/app/app.yml at deploy time, or in
# src/app/.env for local dev). The defaults are deliberately invalid
# placeholders — the Setup Wizard surfaces a clear error instead of silently
# pointing at the wrong workspace if these are unset.
_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "your-warehouse-id")
_CATALOG = os.environ.get("BHE_CATALOG", "your_catalog")
_RAW_SCHEMA = os.environ.get("BHE_RAW_SCHEMA", "bhe_raw")
_SILVER_SCHEMA = os.environ.get("BHE_SILVER_SCHEMA", "bhe_silver")
_GOLD_SCHEMA = os.environ.get("BHE_GOLD_SCHEMA", "bhe_gold")

# Lazy-initialized auth config. The SDK auto-discovers credentials from env in
# this order:
#   1. DATABRICKS_TOKEN       (PAT — used by local dev via start_local.sh)
#   2. DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET  (OAuth M2M — auto-injected
#      by Databricks Apps runtime for the app's service principal)
#   3. DATABRICKS_CFG_PROFILE (local .databrickscfg)
# We keep one Config per process so auth tokens are cached/refreshed by the SDK.
_CONFIG: Config | None = None


def _get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = WorkspaceClient().config
    return _CONFIG


def get_catalog() -> str:
    return _CATALOG

def get_silver_schema() -> str:
    return _SILVER_SCHEMA

def get_gold_schema() -> str:
    return _GOLD_SCHEMA

def get_raw_schema() -> str:
    return _RAW_SCHEMA


def _get_host() -> str:
    host = _get_config().host or os.environ.get("DATABRICKS_HOST", "")
    if not host:
        raise ValueError(
            "Databricks host not configured. Set DATABRICKS_HOST or run inside a "
            "Databricks App (host is auto-injected)."
        )
    if not host.startswith("http"):
        host = f"https://{host}"
    return host.rstrip("/")


def _get_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    auth_headers = _get_config().authenticate() or {}
    headers.update(auth_headers)
    if "Authorization" not in headers:
        raise ValueError(
            "No Databricks credentials found. Locally set DATABRICKS_TOKEN; on "
            "Databricks Apps the runtime auto-injects OAuth credentials."
        )
    return headers


def _execute_sql_api(
    query: str,
    wait_timeout: str = "30s",
    poll_timeout: int = 600,
    tag_overrides: dict[str, str] | None = None,
) -> dict:
    """Execute SQL via the Statement Execution API and poll until complete.

    Args:
        poll_timeout: Max seconds to poll for long-running queries (e.g. ai_query).
        tag_overrides: Optional per-call overrides on top of the request-scoped
            tag context (set by middleware). Use this when an endpoint dispatches
            multiple SQL calls and wants to distinguish them in Query History,
            e.g. tag_overrides={"submodule": "sankey_data.schema_lookup"}.
    """
    host = _get_host()
    headers = _get_headers()
    url = f"{host}/api/2.0/sql/statements/"

    body = {
        "warehouse_id": _WAREHOUSE_ID,
        "statement": query,
        "wait_timeout": wait_timeout,
        "catalog": _CATALOG,
        "query_tags": with_overrides(**(tag_overrides or {})),
    }

    resp = requests.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        try:
            err_detail = resp.json()
        except Exception:
            err_detail = resp.text
        logger.error(f"SQL submit {resp.status_code}: {err_detail}")
    resp.raise_for_status()
    result = resp.json()

    deadline = time.time() + poll_timeout
    poll_interval = 2
    while result.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        if time.time() > deadline:
            raise RuntimeError(f"SQL statement timed out after {poll_timeout}s")
        time.sleep(min(poll_interval, deadline - time.time()))
        poll_interval = min(poll_interval * 1.5, 15)
        stmt_id = result["statement_id"]
        poll_resp = requests.get(f"{url}{stmt_id}", headers=headers)
        poll_resp.raise_for_status()
        result = poll_resp.json()

    if result.get("status", {}).get("state") == "FAILED":
        error = result.get("status", {}).get("error", {})
        raise RuntimeError(f"SQL execution failed: {error.get('message', 'Unknown error')}")

    return result


def execute_query(
    query: str,
    params: dict[str, Any] | None = None,
    poll_timeout: int = 600,
    tag_overrides: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Execute a SQL query and return results as list of dicts.

    Args:
        tag_overrides: Optional Query History tags to add on top of the
            request-scoped context (set by middleware).
    """
    try:
        result = _execute_sql_api(query, poll_timeout=poll_timeout, tag_overrides=tag_overrides)
        manifest = result.get("manifest", {})
        columns = [c["name"] for c in manifest.get("schema", {}).get("columns", [])]
        data_array = result.get("result", {}).get("data_array", [])
        return [dict(zip(columns, row)) for row in data_array]
    except Exception as e:
        logger.error(f"Query execution failed: {e}")
        raise


def execute_update(
    query: str,
    params: dict[str, Any] | None = None,
    tag_overrides: dict[str, str] | None = None,
) -> int:
    """Execute a SQL update/insert and return affected row count."""
    try:
        result = _execute_sql_api(query, tag_overrides=tag_overrides)
        return result.get("manifest", {}).get("total_row_count", 0)
    except Exception as e:
        logger.error(f"Update execution failed: {e}")
        raise


def fqn(schema: str, table: str) -> str:
    """Return fully qualified table name."""
    return f"{_CATALOG}.{schema}.{table}"
