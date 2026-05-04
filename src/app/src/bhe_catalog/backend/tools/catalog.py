"""App-typed tools that read the catalog directly via the silver/gold layer.

These tools intentionally do NOT call our own HTTP API — they go straight
to `db.execute_query` against the same tables the REST handlers use.
Going through HTTP would mean serializing identity headers, paying a
round-trip for every tool call, and making local-vs-deployed behavior
diverge. The service layer is already the source of truth.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..db import execute_query, fqn, get_gold_schema, get_silver_schema
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# app_search_use_cases
# ---------------------------------------------------------------------------


class SearchUseCasesArgs(BaseModel):
    """The slice of /value/use-cases the chat needs.

    We deliberately expose fewer knobs than the REST endpoint. The model
    doesn't need `formula` (always 'simple' here) or `priority`/`status`
    filters in PR #2 — those add tool-call complexity without unlocking
    questions the model gets asked in practice. We can add them later.
    """

    query: str | None = Field(
        default=None,
        description=(
            "Free-text search over use-case name and description. "
            "Omit to list top use cases without filtering by text."
        ),
    )
    department: str | None = Field(
        default=None,
        description=(
            "Filter to use cases owned by this department. Match is "
            "case-sensitive against the canonical department name "
            "(e.g. 'Operations', 'IT', 'Generation')."
        ),
    )
    affiliate: str | None = Field(
        default=None,
        description=(
            "Filter to use cases applicable to this affiliate. Examples: "
            "'PacifiCorp', 'NV Energy', 'BHE Pipeline Group'."
        ),
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max use cases to return. Smaller is better for chat readability.",
    )


def _search_use_cases(args: SearchUseCasesArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()

    # SQL escape helper local to keep this module self-contained. Same
    # convention as bootstrap_tables.py and chat.py: single-quote doubling.
    def q(s: str) -> str:
        return s.replace("'", "''")

    where: list[str] = []
    if args.query:
        s = q(args.query.lower())
        where.append(
            "(LOWER(uc.use_case_name) LIKE '%" + s + "%' "
            "OR LOWER(COALESCE(uc.description,'')) LIKE '%" + s + "%')"
        )
    if args.department:
        where.append(
            "COALESCE(NULLIF(TRIM(uc.department), ''), 'Unassigned') = '"
            + q(args.department) + "'"
        )

    affiliate_join = ""
    if args.affiliate:
        affiliate_join = (
            f"JOIN {fqn(gold, 'use_case_affiliates')} ua_filter "
            f"ON ua_filter.use_case_id = uc.id "
            f"AND ua_filter.affiliate_name = '{q(args.affiliate)}'"
        )

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        WITH affs AS (
            SELECT use_case_id, COLLECT_SET(affiliate_name) AS applicable_affiliates
            FROM {fqn(gold, 'use_case_affiliates')}
            GROUP BY use_case_id
        )
        SELECT
            uc.id AS use_case_id,
            uc.use_case_name,
            uc.description,
            uc.department,
            uc.priority,
            COALESCE(uc.status, 'not_started') AS status,
            COALESCE(uc.estimated_value_usd, 0) AS estimated_value_usd,
            COALESCE(affs.applicable_affiliates, array()) AS applicable_affiliates
        FROM {fqn(silver, 'use_cases')} uc
        {affiliate_join}
        LEFT JOIN affs ON affs.use_case_id = uc.id
        {where_sql}
        ORDER BY uc.estimated_value_usd DESC NULLS LAST, uc.use_case_name
        LIMIT {args.limit}
    """
    try:
        rows = execute_query(
            sql,
            tag_overrides={"submodule": "chat.tool.search_use_cases"},
        )
    except Exception as e:
        logger.exception("app_search_use_cases SQL failed")
        return ToolResult(ok=False, summary=f"Search failed: {e}", data={"error": str(e)})

    items: list[dict] = []
    citations: list[dict[str, str]] = []
    for r in rows:
        affs = r.get("applicable_affiliates") or []
        # Spark sometimes returns the array as a JSON string when going
        # through SEA; normalize both shapes.
        if isinstance(affs, str):
            try:
                import json as _json
                affs = _json.loads(affs) or []
            except Exception:
                affs = []
        item = {
            "use_case_id": r.get("use_case_id"),
            "use_case_name": r.get("use_case_name"),
            "description": (r.get("description") or "").strip()[:280] or None,
            "department": r.get("department"),
            "priority": r.get("priority"),
            "status": r.get("status"),
            "estimated_value_usd": float(r.get("estimated_value_usd") or 0),
            "applicable_affiliates": affs,
        }
        items.append(item)
        if item["use_case_id"]:
            citations.append({
                "label": item["use_case_name"] or item["use_case_id"],
                # The use-case page is /value-readiness in this app.
                # `?uc=<id>` is a forward-looking convention — wiring the
                # page to open the drawer from a search param is a small
                # follow-up; for now the link lands on the right page and
                # the user sees the row in context.
                "deeplink": f"/value-readiness?uc={item['use_case_id']}",
            })

    if not items:
        bits = []
        if args.query:
            bits.append(f"query='{args.query}'")
        if args.department:
            bits.append(f"department='{args.department}'")
        if args.affiliate:
            bits.append(f"affiliate='{args.affiliate}'")
        msg = "No use cases matched"
        if bits:
            msg += " (" + ", ".join(bits) + ")"
        return ToolResult(ok=True, summary=msg, data={"use_cases": []})

    return ToolResult(
        ok=True,
        summary=f"Found {len(items)} use case{'s' if len(items) != 1 else ''}",
        data={
            "use_cases": items,
            "filters": {
                "query": args.query,
                "department": args.department,
                "affiliate": args.affiliate,
            },
        },
        citations=citations,
    )


SEARCH_USE_CASES = Tool(
    name="app_search_use_cases",
    description=(
        "Search BHE business use cases by free-text query, department, or "
        "affiliate. Returns the top matches ordered by estimated business "
        "value (descending). Use this when the user asks about specific use "
        "cases by name, theme, owner department, or affiliate scope. Each "
        "result includes a stable use_case_id you can cite."
    ),
    args_model=SearchUseCasesArgs,
    handler=_search_use_cases,
)


# ---------------------------------------------------------------------------
# app_get_use_case
# ---------------------------------------------------------------------------
#
# This is the natural follow-up to app_search_use_cases. The model uses the
# search tool to locate the use_case_id and then this tool to drill into the
# full record: business value, value rationale, required canonical sources
# (with present/missing status), applicability across affiliates, and
# delivery status. The result intentionally mirrors the shape of
# `/value/use-cases/{id}` so future Phase A2 propose_* tools can build a
# diff against it without reshaping data.


class GetUseCaseArgs(BaseModel):
    """Look up a single use case by its stable identifier."""

    use_case_id: str = Field(
        ...,
        description=(
            "Stable use_case_id as returned by app_search_use_cases — "
            "usually 8 hex characters (e.g. '3b6bb55b'); chat-created "
            "rows use a 'uc_<12hex>' form. Pass exactly what you got "
            "from a prior tool result; the lookup tolerates either form "
            "if the user pastes a raw value."
        ),
    )
    affiliate: str | None = Field(
        default=None,
        description=(
            "Optional affiliate scope (e.g. 'PacifiCorp', 'NV Energy'). When "
            "set, the readiness summary counts a canonical source as 'present' "
            "only if it's ingested in that affiliate's catalogs. Omit for a "
            "global view."
        ),
    )


def _get_use_case(args: GetUseCaseArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()

    def q(s: str) -> str:
        return s.replace("'", "''")

    # Tolerate both `uc_xxxx` (chat-created) and bare `xxxxxxxx` (seeded)
    # forms. We probe with whichever variant the caller did NOT pass as
    # a fallback — keeps things to one query for the common case but
    # always finds the row if it exists.
    raw = args.use_case_id.strip()
    bare = raw[3:] if raw.startswith("uc_") else raw
    candidates = [raw] if raw == bare else [raw, bare]
    uc_id = q(candidates[0])
    candidates_sql = ", ".join(f"'{q(c)}'" for c in candidates)

    # Affiliate-scoped or global "present canonicals" set. Mirrors the
    # _present_canonicals_cte() in router.py; we inline the SQL here so the
    # tool stays a leaf module that doesn't depend on router internals.
    if args.affiliate:
        aff = q(args.affiliate)
        present_cte = f"""
            present AS (
                SELECT DISTINCT t.source_system_canonical AS canonical
                FROM {fqn(silver, 'silver_tables')} t
                JOIN {fqn(silver, 'silver_schemas')} s
                    ON s.catalog_name = t.table_catalog
                   AND s.schema_name = t.table_schema
                JOIN {fqn(gold, 'program_affiliate_map')} pm
                    ON pm.program = COALESCE(s.program, 'Unknown')
                WHERE COALESCE(t.source_system_canonical, '') NOT IN ('', 'Unmapped', 'Other')
                  AND pm.affiliate_name = '{aff}'
            )
        """
    else:
        present_cte = f"""
            present AS (
                SELECT DISTINCT source_system_canonical AS canonical
                FROM {fqn(silver, 'silver_tables')}
                WHERE COALESCE(source_system_canonical, '') NOT IN ('', 'Unmapped', 'Other')
            )
        """

    try:
        meta_rows = execute_query(
            f"""
            SELECT
                id, use_case_name, description, department, category, priority,
                business_value, COALESCE(estimated_value_usd, 0) AS estimated_value_usd,
                value_rationale,
                COALESCE(status, 'not_started') AS status,
                status_notes
            FROM {fqn(silver, 'use_cases')}
            WHERE id IN ({candidates_sql})
            LIMIT 1
            """,
            tag_overrides={"submodule": "chat.tool.get_use_case"},
        )
    except Exception as e:
        logger.exception("app_get_use_case meta lookup failed")
        return ToolResult(ok=False, summary=f"Lookup failed: {e}", data={"error": str(e)})

    if not meta_rows:
        return ToolResult(
            ok=True,
            summary=f"No use case with id '{args.use_case_id}'",
            data={"use_case": None},
        )
    m = meta_rows[0]
    # Pin uc_id to whatever actually matched so the readiness CTE below
    # uses the right key.
    uc_id = q(str(m.get("id") or candidates[0]))

    try:
        reqs = execute_query(
            f"""
            WITH {present_cte}
            SELECT r.required_canonical, r.necessity, r.data_need_excerpt,
                   CASE WHEN p.canonical IS NOT NULL THEN true ELSE false END AS is_present
            FROM {fqn(gold, 'use_case_source_requirements')} r
            LEFT JOIN present p ON p.canonical = r.required_canonical
            WHERE r.use_case_id = '{uc_id}'
            ORDER BY r.necessity DESC, r.required_canonical
            """,
            tag_overrides={"submodule": "chat.tool.get_use_case"},
        )
        affs = execute_query(
            f"""
            SELECT affiliate_name, applicability, rationale
            FROM {fqn(gold, 'use_case_affiliates')}
            WHERE use_case_id = '{uc_id}'
            ORDER BY CASE WHEN applicability='primary' THEN 0 ELSE 1 END, affiliate_name
            """,
            tag_overrides={"submodule": "chat.tool.get_use_case"},
        )
    except Exception as e:
        logger.exception("app_get_use_case detail lookup failed")
        return ToolResult(
            ok=False,
            summary=f"Detail lookup failed: {e}",
            data={"use_case": m, "error": str(e)},
        )

    present, missing, unmapped = [], [], []
    must_total = must_present = total = 0
    for r in reqs:
        canonical = r.get("required_canonical")
        is_present = bool(r.get("is_present"))
        excerpt = (r.get("data_need_excerpt") or "").strip()[:200] or None
        item = {
            "canonical": canonical,
            "necessity": r.get("necessity"),
            "data_need": excerpt,
            "is_present": is_present,
        }
        if canonical == "Unmapped":
            unmapped.append(item)
            continue
        total += 1
        if r.get("necessity") == "must_have":
            must_total += 1
            if is_present:
                must_present += 1
        if is_present:
            present.append(item)
        else:
            missing.append(item)

    primary_aff = next(
        (a.get("affiliate_name") for a in affs if a.get("applicability") == "primary"),
        None,
    )

    use_case = {
        "use_case_id": m.get("id"),
        "use_case_name": m.get("use_case_name"),
        "description": m.get("description"),
        "department": m.get("department"),
        "category": m.get("category"),
        "priority": m.get("priority"),
        "status": m.get("status"),
        "status_notes": (m.get("status_notes") or "").strip() or None,
        "business_value": (m.get("business_value") or "").strip() or None,
        "value_rationale": (m.get("value_rationale") or "").strip() or None,
        "estimated_value_usd": float(m.get("estimated_value_usd") or 0),
        "primary_affiliate": primary_aff,
        "applicable_affiliates": [
            {
                "name": a.get("affiliate_name"),
                "applicability": a.get("applicability"),
            }
            for a in affs
        ],
    }
    readiness = {
        "total_required": total,
        "present_count": len(present),
        "missing_count": len(missing),
        "must_total": must_total,
        "must_present": must_present,
        "unmapped_count": len(unmapped),
        "readiness_pct_simple": (round(100.0 * len(present) / total, 1) if total else None),
        "readiness_pct_must": (round(100.0 * must_present / must_total, 1) if must_total else None),
    }

    citations: list[dict[str, str]] = []
    if use_case["use_case_id"]:
        citations.append({
            "label": use_case["use_case_name"] or use_case["use_case_id"],
            "deeplink": f"/value-readiness?uc={use_case['use_case_id']}",
        })

    bits = [f"value ${use_case['estimated_value_usd']:,.0f}"]
    if readiness["readiness_pct_simple"] is not None:
        bits.append(f"{readiness['readiness_pct_simple']:.0f}% data-ready")
    bits.append(f"{readiness['present_count']}/{total} sources present")
    summary = f"{use_case['use_case_name']} — " + ", ".join(bits)

    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "use_case": use_case,
            "readiness": readiness,
            "present_sources": present,
            "missing_sources": missing,
            "unmapped_needs": unmapped,
            "scope": {"affiliate": args.affiliate} if args.affiliate else None,
        },
        citations=citations,
    )


GET_USE_CASE = Tool(
    name="app_get_use_case",
    description=(
        "Get the full record for one use case by its use_case_id: business "
        "value, value rationale, required canonical sources with present/"
        "missing status, applicability across affiliates, and delivery "
        "status. Use this after app_search_use_cases to drill into a "
        "specific use case the user asked about."
    ),
    args_model=GetUseCaseArgs,
    handler=_get_use_case,
)
