"""
Bootstrap silver tables from CSVs already uploaded to the Volume.
Uses read_files() SQL to ingest and applies rule-based enrichment inline.
"""
import json
import os
import sys
import time
import requests

HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
WH = os.environ.get("DATABRICKS_WAREHOUSE_ID", "your-warehouse-id")
CATALOG = os.environ.get("BHE_CATALOG", "your_catalog")
SILVER = os.environ.get("BHE_SILVER_SCHEMA", "bhe_silver")

if not HOST.startswith("http"):
    HOST = f"https://{HOST}"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


_QUERY_TAGS = [
    {"key": "app", "value": "bhe_catalog"},
    {"key": "module", "value": "bootstrap"},
    {"key": "submodule", "value": "bootstrap_tables"},
]


def run_sql(stmt, *, submodule: str | None = None):
    url = f"{HOST}/api/2.0/sql/statements/"
    tags = list(_QUERY_TAGS)
    if submodule:
        tags = [t for t in tags if t["key"] != "submodule"] + [
            {"key": "submodule", "value": submodule[:128]}
        ]
    body = {"warehouse_id": WH, "statement": stmt, "wait_timeout": "50s",
            "query_tags": tags}
    r = requests.post(url, json=body, headers=HEADERS)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:300]}")
        return None
    result = r.json()
    while result.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        time.sleep(2)
        poll = requests.get(f"{url}{result['statement_id']}", headers=HEADERS)
        result = poll.json()
    state = result["status"]["state"]
    if state == "FAILED":
        print(f"  FAILED: {result['status'].get('error',{}).get('message','')}")
        return None
    rows = result.get("manifest", {}).get("total_row_count", 0)
    print(f"  OK ({rows} rows)")
    return result


print("=== Bootstrapping silver tables from Volume CSVs ===\n")

# --- silver_schemas ---
# DQ at the silver-build step: drop system/sample catalogs, drop
# information_schema/default schemas, and dedupe by (catalog, schema)
# so downstream jobs (enrichment, glossary, sankey) never have to
# repeat these filters or worry about duplicate keys in MERGEs.
print("1. Creating silver_schemas from read_files() (filtered + deduped)...")
run_sql(f"DROP TABLE IF EXISTS {CATALOG}.{SILVER}.silver_schemas",
        submodule="drop_silver_schemas")
run_sql(f"""
CREATE TABLE {CATALOG}.{SILVER}.silver_schemas AS
WITH raw AS (
  SELECT * FROM read_files(
    '/Volumes/{CATALOG}/bhe_raw/uploads/all_schemas_dbrk.csv',
    format => 'csv',
    header => true,
    multiLine => true,
    escape => '"'
  )
),
filtered AS (
  SELECT *
  FROM raw
  WHERE catalog_name IS NOT NULL
    AND schema_name IS NOT NULL
    AND NOT (
      lower(catalog_name) LIKE '__databricks%'
      OR lower(catalog_name) IN ('system', 'samples')
    )
    AND lower(schema_name) NOT IN ('information_schema', 'default')
),
counted AS (
  -- workspace_count is computed BEFORE dedup so it reflects the true
  -- number of distinct workspaces that saw this (catalog, schema) in
  -- the bronze extract.
  SELECT *,
         COUNT(*) OVER (PARTITION BY catalog_name, schema_name) AS workspace_count
  FROM filtered
),
deduped AS (
  SELECT *
  FROM counted
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY catalog_name, schema_name
    ORDER BY last_altered DESC NULLS LAST, created DESC NULLS LAST
  ) = 1
)
SELECT
  catalog_name,
  schema_name,
  COALESCE(schema_owner, '') AS schema_owner,
  COALESCE(comment, '') AS comment,
  COALESCE(created, '') AS created,
  COALESCE(last_altered, '') AS last_altered,
  COALESCE(workspace_url, '') AS workspace_url,
  workspace_count,
  CASE
    WHEN lower(catalog_name) RLIKE '_dev[0-9]{{2}}_' THEN 'DEV'
    WHEN lower(catalog_name) RLIKE '_qa[0-9]{{2}}_' THEN 'QA'
    WHEN lower(catalog_name) RLIKE '_prod[0-9]{{2}}_' THEN 'PROD'
    WHEN lower(catalog_name) LIKE '%_sbx_%' THEN 'SANDBOX'
    WHEN lower(catalog_name) LIKE '%oracle%' OR lower(catalog_name) LIKE '%sqlserver%' THEN 'EXTERNAL'
    ELSE 'UNKNOWN'
  END AS environment,
  CASE
    WHEN lower(catalog_name) LIKE '%tmp_landing%' THEN 'LANDING'
    WHEN lower(catalog_name) LIKE '%standardized%' THEN 'STANDARDIZED'
    WHEN lower(catalog_name) LIKE '%published%' THEN 'PUBLISHED'
    WHEN lower(catalog_name) LIKE '%discovery%' THEN 'DISCOVERY'
    WHEN lower(catalog_name) LIKE '%archived%' THEN 'ARCHIVED'
    WHEN lower(catalog_name) LIKE '%config%' THEN 'CONFIG'
    WHEN lower(catalog_name) LIKE '%analytics%' THEN 'ANALYTICS'
    WHEN lower(catalog_name) LIKE '%oracle%' OR lower(catalog_name) LIKE '%sqlserver%' THEN 'FEDERATED'
    ELSE 'OTHER'
  END AS zone,
  -- Program is intentionally NOT hardcoded here (closes circular dep E,
  -- 2026-05-05). The label is derived by populate_gold from the editable
  -- `bhe_gold.classification_rules` table and backfilled into
  -- silver_schemas at the end of populate_gold. Until populate_gold runs
  -- (or for catalogs unmatched by any rule), rows show 'Unknown'. Same
  -- rationale lives in the `ingest_schemas` MERGE in router.py.
  CAST('Unknown' AS STRING) AS program,
  CASE
    WHEN lower(schema_name) LIKE '%test%' OR lower(schema_name) LIKE '%poc%' THEN 'TEST'
    WHEN lower(schema_name) LIKE 'wflw_%' THEN 'MIGRATION'
    ELSE 'PRODUCTION'
  END AS classification,
  '' AS ai_definition,
  '' AS business_friendly_name,
  '' AS suggested_department,
  '' AS suggested_domain,
  '' AS data_sensitivity,
  false AS is_user_edited,
  '' AS user_edited_at
FROM deduped
""", submodule="create_silver_schemas")

# --- silver_tables ---
# DQ at the silver-build step: same exclusion list as silver_schemas
# plus dedupe by (catalog, schema, table). The MERGE in
# ai_enrich_tables.py was failing with
# DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW_IN_MERGE precisely
# because raw silver had ~50% duplicate keys. The right place to
# kill those duplicates is here, not in every downstream consumer.
print("\n2. Creating silver_tables from read_files() (filtered + deduped)...")
run_sql(f"DROP TABLE IF EXISTS {CATALOG}.{SILVER}.silver_tables",
        submodule="drop_silver_tables")
run_sql(f"""
CREATE TABLE {CATALOG}.{SILVER}.silver_tables AS
WITH raw AS (
  SELECT * FROM read_files(
    '/Volumes/{CATALOG}/bhe_raw/uploads/all_tables_dbrk.csv',
    format => 'csv',
    header => true,
    multiLine => true,
    escape => '"'
  )
),
filtered AS (
  SELECT *
  FROM raw
  WHERE table_catalog IS NOT NULL
    AND table_schema IS NOT NULL
    AND table_name IS NOT NULL
    AND NOT (
      lower(table_catalog) LIKE '__databricks%'
      OR lower(table_catalog) IN ('system', 'samples')
    )
    AND lower(table_schema) NOT IN ('information_schema', 'default')
),
counted AS (
  -- workspace_count is computed BEFORE dedup so it reflects the true
  -- number of distinct workspaces that saw this (catalog, schema, table)
  -- in the bronze extract.
  SELECT *,
         COUNT(*) OVER (
           PARTITION BY table_catalog, table_schema, table_name
         ) AS workspace_count
  FROM filtered
),
deduped AS (
  SELECT *
  FROM counted
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY table_catalog, table_schema, table_name
    ORDER BY last_altered DESC NULLS LAST, created DESC NULLS LAST
  ) = 1
)
SELECT
  table_catalog,
  table_schema,
  table_name,
  COALESCE(table_type, '') AS table_type,
  COALESCE(table_owner, '') AS table_owner,
  COALESCE(comment, '') AS comment,
  COALESCE(created, '') AS created,
  COALESCE(last_altered, '') AS last_altered,
  COALESCE(data_source_format, '') AS data_source_format,
  workspace_count,
  'PRODUCTION' AS classification,
  '' AS ai_definition,
  '' AS business_friendly_name,
  '' AS source_system,
  false AS is_user_edited,
  '' AS user_edited_at
FROM deduped
""", submodule="create_silver_tables")

# --- Sankey/company placeholder tables ---
print("\n3. Creating placeholder company & sankey tables...")
for tbl, ddl in [
    ("company_profile", """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, company_name STRING, industry STRING, sub_industry STRING,
        description STRING, headquarters STRING, key_business_units STRING,
        strategic_priorities STRING, regulatory_environment STRING,
        catalog_name STRING, logo_url STRING, primary_domain STRING,
        branding_user_edited BOOLEAN
    ) USING DELTA"""),
    ("departments", """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, department_name STRING, description STRING,
        key_functions STRING, data_needs STRING, company_name STRING,
        is_user_edited BOOLEAN
    ) USING DELTA"""),
    # `use_cases` carries three waves of columns:
    #   1. Core seed (chat / company_research / Edit Center): id..is_user_edited
    #   2. Delivery lifecycle (Value & Readiness): status, status_notes,
    #      status_updated_at, created_at — used by the inline-thread
    #      `insert_use_case_row` path and the status PATCH endpoint.
    #   3. Generation lens (PR 2 of UC redesign — `/use-cases` page):
    #      affiliate, lens, time_horizon, value_type, is_regulatory,
    #      required_canonicals, generated_at, generated_by.
    # All three are baked into bootstrap so a fresh deploy never has to run
    # `_ensure_use_case_status_columns()`'s defensive ALTER. The helper only
    # exists for already-deployed environments (e.g. UB_TEST) that pre-date
    # PR 2 and need to catch up in place.
    ("use_cases", """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, use_case_name STRING, description STRING,
        department STRING, category STRING, business_value STRING,
        estimated_value_usd DOUBLE, value_rationale STRING,
        data_requirements STRING, priority STRING, company_name STRING,
        is_user_edited BOOLEAN,
        status STRING COMMENT 'Delivery lifecycle: not_started|in_progress|delivered|on_hold',
        status_notes STRING,
        status_updated_at TIMESTAMP,
        created_at TIMESTAMP,
        affiliate STRING,
        lens STRING COMMENT 'ready|gap|manual',
        time_horizon STRING COMMENT 'quick_win|strategic|NULL',
        value_type STRING COMMENT 'cost|revenue|risk|mixed|NULL',
        is_regulatory BOOLEAN,
        required_canonicals STRING COMMENT 'JSON array of canonical source names',
        generated_at TIMESTAMP,
        generated_by STRING COMMENT 'llm endpoint id, or "chat" for free-form'
    ) USING DELTA"""),
    ("use_case_entities", """CREATE TABLE IF NOT EXISTS {fqn} (
        entity_id STRING, use_case_id STRING, use_case_name STRING,
        entity_name STRING, entity_type STRING,
        description STRING, is_matched BOOLEAN, matched_source STRING,
        company_name STRING, created_at STRING
    ) USING DELTA"""),
    ("job_progress", """CREATE TABLE IF NOT EXISTS {fqn} (
        run_id STRING, step STRING, step_index INT, total_steps INT,
        item_name STRING, parent_item STRING, detail STRING,
        created_at TIMESTAMP
    ) USING DELTA"""),
    ("sankey_mappings", """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, source_system STRING, source_category STRING,
        use_case STRING, department STRING, entity_name STRING,
        relevance STRING,
        company_name STRING, is_user_edited BOOLEAN, created_at STRING
    ) USING DELTA"""),
    # Knowledge articles: tree of folders + articles. Bodies live in Volume
    # (/Volumes/{catalog}/{raw}/uploads/knowledge/{node_id}/<filename>); this
    # table only holds metadata. node_id is the stable cross-app reference key.
    ("knowledge_nodes", """CREATE TABLE IF NOT EXISTS {fqn} (
        node_id STRING,
        parent_id STRING,
        node_type STRING,            -- 'folder' | 'article'
        title STRING,
        summary STRING,
        content_format STRING,       -- 'markdown' | 'pdf' | 'docx' | ''
        volume_path STRING,          -- file path in UC Volume (articles only)
        original_filename STRING,
        mime_type STRING,
        file_size_bytes BIGINT,
        tags STRING,                 -- comma-separated for now
        sort_order INT,
        version INT,
        created_by STRING,
        updated_by STRING,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        is_deleted BOOLEAN
    ) USING DELTA"""),
    # Polymorphic associations from articles to other catalog entities.
    # Phase-2 UI reads this to show "related articles" on table/artifact pages.
    ("knowledge_links", """CREATE TABLE IF NOT EXISTS {fqn} (
        link_id STRING,
        node_id STRING,
        target_type STRING,          -- 'catalog'|'schema'|'table'|'artifact'|'use_case'|'department'|'page'
        target_key STRING,           -- natural key for the target (e.g. 'cat.sch.tbl')
        created_by STRING,
        created_at TIMESTAMP
    ) USING DELTA"""),
    # Chatbot (Track A) — per-user conversation history.
    # See docs/plans/chatbot-track-a.md.
    # `user_key` is the stable identifier we resolve from the Databricks Apps
    # forwarded headers (preferred-username when available, else email). It
    # lower-cases the value so equality joins are predictable. Every read in
    # the chat router MUST filter by user_key for isolation.
    ("chat_conversations", """CREATE TABLE IF NOT EXISTS {fqn} (
        conversation_id STRING,
        user_key STRING,             -- lower(preferred_username) | lower(email)
        title STRING,                -- LLM- or first-message-derived
        genie_conversation_id STRING,-- pinned on first genie_ask call so the
                                     -- Genie space's stateful follow-up context
                                     -- stays scoped to our chat thread (PR #2)
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        last_message_at TIMESTAMP,
        message_count INT,
        is_deleted BOOLEAN
    ) USING DELTA"""),
    # `parts` holds a JSON array (stored as STRING) of structured message parts
    # so future phases can add tool_call / tool_result / chart parts without
    # another schema migration. For Phase A1 the shape is simply
    # [{"type":"text","text":"..."}].
    ("chat_messages", """CREATE TABLE IF NOT EXISTS {fqn} (
        message_id STRING,
        conversation_id STRING,
        user_key STRING,             -- denormalized for fast user-scoped reads
        role STRING,                 -- 'user' | 'assistant' | 'system' | 'tool'
        content STRING,              -- plain-text body for the dominant text part
        parts STRING,                -- JSON array of structured parts (see above)
        model STRING,                -- LLM endpoint used for assistant messages
        prompt_tokens INT,
        completion_tokens INT,
        latency_ms INT,
        finish_reason STRING,        -- 'stop' | 'length' | 'tool_calls' | 'error' | ''
        error STRING,                -- non-empty when assistant call failed
        created_at TIMESTAMP
    ) USING DELTA"""),
    # Chatbot Phase A2 — single-use confirmation tokens for chat-driven writes.
    # The propose_* tools issue a token; the confirm endpoint validates it (not
    # consumed, not expired, payload hash matches) and then performs the write.
    # Storing the full payload server-side means the FE can't tamper with it
    # between propose and confirm. `consumed_at` is set atomically in a single
    # UPDATE...WHERE consumed_at IS NULL so two concurrent confirms can't
    # double-write (Spark Delta MERGE would be cleaner; we accept the small
    # race window because writes are user-driven, not high-throughput).
    ("chat_confirm_tokens", """CREATE TABLE IF NOT EXISTS {fqn} (
        token STRING,                -- random opaque string (32 hex chars)
        conversation_id STRING,      -- the chat thread that issued it
        user_key STRING,             -- the user the token is bound to
        intent STRING,               -- e.g. 'updateUseCaseStatus' (matches operation_id)
        target_id STRING,            -- the primary entity id, for audit + UI
        payload STRING,              -- JSON of the full payload to write
        created_at TIMESTAMP,
        expires_at TIMESTAMP,        -- TTL ~10min from issue
        consumed_at TIMESTAMP        -- NULL until used; single-use enforcement
    ) USING DELTA"""),
]:
    fqn = f"{CATALOG}.{SILVER}.{tbl}"
    print(f"  {tbl}...")
    run_sql(ddl.format(fqn=fqn))


# --- Metadata comments (single source of truth for Genie + UC docs) ---
# Applied serially after CREATE because:
#   1. silver_schemas/silver_tables are built via CTAS so they can't carry
#      inline column comments; ALTER COLUMN ... COMMENT must run separately.
#   2. Parallel ALTER COLUMN on the same Delta table races on metadata
#      versions (DELTA_METADATA_CHANGED), so we keep this serial.
# Idempotent: rerunning just refreshes the comments.
print("\n4. Applying table & column comments (Genie-readability)...")

_TABLE_COMMENTS: dict[str, str] = {
    "silver_tables": (
        "One row per Unity Catalog table across all BHE workspaces. "
        "Table-grain inventory with rule-based classification and AI-enriched "
        "business metadata. Use for any question about specific tables, table "
        "counts, or table-level attributes."
    ),
    "use_cases": (
        "Catalog of business use cases that consume or could consume data assets. "
        "Each row is a discrete data product opportunity (e.g., Wildfire Risk Modeling) "
        "with department ownership, business value, and data requirements. "
        "Affiliate applicability lives in bhe_gold.use_case_affiliates; required "
        "source systems live in bhe_gold.use_case_source_requirements."
    ),
    "departments": (
        "Business departments at BHE that own use cases and consume data. "
        "Lookup dimension joined from bhe_silver.use_cases.department."
    ),
}

_COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "silver_tables": {
        "table_catalog": "Unity Catalog name (e.g., my_program_prod_published).",
        "table_schema": "Schema name within the catalog.",
        "table_name": "Table or view name.",
        "table_type": "MANAGED, EXTERNAL, VIEW, MATERIALIZED_VIEW, etc. (from information_schema).",
        "table_owner": "Workspace user or service principal that owns this table in Unity Catalog.",
        "comment": "Original Unity Catalog table comment, if set by the data owner. Often empty.",
        "created": "Table creation timestamp from Unity Catalog metadata.",
        "last_altered": "Most recent DDL alteration timestamp from Unity Catalog metadata.",
        "data_source_format": "Storage format: DELTA, PARQUET, CSV, JSON, etc.",
        "classification": "PRODUCTION (default). Derived classification used by enrichment filters.",
        "ai_definition": "AI-generated business description of what this table contains. Empty until AI table enrichment runs.",
        "business_friendly_name": "AI-generated human-friendly display name (e.g., Substation Equipment Master).",
        "source_system": "Raw source-system label from AI table enrichment. May be free-text. Use source_system_canonical for the normalized value.",
        "is_user_edited": "TRUE if a human edited this row in the UI. AI re-enrichment never overwrites user-edited rows.",
        "user_edited_at": "ISO timestamp of the most recent user edit.",
    },
    "use_cases": {
        "id": "Stable use-case identifier (e.g., uc_a1b2c3d4). Primary key. Referenced by bhe_gold.use_case_affiliates and bhe_gold.use_case_source_requirements.",
        "use_case_name": "Short human-readable use case title (e.g., Wildfire Risk Modeling).",
        "description": "Long-form description of the business problem the use case solves.",
        "department": "Owning department name. Joins to bhe_silver.departments.department_name.",
        "category": "Free-text category label (e.g., Risk, Operations, Customer). Not strictly governed.",
        "business_value": "Free-text statement of why this use case matters to the business.",
        "data_requirements": "JSON array (stored as STRING) of {source, fields[], grain, frequency} describing the data this use case needs. Source of truth that drives bhe_gold.use_case_source_requirements via LLM mapping.",
        "priority": "High | Medium | Low. Analyst-assigned business priority.",
        "company_name": "Owning company within BHE (e.g., PacifiCorp, NV Energy). May be empty when the use case spans multiple companies. See bhe_gold.use_case_affiliates for full applicability.",
        "is_user_edited": "TRUE if a human edited this row in the UI. Survives reseeds and AI regeneration.",
        "created_at": "ISO timestamp of when the use case was created.",
        "estimated_value_usd": "Estimated annual business value in USD.",
        "value_rationale": "Free-text justification for estimated_value_usd.",
    },
    "departments": {
        "id": "Stable department identifier.",
        "department_name": "Canonical department name (e.g., Customer Service, Trading & Risk Management). Joins to bhe_silver.use_cases.department.",
        "description": "Short description of what this department does.",
        "key_functions": "Comma-separated list of major functions this department performs.",
        "data_needs": "Free-text summary of the kinds of data this department typically needs.",
        "company_name": "Owning company name within BHE.",
        "is_user_edited": "TRUE if a human edited this row in the UI.",
        "created_at": "ISO timestamp of department creation.",
    },
}


def _sql_quote(text: str) -> str:
    return text.replace("'", "''")


for table, comment in _TABLE_COMMENTS.items():
    fqn = f"{CATALOG}.{SILVER}.{table}"
    print(f"  COMMENT ON TABLE {table}...")
    run_sql(
        f"COMMENT ON TABLE {fqn} IS '{_sql_quote(comment)}'",
        submodule=f"comment_table_{table}",
    )

for table, cols in _COLUMN_COMMENTS.items():
    fqn = f"{CATALOG}.{SILVER}.{table}"
    print(f"  ALTER COLUMN comments on {table} ({len(cols)} cols)...")
    for col, comment in cols.items():
        run_sql(
            f"ALTER TABLE {fqn} ALTER COLUMN {col} COMMENT '{_sql_quote(comment)}'",
            submodule=f"comment_col_{table}_{col}",
        )

print("\n=== Done! Refresh the app to see data. ===")
