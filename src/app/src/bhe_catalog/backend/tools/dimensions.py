"""Dimension-list tools: affiliates and source systems.

These are the two slicer dimensions the Value & Readiness page exposes.
The chat needs them so the model can answer "which affiliates do we
serve?" and "what source systems are in the lake?" without falling
through to genie_ask. Both tools return small fixed result sets — no
pagination, no filters beyond a single text query.

We deliberately mirror the shape of `/value/affiliates` and
`/source-systems` so a future Phase A2 propose_use_case tool can
consume the same dicts to suggest applicability + source mappings.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..db import execute_query, fqn, get_gold_schema, get_silver_schema
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# app_list_affiliates
# ---------------------------------------------------------------------------


class ListAffiliatesArgs(BaseModel):
    """No required args — affiliates are a fixed dimension table."""

    query: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter against the "
            "affiliate name, code, business type, or region. Omit to list "
            "all active affiliates."
        ),
    )
    include_inactive: bool = Field(
        default=False,
        description=(
            "If true, include affiliates with is_active=false. Default is "
            "active-only (matches what the UI slicer shows)."
        ),
    )


def _q(s: str) -> str:
    return s.replace("'", "''")


def _list_affiliates(args: ListAffiliatesArgs, ctx: ToolContext) -> ToolResult:
    gold = get_gold_schema()
    where: list[str] = []
    if not args.include_inactive:
        where.append("COALESCE(a.is_active, true) = true")
    if args.query:
        s = _q(args.query.lower())
        where.append(
            "("
            f"LOWER(a.affiliate_name) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(a.affiliate_code,'')) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(a.business_type,'')) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(a.region,'')) LIKE '%{s}%'"
            ")"
        )
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT
            a.affiliate_name,
            COALESCE(a.affiliate_code, '')   AS affiliate_code,
            COALESCE(a.business_type, '')    AS business_type,
            COALESCE(a.region, '')           AS region,
            COALESCE(a.description, '')      AS description,
            COALESCE(a.is_active, true)      AS is_active,
            COUNT(DISTINCT ua.use_case_id)   AS use_case_count,
            COUNT(DISTINCT CASE WHEN ua.applicability = 'primary'
                                THEN ua.use_case_id END) AS primary_use_case_count
        FROM {fqn(gold, 'affiliates')} a
        LEFT JOIN {fqn(gold, 'use_case_affiliates')} ua
            ON ua.affiliate_name = a.affiliate_name
        {where_sql}
        GROUP BY a.affiliate_name, a.affiliate_code, a.business_type,
                 a.region, a.description, a.is_active
        ORDER BY use_case_count DESC, a.affiliate_name
    """
    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.list_affiliates"}
        )
    except Exception as e:
        logger.exception("app_list_affiliates SQL failed")
        return ToolResult(ok=False, summary=f"List failed: {e}", data={"error": str(e)})

    items = [
        {
            "affiliate_name": r.get("affiliate_name"),
            "affiliate_code": r.get("affiliate_code") or None,
            "business_type": r.get("business_type") or None,
            "region": r.get("region") or None,
            "description": (r.get("description") or "").strip()[:240] or None,
            "is_active": bool(r.get("is_active")),
            "use_case_count": int(r.get("use_case_count") or 0),
            "primary_use_case_count": int(r.get("primary_use_case_count") or 0),
        }
        for r in rows
    ]
    return ToolResult(
        ok=True,
        summary=f"{len(items)} affiliate{'s' if len(items) != 1 else ''}",
        data={
            "affiliates": items,
            "filters": {
                "query": args.query,
                "include_inactive": args.include_inactive,
            },
        },
    )


LIST_AFFILIATES = Tool(
    name="app_list_affiliates",
    description=(
        "List BHE affiliates (operating companies) with their business type, "
        "region, and how many use cases reference each one. Use this when "
        "the user asks 'which affiliates do we have' or needs to pick an "
        "affiliate name to pass to other tools."
    ),
    args_model=ListAffiliatesArgs,
    handler=_list_affiliates,
)


# ---------------------------------------------------------------------------
# app_list_source_systems
# ---------------------------------------------------------------------------


class ListSourceSystemsArgs(BaseModel):
    """List canonical source systems with ingest stats."""

    query: str | None = Field(
        default=None,
        description=(
            "Optional case-insensitive substring filter against canonical "
            "name, category, or description."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "Optional category filter (e.g. 'Operations', 'Customer', "
            "'Asset', 'GIS'). Use app_list_source_systems with no filters "
            "first to discover available categories."
        ),
    )
    only_with_data: bool = Field(
        default=False,
        description=(
            "If true, drop canonicals with zero tables in the lake. Use "
            "this for 'what data sources do we actually have' style "
            "questions; leave false to also surface seeded-but-empty "
            "canonicals."
        ),
    )
    limit: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Max source systems to return.",
    )


def _list_source_systems(args: ListSourceSystemsArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()

    where: list[str] = ["c.is_active = true"]
    if args.category:
        where.append(f"c.category = '{_q(args.category)}'")
    if args.query:
        s = _q(args.query.lower())
        where.append(
            "("
            f"LOWER(c.canonical) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(c.description,'')) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(c.category,'')) LIKE '%{s}%'"
            ")"
        )
    where_sql = "WHERE " + " AND ".join(where)
    if args.only_with_data:
        where_sql += " AND COALESCE(ts.table_count, 0) > 0"

    sql = f"""
        WITH table_stats AS (
            SELECT
                t.source_system_canonical AS canonical,
                COUNT(*) AS table_count,
                COUNT(DISTINCT CONCAT(t.table_catalog, '.', t.table_schema)) AS schema_count,
                COUNT(DISTINCT s.program) AS affiliate_count
            FROM {fqn(silver, 'silver_tables')} t
            LEFT JOIN {fqn(silver, 'silver_schemas')} s
                ON s.catalog_name = t.table_catalog
               AND s.schema_name = t.table_schema
            WHERE t.source_system_canonical IS NOT NULL
              AND t.source_system_canonical != ''
            GROUP BY t.source_system_canonical
        )
        SELECT
            c.canonical                       AS canonical,
            COALESCE(c.category, 'Other')     AS category,
            COALESCE(c.description, '')       AS description,
            COALESCE(ts.table_count, 0)       AS table_count,
            COALESCE(ts.schema_count, 0)      AS schema_count,
            COALESCE(ts.affiliate_count, 0)   AS affiliate_count
        FROM {fqn(gold, 'source_system_canonical')} c
        LEFT JOIN table_stats ts ON ts.canonical = c.canonical
        {where_sql}
        ORDER BY COALESCE(ts.table_count, 0) DESC, c.canonical ASC
        LIMIT {args.limit}
    """
    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.list_source_systems"}
        )
    except Exception as e:
        logger.exception("app_list_source_systems SQL failed")
        return ToolResult(ok=False, summary=f"List failed: {e}", data={"error": str(e)})

    items = [
        {
            "canonical": r.get("canonical"),
            "category": r.get("category") or "Other",
            "description": (r.get("description") or "").strip()[:240] or None,
            "table_count": int(r.get("table_count") or 0),
            "schema_count": int(r.get("schema_count") or 0),
            "affiliate_count": int(r.get("affiliate_count") or 0),
            "is_present": int(r.get("table_count") or 0) > 0,
        }
        for r in rows
    ]
    present = sum(1 for it in items if it["is_present"])
    return ToolResult(
        ok=True,
        summary=(
            f"{len(items)} source system{'s' if len(items) != 1 else ''} "
            f"({present} in lake)"
        ),
        data={
            "source_systems": items,
            "filters": {
                "query": args.query,
                "category": args.category,
                "only_with_data": args.only_with_data,
            },
        },
    )


LIST_SOURCE_SYSTEMS = Tool(
    name="app_list_source_systems",
    description=(
        "List canonical source systems with how many tables, schemas, and "
        "affiliates each one feeds in the lake today. Use this when the "
        "user asks 'what data sources do we have' or 'which systems feed "
        "PacifiCorp ops'. Pair with app_value_source_rollup for the "
        "$ value each source unlocks."
    ),
    args_model=ListSourceSystemsArgs,
    handler=_list_source_systems,
)
