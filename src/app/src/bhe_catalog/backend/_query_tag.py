"""
Query-tag helpers.

We tag every SQL statement we send to a Databricks SQL warehouse so the
"Query tags" column in Query History (and the `query_tags` column of
`system.query.history`) is populated.

Format we standardize on (all lowercase keys, underscores only):

    app=bhe_catalog
    module=<area>          e.g. ui_api, enrichment, company_research, bootstrap
    submodule=<specifics>  e.g. analytics_schema_inventory, ai_enrich_tables_step1
    run_id=<id>            e.g. FastAPI request id, Databricks job run id
    extras as needed       e.g. user, company

Constraints (from Databricks docs):
  - Tag keys MUST NOT contain , : - / = .
  - Max 20 tags per statement
  - Max 128 chars per key/value
  - Plain-text storage; never put secrets/PII

Backend usage:
  - A FastAPI middleware sets a ContextVar at the start of every request
    holding {module, submodule, request_id}. db._execute_sql_api reads it
    and passes the structured query_tags field on the SEA POST body.
  - Endpoints can override individual tags via with_tags(...).
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token

APP_NAME = "bhe_catalog"

_INVALID_KEY_CHARS = re.compile(r"[,:\-/=.]")

# Default tag context for SQL submitted from this process. Routes / FastAPI
# middleware push their own values via push_tags(...).
_tag_context: ContextVar[dict[str, str]] = ContextVar(
    "_query_tag_context", default={"app": APP_NAME, "module": "ui_api", "submodule": "unknown"}
)


def to_query_tags(**kv: str | None) -> list[dict[str, str]]:
    """Convert KV pairs to the SEA query_tags list-of-dicts shape.

    Skips empty values, validates key chars, truncates to 128 chars,
    and caps the total at 20 tags (Databricks limit).
    """
    out: list[dict[str, str]] = []
    for k, v in kv.items():
        if v is None or v == "":
            continue
        if _INVALID_KEY_CHARS.search(k):
            raise ValueError(
                f"query tag key '{k}' contains a forbidden char "
                "(no , : - / = . allowed; use underscores)"
            )
        out.append({"key": str(k)[:128], "value": str(v)[:128]})
    return out[:20]


def current_tags() -> dict[str, str]:
    """Return a shallow copy of the current ContextVar tag dict."""
    return dict(_tag_context.get())


def push_tags(**kv: str) -> Token:
    """Push a new tag context, merging onto the current one. Returns a token
    that the caller MUST pass to reset_tags(token) to restore the prior context.
    Typically used by FastAPI middleware around a request.
    """
    merged = {**_tag_context.get(), **{k: v for k, v in kv.items() if v}}
    return _tag_context.set(merged)


def reset_tags(token: Token) -> None:
    _tag_context.reset(token)


def with_overrides(**overrides: str) -> list[dict[str, str]]:
    """Return SEA-shaped query_tags = current context merged with overrides."""
    merged = {**_tag_context.get(), **{k: v for k, v in overrides.items() if v}}
    return to_query_tags(**merged)
