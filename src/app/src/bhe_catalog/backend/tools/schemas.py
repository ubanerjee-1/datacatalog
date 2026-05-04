"""Schema-side typed tools.

`app_search_schemas` mirrors the Catalog Browser query (`/catalog/schemas`)
without the system-classification filter the page applies — the chat is
allowed to surface internal schemas if the user explicitly asks. We still
exclude the `SYSTEM` classification by default because it's noise (Unity
Catalog's own bookkeeping schemas).

`app_get_schema` returns one row per *logical* schema (the same name can
exist across dev/qa/prod catalogs), grouped the same way the Schema
Explorer page does. We pull from `bhe_gold.schema_inventory` so the
environment flags + table count come out of pre-aggregated data and the
chat doesn't pay the GROUP BY cost on every call.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..db import execute_query, fqn, get_gold_schema, get_silver_schema
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# app_search_schemas
# ---------------------------------------------------------------------------


class SearchSchemasArgs(BaseModel):
    """Filter the silver schema inventory by environment, program, and text."""

    query: str | None = Field(
        default=None,
        description=(
            "Free-text match against catalog_name, schema_name, "
            "business_friendly_name, and AI definition. Omit to list "
            "schemas without filtering by text."
        ),
    )
    environment: str | None = Field(
        default=None,
        description="Filter by environment: 'dev' | 'qa' | 'prod'.",
    )
    program: str | None = Field(
        default=None,
        description=(
            "Filter by program/affiliate (e.g. 'PacifiCorp', 'NV Energy', "
            "'MidAmerican')."
        ),
    )
    domain: str | None = Field(
        default=None,
        description=(
            "Filter by suggested domain (e.g. 'Operations', 'Customer', "
            "'Regulatory'). The chat can surface 'distinct domains' via the "
            "filter facet returned alongside results."
        ),
    )
    missing_definition: bool = Field(
        default=False,
        description=(
            "If true, return ONLY schemas whose AI definition is empty/null. "
            "Use this to answer questions like 'which schemas are missing "
            "AI definitions in PacifiCorp?'."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max schemas to return. Smaller is better for chat.",
    )


def _q(s: str) -> str:
    return s.replace("'", "''")


def _search_schemas(args: SearchSchemasArgs, ctx: ToolContext) -> ToolResult:
    silver = get_silver_schema()
    where: list[str] = ["classification NOT IN ('INTERNAL', 'SYSTEM')"]
    if args.environment:
        where.append(f"environment = '{_q(args.environment)}'")
    if args.program:
        where.append(f"program = '{_q(args.program)}'")
    if args.domain:
        where.append(f"suggested_domain = '{_q(args.domain)}'")
    if args.missing_definition:
        where.append("(ai_definition IS NULL OR TRIM(ai_definition) = '')")
    if args.query:
        s = _q(args.query.lower())
        where.append(
            "("
            f"LOWER(catalog_name) LIKE '%{s}%' "
            f"OR LOWER(schema_name) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(business_friendly_name,'')) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(ai_definition,'')) LIKE '%{s}%'"
            ")"
        )
    where_sql = "WHERE " + " AND ".join(where)

    sql = f"""
        SELECT
            catalog_name,
            schema_name,
            COALESCE(environment, '') AS environment,
            COALESCE(program, '') AS program,
            COALESCE(zone, '') AS zone,
            COALESCE(business_friendly_name, '') AS business_friendly_name,
            COALESCE(ai_definition, '') AS ai_definition,
            COALESCE(suggested_domain, '') AS suggested_domain,
            COALESCE(suggested_department, '') AS suggested_department,
            COALESCE(classification, '') AS classification
        FROM {fqn(silver, 'silver_schemas')}
        {where_sql}
        ORDER BY catalog_name, schema_name
        LIMIT {args.limit}
    """
    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.search_schemas"}
        )
        # Total count (without LIMIT) so the LLM can say "showing 20 of 45".
        cnt_rows = execute_query(
            f"SELECT COUNT(*) AS n FROM {fqn(silver, 'silver_schemas')} {where_sql}",
            tag_overrides={"submodule": "chat.tool.search_schemas"},
        )
        total = int(cnt_rows[0].get("n") or 0) if cnt_rows else 0
    except Exception as e:
        logger.exception("app_search_schemas SQL failed")
        return ToolResult(ok=False, summary=f"Search failed: {e}", data={"error": str(e)})

    items: list[dict] = []
    citations: list[dict[str, str]] = []
    for r in rows:
        catalog = r.get("catalog_name") or ""
        schema = r.get("schema_name") or ""
        ai_def = (r.get("ai_definition") or "").strip()
        items.append({
            "catalog_name": catalog,
            "schema_name": schema,
            "environment": r.get("environment") or None,
            "program": r.get("program") or None,
            "zone": r.get("zone") or None,
            "business_friendly_name": r.get("business_friendly_name") or None,
            "ai_definition": (ai_def[:240] + "…") if len(ai_def) > 240 else (ai_def or None),
            "has_definition": bool(ai_def),
            "suggested_domain": r.get("suggested_domain") or None,
            "suggested_department": r.get("suggested_department") or None,
        })
        # The Schema Explorer pivots on the logical schema name (not the
        # catalog), so the deeplink is keyed by `schema_name` only. The page
        # then resolves environment flags itself.
        if schema:
            citations.append({
                "label": f"{catalog}.{schema}",
                "deeplink": f"/explorer?schema={schema}",
            })

    if not items:
        bits = []
        if args.query:
            bits.append(f"query='{args.query}'")
        if args.environment:
            bits.append(f"environment='{args.environment}'")
        if args.program:
            bits.append(f"program='{args.program}'")
        if args.domain:
            bits.append(f"domain='{args.domain}'")
        if args.missing_definition:
            bits.append("missing_definition=true")
        msg = "No schemas matched"
        if bits:
            msg += " (" + ", ".join(bits) + ")"
        return ToolResult(ok=True, summary=msg, data={"schemas": [], "total": 0})

    summary = f"Found {total} schema{'s' if total != 1 else ''}"
    if total > len(items):
        summary += f" (showing top {len(items)})"
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "schemas": items,
            "total": total,
            "filters": {
                "query": args.query,
                "environment": args.environment,
                "program": args.program,
                "domain": args.domain,
                "missing_definition": args.missing_definition,
            },
        },
        citations=citations,
    )


SEARCH_SCHEMAS = Tool(
    name="app_search_schemas",
    description=(
        "Search the BHE silver schema inventory by environment, program "
        "(affiliate), domain, free-text, or 'missing AI definition'. Use "
        "this for questions about catalog coverage like 'which schemas are "
        "missing definitions in PacifiCorp?' or 'show me all NV Energy "
        "schemas in prod'. Returns at most 100 rows; total count is in "
        "the result so you can say 'X of Y'."
    ),
    args_model=SearchSchemasArgs,
    handler=_search_schemas,
)


# ---------------------------------------------------------------------------
# app_get_schema
# ---------------------------------------------------------------------------


class GetSchemaArgs(BaseModel):
    """Look up a single logical schema (rolled up across environments)."""

    schema_name: str = Field(
        ...,
        description=(
            "Logical schema name (e.g. 'maximo', 'pi_historian'). The same "
            "name can exist across dev/qa/prod catalogs — this tool collapses "
            "those into one row with environment flags."
        ),
    )


def _get_schema(args: GetSchemaArgs, ctx: ToolContext) -> ToolResult:
    gold = get_gold_schema()
    name_q = _q(args.schema_name.strip())
    inv = fqn(gold, "schema_inventory")

    sql = f"""
        SELECT
            schema_name,
            MAX(program) AS program,
            MAX(affiliate) AS affiliate,
            MAX(zone) AS zone,
            MAX(CASE WHEN environment = 'dev'  THEN true ELSE false END) AS in_dev,
            MAX(CASE WHEN environment = 'qa'   THEN true ELSE false END) AS in_qa,
            MAX(CASE WHEN environment = 'prod' THEN true ELSE false END) AS in_prod,
            COALESCE(SUM(table_count), 0)            AS total_tables,
            MAX(CASE WHEN business_name IS NOT NULL AND business_name != ''
                     THEN business_name END)         AS business_name,
            MAX(CASE WHEN definition IS NOT NULL AND definition != ''
                     THEN definition END)            AS definition,
            MAX(data_domain)                         AS data_domain,
            MAX(department_owner)                    AS department_owner,
            MAX(source_system)                       AS source_system,
            MAX(sensitivity)                         AS sensitivity,
            COLLECT_SET(catalog_name)                AS catalogs
        FROM {inv}
        WHERE schema_name = '{name_q}'
        GROUP BY schema_name
    """
    try:
        rows = execute_query(
            sql, tag_overrides={"submodule": "chat.tool.get_schema"}
        )
    except Exception as e:
        logger.exception("app_get_schema SQL failed")
        return ToolResult(ok=False, summary=f"Lookup failed: {e}", data={"error": str(e)})

    if not rows:
        return ToolResult(
            ok=True,
            summary=f"No schema named '{args.schema_name}'",
            data={"schema": None},
        )
    r = rows[0]

    catalogs_raw = r.get("catalogs")
    if isinstance(catalogs_raw, str):
        try:
            import json as _json
            catalogs_raw = _json.loads(catalogs_raw) or []
        except Exception:
            catalogs_raw = []
    catalogs = [c for c in (catalogs_raw or []) if c]

    envs = []
    if bool(r.get("in_dev")):  envs.append("dev")
    if bool(r.get("in_qa")):   envs.append("qa")
    if bool(r.get("in_prod")): envs.append("prod")

    definition = (r.get("definition") or "").strip()
    schema = {
        "schema_name": r.get("schema_name"),
        "program": r.get("program"),
        "affiliate": r.get("affiliate"),
        "zone": r.get("zone"),
        "environments": envs,
        "catalogs": sorted(catalogs),
        "total_tables": int(r.get("total_tables") or 0),
        "business_name": r.get("business_name"),
        "definition": definition or None,
        "data_domain": r.get("data_domain"),
        "department_owner": r.get("department_owner"),
        "source_system": r.get("source_system"),
        "sensitivity": r.get("sensitivity"),
        "has_definition": bool(definition),
    }

    citations = [{
        "label": schema["schema_name"] or args.schema_name,
        "deeplink": f"/explorer?schema={schema['schema_name'] or args.schema_name}",
    }]

    summary_bits = [f"{schema['total_tables']} tables"]
    if envs:
        summary_bits.append("/".join(envs))
    if schema["affiliate"]:
        summary_bits.append(schema["affiliate"])
    summary = f"{schema['schema_name']}: " + " · ".join(summary_bits)

    return ToolResult(
        ok=True,
        summary=summary,
        data={"schema": schema},
        citations=citations,
    )


GET_SCHEMA = Tool(
    name="app_get_schema",
    description=(
        "Get one logical schema's rollup: catalogs it lives in, environment "
        "flags (dev/qa/prod), table count, AI-generated definition, owner "
        "department, and source system. Use this after app_search_schemas "
        "or when the user names a specific schema like 'tell me about the "
        "maximo schema'."
    ),
    args_model=GetSchemaArgs,
    handler=_get_schema,
)
