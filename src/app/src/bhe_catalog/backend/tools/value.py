"""Value & Readiness aggregate tools.

Three tools that mirror the Value & Readiness page's three KPI sources:
  - app_value_summary       → KPI strip (total / ready / gap / by-status)
  - app_value_source_rollup → Source ROI Pareto (which sources unlock $)
  - app_gaps_matrix         → canonical × affiliate coverage matrix

These are the tools the LLM should reach for when the user asks
portfolio-level questions like "what's our total addressable value at
PacifiCorp?" or "which sources have the most gaps". Each tool is a
direct read against the same gold tables the page uses, so numbers
reconcile bit-for-bit between chat answers and the UI.

We DO NOT delegate to genie_ask for these — the typed shape gives the
model better citations and lets the FE deep-link straight to the right
view (?tab=pareto, ?tab=flow, etc.).
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..db import execute_query, fqn, get_gold_schema, get_silver_schema
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


def _q(s: str) -> str:
    return s.replace("'", "''")


# ---------------------------------------------------------------------------
# Shared SQL helpers
# ---------------------------------------------------------------------------
#
# These mirror router._build_value_filters() and router._present_canonicals_cte().
# Inlined here so this file stays a leaf module that doesn't import router.
# Drift risk: if router's filter shape changes, mirror the change here.


def _present_canonicals_cte(silver: str, gold: str, affiliate: str | None) -> str:
    """CTE that yields the set of canonical sources currently in the lake,
    optionally scoped to a single affiliate.
    """
    if affiliate:
        return f"""
            present AS (
                SELECT DISTINCT t.source_system_canonical AS canonical
                FROM {fqn(silver, 'silver_tables')} t
                JOIN {fqn(silver, 'silver_schemas')} s
                    ON s.catalog_name = t.table_catalog
                   AND s.schema_name = t.table_schema
                JOIN {fqn(gold, 'program_affiliate_map')} pm
                    ON pm.program = COALESCE(s.program, 'Unknown')
                WHERE COALESCE(t.source_system_canonical, '') NOT IN ('', 'Unmapped', 'Other')
                  AND pm.affiliate_name = '{_q(affiliate)}'
            )
        """
    return f"""
        present AS (
            SELECT DISTINCT source_system_canonical AS canonical
            FROM {fqn(silver, 'silver_tables')}
            WHERE COALESCE(source_system_canonical, '') NOT IN ('', 'Unmapped', 'Other')
        )
    """


def _build_filter_clauses(
    *,
    silver: str,
    gold: str,
    affiliate: str | None,
    priority: str | None,
    department: str | None,
    status: str | None,
    search: str | None,
) -> tuple[str, str, str]:
    """Returns (present_cte, affiliate_join, where_sql)."""
    where_clauses: list[str] = []
    if priority:
        where_clauses.append(f"uc.priority = '{_q(priority)}'")
    if status:
        where_clauses.append(
            f"COALESCE(uc.status, 'not_started') = '{_q(status)}'"
        )
    if department:
        where_clauses.append(
            "COALESCE(NULLIF(TRIM(uc.department), ''), 'Unassigned') = "
            f"'{_q(department)}'"
        )
    if search:
        s = _q(search.lower())
        where_clauses.append(
            "("
            f"LOWER(uc.use_case_name) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(uc.description,'')) LIKE '%{s}%'"
            ")"
        )

    if affiliate:
        aff = _q(affiliate)
        join_sql = (
            f"JOIN {fqn(gold, 'use_case_affiliates')} ua_filter "
            f"ON ua_filter.use_case_id = uc.id "
            f"AND ua_filter.affiliate_name = '{aff}'"
        )
    else:
        join_sql = ""

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    return _present_canonicals_cte(silver, gold, affiliate), join_sql, where_sql


# ---------------------------------------------------------------------------
# app_value_summary
# ---------------------------------------------------------------------------


class ValueSummaryArgs(BaseModel):
    """Filter the Value & Readiness page-level KPIs."""

    affiliate: str | None = Field(
        default=None,
        description="Scope to a single affiliate (e.g. 'PacifiCorp').",
    )
    priority: str | None = Field(
        default=None,
        description="Filter by priority: 'High' | 'Medium' | 'Low'.",
    )
    status: str | None = Field(
        default=None,
        description=(
            "Filter by delivery status: 'not_started' | 'in_progress' | "
            "'delivered' | 'on_hold'."
        ),
    )
    department: str | None = Field(
        default=None,
        description="Filter to use cases owned by this department.",
    )
    search: str | None = Field(
        default=None,
        description="Free-text match against use case name/description.",
    )
    formula: str = Field(
        default="simple",
        description=(
            "Readiness formula: 'simple' = present/total_known, "
            "'must' = must_have_present/must_have_total."
        ),
    )


def _value_summary(args: ValueSummaryArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()
    present_cte, join_sql, where_sql = _build_filter_clauses(
        silver=silver, gold=gold,
        affiliate=args.affiliate, priority=args.priority,
        department=args.department, status=args.status, search=args.search,
    )

    if args.formula == "must":
        num_expr = (
            "SUM(CASE WHEN necessity='must_have' AND required_canonical!='Unmapped' "
            "THEN is_present ELSE 0 END)"
        )
        den_expr = (
            "COUNT(CASE WHEN necessity='must_have' AND required_canonical!='Unmapped' "
            "THEN 1 END)"
        )
    else:
        num_expr = (
            "SUM(CASE WHEN required_canonical!='Unmapped' THEN is_present ELSE 0 END)"
        )
        den_expr = (
            "COUNT(CASE WHEN required_canonical!='Unmapped' THEN 1 END)"
        )

    sql = f"""
        WITH {present_cte},
        scoped_uc AS (
            SELECT uc.id, uc.estimated_value_usd,
                   COALESCE(uc.status, 'not_started') AS status
            FROM {fqn(silver, 'use_cases')} uc
            {join_sql}
            {where_sql}
        ),
        joined AS (
            SELECT u.id, COALESCE(u.estimated_value_usd, 0) AS value, u.status,
                   r.required_canonical, r.necessity,
                   CASE WHEN p.canonical IS NOT NULL THEN 1 ELSE 0 END AS is_present
            FROM scoped_uc u
            LEFT JOIN {fqn(gold, 'use_case_source_requirements')} r ON r.use_case_id = u.id
            LEFT JOIN present p
                   ON p.canonical = r.required_canonical
                  AND r.required_canonical != 'Unmapped'
        ),
        per_uc AS (
            SELECT id, MAX(value) AS value, MAX(status) AS status,
                   {num_expr} AS num,
                   {den_expr} AS den
            FROM joined
            GROUP BY id
        )
        SELECT
            COUNT(*)                                               AS total_use_cases,
            COALESCE(SUM(value), 0)                                AS total_value,
            COALESCE(SUM(value * CASE WHEN den IS NULL OR den = 0 THEN 0
                                       ELSE num * 1.0 / den END), 0)  AS ready_value,
            COALESCE(SUM(value * (1 - CASE WHEN den IS NULL OR den = 0 THEN 0
                                            ELSE num * 1.0 / den END)), 0) AS gap_value,
            COUNT(CASE WHEN status='delivered'   THEN 1 END)       AS uc_delivered,
            COUNT(CASE WHEN status='in_progress' THEN 1 END)       AS uc_in_progress,
            COUNT(CASE WHEN status='not_started' THEN 1 END)       AS uc_not_started,
            COUNT(CASE WHEN status='on_hold'     THEN 1 END)       AS uc_on_hold,
            COALESCE(SUM(CASE WHEN status='delivered'   THEN value END), 0) AS val_delivered,
            COALESCE(SUM(CASE WHEN status='in_progress' THEN value END), 0) AS val_in_progress,
            COALESCE(SUM(CASE WHEN status='not_started' THEN value END), 0) AS val_not_started,
            COALESCE(SUM(CASE WHEN status='on_hold'     THEN value END), 0) AS val_on_hold
        FROM per_uc
    """
    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.value_summary"}
        )
    except Exception as e:
        logger.exception("app_value_summary SQL failed")
        return ToolResult(ok=False, summary=f"Summary failed: {e}", data={"error": str(e)})

    if not rows:
        return ToolResult(ok=True, summary="No use cases in scope", data={"summary": None})
    r = rows[0]
    total_value = float(r.get("total_value") or 0)
    ready_value = float(r.get("ready_value") or 0)

    summary_data = {
        "total_use_cases": int(r.get("total_use_cases") or 0),
        "total_value": total_value,
        "ready_value": ready_value,
        "gap_value": float(r.get("gap_value") or 0),
        "ready_pct": (round(100.0 * ready_value / total_value, 1) if total_value > 0 else None),
        "by_status": {
            "delivered": {
                "use_cases": int(r.get("uc_delivered") or 0),
                "value":     float(r.get("val_delivered") or 0),
            },
            "in_progress": {
                "use_cases": int(r.get("uc_in_progress") or 0),
                "value":     float(r.get("val_in_progress") or 0),
            },
            "not_started": {
                "use_cases": int(r.get("uc_not_started") or 0),
                "value":     float(r.get("val_not_started") or 0),
            },
            "on_hold": {
                "use_cases": int(r.get("uc_on_hold") or 0),
                "value":     float(r.get("val_on_hold") or 0),
            },
        },
        "filters": {
            "affiliate": args.affiliate, "priority": args.priority,
            "department": args.department, "status": args.status,
            "search": args.search, "formula": args.formula,
        },
    }
    bits = [
        f"{summary_data['total_use_cases']} use case{'s' if summary_data['total_use_cases'] != 1 else ''}",
        f"${total_value/1e6:.1f}M total",
    ]
    if summary_data["ready_pct"] is not None:
        bits.append(f"{summary_data['ready_pct']:.0f}% data-ready")
    return ToolResult(
        ok=True,
        summary=", ".join(bits),
        data={"summary": summary_data},
    )


VALUE_SUMMARY = Tool(
    name="app_value_summary",
    description=(
        "Portfolio-level value KPIs across BHE use cases: total value, "
        "data-ready value, gap value, and breakdown by delivery status "
        "(delivered/in-progress/not-started/on-hold). All filters are "
        "optional. Use this when the user asks 'what's our total value', "
        "'how much value is data-ready at PacifiCorp', etc."
    ),
    args_model=ValueSummaryArgs,
    handler=_value_summary,
)


# ---------------------------------------------------------------------------
# app_value_source_rollup
# ---------------------------------------------------------------------------


class ValueSourceRollupArgs(BaseModel):
    """Source ROI: 'if you ingest X you unlock $Y of use cases'."""

    affiliate: str | None = Field(default=None)
    priority: str | None = Field(default=None)
    department: str | None = Field(default=None)
    search: str | None = Field(default=None)
    only_missing: bool = Field(
        default=False,
        description=(
            "If true, return only canonical sources NOT yet in the lake — "
            "the gap list. Use this for questions like 'what should we "
            "ingest next to unlock the most value?'."
        ),
    )
    limit: int = Field(default=15, ge=1, le=50)


def _value_source_rollup(args: ValueSourceRollupArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()
    present_cte, join_sql, where_sql = _build_filter_clauses(
        silver=silver, gold=gold,
        affiliate=args.affiliate, priority=args.priority,
        department=args.department, status=None, search=args.search,
    )

    sql = f"""
        WITH {present_cte},
        scoped_uc AS (
            SELECT uc.id, COALESCE(uc.estimated_value_usd, 0) AS value
            FROM {fqn(silver, 'use_cases')} uc
            {join_sql}
            {where_sql}
        )
        SELECT
            r.required_canonical                            AS canonical,
            COALESCE(c.category, 'Other')                   AS category,
            COUNT(DISTINCT u.id)                            AS use_case_count,
            COALESCE(SUM(u.value), 0)                       AS total_value,
            SUM(CASE WHEN r.necessity='must_have' THEN 1 ELSE 0 END) AS must_have_links,
            COALESCE(SUM(CASE WHEN r.necessity='must_have' THEN u.value ELSE 0 END), 0)
                                                            AS must_have_value,
            CASE WHEN p.canonical IS NOT NULL THEN true ELSE false END AS is_present
        FROM scoped_uc u
        JOIN {fqn(gold, 'use_case_source_requirements')} r ON r.use_case_id = u.id
        LEFT JOIN present p
               ON p.canonical = r.required_canonical
              AND r.required_canonical != 'Unmapped'
        LEFT JOIN {fqn(gold, 'source_system_canonical')} c
               ON c.canonical = r.required_canonical
        WHERE r.required_canonical NOT IN ('Unmapped', 'Other')
        GROUP BY r.required_canonical, p.canonical, c.category
        ORDER BY total_value DESC, use_case_count DESC
    """
    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.value_source_rollup"}
        )
    except Exception as e:
        logger.exception("app_value_source_rollup SQL failed")
        return ToolResult(ok=False, summary=f"Rollup failed: {e}", data={"error": str(e)})

    items: list[dict] = []
    for r in rows:
        is_present = bool(r.get("is_present"))
        if args.only_missing and is_present:
            continue
        items.append({
            "canonical": r.get("canonical"),
            "category": r.get("category"),
            "use_case_count": int(r.get("use_case_count") or 0),
            "total_value": float(r.get("total_value") or 0),
            "must_have_links": int(r.get("must_have_links") or 0),
            "must_have_value": float(r.get("must_have_value") or 0),
            "is_present": is_present,
        })
        if len(items) >= args.limit:
            break

    summary_total = sum(it["total_value"] for it in items)
    summary = (
        f"{len(items)} source{'s' if len(items) != 1 else ''} "
        f"unlock ${summary_total/1e6:.1f}M"
    )
    if args.only_missing:
        summary += " (gaps only)"
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "sources": items,
            "filters": {
                "affiliate": args.affiliate, "priority": args.priority,
                "department": args.department, "search": args.search,
                "only_missing": args.only_missing,
            },
        },
    )


VALUE_SOURCE_ROLLUP = Tool(
    name="app_value_source_rollup",
    description=(
        "Rank canonical source systems by the total $ value of use cases "
        "that need them. Each row says 'if we ingest X, we unlock Y use "
        "cases worth $Z'. Use this for source-investment-prioritization "
        "questions; pass only_missing=true for the 'what should we ingest "
        "next' shortlist."
    ),
    args_model=ValueSourceRollupArgs,
    handler=_value_source_rollup,
)


# ---------------------------------------------------------------------------
# app_gaps_matrix
# ---------------------------------------------------------------------------


class GapsMatrixArgs(BaseModel):
    """Affiliate × canonical coverage. The model usually wants a slice, not
    the whole grid — accept optional filters and a sensible default cap."""

    affiliate: str | None = Field(
        default=None,
        description=(
            "Optional: restrict to one affiliate. The matrix collapses to a "
            "list of canonicals with present/required/gap flags for that "
            "affiliate."
        ),
    )
    canonical: str | None = Field(
        default=None,
        description=(
            "Optional: restrict to one canonical source. The matrix "
            "collapses to a list of affiliates with present/required/gap "
            "flags for that source."
        ),
    )
    only_gaps: bool = Field(
        default=False,
        description=(
            "If true, return only cells where the source IS required and "
            "NOT present (the gap list)."
        ),
    )
    limit: int = Field(default=40, ge=1, le=200)


def _gaps_matrix(args: GapsMatrixArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()

    base_sql = f"""
        WITH required AS (
            SELECT
                r.required_canonical AS canonical,
                ua.affiliate_name,
                COUNT(DISTINCT r.use_case_id) AS uc_count,
                SUM(CASE WHEN r.necessity='must_have' THEN 1 ELSE 0 END) AS must_count,
                COALESCE(SUM(COALESCE(uc.estimated_value_usd, 0)), 0) AS total_value
            FROM {fqn(gold, 'use_case_source_requirements')} r
            JOIN {fqn(gold, 'use_case_affiliates')} ua
                   ON ua.use_case_id = r.use_case_id
            JOIN {fqn(silver, 'use_cases')} uc
                   ON uc.id = r.use_case_id
            WHERE r.required_canonical NOT IN ('Unmapped', 'Other')
              AND ua.affiliate_name NOT IN ('Multi-Affiliate')
            GROUP BY r.required_canonical, ua.affiliate_name
        ),
        present_per_aff AS (
            SELECT DISTINCT t.source_system_canonical AS canonical,
                            pm.affiliate_name
            FROM {fqn(silver, 'silver_tables')} t
            JOIN {fqn(silver, 'silver_schemas')} s
                   ON s.catalog_name = t.table_catalog
                  AND s.schema_name = t.table_schema
            JOIN {fqn(gold, 'program_affiliate_map')} pm
                   ON pm.program = COALESCE(s.program, 'Unknown')
            WHERE COALESCE(t.source_system_canonical, '') NOT IN ('', 'Other', 'Unmapped')
        ),
        all_canonicals AS (
            SELECT canonical FROM required
            UNION
            SELECT canonical FROM present_per_aff
        ),
        all_affiliates AS (
            SELECT affiliate_name FROM {fqn(gold, 'affiliates')}
            WHERE COALESCE(is_active, true) = true
              AND affiliate_name NOT IN ('Multi-Affiliate')
        )
        SELECT
            c.canonical                                       AS canonical,
            a.affiliate_name                                  AS affiliate_name,
            COALESCE(r.uc_count, 0)                           AS uc_count,
            COALESCE(r.must_count, 0)                         AS must_count,
            COALESCE(r.total_value, 0)                        AS total_value,
            CASE WHEN p.canonical IS NOT NULL THEN true ELSE false END AS is_present,
            CASE WHEN r.canonical IS NOT NULL THEN true ELSE false END AS is_required
        FROM all_canonicals c
        CROSS JOIN all_affiliates a
        LEFT JOIN required r
               ON r.canonical = c.canonical
              AND r.affiliate_name = a.affiliate_name
        LEFT JOIN present_per_aff p
               ON p.canonical = c.canonical
              AND p.affiliate_name = a.affiliate_name
        WHERE (r.canonical IS NOT NULL OR p.canonical IS NOT NULL)
    """
    extras: list[str] = []
    if args.affiliate:
        extras.append(f"AND a.affiliate_name = '{_q(args.affiliate)}'")
    if args.canonical:
        extras.append(f"AND c.canonical = '{_q(args.canonical)}'")
    sql = base_sql + "\n" + "\n".join(extras) + "\nORDER BY total_value DESC, c.canonical, a.affiliate_name"

    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.gaps_matrix"}
        )
    except Exception as e:
        logger.exception("app_gaps_matrix SQL failed")
        return ToolResult(ok=False, summary=f"Matrix failed: {e}", data={"error": str(e)})

    cells: list[dict] = []
    gap_count = covered_count = available_count = 0
    total_gap_value = 0.0
    for r in rows:
        is_required = bool(r.get("is_required"))
        is_present = bool(r.get("is_present"))
        if is_required and is_present:
            state = "covered"
            covered_count += 1
        elif is_required and not is_present:
            state = "gap"
            gap_count += 1
            total_gap_value += float(r.get("total_value") or 0)
        elif not is_required and is_present:
            state = "available"
            available_count += 1
        else:
            continue
        if args.only_gaps and state != "gap":
            continue
        cells.append({
            "canonical": r.get("canonical"),
            "affiliate": r.get("affiliate_name"),
            "state": state,
            "use_case_count": int(r.get("uc_count") or 0),
            "must_count": int(r.get("must_count") or 0),
            "total_value": float(r.get("total_value") or 0),
        })
        if len(cells) >= args.limit:
            break

    bits = [f"{gap_count} gap{'s' if gap_count != 1 else ''}"]
    if total_gap_value > 0:
        bits.append(f"${total_gap_value/1e6:.1f}M at risk")
    if covered_count:
        bits.append(f"{covered_count} covered")
    return ToolResult(
        ok=True,
        summary=", ".join(bits),
        data={
            "cells": cells,
            "totals": {
                "gap_count": gap_count,
                "covered_count": covered_count,
                "available_count": available_count,
                "total_gap_value": total_gap_value,
            },
            "filters": {
                "affiliate": args.affiliate,
                "canonical": args.canonical,
                "only_gaps": args.only_gaps,
            },
        },
    )


GAPS_MATRIX = Tool(
    name="app_gaps_matrix",
    description=(
        "Affiliate × canonical-source coverage matrix: which sources are "
        "required by which affiliate's use cases, which are present in the "
        "lake, and which are gaps. Pass affiliate or canonical to slice; "
        "pass only_gaps=true for the gap list. Use this for cross-affiliate "
        "questions like 'where do we have the most data gaps?'."
    ),
    args_model=GapsMatrixArgs,
    handler=_gaps_matrix,
)
