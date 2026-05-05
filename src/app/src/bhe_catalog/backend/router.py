"""
Data Catalog - API Router
All endpoints for catalog browsing, Sankey data, company research, and job management.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from . import app_config, setup_genie
from .chat import chat_router
from .confirm import confirm_router
from .core import Dependencies, create_router
from .core._defaults import DatabricksClient
from .core._headers import HeadersDependency
from .db import (
    _execute_sql_api,
    execute_query,
    execute_update,
    fqn,
    get_catalog,
    get_gold_schema,
    get_raw_schema,
    get_silver_schema,
)
from .models import (
    AffiliateUpdateIn,
    AffiliateUpsertIn,
    ArtifactFiltersOut,
    ArtifactOut,
    ArtifactStatsOut,
    ArtifactUpdateIn,
    ARTIFACT_ACCESS_LEVELS,
    ARTIFACT_REFRESH_FREQUENCIES,
    ARTIFACT_STATUSES,
    ARTIFACT_TYPES,
    CanonicalSourceUpdateIn,
    CanonicalSourceUpsertIn,
    CatalogStatsOut,
    BrandingOut,
    BrandingUpdateIn,
    CompanyProfileOut,
    CompanyResearchIn,
    DepartmentOut,
    DepartmentUpdateIn,
    JobStatusOut,
    JobTriggerOut,
    KNOWLEDGE_CONTENT_FORMATS,
    KNOWLEDGE_LINK_TARGETS,
    KnowledgeArticleContentOut,
    KnowledgeArticleCreateIn,
    KnowledgeFolderCreateIn,
    KnowledgeLinkCreateIn,
    KnowledgeLinkOut,
    KnowledgeNodeOut,
    KnowledgeNodeUpdateIn,
    ProgramAffiliateMapUpdateIn,
    ProgramAffiliateMapUpsertIn,
    ProposalGenerateIn,
    SankeyDataOut,
    SankeyLinkOut,
    SankeyMappingIn,
    SankeyMappingUpdateIn,
    SankeyNodeOut,
    SchemaOut,
    SchemaUpdateIn,
    TableOut,
    TableUpdateIn,
    TaxonomyUpdateIn,
    TAXONOMY_ALLOWED_VALUES,
    TAXONOMY_DIMENSIONS,
    USE_CASE_STATUSES,
    UseCaseAffiliateUpsertIn,
    UseCaseCreateIn,
    UseCaseEntityUpsertIn,
    UseCaseOut,
    UseCaseSourceRequirementUpsertIn,
    UseCaseStatusIn,
    UseCaseUpdateIn,
    VersionOut,
)

logger = logging.getLogger(__name__)

router = create_router()

# Chatbot (Track A) endpoints. Kept in their own module so the chat surface
# can grow tools/streaming logic without bloating router.py further.
router.include_router(chat_router)
# Phase A2 — propose/confirm token endpoint. Lives next to chat_router but
# in its own module because the write executors here import from router.py
# and we'd otherwise create a circular dependency at module-load time.
router.include_router(confirm_router)


def _get_company_name() -> str:
    """Read the configured company name from company_profile, or return a default."""
    try:
        rows = execute_query(
            f"SELECT company_name FROM {fqn(get_silver_schema(), 'company_profile')} LIMIT 1"
        )
        if rows and rows[0].get("company_name"):
            return rows[0]["company_name"]
    except Exception:
        pass
    return os.environ.get("COMPANY_NAME", "the company")


# Branding columns are added on-demand to silver.company_profile so existing
# deployments self-migrate without a separate bootstrap step. Cached in a flag
# so we don't re-issue DESCRIBE on every branding fetch.
_BRANDING_COLUMNS_ENSURED = False
_BRANDING_REQUIRED_COLS = {
    "catalog_name": "STRING",
    "logo_url": "STRING",
    "primary_domain": "STRING",
    "branding_user_edited": "BOOLEAN",
}


def _ensure_branding_columns() -> str:
    """Ensure silver.company_profile has the branding columns. Returns the FQN.

    Databricks SQL only supports ``IF NOT EXISTS`` with the singular
    ``ADD COLUMN`` form, not the plural ``ADD COLUMNS (...)``. Rather than
    issuing N separate ALTERs, we DESCRIBE the table once and only ALTER
    when something is actually missing.
    """
    global _BRANDING_COLUMNS_ENSURED
    table = fqn(get_silver_schema(), "company_profile")
    if _BRANDING_COLUMNS_ENSURED:
        return table
    try:
        existing_rows = execute_query(f"DESCRIBE TABLE {table}")
        existing = {
            (r.get("col_name") or "").lower()
            for r in existing_rows
            if r.get("col_name") and not r["col_name"].startswith("#")
        }
        missing = [(c, t) for c, t in _BRANDING_REQUIRED_COLS.items() if c not in existing]
        if missing:
            cols_sql = ", ".join(f"{c} {t}" for c, t in missing)
            execute_query(f"ALTER TABLE {table} ADD COLUMNS ({cols_sql})")
            logger.info(f"Added branding columns to {table}: {[c for c, _ in missing]}")
        _BRANDING_COLUMNS_ENSURED = True
    except Exception as e:
        logger.warning(f"Could not ensure branding columns on {table}: {e}")
    return table


SANKEY_COLORS = {
    "source": "#4ECDC4",
    "entity": "#8b5cf6",
    "use_case": "#A29BFE",
    "department": "#FF6B6B",
}


# ---------------------------------------------------------------------------
# Health & User
# ---------------------------------------------------------------------------


@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@router.get("/current-user", operation_id="currentUser")
def me(headers: HeadersDependency):
    """Return the *end user* hitting the app -- not the app's service principal.

    Resolution order:
      1. Databricks Apps: read `X-Forwarded-Email` / `X-Forwarded-Preferred-Username`
         / `X-Forwarded-User` (set by the platform after SSO). If `X-Forwarded-Access-Token`
         is also present, enrich with a SCIM /Me call *as the user* (OBO) so we
         pick up display_name, given/family name and the full email list.
      2. Local dev: no forwarded headers -> fall back to the env-configured
         token (DATABRICKS_TOKEN from start_local.sh) and SCIM /Me, which
         resolves to whoever owns the PAT (typically the developer).

    The previous implementation always called SCIM as the *app SP* via the M2M
    OAuth creds, which is why the sidebar was showing the SP name + UUID.
    """
    import os

    def _normalize(raw: dict) -> dict:
        """Normalize SCIM camelCase fields to snake_case for the frontend."""
        name_obj = raw.get("name", {}) or {}
        return {
            "id": raw.get("id", "local"),
            "user_name": raw.get("userName", raw.get("user_name", "")),
            "display_name": raw.get("displayName", raw.get("display_name", "")),
            "active": raw.get("active", True),
            "name": {
                "given_name": name_obj.get("givenName", name_obj.get("given_name", "")),
                "family_name": name_obj.get("familyName", name_obj.get("family_name", "")),
            },
            "emails": raw.get("emails", []),
            "roles": raw.get("roles", []),
            "groups": raw.get("groups", []),
            "entitlements": raw.get("entitlements", []),
            "external_id": raw.get("externalId", raw.get("external_id", "")),
        }

    forwarded_email = headers.user_email or ""
    forwarded_username = headers.user_name or ""
    forwarded_user_id = headers.user_id or ""

    if forwarded_email or forwarded_username or forwarded_user_id:
        # Running behind Databricks Apps -> the platform already told us who
        # the human is. Try to enrich with SCIM via the OBO token so we get a
        # proper display_name; fall back to header-only data if SCIM is
        # unavailable for any reason (token missing, SCIM 5xx, etc.).
        if headers.token:
            try:
                user_client = DatabricksClient(token=headers.token.get_secret_value())
                if user_client.host:
                    scim = user_client.current_user.me()
                    normalized = _normalize(scim)
                    if not normalized["user_name"]:
                        normalized["user_name"] = forwarded_email or forwarded_username
                    return normalized
            except Exception as e:
                logger.warning(f"OBO SCIM /Me call failed, using forwarded headers: {e}")

        # No OBO token, or SCIM failed: synthesize a response from headers.
        primary_email = forwarded_email or forwarded_username
        return _normalize({
            "id": forwarded_user_id or "forwarded",
            "userName": primary_email or forwarded_username,
            "displayName": primary_email or forwarded_username,
            "emails": [{"value": forwarded_email, "primary": True}] if forwarded_email else [],
        })

    # No forwarded headers -> truly local dev. Use the env PAT.
    try:
        client = DatabricksClient()
        if client.host and client.token:
            return _normalize(client.current_user.me())
    except Exception as e:
        logger.warning(f"Local SCIM /Me call failed: {e}")
    return _normalize({
        "userName": os.environ.get("USER", "local-dev"),
        "displayName": "Local Developer",
    })


# ---------------------------------------------------------------------------
# Setup / Onboarding (run once per fresh install)
#
# These endpoints power the guided "Company Setup" wizard. They check
# prerequisites in order and let the user provision the database objects
# from the UI without ever shelling into a notebook or running the local
# bootstrap scripts.
# ---------------------------------------------------------------------------

# Silver "skeleton" tables created at bootstrap time. silver_schemas /
# silver_tables are intentionally NOT in this list -- those are populated by
# the ingest endpoints (CTAS from Volume CSVs) and don't have a fixed schema
# we want to commit to here.
_SETUP_SILVER_DDL: dict[str, str] = {
    # Schema-grain catalog inventory loaded from the schema-extractor CSV.
    # Pre-creating here lets `ingest_schemas` MERGE into the table instead of
    # DROP+CTAS, which preserves ALTER-added columns and user-edited rows
    # across re-ingests. See onboarding-bug-backlog "Circular dep B".
    "silver_schemas": """CREATE TABLE IF NOT EXISTS {fqn} (
        catalog_name STRING NOT NULL,
        schema_name STRING NOT NULL,
        schema_owner STRING,
        comment STRING,
        created STRING,
        last_altered STRING,
        workspace_url STRING,
        environment STRING,
        zone STRING,
        program STRING,
        classification STRING,
        ai_definition STRING,
        business_friendly_name STRING,
        suggested_department STRING,
        suggested_domain STRING,
        data_sensitivity STRING,
        is_user_edited BOOLEAN,
        user_edited_at STRING
    ) USING DELTA COMMENT 'Silver: schema-grain catalog inventory ingested from extractor CSV'""",
    # Table-grain inventory. `source_system` / `source_system_canonical` are
    # included from day-one so the normalize_source_systems job's ALTER+MERGE
    # is a no-op on a fresh deploy and the read paths (Schema Explorer,
    # Source Systems) work pre-normalize. Closes B-017.
    "silver_tables": """CREATE TABLE IF NOT EXISTS {fqn} (
        table_catalog STRING NOT NULL,
        table_schema STRING NOT NULL,
        table_name STRING NOT NULL,
        table_type STRING,
        table_owner STRING,
        comment STRING,
        created STRING,
        last_altered STRING,
        data_source_format STRING,
        classification STRING,
        ai_definition STRING,
        business_friendly_name STRING,
        is_user_edited BOOLEAN,
        user_edited_at STRING,
        source_system STRING COMMENT 'Raw value populated by normalize_source_systems job',
        source_system_canonical STRING COMMENT 'Resolved canonical populated by normalize_source_systems job'
    ) USING DELTA COMMENT 'Silver: table-grain catalog inventory ingested from extractor CSV'""",
    "company_profile": """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, company_name STRING, industry STRING, sub_industry STRING,
        description STRING, headquarters STRING, key_business_units STRING,
        strategic_priorities STRING, regulatory_environment STRING,
        catalog_name STRING, logo_url STRING, primary_domain STRING,
        branding_user_edited BOOLEAN
    ) USING DELTA""",
    "departments": """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, department_name STRING, description STRING,
        key_functions STRING, data_needs STRING, company_name STRING,
        is_user_edited BOOLEAN
    ) USING DELTA""",
    "use_cases": """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, use_case_name STRING, description STRING,
        department STRING, category STRING, business_value STRING,
        estimated_value_usd DOUBLE, value_rationale STRING,
        data_requirements STRING, priority STRING, company_name STRING,
        is_user_edited BOOLEAN
    ) USING DELTA""",
    "use_case_entities": """CREATE TABLE IF NOT EXISTS {fqn} (
        entity_id STRING, use_case_id STRING, use_case_name STRING,
        entity_name STRING, entity_type STRING,
        description STRING, is_matched BOOLEAN, matched_source STRING,
        company_name STRING, created_at STRING
    ) USING DELTA""",
    "job_progress": """CREATE TABLE IF NOT EXISTS {fqn} (
        run_id STRING, step STRING, step_index INT, total_steps INT,
        item_name STRING, parent_item STRING, detail STRING,
        created_at TIMESTAMP
    ) USING DELTA""",
    "sankey_mappings": """CREATE TABLE IF NOT EXISTS {fqn} (
        id STRING, source_system STRING, source_category STRING,
        use_case STRING, department STRING, entity_name STRING,
        relevance STRING,
        company_name STRING, is_user_edited BOOLEAN, created_at STRING
    ) USING DELTA""",
    "knowledge_nodes": """CREATE TABLE IF NOT EXISTS {fqn} (
        node_id STRING, parent_id STRING, node_type STRING, title STRING,
        summary STRING, content_format STRING, volume_path STRING,
        original_filename STRING, mime_type STRING, file_size_bytes BIGINT,
        tags STRING, sort_order INT, version INT,
        created_by STRING, updated_by STRING,
        created_at TIMESTAMP, updated_at TIMESTAMP, is_deleted BOOLEAN
    ) USING DELTA""",
    "knowledge_links": """CREATE TABLE IF NOT EXISTS {fqn} (
        link_id STRING, node_id STRING, target_type STRING, target_key STRING,
        created_by STRING, created_at TIMESTAMP
    ) USING DELTA""",
    "chat_conversations": """CREATE TABLE IF NOT EXISTS {fqn} (
        conversation_id STRING, user_key STRING, title STRING,
        genie_conversation_id STRING,
        created_at TIMESTAMP, updated_at TIMESTAMP, last_message_at TIMESTAMP,
        message_count INT, is_deleted BOOLEAN
    ) USING DELTA""",
    "chat_messages": """CREATE TABLE IF NOT EXISTS {fqn} (
        message_id STRING, conversation_id STRING, user_key STRING,
        role STRING, content STRING, parts STRING, model STRING,
        prompt_tokens INT, completion_tokens INT, latency_ms INT,
        finish_reason STRING, error STRING, created_at TIMESTAMP
    ) USING DELTA""",
    "chat_confirm_tokens": """CREATE TABLE IF NOT EXISTS {fqn} (
        token STRING, conversation_id STRING, user_key STRING,
        intent STRING, target_id STRING, payload STRING,
        created_at TIMESTAMP, expires_at TIMESTAMP, consumed_at TIMESTAMP
    ) USING DELTA""",
}

_SETUP_GOLD_DDL: dict[str, str] = {
    "app_config": """CREATE TABLE IF NOT EXISTS {fqn} (
        key STRING NOT NULL,
        value STRING,
        updated_at TIMESTAMP,
        updated_by STRING
    ) USING DELTA COMMENT 'Runtime key/value config written by the in-app Setup Wizard (e.g. genie_space_id)'""",
    "schema_inventory": """CREATE TABLE IF NOT EXISTS {fqn} (
        schema_key STRING, workspace_id STRING, workspace_url STRING,
        workspace_name STRING, catalog_name STRING, schema_name STRING,
        schema_owner STRING, program STRING, affiliate STRING,
        environment STRING, zone STRING, classification STRING,
        table_count INT, view_count INT,
        definition STRING, business_name STRING, source_system STRING,
        data_domain STRING, department_owner STRING, sensitivity STRING,
        data_quality_tier STRING,
        created STRING, last_altered STRING, enriched_at TIMESTAMP,
        is_user_edited BOOLEAN, comment STRING
    ) USING DELTA COMMENT 'Gold layer: enriched schema inventory at schema grain'""",
    "source_summary": """CREATE TABLE IF NOT EXISTS {fqn} (
        program STRING, affiliate STRING,
        dev_schemas INT, qa_schemas INT, prod_schemas INT,
        dev_tables INT, qa_tables INT, prod_tables INT, total_tables INT,
        consistency_score FLOAT,
        schemas_only_dev STRING, schemas_only_qa STRING, schemas_only_prod STRING,
        updated_at TIMESTAMP
    ) USING DELTA""",
    "workspace_summary": """CREATE TABLE IF NOT EXISTS {fqn} (
        workspace_id STRING, workspace_url STRING, workspace_name STRING,
        affiliates STRING, programs STRING, environments STRING,
        catalog_count INT, schema_count INT, table_count INT,
        updated_at TIMESTAMP
    ) USING DELTA""",
    "env_consistency": """CREATE TABLE IF NOT EXISTS {fqn} (
        program STRING, affiliate STRING, schema_name STRING,
        in_dev BOOLEAN, in_qa BOOLEAN, in_prod BOOLEAN,
        dev_tables INT, qa_tables INT, prod_tables INT,
        issue_type STRING, updated_at TIMESTAMP
    ) USING DELTA""",
    "glossary_system_domain": """CREATE TABLE IF NOT EXISTS {fqn} (
        affiliate STRING, source_system STRING, data_domain STRING,
        catalog_schemas ARRAY<STRUCT<catalog: STRING, schema: STRING, table_count: INT>>,
        schema_count INT, table_count INT,
        programs ARRAY<STRING>, zones ARRAY<STRING>, environments ARRAY<STRING>,
        sample_table_names ARRAY<STRING>, updated_at TIMESTAMP
    ) USING DELTA""",
    # User-editable parsing rules: catalog/schema names -> programs, environments,
    # zones, ignore lists. Read by `_load_rules()` during populate_gold; edited
    # via the /rules page. The wizard seeds universal ignore patterns into this
    # table after creation (see `_seed_classification_rules_if_empty()`).
    "classification_rules": """CREATE TABLE IF NOT EXISTS {fqn} (
        rule_id STRING NOT NULL,
        category STRING NOT NULL COMMENT 'program | zone | environment | ignore_catalog | ignore_schema | federated_source',
        pattern STRING NOT NULL COMMENT 'glob pattern matched against catalog/schema name',
        label STRING COMMENT 'canonical name produced when pattern matches (empty for ignore rules)',
        description STRING,
        metadata STRING COMMENT 'JSON blob (e.g. affiliate name for program rules)',
        is_active BOOLEAN,
        display_order INT,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: parsing rules driving schema_inventory classification'""",
    # AI-classified taxonomy dimensions per schema. Written by Generate
    # Taxonomy job; read by Schema Explorer / Analytics. SCD Type 2 via
    # effective_from / effective_to.
    "schema_taxonomy": """CREATE TABLE IF NOT EXISTS {fqn} (
        taxonomy_id STRING,
        schema_key STRING NOT NULL,
        dimension STRING NOT NULL,
        value STRING,
        source STRING,
        confidence FLOAT,
        ai_reasoning STRING,
        effective_from TIMESTAMP,
        effective_to TIMESTAMP,
        created_by STRING,
        created_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: AI taxonomy classification across 8 dimensions per schema'""",
    # Customer-editable affiliates (operating subsidiaries). Seeded by
    # `bhe_build_value_model` job from src/data/affiliates_seed.csv; the job
    # also has a CREATE TABLE IF NOT EXISTS, so pre-creating here is a no-op
    # for that job but unblocks Edit Center / Value & Readiness pages on a
    # fresh deploy.
    "affiliates": """CREATE TABLE IF NOT EXISTS {fqn} (
        affiliate_name STRING NOT NULL COMMENT 'Canonical affiliate name (primary key)',
        affiliate_code STRING COMMENT 'Short code (PAC, NVE, MEC, ...)',
        business_type STRING COMMENT 'electric_utility | electric_gas_utility | renewables_developer | natural_gas_pipeline | corporate | ...',
        region STRING COMMENT 'Geographic footprint',
        description STRING,
        is_active BOOLEAN,
        is_user_edited BOOLEAN COMMENT 'true = manual edit; never overwritten by job',
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: operating affiliates (customer-editable)'""",
    "program_affiliate_map": """CREATE TABLE IF NOT EXISTS {fqn} (
        program STRING NOT NULL COMMENT 'silver_schemas.program value',
        affiliate_name STRING NOT NULL COMMENT 'FK -> affiliates.affiliate_name',
        affiliation_strength STRING COMMENT 'primary | secondary',
        notes STRING,
        is_user_edited BOOLEAN,
        updated_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: bridge program -> affiliate (M:N)'""",
    "use_case_source_requirements": """CREATE TABLE IF NOT EXISTS {fqn} (
        use_case_id STRING NOT NULL COMMENT 'FK -> use_cases.id',
        required_canonical STRING NOT NULL COMMENT 'FK -> source_system_canonical.canonical, or "Unmapped"',
        necessity STRING COMMENT 'must_have | nice_to_have',
        data_need_excerpt STRING COMMENT 'Source phrase from data_requirements that triggered the mapping',
        confidence STRING COMMENT 'high | med | low',
        mapped_by STRING COMMENT 'llm | manual',
        is_user_edited BOOLEAN,
        mapped_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: use case -> required source systems (LLM mapped, analyst-overridable)'""",
    "use_case_affiliates": """CREATE TABLE IF NOT EXISTS {fqn} (
        use_case_id STRING NOT NULL COMMENT 'FK -> use_cases.id',
        affiliate_name STRING NOT NULL COMMENT 'FK -> affiliates.affiliate_name',
        applicability STRING COMMENT 'primary | secondary',
        rationale STRING,
        mapped_by STRING COMMENT 'llm | manual',
        is_user_edited BOOLEAN,
        mapped_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: use case -> applicable affiliates (LLM mapped, analyst-overridable)'""",
    "source_system_canonical": """CREATE TABLE IF NOT EXISTS {fqn} (
        canonical STRING NOT NULL COMMENT 'Canonical source-system name (primary key)',
        category STRING COMMENT 'Category bucket (ERP, CIS, Historian, ...)',
        description STRING,
        is_active BOOLEAN,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    ) USING DELTA COMMENT 'Gold layer: customer-editable canonical source systems'""",
    "source_system_aliases": """CREATE TABLE IF NOT EXISTS {fqn} (
        raw STRING NOT NULL COMMENT 'Source-system value as it appears in silver_tables (primary key)',
        raw_normalized STRING COMMENT 'lower(trim(raw)) for case-insensitive matching',
        canonical STRING COMMENT 'Resolved canonical name (FK to source_system_canonical)',
        mapped_by STRING COMMENT 'seed | exact | normalized | alias_seed | llm | manual | fallback_other',
        confidence STRING COMMENT 'high | med | low | NULL',
        mapped_at TIMESTAMP,
        is_user_edited BOOLEAN
    ) USING DELTA COMMENT 'Gold layer: persistent raw->canonical source-system mapping'""",
}


# Universal ignore-rule seeds for `classification_rules`. These mirror the
# hardcoded filters in `bootstrap_tables.py` and are safe across all customers
# (Databricks platform internals + INFORMATION_SCHEMA). Customer-specific
# program / zone / environment rules are NOT seeded — those are added via the
# /rules UI as the user discovers their conventions.
_CLASSIFICATION_RULES_SEED: list[dict] = [
    {"rule_id": "ig_dbx", "category": "ignore_catalog", "pattern": "__databricks_internal_*", "label": "", "description": "Databricks platform internals", "display_order": 10},
    {"rule_id": "ig_sys", "category": "ignore_catalog", "pattern": "system",                  "label": "", "description": "Databricks system catalog",    "display_order": 11},
    {"rule_id": "ig_smp", "category": "ignore_catalog", "pattern": "samples",                 "label": "", "description": "Databricks sample data",       "display_order": 12},
    {"rule_id": "ig_inf", "category": "ignore_schema",  "pattern": "information_schema",      "label": "", "description": "INFORMATION_SCHEMA metadata",  "display_order": 10},
    {"rule_id": "ig_def", "category": "ignore_schema",  "pattern": "default",                 "label": "", "description": "Empty default schema",         "display_order": 11},
]


def _resolve_app_identity() -> dict:
    """Best-effort identity of whoever the app is authenticating *as*.

    On Databricks Apps this is the app's service principal. Locally it's the
    user that owns DATABRICKS_TOKEN (or .databrickscfg profile). Returned
    fields are the inputs the UI needs to render `GRANT ... TO \\`<who>\\``.
    """
    try:
        client = DatabricksClient()
        me = client.current_user.me()
        # SDK returns either a `User` dataclass or a dict depending on call site
        as_dict = me if isinstance(me, dict) else getattr(me, "as_dict", lambda: {})()
        user_name = as_dict.get("userName") or as_dict.get("user_name") or ""
        display_name = as_dict.get("displayName") or as_dict.get("display_name") or user_name
        # SP user_names look like UUIDs; humans look like emails
        is_sp = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", (user_name or "").lower()))
        client_id = os.environ.get("DATABRICKS_CLIENT_ID", "")
        return {
            "type": "service_principal" if (is_sp or client_id) else "user",
            "user_name": user_name,
            "display_name": display_name,
            "client_id": client_id,
            "host": client.host,
        }
    except Exception as e:
        logger.warning(f"Could not resolve app identity: {e}")
        return {
            "type": "unknown",
            "user_name": "",
            "display_name": "",
            "client_id": os.environ.get("DATABRICKS_CLIENT_ID", ""),
            "host": os.environ.get("DATABRICKS_HOST", ""),
            "error": str(e),
        }


def _setup_check(fn) -> dict:
    """Run a check function and return {ok, message}, never raising."""
    try:
        message = fn()
        return {"ok": True, "message": message or "OK"}
    except Exception as e:
        msg = str(e)
        # Trim noisy stack-y error bodies that come back from the SQL API
        if len(msg) > 400:
            msg = msg[:400] + "…"
        return {"ok": False, "message": msg}


@router.get("/setup/status", operation_id="setupStatus")
async def setup_status() -> dict:
    """Return everything the setup wizard needs to render.

    Each top-level field is independent so the UI can show partial progress
    while later checks fail (e.g. config + identity render even when warehouse
    access is denied).
    """
    catalog = get_catalog()
    raw_schema = get_raw_schema()
    silver_schema = get_silver_schema()
    gold_schema = get_gold_schema()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
    llm_endpoint = os.environ.get("LLM_ENDPOINT", "")
    host = os.environ.get("DATABRICKS_HOST", "") or ""

    config = {
        "catalog": catalog,
        "raw_schema": raw_schema,
        "silver_schema": silver_schema,
        "gold_schema": gold_schema,
        "warehouse_id": warehouse_id,
        "llm_endpoint": llm_endpoint,
        "host": host,
    }
    config_ok = bool(catalog and warehouse_id)
    config_check = {
        "ok": config_ok,
        "message": "All required env vars set" if config_ok
            else "Missing BHE_CATALOG and/or DATABRICKS_WAREHOUSE_ID",
    }

    identity = _resolve_app_identity()

    # Warehouse: SELECT 1 is independent of catalog access
    warehouse = _setup_check(lambda: (
        execute_query("SELECT 1 AS probe", tag_overrides={"submodule": "setup_probe_warehouse"}),
        "Warehouse responded to SELECT 1",
    )[1])

    # Catalog access: SHOW SCHEMAS requires USE_CATALOG
    catalog_access = _setup_check(lambda: (
        execute_query(
            f"SHOW SCHEMAS IN `{catalog}`",
            tag_overrides={"submodule": "setup_probe_catalog"},
        ),
        f"Catalog `{catalog}` is reachable",
    )[1])

    # LLM endpoint: probe via ai_query() because that's the exact path the
    # schema/table enrichment jobs use. If this works, the SP has CAN_QUERY
    # on the endpoint AND the warehouse can dispatch to model serving.
    if not llm_endpoint:
        llm_access = {"ok": False, "message": "LLM_ENDPOINT not configured"}
    elif not warehouse["ok"]:
        llm_access = {"ok": False, "message": "Skipped (warehouse unreachable)"}
    else:
        # Cap the response size hard so we don't burn tokens on the probe.
        llm_access = _setup_check(lambda: (
            execute_query(
                f"SELECT ai_query('{llm_endpoint}', 'Reply with the single word OK.', "
                f"modelParameters => named_struct('max_tokens', 4)) AS reply",
                tag_overrides={"submodule": "setup_probe_llm"},
                poll_timeout=60,
            ),
            f"`{llm_endpoint}` responded to ai_query()",
        )[1])

    # Schemas present?
    schemas_state: dict = {raw_schema: False, silver_schema: False, gold_schema: False}
    schemas_check_msg = ""
    if catalog_access["ok"]:
        try:
            in_list = ", ".join(f"'{s}'" for s in [raw_schema, silver_schema, gold_schema])
            rows = execute_query(
                f"SELECT schema_name FROM `{catalog}`.information_schema.schemata "
                f"WHERE schema_name IN ({in_list})",
                tag_overrides={"submodule": "setup_probe_schemas"},
            )
            present = {r["schema_name"] for r in rows}
            for s in schemas_state:
                schemas_state[s] = s in present
            missing = [s for s, ok in schemas_state.items() if not ok]
            schemas_check_msg = (
                "All 3 schemas exist" if not missing
                else f"Missing schemas: {', '.join(missing)}"
            )
        except Exception as e:
            schemas_check_msg = f"Could not query schemata: {e}"
    else:
        schemas_check_msg = "Skipped (catalog unreachable)"
    schemas_ok = catalog_access["ok"] and all(schemas_state.values())

    # Volume present? CSV ingest + branding logo upload + seed data all
    # depend on `<raw_schema>.uploads`. Probe via information_schema.volumes
    # so a missing volume becomes a visible "ok=false" instead of a confusing
    # 502 the first time the user clicks "Upload".
    volume_state = {"uploads": False}
    volume_msg = ""
    if schemas_state.get(raw_schema):
        try:
            rows = execute_query(
                f"SELECT volume_name FROM `{catalog}`.information_schema.volumes "
                f"WHERE volume_schema = '{raw_schema}' AND volume_name = 'uploads'",
                tag_overrides={"submodule": "setup_probe_volume"},
            )
            volume_state["uploads"] = bool(rows)
            volume_msg = (
                f"`{raw_schema}.uploads` exists" if volume_state["uploads"]
                else f"Missing volume `{raw_schema}.uploads` — re-run Bootstrap Schemas"
            )
        except Exception as e:
            volume_msg = f"Could not probe volumes: {e}"
    else:
        volume_msg = "Skipped (raw schema missing)"
    volume_ok = volume_state["uploads"]

    # Tables present? Only check the schemas that exist.
    silver_present: list[str] = []
    silver_missing: list[str] = []
    gold_present: list[str] = []
    gold_missing: list[str] = []
    if schemas_state.get(silver_schema):
        try:
            rows = execute_query(
                f"SELECT table_name FROM `{catalog}`.information_schema.tables "
                f"WHERE table_schema = '{silver_schema}'",
                tag_overrides={"submodule": "setup_probe_silver_tables"},
            )
            seen = {r["table_name"] for r in rows}
            for t in _SETUP_SILVER_DDL:
                (silver_present if t in seen else silver_missing).append(t)
        except Exception as e:
            logger.warning(f"silver tables probe failed: {e}")
            silver_missing = list(_SETUP_SILVER_DDL.keys())
    else:
        silver_missing = list(_SETUP_SILVER_DDL.keys())
    if schemas_state.get(gold_schema):
        try:
            rows = execute_query(
                f"SELECT table_name FROM `{catalog}`.information_schema.tables "
                f"WHERE table_schema = '{gold_schema}'",
                tag_overrides={"submodule": "setup_probe_gold_tables"},
            )
            seen = {r["table_name"] for r in rows}
            for t in _SETUP_GOLD_DDL:
                (gold_present if t in seen else gold_missing).append(t)
        except Exception as e:
            logger.warning(f"gold tables probe failed: {e}")
            gold_missing = list(_SETUP_GOLD_DDL.keys())
    else:
        gold_missing = list(_SETUP_GOLD_DDL.keys())
    tables_ok = not silver_missing and not gold_missing

    # Data ingested? (silver_schemas / silver_tables both have rows)
    data_counts = {"silver_schemas": 0, "silver_tables": 0}
    if "silver_schemas" in silver_present or schemas_state.get(silver_schema):
        try:
            r = execute_query(
                f"SELECT count(*) AS n FROM {catalog}.{silver_schema}.silver_schemas",
                tag_overrides={"submodule": "setup_probe_data_schemas"},
            )
            data_counts["silver_schemas"] = int((r[0] if r else {}).get("n") or 0)
        except Exception:
            pass
        try:
            r = execute_query(
                f"SELECT count(*) AS n FROM {catalog}.{silver_schema}.silver_tables",
                tag_overrides={"submodule": "setup_probe_data_tables"},
            )
            data_counts["silver_tables"] = int((r[0] if r else {}).get("n") or 0)
        except Exception:
            pass
    data_ok = data_counts["silver_schemas"] > 0 or data_counts["silver_tables"] > 0

    # Company profile present?
    company = {"present": False, "company_name": ""}
    if "company_profile" in silver_present:
        try:
            r = execute_query(
                f"SELECT company_name FROM {catalog}.{silver_schema}.company_profile LIMIT 1",
                tag_overrides={"submodule": "setup_probe_company"},
            )
            if r:
                company["present"] = True
                company["company_name"] = r[0].get("company_name") or ""
        except Exception:
            pass

    # Genie space: deployable only after gold tables exist (the space's
    # data_sources reference bhe_silver/bhe_gold). Status is informational --
    # the chatbot's app_* tools work without Genie; only the `genie_ask`
    # fallback tool needs it.
    genie_state: dict = {
        "deployable": "app_config" in gold_present,
        "deployed": False,
        "space_id": "",
        "url": "",
        "source": "",
    }
    # Prefer the runtime config table (where the wizard writes); fall back to
    # the app.yml env var (where deploy-time overrides go).
    space_id = ""
    if "app_config" in gold_present:
        try:
            v = app_config.get_config_value("genie_space_id")
            if v:
                space_id = v
                genie_state["source"] = "app_config"
        except Exception:
            pass
    if not space_id:
        env_id = os.environ.get("GENIE_SPACE_ID", "").strip()
        if env_id:
            space_id = env_id
            genie_state["source"] = "env"
    if space_id:
        genie_state["deployed"] = True
        genie_state["space_id"] = space_id
        genie_state["url"] = f"{host.rstrip('/')}/genie/rooms/{space_id}" if host else ""

    # Build the GRANT statements an admin can copy/paste. Identifier-quote
    # everything; principal_id is either an email (humans) or UUID (SPs).
    sp_principal = identity.get("user_name") or identity.get("client_id") or "<service-principal-id>"
    grants_sql = [
        f"-- Run as a metastore admin or catalog owner",
        f"GRANT USE_CATALOG ON CATALOG `{catalog}` TO `{sp_principal}`;",
        f"GRANT CREATE_SCHEMA ON CATALOG `{catalog}` TO `{sp_principal}`;",
    ]
    for sch in [raw_schema, silver_schema, gold_schema]:
        if schemas_state.get(sch):
            grants_sql.append(
                f"GRANT USE_SCHEMA, SELECT, MODIFY, CREATE_TABLE "
                f"ON SCHEMA `{catalog}`.`{sch}` TO `{sp_principal}`;"
            )
    grants_sql.append(
        f"-- And in the SQL Warehouse permissions UI, grant CAN_USE on "
        f"warehouse `{warehouse_id}` to `{sp_principal}`."
    )
    if llm_endpoint:
        grants_sql.append(
            f"-- And in Serving Endpoints UI, grant CAN_QUERY on "
            f"`{llm_endpoint}` to `{sp_principal}` (or via CLI: "
            f"databricks serving-endpoints update-permissions {llm_endpoint} "
            f"--json '{{\"access_control_list\":[{{\"service_principal_name\":\"{sp_principal}\",\"permission_level\":\"CAN_QUERY\"}}]}}')."
        )

    # Overall status: tier 1 = identity + warehouse + catalog + llm, tier 2 = schemas + volume + tables
    is_setup_ready = (
        config_ok and warehouse["ok"] and catalog_access["ok"] and llm_access["ok"]
        and schemas_ok and volume_ok and tables_ok
    )
    is_data_ready = is_setup_ready and data_ok
    is_complete = is_data_ready and company["present"]

    return {
        "config": config,
        "config_check": config_check,
        "identity": identity,
        "warehouse_access": warehouse,
        "catalog_access": catalog_access,
        "llm_access": llm_access,
        "schemas": {
            "ok": schemas_ok,
            "message": schemas_check_msg,
            "state": schemas_state,
        },
        "volume": {
            "ok": volume_ok,
            "message": volume_msg,
            "state": volume_state,
        },
        "tables": {
            "ok": tables_ok,
            "silver_present": silver_present,
            "silver_missing": silver_missing,
            "gold_present": gold_present,
            "gold_missing": gold_missing,
        },
        "data": {
            "ok": data_ok,
            "counts": data_counts,
        },
        "company": company,
        "genie": genie_state,
        "grants_sql": grants_sql,
        "is_setup_ready": is_setup_ready,
        "is_data_ready": is_data_ready,
        "is_complete": is_complete,
    }


@router.post("/setup/bootstrap-schemas", operation_id="setupBootstrapSchemas")
async def setup_bootstrap_schemas() -> dict:
    """Create the raw / silver / gold schemas + the uploads Volume.

    Idempotent (CREATE SCHEMA / VOLUME IF NOT EXISTS). Fails fast with a clear
    error if the service principal lacks CREATE SCHEMA on the catalog.

    The `uploads` Volume in the raw schema is required for *every* CSV ingest
    (schema-extractor output, affiliate seeds, branding logos). Creating it
    here means a fresh wizard run on a brand-new catalog leaves the user able
    to upload — without requiring them to also run `scripts/deploy.py`. See
    onboarding-bug-backlog B-013.
    """
    catalog = get_catalog()
    raw_schema = get_raw_schema()
    schemas = [raw_schema, get_silver_schema(), get_gold_schema()]
    created: list[str] = []
    created_volumes: list[str] = []
    failed: list[dict] = []
    for sch in schemas:
        try:
            execute_update(
                f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{sch}`",
                tag_overrides={"submodule": f"setup_create_schema_{sch}"},
            )
            created.append(sch)
        except Exception as e:
            failed.append({"schema": sch, "error": str(e)[:400]})

    # Volume creation is best-effort: if the raw schema didn't get created we
    # can't create the volume in it; log to `failed` and let the wizard
    # surface it. If the raw schema *did* get created, missing CREATE_VOLUME
    # privilege on it is a separate (and recoverable) error.
    if raw_schema in created:
        try:
            execute_update(
                f"CREATE VOLUME IF NOT EXISTS `{catalog}`.`{raw_schema}`.`uploads` "
                f"COMMENT 'Landing zone for schema-extractor CSVs and seed data uploads'",
                tag_overrides={"submodule": "setup_create_volume_uploads"},
            )
            created_volumes.append(f"{raw_schema}.uploads")
        except Exception as e:
            failed.append({"volume": f"{raw_schema}.uploads", "error": str(e)[:400]})

    if failed and not created:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Could not create any schemas. The app's service "
                           "principal is likely missing CREATE_SCHEMA on the "
                           "catalog.",
                "catalog": catalog,
                "failed": failed,
            },
        )
    return {
        "catalog": catalog,
        "created": created,
        "created_volumes": created_volumes,
        "failed": failed,
    }


@router.post("/setup/bootstrap-tables", operation_id="setupBootstrapTables")
async def setup_bootstrap_tables() -> dict:
    """Create all required silver + gold skeleton tables.

    `silver_schemas` / `silver_tables` are *not* created here — they are built
    from CSV via the ingest endpoints. Idempotent (CREATE TABLE IF NOT EXISTS).
    """
    catalog = get_catalog()
    silver = get_silver_schema()
    gold = get_gold_schema()

    created: list[str] = []
    failed: list[dict] = []

    for table, ddl_template in _SETUP_SILVER_DDL.items():
        ddl = ddl_template.format(fqn=f"{catalog}.{silver}.{table}")
        try:
            execute_update(
                ddl,
                tag_overrides={"submodule": f"setup_create_silver_{table}"},
            )
            created.append(f"{silver}.{table}")
        except Exception as e:
            failed.append({"table": f"{silver}.{table}", "error": str(e)[:400]})

    for table, ddl_template in _SETUP_GOLD_DDL.items():
        ddl = ddl_template.format(fqn=f"{catalog}.{gold}.{table}")
        try:
            execute_update(
                ddl,
                tag_overrides={"submodule": f"setup_create_gold_{table}"},
            )
            created.append(f"{gold}.{table}")
        except Exception as e:
            failed.append({"table": f"{gold}.{table}", "error": str(e)[:400]})

    seeded_rules = 0
    if f"{gold}.classification_rules" in created:
        seeded_rules = _seed_classification_rules_if_empty(gold)

    if failed and not created:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Could not create any tables. The app's service "
                           "principal is likely missing CREATE_TABLE on the "
                           "schemas. Run the schema bootstrap first if the "
                           "schemas don't exist.",
                "failed": failed,
            },
        )
    return {
        "catalog": catalog,
        "created": created,
        "failed": failed,
        "seeded_classification_rules": seeded_rules,
    }


@router.post("/setup/deploy-genie-space", operation_id="setupDeployGenie")
async def setup_deploy_genie_space(
    force_new: bool = Query(
        False,
        description=(
            "If true, ignore any existing space ID (in app_config or env) and "
            "create a fresh Genie space. Use after a PATCH failure that the "
            "user wants to recover from by abandoning the old space."
        ),
    ),
) -> dict:
    """Create-or-update the BHE Catalog Explorer Genie space.

    Reads the canonical space JSON, substitutes the running app's catalog
    and silver/gold schema names into every table identifier, and POSTs (or
    PATCHes) to the Genie REST API. The returned `space_id` is persisted to
    `<gold>.app_config` so the chatbot's `genie_ask` tool resolves it at
    request time without an app restart.

    Prerequisites: silver + gold schemas exist and `app_config` table has
    been bootstrapped. Returns 412 with a remediation hint otherwise.
    """
    catalog = get_catalog()
    gold = get_gold_schema()

    # Probe app_config: required for persisting the returned space_id.
    try:
        execute_query(
            f"SELECT 1 FROM `{catalog}`.`{gold}`.app_config LIMIT 1",
            tag_overrides={"submodule": "setup_genie_probe_app_config"},
        )
    except Exception as e:
        raise HTTPException(
            status_code=412,
            detail={
                "message": (
                    "The `app_config` table doesn't exist yet. Click "
                    "'Create database tables' in the Setup Wizard first, "
                    "then re-try."
                ),
                "table": f"{catalog}.{gold}.app_config",
                "error": str(e)[:300],
            },
        )

    # Resolve principal for the audit column on app_config writes.
    identity = _resolve_app_identity()
    principal = identity.get("user_name") or identity.get("client_id") or "app"

    try:
        result = setup_genie.deploy_genie_space(
            principal=principal, force_new=force_new
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail={"message": str(e)})
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail={"message": str(e)[:600]})
    except Exception as e:
        logger.exception("Genie deploy failed")
        raise HTTPException(status_code=500, detail={"message": str(e)[:600]})

    return result


@router.post("/setup/nuke", operation_id="setupNuke")
async def setup_nuke(confirm_catalog: str = Query(..., description="Must equal the configured BHE_CATALOG")) -> dict:
    """DROP all 3 BHE schemas (raw, silver, gold) CASCADE.

    Requires the caller to pass `?confirm_catalog={catalog}` matching the
    currently-configured catalog as a typed-confirmation safety. This is the
    "start over" button — every Delta table, every uploaded Volume file, and
    every research / enrichment row goes away.
    """
    catalog = get_catalog()
    if confirm_catalog != catalog:
        raise HTTPException(
            status_code=400,
            detail=f"confirm_catalog must equal the configured catalog '{catalog}' "
                   f"(received '{confirm_catalog}')",
        )

    schemas = [get_raw_schema(), get_silver_schema(), get_gold_schema()]
    dropped: list[str] = []
    failed: list[dict] = []
    for sch in schemas:
        try:
            execute_update(
                f"DROP SCHEMA IF EXISTS `{catalog}`.`{sch}` CASCADE",
                tag_overrides={"submodule": f"setup_nuke_{sch}"},
            )
            dropped.append(sch)
        except Exception as e:
            failed.append({"schema": sch, "error": str(e)[:400]})
    return {"catalog": catalog, "dropped": dropped, "failed": failed}


# ---------------------------------------------------------------------------
# Catalog Stats (Dashboard)
# ---------------------------------------------------------------------------


@router.get("/catalog/stats", operation_id="catalogStats")
async def catalog_stats() -> CatalogStatsOut:
    """Dashboard summary stats.

    Source-of-truth notes (post-redesign):
      - Schema-level fields (definition, data_domain, department_owner) live in
        gold.schema_inventory because the AI enrichment job writes there.
      - Totals (catalogs/schemas) come from gold and exclude SYSTEM rows.
      - `enrichable_schemas` and `enrichable_tables` count only PRODUCTION
        rows -- the universe the AI enrichment jobs actually operate on
        (see _run_enrichment / _run_table_enrichment, which both filter on
        classification = 'PRODUCTION'). Using these as the AI Coverage
        denominator avoids the dashboard reporting ~7% coverage when in
        reality the job covered ~99% of what it's allowed to touch.
      - Tables remain on silver_tables (which the table-enrichment job updates).
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    try:
        schema_stats = execute_query(
            f"""
            SELECT
                COUNT(DISTINCT catalog_name) as total_catalogs,
                COUNT(*) as total_schemas,
                SUM(CASE WHEN classification = 'PRODUCTION' THEN 1 ELSE 0 END) as enrichable,
                SUM(CASE WHEN classification = 'PRODUCTION'
                          AND definition IS NOT NULL AND definition != ''
                         THEN 1 ELSE 0 END) as enriched
            FROM {fqn(gold, 'schema_inventory')}
            WHERE COALESCE(classification, '') NOT IN ('SYSTEM')
            """
        )
        table_stats = execute_query(
            f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN classification = 'PRODUCTION' THEN 1 ELSE 0 END) as enrichable,
                SUM(CASE WHEN classification = 'PRODUCTION'
                          AND ai_definition IS NOT NULL AND ai_definition != ''
                         THEN 1 ELSE 0 END) as enriched
            FROM {fqn(silver, 'silver_tables')}
            """
        )
        env_dist = execute_query(
            f"""
            SELECT environment as name, COUNT(*) as value
            FROM {fqn(gold, 'schema_inventory')}
            WHERE environment IS NOT NULL AND environment != ''
              AND COALESCE(classification, '') NOT IN ('SYSTEM')
            GROUP BY environment ORDER BY value DESC
            """
        )
        domain_dist = execute_query(
            f"""
            SELECT data_domain as name, COUNT(*) as value
            FROM {fqn(gold, 'schema_inventory')}
            WHERE data_domain IS NOT NULL AND data_domain != ''
            GROUP BY data_domain ORDER BY value DESC LIMIT 15
            """
        )
        type_dist = execute_query(
            f"""
            SELECT table_type as name, COUNT(*) as value
            FROM {fqn(silver, 'silver_tables')}
            WHERE table_type IS NOT NULL AND table_type != ''
            GROUP BY table_type ORDER BY value DESC
            """
        )
        dept_dist = execute_query(
            f"""
            SELECT department_owner as name, COUNT(*) as value
            FROM {fqn(gold, 'schema_inventory')}
            WHERE department_owner IS NOT NULL AND department_owner != ''
            GROUP BY department_owner ORDER BY value DESC LIMIT 15
            """
        )
        s = schema_stats[0] if schema_stats else {}
        t = table_stats[0] if table_stats else {}

        # Databricks SQL Statements API serializes BIGINTs as JSON strings to
        # preserve precision. The COUNT(*) in the distribution queries is a
        # BIGINT, so without coercion the frontend ends up doing
        # `string_value / number_total` -> NaN -> 0% bars. Force value to int
        # here so the API contract is `{name: str, value: int}` end-to-end.
        def _coerce_dist(rows):
            return [
                {"name": r.get("name") or "", "value": int(r.get("value") or 0)}
                for r in (rows or [])
            ]

        return CatalogStatsOut(
            total_catalogs=int(s.get("total_catalogs") or 0),
            total_schemas=int(s.get("total_schemas") or 0),
            enrichable_schemas=int(s.get("enrichable") or 0),
            total_tables=int(t.get("total") or 0),
            enrichable_tables=int(t.get("enrichable") or 0),
            enriched_schemas=int(s.get("enriched") or 0),
            enriched_tables=int(t.get("enriched") or 0),
            environments=_coerce_dist(env_dist),
            domains=_coerce_dist(domain_dist),
            table_types=_coerce_dist(type_dist),
            departments=_coerce_dist(dept_dist),
        )
    except Exception as e:
        logger.warning(f"Stats query failed (tables may not exist yet): {e}")
        return CatalogStatsOut()


# ---------------------------------------------------------------------------
# Catalog Browser
# ---------------------------------------------------------------------------


@router.get("/catalog/schemas", operation_id="listSchemas")
async def list_schemas(
    domain: Optional[str] = Query(None),
    environment: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    classification: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
) -> dict:
    silver = get_silver_schema()
    conditions = ["classification NOT IN ('INTERNAL', 'SYSTEM')"]
    if domain:
        conditions.append(f"suggested_domain = '{domain}'")
    if environment:
        conditions.append(f"environment = '{environment}'")
    if department:
        conditions.append(f"suggested_department = '{department}'")
    if classification:
        conditions.append(f"classification = '{classification}'")
    if search:
        conditions.append(
            f"(LOWER(catalog_name) LIKE '%{search.lower()}%' "
            f"OR LOWER(schema_name) LIKE '%{search.lower()}%' "
            f"OR LOWER(business_friendly_name) LIKE '%{search.lower()}%' "
            f"OR LOWER(ai_definition) LIKE '%{search.lower()}%')"
        )
    where = " AND ".join(conditions)

    try:
        total = execute_query(
            f"SELECT COUNT(*) as cnt FROM {fqn(silver, 'silver_schemas')} WHERE {where}"
        )
        rows = execute_query(
            f"""
            SELECT * FROM {fqn(silver, 'silver_schemas')}
            WHERE {where}
            ORDER BY catalog_name, schema_name
            LIMIT {limit} OFFSET {offset}
            """
        )
        return {
            "total": total[0]["cnt"] if total else 0,
            "schemas": rows,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.warning(f"Schema query failed: {e}")
        return {"total": 0, "schemas": [], "limit": limit, "offset": offset}


@router.get("/catalog/schemas/filters", operation_id="schemaFilters")
async def schema_filters() -> dict:
    silver = get_silver_schema()
    try:
        domains = execute_query(
            f"SELECT DISTINCT suggested_domain as value FROM {fqn(silver, 'silver_schemas')} WHERE suggested_domain IS NOT NULL AND suggested_domain != '' ORDER BY value"
        )
        envs = execute_query(
            f"SELECT DISTINCT environment as value FROM {fqn(silver, 'silver_schemas')} WHERE environment IS NOT NULL ORDER BY value"
        )
        depts = execute_query(
            f"SELECT DISTINCT suggested_department as value FROM {fqn(silver, 'silver_schemas')} WHERE suggested_department IS NOT NULL AND suggested_department != '' ORDER BY value"
        )
        classifications = execute_query(
            f"SELECT DISTINCT classification as value FROM {fqn(silver, 'silver_schemas')} WHERE classification IS NOT NULL ORDER BY value"
        )
        return {
            "domains": [r["value"] for r in domains],
            "environments": [r["value"] for r in envs],
            "departments": [r["value"] for r in depts],
            "classifications": [r["value"] for r in classifications],
        }
    except Exception:
        return {"domains": [], "environments": [], "departments": [], "classifications": []}


@router.get("/catalog/tables/{catalog_name}/{schema_name}", operation_id="listTables")
async def list_tables(
    catalog_name: str,
    schema_name: str,
    search: Optional[str] = Query(None),
    limit: int = Query(200, le=1000),
    offset: int = Query(0),
) -> dict:
    silver = get_silver_schema()
    conditions = [
        f"table_catalog = '{catalog_name}'",
        f"table_schema = '{schema_name}'",
    ]
    if search:
        conditions.append(
            f"(LOWER(table_name) LIKE '%{search.lower()}%' "
            f"OR LOWER(ai_definition) LIKE '%{search.lower()}%')"
        )
    where = " AND ".join(conditions)

    try:
        total = execute_query(
            f"SELECT COUNT(*) as cnt FROM {fqn(silver, 'silver_tables')} WHERE {where}"
        )
        rows = execute_query(
            f"""
            SELECT * FROM {fqn(silver, 'silver_tables')}
            WHERE {where}
            ORDER BY table_name
            LIMIT {limit} OFFSET {offset}
            """
        )
        return {
            "total": total[0]["cnt"] if total else 0,
            "tables": rows,
        }
    except Exception as e:
        logger.warning(f"Table query failed: {e}")
        return {"total": 0, "tables": []}


# ---------------------------------------------------------------------------
# Catalog Edit
# ---------------------------------------------------------------------------


# Fields the chat / UI can patch on bhe_silver.silver_schemas. Kept
# in one place so propose-tool args, executor payloads, and the SET
# clause builder can't drift out of sync.
SCHEMA_EDITABLE_FIELDS: tuple[str, ...] = (
    "ai_definition",
    "business_friendly_name",
    "suggested_department",
    "suggested_domain",
    "data_sensitivity",
)


def build_schema_update_set_clause(patch: dict) -> Optional[str]:
    """Build the SQL `SET` body for a silver_schemas UPDATE.

    Drops fields not in `SCHEMA_EDITABLE_FIELDS`, escapes string
    literals, and always appends `is_user_edited=true` + a fresh
    `user_edited_at` so the row survives the next reseed.

    Returns None if `patch` is empty after filtering — callers should
    treat that as a no-op (don't issue an UPDATE with just the audit
    columns; that would still bump user_edited_at and confuse the
    "did anything actually change?" question).
    """
    pieces: list[str] = []
    for f in SCHEMA_EDITABLE_FIELDS:
        if f not in patch:
            continue
        v = patch[f]
        if v is None:
            # Nullable fields can be cleared by passing None (vs not
            # passing the key at all). The UI doesn't use this path
            # today; the chat tools strip empty strings before calling.
            pieces.append(f"{f} = NULL")
        else:
            pieces.append(f"{f} = '{_sql_escape(str(v))}'")
    if not pieces:
        return None
    pieces.append("is_user_edited = true")
    pieces.append(f"user_edited_at = '{datetime.utcnow().isoformat()}'")
    return ", ".join(pieces)


def update_silver_schema_rows(
    schema_name: str,
    patch: dict,
    *,
    catalog_filter: Optional[list[str]] = None,
) -> int:
    """Apply a patch to all silver_schemas rows for a logical schema.

    A logical schema can have multiple physical rows (one per dev/qa/prod
    catalog); the chat write tool defaults to "update them all" because
    that matches how users phrase the ask ("update the maximo schema").
    Pass `catalog_filter` to narrow if the user explicitly named one or
    more catalogs.

    Returns the count of rows targeted by the WHERE clause; the actual
    rows-updated number isn't returned by the SQL Statement Execution
    API, so we rely on the propose-tool's pre-flight count + the
    confirm flow's after-the-fact verification.

    Shared by the UI PUT `/catalog/schemas/{catalog}/{schema}` endpoint
    (single-row mode) and the chat propose/confirm executor
    (multi-row mode).
    """
    silver = get_silver_schema()
    set_clause = build_schema_update_set_clause(patch)
    if not set_clause:
        return 0
    sn = _sql_escape(schema_name)
    wheres = [f"schema_name = '{sn}'"]
    if catalog_filter:
        cats = ", ".join(f"'{_sql_escape(c)}'" for c in catalog_filter)
        wheres.append(f"catalog_name IN ({cats})")
    where_sql = " AND ".join(wheres)
    execute_query(
        f"""
        UPDATE {fqn(silver, 'silver_schemas')}
        SET {set_clause}
        WHERE {where_sql}
        """
    )
    # We can't get rowcount from SEA cleanly; fall back to a SELECT
    # COUNT to give the caller something honest. The propose tool
    # already does this pre-flight, so duplicating is cheap.
    rows = execute_query(
        f"""
        SELECT COUNT(*) AS n FROM {fqn(silver, 'silver_schemas')}
        WHERE {where_sql}
        """
    )
    return int((rows[0] or {}).get("n") or 0) if rows else 0


def find_silver_schema_rows(schema_name: str) -> list[dict]:
    """Return all silver_schemas rows for a logical schema name.

    Used by the chat propose tool to (a) confirm the schema exists,
    (b) compute per-catalog before-values for the diff card, and
    (c) detect divergence — when dev / qa / prod hold different values
    for a field, the user needs to know that "update" will collapse
    them to one value.
    """
    silver = get_silver_schema()
    sn = _sql_escape(schema_name)
    cols = ", ".join(["catalog_name", "schema_name"] + list(SCHEMA_EDITABLE_FIELDS))
    try:
        return execute_query(
            f"""
            SELECT {cols}
            FROM {fqn(silver, 'silver_schemas')}
            WHERE schema_name = '{sn}'
            ORDER BY catalog_name
            """
        )
    except Exception as e:
        logger.warning(f"find_silver_schema_rows failed: {e}")
        return []


@router.put("/catalog/schemas/{catalog_name}/{schema_name}", operation_id="updateSchema")
async def update_schema(catalog_name: str, schema_name: str, body: SchemaUpdateIn) -> dict:
    """UI single-row update path. The chat uses the multi-row helper above."""
    patch: dict = {}
    if body.ai_definition is not None:
        patch["ai_definition"] = body.ai_definition
    if body.business_friendly_name is not None:
        patch["business_friendly_name"] = body.business_friendly_name
    if body.suggested_department is not None:
        patch["suggested_department"] = body.suggested_department
    if body.suggested_domain is not None:
        patch["suggested_domain"] = body.suggested_domain
    if body.data_sensitivity is not None:
        patch["data_sensitivity"] = body.data_sensitivity

    set_clause = build_schema_update_set_clause(patch)
    if not set_clause:
        raise HTTPException(400, "No fields to update")

    silver = get_silver_schema()
    execute_query(
        f"""
        UPDATE {fqn(silver, 'silver_schemas')}
        SET {set_clause}
        WHERE catalog_name = '{_sql_escape(catalog_name)}'
          AND schema_name  = '{_sql_escape(schema_name)}'
        """
    )
    return {"status": "updated", "catalog_name": catalog_name, "schema_name": schema_name}


@router.put("/catalog/tables/{catalog_name}/{schema_name}/{table_name}", operation_id="updateTable")
async def update_table(
    catalog_name: str, schema_name: str, table_name: str, body: TableUpdateIn
) -> dict:
    silver = get_silver_schema()
    updates = []
    if body.ai_definition is not None:
        updates.append(f"ai_definition = '{body.ai_definition}'")
    if body.business_friendly_name is not None:
        updates.append(f"business_friendly_name = '{body.business_friendly_name}'")

    if not updates:
        raise HTTPException(400, "No fields to update")

    updates.append("is_user_edited = true")
    updates.append(f"user_edited_at = '{datetime.utcnow().isoformat()}'")
    set_clause = ", ".join(updates)

    execute_query(
        f"""
        UPDATE {fqn(silver, 'silver_tables')}
        SET {set_clause}
        WHERE table_catalog = '{catalog_name}'
          AND table_schema = '{schema_name}'
          AND table_name = '{table_name}'
        """
    )
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Source Systems Browser
#
# Business-user oriented view: "Do we have SAP data? If so, where? what tables?"
# Backed by gold.source_system_canonical + gold.source_system_aliases +
# silver.silver_tables.source_system_canonical. Always treats empty/null
# canonical as the synthetic bucket "Unclassified".
# ---------------------------------------------------------------------------


UNCLASSIFIED_BUCKET = "Unclassified"


def _sql_escape(value: str) -> str:
    """Escape single quotes for safe inline use in SQL string literals."""
    return value.replace("'", "''")


def _parse_array(value) -> list:
    """
    The Databricks SQL Statement Execution API returns ARRAY columns as JSON
    strings (e.g. '["SAP","Maximo"]'), not native lists. Coerce them.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [v for v in parsed if v not in (None, "")]
        except Exception:
            pass
    return []


def _to_int(value, default: int = 0) -> int:
    """SEA returns numeric aggregates as strings; coerce them for JSON output."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _to_bool(value) -> bool:
    """SEA returns BOOLEAN columns as the strings 'true'/'false'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


# ---------------------------------------------------------------------------
# Delivery-lifecycle status for use cases
#
# We record a status (not_started/in_progress/delivered/on_hold) on every row in
# bhe_silver.use_cases so the Value & Readiness page can report "realized" value
# (delivered) separately from "in-flight" and "opportunity" value. The columns
# were added after the initial seed, so we ensure them on first read/write and
# remember that we've done so to avoid re-issuing DDL on every request.
# ---------------------------------------------------------------------------
_USE_CASE_STATUS_COLUMNS_READY = False


def _ensure_use_case_status_columns() -> None:
    """Idempotently add status columns to bhe_silver.use_cases.

    Uses DESCRIBE-then-ALTER (same pattern as normalize_source_systems.py) so
    this is safe to call repeatedly and safe to ship with environments whose
    Delta runtime doesn't support ``ADD COLUMN IF NOT EXISTS``.
    """
    global _USE_CASE_STATUS_COLUMNS_READY
    if _USE_CASE_STATUS_COLUMNS_READY:
        return
    silver = get_silver_schema()
    tbl = fqn(silver, "use_cases")
    try:
        rows = execute_query(f"DESCRIBE TABLE {tbl}")
        existing = {
            (r.get("col_name") or "").lower()
            for r in rows
            if r.get("col_name") and not str(r.get("col_name")).startswith("#")
        }
        to_add: list[str] = []
        if "status" not in existing:
            to_add.append(
                "status STRING COMMENT 'Delivery lifecycle: "
                "not_started|in_progress|delivered|on_hold'"
            )
        if "status_notes" not in existing:
            to_add.append(
                "status_notes STRING COMMENT 'Free-form note explaining the current status'"
            )
        if "status_updated_at" not in existing:
            to_add.append(
                "status_updated_at TIMESTAMP COMMENT 'When status last changed'"
            )
        if to_add:
            execute_query(
                f"ALTER TABLE {tbl} ADD COLUMNS (" + ", ".join(to_add) + ")"
            )
            logger.info(
                "Added use_cases status columns: %s", ", ".join(to_add)
            )
        _USE_CASE_STATUS_COLUMNS_READY = True
    except Exception as e:
        # Don't keep hammering DDL if it fails (e.g. token expired); the caller
        # will still get a sensible response because every read path uses
        # COALESCE over the columns.
        logger.warning(f"_ensure_use_case_status_columns failed: {e}")


# ---------------------------------------------------------------------------
# Defensive ALTER for silver_tables source-system columns.
#
# `silver_tables` is created by `bootstrap_tables` via CTAS from the
# schema-extractor CSV. The CTAS does *not* project `source_system` or
# `source_system_canonical` — those columns are added later by the
# `normalize_source_systems` job via DESCRIBE+ALTER.
#
# But several read paths (analytics_schema_tables, list_source_systems,
# source_system_detail) reference `silver_tables.source_system` directly.
# Without the normalize job having run, those queries 500 with
# UNRESOLVED_COLUMN. This helper is the same pattern as
# `_ensure_use_case_status_columns()` above and lets the read paths render
# (with NULL values) before the normalize job has run.
#
# B-017 in onboarding-bug-backlog.md.
# ---------------------------------------------------------------------------
_SILVER_TABLES_SOURCE_COLUMNS_READY = False


def _ensure_silver_tables_columns() -> None:
    """Idempotently add `source_system` / `source_system_canonical` to silver_tables.

    Safe to call repeatedly; uses a process-local flag to avoid issuing DDL on
    every request after the first success. Failures (e.g. table doesn't exist
    yet, token expired) are logged but never raised so the caller can still
    return a sensible response — the underlying SQL will simply fail on the
    missing column with a clearer error than the user would otherwise see.
    """
    global _SILVER_TABLES_SOURCE_COLUMNS_READY
    if _SILVER_TABLES_SOURCE_COLUMNS_READY:
        return
    silver = get_silver_schema()
    tbl = fqn(silver, "silver_tables")
    try:
        rows = execute_query(
            f"DESCRIBE TABLE {tbl}",
            tag_overrides={"submodule": "ensure_silver_tables_cols_describe"},
        )
        existing = {
            (r.get("col_name") or "").lower()
            for r in rows or []
            if r.get("col_name") and not str(r.get("col_name")).startswith("#")
        }
        to_add: list[str] = []
        if "source_system" not in existing:
            to_add.append(
                "source_system STRING COMMENT 'Raw source-system value (added by normalize_source_systems job)'"
            )
        if "source_system_canonical" not in existing:
            to_add.append(
                "source_system_canonical STRING COMMENT 'Resolved canonical name (added by normalize_source_systems job)'"
            )
        if to_add:
            execute_update(
                f"ALTER TABLE {tbl} ADD COLUMNS (" + ", ".join(to_add) + ")",
                tag_overrides={"submodule": "ensure_silver_tables_cols_alter"},
            )
            logger.info(
                "Added silver_tables columns: %s", ", ".join(c.split(' ', 1)[0] for c in to_add)
            )
        _SILVER_TABLES_SOURCE_COLUMNS_READY = True
    except Exception as e:
        logger.warning(f"_ensure_silver_tables_columns failed: {e}")


def _normalize_status(value: Optional[str]) -> str:
    """Map any value to a known status, defaulting to 'not_started'."""
    if not value:
        return "not_started"
    v = str(value).strip().lower()
    return v if v in USE_CASE_STATUSES else "not_started"


@router.get("/source-systems", operation_id="listSourceSystems")
async def list_source_systems(
    search: Optional[str] = Query(None, description="Substring match against canonical name/description/category"),
    category: Optional[str] = Query(None),
    include_empty: bool = Query(True, description="If false, drop canonicals with 0 tables"),
) -> dict:
    """
    List canonical source systems with ingest stats.

    Every row reflects what the Lake actually holds for that system:
      - table_count / schema_count / affiliates / environments are counted
        from silver_tables (joined to silver_schemas for env/affiliate)
      - alias_count is how many raw labels collapse into this canonical
      - an "Unclassified" synthetic row is appended for tables whose
        source_system_canonical is NULL/empty
    """
    silver = get_silver_schema()
    gold = get_gold_schema()

    # Defensive: ensure source_system / source_system_canonical columns exist
    # on silver_tables (added by normalize_source_systems job; missing on a
    # fresh deploy before the job runs). See B-017.
    _ensure_silver_tables_columns()

    # Per-canonical rollup driven by silver_tables so we only show what
    # actually landed. LEFT JOIN to canonical so we can include zero-table
    # canonicals when include_empty=true.
    canonical_conditions = ["c.is_active = true"]
    if category:
        canonical_conditions.append(f"c.category = '{_sql_escape(category)}'")
    if search:
        s = _sql_escape(search.lower())
        canonical_conditions.append(
            f"(LOWER(c.canonical) LIKE '%{s}%' OR LOWER(COALESCE(c.description,'')) LIKE '%{s}%' OR LOWER(COALESCE(c.category,'')) LIKE '%{s}%')"
        )
    canonical_where = " AND ".join(canonical_conditions)

    try:
        rows = execute_query(
            f"""
            WITH table_stats AS (
                SELECT
                    t.source_system_canonical AS canonical,
                    COUNT(*) AS table_count,
                    COUNT(DISTINCT CONCAT(t.table_catalog, '.', t.table_schema)) AS schema_count,
                    COUNT(DISTINCT s.program) AS affiliate_count,
                    COLLECT_SET(s.program) AS affiliates,
                    COLLECT_SET(s.environment) AS environments
                FROM {fqn(silver, 'silver_tables')} t
                LEFT JOIN {fqn(silver, 'silver_schemas')} s
                    ON s.catalog_name = t.table_catalog
                   AND s.schema_name = t.table_schema
                WHERE t.source_system_canonical IS NOT NULL
                  AND t.source_system_canonical != ''
                GROUP BY t.source_system_canonical
            ),
            alias_stats AS (
                SELECT canonical, COUNT(*) AS alias_count
                FROM {fqn(gold, 'source_system_aliases')}
                GROUP BY canonical
            )
            SELECT
                c.canonical AS name,
                c.category,
                c.description,
                COALESCE(ts.table_count, 0) AS table_count,
                COALESCE(ts.schema_count, 0) AS schema_count,
                COALESCE(ts.affiliate_count, 0) AS affiliate_count,
                COALESCE(ts.affiliates, ARRAY()) AS affiliates,
                COALESCE(ts.environments, ARRAY()) AS environments,
                COALESCE(a.alias_count, 0) AS alias_count
            FROM {fqn(gold, 'source_system_canonical')} c
            LEFT JOIN table_stats ts ON ts.canonical = c.canonical
            LEFT JOIN alias_stats a ON a.canonical = c.canonical
            WHERE {canonical_where}
            {'' if include_empty else 'AND COALESCE(ts.table_count, 0) > 0'}
            ORDER BY COALESCE(ts.table_count, 0) DESC, c.canonical ASC
            """
        )
        for r in rows:
            r["affiliates"] = _parse_array(r.get("affiliates"))
            r["environments"] = _parse_array(r.get("environments"))
            r["table_count"] = _to_int(r.get("table_count"))
            r["schema_count"] = _to_int(r.get("schema_count"))
            r["affiliate_count"] = _to_int(r.get("affiliate_count"))
            r["alias_count"] = _to_int(r.get("alias_count"))

        # Synthetic "Unclassified" bucket for tables with no canonical yet.
        # Only surface it if the search/category filter would have matched it.
        show_unclassified = (not category) and (
            not search or search.lower() in UNCLASSIFIED_BUCKET.lower()
        )
        if show_unclassified:
            unclassified = execute_query(
                f"""
                SELECT
                    COUNT(*) AS table_count,
                    COUNT(DISTINCT CONCAT(t.table_catalog, '.', t.table_schema)) AS schema_count,
                    COUNT(DISTINCT s.program) AS affiliate_count,
                    COLLECT_SET(s.program) AS affiliates,
                    COLLECT_SET(s.environment) AS environments
                FROM {fqn(silver, 'silver_tables')} t
                LEFT JOIN {fqn(silver, 'silver_schemas')} s
                    ON s.catalog_name = t.table_catalog
                   AND s.schema_name = t.table_schema
                WHERE t.source_system_canonical IS NULL OR t.source_system_canonical = ''
                """
            )
            u = unclassified[0] if unclassified else {}
            u_count = int(u.get("table_count") or 0)
            if u_count > 0 or include_empty:
                rows.append({
                    "name": UNCLASSIFIED_BUCKET,
                    "category": "Unclassified",
                    "description": "Tables whose source system has not yet been mapped to a canonical. Use this view to find gaps in the alias mapping.",
                    "table_count": u_count,
                    "schema_count": u.get("schema_count", 0) or 0,
                    "affiliate_count": u.get("affiliate_count", 0) or 0,
                    "affiliates": _parse_array(u.get("affiliates")),
                    "environments": _parse_array(u.get("environments")),
                    "alias_count": 0,
                    "is_unclassified": True,
                })

        # Category facet (for filter dropdown). Driven by the canonical table
        # so customers see all seeded categories even when no data matches yet.
        categories = execute_query(
            f"""
            SELECT category, COUNT(*) AS n
            FROM {fqn(gold, 'source_system_canonical')}
            WHERE is_active = true AND category IS NOT NULL AND category != ''
            GROUP BY category
            ORDER BY category
            """
        )

        return {
            "total": len(rows),
            "systems": rows,
            "categories": [c["category"] for c in categories],
        }
    except Exception as e:
        logger.warning(f"list_source_systems failed: {e}")
        return {"total": 0, "systems": [], "categories": []}


@router.get("/source-systems/{name}", operation_id="sourceSystemDetail")
async def source_system_detail(name: str) -> dict:
    """
    Detail for a single canonical source system (or 'Unclassified' bucket).
    Returns description + per-schema rollup + raw aliases feeding it.
    """
    silver = get_silver_schema()
    gold = get_gold_schema()

    # Defensive: see B-017 / `_ensure_silver_tables_columns`.
    _ensure_silver_tables_columns()

    is_unclassified = name == UNCLASSIFIED_BUCKET
    name_esc = _sql_escape(name)

    try:
        if is_unclassified:
            meta = {
                "name": UNCLASSIFIED_BUCKET,
                "category": "Unclassified",
                "description": "Tables whose source_system_canonical is NULL/empty. Re-run the normalize job after editing aliases to reclassify.",
            }
            where_t = "(t.source_system_canonical IS NULL OR t.source_system_canonical = '')"
        else:
            meta_rows = execute_query(
                f"""
                SELECT canonical AS name, category, description, is_active, created_at, updated_at
                FROM {fqn(gold, 'source_system_canonical')}
                WHERE canonical = '{name_esc}'
                LIMIT 1
                """
            )
            if not meta_rows:
                raise HTTPException(404, f"Unknown source system: {name}")
            meta = meta_rows[0]
            where_t = f"t.source_system_canonical = '{name_esc}'"

        # Per-schema rollup: one row per (catalog.schema) feeding this system.
        schemas = execute_query(
            f"""
            SELECT
                t.table_catalog AS catalog_name,
                t.table_schema AS schema_name,
                COALESCE(MAX(s.environment), 'UNKNOWN') AS environment,
                COALESCE(MAX(s.program), 'Unknown') AS affiliate,
                COALESCE(MAX(s.zone), 'OTHER') AS zone,
                COALESCE(MAX(s.classification), '') AS classification,
                COALESCE(MAX(s.business_friendly_name), '') AS schema_friendly_name,
                COALESCE(MAX(s.ai_definition), '') AS schema_definition,
                COUNT(*) AS table_count,
                COLLECT_SET(t.source_system) AS raw_source_systems
            FROM {fqn(silver, 'silver_tables')} t
            LEFT JOIN {fqn(silver, 'silver_schemas')} s
                ON s.catalog_name = t.table_catalog
               AND s.schema_name = t.table_schema
            WHERE {where_t}
            GROUP BY t.table_catalog, t.table_schema
            ORDER BY table_count DESC, catalog_name, schema_name
            LIMIT 500
            """
        )
        for s in schemas:
            s["raw_source_systems"] = _parse_array(s.get("raw_source_systems"))
            s["table_count"] = _to_int(s.get("table_count"))

        # High-level totals
        totals_rows = execute_query(
            f"""
            SELECT
                COUNT(*) AS table_count,
                COUNT(DISTINCT CONCAT(t.table_catalog, '.', t.table_schema)) AS schema_count,
                COUNT(DISTINCT s.program) AS affiliate_count,
                COUNT(DISTINCT s.environment) AS environment_count,
                COLLECT_SET(s.program) AS affiliates,
                COLLECT_SET(s.environment) AS environments
            FROM {fqn(silver, 'silver_tables')} t
            LEFT JOIN {fqn(silver, 'silver_schemas')} s
                ON s.catalog_name = t.table_catalog
               AND s.schema_name = t.table_schema
            WHERE {where_t}
            """
        )
        totals = totals_rows[0] if totals_rows else {}

        # Raw aliases that map into this canonical (skip for Unclassified).
        if is_unclassified:
            aliases: list = []
        else:
            aliases = execute_query(
                f"""
                SELECT raw, mapped_by, confidence, is_user_edited,
                       CAST(mapped_at AS STRING) AS mapped_at
                FROM {fqn(gold, 'source_system_aliases')}
                WHERE canonical = '{name_esc}'
                ORDER BY
                    CASE WHEN is_user_edited THEN 0 ELSE 1 END,
                    raw
                LIMIT 500
                """
            )

        for a in aliases:
            a["is_user_edited"] = _to_bool(a.get("is_user_edited"))

        return {
            "meta": meta,
            "totals": {
                "table_count": _to_int(totals.get("table_count")),
                "schema_count": _to_int(totals.get("schema_count")),
                "affiliate_count": _to_int(totals.get("affiliate_count")),
                "environment_count": _to_int(totals.get("environment_count")),
                "affiliates": _parse_array(totals.get("affiliates")),
                "environments": _parse_array(totals.get("environments")),
            },
            "schemas": schemas,
            "aliases": aliases,
            "is_unclassified": is_unclassified,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"source_system_detail failed for {name!r}: {e}")
        raise HTTPException(500, f"Failed to load detail: {e}")


@router.get("/source-systems/{name}/tables", operation_id="sourceSystemTables")
async def source_system_tables(
    name: str,
    search: Optional[str] = Query(None),
    catalog: Optional[str] = Query(None, description="Filter to a specific catalog"),
    schema: Optional[str] = Query(None, description="Filter to a specific schema (requires catalog)"),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
) -> dict:
    """
    Paginated table listing for a canonical source system.
    Joins silver_schemas to surface environment + affiliate on each row.
    """
    silver = get_silver_schema()
    is_unclassified = name == UNCLASSIFIED_BUCKET
    name_esc = _sql_escape(name)

    conditions = (
        ["(t.source_system_canonical IS NULL OR t.source_system_canonical = '')"]
        if is_unclassified
        else [f"t.source_system_canonical = '{name_esc}'"]
    )
    if catalog:
        conditions.append(f"t.table_catalog = '{_sql_escape(catalog)}'")
    if schema:
        conditions.append(f"t.table_schema = '{_sql_escape(schema)}'")
    if search:
        s = _sql_escape(search.lower())
        conditions.append(
            "("
            f"LOWER(t.table_name) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(t.business_friendly_name,'')) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(t.ai_definition,'')) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(t.source_system,'')) LIKE '%{s}%'"
            ")"
        )
    where = " AND ".join(conditions)

    try:
        total_rows = execute_query(
            f"SELECT COUNT(*) AS cnt FROM {fqn(silver, 'silver_tables')} t WHERE {where}"
        )
        rows = execute_query(
            f"""
            SELECT
                t.table_catalog,
                t.table_schema,
                t.table_name,
                t.table_type,
                t.table_owner,
                t.comment,
                t.created,
                t.last_altered,
                t.data_source_format,
                t.classification,
                t.ai_definition,
                t.business_friendly_name,
                t.source_system,
                t.source_system_canonical,
                t.is_user_edited,
                COALESCE(s.environment, 'UNKNOWN') AS environment,
                COALESCE(s.program, 'Unknown') AS affiliate
            FROM {fqn(silver, 'silver_tables')} t
            LEFT JOIN {fqn(silver, 'silver_schemas')} s
                ON s.catalog_name = t.table_catalog
               AND s.schema_name = t.table_schema
            WHERE {where}
            ORDER BY t.table_catalog, t.table_schema, t.table_name
            LIMIT {limit} OFFSET {offset}
            """
        )
        for r in rows:
            r["is_user_edited"] = _to_bool(r.get("is_user_edited"))
        return {
            "total": _to_int(total_rows[0]["cnt"]) if total_rows else 0,
            "tables": rows,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.warning(f"source_system_tables failed for {name!r}: {e}")
        return {"total": 0, "tables": [], "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Value & Readiness (Phase 0 sanity API)
#
# Joins:
#   silver.use_cases
#   gold.use_case_source_requirements   (LLM-derived: required canonical sources)
#   gold.use_case_affiliates            (LLM-derived: applicable affiliates)
#   gold.affiliates                     (seed dim)
#   gold.program_affiliate_map          (seed bridge: catalog program -> affiliate)
#   silver.silver_tables                (presence: which canonical sources actually
#                                        have tables in the lake)
#   silver.silver_schemas               (program <-> catalog/schema)
#
# Readiness formula is selectable via ?formula=
#   simple : present_known / total_known                (0 weight on necessity)
#   must   : must_have_present / must_have_total        (only must_have requirements)
# 'Unmapped' source rows are excluded from both numerator and denominator (they
# are tracked separately as a vocabulary gap).
# ---------------------------------------------------------------------------


def _present_canonicals_cte(silver: str, affiliate_filter_sql: str = "") -> str:
    """
    CTE that emits the set of canonical source systems currently present in the
    lake. Optionally restricted to tables whose catalog/schema map to a given
    affiliate via silver_schemas + program_affiliate_map.
    """
    if affiliate_filter_sql:
        return f"""
        present AS (
            SELECT DISTINCT t.source_system_canonical AS canonical
            FROM {fqn(silver, 'silver_tables')} t
            JOIN {fqn(silver, 'silver_schemas')} s
              ON s.catalog_name = t.table_catalog
             AND s.schema_name = t.table_schema
            JOIN {fqn(get_gold_schema(), 'program_affiliate_map')} pm
              ON pm.program = COALESCE(s.program, 'Unknown')
            WHERE COALESCE(t.source_system_canonical, '') NOT IN ('', 'Other', 'Unmapped')
              AND {affiliate_filter_sql}
        )
        """
    return f"""
    present AS (
        SELECT DISTINCT source_system_canonical AS canonical
        FROM {fqn(silver, 'silver_tables')}
        WHERE COALESCE(source_system_canonical, '') NOT IN ('', 'Other', 'Unmapped')
    )
    """


@router.get("/value/affiliates", operation_id="listAffiliates")
async def list_affiliates() -> dict:
    """Affiliate dimension for the slicer. Includes a per-affiliate count of
    use cases (any applicability) so the UI can show distribution at a glance."""
    gold = get_gold_schema()
    silver = get_silver_schema()
    try:
        rows = execute_query(f"""
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
            LEFT JOIN {fqn(silver, 'use_cases')} uc
                ON uc.id = ua.use_case_id
            GROUP BY a.affiliate_name, a.affiliate_code, a.business_type,
                     a.region, a.description, a.is_active
            ORDER BY use_case_count DESC, a.affiliate_name
        """)
        for r in rows:
            r["is_active"] = _to_bool(r.get("is_active"))
            r["use_case_count"] = _to_int(r.get("use_case_count"))
            r["primary_use_case_count"] = _to_int(r.get("primary_use_case_count"))
        return {"affiliates": rows}
    except Exception as e:
        logger.warning(f"list_affiliates failed: {e}")
        return {"affiliates": []}


@router.get("/value/use-cases", operation_id="listValueUseCases")
async def list_value_use_cases(
    affiliate: Optional[str] = Query(
        None,
        description="Filter to use cases applicable to this affiliate "
                    "(matches use_case_affiliates.affiliate_name).",
    ),
    formula: str = Query(
        "simple",
        regex="^(simple|must)$",
        description="Readiness formula: 'simple' = present/total_known, "
                    "'must' = must_have_present/must_have_total.",
    ),
    priority: Optional[str] = Query(None, description="High | Medium | Low"),
    status: Optional[str] = Query(
        None,
        description="Filter by delivery status: not_started | in_progress | "
                    "delivered | on_hold",
    ),
    department: Optional[str] = Query(
        None,
        description="Filter to use cases owned by this department.",
    ),
    search: Optional[str] = Query(None),
    limit: int = Query(200, le=500),
) -> dict:
    """
    Returns ranked use cases with readiness % and the data needed to render
    the Treemap, Pareto, and Readiness views. Each row includes:
      - estimated_value_usd, priority, department
      - total_required (canonical sources, excluding 'Unmapped')
      - present_count, missing_count
      - must_have_total, must_have_present
      - unmapped_count   (LLM said this need has no canonical match - vocab gap)
      - readiness_pct    (computed per the requested formula)
      - applicable_affiliates (deduped list)
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    _ensure_use_case_status_columns()

    where_clauses: list[str] = []
    if priority:
        where_clauses.append(f"uc.priority = '{_sql_escape(priority)}'")
    if status:
        where_clauses.append(
            f"COALESCE(uc.status, 'not_started') = "
            f"'{_sql_escape(_normalize_status(status))}'"
        )
    if department:
        where_clauses.append(
            f"COALESCE(NULLIF(TRIM(uc.department), ''), 'Unassigned') = "
            f"'{_sql_escape(department)}'"
        )
    if search:
        s = _sql_escape(search.lower())
        where_clauses.append(
            "("
            f"LOWER(uc.use_case_name) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(uc.description,'')) LIKE '%{s}%'"
            ")"
        )
    affiliate_join = ""
    if affiliate:
        aff_esc = _sql_escape(affiliate)
        affiliate_join = (
            f"JOIN {fqn(gold, 'use_case_affiliates')} ua_filter "
            f"ON ua_filter.use_case_id = uc.id "
            f"AND ua_filter.affiliate_name = '{aff_esc}'"
        )
        # When sliced by affiliate, presence is also restricted to that
        # affiliate's catalogs (a use case isn't 'ready for PacifiCorp' if the
        # data only exists in NV Energy).
        present_cte = _present_canonicals_cte(
            silver, f"pm.affiliate_name = '{aff_esc}'"
        )
    else:
        present_cte = _present_canonicals_cte(silver)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        WITH {present_cte},
        scoped_uc AS (
            SELECT uc.id, uc.use_case_name, uc.description, uc.department,
                   uc.priority, uc.estimated_value_usd, uc.business_value,
                   COALESCE(uc.status, 'not_started') AS status
            FROM {fqn(silver, 'use_cases')} uc
            {affiliate_join}
            {where_sql}
        ),
        req AS (
            SELECT
                u.id, u.use_case_name, u.description, u.department, u.priority,
                COALESCE(u.estimated_value_usd, 0) AS estimated_value_usd,
                u.business_value, u.status,
                r.required_canonical, r.necessity,
                CASE WHEN p.canonical IS NOT NULL THEN 1 ELSE 0 END AS is_present
            FROM scoped_uc u
            LEFT JOIN {fqn(gold, 'use_case_source_requirements')} r ON r.use_case_id = u.id
            LEFT JOIN present p
                   ON p.canonical = r.required_canonical
                  AND r.required_canonical != 'Unmapped'
        ),
        agg AS (
            SELECT
                id, use_case_name, description, department, priority,
                estimated_value_usd, business_value, status,
                COUNT(CASE WHEN required_canonical IS NOT NULL
                            AND required_canonical != 'Unmapped' THEN 1 END) AS total_required,
                SUM(CASE WHEN required_canonical != 'Unmapped' THEN is_present ELSE 0 END) AS present_count,
                COUNT(CASE WHEN required_canonical = 'Unmapped' THEN 1 END) AS unmapped_count,
                COUNT(CASE WHEN necessity='must_have'
                            AND required_canonical != 'Unmapped' THEN 1 END) AS must_total,
                SUM(CASE WHEN necessity='must_have'
                          AND required_canonical != 'Unmapped'
                          THEN is_present ELSE 0 END) AS must_present
            FROM req
            GROUP BY id, use_case_name, description, department, priority,
                     estimated_value_usd, business_value, status
        ),
        affs AS (
            SELECT use_case_id, COLLECT_SET(affiliate_name) AS applicable_affiliates
            FROM {fqn(gold, 'use_case_affiliates')}
            GROUP BY use_case_id
        )
        SELECT
            agg.id, agg.use_case_name, agg.description, agg.department,
            agg.priority, agg.estimated_value_usd, agg.business_value, agg.status,
            agg.total_required, agg.present_count,
            (agg.total_required - agg.present_count) AS missing_count,
            agg.must_total, agg.must_present,
            agg.unmapped_count,
            COALESCE(affs.applicable_affiliates, array()) AS applicable_affiliates
        FROM agg
        LEFT JOIN affs ON affs.use_case_id = agg.id
        ORDER BY agg.estimated_value_usd DESC NULLS LAST, agg.use_case_name
        LIMIT {limit}
    """

    try:
        rows = execute_query(sql)
    except Exception as e:
        logger.warning(f"list_value_use_cases failed: {e}")
        return {"use_cases": [], "formula": formula, "affiliate": affiliate}

    out = []
    for r in rows:
        total = _to_int(r.get("total_required"))
        present = _to_int(r.get("present_count"))
        must_total = _to_int(r.get("must_total"))
        must_present = _to_int(r.get("must_present"))
        if formula == "must":
            denom, num = must_total, must_present
        else:
            denom, num = total, present
        readiness_pct = round(100.0 * num / denom, 1) if denom > 0 else None
        out.append({
            "id": r.get("id"),
            "use_case_name": r.get("use_case_name"),
            "description": r.get("description"),
            "department": r.get("department"),
            "priority": r.get("priority"),
            "status": _normalize_status(r.get("status")),
            "estimated_value_usd": float(r.get("estimated_value_usd") or 0),
            "business_value": r.get("business_value"),
            "total_required": total,
            "present_count": present,
            "missing_count": _to_int(r.get("missing_count")),
            "must_total": must_total,
            "must_present": must_present,
            "unmapped_count": _to_int(r.get("unmapped_count")),
            "readiness_pct": readiness_pct,
            "applicable_affiliates": _parse_array(r.get("applicable_affiliates")),
        })
    return {
        "use_cases": out,
        "formula": formula,
        "affiliate": affiliate,
        "status": status,
    }


@router.get("/value/use-cases/{use_case_id}", operation_id="valueUseCaseDetail")
async def value_use_case_detail(
    use_case_id: str,
    affiliate: Optional[str] = Query(
        None,
        description="If supplied, presence is computed against tables in this "
                    "affiliate's catalogs only.",
    ),
) -> dict:
    """Per-use-case detail: requirements, present sources, missing sources,
    unmapped data needs, and the affiliate applicability list."""
    silver = get_silver_schema()
    gold = get_gold_schema()
    uc_esc = _sql_escape(use_case_id)

    if affiliate:
        present_cte = _present_canonicals_cte(
            silver, f"pm.affiliate_name = '{_sql_escape(affiliate)}'"
        )
    else:
        present_cte = _present_canonicals_cte(silver)

    _ensure_use_case_status_columns()
    try:
        meta = execute_query(f"""
            SELECT id, use_case_name, description, department, category,
                   priority, business_value, estimated_value_usd,
                   value_rationale, data_requirements,
                   COALESCE(status, 'not_started') AS status,
                   status_notes, status_updated_at
            FROM {fqn(silver, 'use_cases')}
            WHERE id = '{uc_esc}'
        """)
        if not meta:
            return {"error": "not_found"}
        m = meta[0]

        reqs = execute_query(f"""
            WITH {present_cte}
            SELECT r.required_canonical, r.necessity, r.data_need_excerpt,
                   r.confidence,
                   CASE WHEN p.canonical IS NOT NULL THEN true ELSE false END AS is_present
            FROM {fqn(gold, 'use_case_source_requirements')} r
            LEFT JOIN present p ON p.canonical = r.required_canonical
            WHERE r.use_case_id = '{uc_esc}'
            ORDER BY r.necessity DESC, r.required_canonical
        """)

        affs = execute_query(f"""
            SELECT ua.affiliate_name, ua.applicability, ua.rationale
            FROM {fqn(gold, 'use_case_affiliates')} ua
            WHERE ua.use_case_id = '{uc_esc}'
            ORDER BY CASE WHEN ua.applicability='primary' THEN 0 ELSE 1 END,
                     ua.affiliate_name
        """)

        present, missing, unmapped = [], [], []
        must_total = must_present = total = present_n = 0
        for r in reqs:
            r["is_present"] = _to_bool(r.get("is_present"))
            necessity = r.get("necessity")
            canonical = r.get("required_canonical")
            if canonical == "Unmapped":
                unmapped.append(r)
                continue
            total += 1
            if necessity == "must_have":
                must_total += 1
            if r["is_present"]:
                present.append(r)
                present_n += 1
                if necessity == "must_have":
                    must_present += 1
            else:
                missing.append(r)

        return {
            "use_case": {
                "id": m.get("id"),
                "use_case_name": m.get("use_case_name"),
                "description": m.get("description"),
                "department": m.get("department"),
                "category": m.get("category"),
                "priority": m.get("priority"),
                "business_value": m.get("business_value"),
                "value_rationale": m.get("value_rationale"),
                "estimated_value_usd": float(m.get("estimated_value_usd") or 0),
                "data_requirements": m.get("data_requirements"),
                "status": _normalize_status(m.get("status")),
                "status_notes": m.get("status_notes") or "",
                "status_updated_at": (
                    str(m.get("status_updated_at"))
                    if m.get("status_updated_at") else None
                ),
            },
            "readiness": {
                "total_required": total,
                "present_count": present_n,
                "missing_count": total - present_n,
                "must_total": must_total,
                "must_present": must_present,
                "unmapped_count": len(unmapped),
                "readiness_pct_simple": (
                    round(100.0 * present_n / total, 1) if total > 0 else None
                ),
                "readiness_pct_must": (
                    round(100.0 * must_present / must_total, 1) if must_total > 0 else None
                ),
            },
            "present_sources": present,
            "missing_sources": missing,
            "unmapped_needs": unmapped,
            "applicable_affiliates": affs,
        }
    except Exception as e:
        logger.warning(f"value_use_case_detail failed for {use_case_id!r}: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Value & Readiness page aggregates (Phase 1)
#
# These build on the same gold tables but pre-aggregate for the visualizations
# to keep frontend bundles lean. All three accept the same affiliate / formula
# / priority / search filters as /value/use-cases so the page-level slicer
# applies consistently.
# ---------------------------------------------------------------------------


def _build_value_filters(
    *, affiliate: Optional[str], priority: Optional[str], search: Optional[str],
    silver: str, gold: str, status: Optional[str] = None,
    department: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Returns (present_cte, scoped_uc_join, scoped_uc_where) so the three
    aggregate endpoints can share a single source of truth for filtering.
    """
    where_clauses: list[str] = []
    if priority:
        where_clauses.append(f"uc.priority = '{_sql_escape(priority)}'")
    if status:
        where_clauses.append(
            f"COALESCE(uc.status, 'not_started') = "
            f"'{_sql_escape(_normalize_status(status))}'"
        )
    if department:
        where_clauses.append(
            f"COALESCE(NULLIF(TRIM(uc.department), ''), 'Unassigned') = "
            f"'{_sql_escape(department)}'"
        )
    if search:
        s = _sql_escape(search.lower())
        where_clauses.append(
            "("
            f"LOWER(uc.use_case_name) LIKE '%{s}%' "
            f"OR LOWER(COALESCE(uc.description,'')) LIKE '%{s}%'"
            ")"
        )
    if affiliate:
        aff_esc = _sql_escape(affiliate)
        join_sql = (
            f"JOIN {fqn(gold, 'use_case_affiliates')} ua_filter "
            f"ON ua_filter.use_case_id = uc.id "
            f"AND ua_filter.affiliate_name = '{aff_esc}'"
        )
        present_cte = _present_canonicals_cte(
            silver, f"pm.affiliate_name = '{aff_esc}'"
        )
    else:
        join_sql = ""
        present_cte = _present_canonicals_cte(silver)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    return present_cte, join_sql, where_sql


@router.get("/value/summary", operation_id="valueSummary")
async def value_summary(
    affiliate: Optional[str] = Query(None),
    formula: str = Query("simple", regex="^(simple|must)$"),
    priority: Optional[str] = Query(None),
    status: Optional[str] = Query(
        None,
        description="Optional pre-filter: only count use cases in this status",
    ),
    department: Optional[str] = Query(
        None,
        description="Filter to use cases owned by this department.",
    ),
    search: Optional[str] = Query(None),
) -> dict:
    """Page header KPIs: total value, ready value, gap value, count by bucket.

    Also returns a delivery-status rollup so the UI can show
    Realized (delivered) / In Flight (in_progress) / Opportunity (not_started)
    alongside the readiness-based Ready / Gap split.
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    _ensure_use_case_status_columns()
    present_cte, join_sql, where_sql = _build_value_filters(
        affiliate=affiliate, priority=priority, search=search, status=status,
        department=department, silver=silver, gold=gold,
    )

    # The single ratio expression we'll switch on per formula:
    if formula == "must":
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
        ),
        bucketed AS (
            SELECT id, value, status,
                   CASE
                       WHEN den IS NULL OR den = 0 THEN 'no_data'
                       WHEN num * 1.0 / den >= 1.00 THEN 'b100'
                       WHEN num * 1.0 / den >= 0.75 THEN 'b75'
                       WHEN num * 1.0 / den >= 0.50 THEN 'b50'
                       WHEN num * 1.0 / den >= 0.25 THEN 'b25'
                       WHEN num * 1.0 / den >  0.00 THEN 'b1'
                       ELSE 'b0'
                   END AS bucket,
                   CASE WHEN den IS NULL OR den = 0 THEN 0
                        ELSE num * 1.0 / den END AS ratio
            FROM per_uc
        )
        SELECT
            COUNT(*)                                            AS total_use_cases,
            COALESCE(SUM(value), 0)                             AS total_value,
            COALESCE(SUM(value * ratio), 0)                     AS ready_value,
            COALESCE(SUM(value * (1 - ratio)), 0)               AS gap_value,
            COUNT(CASE WHEN bucket = 'b100' THEN 1 END)         AS uc_b100,
            COUNT(CASE WHEN bucket = 'b75'  THEN 1 END)         AS uc_b75,
            COUNT(CASE WHEN bucket = 'b50'  THEN 1 END)         AS uc_b50,
            COUNT(CASE WHEN bucket = 'b25'  THEN 1 END)         AS uc_b25,
            COUNT(CASE WHEN bucket = 'b1'   THEN 1 END)         AS uc_b1,
            COUNT(CASE WHEN bucket = 'b0'   THEN 1 END)         AS uc_b0,
            COUNT(CASE WHEN bucket = 'no_data' THEN 1 END)      AS uc_nodata,
            COALESCE(SUM(CASE WHEN bucket='b100' THEN value END), 0) AS val_b100,
            COALESCE(SUM(CASE WHEN bucket='b75'  THEN value END), 0) AS val_b75,
            COALESCE(SUM(CASE WHEN bucket='b50'  THEN value END), 0) AS val_b50,
            COALESCE(SUM(CASE WHEN bucket='b25'  THEN value END), 0) AS val_b25,
            COALESCE(SUM(CASE WHEN bucket='b1'   THEN value END), 0) AS val_b1,
            COALESCE(SUM(CASE WHEN bucket='b0'   THEN value END), 0) AS val_b0,
            COUNT(CASE WHEN status='delivered'   THEN 1 END)    AS uc_delivered,
            COUNT(CASE WHEN status='in_progress' THEN 1 END)    AS uc_in_progress,
            COUNT(CASE WHEN status='not_started' THEN 1 END)    AS uc_not_started,
            COUNT(CASE WHEN status='on_hold'     THEN 1 END)    AS uc_on_hold,
            COALESCE(SUM(CASE WHEN status='delivered'   THEN value END), 0) AS val_delivered,
            COALESCE(SUM(CASE WHEN status='in_progress' THEN value END), 0) AS val_in_progress,
            COALESCE(SUM(CASE WHEN status='not_started' THEN value END), 0) AS val_not_started,
            COALESCE(SUM(CASE WHEN status='on_hold'     THEN value END), 0) AS val_on_hold
        FROM bucketed
    """
    try:
        rows = execute_query(sql)
        if not rows:
            return {"error": "no_rows"}
        r = rows[0]

        def fnum(k: str) -> float:
            v = r.get(k)
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        return {
            "filters": {
                "affiliate": affiliate, "priority": priority,
                "department": department,
                "search": search, "formula": formula, "status": status,
            },
            "total_use_cases": _to_int(r.get("total_use_cases")),
            "total_value": fnum("total_value"),
            "ready_value": fnum("ready_value"),
            "gap_value": fnum("gap_value"),
            "ready_pct": (
                round(100.0 * fnum("ready_value") / fnum("total_value"), 1)
                if fnum("total_value") > 0 else None
            ),
            # Delivery-status rollup: realized vs in-flight vs opportunity.
            # Sum(value) across these four equals total_value.
            "status_rollup": {
                "delivered": {
                    "use_cases": _to_int(r.get("uc_delivered")),
                    "value":     fnum("val_delivered"),
                },
                "in_progress": {
                    "use_cases": _to_int(r.get("uc_in_progress")),
                    "value":     fnum("val_in_progress"),
                },
                "not_started": {
                    "use_cases": _to_int(r.get("uc_not_started")),
                    "value":     fnum("val_not_started"),
                },
                "on_hold": {
                    "use_cases": _to_int(r.get("uc_on_hold")),
                    "value":     fnum("val_on_hold"),
                },
            },
            "buckets": [
                {"key": "100%",   "use_cases": _to_int(r.get("uc_b100")), "value": fnum("val_b100")},
                {"key": "75-99%", "use_cases": _to_int(r.get("uc_b75")),  "value": fnum("val_b75")},
                {"key": "50-74%", "use_cases": _to_int(r.get("uc_b50")),  "value": fnum("val_b50")},
                {"key": "25-49%", "use_cases": _to_int(r.get("uc_b25")),  "value": fnum("val_b25")},
                {"key": "1-24%",  "use_cases": _to_int(r.get("uc_b1")),   "value": fnum("val_b1")},
                {"key": "0%",     "use_cases": _to_int(r.get("uc_b0")),   "value": fnum("val_b0")},
                {"key": "no data","use_cases": _to_int(r.get("uc_nodata")), "value": 0.0},
            ],
        }
    except Exception as e:
        logger.warning(f"value_summary failed: {e}")
        return {"error": str(e)}


@router.get("/value/source-rollup", operation_id="valueSourceRollup")
async def value_source_rollup(
    affiliate: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    department: Optional[str] = Query(
        None,
        description="Filter to use cases owned by this department.",
    ),
    search: Optional[str] = Query(None),
    only_missing: bool = Query(
        False,
        description="If true, return only canonical sources NOT yet present in "
                    "the lake (the gap list - 'bring this in to unlock $X').",
    ),
) -> dict:
    """
    Per canonical source system: how many use cases require it, total $ value
    of those use cases, and whether it's currently in the lake.

    Powers the Source Pareto / 'gap-to-revenue' view: 'If you ingest X, you
    unlock Y use cases worth $Z.'
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    present_cte, join_sql, where_sql = _build_value_filters(
        affiliate=affiliate, priority=priority, search=search,
        department=department, silver=silver, gold=gold,
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
            COUNT(DISTINCT u.id)                            AS use_case_count,
            COALESCE(SUM(u.value), 0)                       AS total_value,
            SUM(CASE WHEN r.necessity='must_have' THEN 1 ELSE 0 END) AS must_have_links,
            COALESCE(SUM(CASE WHEN r.necessity='must_have' THEN u.value ELSE 0 END), 0)
                                                            AS must_have_value,
            CASE WHEN p.canonical IS NOT NULL THEN true ELSE false END AS is_present,
            COALESCE(c.category, 'Other')                   AS category
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
        rows = execute_query(sql)
    except Exception as e:
        logger.warning(f"value_source_rollup failed: {e}")
        return {"sources": []}

    out = []
    for r in rows:
        is_present = _to_bool(r.get("is_present"))
        if only_missing and is_present:
            continue
        out.append({
            "canonical": r.get("canonical"),
            "category": r.get("category"),
            "use_case_count": _to_int(r.get("use_case_count")),
            "total_value": float(r.get("total_value") or 0),
            "must_have_links": _to_int(r.get("must_have_links")),
            "must_have_value": float(r.get("must_have_value") or 0),
            "is_present": is_present,
        })
    return {
        "sources": out,
        "filters": {
            "affiliate": affiliate, "priority": priority,
            "department": department,
            "search": search, "only_missing": only_missing,
        },
    }


@router.get("/value/source/{canonical}", operation_id="valueSourceDetail")
async def value_source_detail(
    canonical: str,
    affiliate: Optional[str] = Query(None),
) -> dict:
    """
    Drill-down for a single canonical source system from the Value &
    Readiness perspective. Returns the use cases that require this source
    (with $ value, readiness, necessity, status) plus presence info and
    where it's already ingested in the lake.

    This is the 'click on a source node in the Sankey to see what it
    unlocks' endpoint. For the operational/lake-side detail (schemas,
    aliases, environments) the UI should fall back to /source-systems/{name}.
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    canonical_esc = _sql_escape(canonical)

    # Presence (optionally scoped to a single affiliate).
    if affiliate:
        aff_esc = _sql_escape(affiliate)
        present_cte = _present_canonicals_cte(
            silver, f"pm.affiliate_name = '{aff_esc}'"
        )
    else:
        present_cte = _present_canonicals_cte(silver)

    # Pull the metadata row + category.
    meta: dict = {"canonical": canonical}
    try:
        meta_rows = execute_query(
            f"""
            SELECT canonical AS name, category, description
            FROM {fqn(gold, 'source_system_canonical')}
            WHERE canonical = '{canonical_esc}'
            LIMIT 1
            """
        )
        if meta_rows:
            meta = {
                "canonical": meta_rows[0].get("name") or canonical,
                "category": meta_rows[0].get("category"),
                "description": meta_rows[0].get("description"),
            }
    except Exception as e:
        logger.warning(f"value_source_detail meta lookup failed: {e}")

    # Use cases that require this canonical, with $/readiness/necessity.
    sql = f"""
        WITH {present_cte},
        scoped_uc AS (
            SELECT uc.id, uc.use_case_name, uc.department, uc.priority,
                   COALESCE(uc.estimated_value_usd, 0) AS value,
                   COALESCE(uc.status, 'not_started') AS status
            FROM {fqn(silver, 'use_cases')} uc
        ),
        req_for_canon AS (
            SELECT use_case_id, MAX(necessity) AS necessity
            FROM {fqn(gold, 'use_case_source_requirements')}
            WHERE required_canonical = '{canonical_esc}'
            GROUP BY use_case_id
        ),
        all_req AS (
            SELECT u.id AS uc_id,
                   r.required_canonical, r.necessity,
                   CASE WHEN p.canonical IS NOT NULL THEN 1 ELSE 0 END AS is_present
            FROM scoped_uc u
            JOIN req_for_canon q ON q.use_case_id = u.id
            LEFT JOIN {fqn(gold, 'use_case_source_requirements')} r
                   ON r.use_case_id = u.id
            LEFT JOIN present p
                   ON p.canonical = r.required_canonical
                  AND r.required_canonical != 'Unmapped'
        ),
        readiness AS (
            SELECT uc_id,
                   COUNT(CASE WHEN required_canonical IS NOT NULL
                              AND required_canonical != 'Unmapped'
                              THEN 1 END) AS total,
                   SUM(CASE WHEN required_canonical != 'Unmapped'
                            THEN is_present ELSE 0 END) AS present_cnt,
                   COUNT(CASE WHEN necessity='must_have'
                              AND required_canonical != 'Unmapped'
                              THEN 1 END) AS must_total,
                   SUM(CASE WHEN necessity='must_have'
                            AND required_canonical != 'Unmapped'
                            THEN is_present ELSE 0 END) AS must_present
            FROM all_req
            GROUP BY uc_id
        )
        SELECT u.id, u.use_case_name, u.department, u.priority, u.value, u.status,
               q.necessity, r.total, r.present_cnt, r.must_total, r.must_present
        FROM scoped_uc u
        JOIN req_for_canon q ON q.use_case_id = u.id
        LEFT JOIN readiness r ON r.uc_id = u.id
        ORDER BY u.value DESC NULLS LAST, u.use_case_name
    """

    try:
        rows = execute_query(sql)
    except Exception as e:
        logger.warning(f"value_source_detail use cases failed: {e}")
        rows = []

    use_cases: list[dict] = []
    for r in rows:
        total = _to_int(r.get("total"))
        present_cnt = _to_int(r.get("present_cnt"))
        readiness_pct = (
            round(100.0 * present_cnt / total, 1) if total > 0 else None
        )
        use_cases.append({
            "id": r.get("id"),
            "use_case_name": r.get("use_case_name"),
            "department": r.get("department"),
            "priority": r.get("priority"),
            "value_usd": float(r.get("value") or 0),
            "status": _normalize_status(r.get("status")),
            "necessity": r.get("necessity"),
            "readiness_pct": readiness_pct,
            "missing_count": max(total - present_cnt, 0),
        })

    # Presence flag + a small set of lake locations (catalog.schema /
    # affiliate) so the drawer can show 'where it lives' / 'we don't have it'.
    is_present = False
    locations: list[dict] = []
    try:
        loc_rows = execute_query(
            f"""
            SELECT
                t.table_catalog AS catalog_name,
                t.table_schema AS schema_name,
                COALESCE(MAX(s.program), 'Unknown') AS affiliate,
                COALESCE(MAX(s.environment), '') AS environment,
                COUNT(*) AS table_count
            FROM {fqn(silver, 'silver_tables')} t
            LEFT JOIN {fqn(silver, 'silver_schemas')} s
                ON s.catalog_name = t.table_catalog
               AND s.schema_name = t.table_schema
            WHERE t.source_system_canonical = '{canonical_esc}'
            GROUP BY t.table_catalog, t.table_schema
            ORDER BY table_count DESC, catalog_name, schema_name
            LIMIT 25
            """
        )
        for l in loc_rows:
            locations.append({
                "catalog": l.get("catalog_name"),
                "schema": l.get("schema_name"),
                "affiliate": l.get("affiliate"),
                "environment": l.get("environment"),
                "table_count": _to_int(l.get("table_count")),
            })
        is_present = len(locations) > 0
    except Exception as e:
        logger.warning(f"value_source_detail locations failed: {e}")

    total_value = sum(u["value_usd"] for u in use_cases)
    must_value = sum(
        u["value_usd"] for u in use_cases if u["necessity"] == "must_have"
    )

    return {
        "meta": meta,
        "is_present": is_present,
        "use_cases": use_cases,
        "totals": {
            "use_case_count": len(use_cases),
            "total_value": total_value,
            "must_have_count": sum(
                1 for u in use_cases if u["necessity"] == "must_have"
            ),
            "must_have_value": must_value,
        },
        "locations": locations,
        "filters": {"affiliate": affiliate},
    }


@router.get("/value/sankey", operation_id="valueSankey")
async def value_sankey(
    affiliate: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    department: Optional[str] = Query(
        None,
        description="Filter to use cases owned by this department.",
    ),
    search: Optional[str] = Query(None),
    formula: str = Query("simple", regex="^(simple|must)$"),
    top_use_cases: int = Query(
        25, ge=5, le=200,
        description="Cap on number of use cases shown (highest $ first) to keep "
                    "the diagram readable.",
    ),
) -> dict:
    """
    Three-level value-weighted Sankey: source -> use case -> department.

    - Link width = $ value carried. Use-case value is distributed evenly
      across its required canonical sources for the source -> use-case
      links; the full use-case value flows through to its owning
      department.
    - Source nodes are colored by presence (red = missing canonical, the
      'investment unlock' signal).
    - Use case nodes are colored by readiness bucket.
    - Use cases without a department fall back to an 'Unassigned' bucket
      so they still show up on the right.
    Compatible with the existing SankeyDiagram component shape.
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    present_cte, join_sql, where_sql = _build_value_filters(
        affiliate=affiliate, priority=priority, search=search,
        department=department, silver=silver, gold=gold,
    )

    if formula == "must":
        num_expr = (
            "SUM(CASE WHEN necessity='must_have' AND required_canonical!='Unmapped' "
            "THEN is_present ELSE 0 END)"
        )
        den_expr = (
            "COUNT(CASE WHEN necessity='must_have' AND required_canonical!='Unmapped' "
            "THEN 1 END)"
        )
    else:
        num_expr = "SUM(CASE WHEN required_canonical!='Unmapped' THEN is_present ELSE 0 END)"
        den_expr = "COUNT(CASE WHEN required_canonical!='Unmapped' THEN 1 END)"

    sql = f"""
        WITH {present_cte},
        scoped_uc AS (
            SELECT uc.id, uc.use_case_name,
                   COALESCE(uc.estimated_value_usd, 0) AS value,
                   COALESCE(NULLIF(TRIM(uc.department), ''), 'Unassigned') AS department
            FROM {fqn(silver, 'use_cases')} uc
            {join_sql}
            {where_sql}
        ),
        req AS (
            SELECT u.id AS uc_id, u.use_case_name, u.value, u.department,
                   r.required_canonical, r.necessity,
                   CASE WHEN p.canonical IS NOT NULL THEN 1 ELSE 0 END AS is_present
            FROM scoped_uc u
            LEFT JOIN {fqn(gold, 'use_case_source_requirements')} r ON r.use_case_id = u.id
            LEFT JOIN present p
                   ON p.canonical = r.required_canonical
                  AND r.required_canonical != 'Unmapped'
        ),
        readiness AS (
            SELECT uc_id,
                   MAX(use_case_name) AS use_case_name,
                   MAX(value) AS value,
                   MAX(department) AS department,
                   {num_expr} AS num, {den_expr} AS den
            FROM req
            GROUP BY uc_id
        ),
        ranked_uc AS (
            SELECT uc_id, use_case_name, value, department, num, den,
                   ROW_NUMBER() OVER (ORDER BY value DESC) AS rnk
            FROM readiness
        ),
        top_uc AS (
            SELECT * FROM ranked_uc WHERE rnk <= {top_use_cases}
        ),
        uc_src AS (
            -- Strip 'Unmapped' / 'Other' canonicals - not actionable source
            -- systems, just LLM catch-all buckets. Same exclusion list as
            -- the source-rollup endpoint for consistency.
            SELECT u.uc_id, u.use_case_name, u.value, u.num, u.den,
                   r.required_canonical, r.necessity
            FROM top_uc u
            JOIN {fqn(gold, 'use_case_source_requirements')} r ON r.use_case_id = u.uc_id
            WHERE r.required_canonical NOT IN ('Unmapped', 'Other')
        ),
        uc_src_count AS (
            SELECT uc_id, COUNT(*) AS n_src FROM uc_src GROUP BY uc_id
        ),
        present_set AS (SELECT canonical FROM present),
        src_present AS (
            SELECT s.required_canonical AS canonical,
                   MAX(CASE WHEN p.canonical IS NOT NULL THEN 1 ELSE 0 END) AS is_present
            FROM uc_src s
            LEFT JOIN present_set p ON p.canonical = s.required_canonical
            GROUP BY s.required_canonical
        ),
        link_src_uc AS (
            SELECT s.required_canonical, s.uc_id, s.use_case_name,
                   SUM(s.value / NULLIF(sc.n_src, 0)) AS link_value
            FROM uc_src s
            JOIN uc_src_count sc ON sc.uc_id = s.uc_id
            GROUP BY s.required_canonical, s.uc_id, s.use_case_name
        )
        SELECT 'src_uc' AS kind, required_canonical AS a, uc_id AS b,
               link_value, uc_id, use_case_name,
               NULL AS uc_value, NULL AS num, NULL AS den, NULL AS department
        FROM link_src_uc
        UNION ALL
        SELECT 'uc_dept' AS kind, uc_id AS a, department AS b,
               value AS link_value, uc_id, use_case_name,
               value AS uc_value, num, den, department
        FROM top_uc
        UNION ALL
        SELECT 'src_meta' AS kind, canonical AS a, NULL AS b,
               CAST(is_present AS DOUBLE) AS link_value,
               NULL, NULL, NULL, NULL, NULL, NULL
        FROM src_present
    """

    try:
        rows = execute_query(sql)
    except Exception as e:
        logger.warning(f"value_sankey failed: {e}")
        return {"nodes": [], "links": [], "metadata": {"error": str(e)}}

    src_present_map: dict[str, bool] = {}
    src_uc_links: list[dict] = []
    uc_dept_links: list[dict] = []
    uc_meta: dict[str, dict] = {}

    for r in rows:
        kind = r.get("kind")
        if kind == "src_meta":
            src_present_map[r.get("a")] = bool(_to_int(r.get("link_value")))
        elif kind == "src_uc":
            v = float(r.get("link_value") or 0)
            uc_id = r.get("uc_id")
            if v > 0 and uc_id:
                src_uc_links.append({
                    "source": r.get("a"), "target": uc_id, "value": v,
                })
        elif kind == "uc_dept":
            v = float(r.get("link_value") or 0)
            uc_id = r.get("uc_id")
            dept = r.get("department") or "Unassigned"
            if uc_id:
                # Even for $0 use cases keep a tiny link so the node still
                # appears in the diagram (otherwise zero-value UCs vanish).
                uc_dept_links.append({
                    "source": uc_id, "target": dept,
                    "value": v if v > 0 else 1,
                })
                uc_meta[uc_id] = {
                    "name": r.get("use_case_name"),
                    "value": float(r.get("uc_value") or 0),
                    "num": _to_int(r.get("num")),
                    "den": _to_int(r.get("den")),
                    "department": dept,
                }

    sources = sorted({l["source"] for l in src_uc_links})
    use_cases = list(uc_meta.keys())
    departments = sorted({l["target"] for l in uc_dept_links})

    nodes = []
    for name in sources:
        is_present = src_present_map.get(name, False)
        nodes.append({
            "id": f"src::{name}",
            "name": name,
            "category": "source",
            "level": 0,
            "color": "hsl(174, 80%, 55%)" if is_present else "hsl(0, 75%, 55%)",
            "metadata": {"is_present": is_present},
        })
    for uc_id in use_cases:
        m = uc_meta[uc_id]
        ratio = (m["num"] / m["den"]) if m["den"] > 0 else 0
        if ratio >= 1.0:    color = "hsl(140, 60%, 45%)"
        elif ratio >= 0.75: color = "hsl(80, 60%, 50%)"
        elif ratio >= 0.50: color = "hsl(45, 80%, 55%)"
        elif ratio >= 0.25: color = "hsl(25, 80%, 55%)"
        else:               color = "hsl(0, 75%, 55%)"
        nodes.append({
            "id": f"uc::{uc_id}",
            "name": m["name"],
            "category": "use_case",
            "level": 1,
            "color": color,
            "metadata": {
                "value_usd": m["value"],
                "readiness_pct": round(100 * ratio, 1),
                "num": m["num"], "den": m["den"],
                "department": m["department"],
            },
        })
    for name in departments:
        nodes.append({
            "id": f"dept::{name}",
            "name": name,
            "category": "department",
            "level": 2,
            "color": "hsl(350, 75%, 60%)",
        })

    links = []
    for l in src_uc_links:
        links.append({
            "source": f"src::{l['source']}",
            "target": f"uc::{l['target']}",
            "value": l["value"],
            "color": "rgba(120, 200, 180, 0.30)",
        })
    for l in uc_dept_links:
        links.append({
            "source": f"uc::{l['source']}",
            "target": f"dept::{l['target']}",
            "value": l["value"],
            "color": "rgba(160, 140, 220, 0.25)",
        })

    return {
        "nodes": nodes,
        "links": links,
        "metadata": {
            "source_count": len(sources),
            "use_case_count": len(use_cases),
            "department_count": len(departments),
            "missing_source_count": sum(
                1 for n in nodes
                if n["category"] == "source" and not n["metadata"]["is_present"]
            ),
            "filters": {
                "affiliate": affiliate, "priority": priority,
                "department": department,
                "search": search, "formula": formula,
                "top_use_cases": top_use_cases,
            },
        },
    }


# ---------------------------------------------------------------------------
# Gaps matrix (Insights > Gaps)
#
# Pivots canonical source systems against BHE affiliates to surface
# whitespace: cells where an affiliate needs a canonical (because a use case
# applicable to that affiliate requires it) but the canonical is not present
# in that affiliate's lake slice.
#
# Cell states:
#   - 'covered'   : needed by a UC for this affiliate AND present in the lake
#   - 'gap'       : needed by a UC for this affiliate AND missing (RED - the
#                   whitespace the business should prioritize)
#   - 'available' : present in the lake but no UC applicable to this affiliate
#                   needs it (dim - opportunity for new use cases)
#   - not returned (sparse): neither required nor present
#
# Affiliate presence uses the same program_affiliate_map join as the Value
# endpoints, so 'presence' is genuinely scoped to the affiliate's portion of
# the catalog, not a global "is this canonical anywhere in the lake".
# ---------------------------------------------------------------------------


@router.get("/gaps/matrix", operation_id="gapsMatrix")
async def gaps_matrix() -> dict:
    """Canonical source x affiliate coverage matrix."""
    silver = get_silver_schema()
    gold = get_gold_schema()

    sql = f"""
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
            -- Skip 'Multi-Affiliate' meta-tag; it's not a real BHE entity
            -- you can target with a rollout plan.
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
        WHERE r.canonical IS NOT NULL OR p.canonical IS NOT NULL
        ORDER BY c.canonical, a.affiliate_name
    """

    try:
        rows = execute_query(sql)
    except Exception as e:
        logger.warning(f"gaps_matrix failed: {e}")
        return {"error": str(e), "canonicals": [], "affiliates": [], "cells": []}

    # Pull canonical category + affiliate dimension attrs in parallel for
    # richer tooltips on the client.
    try:
        cat_rows = execute_query(
            f"SELECT canonical, category FROM {fqn(gold, 'source_system_canonical')} "
            f"WHERE COALESCE(is_active, true) = true"
        )
        categories = {r.get("canonical"): r.get("category") for r in cat_rows}
    except Exception:
        categories = {}

    try:
        aff_rows = execute_query(
            f"SELECT affiliate_name, affiliate_code, business_type, region, description "
            f"FROM {fqn(gold, 'affiliates')} "
            f"WHERE COALESCE(is_active, true) = true "
            f"  AND affiliate_name NOT IN ('Multi-Affiliate') "
            f"ORDER BY affiliate_name"
        )
    except Exception:
        aff_rows = []

    # Aggregate cells into the matrix payload.
    cells: list[dict] = []
    canonical_stats: dict[str, dict] = {}
    affiliate_stats: dict[str, dict] = {
        r.get("affiliate_name"): {
            "affiliate_name": r.get("affiliate_name"),
            "affiliate_code": r.get("affiliate_code"),
            "business_type": r.get("business_type"),
            "region": r.get("region"),
            "description": r.get("description"),
            "required_count": 0,
            "present_count": 0,
            "gap_count": 0,
            "available_count": 0,
        }
        for r in aff_rows
    }

    for r in rows:
        canonical = r.get("canonical")
        affiliate = r.get("affiliate_name")
        is_required = _to_bool(r.get("is_required"))
        is_present = _to_bool(r.get("is_present"))
        uc_count = _to_int(r.get("uc_count"))
        must_count = _to_int(r.get("must_count"))
        try:
            total_value = float(r.get("total_value") or 0)
        except (TypeError, ValueError):
            total_value = 0.0

        if is_required and is_present:
            state = "covered"
        elif is_required and not is_present:
            state = "gap"
        elif not is_required and is_present:
            state = "available"
        else:
            continue  # shouldn't happen due to WHERE clause

        cells.append({
            "canonical": canonical,
            "affiliate": affiliate,
            "state": state,
            "uc_count": uc_count,
            "must_count": must_count,
            "total_value": total_value,
            "is_required": is_required,
            "is_present": is_present,
        })

        cs = canonical_stats.setdefault(canonical, {
            "name": canonical,
            "category": categories.get(canonical),
            "affiliates_needing": 0,
            "affiliates_present": 0,
            "affiliates_gap": 0,
            "total_use_cases": 0,
            "total_must_links": 0,
            "total_value_affected": 0.0,
        })
        if is_required:
            cs["affiliates_needing"] += 1
            cs["total_use_cases"] += uc_count
            cs["total_must_links"] += must_count
            cs["total_value_affected"] += total_value
        if is_present:
            cs["affiliates_present"] += 1
        if state == "gap":
            cs["affiliates_gap"] += 1

        ast = affiliate_stats.get(affiliate)
        if ast:
            if is_required:
                ast["required_count"] += 1
            if is_present:
                ast["present_count"] += 1
            if state == "gap":
                ast["gap_count"] += 1
            if state == "available":
                ast["available_count"] += 1

    # Stable ordering for the UI: canonicals with most gaps first,
    # then most use cases; affiliates by the seed ordering from the dim table.
    canonicals = sorted(
        canonical_stats.values(),
        key=lambda c: (
            -c["affiliates_gap"],
            -c["total_use_cases"],
            -c["total_value_affected"],
            c["name"] or "",
        ),
    )
    affiliates = list(affiliate_stats.values())

    return {
        "canonicals": canonicals,
        "affiliates": affiliates,
        "cells": cells,
        "summary": {
            "canonical_count": len(canonicals),
            "affiliate_count": len(affiliates),
            "gap_count": sum(1 for c in cells if c["state"] == "gap"),
            "covered_count": sum(1 for c in cells if c["state"] == "covered"),
            "available_count": sum(1 for c in cells if c["state"] == "available"),
            "total_gap_value": sum(
                c["total_value"] for c in cells if c["state"] == "gap"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Sankey
# ---------------------------------------------------------------------------


@router.get("/sankey/data", operation_id="sankeyData")
async def sankey_data(
    department: Optional[str] = Query(None),
    use_case: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
) -> SankeyDataOut:
    silver = get_silver_schema()
    conditions = ["1=1"]
    if department:
        conditions.append(f"department = '{department}'")
    if use_case:
        conditions.append(f"use_case = '{use_case}'")
    if source:
        conditions.append(f"source_system = '{source}'")
    where = " AND ".join(conditions)

    try:
        mappings = execute_query(
            f"SELECT * FROM {fqn(silver, 'sankey_mappings')} WHERE {where}"
        )
    except Exception:
        return SankeyDataOut(nodes=[], links=[], metadata={})

    sources_set: dict[str, str] = {}
    entity_set: set[str] = set()
    uc_set: set[str] = set()
    dept_set: set[str] = set()
    unmapped_entities: set[str] = set()

    for m in mappings:
        src = m.get("source_system", "")
        ent = m.get("entity_name", "")
        sources_set[src] = m.get("source_category", "")
        if ent:
            entity_set.add(ent)
        uc_set.add(m["use_case"])
        dept_set.add(m["department"])
        if src == "UNMAPPED" and ent:
            unmapped_entities.add(ent)

    has_entities = bool(entity_set)

    schema_rows: list[dict] = []
    try:
        schema_rows = execute_query(
            f"SELECT catalog_name, schema_name, "
            f"COALESCE(business_friendly_name, '') AS business_friendly_name, "
            f"COALESCE(program, '') AS program "
            f"FROM {fqn(silver, 'silver_schemas')} "
            f"WHERE COALESCE(classification, '') = 'PRODUCTION' "
            f"AND COALESCE(environment, '') != 'SYSTEM'"
        )
    except Exception:
        schema_rows = []

    NOISE_TOKENS = {
        "prod", "dev", "qa", "sbx", "uat", "test", "tmp", "temp",
        "landing", "standardized", "published", "discovery", "archived",
        "config", "analytics", "dwh", "ods", "raw", "silver", "gold", "bronze",
        "data", "db", "lake", "lakehouse", "system", "internal", "default",
        "schema", "catalog", "table", "view", "model", "modeling",
    }

    def _meaningful_tokens(text: str) -> set[str]:
        toks = {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 4}
        return {t for t in toks if t not in NOISE_TOKENS and not t.isdigit()
                and not re.fullmatch(r"[a-z]+\d+", t)}

    def _match_schemas_for_source(src_name: str) -> tuple[list[str], list[dict]]:
        """Match silver_schemas rows to a source_system label.

        Strategy (in order of precedence):
          1. Exact catalog.schema FQN match (case-insensitive).
          2. Exact catalog or schema name match.
          3. Substring match against business_friendly_name or program
             (both directions, ignoring noise tokens like env/zone names).
        """
        if not src_name or not schema_rows:
            return [], []

        s = src_name.lower().strip()
        s_clean = re.sub(r"\s*\([^)]*\)", "", s).strip()
        candidates = {s, s_clean}
        affiliates: set[str] = set()
        schemas: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def _add(r: dict) -> None:
            key = (r.get("catalog_name", ""), r.get("schema_name", ""))
            if key in seen:
                return
            seen.add(key)
            if r.get("program"):
                affiliates.add(r["program"])
            schemas.append({
                "catalog": r.get("catalog_name", ""),
                "schema": r.get("schema_name", ""),
                "fqn": f"{r.get('catalog_name', '')}.{r.get('schema_name', '')}",
                "business_friendly_name": r.get("business_friendly_name", ""),
                "program": r.get("program", ""),
            })

        # Pass 1: exact FQN / catalog / schema match
        for r in schema_rows:
            cat = (r.get("catalog_name") or "").lower()
            sch = (r.get("schema_name") or "").lower()
            fq = f"{cat}.{sch}"
            if fq in candidates or cat in candidates or sch in candidates:
                _add(r)
        if schemas:
            schemas.sort(key=lambda x: (x["program"], x["fqn"]))
            return sorted(affiliates), schemas

        # Pass 2: substring (catalog/schema) — only if src looks like an FQN-ish identifier
        if re.search(r"[._]", s) and not re.search(r"\s", s):
            for r in schema_rows:
                cat = (r.get("catalog_name") or "").lower()
                sch = (r.get("schema_name") or "").lower()
                if cat and (cat in s or s in cat) and len(cat) >= 5:
                    _add(r)
                    continue
                if sch and (sch in s and len(sch) >= 5):
                    _add(r)
            if schemas:
                schemas.sort(key=lambda x: (x["program"], x["fqn"]))
                return sorted(affiliates), schemas

        # Pass 3: business_friendly_name / program substring match (ignoring noise)
        s_tokens = _meaningful_tokens(s)
        for r in schema_rows:
            bfn = (r.get("business_friendly_name") or "").lower()
            prog = (r.get("program") or "").lower()
            sub_match = any(
                cand and (cand in s_clean or s_clean in cand)
                for cand in (bfn, prog) if cand and len(cand) >= 4
            )
            if sub_match:
                _add(r)
                continue
            cand_tokens = _meaningful_tokens(bfn) | _meaningful_tokens(prog)
            if s_tokens and cand_tokens and (s_tokens & cand_tokens):
                _add(r)

        schemas.sort(key=lambda x: (x["program"], x["fqn"]))
        return sorted(affiliates), schemas

    # Roll up table-level source_system to schema level. Prefer the
    # normalized canonical produced by the normalize_source_systems WF
    # (covers ~97% of tables, ~59 distinct systems). Falls back to the
    # raw LLM label so this query stays correct even if normalization
    # hasn't run.
    schema_to_system: dict[str, str] = {}
    try:
        sys_rows = execute_query(
            f"""WITH ranked AS (
                SELECT
                    lower(table_catalog || '.' || table_schema) AS sch,
                    COALESCE(source_system_canonical, source_system) AS source_system,
                    COUNT(*) AS n
                FROM {fqn(silver, 'silver_tables')}
                WHERE COALESCE(source_system_canonical, source_system, '') != ''
                  AND upper(COALESCE(source_system_canonical, source_system))
                      NOT IN ('UNKNOWN', 'N/A', 'NA', 'NONE')
                GROUP BY 1, 2
            ),
            top_per_sch AS (
                SELECT sch, source_system,
                       ROW_NUMBER() OVER (
                           PARTITION BY sch
                           ORDER BY n DESC, source_system
                       ) AS rn
                FROM ranked
            )
            SELECT sch, source_system FROM top_per_sch WHERE rn = 1"""
        )
        for r in sys_rows:
            schema_to_system[r["sch"]] = r["source_system"]
    except Exception:
        pass

    def _resolve_system(src_name: str, src_category: str) -> str:
        """Resolve an LLM source_system label (often an FQN) to a real system name."""
        if not src_name:
            return src_category or "Other"
        s = src_name.lower().strip()
        s_clean = re.sub(r"\s*\([^)]*\)", "", s).strip()
        if s in schema_to_system:
            return schema_to_system[s]
        if s_clean in schema_to_system:
            return schema_to_system[s_clean]
        # FQN-shaped: try substring against any known schema FQN
        if "." in s or "_" in s:
            for fq, sys_name in schema_to_system.items():
                if fq in s or (len(fq) >= 8 and s in fq):
                    return sys_name
        # If src_name doesn't look like an FQN, trust the LLM label
        if (
            "." not in s
            and not re.search(r"_(prod|dev|qa|sbx|uat)\d", s)
            and len(src_name) <= 60
        ):
            return src_name
        # Last resort: bucket by category so we still get a sensible label
        return src_category or "Other"

    # Group mapping FQNs by resolved system
    src_fqn_to_system: dict[str, str] = {}
    for src_fqn, cat in sources_set.items():
        if src_fqn == "UNMAPPED":
            continue
        src_fqn_to_system[src_fqn] = _resolve_system(src_fqn, cat)

    system_meta: dict[str, dict] = {}
    for src_fqn, system in src_fqn_to_system.items():
        cat = sources_set.get(src_fqn, "")
        affiliates, sch_list = _match_schemas_for_source(src_fqn)
        meta = system_meta.setdefault(
            system,
            {
                "category": "",
                "categories": set(),
                "affiliates": set(),
                "schemas": [],
                "schema_seen": set(),
                "source_fqns": [],
            },
        )
        if cat:
            meta["categories"].add(cat)
            meta["category"] = meta["category"] or cat
        meta["affiliates"].update(affiliates)
        meta["source_fqns"].append(src_fqn)
        for s in sch_list:
            if s["fqn"] not in meta["schema_seen"]:
                meta["schema_seen"].add(s["fqn"])
                meta["schemas"].append(s)

    nodes: list[SankeyNodeOut] = []
    for system, meta in system_meta.items():
        nodes.append(SankeyNodeOut(
            id=f"src_{system}", name=system, category="source",
            level=0, color=SANKEY_COLORS["source"],
            metadata={
                "source_category": meta["category"],
                "categories": sorted(meta["categories"]),
                "affiliates": sorted(meta["affiliates"]),
                "schemas": sorted(meta["schemas"], key=lambda x: (x["program"], x["fqn"])),
                "schema_count": len(meta["schemas"]),
                "source_fqns": sorted(meta["source_fqns"]),
            },
        ))
    if has_entities:
        for ent in sorted(entity_set):
            is_gap = ent in unmapped_entities
            nodes.append(SankeyNodeOut(
                id=f"ent_{ent}", name=ent, category="entity",
                level=1, color="#ef4444" if is_gap else SANKEY_COLORS.get("entity", "#8b5cf6"),
                metadata={"is_gap": is_gap},
            ))
    for uc in sorted(uc_set):
        nodes.append(SankeyNodeOut(
            id=f"uc_{uc}", name=uc, category="use_case",
            level=2 if has_entities else 1,
            color=SANKEY_COLORS["use_case"],
        ))
    for dept in sorted(dept_set):
        nodes.append(SankeyNodeOut(
            id=f"dept_{dept}", name=dept, category="department",
            level=3 if has_entities else 2,
            color=SANKEY_COLORS["department"],
        ))

    from collections import Counter
    src_ent_counts: Counter = Counter()
    ent_uc_counts: Counter = Counter()
    src_uc_counts: Counter = Counter()
    uc_dept_counts: Counter = Counter()

    for m in mappings:
        src = m.get("source_system", "")
        ent = m.get("entity_name", "")
        uc = m["use_case"]
        dept = m["department"]
        system = src_fqn_to_system.get(src) if src and src != "UNMAPPED" else None

        if has_entities and ent:
            if system:
                src_ent_counts[(f"src_{system}", f"ent_{ent}")] += 1
            ent_uc_counts[(f"ent_{ent}", f"uc_{uc}")] += 1
        else:
            if system:
                src_uc_counts[(f"src_{system}", f"uc_{uc}")] += 1
        uc_dept_counts[(f"uc_{uc}", f"dept_{dept}")] += 1

    links: list[SankeyLinkOut] = []
    if has_entities:
        for (s, t), v in src_ent_counts.items():
            links.append(SankeyLinkOut(
                source=s, target=t, value=v,
                color=f"{SANKEY_COLORS['source']}80",
            ))
        for (s, t), v in ent_uc_counts.items():
            is_gap = s.replace("ent_", "") in unmapped_entities
            links.append(SankeyLinkOut(
                source=s, target=t, value=v,
                color="#ef444480" if is_gap else "#8b5cf680",
            ))
    else:
        for (s, t), v in src_uc_counts.items():
            links.append(SankeyLinkOut(
                source=s, target=t, value=v,
                color=f"{SANKEY_COLORS['source']}80",
            ))
    for (s, t), v in uc_dept_counts.items():
        links.append(SankeyLinkOut(
            source=s, target=t, value=v,
            color=f"{SANKEY_COLORS['use_case']}80",
        ))

    return SankeyDataOut(
        nodes=nodes,
        links=links,
        metadata={
            "total_sources": len(system_meta),
            "total_source_schemas": len([s for s in sources_set if s != "UNMAPPED"]),
            "total_entities": len(entity_set),
            "total_use_cases": len(uc_set),
            "total_departments": len(dept_set),
            "total_mappings": len(mappings),
            "gap_count": len(unmapped_entities),
            "gaps": sorted(unmapped_entities),
        },
    )


@router.get("/sankey/filters", operation_id="sankeyFilters")
async def sankey_filters() -> dict:
    silver = get_silver_schema()
    try:
        depts = execute_query(
            f"SELECT DISTINCT department as value FROM {fqn(silver, 'sankey_mappings')} ORDER BY value"
        )
        ucs = execute_query(
            f"SELECT DISTINCT use_case as value FROM {fqn(silver, 'sankey_mappings')} ORDER BY value"
        )
        sources = execute_query(
            f"SELECT DISTINCT source_system as value FROM {fqn(silver, 'sankey_mappings')} ORDER BY value"
        )
        cats = execute_query(
            f"SELECT DISTINCT source_category as value FROM {fqn(silver, 'sankey_mappings')} WHERE source_category IS NOT NULL ORDER BY value"
        )
        return {
            "departments": [r["value"] for r in depts],
            "use_cases": [r["value"] for r in ucs],
            "sources": [r["value"] for r in sources],
            "source_categories": [r["value"] for r in cats],
        }
    except Exception:
        return {"departments": [], "use_cases": [], "sources": [], "source_categories": []}


@router.post("/sankey/mappings", operation_id="createSankeyMapping")
async def create_sankey_mapping(body: SankeyMappingIn) -> dict:
    silver = get_silver_schema()
    new_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    execute_query(
        f"""
        INSERT INTO {fqn(silver, 'sankey_mappings')}
        (id, source_system, source_category, use_case, department, relevance,
         company_name, is_user_edited, created_at)
        VALUES ('{new_id}', '{body.source_system}', '{body.source_category}',
                '{body.use_case}', '{body.department}', '{body.relevance}',
                '', true, '{now}')
        """
    )
    return {"status": "created", "id": new_id}


@router.put("/sankey/mappings/{mapping_id}", operation_id="updateSankeyMapping")
async def update_sankey_mapping(mapping_id: str, body: SankeyMappingUpdateIn) -> dict:
    silver = get_silver_schema()
    updates = []
    if body.source_system is not None:
        updates.append(f"source_system = '{body.source_system}'")
    if body.source_category is not None:
        updates.append(f"source_category = '{body.source_category}'")
    if body.use_case is not None:
        updates.append(f"use_case = '{body.use_case}'")
    if body.department is not None:
        updates.append(f"department = '{body.department}'")
    if body.relevance is not None:
        updates.append(f"relevance = '{body.relevance}'")

    if not updates:
        raise HTTPException(400, "No fields to update")

    updates.append("is_user_edited = true")
    set_clause = ", ".join(updates)

    execute_query(
        f"UPDATE {fqn(silver, 'sankey_mappings')} SET {set_clause} WHERE id = '{mapping_id}'"
    )
    return {"status": "updated", "id": mapping_id}


@router.delete("/sankey/mappings/{mapping_id}", operation_id="deleteSankeyMapping")
async def delete_sankey_mapping(mapping_id: str) -> dict:
    silver = get_silver_schema()
    execute_query(
        f"DELETE FROM {fqn(silver, 'sankey_mappings')} WHERE id = '{mapping_id}'"
    )
    return {"status": "deleted", "id": mapping_id}


# ---------------------------------------------------------------------------
# Company Research
# ---------------------------------------------------------------------------


@router.get("/company/profile", operation_id="companyProfile")
async def company_profile() -> CompanyProfileOut:
    silver = get_silver_schema()
    _ensure_branding_columns()
    try:
        rows = execute_query(
            f"SELECT * FROM {fqn(silver, 'company_profile')} LIMIT 1"
        )
        if not rows:
            return CompanyProfileOut()
        r = rows[0]
        return CompanyProfileOut(
            id=r.get("id", ""),
            company_name=r.get("company_name", ""),
            industry=r.get("industry", ""),
            sub_industry=r.get("sub_industry", ""),
            description=r.get("description", ""),
            headquarters=r.get("headquarters", ""),
            key_business_units=json.loads(r.get("key_business_units", "[]")),
            strategic_priorities=json.loads(r.get("strategic_priorities", "[]")),
            regulatory_environment=r.get("regulatory_environment", ""),
            catalog_name=r.get("catalog_name", "") or "",
            logo_url=r.get("logo_url", "") or "",
            primary_domain=r.get("primary_domain", "") or "",
            branding_user_edited=bool(r.get("branding_user_edited") in (True, "true", 1, "1")),
        )
    except Exception:
        return CompanyProfileOut()


# ---------------------------------------------------------------------------
# Branding (multi-tenant catalog name + logo)
# ---------------------------------------------------------------------------

# Logos are stored as a subfolder of the existing uploads Volume (which
# already exists in the bundle) at a deterministic basename so the GET
# endpoint can find them without tracking filenames in the DB. We keep the
# original extension so content-type can be served correctly.
_LOGO_VOLUME_SUBDIR = "branding"
_LOGO_BASENAME = "logo"
_LOGO_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"}
_LOGO_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _branding_volume_dir() -> str:
    """Reuses the existing uploads Volume + a 'branding/' subfolder."""
    return f"/Volumes/{get_catalog()}/{get_raw_schema()}/uploads/{_LOGO_VOLUME_SUBDIR}"


def _find_uploaded_logo() -> tuple[str, str] | None:
    """Return (volume_path, extension) of the uploaded logo, if any."""
    try:
        rows = execute_query(f"LIST '{_branding_volume_dir()}'")
    except Exception:
        # Most common reason: the subfolder doesn't exist yet (nothing uploaded).
        return None
    for r in rows or []:
        name = (r.get("name") or "").lower()
        if not name.startswith(_LOGO_BASENAME + "."):
            continue
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if ext in _LOGO_ALLOWED_EXTS:
            return (r.get("path") or f"{_branding_volume_dir()}/{name}"), ext
    return None


@router.get("/branding", response_model=BrandingOut, operation_id="getBranding")
async def get_branding() -> BrandingOut:
    """Public, cacheable: returns the catalog name + logo URL for the top banner.

    If the user has uploaded a logo to the Volume, ``logo_url`` resolves to
    our own ``/api/branding/logo`` endpoint (so the browser pulls bytes
    through the backend). Otherwise it returns whatever URL is stored in
    company_profile.logo_url (e.g. a Clearbit URL).
    """
    silver = get_silver_schema()
    _ensure_branding_columns()
    catalog_name = ""
    logo_url = ""
    try:
        rows = execute_query(
            f"SELECT catalog_name, logo_url, company_name "
            f"FROM {fqn(silver, 'company_profile')} LIMIT 1"
        )
        if rows:
            r = rows[0]
            catalog_name = (r.get("catalog_name") or "").strip()
            logo_url = (r.get("logo_url") or "").strip()
            if not catalog_name and r.get("company_name"):
                catalog_name = f"{r['company_name']} Data Catalog"
    except Exception as e:
        logger.warning(f"get_branding failed to read profile: {e}")

    has_uploaded = _find_uploaded_logo() is not None
    # An uploaded logo always wins over a stored URL — the user explicitly
    # picked it, and serving via our endpoint avoids cross-origin / CORS / CSP
    # surprises with public-internet URLs.
    if has_uploaded:
        logo_url = "/api/branding/logo"

    return BrandingOut(
        catalog_name=catalog_name,
        logo_url=logo_url,
        has_uploaded_logo=has_uploaded,
    )


@router.put("/company/branding", operation_id="updateBranding")
async def update_branding(body: BrandingUpdateIn) -> dict:
    """Save manual branding overrides. Sets branding_user_edited=true so a
    subsequent company-research run will not overwrite these values."""
    silver = get_silver_schema()
    table = _ensure_branding_columns()

    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "No fields to update")

    # Only allow the two whitelisted fields to be set via this endpoint;
    # everything else on the profile flows through company research.
    allowed = {"catalog_name", "logo_url"}
    bad = set(data) - allowed
    if bad:
        raise HTTPException(400, f"Cannot update fields: {sorted(bad)}")

    # If the row doesn't exist yet (user is branding before running research),
    # create a stub so the UPDATE has something to hit.
    rows = execute_query(f"SELECT id FROM {table} LIMIT 1")
    if not rows:
        execute_query(
            f"INSERT INTO {table} (id, company_name, branding_user_edited) "
            f"VALUES ('manual', '', true)"
        )

    sets = [f"{k} = '{_sql_escape(str(v))}'" for k, v in data.items()]
    sets.append("branding_user_edited = true")
    execute_query(f"UPDATE {table} SET {', '.join(sets)}")
    return {"status": "updated", **data}


@router.post("/branding/logo", operation_id="uploadBrandingLogo")
async def upload_branding_logo(file: UploadFile = File(...)) -> dict:
    """Upload a logo image to the branding Volume directory. Replaces any
    existing logo (we keep at most one). Sets branding_user_edited=true and
    points logo_url at /api/branding/logo so the new bytes are served."""
    if not file.filename:
        raise HTTPException(400, "Filename required")
    ext = ("." + file.filename.rsplit(".", 1)[-1].lower()) if "." in file.filename else ""
    if ext not in _LOGO_ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported logo type: {ext or 'unknown'}. Allowed: {sorted(_LOGO_ALLOWED_EXTS)}")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "Logo must be under 5 MB")

    from .db import _get_headers, _get_host
    import requests as req
    host = _get_host()
    headers = _get_headers()

    # Delete any prior logo (different extension) so only the freshest one
    # exists; otherwise _find_uploaded_logo() could return the stale file.
    prev = _find_uploaded_logo()
    if prev:
        prev_path, _ = prev
        try:
            req.delete(f"{host}/api/2.0/fs/files{prev_path}", headers=headers)
        except Exception as e:
            logger.warning(f"Could not delete prior logo {prev_path}: {e}")

    target = f"{_branding_volume_dir()}/{_LOGO_BASENAME}{ext}"
    resp = req.put(
        f"{host}/api/2.0/fs/files{target}?overwrite=true",
        headers={**headers, "Content-Type": "application/octet-stream"},
        data=content,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(502, f"Volume upload failed: {resp.status_code} {resp.text[:200]}")

    table = _ensure_branding_columns()
    rows = execute_query(f"SELECT id FROM {table} LIMIT 1")
    if not rows:
        execute_query(
            f"INSERT INTO {table} (id, company_name, branding_user_edited) "
            f"VALUES ('manual', '', true)"
        )
    execute_query(
        f"UPDATE {table} SET logo_url = '/api/branding/logo', "
        f"branding_user_edited = true"
    )
    return {"status": "uploaded", "path": target, "size": len(content), "logo_url": "/api/branding/logo"}


@router.delete("/branding/logo", operation_id="deleteBrandingLogo")
async def delete_branding_logo() -> dict:
    """Remove the uploaded logo. Falls back to whatever URL was previously
    suggested by AI on next refresh (logo_url is cleared)."""
    from .db import _get_headers, _get_host
    import requests as req
    prev = _find_uploaded_logo()
    if prev:
        prev_path, _ = prev
        try:
            req.delete(f"{_get_host()}/api/2.0/fs/files{prev_path}", headers=_get_headers())
        except Exception as e:
            logger.warning(f"Could not delete logo {prev_path}: {e}")

    table = _ensure_branding_columns()
    execute_query(f"UPDATE {table} SET logo_url = ''")
    return {"status": "deleted"}


@router.get("/branding/logo", operation_id="getBrandingLogo")
async def get_branding_logo():
    """Stream the uploaded logo file from the Volume."""
    found = _find_uploaded_logo()
    if not found:
        raise HTTPException(404, "No logo uploaded")
    vol_path, ext = found

    from .db import _get_headers, _get_host
    import requests as req
    resp = req.get(
        f"{_get_host()}/api/2.0/fs/files{vol_path}",
        headers=_get_headers(),
        stream=True,
    )
    if resp.status_code != 200:
        logger.warning(f"get_branding_logo: Volume read returned {resp.status_code}: {resp.text[:200]}")
        raise HTTPException(502, f"Failed to read logo from Volume: {resp.status_code}")

    content_type = _LOGO_CONTENT_TYPES.get(ext, "application/octet-stream")

    def _iter():
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        # Short cache so logo updates show up quickly while still avoiding a
        # round-trip on every page load.
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/company/departments", operation_id="listDepartments")
async def list_departments() -> list[DepartmentOut]:
    silver = get_silver_schema()
    try:
        rows = execute_query(
            f"SELECT * FROM {fqn(silver, 'departments')} ORDER BY department_name"
        )
        return [
            DepartmentOut(
                id=r.get("id", ""),
                department_name=r.get("department_name", ""),
                description=r.get("description", ""),
                key_functions=json.loads(r.get("key_functions", "[]")),
                data_needs=r.get("data_needs", ""),
                is_user_edited=r.get("is_user_edited", False),
            )
            for r in rows
        ]
    except Exception:
        return []


@router.put("/company/departments/{dept_id}", operation_id="updateDepartment")
async def update_department(dept_id: str, body: DepartmentUpdateIn) -> dict:
    silver = get_silver_schema()
    updates = []
    if body.department_name is not None:
        updates.append(f"department_name = '{body.department_name}'")
    if body.description is not None:
        updates.append(f"description = '{body.description}'")
    if body.data_needs is not None:
        updates.append(f"data_needs = '{body.data_needs}'")
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates.append("is_user_edited = true")
    set_clause = ", ".join(updates)
    execute_query(
        f"UPDATE {fqn(silver, 'departments')} SET {set_clause} WHERE id = '{dept_id}'"
    )
    return {"status": "updated"}


@router.get("/company/use-cases", operation_id="listUseCases")
async def list_use_cases(
    department: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="Filter by delivery status"),
) -> list[UseCaseOut]:
    silver = get_silver_schema()
    _ensure_use_case_status_columns()
    conditions = ["1=1"]
    if department:
        conditions.append(f"department = '{_sql_escape(department)}'")
    if category:
        conditions.append(f"category = '{_sql_escape(category)}'")
    if status:
        conditions.append(
            f"COALESCE(status, 'not_started') = '{_sql_escape(_normalize_status(status))}'"
        )
    where = " AND ".join(conditions)
    try:
        rows = execute_query(
            f"SELECT * FROM {fqn(silver, 'use_cases')} WHERE {where} ORDER BY priority, use_case_name"
        )

        def _raw_reqs(v):
            try:
                return json.loads(v) if isinstance(v, str) else (v or [])
            except Exception:
                return []

        return [
            UseCaseOut(
                id=r.get("id", ""),
                use_case_name=r.get("use_case_name", ""),
                description=r.get("description", ""),
                department=r.get("department", ""),
                category=r.get("category", ""),
                business_value=r.get("business_value", ""),
                estimated_value_usd=r.get("estimated_value_usd"),
                value_rationale=r.get("value_rationale", ""),
                data_requirements=_raw_reqs(r.get("data_requirements", "[]")),
                priority=r.get("priority", "Medium"),
                status=_normalize_status(r.get("status")),
                status_notes=r.get("status_notes") or "",
                status_updated_at=(
                    str(r.get("status_updated_at"))
                    if r.get("status_updated_at") else None
                ),
                is_user_edited=_to_bool(r.get("is_user_edited", False)),
            )
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"list_use_cases failed: {e}")
        return []


@router.get("/company/entities", operation_id="listEntities")
async def list_entities(
    use_case_name: Optional[str] = Query(None),
    matched_only: bool = Query(False),
) -> list[dict]:
    """List use case entities, optionally filtered by use case or match status."""
    silver = get_silver_schema()
    conditions = ["1=1"]
    if use_case_name:
        conditions.append(f"use_case_name = '{use_case_name}'")
    if matched_only:
        conditions.append("is_matched = true")
    where = " AND ".join(conditions)
    try:
        rows = execute_query(
            f"SELECT * FROM {fqn(silver, 'use_case_entities')} WHERE {where} ORDER BY use_case_name, entity_name"
        )
        return rows
    except Exception:
        return []


def build_use_case_update_set_clause(patch: dict) -> Optional[str]:
    """Build the SET clause for a UPDATE silver.use_cases ... statement.

    Accepts a dict with any subset of the keys in UseCaseUpdateIn (see
    models.py). Returns the SET clause WITHOUT the leading 'SET ', or
    None if no recognized fields were present (the caller can then 400
    instead of issuing a no-op UPDATE).

    Extracted so both `update_use_case` (UI) and the chat propose/confirm
    executor share one source of truth for which fields are writable
    and how each one is escaped. New writable fields get added here once
    and both code paths pick them up. Drift between UI writes and chat
    writes was a real risk before this lived in one place.

    Always appends `is_user_edited = true` so reseed jobs preserve the
    edit, mirroring what the original endpoint always did.
    """
    updates: list[str] = []

    def _set_str(field: str) -> None:
        v = patch.get(field)
        if v is not None:
            updates.append(f"{field} = '{_sql_escape(str(v))}'")

    _set_str("use_case_name")
    _set_str("description")
    _set_str("department")
    _set_str("category")
    _set_str("business_value")
    if patch.get("estimated_value_usd") is not None:
        updates.append(
            f"estimated_value_usd = {float(patch['estimated_value_usd'])}"
        )
    _set_str("value_rationale")
    _set_str("priority")
    if patch.get("data_requirements") is not None:
        # data_requirements is stored as a JSON string in silver.use_cases
        dr_json = json.dumps(list(patch["data_requirements"]))
        updates.append(f"data_requirements = '{_sql_escape(dr_json)}'")
    if patch.get("status") is not None:
        updates.append(
            f"status = '{_sql_escape(_normalize_status(patch['status']))}'"
        )
        updates.append("status_updated_at = current_timestamp()")
    if patch.get("status_notes") is not None:
        updates.append(
            f"status_notes = '{_sql_escape(str(patch['status_notes']))}'"
        )
    if not updates:
        return None
    updates.append("is_user_edited = true")
    return ", ".join(updates)


@router.put("/company/use-cases/{uc_id}", operation_id="updateUseCase")
async def update_use_case(uc_id: str, body: UseCaseUpdateIn) -> dict:
    silver = get_silver_schema()
    _ensure_use_case_status_columns()
    set_clause = build_use_case_update_set_clause(body.model_dump(exclude_none=True))
    if set_clause is None:
        raise HTTPException(400, "No fields to update")
    execute_query(
        f"UPDATE {fqn(silver, 'use_cases')} SET {set_clause} "
        f"WHERE id = '{_sql_escape(uc_id)}'"
    )
    return {"status": "updated"}


@router.patch(
    "/company/use-cases/{uc_id}/status", operation_id="updateUseCaseStatus"
)
async def update_use_case_status(uc_id: str, body: UseCaseStatusIn) -> dict:
    """Dedicated quick-set endpoint used by the detail drawer's status pill.

    Separate from PUT /company/use-cases/{id} so the UI can ship a
    "mark delivered" affordance without having to echo every other field.
    """
    silver = get_silver_schema()
    _ensure_use_case_status_columns()
    status = _normalize_status(body.status)
    updates = [
        f"status = '{status}'",
        "status_updated_at = current_timestamp()",
        "is_user_edited = true",
    ]
    if body.status_notes is not None:
        updates.append(f"status_notes = '{_sql_escape(body.status_notes)}'")
    execute_query(
        f"UPDATE {fqn(silver, 'use_cases')} "
        f"SET {', '.join(updates)} "
        f"WHERE id = '{_sql_escape(uc_id)}'"
    )
    return {"status": "updated", "id": uc_id, "new_status": status}


def insert_use_case_row(
    *,
    use_case_name: str,
    description: str = "",
    department: str = "",
    category: str = "",
    priority: str = "Medium",
    business_value: str = "",
    estimated_value_usd: Optional[float] = None,
    value_rationale: str = "",
    data_requirements: Optional[list[str]] = None,
    status: str = "not_started",
    status_notes: str = "",
) -> str:
    """Insert one row into bhe_silver.use_cases. Returns the new id.

    Always sets `is_user_edited=true` so the seed/reseed jobs preserve
    the user's row (a chat-created use case must not be wiped out by
    the next periodic enrichment). ID format `uc_<12hex>` matches what
    the UI POST endpoint generates today.

    Shared by the UI POST `/company/use-cases` endpoint and the chat
    propose/confirm executor (`_exec_create_use_case` in confirm.py)
    so both write the same column set the same way.
    """
    silver = get_silver_schema()
    _ensure_use_case_status_columns()
    new_id = f"uc_{uuid.uuid4().hex[:12]}"
    dr_json = json.dumps(list(data_requirements or []))
    norm_status = _normalize_status(status)
    execute_query(f"""
        INSERT INTO {fqn(silver, 'use_cases')}
            (id, use_case_name, description, department, category, priority,
             business_value, estimated_value_usd, value_rationale,
             data_requirements, status, status_notes, status_updated_at,
             is_user_edited, created_at)
        VALUES (
            '{_sql_escape(new_id)}',
            '{_sql_escape(use_case_name)}',
            '{_sql_escape(description or "")}',
            '{_sql_escape(department or "")}',
            '{_sql_escape(category or "")}',
            '{_sql_escape(priority or "Medium")}',
            '{_sql_escape(business_value or "")}',
            {float(estimated_value_usd) if estimated_value_usd is not None else 'NULL'},
            '{_sql_escape(value_rationale or "")}',
            '{_sql_escape(dr_json)}',
            '{norm_status}',
            '{_sql_escape(status_notes or "")}',
            current_timestamp(),
            true,
            current_timestamp()
        )
    """)
    return new_id


def find_use_case_by_name(name: str) -> Optional[dict]:
    """Case-insensitive trimmed name lookup. Returns the row or None.

    Used by the chat propose_use_case tool to reject create requests
    that would collide with an existing use case (cleaner than letting
    two rows with the same display name coexist). The UI doesn't call
    this today — it allows duplicates by design (the analyst owns the
    name) — but the chat is held to a stricter standard because the
    LLM might otherwise create accidental duplicates on every retry.
    """
    silver = get_silver_schema()
    name_q = _sql_escape((name or "").strip())
    if not name_q:
        return None
    try:
        rows = execute_query(
            f"""
            SELECT id, use_case_name
            FROM {fqn(silver, 'use_cases')}
            WHERE LOWER(TRIM(use_case_name)) = LOWER('{name_q}')
            LIMIT 1
            """
        )
        return rows[0] if rows else None
    except Exception as e:
        logger.warning(f"find_use_case_by_name failed: {e}")
        return None


@router.post("/company/use-cases", operation_id="createUseCase")
async def create_use_case(body: UseCaseCreateIn) -> dict:
    """Create a new use case. Marked is_user_edited so it survives reseeds."""
    new_id = insert_use_case_row(
        use_case_name=body.use_case_name,
        description=body.description or "",
        department=body.department or "",
        category=body.category or "",
        priority=body.priority or "Medium",
        business_value=body.business_value or "",
        estimated_value_usd=body.estimated_value_usd,
        value_rationale=body.value_rationale or "",
        data_requirements=body.data_requirements,
        status=body.status,
        status_notes=body.status_notes or "",
    )
    return {"status": "created", "id": new_id}


@router.delete("/company/use-cases/{uc_id}", operation_id="deleteUseCase")
async def delete_use_case(uc_id: str) -> dict:
    """Hard-delete a use case AND its derived gold rows.

    The build_value_model job will not regenerate rows for a use_case_id that
    no longer exists in silver.use_cases, so this is safe.
    """
    silver = get_silver_schema()
    gold = get_gold_schema()
    uc = _sql_escape(uc_id)
    # Remove gold derivatives first to keep referential integrity meaningful.
    for tbl in ("use_case_source_requirements", "use_case_affiliates"):
        try:
            execute_query(
                f"DELETE FROM {fqn(gold, tbl)} WHERE use_case_id = '{uc}'"
            )
        except Exception as e:
            logger.warning(f"delete {tbl} for {uc_id} failed: {e}")
    try:
        execute_query(
            f"DELETE FROM {fqn(silver, 'use_case_entities')} WHERE use_case_id = '{uc}'"
        )
    except Exception as e:
        logger.warning(f"delete entities for {uc_id} failed: {e}")
    execute_query(
        f"DELETE FROM {fqn(silver, 'use_cases')} WHERE id = '{uc}'"
    )
    return {"status": "deleted", "id": uc_id}


# ---------------------------------------------------------------------------
# Edit Center: bhe_silver.use_case_entities
#
# Free-text data entities required by each use case. AI-generated by company
# research. Editing here lets analysts correct bad entity tags and add real
# domain entities the LLM missed.
# ---------------------------------------------------------------------------


@router.post("/edit/use-case-entities", operation_id="createUseCaseEntity")
async def create_use_case_entity(body: UseCaseEntityUpsertIn) -> dict:
    silver = get_silver_schema()
    entity_id = f"uce_{uuid.uuid4().hex[:12]}"
    execute_query(f"""
        INSERT INTO {fqn(silver, 'use_case_entities')}
            (entity_id, use_case_id, use_case_name, entity_name, entity_type,
             description, is_matched, matched_source)
        VALUES (
            '{_sql_escape(entity_id)}',
            '{_sql_escape(body.use_case_id)}',
            '{_sql_escape(body.use_case_name)}',
            '{_sql_escape(body.entity_name)}',
            '{_sql_escape(body.entity_type or "")}',
            '{_sql_escape(body.description or "")}',
            {str(bool(body.is_matched)).lower()},
            '{_sql_escape(body.matched_source or "")}'
        )
    """)
    return {"status": "created", "entity_id": entity_id}


@router.delete("/edit/use-case-entities/{entity_id}", operation_id="deleteUseCaseEntity")
async def delete_use_case_entity(entity_id: str) -> dict:
    silver = get_silver_schema()
    execute_query(
        f"DELETE FROM {fqn(silver, 'use_case_entities')} "
        f"WHERE entity_id = '{_sql_escape(entity_id)}'"
    )
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Edit Center: bhe_gold.affiliates
#
# BHE operating subsidiaries. Customer-editable. The build_value_model job
# only updates rows where is_user_edited=false on reseed, so any change made
# here is preserved across pipeline runs.
# ---------------------------------------------------------------------------


@router.get("/edit/affiliates", operation_id="editListAffiliates")
async def edit_list_affiliates() -> list[dict]:
    gold = get_gold_schema()
    try:
        rows = execute_query(
            f"SELECT affiliate_name, affiliate_code, business_type, region, "
            f"description, is_active, is_user_edited "
            f"FROM {fqn(gold, 'affiliates')} ORDER BY affiliate_name"
        )
        for r in rows:
            r["is_active"] = _to_bool(r.get("is_active"))
            r["is_user_edited"] = _to_bool(r.get("is_user_edited"))
        return rows
    except Exception as e:
        logger.warning(f"edit_list_affiliates failed: {e}")
        return []


@router.post("/edit/affiliates", operation_id="createAffiliate")
async def create_affiliate(body: AffiliateUpsertIn) -> dict:
    gold = get_gold_schema()
    execute_query(f"""
        INSERT INTO {fqn(gold, 'affiliates')}
            (affiliate_name, affiliate_code, business_type, region, description,
             is_active, is_user_edited, created_at, updated_at)
        VALUES (
            '{_sql_escape(body.affiliate_name)}',
            '{_sql_escape(body.affiliate_code or "")}',
            '{_sql_escape(body.business_type or "")}',
            '{_sql_escape(body.region or "")}',
            '{_sql_escape(body.description or "")}',
            {str(bool(body.is_active)).lower()},
            true,
            current_timestamp(),
            current_timestamp()
        )
    """)
    return {"status": "created", "affiliate_name": body.affiliate_name}


@router.put("/edit/affiliates/{affiliate_name}", operation_id="updateAffiliate")
async def update_affiliate(affiliate_name: str, body: AffiliateUpdateIn) -> dict:
    gold = get_gold_schema()
    updates = []
    if body.affiliate_code is not None:
        updates.append(f"affiliate_code = '{_sql_escape(body.affiliate_code)}'")
    if body.business_type is not None:
        updates.append(f"business_type = '{_sql_escape(body.business_type)}'")
    if body.region is not None:
        updates.append(f"region = '{_sql_escape(body.region)}'")
    if body.description is not None:
        updates.append(f"description = '{_sql_escape(body.description)}'")
    if body.is_active is not None:
        updates.append(f"is_active = {str(bool(body.is_active)).lower()}")
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates.append("is_user_edited = true")
    updates.append("updated_at = current_timestamp()")
    execute_query(
        f"UPDATE {fqn(gold, 'affiliates')} SET {', '.join(updates)} "
        f"WHERE affiliate_name = '{_sql_escape(affiliate_name)}'"
    )
    return {"status": "updated"}


@router.delete("/edit/affiliates/{affiliate_name}", operation_id="deleteAffiliate")
async def delete_affiliate(affiliate_name: str) -> dict:
    """Soft-delete: flips is_active=false. Use a real DELETE only if you also
    plan to clean up program_affiliate_map and use_case_affiliates rows."""
    gold = get_gold_schema()
    execute_query(
        f"UPDATE {fqn(gold, 'affiliates')} "
        f"SET is_active = false, is_user_edited = true, "
        f"    updated_at = current_timestamp() "
        f"WHERE affiliate_name = '{_sql_escape(affiliate_name)}'"
    )
    return {"status": "deactivated", "affiliate_name": affiliate_name}


# ---------------------------------------------------------------------------
# Edit Center: bhe_gold.source_system_canonical
#
# The closed vocabulary the LLM uses when mapping use cases to source systems.
# Add entries here when business needs introduce new canonical sources.
# Mark inactive (rather than delete) to preserve historic mappings.
# ---------------------------------------------------------------------------


@router.get("/edit/canonical-sources", operation_id="editListCanonicalSources")
async def edit_list_canonical_sources() -> list[dict]:
    gold = get_gold_schema()
    try:
        rows = execute_query(
            f"SELECT canonical, category, description, is_active, "
            f"       created_at, updated_at "
            f"FROM {fqn(gold, 'source_system_canonical')} "
            f"ORDER BY canonical"
        )
        for r in rows:
            r["is_active"] = _to_bool(r.get("is_active"))
        return rows
    except Exception as e:
        logger.warning(f"edit_list_canonical_sources failed: {e}")
        return []


@router.post("/edit/canonical-sources", operation_id="createCanonicalSource")
async def create_canonical_source(body: CanonicalSourceUpsertIn) -> dict:
    gold = get_gold_schema()
    execute_query(f"""
        INSERT INTO {fqn(gold, 'source_system_canonical')}
            (canonical, category, description, is_active,
             created_at, updated_at)
        VALUES (
            '{_sql_escape(body.canonical)}',
            '{_sql_escape(body.category or "")}',
            '{_sql_escape(body.description or "")}',
            {str(bool(body.is_active)).lower()},
            current_timestamp(),
            current_timestamp()
        )
    """)
    return {"status": "created", "canonical": body.canonical}


@router.put("/edit/canonical-sources/{canonical}", operation_id="updateCanonicalSource")
async def update_canonical_source(canonical: str, body: CanonicalSourceUpdateIn) -> dict:
    gold = get_gold_schema()
    updates = []
    if body.category is not None:
        updates.append(f"category = '{_sql_escape(body.category)}'")
    if body.description is not None:
        updates.append(f"description = '{_sql_escape(body.description)}'")
    if body.is_active is not None:
        updates.append(f"is_active = {str(bool(body.is_active)).lower()}")
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates.append("updated_at = current_timestamp()")
    execute_query(
        f"UPDATE {fqn(gold, 'source_system_canonical')} SET {', '.join(updates)} "
        f"WHERE canonical = '{_sql_escape(canonical)}'"
    )
    return {"status": "updated"}


@router.delete("/edit/canonical-sources/{canonical}", operation_id="deleteCanonicalSource")
async def delete_canonical_source(canonical: str) -> dict:
    """Soft-delete by flipping is_active=false."""
    gold = get_gold_schema()
    execute_query(
        f"UPDATE {fqn(gold, 'source_system_canonical')} "
        f"SET is_active = false, updated_at = current_timestamp() "
        f"WHERE canonical = '{_sql_escape(canonical)}'"
    )
    return {"status": "deactivated"}


# ---------------------------------------------------------------------------
# Edit Center: bhe_gold.program_affiliate_map
#
# Bridge from a catalog "program" (silver_schemas.program) to a BHE affiliate.
# A program can map to multiple affiliates. The composite primary key is
# (program, affiliate_name). The build_value_model job preserves any row with
# is_user_edited=true, so analyst overrides are sticky.
# ---------------------------------------------------------------------------


@router.get("/edit/program-affiliate-map", operation_id="editListProgramAffiliateMap")
async def edit_list_program_affiliate_map() -> list[dict]:
    gold = get_gold_schema()
    try:
        rows = execute_query(
            f"SELECT program, affiliate_name, affiliation_strength, notes, "
            f"       is_user_edited, updated_at "
            f"FROM {fqn(gold, 'program_affiliate_map')} "
            f"ORDER BY program, affiliate_name"
        )
        for r in rows:
            r["is_user_edited"] = _to_bool(r.get("is_user_edited"))
        return rows
    except Exception as e:
        logger.warning(f"edit_list_program_affiliate_map failed: {e}")
        return []


@router.post("/edit/program-affiliate-map", operation_id="createProgramAffiliateMap")
async def create_program_affiliate_map(body: ProgramAffiliateMapUpsertIn) -> dict:
    gold = get_gold_schema()
    execute_query(f"""
        INSERT INTO {fqn(gold, 'program_affiliate_map')}
            (program, affiliate_name, affiliation_strength, notes,
             is_user_edited, updated_at)
        VALUES (
            '{_sql_escape(body.program)}',
            '{_sql_escape(body.affiliate_name)}',
            '{_sql_escape(body.affiliation_strength or "primary")}',
            '{_sql_escape(body.notes or "")}',
            true,
            current_timestamp()
        )
    """)
    return {"status": "created"}


@router.put(
    "/edit/program-affiliate-map/{program}/{affiliate_name}",
    operation_id="updateProgramAffiliateMap",
)
async def update_program_affiliate_map(
    program: str, affiliate_name: str, body: ProgramAffiliateMapUpdateIn
) -> dict:
    gold = get_gold_schema()
    updates = []
    if body.affiliation_strength is not None:
        updates.append(
            f"affiliation_strength = '{_sql_escape(body.affiliation_strength)}'"
        )
    if body.notes is not None:
        updates.append(f"notes = '{_sql_escape(body.notes)}'")
    if not updates:
        raise HTTPException(400, "No fields to update")
    updates.append("is_user_edited = true")
    updates.append("updated_at = current_timestamp()")
    execute_query(
        f"UPDATE {fqn(gold, 'program_affiliate_map')} SET {', '.join(updates)} "
        f"WHERE program = '{_sql_escape(program)}' "
        f"  AND affiliate_name = '{_sql_escape(affiliate_name)}'"
    )
    return {"status": "updated"}


@router.delete(
    "/edit/program-affiliate-map/{program}/{affiliate_name}",
    operation_id="deleteProgramAffiliateMap",
)
async def delete_program_affiliate_map(program: str, affiliate_name: str) -> dict:
    gold = get_gold_schema()
    execute_query(
        f"DELETE FROM {fqn(gold, 'program_affiliate_map')} "
        f"WHERE program = '{_sql_escape(program)}' "
        f"  AND affiliate_name = '{_sql_escape(affiliate_name)}'"
    )
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Edit Center: bhe_gold.use_case_affiliates  (LLM with manual override)
#
# Manual edits set mapped_by='manual' and is_user_edited=true so the
# build_value_model job's MERGE skips them on subsequent runs.
# ---------------------------------------------------------------------------


@router.get("/edit/use-case-affiliates", operation_id="editListUseCaseAffiliates")
async def edit_list_use_case_affiliates(
    use_case_id: Optional[str] = Query(None),
) -> list[dict]:
    gold = get_gold_schema()
    silver = get_silver_schema()
    where = ""
    if use_case_id:
        where = f"WHERE ua.use_case_id = '{_sql_escape(use_case_id)}'"
    try:
        rows = execute_query(f"""
            SELECT ua.use_case_id, ua.affiliate_name, ua.applicability,
                   ua.rationale, ua.mapped_by, ua.is_user_edited, ua.mapped_at,
                   uc.use_case_name
            FROM {fqn(gold, 'use_case_affiliates')} ua
            LEFT JOIN {fqn(silver, 'use_cases')} uc ON uc.id = ua.use_case_id
            {where}
            ORDER BY ua.use_case_id, ua.affiliate_name
        """)
        for r in rows:
            r["is_user_edited"] = _to_bool(r.get("is_user_edited"))
        return rows
    except Exception as e:
        logger.warning(f"edit_list_use_case_affiliates failed: {e}")
        return []


def merge_use_case_affiliate(
    use_case_id: str,
    affiliate_name: str,
    applicability: str = "primary",
    rationale: str = "",
) -> None:
    """Upsert a single (use_case_id, affiliate_name) row.

    Idempotent. Sets `mapped_by='manual'` and `is_user_edited=true` so
    the periodic LLM job's MERGE skips this row on subsequent runs and
    the user-set values stick. Shared by the UI POST endpoint and the
    chat propose/confirm executor (`_exec_update_affiliates` in
    confirm.py) — drift between those two write paths was a real risk
    before this lived in one place.
    """
    gold = get_gold_schema()
    uc = _sql_escape(use_case_id)
    aff = _sql_escape(affiliate_name)
    app = _sql_escape(applicability or "primary")
    rat = _sql_escape(rationale or "")
    execute_query(f"""
        MERGE INTO {fqn(gold, 'use_case_affiliates')} AS t
        USING (SELECT '{uc}' AS use_case_id, '{aff}' AS affiliate_name) AS s
          ON t.use_case_id = s.use_case_id
         AND t.affiliate_name = s.affiliate_name
        WHEN MATCHED THEN UPDATE SET
            applicability  = '{app}',
            rationale      = '{rat}',
            mapped_by      = 'manual',
            is_user_edited = true,
            mapped_at      = current_timestamp()
        WHEN NOT MATCHED THEN INSERT
            (use_case_id, affiliate_name, applicability, rationale,
             mapped_by, is_user_edited, mapped_at)
            VALUES ('{uc}', '{aff}', '{app}', '{rat}',
                    'manual', true, current_timestamp())
    """)


def delete_use_case_affiliate_row(
    use_case_id: str, affiliate_name: str
) -> None:
    """Hard-delete a single (use_case_id, affiliate_name) row.

    Caveat: the LLM job MAY recreate this row on its next run if the
    underlying use case description still implies the affiliate. The
    chat tool surfaces this in the confirm card so users aren't
    surprised by a "re-appearing" affiliate on the next reseed.
    """
    gold = get_gold_schema()
    execute_query(
        f"DELETE FROM {fqn(gold, 'use_case_affiliates')} "
        f"WHERE use_case_id = '{_sql_escape(use_case_id)}' "
        f"  AND affiliate_name = '{_sql_escape(affiliate_name)}'"
    )


@router.post("/edit/use-case-affiliates", operation_id="upsertUseCaseAffiliate")
async def upsert_use_case_affiliate(body: UseCaseAffiliateUpsertIn) -> dict:
    """MERGE-style upsert (composite PK = use_case_id + affiliate_name)."""
    merge_use_case_affiliate(
        use_case_id=body.use_case_id,
        affiliate_name=body.affiliate_name,
        applicability=body.applicability or "primary",
        rationale=body.rationale or "",
    )
    return {"status": "upserted"}


@router.delete(
    "/edit/use-case-affiliates/{use_case_id}/{affiliate_name}",
    operation_id="deleteUseCaseAffiliate",
)
async def delete_use_case_affiliate(use_case_id: str, affiliate_name: str) -> dict:
    """Hard-delete the row. See `delete_use_case_affiliate_row` docstring
    for the LLM-reseed caveat."""
    delete_use_case_affiliate_row(use_case_id, affiliate_name)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Edit Center: bhe_gold.use_case_source_requirements (LLM + manual override)
# ---------------------------------------------------------------------------


@router.get(
    "/edit/use-case-source-requirements",
    operation_id="editListUseCaseSourceRequirements",
)
async def edit_list_use_case_source_requirements(
    use_case_id: Optional[str] = Query(None),
) -> list[dict]:
    gold = get_gold_schema()
    silver = get_silver_schema()
    where = ""
    if use_case_id:
        where = f"WHERE r.use_case_id = '{_sql_escape(use_case_id)}'"
    try:
        rows = execute_query(f"""
            SELECT r.use_case_id, r.required_canonical, r.necessity,
                   r.data_need_excerpt, r.confidence, r.mapped_by,
                   r.is_user_edited, r.mapped_at,
                   uc.use_case_name
            FROM {fqn(gold, 'use_case_source_requirements')} r
            LEFT JOIN {fqn(silver, 'use_cases')} uc ON uc.id = r.use_case_id
            {where}
            ORDER BY r.use_case_id, r.required_canonical
        """)
        for r in rows:
            r["is_user_edited"] = _to_bool(r.get("is_user_edited"))
        return rows
    except Exception as e:
        logger.warning(f"edit_list_use_case_source_requirements failed: {e}")
        return []


def merge_use_case_source_requirement(
    use_case_id: str,
    required_canonical: str,
    necessity: str = "must_have",
    data_need_excerpt: str = "",
    confidence: str = "high",
) -> None:
    """Upsert a single (use_case_id, required_canonical) row.

    Same `mapped_by='manual'` + `is_user_edited=true` semantics as
    `merge_use_case_affiliate`. Shared by the UI POST endpoint and the
    chat propose/confirm executor.
    """
    gold = get_gold_schema()
    uc = _sql_escape(use_case_id)
    canon = _sql_escape(required_canonical)
    nec = _sql_escape(necessity or "must_have")
    excerpt = _sql_escape(data_need_excerpt or "")
    conf = _sql_escape(confidence or "high")
    execute_query(f"""
        MERGE INTO {fqn(gold, 'use_case_source_requirements')} AS t
        USING (SELECT '{uc}' AS use_case_id,
                      '{canon}' AS required_canonical) AS s
          ON t.use_case_id = s.use_case_id
         AND t.required_canonical = s.required_canonical
        WHEN MATCHED THEN UPDATE SET
            necessity         = '{nec}',
            data_need_excerpt = '{excerpt}',
            confidence        = '{conf}',
            mapped_by         = 'manual',
            is_user_edited    = true,
            mapped_at         = current_timestamp()
        WHEN NOT MATCHED THEN INSERT
            (use_case_id, required_canonical, necessity, data_need_excerpt,
             confidence, mapped_by, is_user_edited, mapped_at)
            VALUES ('{uc}', '{canon}', '{nec}', '{excerpt}',
                    '{conf}', 'manual', true, current_timestamp())
    """)


def delete_use_case_source_requirement_row(
    use_case_id: str, required_canonical: str
) -> None:
    """Hard-delete a single canonical requirement row.

    Same LLM-reseed caveat as `delete_use_case_affiliate_row`: if the
    use case description still implies the canonical, the next LLM job
    run may recreate the row.
    """
    gold = get_gold_schema()
    execute_query(
        f"DELETE FROM {fqn(gold, 'use_case_source_requirements')} "
        f"WHERE use_case_id = '{_sql_escape(use_case_id)}' "
        f"  AND required_canonical = '{_sql_escape(required_canonical)}'"
    )


@router.post(
    "/edit/use-case-source-requirements",
    operation_id="upsertUseCaseSourceRequirement",
)
async def upsert_use_case_source_requirement(
    body: UseCaseSourceRequirementUpsertIn,
) -> dict:
    merge_use_case_source_requirement(
        use_case_id=body.use_case_id,
        required_canonical=body.required_canonical,
        necessity=body.necessity or "must_have",
        data_need_excerpt=body.data_need_excerpt or "",
        confidence=body.confidence or "high",
    )
    return {"status": "upserted"}


@router.delete(
    "/edit/use-case-source-requirements/{use_case_id}/{required_canonical}",
    operation_id="deleteUseCaseSourceRequirement",
)
async def delete_use_case_source_requirement(
    use_case_id: str, required_canonical: str
) -> dict:
    delete_use_case_source_requirement_row(use_case_id, required_canonical)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Job Triggers
# ---------------------------------------------------------------------------


import threading

_active_runs: dict[str, dict] = {}

LLM_ENDPOINT = os.environ.get("LLM_ENDPOINT", "databricks-claude-sonnet-4")


def _sql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _ai_query(prompt: str, response_format: str | None = None) -> str:
    """Call ai_query via SQL and return the raw JSON string result."""
    if response_format:
        sql = f"""
            SELECT ai_query(
                '{LLM_ENDPOINT}',
                '{_sql_escape(prompt)}',
                responseFormat => '{_sql_escape(response_format)}',
                modelParameters => named_struct('max_tokens', 4000, 'temperature', 0.4)
            ) AS result
        """
    else:
        sql = f"""
            SELECT ai_query(
                '{LLM_ENDPOINT}',
                '{_sql_escape(prompt)}',
                modelParameters => named_struct('max_tokens', 4000, 'temperature', 0.4)
            ) AS result
        """
    rows = execute_query(sql)
    if not rows:
        raise RuntimeError("ai_query returned no rows")
    raw = rows[0]["result"]
    if not response_format and raw:
        import re as _re
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = _re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = _re.sub(r"\n?```$", "", cleaned)
        return cleaned
    return raw


def _emit_progress(run_id: str, step: str, step_index: int, total_steps: int,
                   item_name: str = "", parent_item: str = "", detail: str = ""):
    """Write a progress row to job_progress so the frontend can track steps."""
    silver = get_silver_schema()
    esc = lambda s: s.replace("'", "''")
    try:
        execute_query(
            f"INSERT INTO {fqn(silver, 'job_progress')} "
            f"(run_id, step, step_index, total_steps, item_name, parent_item, detail, created_at) "
            f"VALUES ('{esc(run_id)}', '{esc(step)}', {step_index}, {total_steps}, "
            f"'{esc(item_name)}', '{esc(parent_item)}', '{esc(detail)}', current_timestamp())"
        )
    except Exception as e:
        logger.warning(f"Failed to emit progress for step {step}: {e}")


def _run_company_research(
    run_id: str,
    company: str,
    steps: Optional[list[str]] = None,
    force: bool = False,
):
    """Run company research using ai_query() SQL for serverless batch inference.

    Emits granular progress to job_progress table so the UI can render
    a live tree: Company -> Departments -> Use Cases per department.

    Args:
        steps: subset of `ALL_RESEARCH_STEPS` to run. If None, all steps run.
        force: if true, re-generate even when output rows already exist.
            Otherwise each step early-exits when its output table already
            has rows for this company.

    Closes B-006: previously the function ran every step unconditionally,
    which made the "Resume Research (N steps left)" button misleading and
    expensive (re-burned LLM tokens + 5+ minutes of work per click).
    """
    catalog = get_catalog()
    silver = get_silver_schema()
    esc_company = _sql_escape(company)
    steps_set = set(steps) if steps else set(ALL_RESEARCH_STEPS)

    def _log(msg: str):
        print(f"[{run_id}] {msg}", flush=True)
        logger.info(f"[{run_id}] {msg}")

    def _company_count(table: str) -> int:
        """Count rows in a research table for THIS company. Returns 0 on error."""
        try:
            rows = execute_query(
                f"SELECT count(*) AS n FROM {fqn(silver, table)} "
                f"WHERE company_name = '{esc_company}'"
            )
            return int(rows[0]["n"]) if rows else 0
        except Exception as e:
            logger.warning(f"_company_count({table}) failed: {e}")
            return 0

    def _should_run(step: str, table: str) -> bool:
        """True iff `step` is requested and (force or its output is empty)."""
        if step not in steps_set:
            _log(f"Skipping `{step}` (not in requested step list)")
            return False
        if not force and _company_count(table) > 0:
            _log(f"Skipping `{step}` (already has data; pass force=true to re-run)")
            return False
        return True

    try:
        _active_runs[run_id]["status"] = "RUNNING"

        # ------------------------------------------------------------------
        # Short-circuit when the user clicks "Resume Research" but every
        # requested step is already populated and `force` is not set.
        #
        # This is the half of B-006 that prevents the embarrassing 5-minute
        # no-op re-run when the user clicks Resume one extra time. The other
        # half (per-step skip when only some steps are missing) is tracked as
        # a follow-up in the backlog under B-006 — fully implementing it
        # requires extracting each step body into a guarded block, and the
        # current monolithic structure makes that risky to do in the same
        # pass as the other circular-dep fixes.
        # ------------------------------------------------------------------
        step_to_table = {
            "profile":     "company_profile",
            "departments": "departments",
            "usecases":    "use_cases",
            "sankey":      "sankey_mappings",
            "entities":    "use_case_entities",
        }
        requested = [s for s in ALL_RESEARCH_STEPS if s in steps_set]
        all_present = all(
            _company_count(step_to_table[s]) > 0 for s in requested
        ) if requested else False
        if not force and all_present:
            _log("All requested steps already complete; skipping run "
                 "(pass force=true to re-generate).")
            _active_runs[run_id]["status"] = "TERMINATED"
            _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
            return

        execute_query(
            f"DELETE FROM {fqn(silver, 'job_progress')} WHERE run_id = '{_sql_escape(run_id)}'"
        )

        # ---- Step 1: Company Profile via ai_query() ----
        _log("Step 1: Researching company profile via ai_query...")

        profile_prompt = (
            f"You are a business analyst specializing in enterprise organizations. "
            f"Research the company: {company}. Provide a comprehensive profile. "
            f"For key_business_units and strategic_priorities, return comma-separated strings. "
            f"For catalog_name, propose a short brand label suitable for the top of an "
            f"internal data catalog UI (e.g. 'PacifiCorp Data Catalog' or 'NV Energy Data Hub'). "
            f"For primary_domain, return the company's primary public web domain "
            f"without a scheme or path (e.g. 'pacificorp.com'). Use the most "
            f"recognizable corporate domain, not a subsidiary."
        )
        profile_resp_fmt = (
            'STRUCT<profile:STRUCT<company_name:STRING, industry:STRING, sub_industry:STRING, '
            'description:STRING, headquarters:STRING, '
            'key_business_units:STRING, strategic_priorities:STRING, '
            'regulatory_environment:STRING, catalog_name:STRING, primary_domain:STRING>>'
        )

        profile_json = _ai_query(profile_prompt, profile_resp_fmt)
        _log(f"Profile JSON received ({len(profile_json)} chars)")
        profile = json.loads(profile_json)

        def _esc(v):
            if isinstance(v, list):
                return json.dumps(v).replace("'", "''")
            return str(v).replace("'", "''")

        # Make sure the branding columns exist before we try to write to them
        # (no-op on fresh installs since bootstrap_tables.py creates them up
        # front; safety net for existing deployments mid-migration).
        _ensure_branding_columns()

        # Preserve user-edited branding across re-runs. If they've manually
        # set a catalog_name or logo, leave those alone; otherwise let the AI
        # suggestion populate them.
        existing_branding = execute_query(
            f"SELECT catalog_name, logo_url, primary_domain, branding_user_edited "
            f"FROM {fqn(silver, 'company_profile')} LIMIT 1"
        )
        prev = existing_branding[0] if existing_branding else {}
        prev_user_edited = bool(prev.get("branding_user_edited") in (True, "true", 1, "1"))

        ai_catalog_name = (profile.get("catalog_name") or "").strip()
        ai_domain = (profile.get("primary_domain") or "").strip().lower()
        # Strip any accidental scheme / path from the LLM output before we
        # build the Clearbit URL.
        if ai_domain:
            ai_domain = ai_domain.split("//")[-1].split("/")[0].strip()
        ai_logo_url = f"https://logo.clearbit.com/{ai_domain}" if ai_domain else ""

        if prev_user_edited:
            final_catalog_name = prev.get("catalog_name") or ai_catalog_name or f"{company} Data Catalog"
            final_logo_url = prev.get("logo_url") or ai_logo_url
            final_domain = prev.get("primary_domain") or ai_domain
            _log("Preserving user-edited branding (catalog_name / logo_url).")
        else:
            final_catalog_name = ai_catalog_name or f"{company} Data Catalog"
            final_logo_url = ai_logo_url
            final_domain = ai_domain

        execute_query(f"DELETE FROM {fqn(silver, 'company_profile')} WHERE 1=1")
        execute_query(f"""INSERT INTO {fqn(silver, 'company_profile')}
            (id, company_name, industry, sub_industry, description, headquarters,
             key_business_units, strategic_priorities, regulatory_environment,
             catalog_name, logo_url, primary_domain, branding_user_edited)
            VALUES ('{run_id}',
                '{_esc(profile.get("company_name", company))}',
                '{_esc(profile.get("industry", ""))}',
                '{_esc(profile.get("sub_industry", ""))}',
                '{_esc(profile.get("description", ""))}',
                '{_esc(profile.get("headquarters", ""))}',
                '{_esc(profile.get("key_business_units", []))}',
                '{_esc(profile.get("strategic_priorities", []))}',
                '{_esc(profile.get("regulatory_environment", ""))}',
                '{_esc(final_catalog_name)}',
                '{_esc(final_logo_url)}',
                '{_esc(final_domain)}',
                {'true' if prev_user_edited else 'false'})""")
        _log(f"Company profile saved. Branding: catalog_name='{final_catalog_name}', logo='{final_logo_url}'")

        # Emit profile progress (total_steps is a placeholder — updated after departments known)
        _emit_progress(run_id, "profile", 1, 5, company, "", profile.get("industry", ""))

        # ---- Step 2: Departments via ai_query() ----
        _log("Step 2: Generating departments via ai_query...")

        dept_prompt = (
            f"You are a business analyst for large enterprises. "
            f"Generate 15-25 key departments for {company} relevant to data and analytics. "
            f"Include both business and technology departments. "
            f"Return a JSON object with a departments array."
        )
        dept_resp_fmt = (
            'STRUCT<output:STRUCT<departments:ARRAY<STRUCT<'
            'department_name:STRING, description:STRING, data_needs:STRING'
            '>>>>'
        )

        dept_json = _ai_query(dept_prompt, dept_resp_fmt)
        _log(f"Departments JSON received ({len(dept_json)} chars)")
        departments = json.loads(dept_json).get("departments", [])

        execute_query(f"DELETE FROM {fqn(silver, 'departments')} WHERE 1=1")
        dept_batch = []
        for dept in departments:
            did = str(uuid.uuid4())[:8]
            dn = dept.get("department_name", "").replace("'", "''")
            dd = dept.get("description", "").replace("'", "''")
            dneeds = dept.get("data_needs", "").replace("'", "''")
            dept_batch.append(f"('{did}', '{dn}', '{dd}', '', '{dneeds}', '{esc_company}', false)")

        batch = ", ".join(dept_batch)
        execute_query(f"""INSERT INTO {fqn(silver, 'departments')}
            (id, department_name, description, key_functions, data_needs, company_name, is_user_edited)
            VALUES {batch}""")
        dept_rows = execute_query(f"SELECT department_name FROM {fqn(silver, 'departments')}")
        dept_names = [r["department_name"] for r in dept_rows]
        _log(f"Saved {len(dept_names)} departments.")

        # Now we know total steps: 1 profile + 1 departments + N dept use-cases
        # + 1 sankey + 1 entities = N+4
        n_depts = len(dept_names)
        total_steps = n_depts + 4

        # Update profile progress row with correct total
        try:
            execute_query(
                f"UPDATE {fqn(silver, 'job_progress')} "
                f"SET total_steps = {total_steps} "
                f"WHERE run_id = '{_sql_escape(run_id)}' AND step = 'profile'"
            )
        except Exception:
            pass

        _emit_progress(run_id, "departments", 2, total_steps, "",
                       company, f"{n_depts} departments")
        for dn in dept_names:
            _emit_progress(run_id, "dept_item", 2, total_steps, dn, company, "")

        # ---- Step 3: Use Cases PER DEPARTMENT via ai_query() ----
        _log("Step 3: Generating use cases per department...")

        execute_query(f"DELETE FROM {fqn(silver, 'use_cases')} WHERE 1=1")
        all_uc_names = []
        dept_list = ", ".join(dept_names[:25])

        for dept_idx, dept_name in enumerate(dept_names):
            _log(f"  Generating use cases for: {dept_name}")
            step_index = 3 + dept_idx

            uc_prompt = (
                f"You are a data strategy consultant. Generate 3-10 high-value analytics and data use cases "
                f"for the {dept_name} department at {company}. "
                f"For each use case, include estimated_value_usd (annual dollar value) and value_rationale. "
                f"Categories: Predictive Analytics, Reporting and BI, ML/AI, Data Integration, "
                f"Real-Time Monitoring, Regulatory Compliance, Customer Analytics, Operational Efficiency, "
                f"Risk Management, Revenue Optimization. "
                f"Priorities: High, Medium, Low. "
                f"Respond ONLY with a valid JSON object (no markdown). Use this exact format: "
                f'{{"use_cases": [{{"use_case_name":"Name","description":"Desc","category":"Cat",'
                f'"business_value":"Value","estimated_value_usd":5000000,"value_rationale":"Rationale",'
                f'"priority":"High"}}]}}'
            )

            try:
                uc_json = _ai_query(uc_prompt)
                use_cases = json.loads(uc_json).get("use_cases", [])
            except Exception as e:
                _log(f"  Failed for {dept_name}: {e}")
                _emit_progress(run_id, f"usecase:{dept_name}", step_index, total_steps,
                               dept_name, company, f"failed: {str(e)[:60]}")
                continue

            uc_batch = []
            uc_names_this_dept = []
            for uc in use_cases:
                uid = str(uuid.uuid4())[:8]
                uc_name = uc.get("use_case_name", "").replace("'", "''")
                uc_desc = uc.get("description", "").replace("'", "''")
                uc_cat = uc.get("category", "").replace("'", "''")
                uc_bv = uc.get("business_value", "").replace("'", "''")
                uc_ev = float(uc.get("estimated_value_usd", 0))
                uc_vr = uc.get("value_rationale", "").replace("'", "''")
                uc_pri = uc.get("priority", "Medium").replace("'", "''")
                uc_co = company.replace("'", "''")
                dn_esc = dept_name.replace("'", "''")
                row = (
                    f"('{uid}', '{uc_name}', '{uc_desc}', '{dn_esc}', '{uc_cat}', "
                    f"'{uc_bv}', {uc_ev}, '{uc_vr}', '', '{uc_pri}', '{uc_co}', false)"
                )
                uc_batch.append(row)
                uc_names_this_dept.append(uc.get("use_case_name", ""))

            if uc_batch:
                for i in range(0, len(uc_batch), 10):
                    b = ", ".join(uc_batch[i:i+10])
                    execute_query(f"""INSERT INTO {fqn(silver, 'use_cases')}
                        (id, use_case_name, description, department, category, business_value,
                         estimated_value_usd, value_rationale,
                         data_requirements, priority, company_name, is_user_edited) VALUES {b}""")

            all_uc_names.extend(uc_names_this_dept)
            uc_detail = ", ".join(uc_names_this_dept[:5])
            if len(uc_names_this_dept) > 5:
                uc_detail += f" (+{len(uc_names_this_dept) - 5} more)"
            _emit_progress(run_id, f"usecase:{dept_name}", step_index, total_steps,
                           dept_name, company, f"{len(uc_names_this_dept)} use cases")
            _log(f"  {dept_name}: {len(uc_names_this_dept)} use cases saved")

        _log(f"Total use cases saved: {len(all_uc_names)}")

        # ---- Step 4: Sankey Mappings via ai_query() ----
        _log("Step 4: Generating Sankey mappings via ai_query...")

        try:
            source_rows = execute_query(
                f"SELECT DISTINCT program, count(*) as cnt "
                f"FROM {fqn(silver, 'silver_schemas')} "
                f"WHERE classification = 'PRODUCTION' AND environment != 'SYSTEM' "
                f"GROUP BY program ORDER BY cnt DESC LIMIT 20"
            )
            sources_text = ", ".join(
                f"{r.get('program','')} ({r.get('cnt',0)} schemas)" for r in source_rows
            )
        except Exception:
            sources_text = "No enriched catalog data available yet"

        uc_list = ", ".join(all_uc_names[:25])
        sankey_prompt = (
            f"You are a data architect for {company}. "
            f"Data sources/programs: {sources_text}. "
            f"Use cases: {uc_list}. "
            f"Departments: {dept_list}. "
            f"Map data sources through data entities to use cases to departments. "
            f"Include entity_name for the data entity that connects the source to the use case. "
            f"For entities without a matching source, use source_system='UNMAPPED'. "
            f"Relevance: Primary, Secondary, or Supporting. "
            f"Source categories: ERP, CRM, SCADA, IoT, GIS, HR, Finance, etc. "
            f"Generate comprehensive mappings. "
            f"Respond ONLY with valid JSON (no markdown). Use this format: "
            f'{{"mappings": [{{"source_system":"Name","source_category":"Cat","entity_name":"Entity","use_case":"UC","department":"Dept","relevance":"Primary"}}]}}'
        )

        sankey_json = _ai_query(sankey_prompt)
        _log(f"Sankey JSON received ({len(sankey_json)} chars)")
        mappings = json.loads(sankey_json).get("mappings", [])

        execute_query(f"DELETE FROM {fqn(silver, 'sankey_mappings')} WHERE 1=1")
        now = datetime.utcnow().isoformat()
        batch_rows = []
        for m in mappings:
            vals = [str(uuid.uuid4())[:8]]
            for k in ["source_system", "source_category", "use_case", "department", "entity_name", "relevance"]:
                vals.append(m.get(k, "").replace("'", "''"))
            vals.append(company.replace("'", "''"))
            row = "(" + ", ".join(f"'{v}'" for v in vals) + f", false, '{now}')"
            batch_rows.append(row)

        for i in range(0, len(batch_rows), 20):
            b = ", ".join(batch_rows[i:i+20])
            execute_query(f"""INSERT INTO {fqn(silver, 'sankey_mappings')}
                (id, source_system, source_category, use_case, department, entity_name, relevance,
                 company_name, is_user_edited, created_at) VALUES {b}""")
        mapping_count = execute_query(f"SELECT count(*) as cnt FROM {fqn(silver, 'sankey_mappings')}")
        cnt = mapping_count[0]["cnt"] if mapping_count else 0
        _log(f"Saved {cnt} Sankey mappings.")

        # Step index for sankey is total_steps - 1; entities is the final step.
        _emit_progress(run_id, "sankey", total_steps - 1, total_steps, "", company,
                       f"{cnt} mappings")

        # ---- Step 5: Use-case Entities derived from Sankey ----
        # Closes B-007: the `entities` step was declared in
        # ALL_RESEARCH_STEPS but never implemented, so the wizard reported
        # "Resume Research (1 step left)" forever.
        #
        # Sankey mappings already contain (use_case, entity_name, source_system)
        # tuples. Flatten them into use_case_entities, joining to silver.use_cases
        # to resolve use_case_id. is_matched=true when the entity has a
        # non-UNMAPPED, non-empty source_system in any of its sankey arcs.
        _log("Step 5: Deriving use-case entities from sankey mappings...")
        execute_query(f"DELETE FROM {fqn(silver, 'use_case_entities')} WHERE 1=1")

        # Aggregate sankey -> distinct (use_case, entity) with best source.
        # `MAX(...) FILTER` would be cleaner but the DBSQL we target across
        # this codebase doesn't support FILTER on MAX, so we fall back to a
        # CASE expression inside the aggregate.
        try:
            execute_query(f"""
                INSERT INTO {fqn(silver, 'use_case_entities')}
                (entity_id, use_case_id, use_case_name, entity_name, entity_type,
                 description, is_matched, matched_source, company_name, created_at)
                SELECT
                  uuid() AS entity_id,
                  uc.id AS use_case_id,
                  sm.use_case AS use_case_name,
                  sm.entity_name,
                  '' AS entity_type,
                  '' AS description,
                  CASE WHEN MAX(CASE
                       WHEN sm.source_system IS NOT NULL
                        AND sm.source_system != ''
                        AND UPPER(sm.source_system) != 'UNMAPPED'
                       THEN 1 ELSE 0 END) = 1
                       THEN true ELSE false
                  END AS is_matched,
                  COALESCE(MAX(CASE
                       WHEN sm.source_system IS NOT NULL
                        AND sm.source_system != ''
                        AND UPPER(sm.source_system) != 'UNMAPPED'
                       THEN sm.source_system END), '') AS matched_source,
                  '{esc_company}' AS company_name,
                  CAST(current_timestamp() AS STRING) AS created_at
                FROM {fqn(silver, 'sankey_mappings')} sm
                LEFT JOIN {fqn(silver, 'use_cases')} uc
                  ON uc.use_case_name = sm.use_case
                 AND uc.company_name = '{esc_company}'
                WHERE sm.company_name = '{esc_company}'
                  AND sm.entity_name IS NOT NULL
                  AND sm.entity_name != ''
                GROUP BY uc.id, sm.use_case, sm.entity_name
            """)
        except Exception as e:
            _log(f"Entity derivation FAILED (non-fatal): {e}")

        ent_count_rows = execute_query(
            f"SELECT count(*) AS cnt FROM {fqn(silver, 'use_case_entities')} "
            f"WHERE company_name = '{esc_company}'"
        )
        ent_count = ent_count_rows[0]["cnt"] if ent_count_rows else 0
        matched_rows = execute_query(
            f"SELECT count(*) AS cnt FROM {fqn(silver, 'use_case_entities')} "
            f"WHERE company_name = '{esc_company}' AND is_matched = true"
        )
        matched_count = matched_rows[0]["cnt"] if matched_rows else 0
        _log(f"Saved {ent_count} entities ({matched_count} matched to a source).")

        _emit_progress(run_id, "entities", total_steps, total_steps, "", company,
                       f"{ent_count} entities ({matched_count} matched)")

        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
        _log("Company research complete!")

    except Exception as e:
        import traceback
        _log(f"Company research FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


def _run_enrichment(run_id: str, batch_size: int = 200):
    """Enrich gold.schema_inventory using ai_query() as a per-row column expression.

    Joins table names from silver_tables into the prompt so the LLM can
    produce richer, more accurate definitions. Spark/DBSQL handles per-row
    parallelism automatically.
    """

    def _log(msg: str):
        print(f"[{run_id}] {msg}", flush=True)

    gold = get_gold_schema()
    silver = get_silver_schema()
    inv = fqn(gold, "schema_inventory")
    stables = fqn(silver, "silver_tables")

    company = _sql_escape(_get_company_name())

    prompt_col = (
        "CONCAT("
        f"'You are a data catalog expert for {company}. "
        "Given this schema and its table names, return a JSON object with: "
        "definition (2-3 sentence business description — what data lives here, what business process it supports, who uses it), "
        "business_name (concise human-friendly name), "
        "source_system (external source system this data originates from, e.g. SAP, Oracle, Salesforce, or Internal), "
        "data_domain (e.g. Finance, Operations, HR, Customer, Asset Management, Supply Chain), "
        "department_owner (which department owns this data), "
        "sensitivity (Public, Internal, Confidential, or Restricted), "
        "data_quality_tier (Raw, Cleansed, Curated, or Certified). "
        "Schema: catalog=', si.catalog_name, "
        "' schema=', si.schema_name, "
        "' program=', COALESCE(si.program, ''), "
        "' affiliate=', COALESCE(si.affiliate, ''), "
        "' zone=', COALESCE(si.zone, ''), "
        "' table_count=', CAST(si.table_count AS STRING), "
        "' comment=', COALESCE(LEFT(si.comment, 100), ''), "
        "' table_names=', COALESCE(tl.table_list, 'none')"
        ")"
    )

    json_schema = (
        "definition STRING, business_name STRING, source_system STRING, "
        "data_domain STRING, department_owner STRING, sensitivity STRING, "
        "data_quality_tier STRING"
    )

    try:
        _active_runs[run_id]["status"] = "RUNNING"
        _log("Starting AI enrichment of schema_inventory...")

        count_rows = execute_query(f"""
            SELECT COUNT(*) as cnt FROM {inv}
            WHERE (definition IS NULL OR definition = '')
              AND classification = 'PRODUCTION'
              AND is_user_edited = false        """)
        to_enrich = int(count_rows[0]["cnt"]) if count_rows else 0
        _log(f"Found {to_enrich} unenriched schemas (will process up to {batch_size})")

        if to_enrich == 0:
            _active_runs[run_id]["status"] = "TERMINATED"
            _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
            _log("No schemas to enrich.")
            return

        sql = f"""
            MERGE INTO {inv} AS target
            USING (
                WITH candidates AS (
                    SELECT schema_key, catalog_name, schema_name, program,
                           affiliate, zone, table_count, comment
                    FROM {inv}
                    WHERE (definition IS NULL OR definition = '')
                      AND classification = 'PRODUCTION'
                      AND is_user_edited = false                    LIMIT {batch_size}
                ),
                table_lists AS (
                    SELECT
                        table_catalog,
                        table_schema,
                        array_join(SLICE(collect_set(table_name), 1, 50), ', ') AS table_list
                    FROM {stables}
                    GROUP BY table_catalog, table_schema
                ),
                raw_ai AS (
                    -- ai_query(..., failOnError => false) returns
                    -- STRUCT<result: STRING, errorMessage: STRING>. We only
                    -- care about the text response here -- erroring rows
                    -- come back with result = NULL and are filtered out
                    -- by the downstream "WHERE raw_json IS NOT NULL" guard.
                    SELECT
                        si.schema_key,
                        ai_query(
                            '{LLM_ENDPOINT}',
                            {prompt_col},
                            failOnError => false
                        ).result AS raw_json
                    FROM candidates si
                    LEFT JOIN table_lists tl
                        ON si.catalog_name = tl.table_catalog
                       AND si.schema_name = tl.table_schema
                ),
                cleaned AS (
                    SELECT
                        schema_key,
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(raw_json, '^```\\\\w*\\\\n?', ''),
                            '\\\\n?```$', ''
                        ) AS clean_json
                    FROM raw_ai
                    WHERE raw_json IS NOT NULL
                ),
                parsed AS (
                    SELECT
                        schema_key,
                        from_json(TRIM(clean_json), '{json_schema}') AS c,
                        ROW_NUMBER() OVER (PARTITION BY schema_key ORDER BY schema_key) AS rn
                    FROM cleaned
                )
                SELECT schema_key, c FROM parsed
                WHERE rn = 1
                  AND c.definition IS NOT NULL
                  AND c.definition != ''
            ) AS src
            ON target.schema_key = src.schema_key
            WHEN MATCHED THEN UPDATE SET
                target.definition = src.c.definition,
                target.business_name = src.c.business_name,
                target.source_system = src.c.source_system,
                target.data_domain = src.c.data_domain,
                target.department_owner = src.c.department_owner,
                target.sensitivity = src.c.sensitivity,
                target.data_quality_tier = src.c.data_quality_tier,
                target.enriched_at = current_timestamp()
        """

        _log(f"Running ai_query per-row enrichment on up to {batch_size} schemas (with table names)...")
        execute_query(sql, poll_timeout=1800)

        total_enriched = execute_query(
            f"SELECT count(*) as cnt FROM {inv} WHERE definition != '' AND definition IS NOT NULL"
        )
        _log(f"Enrichment complete. Total enriched in inventory: {total_enriched[0]['cnt'] if total_enriched else 0}")

        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()

    except Exception as e:
        import traceback
        _log(f"Enrichment FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


@router.get("/jobs/pipeline-status", operation_id="pipelineStatus")
async def pipeline_status() -> dict:
    """Return last-run timestamps and enrichment counts for each pipeline job."""
    gold = get_gold_schema()
    silver = get_silver_schema()
    inv = fqn(gold, "schema_inventory")
    tax = fqn(gold, "schema_taxonomy")
    stables = fqn(silver, "silver_tables")

    try:
        rows = execute_query(f"""
            SELECT
                -- Gold layer (created is a string timestamp in schema_inventory)
                (SELECT MAX(created) FROM {inv}) AS gold_last_run,
                (SELECT COUNT(*) FROM {inv}) AS gold_total,
                -- Schema enrichment
                (SELECT MAX(enriched_at) FROM {inv}
                 WHERE definition IS NOT NULL AND definition != '') AS enrich_last_run,
                (SELECT COUNT(*) FROM {inv}
                 WHERE definition IS NOT NULL AND definition != '') AS enrich_done,
                (SELECT COUNT(*) FROM {inv}
                 WHERE (definition IS NULL OR definition = '')
                   AND classification = 'PRODUCTION'
                   AND is_user_edited = false) AS enrich_remaining,
                -- Taxonomy (created_at is a timestamp in schema_taxonomy)
                (SELECT MAX(created_at) FROM {tax}
                 WHERE effective_to IS NULL) AS taxonomy_last_run,
                (SELECT COUNT(DISTINCT schema_key) FROM {tax}
                 WHERE effective_to IS NULL) AS taxonomy_done
        """, poll_timeout=30)
        r = rows[0] if rows else {}
    except Exception:
        r = {}

    # Table enrichment: check the Databricks job directly
    table_enrich = {"status": "UNKNOWN", "last_run": None, "run_page_url": None}
    try:
        db = DatabricksClient()
        JOB_NAME = "BHE AI Table Enrichment"
        jobs_list = list(db.jobs.list(name="Table Enrichment"))
        matched = [j for j in jobs_list if JOB_NAME in (j.settings.name if j.settings else "")]
        if not matched:
            jobs_list = list(db.jobs.list())
            matched = [j for j in jobs_list if JOB_NAME in (j.settings.name if j.settings else "")]
        if matched:
            job_id = matched[0].job_id
            runs = db.api("GET", "/api/2.1/jobs/runs/list",
                          params={"job_id": job_id, "limit": 1})
            run_list = runs.get("runs", [])
            if run_list:
                run = run_list[0]
                state = run.get("state", {})
                table_enrich = {
                    "status": state.get("result_state") or state.get("life_cycle_state", "UNKNOWN"),
                    "last_run": run.get("end_time") or run.get("start_time"),
                    "run_page_url": run.get("run_page_url", ""),
                    "job_id": job_id,
                }
    except Exception:
        pass

    return {
        "populate_gold": {
            "last_run": r.get("gold_last_run"),
            "total_schemas": int(r["gold_total"]) if r.get("gold_total") else 0,
        },
        "enrich_schemas": {
            "last_run": r.get("enrich_last_run"),
            "enriched": int(r["enrich_done"]) if r.get("enrich_done") else 0,
            "remaining": int(r["enrich_remaining"]) if r.get("enrich_remaining") else 0,
        },
        "generate_taxonomy": {
            "last_run": r.get("taxonomy_last_run"),
            "classified": int(r["taxonomy_done"]) if r.get("taxonomy_done") else 0,
        },
        "enrich_tables": table_enrich,
    }


@router.post("/jobs/enrich", operation_id="triggerEnrichJob")
async def trigger_enrich_job(batch_size: int = Query(1000)) -> JobTriggerOut:
    """Enrich un-enriched schemas using ai_query() serverless batch inference."""
    run_id = str(uuid.uuid4())[:8]
    _active_runs[run_id] = {
        "status": "PENDING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
    }
    thread = threading.Thread(
        target=_run_enrichment, args=(run_id, batch_size), daemon=True
    )
    thread.start()
    return JobTriggerOut(run_id=run_id, job_id="inline-enrich", status="QUEUED")


def _run_table_enrichment(run_id: str, schema_name: str, batch_size: int = 100):
    """Enrich silver_tables with ai_query() using schema context for richer definitions."""

    def _log(msg: str):
        print(f"[table-enrich-{run_id}] {msg}", flush=True)

    gold = get_gold_schema()
    silver = get_silver_schema()
    inv = fqn(gold, "schema_inventory")
    stables = fqn(silver, "silver_tables")

    company = _sql_escape(_get_company_name())

    prompt_col = (
        "CONCAT("
        f"'You are a data catalog expert for {company}. "
        "Given a table name and its schema context, return a JSON with: "
        "business_friendly_name (concise human-friendly name for this table), "
        "ai_definition (1-2 sentence business description of what data this table contains), "
        "source_system (the specific external source system this table''s data originates from, or Internal). "
        "Context: catalog=', t.table_catalog, "
        "' schema=', t.table_schema, "
        "' table=', t.table_name, "
        "' table_type=', COALESCE(t.table_type, ''), "
        "' format=', COALESCE(t.data_source_format, ''), "
        "' comment=', COALESCE(LEFT(t.comment, 200), ''), "
        "' schema_definition=', COALESCE(LEFT(si.definition, 300), ''), "
        "' schema_business_name=', COALESCE(si.business_name, ''), "
        "' program=', COALESCE(si.program, ''), "
        "' affiliate=', COALESCE(si.affiliate, ''), "
        "' zone=', COALESCE(si.zone, '')"
        ")"
    )

    json_schema = "business_friendly_name STRING, ai_definition STRING, source_system STRING"

    schema_filter = ""
    if schema_name:
        esc_schema = schema_name.replace("'", "''")
        schema_filter = f"AND t.table_schema = '{esc_schema}'"

    try:
        _active_runs[run_id]["status"] = "RUNNING"
        _log(f"Starting table enrichment{' for schema ' + schema_name if schema_name else ''}...")

        count_rows = execute_query(f"""
            SELECT COUNT(*) as cnt FROM {stables} t
            WHERE (t.ai_definition IS NULL OR t.ai_definition = ''
                   OR t.source_system IS NULL OR t.source_system = '')
              AND t.classification = 'PRODUCTION'              AND t.is_user_edited = false
              {schema_filter}
        """)
        to_enrich = int(count_rows[0]["cnt"]) if count_rows else 0
        _log(f"Found {to_enrich} unenriched tables (will process up to {batch_size})")

        if to_enrich == 0:
            _active_runs[run_id]["status"] = "TERMINATED"
            _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
            _log("No tables to enrich.")
            return

        sql = f"""
            MERGE INTO {stables} AS target
            USING (
                WITH candidates AS (
                    SELECT t.table_catalog, t.table_schema, t.table_name,
                           t.table_type, t.data_source_format, t.comment
                    FROM {stables} t
                    WHERE (t.ai_definition IS NULL OR t.ai_definition = ''
                           OR t.source_system IS NULL OR t.source_system = '')
                      AND t.classification = 'PRODUCTION'                      AND t.is_user_edited = false
                      {schema_filter}
                    LIMIT {batch_size}
                ),
                raw_ai AS (
                    -- See note in _run_enrichment: extract .result so the
                    -- downstream REGEXP_REPLACE/from_json see a STRING.
                    SELECT
                        t.table_catalog, t.table_schema, t.table_name,
                        ai_query(
                            '{LLM_ENDPOINT}',
                            {prompt_col},
                            failOnError => false
                        ).result AS raw_json
                    FROM candidates t
                    LEFT JOIN {inv} si
                        ON t.table_catalog = si.catalog_name
                       AND t.table_schema = si.schema_name
                ),
                cleaned AS (
                    SELECT
                        table_catalog, table_schema, table_name,
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(raw_json, '^```\\\\w*\\\\n?', ''),
                            '\\\\n?```$', ''
                        ) AS clean_json
                    FROM raw_ai
                    WHERE raw_json IS NOT NULL
                ),
                parsed AS (
                    SELECT
                        table_catalog, table_schema, table_name,
                        from_json(TRIM(clean_json), '{json_schema}') AS c,
                        ROW_NUMBER() OVER (PARTITION BY table_catalog, table_schema, table_name ORDER BY table_name) AS rn
                    FROM cleaned
                )
                SELECT table_catalog, table_schema, table_name, c FROM parsed
                WHERE rn = 1
                  AND c.ai_definition IS NOT NULL
                  AND c.ai_definition != ''
            ) AS src
            ON target.table_catalog = src.table_catalog
               AND target.table_schema = src.table_schema
               AND target.table_name = src.table_name
            WHEN MATCHED THEN UPDATE SET
                target.ai_definition = src.c.ai_definition,
                target.business_friendly_name = src.c.business_friendly_name,
                target.source_system = src.c.source_system
        """

        _log(f"Running ai_query per-row table enrichment on up to {batch_size} tables...")
        execute_query(sql, poll_timeout=1800)

        total_enriched = execute_query(
            f"SELECT count(*) as cnt FROM {stables} WHERE ai_definition != '' AND ai_definition IS NOT NULL"
        )
        _log(f"Table enrichment complete. Total enriched: {total_enriched[0]['cnt'] if total_enriched else 0}")

        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()

    except Exception as e:
        import traceback
        _log(f"Table enrichment FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


@router.post("/jobs/enrich-tables", operation_id="triggerTableEnrichJob")
async def trigger_table_enrich_job(
    batch_size: int = Query(200),
    max_batches: int = Query(500),
) -> JobTriggerOut:
    """Submit the table enrichment job to Databricks (runs independently)."""
    db = DatabricksClient()
    JOB_NAME = "BHE AI Table Enrichment"
    jobs = db.jobs.list(name="Table Enrichment")
    matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
    if not matched:
        jobs = db.jobs.list()
        matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
    if not matched:
        raise HTTPException(404, f"Databricks job containing '{JOB_NAME}' not found")
    job_id = matched[0]["job_id"]
    result = db.jobs.run_now(job_id, python_params=[
        "--batch-size", str(batch_size),
        "--max-batches", str(max_batches),
    ])
    db_run_id = str(result.get("run_id", ""))
    _active_runs[db_run_id] = {
        "status": "RUNNING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
        "databricks_job_id": job_id,
        "databricks_run_id": result.get("run_id"),
    }
    return JobTriggerOut(run_id=db_run_id, job_id=str(job_id), status="RUNNING")


@router.get("/jobs/enrich-tables/status", operation_id="tableEnrichJobStatus")
async def table_enrich_job_status() -> dict:
    """Check the latest run status of the table enrichment Databricks job."""
    db = DatabricksClient()
    JOB_NAME = "BHE AI Table Enrichment"
    jobs = db.jobs.list(name="Table Enrichment")
    matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
    if not matched:
        jobs = db.jobs.list()
        matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
    if not matched:
        return {"status": "NOT_FOUND", "message": "Job not configured"}
    job_id = matched[0]["job_id"]
    runs = db.api("GET", "/api/2.1/jobs/runs/list", params={"job_id": job_id, "limit": 1})
    run_list = runs.get("runs", [])
    if not run_list:
        return {"status": "NEVER_RUN", "job_id": job_id}
    run = run_list[0]
    state = run.get("state", {})
    life_cycle = state.get("life_cycle_state", "UNKNOWN")
    result_state = state.get("result_state", "")
    return {
        "status": result_state or life_cycle,
        "life_cycle_state": life_cycle,
        "result_state": result_state,
        "run_id": run.get("run_id"),
        "job_id": job_id,
        "start_time": run.get("start_time"),
        "end_time": run.get("end_time"),
        "run_page_url": run.get("run_page_url", ""),
    }


@router.get("/jobs/company-research/active", operation_id="activeCompanyResearch")
async def active_company_research() -> dict:
    """Return the currently active company-research run, if any.

    Checks Databricks active runs for the company research job and extracts
    the --run-id python param. Used by the UI to recover the active run_id
    after a page refresh.
    """
    for rid, info in _active_runs.items():
        if info.get("status") in ("RUNNING", "PENDING") and info.get("job_type") == "company-research":
            return {
                "run_id": rid,
                "db_run_id": info.get("db_run_id"),
                "source": "memory",
            }

    try:
        db = DatabricksClient()
        JOB_NAME = "Company Research"
        jobs = db.jobs.list(name=JOB_NAME)
        matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
        if not matched:
            all_jobs = db.jobs.list()
            matched = [j for j in all_jobs if JOB_NAME in j.get("settings", {}).get("name", "")]

        for j in matched:
            jid = j.get("job_id")
            if not jid:
                continue
            runs = db.api("GET", "/api/2.1/jobs/runs/list",
                          params={"job_id": jid, "active_only": "true", "limit": 10})
            for r in runs.get("runs", []):
                params = (r.get("overriding_parameters") or {}).get("python_params", []) or []
                run_id = None
                for i in range(len(params) - 1):
                    if params[i] == "--run-id":
                        run_id = params[i + 1]
                        break
                if not run_id:
                    continue
                db_run_id = str(r.get("run_id", ""))
                _active_runs.setdefault(run_id, {
                    "status": "RUNNING",
                    "start_time": datetime.utcnow().isoformat(),
                    "end_time": None,
                    "error": None,
                    "job_type": "company-research",
                })["db_run_id"] = db_run_id
                return {
                    "run_id": run_id,
                    "db_run_id": db_run_id,
                    "source": "databricks",
                }
    except Exception as e:
        logger.warning(f"Failed to check active company-research runs: {e}")

    return {"run_id": None}


ALL_RESEARCH_STEPS = ["profile", "departments", "usecases", "entities", "sankey"]

_RESEARCH_TABLES = [
    "company_profile",
    "departments",
    "use_cases",
    "use_case_entities",
    "sankey_mappings",
]


def _wipe_research_tables() -> None:
    """Delete all rows from the company-research Delta tables (keeps schema/history)."""
    silver = get_silver_schema()
    for t in _RESEARCH_TABLES:
        try:
            execute_query(f"DELETE FROM {fqn(silver, t)} WHERE 1=1")
        except Exception as e:
            logger.warning(f"Could not wipe {t}: {e}")


@router.get("/company/research-status", operation_id="companyResearchStatus")
async def company_research_status() -> dict:
    """Return a per-step completeness summary so the UI can offer Resume vs Reset."""
    silver = get_silver_schema()

    def _count(table: str) -> int:
        try:
            rows = execute_query(f"SELECT count(*) AS c FROM {fqn(silver, table)}")
            if rows:
                c = rows[0].get("c", 0)
                return int(c) if c is not None else 0
        except Exception:
            return 0
        return 0

    counts = {t: _count(t) for t in _RESEARCH_TABLES}

    step_to_table = {
        "profile": "company_profile",
        "departments": "departments",
        "usecases": "use_cases",
        "entities": "use_case_entities",
        "sankey": "sankey_mappings",
    }
    steps_complete = [s for s, t in step_to_table.items() if counts[t] > 0]
    missing_steps = [s for s in ALL_RESEARCH_STEPS if s not in steps_complete]

    state = (
        "empty" if not steps_complete
        else ("complete" if not missing_steps else "partial")
    )

    return {
        "state": state,
        "counts": counts,
        "complete_steps": steps_complete,
        "missing_steps": missing_steps,
        "all_steps": ALL_RESEARCH_STEPS,
    }


@router.post("/jobs/company-research", operation_id="triggerCompanyResearch")
async def trigger_company_research(body: CompanyResearchIn) -> JobTriggerOut:
    """Submit company research as a Databricks job.

    Body fields:
      - company_name: required
      - reset: if true, wipe all research tables first (full re-run)
      - steps: list of step names to run; if omitted, all steps are requested
               (but each step self-skips when output already exists, unless force=true)
      - force: force re-generation for the requested steps even if outputs exist
    """

    for rid, info in _active_runs.items():
        if info.get("status") in ("RUNNING", "PENDING") and info.get("job_type") == "company-research":
            raise HTTPException(409, f"Company research already running (run_id={rid})")

    db = DatabricksClient()
    JOB_NAME = "BHE Company Research"
    jobs = db.jobs.list(name="Company Research")
    matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
    if not matched:
        jobs = db.jobs.list()
        matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]

    if matched:
        job_id = matched[0]["job_id"]
        try:
            active_runs = db.api("GET", "/api/2.1/jobs/runs/list",
                                 params={"job_id": job_id, "active_only": "true", "limit": 1})
            if active_runs.get("runs"):
                raise HTTPException(409, "Company research job is already running on Databricks")
        except HTTPException:
            raise
        except Exception:
            pass

    steps = body.steps if body.steps else list(ALL_RESEARCH_STEPS)
    unknown = [s for s in steps if s not in ALL_RESEARCH_STEPS]
    if unknown:
        raise HTTPException(400, f"Unknown step(s): {unknown}. Valid: {ALL_RESEARCH_STEPS}")

    if body.reset:
        logger.info(f"Reset requested - wiping research tables before run")
        _wipe_research_tables()
        steps = list(ALL_RESEARCH_STEPS)

    force_flag = bool(body.reset or body.force)

    if not matched:
        run_id = str(uuid.uuid4())[:8]
        _active_runs[run_id] = {
            "status": "PENDING",
            "start_time": datetime.utcnow().isoformat(),
            "end_time": None,
            "error": None,
            "job_type": "company-research",
        }
        # Pass the resolved steps + force flag so the inline runner honors
        # them. Previously these were dropped on the floor for the inline
        # path, which made `Resume Research` always re-run every step.
        # See onboarding-bug-backlog B-006.
        thread = threading.Thread(
            target=_run_company_research,
            args=(run_id, body.company_name, list(steps), force_flag),
            daemon=True,
        )
        thread.start()
        return JobTriggerOut(run_id=run_id, job_id="inline", status="QUEUED")

    job_id = matched[0]["job_id"]
    run_id = str(uuid.uuid4())[:8]
    python_params = [
        "--catalog", get_catalog(),
        "--silver-schema", get_silver_schema(),
        "--model-endpoint", "databricks-claude-sonnet-4-6",
        "--company-name", body.company_name,
        "--run-id", run_id,
        "--steps", ",".join(steps),
    ]
    if force_flag:
        python_params.append("--force")
    result = db.jobs.run_now(job_id, python_params=python_params)
    db_run_id = str(result.get("run_id", ""))
    _active_runs[run_id] = {
        "status": "RUNNING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
        "job_type": "company-research",
        "db_run_id": db_run_id,
    }
    return JobTriggerOut(run_id=run_id, job_id=str(job_id), status="RUNNING")


@router.get("/jobs/{run_id}/status", operation_id="jobStatus")
async def job_status(run_id: str) -> JobStatusOut:
    run = _active_runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run {run_id} not found")

    db_run_id = run.get("db_run_id")
    if db_run_id and run.get("status") in ("RUNNING", "PENDING"):
        try:
            db = DatabricksClient()
            rr = db.api("GET", "/api/2.1/jobs/runs/get", params={"run_id": db_run_id})
            state = rr.get("state", {}) or {}
            lcs = state.get("life_cycle_state")
            rs = state.get("result_state")
            if lcs == "TERMINATED":
                run["status"] = "TERMINATED" if rs == "SUCCESS" else "FAILED"
                run["end_time"] = datetime.utcnow().isoformat()
                if rs != "SUCCESS":
                    run["error"] = state.get("state_message", "")
            elif lcs in ("INTERNAL_ERROR", "SKIPPED"):
                run["status"] = "FAILED"
                run["end_time"] = datetime.utcnow().isoformat()
                run["error"] = state.get("state_message", "")
        except Exception as e:
            logger.warning(f"Failed to check Databricks run state: {e}")

    return JobStatusOut(
        run_id=run_id,
        status=run["status"],
        start_time=run.get("start_time"),
        end_time=run.get("end_time"),
        error=run.get("error"),
    )


@router.get("/jobs/{run_id}/progress", operation_id="jobProgress")
async def job_progress(run_id: str) -> dict:
    """Return granular progress steps for a company research run."""
    silver = get_silver_schema()

    run = _active_runs.get(run_id, {})
    db_run_id = run.get("db_run_id")
    run_page_url = ""
    db = None
    try:
        db = DatabricksClient()
    except Exception:
        db = None

    if db and not db_run_id:
        try:
            JOB_NAME = "Company Research"
            jobs = db.jobs.list(name=JOB_NAME)
            matched = [j for j in jobs if JOB_NAME in j.get("settings", {}).get("name", "")]
            if not matched:
                all_jobs = db.jobs.list()
                matched = [j for j in all_jobs if JOB_NAME in j.get("settings", {}).get("name", "")]

            for j in matched:
                jid = j.get("job_id")
                if not jid:
                    continue
                runs = db.api("GET", "/api/2.1/jobs/runs/list",
                              params={"job_id": jid, "active_only": "true", "limit": 10})
                for r in runs.get("runs", []):
                    params = (r.get("overriding_parameters") or {}).get("python_params", []) or []
                    for i in range(len(params) - 1):
                        if params[i] == "--run-id" and params[i + 1] == run_id:
                            db_run_id = str(r.get("run_id", ""))
                            run_page_url = r.get("run_page_url", "") or ""
                            if run_id in _active_runs:
                                _active_runs[run_id]["db_run_id"] = db_run_id
                            break
                    if db_run_id:
                        break
                if db_run_id:
                    break
        except Exception as e:
            logger.warning(f"Failed to discover Databricks run for {run_id}: {e}")

    if db and db_run_id and not run_page_url:
        try:
            rr = db.api("GET", "/api/2.1/jobs/runs/get", params={"run_id": db_run_id})
            run_page_url = rr.get("run_page_url", "") or ""
        except Exception:
            pass

    def _to_int(v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    try:
        rows = execute_query(
            f"SELECT step, step_index, total_steps, item_name, parent_item, detail, "
            f"CAST(created_at AS STRING) AS created_at "
            f"FROM {fqn(silver, 'job_progress')} "
            f"WHERE run_id = '{_sql_escape(run_id)}' "
            f"ORDER BY step_index, created_at"
        )
    except Exception:
        return {"run_id": run_id, "steps": [], "pct_complete": 0,
                "total_steps": 0, "run_page_url": run_page_url}

    for r in rows:
        r["step_index"] = _to_int(r.get("step_index"))
        r["total_steps"] = _to_int(r.get("total_steps"))

    if not rows:
        return {"run_id": run_id, "steps": [], "pct_complete": 0,
                "total_steps": 0, "run_page_url": run_page_url}

    total = max((r["total_steps"] for r in rows), default=1) or 1
    completed_steps = set()
    for r in rows:
        step = r.get("step", "")
        if step not in ("dept_item",):
            completed_steps.add(step)
    pct = min(round(len(completed_steps) / max(total, 1) * 100), 100)

    return {
        "run_id": run_id,
        "steps": rows,
        "pct_complete": pct,
        "total_steps": total,
        "run_page_url": run_page_url,
    }


# ---------------------------------------------------------------------------
# Schema Extractor Download
# ---------------------------------------------------------------------------

@router.get("/tools/schema-extractor", operation_id="downloadSchemaExtractor")
async def download_schema_extractor():
    """Download the Schema Extractor utility as a ZIP file."""
    import io
    import zipfile
    from pathlib import Path

    extractor_dir = Path(__file__).resolve().parents[4] / "schema-extractor"
    if not extractor_dir.exists():
        extractor_dir = Path(__file__).resolve().parents[5] / "schema-extractor"
    if not extractor_dir.exists():
        raise HTTPException(404, "Schema extractor directory not found")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in sorted(extractor_dir.rglob("*")):
            if fpath.is_file() and "__pycache__" not in str(fpath) and ".csv" not in fpath.name:
                arcname = f"schema-extractor/{fpath.relative_to(extractor_dir)}"
                zf.write(fpath, arcname)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=schema-extractor.zip"},
    )


# ---------------------------------------------------------------------------
# File Upload & Ingest
# ---------------------------------------------------------------------------

VOLUME_PATH = f"/Volumes/{get_catalog()}/{get_raw_schema()}/uploads"


@router.post("/upload/file", operation_id="uploadFile")
async def upload_file(
    file: UploadFile = File(...),
    file_type: str = Form("schemas"),
):
    """Upload a CSV file to the UC Volume, then ingest into silver tables."""
    import requests as req

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported")

    from .db import _get_headers, _get_host
    host = _get_host()
    auth_headers = _get_headers()

    content = await file.read()
    target = f"{VOLUME_PATH}/{file.filename}"
    logger.info(f"Uploading {file.filename} ({len(content)} bytes) to {target}")

    resp = req.put(
        f"{host}/api/2.0/fs/files{target}",
        headers={**auth_headers, "Content-Type": "application/octet-stream"},
        data=content,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(502, f"Volume upload failed: {resp.status_code} {resp.text[:200]}")

    return {"status": "uploaded", "path": target, "size": len(content), "filename": file.filename}


@router.get("/upload/files", operation_id="listUploadedFiles")
async def list_uploaded_files():
    """List files in the upload Volume."""
    try:
        rows = execute_query(f"LIST '{VOLUME_PATH}'")
        files = [
            {"name": r.get("name", ""), "path": r.get("path", ""), "size": r.get("size", 0)}
            for r in rows
        ]
        return {"files": files}
    except Exception:
        return {"files": []}


# ---------------------------------------------------------------------------
# Re-ingest is MERGE-preserving (was DROP+CTAS).
#
# Why this matters:
#   - User-edited rows (is_user_edited=true) survive a re-ingest.
#   - LLM-enriched columns (ai_definition, business_friendly_name) survive.
#   - silver_tables.source_system / source_system_canonical (added by the
#     normalize_source_systems job) are pre-declared in `_SETUP_SILVER_DDL`,
#     so they survive too — a fresh CSV upload no longer drops them.
#   - Schemas/tables that disappeared from the new CSV are removed via
#     WHEN NOT MATCHED BY SOURCE, but only when the row isn't user-edited.
#
# See onboarding-bug-backlog "Circular dep B".
# ---------------------------------------------------------------------------


def _ensure_silver_table_exists(table: str) -> None:
    """Idempotently create a silver table from `_SETUP_SILVER_DDL` if missing.

    Defensive: bootstrap is supposed to run before ingest, but users sometimes
    hit ingest endpoints out of order. Cheap to re-CREATE (IF NOT EXISTS).
    """
    if table not in _SETUP_SILVER_DDL:
        return
    catalog = get_catalog()
    silver = get_silver_schema()
    ddl = _SETUP_SILVER_DDL[table].format(fqn=f"{catalog}.{silver}.{table}")
    try:
        execute_update(
            ddl,
            tag_overrides={"submodule": f"ingest_ensure_{table}"},
        )
    except Exception as e:
        logger.warning(f"_ensure_silver_table_exists({table}) failed: {e}")


@router.post("/ingest/schemas", operation_id="ingestSchemas")
async def ingest_schemas(filename: str = Query("all_schemas_dbrk.csv")):
    """Read a schemas CSV from the Volume and MERGE into silver_schemas.

    Preserves user-edited rows and the rule-derived columns the catalog
    browser expects. Removes schemas that no longer appear in the CSV
    (unless they're user-edited).
    """
    catalog = get_catalog()
    silver = get_silver_schema()
    raw = get_raw_schema()
    vol_path = f"/Volumes/{catalog}/{raw}/uploads/{filename}"
    target = fqn(silver, "silver_schemas")

    _ensure_silver_table_exists("silver_schemas")

    try:
        execute_query(f"""
            MERGE INTO {target} AS t
            USING (
              SELECT
                catalog_name,
                schema_name,
                COALESCE(schema_owner, '') AS schema_owner,
                COALESCE(comment, '') AS comment,
                COALESCE(created, '') AS created,
                COALESCE(last_altered, '') AS last_altered,
                COALESCE(workspace_url, '') AS workspace_url,
                CASE
                  WHEN lower(catalog_name) LIKE '__databricks%' OR lower(catalog_name) IN ('system','samples') THEN 'SYSTEM'
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
                  WHEN lower(catalog_name) LIKE '%analytics%' THEN 'ANALYTICS'
                  WHEN lower(catalog_name) LIKE '%oracle%' OR lower(catalog_name) LIKE '%sqlserver%' THEN 'FEDERATED'
                  ELSE 'OTHER'
                END AS zone,
                CASE
                  WHEN lower(catalog_name) LIKE 'apm_%' THEN 'Asset Performance Management'
                  WHEN lower(catalog_name) LIKE 'bhermred_%' THEN 'BHE Renewable Energy'
                  WHEN lower(catalog_name) LIKE 'fdm_%' THEN 'Financial Data Management'
                  WHEN lower(catalog_name) LIKE 'gtsdl_%' THEN 'GTS Data Lake'
                  WHEN lower(catalog_name) LIKE 'nvedl_%' THEN 'NV Energy Data Lake'
                  WHEN lower(catalog_name) LIKE 'pac_%' THEN 'PacifiCorp'
                  WHEN lower(catalog_name) LIKE 'trp_%' THEN 'Transmission & Reliability Planning'
                  WHEN lower(catalog_name) LIKE 'whub_%' THEN 'Western Hub'
                  ELSE 'Unknown'
                END AS program,
                CASE
                  WHEN lower(catalog_name) LIKE '__databricks%' THEN 'INTERNAL'
                  WHEN lower(schema_name) = 'information_schema' THEN 'SYSTEM'
                  WHEN lower(schema_name) = 'default' THEN 'DEFAULT'
                  WHEN lower(schema_name) LIKE '%test%' OR lower(schema_name) LIKE '%poc%' THEN 'TEST'
                  WHEN lower(schema_name) LIKE 'wflw_%' THEN 'MIGRATION'
                  ELSE 'PRODUCTION'
                END AS classification
              FROM read_files('{vol_path}', format => 'csv', header => true, multiLine => true, escape => '"')
            ) AS s
            ON  t.catalog_name = s.catalog_name
            AND t.schema_name  = s.schema_name
            WHEN MATCHED AND COALESCE(t.is_user_edited, false) = false THEN UPDATE SET
              t.schema_owner   = s.schema_owner,
              t.comment        = s.comment,
              t.created        = s.created,
              t.last_altered   = s.last_altered,
              t.workspace_url  = s.workspace_url,
              t.environment    = s.environment,
              t.zone           = s.zone,
              t.program        = s.program,
              t.classification = s.classification
            WHEN NOT MATCHED THEN INSERT (
              catalog_name, schema_name, schema_owner, comment, created, last_altered,
              workspace_url, environment, zone, program, classification,
              ai_definition, business_friendly_name, suggested_department, suggested_domain,
              data_sensitivity, is_user_edited, user_edited_at
            ) VALUES (
              s.catalog_name, s.schema_name, s.schema_owner, s.comment, s.created, s.last_altered,
              s.workspace_url, s.environment, s.zone, s.program, s.classification,
              '', '', '', '',
              '', false, ''
            )
            WHEN NOT MATCHED BY SOURCE AND COALESCE(t.is_user_edited, false) = false THEN DELETE
        """)
        count = execute_query(f"SELECT count(*) as cnt FROM {target}")
        total = count[0]["cnt"] if count else 0
        return {"status": "merged", "table": "silver_schemas", "rows": total}
    except Exception as e:
        raise HTTPException(500, f"Ingest failed: {e}")


@router.post("/ingest/tables", operation_id="ingestTables")
async def ingest_tables(filename: str = Query("all_tables_dbrk.csv")):
    """Read a tables CSV from the Volume and MERGE into silver_tables.

    Preserves user-edited rows, AI-enriched columns, and the source_system /
    source_system_canonical columns populated by the normalize job.
    """
    catalog = get_catalog()
    silver = get_silver_schema()
    raw = get_raw_schema()
    vol_path = f"/Volumes/{catalog}/{raw}/uploads/{filename}"
    target = fqn(silver, "silver_tables")

    _ensure_silver_table_exists("silver_tables")

    try:
        execute_query(f"""
            MERGE INTO {target} AS t
            USING (
              SELECT
                table_catalog,
                table_schema,
                table_name,
                COALESCE(table_type, '') AS table_type,
                COALESCE(table_owner, '') AS table_owner,
                COALESCE(comment, '') AS comment,
                COALESCE(created, '') AS created,
                COALESCE(last_altered, '') AS last_altered,
                COALESCE(data_source_format, '') AS data_source_format
              FROM read_files('{vol_path}', format => 'csv', header => true, multiLine => true, escape => '"')
            ) AS s
            ON  t.table_catalog = s.table_catalog
            AND t.table_schema  = s.table_schema
            AND t.table_name    = s.table_name
            WHEN MATCHED AND COALESCE(t.is_user_edited, false) = false THEN UPDATE SET
              t.table_type         = s.table_type,
              t.table_owner        = s.table_owner,
              t.comment            = s.comment,
              t.created            = s.created,
              t.last_altered       = s.last_altered,
              t.data_source_format = s.data_source_format
            WHEN NOT MATCHED THEN INSERT (
              table_catalog, table_schema, table_name,
              table_type, table_owner, comment, created, last_altered, data_source_format,
              classification, ai_definition, business_friendly_name,
              is_user_edited, user_edited_at,
              source_system, source_system_canonical
            ) VALUES (
              s.table_catalog, s.table_schema, s.table_name,
              s.table_type, s.table_owner, s.comment, s.created, s.last_altered, s.data_source_format,
              'PRODUCTION', '', '',
              false, '',
              CAST(NULL AS STRING), CAST(NULL AS STRING)
            )
            WHEN NOT MATCHED BY SOURCE AND COALESCE(t.is_user_edited, false) = false THEN DELETE
        """)
        count = execute_query(f"SELECT count(*) as cnt FROM {target}")
        total = count[0]["cnt"] if count else 0
        return {"status": "merged", "table": "silver_tables", "rows": total}
    except Exception as e:
        raise HTTPException(500, f"Ingest tables failed: {e}")


# ---------------------------------------------------------------------------
# Gold Layer Population & Analytics
# ---------------------------------------------------------------------------


def _load_rules(gold: str) -> dict:
    """Load classification rules from the rules table, grouped by category.

    Returns an empty dict (not a 500) when the table is missing or empty so
    callers downstream get a usable shape. Missing rules => no catalogs/schemas
    are filtered, no programs/zones/environments are derived from rules — but
    `populate_gold` still runs and produces a sensible (un-classified) baseline.
    """
    try:
        rows = execute_query(
            f"SELECT category, pattern, label, metadata FROM {fqn(gold, 'classification_rules')} WHERE is_active = true ORDER BY display_order"
        )
    except Exception as e:
        logger.warning(f"_load_rules: classification_rules unavailable ({e}); proceeding with empty ruleset")
        return {}
    rules: dict = {}
    for r in rows or []:
        cat = r["category"]
        rules.setdefault(cat, []).append({
            "pattern": r["pattern"], "label": r["label"],
            "metadata": json.loads(r["metadata"]) if r.get("metadata") and r["metadata"] != '{}' else {},
        })
    return rules


def _seed_classification_rules_if_empty(gold: str) -> int:
    """Seed universal ignore patterns into `classification_rules` if empty.

    Idempotent: only inserts when the table has zero rows. Returns the number
    of rows inserted. Does not raise — failures are logged and swallowed so a
    transient seed failure doesn't break the wider bootstrap flow.
    """
    try:
        cnt = execute_query(
            f"SELECT count(*) AS n FROM {fqn(gold, 'classification_rules')}",
            tag_overrides={"submodule": "setup_seed_rules_probe"},
        )
        existing = int(cnt[0]["n"]) if cnt else 0
        if existing > 0:
            return 0
        values_sql = ",\n".join(
            f"('{r['rule_id']}', '{r['category']}', '{r['pattern']}', "
            f"'{r['label']}', '{r['description']}', '{{}}', true, {r['display_order']}, "
            f"current_timestamp(), current_timestamp())"
            for r in _CLASSIFICATION_RULES_SEED
        )
        execute_update(
            f"INSERT INTO {fqn(gold, 'classification_rules')} "
            f"(rule_id, category, pattern, label, description, metadata, is_active, display_order, created_at, updated_at) "
            f"VALUES {values_sql}",
            tag_overrides={"submodule": "setup_seed_rules_insert"},
        )
        return len(_CLASSIFICATION_RULES_SEED)
    except Exception as e:
        logger.warning(f"_seed_classification_rules_if_empty failed: {e}")
        return 0


def _glob_match(pattern: str, value: str) -> bool:
    """Simple glob matching: * matches any sequence of characters."""
    import fnmatch
    return fnmatch.fnmatch(value.lower(), pattern.lower())


def _parse_catalog(catalog: str, schema: str, rules: dict) -> dict:
    """Parse a catalog + schema name using loaded rules. Returns derived attributes."""
    cat_lower = catalog.lower()
    sch_lower = schema.lower()

    # Check ignore patterns
    for r in rules.get("ignore_catalog", []):
        if _glob_match(r["pattern"], cat_lower):
            return {"skip": True}
    for r in rules.get("ignore_schema", []):
        if _glob_match(r["pattern"], sch_lower):
            return {"skip": True}

    result = {"program": "", "affiliate": "", "environment": "", "zone": "",
              "classification": "PRODUCTION", "workspace_name": "", "federated_source": ""}

    # Check federated sources first (these are special catalogs)
    for r in rules.get("federated_source", []):
        if _glob_match(r["pattern"], cat_lower):
            result["federated_source"] = r["label"]
            result["classification"] = "FEDERATED"
            result["environment"] = "prod"
            # Try to extract program from the part before the suffix
            prefix = cat_lower
            for suffix in ["_oracle_v2", "_oracle", "_fivetran", "_sqlserver"]:
                if cat_lower.endswith(suffix):
                    prefix = cat_lower[: -len(suffix)]
                    break
            result["program"] = prefix
            result["zone"] = "federated"
            result["affiliate"] = r["label"]
            result["workspace_name"] = r["label"]
            return result

    # Standard naming: {program}_{env}{version}_{zone}
    # Try to match program prefix
    for r in rules.get("program", []):
        prog_code = r["pattern"]
        if cat_lower.startswith(prog_code + "_"):
            result["program"] = prog_code
            result["workspace_name"] = r["label"]
            meta = r.get("metadata", {})
            result["affiliate"] = meta.get("affiliate", "")
            remainder = cat_lower[len(prog_code) + 1:]  # e.g. "dev02_standardized"
            break
    else:
        # No program match - try to extract from first segment
        parts = cat_lower.split("_")
        result["program"] = parts[0] if parts else cat_lower
        result["workspace_name"] = "Other"
        result["affiliate"] = "Unknown"
        remainder = "_".join(parts[1:]) if len(parts) > 1 else ""

    # Extract environment from remainder (e.g. "dev02_standardized")
    for r in rules.get("environment", []):
        env_code = r["pattern"]  # e.g. "dev02"
        if remainder.startswith(env_code + "_") or remainder == env_code:
            result["environment"] = r["label"]  # e.g. "dev"
            zone_part = remainder[len(env_code) + 1:] if remainder.startswith(env_code + "_") else ""
            break
    else:
        # Fallback: regex extraction for env patterns like dev02, qa04
        import re
        m = re.match(r'(dev|qa|prod)\d+', remainder)
        if m:
            result["environment"] = m.group(1)
            zone_part = remainder[m.end():].lstrip("_")
        else:
            result["environment"] = "unknown"
            zone_part = remainder

    # Extract zone from remaining part
    if zone_part:
        for r in rules.get("zone", []):
            if zone_part == r["pattern"] or zone_part.startswith(r["pattern"]):
                result["zone"] = r["pattern"]
                meta = r.get("metadata", {})
                break
        else:
            result["zone"] = zone_part if zone_part else "other"
    else:
        result["zone"] = "other"

    # Classification overrides
    if sch_lower in ("information_schema", "default"):
        result["classification"] = "SYSTEM"
    elif sch_lower.startswith("migration_"):
        result["classification"] = "MIGRATION"

    return result


def _run_populate_gold(run_id: str):
    """Populate all gold layer tables from silver data using rule-based derivation."""

    def _log(msg: str):
        print(f"[{run_id}] {msg}", flush=True)

    silver = get_silver_schema()
    gold = get_gold_schema()

    try:
        _active_runs[run_id]["status"] = "RUNNING"

        # Load rules from DB
        _log("Loading classification rules...")
        rules = _load_rules(gold)
        _log(f"Loaded rules: {', '.join(f'{k}={len(v)}' for k, v in rules.items())}")

        _log("Step 1/4: Populating schema_inventory from silver data...")

        # Fetch all schemas + table counts
        schemas = execute_query(f"""
            SELECT s.workspace_url, s.catalog_name, s.schema_name, s.schema_owner,
                   s.created, s.last_altered, s.comment,
                   COALESCE(tc.tbl_count, 0) AS table_count,
                   COALESCE(tc.view_count, 0) AS view_count
            FROM {fqn(silver, 'silver_schemas')} s
            LEFT JOIN (
                SELECT table_catalog, table_schema,
                    SUM(CASE WHEN lower(table_type) != 'view' THEN 1 ELSE 0 END) AS tbl_count,
                    SUM(CASE WHEN lower(table_type) = 'view' THEN 1 ELSE 0 END) AS view_count
                FROM {fqn(silver, 'silver_tables')}
                GROUP BY table_catalog, table_schema
            ) tc ON tc.table_catalog = s.catalog_name AND tc.table_schema = s.schema_name
        """)
        _log(f"Fetched {len(schemas)} schema rows from silver")

        # Parse each schema using rules
        import re
        rows_to_insert = []
        skipped = 0
        for s in schemas:
            parsed = _parse_catalog(s["catalog_name"], s["schema_name"], rules)
            if parsed.get("skip"):
                skipped += 1
                continue

            ws_url = s.get("workspace_url", "")
            m = re.search(r'adb-(\d+)', ws_url)
            ws_id = m.group(1) if m else ""
            sk = f"{ws_id}|{s['catalog_name']}|{s['schema_name']}"

            def esc(v):
                return str(v or "").replace("'", "''")

            # NOTE: enriched_at uses an explicit CAST so the all-NULL column
            # in the VALUES clause doesn't get inferred as VOID and break the
            # MERGE binding to the TIMESTAMP target column.
            rows_to_insert.append(
                f"('{esc(sk)}', '{esc(ws_id)}', '{esc(ws_url)}', '{esc(parsed['workspace_name'])}', "
                f"'{esc(s['catalog_name'])}', '{esc(s['schema_name'])}', '{esc(s.get('schema_owner',''))}', "
                f"'{esc(parsed['program'])}', '{esc(parsed['affiliate'])}', '{esc(parsed['environment'])}', "
                f"'{esc(parsed['zone'])}', '{esc(parsed['classification'])}', "
                f"{s.get('table_count', 0)}, {s.get('view_count', 0)}, "
                f"'', '', '', '', '', '', '', "
                f"'{esc(s.get('created',''))}', '{esc(s.get('last_altered',''))}', "
                f"CAST(NULL AS TIMESTAMP), "
                f"false, '{esc(s.get('comment',''))}')"
            )
        _log(f"Parsed schemas: {len(rows_to_insert)} to insert, {skipped} skipped by ignore rules")

        # ------------------------------------------------------------------
        # MERGE-preserving upsert into schema_inventory.
        #
        # We deliberately do NOT touch:
        #   - LLM-enriched columns (definition, business_name, source_system,
        #     data_domain, department_owner, sensitivity, data_quality_tier,
        #     enriched_at) — these survive across populate-gold runs so the
        #     user doesn't lose hours of `ai_query` work on every reclassify.
        #   - User-edited rows (`is_user_edited = true`) — analyst overrides
        #     of program/affiliate/zone/classification stick.
        #
        # Net behavior for re-runs:
        #   - New schema discovered     → INSERT with empty enrichment
        #   - Existing rule-derived row → UPDATE rule cols only, keep enrichment
        #   - Existing user-edited row  → no-op
        #   - Schema vanished from src  → DELETE (final pass below)
        #
        # Was DELETE + batched INSERT; now MERGE preserves enrichment.
        # See onboarding-bug-backlog.md "Circular dep A".
        # ------------------------------------------------------------------
        seen_keys: list[str] = []
        BATCH = 200
        for i in range(0, len(rows_to_insert), BATCH):
            batch_rows = rows_to_insert[i:i + BATCH]
            batch_values = ", ".join(batch_rows)
            execute_query(f"""
                MERGE INTO {fqn(gold, 'schema_inventory')} AS target
                USING (
                    SELECT * FROM (VALUES {batch_values}) AS s(
                        schema_key, workspace_id, workspace_url, workspace_name,
                        catalog_name, schema_name, schema_owner,
                        program, affiliate, environment, zone, classification,
                        table_count, view_count,
                        definition, business_name, source_system, data_domain,
                        department_owner, sensitivity, data_quality_tier,
                        created, last_altered, enriched_at,
                        is_user_edited, comment
                    )
                ) AS src
                ON target.schema_key = src.schema_key
                WHEN MATCHED AND COALESCE(target.is_user_edited, false) = false THEN UPDATE SET
                    target.workspace_id = src.workspace_id,
                    target.workspace_url = src.workspace_url,
                    target.workspace_name = src.workspace_name,
                    target.catalog_name = src.catalog_name,
                    target.schema_name = src.schema_name,
                    target.schema_owner = src.schema_owner,
                    target.program = src.program,
                    target.affiliate = src.affiliate,
                    target.environment = src.environment,
                    target.zone = src.zone,
                    target.classification = src.classification,
                    target.table_count = src.table_count,
                    target.view_count = src.view_count,
                    target.created = src.created,
                    target.last_altered = src.last_altered,
                    target.comment = src.comment
                WHEN NOT MATCHED THEN INSERT (
                    schema_key, workspace_id, workspace_url, workspace_name,
                    catalog_name, schema_name, schema_owner,
                    program, affiliate, environment, zone, classification,
                    table_count, view_count,
                    definition, business_name, source_system, data_domain,
                    department_owner, sensitivity, data_quality_tier,
                    created, last_altered, enriched_at,
                    is_user_edited, comment
                ) VALUES (
                    src.schema_key, src.workspace_id, src.workspace_url, src.workspace_name,
                    src.catalog_name, src.schema_name, src.schema_owner,
                    src.program, src.affiliate, src.environment, src.zone, src.classification,
                    src.table_count, src.view_count,
                    src.definition, src.business_name, src.source_system, src.data_domain,
                    src.department_owner, src.sensitivity, src.data_quality_tier,
                    src.created, src.last_altered, src.enriched_at,
                    src.is_user_edited, src.comment
                )
            """)
            for row in batch_rows:
                # row starts with "('<schema_key>', '<workspace_id>', ..."
                # extract schema_key (first quoted value)
                m = re.match(r"^\(\s*'([^']*)'", row)
                if m:
                    seen_keys.append(m.group(1))
            if (i // BATCH) % 10 == 0:
                _log(f"  Merged {min(i + BATCH, len(rows_to_insert))}/{len(rows_to_insert)}...")

        # Final pass: remove rows for schemas that no longer appear in silver.
        # Skip user-edited rows so analysts don't lose work if a schema
        # temporarily disappears from extraction.
        if seen_keys:
            keys_in = ", ".join(f"'{k}'" for k in seen_keys)
            removed = execute_query(
                f"DELETE FROM {fqn(gold, 'schema_inventory')} "
                f"WHERE schema_key NOT IN ({keys_in}) "
                f"  AND COALESCE(is_user_edited, false) = false"
            )
            _log(f"  Removed schemas no longer in silver (preserving user-edited rows)")
        else:
            _log("  Skipping orphan-cleanup: no source rows produced this run")

        inv_cnt = execute_query(f"SELECT count(*) as cnt FROM {fqn(gold, 'schema_inventory')}")
        _log(f"schema_inventory populated: {inv_cnt[0]['cnt'] if inv_cnt else 0} rows (skipped {skipped} by ignore rules; LLM enrichment preserved across re-runs)")

        _log("Step 2/4: Populating source_summary...")
        execute_query(f"DELETE FROM {fqn(gold, 'source_summary')} WHERE 1=1")
        execute_query(f"""
            INSERT INTO {fqn(gold, 'source_summary')}
            (program, affiliate, dev_schemas, qa_schemas, prod_schemas,
             dev_tables, qa_tables, prod_tables, total_tables,
             consistency_score, schemas_only_dev, schemas_only_qa, schemas_only_prod,
             updated_at)
            WITH base AS (
                SELECT program, affiliate, schema_name, environment, table_count
                FROM {fqn(gold, 'schema_inventory')}
                WHERE classification NOT IN ('SYSTEM')            ),
            env_pivot AS (
                SELECT program, MAX(affiliate) AS affiliate,
                    COUNT(DISTINCT CASE WHEN environment='dev' THEN schema_name END) AS dev_schemas,
                    COUNT(DISTINCT CASE WHEN environment='qa' THEN schema_name END) AS qa_schemas,
                    COUNT(DISTINCT CASE WHEN environment='prod' THEN schema_name END) AS prod_schemas,
                    SUM(CASE WHEN environment='dev' THEN table_count ELSE 0 END) AS dev_tables,
                    SUM(CASE WHEN environment='qa' THEN table_count ELSE 0 END) AS qa_tables,
                    SUM(CASE WHEN environment='prod' THEN table_count ELSE 0 END) AS prod_tables,
                    SUM(table_count) AS total_tables
                FROM base GROUP BY program
            ),
            schema_envs AS (
                SELECT program, schema_name,
                    MAX(CASE WHEN environment='dev' THEN 1 ELSE 0 END) AS in_dev,
                    MAX(CASE WHEN environment='qa' THEN 1 ELSE 0 END) AS in_qa,
                    MAX(CASE WHEN environment='prod' THEN 1 ELSE 0 END) AS in_prod
                FROM base GROUP BY program, schema_name
            ),
            consistency AS (
                SELECT program,
                    ROUND(100.0 * SUM(CASE WHEN in_dev=1 AND in_qa=1 AND in_prod=1 THEN 1 ELSE 0 END) /
                        NULLIF(COUNT(*), 0), 1) AS consistency_score
                FROM schema_envs GROUP BY program
            )
            SELECT ep.program, ep.affiliate,
                ep.dev_schemas, ep.qa_schemas, ep.prod_schemas,
                ep.dev_tables, ep.qa_tables, ep.prod_tables, ep.total_tables,
                COALESCE(c.consistency_score, 0),
                '[]', '[]', '[]',
                current_timestamp()
            FROM env_pivot ep
            LEFT JOIN consistency c ON ep.program = c.program
            WHERE ep.total_tables > 0
            ORDER BY ep.total_tables DESC
        """)
        src_cnt = execute_query(f"SELECT count(*) as cnt FROM {fqn(gold, 'source_summary')}")
        _log(f"source_summary populated: {src_cnt[0]['cnt'] if src_cnt else 0} rows")

        _log("Step 3/4: Populating workspace_summary...")
        execute_query(f"DELETE FROM {fqn(gold, 'workspace_summary')} WHERE 1=1")

        ws_rows = execute_query(f"""
            SELECT
                workspace_id,
                MAX(workspace_url) AS workspace_url,
                MAX(workspace_name) AS workspace_name,
                COUNT(DISTINCT affiliate) AS n_affiliates,
                COUNT(DISTINCT program) AS n_programs,
                COUNT(DISTINCT catalog_name) AS catalog_count,
                COUNT(*) AS schema_count,
                SUM(table_count) AS table_count
            FROM {fqn(gold, 'schema_inventory')}
            WHERE classification != 'SYSTEM'
            GROUP BY workspace_id
        """)

        for ws in ws_rows:
            wid = ws["workspace_id"]
            affs = execute_query(f"SELECT DISTINCT affiliate FROM {fqn(gold, 'schema_inventory')} WHERE workspace_id = '{wid}' AND classification != 'SYSTEM'")
            progs = execute_query(f"SELECT DISTINCT program FROM {fqn(gold, 'schema_inventory')} WHERE workspace_id = '{wid}' AND classification != 'SYSTEM'")
            envs = execute_query(f"SELECT DISTINCT environment FROM {fqn(gold, 'schema_inventory')} WHERE workspace_id = '{wid}' AND classification != 'SYSTEM'")

            aff_str = ", ".join(sorted(r["affiliate"] for r in affs)).replace("'", "''")
            prog_str = ", ".join(sorted(r["program"] for r in progs)).replace("'", "''")
            env_str = ", ".join(sorted(r["environment"] for r in envs)).replace("'", "''")

            execute_query(f"""
                INSERT INTO {fqn(gold, 'workspace_summary')}
                (workspace_id, workspace_url, workspace_name, affiliates, programs,
                 environments, catalog_count, schema_count, table_count, updated_at)
                VALUES ('{wid}', '{ws['workspace_url'].replace("'","''")}',
                    '{ws['workspace_name'].replace("'","''")}',
                    '{aff_str}', '{prog_str}', '{env_str}',
                    {ws['catalog_count']}, {ws['schema_count']}, {ws['table_count']},
                    current_timestamp())
            """)
        ws_cnt = execute_query(f"SELECT count(*) as cnt FROM {fqn(gold, 'workspace_summary')}")
        _log(f"workspace_summary populated: {ws_cnt[0]['cnt'] if ws_cnt else 0} rows")

        _log("Step 4/4: Populating env_consistency...")
        execute_query(f"DELETE FROM {fqn(gold, 'env_consistency')} WHERE 1=1")
        execute_query(f"""
            INSERT INTO {fqn(gold, 'env_consistency')}
            (program, affiliate, schema_name, in_dev, in_qa, in_prod,
             dev_tables, qa_tables, prod_tables, issue_type, updated_at)
            WITH schema_envs AS (
                SELECT program, affiliate, schema_name,
                    MAX(CASE WHEN environment='dev' THEN true ELSE false END) AS in_dev,
                    MAX(CASE WHEN environment='qa' THEN true ELSE false END) AS in_qa,
                    MAX(CASE WHEN environment='prod' THEN true ELSE false END) AS in_prod,
                    SUM(CASE WHEN environment='dev' THEN table_count ELSE 0 END) AS dev_tables,
                    SUM(CASE WHEN environment='qa' THEN table_count ELSE 0 END) AS qa_tables,
                    SUM(CASE WHEN environment='prod' THEN table_count ELSE 0 END) AS prod_tables
                FROM {fqn(gold, 'schema_inventory')}
                WHERE classification = 'PRODUCTION'                GROUP BY program, affiliate, schema_name
            )
            SELECT program, affiliate, schema_name, in_dev, in_qa, in_prod,
                dev_tables, qa_tables, prod_tables,
                CASE
                    WHEN in_dev AND in_qa AND NOT in_prod THEN 'missing_in_prod'
                    WHEN in_dev AND NOT in_qa AND NOT in_prod THEN 'dev_only'
                    WHEN NOT in_dev AND in_qa AND NOT in_prod THEN 'qa_only'
                    WHEN NOT in_dev AND NOT in_qa AND in_prod THEN 'prod_only'
                    WHEN in_dev AND NOT in_qa AND in_prod THEN 'missing_in_qa'
                    WHEN NOT in_dev AND in_qa AND in_prod THEN 'missing_in_dev'
                    ELSE 'partial'
                END AS issue_type,
                current_timestamp()
            FROM schema_envs
            WHERE NOT (in_dev AND in_qa AND in_prod)
        """)
        ec_cnt = execute_query(f"SELECT count(*) as cnt FROM {fqn(gold, 'env_consistency')}")
        _log(f"env_consistency populated: {ec_cnt[0]['cnt'] if ec_cnt else 0} rows")

        # Artifact aggregation runs only if silver_artifacts exists and has rows.
        # Kept best-effort so a team that hasn't uploaded artifacts yet does not
        # block the rest of the gold pipeline.
        try:
            art_cnt = _populate_artifact_summary()
            _log(f"artifact_summary populated: {art_cnt} rows")
        except Exception as e:
            _log(f"artifact_summary skipped: {e}")

        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
        _log("Gold layer population complete!")

    except Exception as e:
        import traceback
        _log(f"Gold population FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


@router.post("/jobs/populate-gold", operation_id="triggerPopulateGold")
async def trigger_populate_gold() -> JobTriggerOut:
    """Populate gold layer tables from silver data using rule-based derivation."""
    run_id = str(uuid.uuid4())[:8]
    _active_runs[run_id] = {
        "status": "PENDING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
    }
    thread = threading.Thread(
        target=_run_populate_gold, args=(run_id,), daemon=True
    )
    thread.start()
    return JobTriggerOut(run_id=run_id, job_id="populate-gold", status="QUEUED")


# ---------------------------------------------------------------------------
# Analytics Endpoints (read from gold layer)
# ---------------------------------------------------------------------------


@router.get("/analytics/source-summary", operation_id="sourceSummary")
async def analytics_source_summary():
    """Return source/program summary with environment table counts."""
    gold = get_gold_schema()
    rows = execute_query(f"""
        SELECT program, affiliate,
            dev_schemas, qa_schemas, prod_schemas,
            dev_tables, qa_tables, prod_tables, total_tables,
            consistency_score, schemas_only_dev, schemas_only_qa, schemas_only_prod
        FROM {fqn(gold, 'source_summary')}
        ORDER BY total_tables DESC
    """)
    return {"sources": rows}


@router.get("/analytics/workspace-summary", operation_id="workspaceSummary")
async def analytics_workspace_summary():
    """Return workspace-level summary."""
    gold = get_gold_schema()
    rows = execute_query(f"""
        SELECT workspace_id, workspace_url, workspace_name,
            affiliates, programs, environments,
            catalog_count, schema_count, table_count
        FROM {fqn(gold, 'workspace_summary')}
        ORDER BY table_count DESC
    """)
    return {"workspaces": rows}


@router.get("/analytics/env-consistency", operation_id="envConsistency")
async def analytics_env_consistency(
    program: str = Query("", description="Filter by program"),
    issue_type: str = Query("", description="Filter by issue type"),
):
    """Return environment consistency report."""
    gold = get_gold_schema()
    where_clauses = ["1=1"]
    if program:
        where_clauses.append(f"program = '{program.replace(chr(39), chr(39)+chr(39))}'")
    if issue_type:
        where_clauses.append(f"issue_type = '{issue_type.replace(chr(39), chr(39)+chr(39))}'")

    rows = execute_query(f"""
        SELECT program, affiliate, schema_name, in_dev, in_qa, in_prod,
            dev_tables, qa_tables, prod_tables, issue_type
        FROM {fqn(gold, 'env_consistency')}
        WHERE {' AND '.join(where_clauses)}
        ORDER BY program, schema_name
        LIMIT 500
    """)
    programs = execute_query(f"SELECT DISTINCT program FROM {fqn(gold, 'env_consistency')} ORDER BY program")
    issue_types = execute_query(f"SELECT DISTINCT issue_type FROM {fqn(gold, 'env_consistency')} ORDER BY issue_type")
    return {
        "records": rows,
        "programs": [r["program"] for r in programs],
        "issue_types": [r["issue_type"] for r in issue_types],
    }


@router.get("/analytics/schema-inventory", operation_id="schemaInventory")
async def analytics_schema_inventory(
    program: str = Query("", description="Filter by program"),
    affiliate: str = Query("", description="Filter by affiliate"),
    environment: str = Query("", description="Filter by environment"),
    zone: str = Query("", description="Filter by zone"),
    search: str = Query("", description="Search schema/catalog names"),
    enriched_only: bool = Query(False, description="Only show AI-enriched"),
    limit: int = Query(100),
    offset: int = Query(0),
):
    """Return enriched schema inventory with filters."""
    gold = get_gold_schema()
    where_clauses = ["classification != 'SYSTEM'"]
    if program:
        where_clauses.append(f"program = '{program.replace(chr(39), chr(39)+chr(39))}'")
    if affiliate:
        where_clauses.append(f"affiliate = '{affiliate.replace(chr(39), chr(39)+chr(39))}'")
    if environment:
        where_clauses.append(f"environment = '{environment.replace(chr(39), chr(39)+chr(39))}'")
    if zone:
        where_clauses.append(f"zone = '{zone.replace(chr(39), chr(39)+chr(39))}'")
    if search:
        s = search.replace(chr(39), chr(39)+chr(39))
        where_clauses.append(f"(lower(schema_name) LIKE '%{s.lower()}%' OR lower(catalog_name) LIKE '%{s.lower()}%')")
    if enriched_only:
        where_clauses.append("definition != '' AND definition IS NOT NULL")

    where = " AND ".join(where_clauses)
    rows = execute_query(f"""
        SELECT schema_key, workspace_name, catalog_name, schema_name,
            program, affiliate, environment, zone, classification,
            table_count, view_count,
            definition, business_name, source_system, data_domain,
            department_owner, sensitivity, data_quality_tier, is_user_edited
        FROM {fqn(gold, 'schema_inventory')}
        WHERE {where}
        ORDER BY table_count DESC
        LIMIT {limit} OFFSET {offset}
    """)
    total = execute_query(f"SELECT count(*) as cnt FROM {fqn(gold, 'schema_inventory')} WHERE {where}")
    total_count = total[0]["cnt"] if total else 0

    filters = {}
    for col in ["program", "affiliate", "environment", "zone"]:
        vals = execute_query(f"""
            SELECT DISTINCT {col} FROM {fqn(gold, 'schema_inventory')}
            WHERE classification != 'SYSTEM' AND {col} != ''
            ORDER BY {col}
        """)
        filters[col + "s"] = [r[col] for r in vals]

    return {"schemas": rows, "total": total_count, "filters": filters}


@router.get("/analytics/schema-explorer", operation_id="schemaExplorer")
async def analytics_schema_explorer(
    program: str = Query("", description="Filter by program"),
    affiliate: str = Query("", description="Filter by affiliate"),
    zone: str = Query("", description="Filter by zone"),
    search: str = Query("", description="Search schema names or business names"),
    enriched_only: bool = Query(False, description="Only show AI-enriched"),
    limit: int = Query(100),
    offset: int = Query(0),
):
    """Return consolidated schema view: one row per logical schema with env flags."""
    gold = get_gold_schema()
    where_clauses = ["classification != 'SYSTEM'"]
    if program:
        where_clauses.append(f"program = '{program.replace(chr(39), chr(39)+chr(39))}'")
    if affiliate:
        where_clauses.append(f"affiliate = '{affiliate.replace(chr(39), chr(39)+chr(39))}'")
    if zone:
        where_clauses.append(f"zone = '{zone.replace(chr(39), chr(39)+chr(39))}'")
    if search:
        s = search.replace(chr(39), chr(39)+chr(39)).lower()
        where_clauses.append(
            f"(lower(schema_name) LIKE '%{s}%' OR lower(business_name) LIKE '%{s}%')"
        )
    if enriched_only:
        where_clauses.append("definition != '' AND definition IS NOT NULL")

    where = " AND ".join(where_clauses)
    inv = fqn(gold, "schema_inventory")

    rows = execute_query(f"""
        SELECT
            schema_name,
            MAX(program) AS program,
            MAX(affiliate) AS affiliate,
            MAX(zone) AS zone,
            MAX(CASE WHEN environment = 'dev' THEN true ELSE false END) AS in_dev,
            MAX(CASE WHEN environment = 'qa' THEN true ELSE false END) AS in_qa,
            MAX(CASE WHEN environment = 'prod' THEN true ELSE false END) AS in_prod,
            MAX(table_count) AS total_tables,
            MAX(CASE WHEN business_name IS NOT NULL AND business_name != '' THEN business_name ELSE NULL END) AS business_name,
            MAX(CASE WHEN definition IS NOT NULL AND definition != '' THEN definition ELSE NULL END) AS definition,
            MAX(data_domain) AS data_domain,
            MAX(department_owner) AS department_owner,
            MAX(source_system) AS source_system,
            MAX(sensitivity) AS sensitivity,
            MAX(is_user_edited) AS is_user_edited
        FROM {inv}
        WHERE {where}
        GROUP BY schema_name
        ORDER BY total_tables DESC
        LIMIT {limit} OFFSET {offset}
    """)

    total = execute_query(f"""
        SELECT count(*) AS cnt FROM (
            SELECT schema_name FROM {inv} WHERE {where} GROUP BY schema_name
        )
    """)
    total_count = total[0]["cnt"] if total else 0

    filters = {}
    for col in ["program", "affiliate", "zone"]:
        vals = execute_query(f"""
            SELECT DISTINCT {col} FROM {inv}
            WHERE classification != 'SYSTEM' AND {col} != ''
            ORDER BY {col}
        """)
        filters[col + "s"] = [r[col] for r in vals]

    return {"schemas": rows, "total": total_count, "filters": filters}


@router.get("/analytics/catalog-tree", operation_id="catalogTree")
async def analytics_catalog_tree(
    search: str = Query("", description="Search schema, catalog, or workspace"),
    enriched_only: bool = Query(False, description="Only show AI-enriched"),
):
    """Return rows for the workspace -> catalog -> schema tree view.

    One row per (workspace, catalog_name, schema_name). The `workspace` is the
    unique portion of the workspace URL (e.g. `adb-1234567890123456.7`),
    extracted from `workspace_url`. Each row carries the same enrichment
    fields as `/analytics/schema-explorer` so the detail panel renders without
    an additional fetch. The `in_dev/in_qa/in_prod` flags are aggregated
    across workspaces for the same schema_name (matching the program view),
    while `total_tables` is the per (workspace, catalog) table count.
    """
    gold = get_gold_schema()
    where_clauses = ["classification != 'SYSTEM'"]
    if search:
        s = search.replace(chr(39), chr(39) + chr(39)).lower()
        where_clauses.append(
            f"(lower(schema_name) LIKE '%{s}%' "
            f"OR lower(catalog_name) LIKE '%{s}%' "
            f"OR lower(workspace_url) LIKE '%{s}%' "
            f"OR lower(business_name) LIKE '%{s}%')"
        )
    if enriched_only:
        where_clauses.append("definition != '' AND definition IS NOT NULL")
    where = " AND ".join(where_clauses)
    inv = fqn(gold, "schema_inventory")

    # Extract the unique portion of the workspace URL: strip the
    # `https?://` prefix and the `.azuredatabricks.net` (or similar) suffix.
    # Falls back to the raw `workspace_url` (or "unknown") if the regex
    # doesn't match.
    workspace_expr = (
        "COALESCE(NULLIF("
        "regexp_extract(workspace_url, '^https?://([^/]+?)\\\\.(?:azure)?databricks(?:\\\\.net|\\\\.com)', 1)"
        ", ''), NULLIF(workspace_url, ''), 'unknown')"
    )

    rows = execute_query(f"""
        WITH base AS (
            SELECT * FROM {inv}
            WHERE {where}
        ),
        env_agg AS (
            SELECT
                schema_name,
                MAX(CASE WHEN environment = 'dev'  THEN true ELSE false END) AS in_dev,
                MAX(CASE WHEN environment = 'qa'   THEN true ELSE false END) AS in_qa,
                MAX(CASE WHEN environment = 'prod' THEN true ELSE false END) AS in_prod
            FROM {inv}
            WHERE classification != 'SYSTEM'
            GROUP BY schema_name
        )
        SELECT
            {workspace_expr} AS workspace,
            b.workspace_url,
            b.workspace_name,
            COALESCE(NULLIF(b.environment, ''), 'unknown') AS environment,
            b.catalog_name,
            b.schema_name,
            b.table_count AS total_tables,
            b.business_name,
            b.definition,
            b.program,
            b.affiliate,
            b.zone,
            b.data_domain,
            b.department_owner,
            b.source_system,
            b.sensitivity,
            b.is_user_edited,
            ea.in_dev,
            ea.in_qa,
            ea.in_prod
        FROM base b
        LEFT JOIN env_agg ea ON ea.schema_name = b.schema_name
        ORDER BY workspace, catalog_name, schema_name
    """)

    return {"rows": rows, "total": len(rows)}


@router.get("/analytics/schema-tables")
async def analytics_schema_tables(
    schema_name: str = Query(..., description="Schema name to fetch tables for"),
    search: str = Query("", description="Filter table names"),
    limit: int = Query(500),
    offset: int = Query(0),
):
    """Return tables for a schema grouped by table_name with env flags and metadata."""
    gold = get_gold_schema()
    silver = get_silver_schema()
    inv = fqn(gold, "schema_inventory")
    stables = fqn(silver, "silver_tables")

    # Defensive: source_system / source_system_canonical are ALTER-added by
    # normalize_source_systems job. Ensure they exist (NULL-valued is fine
    # for this read path) so we don't 500 on a fresh deploy.
    _ensure_silver_tables_columns()

    esc_schema = schema_name.replace("'", "''")
    search_clause = ""
    if search:
        esc_search = search.replace("'", "''").lower()
        search_clause = f"AND lower(st.table_name) LIKE '%{esc_search}%'"

    rows = execute_query(f"""
        SELECT
            st.table_name,
            MAX(st.table_type) AS table_type,
            MAX(st.data_source_format) AS data_source_format,
            MAX(st.comment) AS comment,
            MAX(st.business_friendly_name) AS business_name,
            MAX(st.ai_definition) AS definition,
            MAX(st.source_system) AS source_system,
            MAX(CASE WHEN si.environment = 'dev' THEN true ELSE false END) AS in_dev,
            MAX(CASE WHEN si.environment = 'qa' THEN true ELSE false END) AS in_qa,
            MAX(CASE WHEN si.environment = 'prod' THEN true ELSE false END) AS in_prod,
            MAX(CASE WHEN si.environment = 'unknown' THEN true ELSE false END) AS in_sbx,
            MAX(st.created) AS created,
            MAX(st.last_altered) AS last_altered
        FROM {stables} st
        JOIN {inv} si
            ON st.table_catalog = si.catalog_name
           AND st.table_schema = si.schema_name
        WHERE st.table_schema = '{esc_schema}'
          AND si.classification = 'PRODUCTION'
          {search_clause}
        GROUP BY st.table_name
        ORDER BY st.table_name
        LIMIT {limit} OFFSET {offset}
    """)

    total = execute_query(f"""
        SELECT COUNT(DISTINCT st.table_name) AS cnt
        FROM {stables} st
        JOIN {inv} si
            ON st.table_catalog = si.catalog_name
           AND st.table_schema = si.schema_name
        WHERE st.table_schema = '{esc_schema}'
          AND si.classification = 'PRODUCTION'
          {search_clause}
    """)

    return {"tables": rows, "total": total[0]["cnt"] if total else 0}


@router.get("/analytics/schema-taxonomy")
async def analytics_schema_taxonomy(
    schema_name: str = Query(..., description="Schema name to fetch taxonomy for"),
):
    """Return aggregated taxonomy dimensions for a schema (across all catalogs)."""
    gold = get_gold_schema()
    tax = fqn(gold, "schema_taxonomy")
    inv = fqn(gold, "schema_inventory")
    esc_schema = schema_name.replace("'", "''")

    rows = execute_query(f"""
        SELECT
            t.dimension,
            t.value,
            COUNT(*) as cnt
        FROM {tax} t
        JOIN {inv} si ON t.schema_key = si.schema_key
        WHERE si.schema_name = '{esc_schema}'
          AND si.classification = 'PRODUCTION'
          AND (t.effective_to IS NULL OR t.effective_to > current_timestamp())
        GROUP BY t.dimension, t.value
        ORDER BY t.dimension, cnt DESC
    """)

    taxonomy: dict[str, list[str]] = {}
    for r in rows:
        dim = r["dimension"]
        if dim not in taxonomy:
            taxonomy[dim] = []
        if r["value"] not in taxonomy[dim]:
            taxonomy[dim].append(r["value"])

    return {"taxonomy": taxonomy}


# ---------------------------------------------------------------------------
# Classification Rules CRUD
# ---------------------------------------------------------------------------

RULE_CATEGORIES = [
    "program", "zone", "environment", "ignore_catalog",
    "ignore_schema", "federated_source",
]


@router.get("/rules", operation_id="listRules")
async def list_rules(category: str = Query("", description="Filter by category")):
    """Return all classification rules, optionally filtered by category."""
    gold = get_gold_schema()
    where = f"WHERE category = '{category.replace(chr(39), chr(39)+chr(39))}'" if category else ""
    rows = execute_query(f"""
        SELECT rule_id, category, pattern, label, description, metadata,
            is_active, display_order
        FROM {fqn(gold, 'classification_rules')}
        {where}
        ORDER BY category, display_order, pattern
    """)
    for r in rows:
        try:
            r["metadata"] = json.loads(r["metadata"]) if r.get("metadata") else {}
        except Exception:
            r["metadata"] = {}
    return {"rules": rows, "categories": RULE_CATEGORIES}


@router.post("/rules", operation_id="createRule")
async def create_rule(rule: dict):
    """Create a new classification rule."""
    gold = get_gold_schema()
    rule_id = rule.get("rule_id", str(uuid.uuid4())[:8])
    category = rule.get("category", "")
    if category not in RULE_CATEGORIES:
        raise HTTPException(400, f"Invalid category. Must be one of: {RULE_CATEGORIES}")

    def esc(v):
        return str(v or "").replace("'", "''")

    metadata_str = json.dumps(rule.get("metadata", {}))
    execute_query(f"""
        INSERT INTO {fqn(gold, 'classification_rules')}
        (rule_id, category, pattern, label, description, metadata, is_active, display_order, created_at, updated_at)
        VALUES ('{esc(rule_id)}', '{esc(category)}', '{esc(rule.get("pattern",""))}',
            '{esc(rule.get("label",""))}', '{esc(rule.get("description",""))}',
            '{esc(metadata_str)}', {str(rule.get("is_active", True)).lower()},
            {rule.get("display_order", 99)}, current_timestamp(), current_timestamp())
    """)
    return {"status": "created", "rule_id": rule_id}


@router.put("/rules/{rule_id}", operation_id="updateRule")
async def update_rule(rule_id: str, rule: dict):
    """Update an existing classification rule."""
    gold = get_gold_schema()

    def esc(v):
        return str(v or "").replace("'", "''")

    sets = []
    for field in ["pattern", "label", "description", "category"]:
        if field in rule:
            sets.append(f"{field} = '{esc(rule[field])}'")
    if "metadata" in rule:
        sets.append(f"metadata = '{esc(json.dumps(rule['metadata']))}'")
    if "is_active" in rule:
        sets.append(f"is_active = {str(rule['is_active']).lower()}")
    if "display_order" in rule:
        sets.append(f"display_order = {int(rule['display_order'])}")

    if not sets:
        raise HTTPException(400, "No fields to update")

    sets.append("updated_at = current_timestamp()")
    execute_query(f"""
        UPDATE {fqn(gold, 'classification_rules')}
        SET {', '.join(sets)}
        WHERE rule_id = '{esc(rule_id)}'
    """)
    return {"status": "updated", "rule_id": rule_id}


@router.delete("/rules/{rule_id}", operation_id="deleteRule")
async def delete_rule(rule_id: str):
    """Delete a classification rule."""
    gold = get_gold_schema()
    execute_query(f"""
        DELETE FROM {fqn(gold, 'classification_rules')}
        WHERE rule_id = '{rule_id.replace(chr(39), chr(39)+chr(39))}'
    """)
    return {"status": "deleted", "rule_id": rule_id}


@router.post("/rules/test", operation_id="testRules")
async def test_rules(payload: dict):
    """Test rule parsing against a sample catalog name."""
    gold = get_gold_schema()
    rules = _load_rules(gold)
    catalog = payload.get("catalog_name", "")
    schema = payload.get("schema_name", "default")
    parsed = _parse_catalog(catalog, schema, rules)
    return {"catalog_name": catalog, "schema_name": schema, "parsed": parsed}


# ---------------------------------------------------------------------------
# Source Taxonomy Module
# ---------------------------------------------------------------------------

def _run_taxonomy_generation(run_id: str, batch_size: int = 2000):
    """Generate taxonomy using ai_query() as an inline SQL column expression.

    ai_query returns a JSON string per row.  We parse it with from_json(),
    then LATERAL VIEW explode to fan out 1 row into 8 taxonomy dimension rows.
    """

    def _log(msg: str):
        print(f"[tax-{run_id}] {msg}", flush=True)

    gold = get_gold_schema()
    inv = fqn(gold, "schema_inventory")
    tax = fqn(gold, "schema_taxonomy")

    prompt_col = (
        "CONCAT("
        "'Return ONLY a JSON object (no markdown) with keys: "
        "category, department, data_domain, integration_pattern, "
        "criticality, vendor_type, industry_vertical, use_case. "
        "Schema: catalog=', si.catalog_name, "
        "' schema=', si.schema_name, "
        "' program=', COALESCE(si.program,''), "
        "' affiliate=', COALESCE(si.affiliate,'')"
        ")"
    )

    json_schema = (
        "category STRING, department STRING, data_domain STRING, "
        "integration_pattern STRING, criticality STRING, vendor_type STRING, "
        "industry_vertical STRING, use_case STRING"
    )

    try:
        _active_runs[run_id]["status"] = "RUNNING"

        count_rows = execute_query(f"""
            SELECT COUNT(*) as cnt
            FROM {inv} si
            LEFT JOIN (SELECT DISTINCT schema_key FROM {tax} WHERE effective_to IS NULL) t
                ON si.schema_key = t.schema_key
            WHERE t.schema_key IS NULL
              AND si.classification = 'PRODUCTION'        """)
        to_classify = int(count_rows[0]["cnt"]) if count_rows else 0
        _log(f"Found {to_classify} unclassified schemas")

        if to_classify == 0:
            _active_runs[run_id]["status"] = "TERMINATED"
            _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
            _log("No schemas to classify.")
            return

        sql = f"""
            INSERT INTO {tax}
            (taxonomy_id, schema_key, dimension, value, source,
             confidence, ai_reasoning, effective_from, effective_to,
             created_by, created_at)
            WITH raw_ai AS (
                -- ai_query(..., failOnError => false) returns a STRUCT;
                -- pull .result so from_json gets the STRING it expects.
                SELECT
                    si.schema_key,
                    ai_query(
                        '{LLM_ENDPOINT}',
                        {prompt_col},
                        failOnError => false
                    ).result AS raw_json
                FROM {inv} si
                LEFT JOIN (SELECT DISTINCT schema_key FROM {tax} WHERE effective_to IS NULL) t
                    ON si.schema_key = t.schema_key
                WHERE t.schema_key IS NULL
                  AND si.classification = 'PRODUCTION'
                  AND si.schema_name NOT IN ('information_schema', 'default')
                LIMIT {batch_size}
            ),
            parsed AS (
                SELECT
                    schema_key,
                    from_json(raw_json, '{json_schema}') AS c
                FROM raw_ai
                WHERE raw_json IS NOT NULL
            )
            SELECT
                uuid() AS taxonomy_id,
                schema_key,
                dim.col.dimension AS dimension,
                dim.col.value AS value,
                'ai_generated' AS source,
                CAST(NULL AS FLOAT) AS confidence,
                '' AS ai_reasoning,
                current_timestamp() AS effective_from,
                CAST(NULL AS TIMESTAMP) AS effective_to,
                'system' AS created_by,
                current_timestamp() AS created_at
            FROM parsed
            LATERAL VIEW explode(array(
                named_struct('dimension', 'category',            'value', c.category),
                named_struct('dimension', 'department',          'value', c.department),
                named_struct('dimension', 'data_domain',         'value', c.data_domain),
                named_struct('dimension', 'integration_pattern', 'value', c.integration_pattern),
                named_struct('dimension', 'criticality',         'value', c.criticality),
                named_struct('dimension', 'vendor_type',         'value', c.vendor_type),
                named_struct('dimension', 'industry_vertical',   'value', c.industry_vertical),
                named_struct('dimension', 'use_case',            'value', c.use_case)
            )) dim AS col
            WHERE dim.col.value IS NOT NULL AND dim.col.value != ''
        """

        _log(f"Running ai_query batch inference on up to {batch_size} schemas...")
        execute_query(sql, poll_timeout=900)

        total = execute_query(
            f"SELECT COUNT(DISTINCT schema_key) as cnt FROM {tax} WHERE effective_to IS NULL"
        )
        _log(f"Done. Total schemas classified: {total[0]['cnt'] if total else 0}")

        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()

    except Exception as e:
        import traceback
        _log(f"Taxonomy generation FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


@router.post("/jobs/generate-taxonomy", operation_id="triggerTaxonomyGeneration")
async def trigger_taxonomy_generation() -> JobTriggerOut:
    """Generate AI taxonomy classifications for unclassified schemas."""
    run_id = str(uuid.uuid4())[:8]
    _active_runs[run_id] = {
        "status": "PENDING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
    }
    thread = threading.Thread(
        target=_run_taxonomy_generation, args=(run_id,), daemon=True
    )
    thread.start()
    return JobTriggerOut(run_id=run_id, job_id="generate-taxonomy", status="QUEUED")


# ---------------------------------------------------------------------------
# Taxonomy CRUD Endpoints
# ---------------------------------------------------------------------------


@router.get("/taxonomy", operation_id="listTaxonomy")
async def list_taxonomy(
    program: str = Query("", description="Filter by program"),
    affiliate: str = Query("", description="Filter by affiliate"),
    environment: str = Query("", description="Filter by environment"),
    search: str = Query("", description="Search schema/catalog names"),
    dim_filters: str = Query("", description="Dimension filters as dim:value pairs separated by |"),
    limit: int = Query(100),
    offset: int = Query(0),
):
    """Return current taxonomy for all schemas, pivoted so each schema is one row with all 8 dimensions."""
    gold = get_gold_schema()
    tax = fqn(gold, "schema_taxonomy")

    where_clauses = [
        "si.classification = 'PRODUCTION'",
    ]
    if program:
        where_clauses.append(f"si.program = '{program.replace(chr(39), chr(39)+chr(39))}'")
    if affiliate:
        where_clauses.append(f"si.affiliate = '{affiliate.replace(chr(39), chr(39)+chr(39))}'")
    if environment:
        where_clauses.append(f"si.environment = '{environment.replace(chr(39), chr(39)+chr(39))}'")
    if search:
        s = search.replace(chr(39), chr(39)+chr(39)).lower()
        where_clauses.append(f"(lower(si.schema_name) LIKE '%{s}%' OR lower(si.catalog_name) LIKE '%{s}%')")

    parsed_dims: list[tuple[str, str]] = []
    if dim_filters:
        for pair in dim_filters.split("|"):
            if ":" in pair:
                d, v = pair.split(":", 1)
                if d in TAXONOMY_DIMENSIONS:
                    parsed_dims.append((d, v))

    # Grain = program + zone + schema_name (logical schema identity).
    # Environment is a deployment variant, not part of identity.
    _grain = "CONCAT(si.program, '|', si.zone, '|', si.schema_name)"
    _grain2 = "CONCAT(si2.program, '|', si2.zone, '|', si2.schema_name)"

    dim_subquery = ""
    if parsed_dims:
        dim_joins = ""
        for idx, (dim, val) in enumerate(parsed_dims):
            alias = f"tf{idx}"
            esc_val = val.replace(chr(39), chr(39) + chr(39))
            dim_joins += (
                f" JOIN {tax} {alias} ON {alias}.schema_key = si2.schema_key"
                f" AND {alias}.dimension = '{dim}'"
                f" AND {alias}.value = '{esc_val}'"
                f" AND {alias}.effective_to IS NULL"
            )
        dim_subquery = (
            f" AND {_grain} IN ("
            f"SELECT DISTINCT {_grain2}"
            f" FROM {fqn(gold, 'schema_inventory')} si2{dim_joins}"
            f" WHERE si2.classification = 'PRODUCTION')"
        )

    where = " AND ".join(where_clauses)

    inv = fqn(gold, 'schema_inventory')
    dedup_cte = f"""WITH si AS (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY program, zone, schema_name ORDER BY table_count DESC
        ) AS _rn
        FROM {inv}
    )"""

    rows = execute_query(f"""
        {dedup_cte}
        SELECT si.schema_key, si.catalog_name, si.schema_name, si.business_name,
            si.program, si.affiliate, si.environment, si.zone,
            si.table_count, si.view_count
        FROM si
        WHERE {where} AND si._rn = 1{dim_subquery}
        ORDER BY si.table_count DESC
        LIMIT {limit} OFFSET {offset}
    """)
    total = execute_query(
        f"{dedup_cte} SELECT count(*) as cnt FROM si WHERE {where} AND si._rn = 1{dim_subquery}"
    )
    total_count = total[0]["cnt"] if total else 0

    if not rows:
        return {"schemas": [], "total": 0, "filters": {}, "dimensions": TAXONOMY_DIMENSIONS}

    # Taxonomy lookup: find taxonomy for ANY schema_key belonging to the same
    # logical schema (program+zone+schema_name), then map by that key
    grain_in = ", ".join(
        f"'{(r['program'] + '|' + r['zone'] + '|' + r['schema_name']).replace(chr(39), chr(39)+chr(39))}'"
        for r in rows
    )
    _grain_inv = "CONCAT(inv2.program, '|', inv2.zone, '|', inv2.schema_name)"

    tax_rows = execute_query(f"""
        SELECT inv2.program, inv2.zone, inv2.schema_name,
            t.dimension, t.value, t.source
        FROM {tax} t
        JOIN {inv} inv2 ON t.schema_key = inv2.schema_key
        WHERE {_grain_inv} IN ({grain_in})
          AND inv2.classification = 'PRODUCTION'
          AND t.effective_to IS NULL
    """)

    tax_map: dict = {}
    for t in tax_rows:
        key = (t["program"], t["zone"], t["schema_name"])
        tax_map.setdefault(key, {})
        if t["dimension"] not in tax_map[key]:
            tax_map[key][t["dimension"]] = {"value": t["value"], "source": t["source"]}

    result = []
    for r in rows:
        key = (r["program"], r["zone"], r["schema_name"])
        dims = tax_map.get(key, {})
        entry = {**r}
        for d in TAXONOMY_DIMENSIONS:
            info = dims.get(d, {})
            entry[d] = info.get("value", "")
            entry[f"{d}_source"] = info.get("source", "")
        result.append(entry)

    # Env flags + per-env table counts, aggregated at the logical schema grain
    env_counts = execute_query(f"""
        SELECT program, zone, schema_name,
            MAX(CASE WHEN environment='dev' THEN table_count ELSE 0 END) AS dev_tables,
            MAX(CASE WHEN environment='qa' THEN table_count ELSE 0 END) AS qa_tables,
            MAX(CASE WHEN environment='prod' THEN table_count ELSE 0 END) AS prod_tables,
            MAX(CASE WHEN environment='dev' THEN 1 ELSE 0 END) AS in_dev,
            MAX(CASE WHEN environment='qa' THEN 1 ELSE 0 END) AS in_qa,
            MAX(CASE WHEN environment='prod' THEN 1 ELSE 0 END) AS in_prod
        FROM {inv}
        WHERE CONCAT(program, '|', zone, '|', schema_name) IN ({grain_in})
          AND classification = 'PRODUCTION'
        GROUP BY program, zone, schema_name
    """)
    env_map = {(r["program"], r["zone"], r["schema_name"]): r for r in env_counts}
    for entry in result:
        ec = env_map.get((entry["program"], entry["zone"], entry["schema_name"]), {})
        entry["dev_tables"] = ec.get("dev_tables", 0)
        entry["qa_tables"] = ec.get("qa_tables", 0)
        entry["prod_tables"] = ec.get("prod_tables", 0)
        entry["in_dev"] = bool(int(ec.get("in_dev", 0)))
        entry["in_qa"] = bool(int(ec.get("in_qa", 0)))
        entry["in_prod"] = bool(int(ec.get("in_prod", 0)))

    filters = {}
    for col in ["program", "affiliate", "environment"]:
        vals = execute_query(f"""
            SELECT DISTINCT {col} FROM {inv}
            WHERE classification = 'PRODUCTION' AND {col} != '' ORDER BY {col}
        """)
        filters[col + "s"] = [r[col] for r in vals]

    return {"schemas": result, "total": total_count, "filters": filters, "dimensions": TAXONOMY_DIMENSIONS}


@router.get("/taxonomy/tables", operation_id="listTaxonomyTables")
async def list_taxonomy_tables(
    search: str = Query("", description="Search table names"),
    dim_filters: str = Query("", description="Dimension filters as dim:value pairs separated by |"),
    limit: int = Query(100),
    offset: int = Query(0),
):
    """Return individual table rows from schemas matching taxonomy dimension filters."""
    gold = get_gold_schema()
    silver = get_silver_schema()
    inv = fqn(gold, "schema_inventory")
    tax = fqn(gold, "schema_taxonomy")
    stables = fqn(silver, "silver_tables")

    parsed_dims: list[tuple[str, str]] = []
    if dim_filters:
        for pair in dim_filters.split("|"):
            if ":" in pair:
                d, v = pair.split(":", 1)
                if d in TAXONOMY_DIMENSIONS:
                    parsed_dims.append((d, v))

    # Find logical schemas (program+zone+schema_name) matching taxonomy filters
    dim_joins = ""
    for idx, (dim, val) in enumerate(parsed_dims):
        alias = f"tf{idx}"
        esc_val = val.replace(chr(39), chr(39) + chr(39))
        dim_joins += (
            f" JOIN {tax} {alias} ON {alias}.schema_key = si.schema_key"
            f" AND {alias}.dimension = '{dim}'"
            f" AND {alias}.value = '{esc_val}'"
            f" AND {alias}.effective_to IS NULL"
        )

    search_clause = ""
    if search:
        esc_search = search.replace(chr(39), chr(39) + chr(39)).lower()
        search_clause = f"AND lower(st.table_name) LIKE '%{esc_search}%'"

    # Grain: program + zone + schema_name + table_name (logical table identity)
    rows = execute_query(f"""
        SELECT
            st.table_name,
            st.table_schema AS schema_name,
            MAX(st.table_type) AS table_type,
            MAX(st.data_source_format) AS data_source_format,
            MAX(st.comment) AS comment,
            MAX(st.business_friendly_name) AS business_name,
            MAX(st.ai_definition) AS definition,
            si.program, si.zone,
            MAX(CASE WHEN si.environment = 'dev' THEN 1 ELSE 0 END) AS in_dev,
            MAX(CASE WHEN si.environment = 'qa' THEN 1 ELSE 0 END) AS in_qa,
            MAX(CASE WHEN si.environment = 'prod' THEN 1 ELSE 0 END) AS in_prod,
            MAX(CASE WHEN si.environment = 'unknown' THEN 1 ELSE 0 END) AS in_sbx
        FROM {stables} st
        JOIN {inv} si
            ON st.table_catalog = si.catalog_name
            AND st.table_schema = si.schema_name
        {dim_joins}
        WHERE si.classification = 'PRODUCTION'
          AND si.schema_name NOT IN ('information_schema', 'default')
          {search_clause}
        GROUP BY st.table_name, st.table_schema, si.program, si.zone
        ORDER BY st.table_name
        LIMIT {limit} OFFSET {offset}
    """)

    total = execute_query(f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT DISTINCT
                CONCAT(si.program, '|', si.zone, '|', st.table_schema, '|', st.table_name)
            FROM {stables} st
            JOIN {inv} si
                ON st.table_catalog = si.catalog_name
                AND st.table_schema = si.schema_name
            {dim_joins}
            WHERE si.classification = 'PRODUCTION'              {search_clause}
        )
    """)
    total_count = total[0]["cnt"] if total else 0

    return {"tables": rows, "total": total_count}


@router.get("/taxonomy/pivot", operation_id="taxonomyPivot")
async def taxonomy_pivot(
    rows_dim: str = Query("category", description="Dimension for rows"),
    cols_dim: str = Query("criticality", description="Dimension for columns"),
    metric: str = Query("systems", description="systems or tables"),
):
    """Return pivot table data for cross-tabulation of taxonomy dimensions."""
    gold = get_gold_schema()

    if rows_dim not in TAXONOMY_DIMENSIONS or cols_dim not in TAXONOMY_DIMENSIONS:
        raise HTTPException(400, f"Dimensions must be one of: {TAXONOMY_DIMENSIONS}")

    inv = fqn(gold, 'schema_inventory')
    tax = fqn(gold, 'schema_taxonomy')

    # Grain = program + zone + schema_name (logical schema identity)
    dedup_cte = f"""WITH si AS (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY program, zone, schema_name ORDER BY table_count DESC
        ) AS _rn
        FROM {inv}
        WHERE classification = 'PRODUCTION'
    )"""

    silver = get_silver_schema()
    stables = fqn(silver, "silver_tables")

    if metric == "systems":
        measure = "COUNT(DISTINCT CONCAT(si.program, '|', si.zone, '|', si.schema_name))"
        extra_join = ""
    else:
        measure = "COUNT(DISTINCT CONCAT(si.program, '|', si.zone, '|', si.schema_name, '|', st.table_name))"
        extra_join = f"JOIN {stables} st ON st.table_catalog = si.catalog_name AND st.table_schema = si.schema_name"

    pivot_rows = execute_query(f"""
        {dedup_cte}
        SELECT r.value AS row_val, c.value AS col_val, {measure} AS cnt
        FROM {tax} r
        JOIN {tax} c
            ON r.schema_key = c.schema_key AND c.dimension = '{cols_dim}' AND c.effective_to IS NULL
        JOIN si ON r.schema_key = si.schema_key
        {extra_join}
        WHERE r.dimension = '{rows_dim}' AND r.effective_to IS NULL
        GROUP BY r.value, c.value
        ORDER BY r.value, c.value
    """)

    row_labels = sorted(set(r["row_val"] for r in pivot_rows))
    col_labels = sorted(set(r["col_val"] for r in pivot_rows))

    lookup: dict = {}
    for r in pivot_rows:
        lookup[(r["row_val"], r["col_val"])] = int(r["cnt"])

    cells = []
    row_totals = []
    for rl in row_labels:
        row = [lookup.get((rl, cl), 0) for cl in col_labels]
        row_totals.append(sum(row))
        cells.append(row)

    col_totals = [sum(cells[i][j] for i in range(len(row_labels))) for j in range(len(col_labels))]

    return {
        "row_labels": row_labels,
        "col_labels": col_labels,
        "cells": cells,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "grand_total": sum(row_totals),
        "rows_dim": rows_dim,
        "cols_dim": cols_dim,
        "metric": metric,
    }


@router.put("/taxonomy/{schema_key}/{dimension}", operation_id="updateTaxonomy")
async def update_taxonomy(schema_key: str, dimension: str, body: TaxonomyUpdateIn):
    """Manual override: close current value and insert new one."""
    gold = get_gold_schema()

    if dimension not in TAXONOMY_DIMENSIONS:
        raise HTTPException(400, f"Invalid dimension. Must be one of: {TAXONOMY_DIMENSIONS}")

    def esc(v):
        return str(v or "").replace("'", "''")

    execute_query(f"""
        UPDATE {fqn(gold, 'schema_taxonomy')}
        SET effective_to = current_timestamp()
        WHERE schema_key = '{esc(schema_key)}'
          AND dimension = '{dimension}'
          AND effective_to IS NULL
    """)

    tid = str(uuid.uuid4())[:8]
    execute_query(f"""
        INSERT INTO {fqn(gold, 'schema_taxonomy')}
        (taxonomy_id, schema_key, dimension, value, source,
         confidence, ai_reasoning, effective_from, effective_to,
         created_by, created_at)
        VALUES ('{tid}', '{esc(schema_key)}', '{dimension}', '{esc(body.value)}',
            'manual', NULL, '', current_timestamp(), NULL,
            '{esc(body.created_by)}', current_timestamp())
    """)
    return {"status": "updated", "schema_key": schema_key, "dimension": dimension, "value": body.value}


@router.get("/taxonomy/{schema_key}/history", operation_id="taxonomyHistory")
async def taxonomy_history(schema_key: str):
    """Return full taxonomy history for a schema."""
    gold = get_gold_schema()

    def esc(v):
        return str(v or "").replace("'", "''")

    rows = execute_query(f"""
        SELECT taxonomy_id, dimension, value, source, confidence, ai_reasoning,
            effective_from, effective_to, created_by, created_at
        FROM {fqn(gold, 'schema_taxonomy')}
        WHERE schema_key = '{esc(schema_key)}'
        ORDER BY dimension, effective_from DESC
    """)
    return {"schema_key": schema_key, "history": rows}


# ---------------------------------------------------------------------------
# Taxonomy Inspection & Reprocessing
# ---------------------------------------------------------------------------


def _is_valid_title_case(value: str) -> bool:
    """Check if a value looks like proper Title Case (not snake_case or all-lower)."""
    if not value:
        return False
    if "_" in value and value == value.lower():
        return False
    if value == value.lower() and len(value) > 3:
        return False
    return True


@router.get("/taxonomy/allowed-values", operation_id="taxonomyAllowedValues")
async def taxonomy_allowed_values():
    """Return the canonical allowed values per dimension."""
    return TAXONOMY_ALLOWED_VALUES


@router.get("/taxonomy/inspect", operation_id="taxonomyInspect")
async def taxonomy_inspect():
    """Inspect taxonomy for quality issues: missing schemas, invalid values, bad casing."""
    gold = get_gold_schema()
    inv = fqn(gold, "schema_inventory")
    tax = fqn(gold, "schema_taxonomy")

    total_prod = execute_query(f"""
        SELECT COUNT(*) as cnt FROM {inv}
        WHERE classification = 'PRODUCTION'    """)
    total = int(total_prod[0]["cnt"]) if total_prod else 0

    missing_rows = execute_query(f"""
        SELECT si.schema_key, si.catalog_name, si.schema_name
        FROM {inv} si
        LEFT JOIN (SELECT DISTINCT schema_key FROM {tax} WHERE effective_to IS NULL) t
            ON si.schema_key = t.schema_key
        WHERE t.schema_key IS NULL
          AND si.classification = 'PRODUCTION'
          AND si.schema_name NOT IN ('information_schema', 'default')
    """)
    missing_count = len(missing_rows)

    current_values = execute_query(f"""
        SELECT schema_key, dimension, value, source
        FROM {tax}
        WHERE effective_to IS NULL
    """)

    issues: list[dict] = []
    valid_count = 0
    classified_schemas = set()

    for row in current_values:
        classified_schemas.add(row["schema_key"])
        dim = row["dimension"]
        val = row["value"]
        src = row["source"]

        if src == "manual":
            valid_count += 1
            continue

        allowed = TAXONOMY_ALLOWED_VALUES.get(dim)
        if allowed:
            if val not in allowed:
                issues.append({
                    "schema_key": row["schema_key"],
                    "dimension": dim,
                    "current_value": val,
                    "issue_type": "invalid_value",
                    "allowed_values": allowed,
                })
            else:
                valid_count += 1
        else:
            if not _is_valid_title_case(val):
                issues.append({
                    "schema_key": row["schema_key"],
                    "dimension": dim,
                    "current_value": val,
                    "issue_type": "inconsistent_case",
                })
            else:
                valid_count += 1

    invalid_by_dim: dict[str, int] = {}
    for iss in issues:
        invalid_by_dim[iss["dimension"]] = invalid_by_dim.get(iss["dimension"], 0) + 1

    return {
        "summary": {
            "total_schemas": total,
            "classified_schemas": len(classified_schemas),
            "missing_schemas": missing_count,
            "total_values": len(current_values),
            "valid_values": valid_count,
            "invalid_values": len(issues),
        },
        "invalid_by_dimension": invalid_by_dim,
        "issues": issues[:500],
        "issues_truncated": len(issues) > 500,
    }


def _build_strict_prompt_col() -> str:
    """Build CONCAT SQL expression for a strict taxonomy prompt with allowed values."""
    allowed_parts = []
    for dim, vals in TAXONOMY_ALLOWED_VALUES.items():
        vals_str = ", ".join(vals)
        allowed_parts.append(f"{dim}: MUST be one of [{vals_str}]")
    allowed_text = ". ".join(allowed_parts)

    return (
        "CONCAT("
        f"'Return ONLY a JSON object (no markdown, no explanation) with these 8 keys. "
        f"{allowed_text}. "
        "For category, department, data_domain, use_case: use Title Case (not snake_case). "
        "Schema: catalog=', si.catalog_name, "
        "' schema=', si.schema_name, "
        "' program=', COALESCE(si.program,''), "
        "' affiliate=', COALESCE(si.affiliate,'')"
        ")"
    )


def _run_taxonomy_reprocessing(run_id: str, batch_size: int = 2000):
    """Reprocess taxonomy: re-classify missing schemas and fix invalid values."""

    def _log(msg: str):
        print(f"[reprocess-{run_id}] {msg}", flush=True)

    gold = get_gold_schema()
    inv = fqn(gold, "schema_inventory")
    tax = fqn(gold, "schema_taxonomy")
    prompt_col = _build_strict_prompt_col()
    json_schema = (
        "category STRING, department STRING, data_domain STRING, "
        "integration_pattern STRING, criticality STRING, vendor_type STRING, "
        "industry_vertical STRING, use_case STRING"
    )

    try:
        _active_runs[run_id]["status"] = "RUNNING"

        # --- Phase 1: Missing schemas (no taxonomy at all) ---
        missing_count = execute_query(f"""
            SELECT COUNT(*) as cnt
            FROM {inv} si
            LEFT JOIN (SELECT DISTINCT schema_key FROM {tax} WHERE effective_to IS NULL) t
                ON si.schema_key = t.schema_key
            WHERE t.schema_key IS NULL
              AND si.classification = 'PRODUCTION'        """)
        n_missing = int(missing_count[0]["cnt"]) if missing_count else 0
        _log(f"Phase 1: {n_missing} missing schemas to classify")

        if n_missing > 0:
            sql_missing = f"""
                INSERT INTO {tax}
                (taxonomy_id, schema_key, dimension, value, source,
                 confidence, ai_reasoning, effective_from, effective_to,
                 created_by, created_at)
                WITH raw_ai AS (
                    -- ai_query(..., failOnError => false) returns a STRUCT;
                    -- pull .result so from_json gets the STRING it expects.
                    SELECT
                        si.schema_key,
                        ai_query('{LLM_ENDPOINT}', {prompt_col}, failOnError => false).result AS raw_json
                    FROM {inv} si
                    LEFT JOIN (SELECT DISTINCT schema_key FROM {tax} WHERE effective_to IS NULL) t
                        ON si.schema_key = t.schema_key
                    WHERE t.schema_key IS NULL
                      AND si.classification = 'PRODUCTION'                    LIMIT {batch_size}
                ),
                parsed AS (
                    SELECT schema_key, from_json(raw_json, '{json_schema}') AS c
                    FROM raw_ai
                    WHERE raw_json IS NOT NULL
                )
                SELECT
                    uuid(), schema_key, dim.col.dimension, dim.col.value,
                    'ai_generated', CAST(NULL AS FLOAT), '',
                    current_timestamp(), CAST(NULL AS TIMESTAMP),
                    'system', current_timestamp()
                FROM parsed
                LATERAL VIEW explode(array(
                    named_struct('dimension', 'category',            'value', c.category),
                    named_struct('dimension', 'department',          'value', c.department),
                    named_struct('dimension', 'data_domain',         'value', c.data_domain),
                    named_struct('dimension', 'integration_pattern', 'value', c.integration_pattern),
                    named_struct('dimension', 'criticality',         'value', c.criticality),
                    named_struct('dimension', 'vendor_type',         'value', c.vendor_type),
                    named_struct('dimension', 'industry_vertical',   'value', c.industry_vertical),
                    named_struct('dimension', 'use_case',            'value', c.use_case)
                )) dim AS col
                WHERE dim.col.value IS NOT NULL AND dim.col.value != ''
            """
            _log("Running ai_query for missing schemas...")
            execute_query(sql_missing, poll_timeout=900)
            _log("Phase 1 complete.")

        # --- Phase 2: Fix invalid values dimension-by-dimension ---
        _log("Phase 2: Finding invalid values...")

        current = execute_query(f"""
            SELECT t.taxonomy_id, t.schema_key, t.dimension, t.value, t.source
            FROM {tax} t
            WHERE t.effective_to IS NULL AND t.source != 'manual'
        """)

        invalid_rows: list[dict] = []
        for row in current:
            dim = row["dimension"]
            val = row["value"]
            allowed = TAXONOMY_ALLOWED_VALUES.get(dim)
            if allowed and val not in allowed:
                invalid_rows.append(row)
            elif not allowed and not _is_valid_title_case(val):
                invalid_rows.append(row)

        _log(f"Phase 2: {len(invalid_rows)} individual values to fix across {len(set(r['schema_key'] for r in invalid_rows))} schemas")

        if invalid_rows:
            def esc(v: str) -> str:
                return v.replace("'", "''")

            for dim_name in TAXONOMY_DIMENSIONS:
                dim_invalid = [r for r in invalid_rows if r["dimension"] == dim_name]
                if not dim_invalid:
                    continue

                allowed = TAXONOMY_ALLOWED_VALUES.get(dim_name)
                if allowed:
                    vals_str = ", ".join(allowed)
                    dim_prompt = (
                        "CONCAT("
                        f"'Pick exactly one value from this list for {dim_name}: [{vals_str}]. "
                        "Return ONLY the value, no JSON, no explanation. "
                        "Schema: catalog=', si.catalog_name, "
                        "' schema=', si.schema_name, "
                        "' program=', COALESCE(si.program,''), "
                        "' affiliate=', COALESCE(si.affiliate,'')"
                        ")"
                    )
                else:
                    dim_prompt = (
                        "CONCAT("
                        f"'Classify this schema for the {dim_name} dimension. "
                        "Return ONLY a short Title Case label (2-4 words, no snake_case). "
                        "Schema: catalog=', si.catalog_name, "
                        "' schema=', si.schema_name, "
                        "' program=', COALESCE(si.program,''), "
                        "' affiliate=', COALESCE(si.affiliate,'')"
                        ")"
                    )

                sk_list = list(set(r["schema_key"] for r in dim_invalid))[:batch_size]
                sk_in = ", ".join(f"'{esc(sk)}'" for sk in sk_list)
                tid_list = [r["taxonomy_id"] for r in dim_invalid if r["schema_key"] in sk_list]
                tid_in = ", ".join(f"'{esc(t)}'" for t in tid_list)

                execute_query(f"""
                    UPDATE {tax}
                    SET effective_to = current_timestamp()
                    WHERE taxonomy_id IN ({tid_in})
                      AND effective_to IS NULL
                """)

                execute_query(f"""
                    INSERT INTO {tax}
                    (taxonomy_id, schema_key, dimension, value, source,
                     confidence, ai_reasoning, effective_from, effective_to,
                     created_by, created_at)
                    SELECT
                        uuid(),
                        si.schema_key,
                        '{dim_name}',
                        -- ai_query(..., failOnError => false) returns STRUCT<result, errorMessage>.
                        -- Extract .result so TRIM gets a STRING; failed calls land as NULL.
                        TRIM(ai_query('{LLM_ENDPOINT}', {dim_prompt}, failOnError => false).result),
                        'ai_generated',
                        CAST(NULL AS FLOAT),
                        '',
                        current_timestamp(),
                        CAST(NULL AS TIMESTAMP),
                        'system',
                        current_timestamp()
                    FROM {inv} si
                    WHERE si.schema_key IN ({sk_in})
                """, poll_timeout=900)
                _log(f"  Fixed {dim_name}: {len(sk_list)} schemas")

            _log("Phase 2 complete.")

        total = execute_query(
            f"SELECT COUNT(DISTINCT schema_key) as cnt FROM {tax} WHERE effective_to IS NULL"
        )
        _log(f"Done. Total schemas classified: {total[0]['cnt'] if total else 0}")

        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()

    except Exception as e:
        import traceback
        _log(f"Reprocessing FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


@router.post("/jobs/reprocess-taxonomy", operation_id="triggerTaxonomyReprocessing")
async def trigger_taxonomy_reprocessing() -> JobTriggerOut:
    """Reprocess taxonomy: fill missing schemas and fix invalid values."""
    run_id = str(uuid.uuid4())[:8]
    _active_runs[run_id] = {
        "status": "PENDING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
    }
    thread = threading.Thread(
        target=_run_taxonomy_reprocessing, args=(run_id,), daemon=True
    )
    thread.start()
    return JobTriggerOut(run_id=run_id, job_id="reprocess-taxonomy", status="QUEUED")


# ---------------------------------------------------------------------------
# BI & AI Artifacts Catalog
#
# Artifacts = deployed BI reports, dashboards, Genie spaces, AI agents, ML
# endpoints, and notebooks the organization has built on top of its data.
# They live in silver (editable by analysts) and are optionally enriched by
# AI. Business users discover them via the /artifacts/* endpoints below.
# ---------------------------------------------------------------------------


ARTIFACT_COLUMNS: list[tuple[str, str]] = [
    # (column_name, sql_type)
    ("artifact_id", "STRING NOT NULL"),
    ("artifact_name", "STRING"),
    ("artifact_type", "STRING"),
    ("description", "STRING"),
    ("platform", "STRING"),
    ("business_owner", "STRING"),
    ("business_team", "STRING"),
    ("technical_owner", "STRING"),
    ("access_level", "STRING"),
    ("location_url", "STRING"),
    ("workspace_name", "STRING"),
    ("folder_path", "STRING"),
    ("topics", "STRING"),
    ("affiliate", "STRING"),
    ("data_domain", "STRING"),
    ("department", "STRING"),
    ("use_case_id", "STRING"),
    ("status", "STRING"),
    ("refresh_frequency", "STRING"),
    ("last_refreshed", "STRING"),
    ("created_date", "STRING"),
    ("last_modified", "STRING"),
    ("certified", "BOOLEAN"),
    ("source_schemas", "STRING"),
    ("source_tables", "STRING"),
    ("ai_summary", "STRING"),
    ("ai_suggested_tags", "STRING"),
    ("ai_data_quality_notes", "STRING"),
    ("is_user_edited", "BOOLEAN"),
    ("enriched_at", "STRING"),
    ("ingested_at", "STRING"),
    ("updated_at", "STRING"),
    ("ingested_by", "STRING"),
]


_ARTIFACT_SORTABLE_COLUMNS = {
    "artifact_name", "platform", "business_team", "business_owner",
    "artifact_type", "status", "last_modified", "last_refreshed",
    "created_date", "certified", "data_domain", "department",
}


def _ensure_artifacts_table() -> str:
    """Create silver_artifacts if it does not exist. Returns the fully-qualified name."""
    silver = get_silver_schema()
    table = fqn(silver, "silver_artifacts")
    cols_sql = ",\n  ".join(f"{name} {sqltype}" for name, sqltype in ARTIFACT_COLUMNS)
    execute_query(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
          {cols_sql}
        ) USING DELTA
        """
    )
    return table


def _artifact_id(name: str, platform: str, location: str) -> str:
    """Deterministic ID so re-uploads update in place instead of duplicating."""
    import hashlib
    key = f"{(name or '').strip().lower()}|{(platform or '').strip().lower()}|{(location or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in ("true", "t", "yes", "y", "1", "certified", "approved")


@router.get("/artifacts", operation_id="listArtifacts")
async def list_artifacts(
    search: Optional[str] = Query(None),
    platform: Optional[str] = Query(None),
    artifact_type: Optional[str] = Query(None, alias="type"),
    team: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    certified: Optional[bool] = Query(None),
    sort_by: str = Query("artifact_name"),
    sort_dir: str = Query("asc"),
    limit: int = Query(50, le=500),
    offset: int = Query(0),
) -> dict:
    """List artifacts with server-side filtering, sorting, and pagination."""
    table = _ensure_artifacts_table()

    conditions: list[str] = ["1 = 1"]
    if platform:
        conditions.append(f"platform = '{_sql_escape(platform)}'")
    if artifact_type:
        conditions.append(f"artifact_type = '{_sql_escape(artifact_type)}'")
    if team:
        conditions.append(f"business_team = '{_sql_escape(team)}'")
    if status:
        conditions.append(f"status = '{_sql_escape(status)}'")
    if domain:
        conditions.append(f"data_domain = '{_sql_escape(domain)}'")
    if department:
        conditions.append(f"department = '{_sql_escape(department)}'")
    if certified is not None:
        conditions.append(f"certified = {'true' if certified else 'false'}")
    if search:
        s = _sql_escape(search.lower())
        conditions.append(
            f"(LOWER(artifact_name) LIKE '%{s}%' "
            f"OR LOWER(description) LIKE '%{s}%' "
            f"OR LOWER(topics) LIKE '%{s}%' "
            f"OR LOWER(business_team) LIKE '%{s}%' "
            f"OR LOWER(business_owner) LIKE '%{s}%')"
        )
    where = " AND ".join(conditions)

    sort_col = sort_by if sort_by in _ARTIFACT_SORTABLE_COLUMNS else "artifact_name"
    direction = "DESC" if (sort_dir or "").lower() == "desc" else "ASC"

    try:
        total_rows = execute_query(f"SELECT COUNT(*) as cnt FROM {table} WHERE {where}")
        rows = execute_query(
            f"""
            SELECT * FROM {table}
            WHERE {where}
            ORDER BY {sort_col} {direction} NULLS LAST, artifact_name ASC
            LIMIT {limit} OFFSET {offset}
            """
        )
        return {
            "total": int(total_rows[0]["cnt"]) if total_rows else 0,
            "artifacts": rows,
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.warning(f"Artifacts list failed: {e}")
        return {"total": 0, "artifacts": [], "limit": limit, "offset": offset}


@router.get("/artifacts/filters", operation_id="artifactFilters", response_model=ArtifactFiltersOut)
async def artifact_filters() -> ArtifactFiltersOut:
    """Distinct values for the filter dropdowns on the Artifacts page."""
    table = _ensure_artifacts_table()

    def _distinct(col: str) -> list[str]:
        try:
            rows = execute_query(
                f"SELECT DISTINCT {col} as v FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} != '' ORDER BY v"
            )
            return [r["v"] for r in rows if r.get("v")]
        except Exception:
            return []

    return ArtifactFiltersOut(
        platforms=_distinct("platform"),
        types=_distinct("artifact_type") or list(ARTIFACT_TYPES),
        teams=_distinct("business_team"),
        statuses=_distinct("status") or list(ARTIFACT_STATUSES),
        domains=_distinct("data_domain"),
        departments=_distinct("department"),
        affiliates=_distinct("affiliate"),
    )


@router.get("/artifacts/stats", operation_id="artifactStats", response_model=ArtifactStatsOut)
async def artifact_stats() -> ArtifactStatsOut:
    """Aggregate counts for the artifacts catalog (used by the Dashboard)."""
    table = _ensure_artifacts_table()

    def _group(col: str, label: str = "value") -> list[dict]:
        try:
            rows = execute_query(
                f"SELECT COALESCE(NULLIF({col}, ''), 'Unspecified') as {label}, "
                f"COUNT(*) as count FROM {table} "
                f"GROUP BY COALESCE(NULLIF({col}, ''), 'Unspecified') "
                f"ORDER BY count DESC LIMIT 20"
            )
            return [{label: r[label], "count": int(r["count"])} for r in rows]
        except Exception:
            return []

    try:
        totals = execute_query(
            f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN certified = true THEN 1 ELSE 0 END) as certified_cnt,
                SUM(CASE WHEN last_refreshed IS NOT NULL AND last_refreshed != ''
                          AND try_cast(last_refreshed AS TIMESTAMP) < date_sub(current_date(), 30)
                    THEN 1 ELSE 0 END) as stale_cnt
            FROM {table}
            """
        )
        t = totals[0] if totals else {}
    except Exception:
        t = {}

    return ArtifactStatsOut(
        total=int(t.get("total") or 0),
        certified=int(t.get("certified_cnt") or 0),
        stale=int(t.get("stale_cnt") or 0),
        by_type=_group("artifact_type"),
        by_platform=_group("platform"),
        by_team=_group("business_team"),
        by_status=_group("status"),
        by_domain=_group("data_domain"),
    )


@router.get("/artifacts/vocabulary", operation_id="artifactVocabulary")
async def artifact_vocabulary() -> dict:
    """Controlled vocabularies for the Artifacts edit UI."""
    return {
        "types": list(ARTIFACT_TYPES),
        "statuses": list(ARTIFACT_STATUSES),
        "access_levels": list(ARTIFACT_ACCESS_LEVELS),
        "refresh_frequencies": list(ARTIFACT_REFRESH_FREQUENCIES),
    }


@router.get("/artifacts/{artifact_id}", operation_id="getArtifact")
async def get_artifact(artifact_id: str) -> dict:
    table = _ensure_artifacts_table()
    rows = execute_query(
        f"SELECT * FROM {table} WHERE artifact_id = '{_sql_escape(artifact_id)}' LIMIT 1"
    )
    if not rows:
        raise HTTPException(404, "Artifact not found")
    return rows[0]


@router.put("/artifacts/{artifact_id}", operation_id="updateArtifact")
async def update_artifact(artifact_id: str, body: ArtifactUpdateIn) -> dict:
    """Manual edit. Sets is_user_edited=true so AI enrichment will not overwrite."""
    table = _ensure_artifacts_table()

    updates: list[str] = []
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(400, "No fields to update")

    for col, val in data.items():
        if isinstance(val, bool):
            updates.append(f"{col} = {'true' if val else 'false'}")
        else:
            updates.append(f"{col} = '{_sql_escape(str(val))}'")

    updates.append("is_user_edited = true")
    updates.append(f"updated_at = '{datetime.utcnow().isoformat()}'")
    set_clause = ", ".join(updates)

    execute_query(
        f"UPDATE {table} SET {set_clause} WHERE artifact_id = '{_sql_escape(artifact_id)}'"
    )
    return {"status": "updated", "artifact_id": artifact_id}


@router.delete("/artifacts/{artifact_id}", operation_id="deleteArtifact")
async def delete_artifact(artifact_id: str) -> dict:
    table = _ensure_artifacts_table()
    execute_query(
        f"DELETE FROM {table} WHERE artifact_id = '{_sql_escape(artifact_id)}'"
    )
    return {"status": "deleted", "artifact_id": artifact_id}


# Maps each silver_artifacts column to the list of acceptable raw CSV column
# names (in priority order). The ingest endpoint introspects the CSV and only
# emits COALESCE() over columns that actually exist, so users can upload
# whatever subset of these headers they have today.
_ARTIFACT_COLUMN_ALIASES: dict[str, list[str]] = {
    "artifact_name":      ["artifact_name", "report_name", "name"],
    "platform":           ["platform"],
    "location_url":       ["location_url", "location", "url", "link"],
    "artifact_type":      ["artifact_type", "type"],
    "description":        ["description", "report_description"],
    "business_owner":     ["business_owner", "owner"],
    "business_team":      ["business_team", "team"],
    "technical_owner":    ["technical_owner", "sysadmin", "admin"],
    "access_level":       ["access_level"],
    "workspace_name":     ["workspace_name", "workspace"],
    "folder_path":        ["folder_path", "folder"],
    "topics":             ["topics", "tags", "list_of_topics"],
    "affiliate":          ["affiliate"],
    "data_domain":        ["data_domain", "domain"],
    "department":         ["department"],
    "use_case_id":        ["use_case_id"],
    "status":             ["status"],
    "refresh_frequency":  ["refresh_frequency"],
    "last_refreshed":     ["last_refreshed"],
    "created_date":       ["created_date", "created"],
    "last_modified":      ["last_modified", "modified"],
    "certified":          ["certified"],
    "source_schemas":     ["source_schemas"],
    "source_tables":      ["source_tables"],
}

# Columns that should default to a non-empty value when entirely missing.
_ARTIFACT_COLUMN_DEFAULTS: dict[str, str] = {
    "artifact_type": "BI Report",
    "status":        "Active",
}


def _read_csv_columns(vol_path: str) -> list[str]:
    """Return the CSV column names by issuing a LIMIT 0 read against read_files."""
    result = _execute_sql_api(
        f"SELECT * FROM read_files('{vol_path}', format => 'csv', header => true, "
        f"multiLine => true, escape => '\"') LIMIT 0"
    )
    cols = result.get("manifest", {}).get("schema", {}).get("columns", [])
    return [c["name"] for c in cols]


def _build_artifact_select(vol_path: str) -> str:
    """Build the SELECT that normalizes a CSV into the silver_artifacts shape.

    Only references columns that actually exist in the CSV, so missing
    headers don't break the load.
    """
    available = {c.lower() for c in _read_csv_columns(vol_path)}

    select_lines: list[str] = []
    for canonical, aliases in _ARTIFACT_COLUMN_ALIASES.items():
        present = [a for a in aliases if a.lower() in available]
        if canonical == "certified":
            if present:
                col = present[0]
                expr = (
                    f"CASE WHEN LOWER(TRIM(CAST({col} AS STRING))) "
                    f"IN ('true','t','yes','y','1','certified','approved') "
                    f"THEN true ELSE false END"
                )
            else:
                expr = "false"
            select_lines.append(f"  {expr} AS {canonical}")
            continue

        if not present:
            default = _ARTIFACT_COLUMN_DEFAULTS.get(canonical, "")
            select_lines.append(f"  '{default}' AS {canonical}")
            continue

        coalesce_args = [f"NULLIF(TRIM({c}), '')" for c in present]
        default = _ARTIFACT_COLUMN_DEFAULTS.get(canonical, "")
        coalesce_args.append(f"'{default}'")
        select_lines.append(
            f"  COALESCE({', '.join(coalesce_args)}) AS {canonical}"
        )

    return (
        "SELECT\n"
        + ",\n".join(select_lines)
        + f"\nFROM read_files('{vol_path}', format => 'csv', header => true, "
        f"multiLine => true, escape => '\"')"
    )


@router.post("/ingest/artifacts", operation_id="ingestArtifacts")
async def ingest_artifacts(
    filename: str = Query(..., description="CSV filename already uploaded to the raw Volume"),
    replace: bool = Query(False, description="If true, truncate the table before load"),
) -> dict:
    """
    Ingest a CSV of BI/AI artifacts from the upload Volume into silver_artifacts.

    Accepts flexible column names (report_name -> artifact_name, sysadmin ->
    technical_owner, location -> location_url, etc.) so the team can upload
    the same view they maintain today. The endpoint inspects the CSV header
    first and only references columns that actually exist, so partial CSVs
    are fine.

    Uses MERGE on a deterministic artifact_id so re-uploads UPDATE existing
    rows rather than duplicating them.
    """
    catalog = get_catalog()
    raw = get_raw_schema()
    silver = get_silver_schema()
    table = _ensure_artifacts_table()
    vol_path = f"/Volumes/{catalog}/{raw}/uploads/{filename}"
    now_iso = datetime.utcnow().isoformat()

    if replace:
        try:
            execute_query(f"DELETE FROM {table}")
        except Exception as e:
            raise HTTPException(500, f"Failed to truncate silver_artifacts: {e}")

    try:
        select_expr = _build_artifact_select(vol_path)
    except Exception as e:
        raise HTTPException(500, f"Could not read CSV header: {e}")

    # Databricks SQL does not expose Python hashing, so compute artifact_id in
    # SQL. sha1(lower(name) || '|' || lower(platform) || '|' || lower(url))
    # truncated to 16 chars matches _artifact_id() above.
    staged = f"""
        WITH raw AS ({select_expr}),
        keyed AS (
          SELECT
            substring(
              sha1(CONCAT(
                LOWER(artifact_name), '|',
                LOWER(platform), '|',
                LOWER(location_url)
              )),
              1, 16
            ) AS artifact_id,
            *
          FROM raw
          WHERE artifact_name != ''
        )
        SELECT * FROM keyed
    """

    merge_sql = f"""
        MERGE INTO {table} AS t
        USING ({staged}) AS s
        ON t.artifact_id = s.artifact_id
        WHEN MATCHED AND (t.is_user_edited IS NULL OR t.is_user_edited = false) THEN UPDATE SET
            t.artifact_name = s.artifact_name,
            t.artifact_type = s.artifact_type,
            t.description = s.description,
            t.platform = s.platform,
            t.business_owner = s.business_owner,
            t.business_team = s.business_team,
            t.technical_owner = s.technical_owner,
            t.access_level = s.access_level,
            t.location_url = s.location_url,
            t.workspace_name = s.workspace_name,
            t.folder_path = s.folder_path,
            t.topics = s.topics,
            t.affiliate = s.affiliate,
            t.data_domain = s.data_domain,
            t.department = s.department,
            t.use_case_id = s.use_case_id,
            t.status = s.status,
            t.refresh_frequency = s.refresh_frequency,
            t.last_refreshed = s.last_refreshed,
            t.created_date = s.created_date,
            t.last_modified = s.last_modified,
            t.certified = s.certified,
            t.source_schemas = s.source_schemas,
            t.source_tables = s.source_tables,
            t.updated_at = '{now_iso}'
        WHEN NOT MATCHED THEN INSERT (
            artifact_id, artifact_name, artifact_type, description, platform,
            business_owner, business_team, technical_owner, access_level,
            location_url, workspace_name, folder_path, topics, affiliate,
            data_domain, department, use_case_id, status, refresh_frequency,
            last_refreshed, created_date, last_modified, certified,
            source_schemas, source_tables,
            ai_summary, ai_suggested_tags, ai_data_quality_notes,
            is_user_edited, enriched_at, ingested_at, updated_at, ingested_by
        ) VALUES (
            s.artifact_id, s.artifact_name, s.artifact_type, s.description, s.platform,
            s.business_owner, s.business_team, s.technical_owner, s.access_level,
            s.location_url, s.workspace_name, s.folder_path, s.topics, s.affiliate,
            s.data_domain, s.department, s.use_case_id, s.status, s.refresh_frequency,
            s.last_refreshed, s.created_date, s.last_modified, s.certified,
            s.source_schemas, s.source_tables,
            '', '', '',
            false, '', '{now_iso}', '{now_iso}', 'csv-upload'
        )
    """

    try:
        execute_query(merge_sql, poll_timeout=300)
        count = execute_query(f"SELECT COUNT(*) as cnt FROM {table}")
        total = int(count[0]["cnt"]) if count else 0
        return {"status": "ingested", "table": "silver_artifacts", "rows": total, "filename": filename}
    except Exception as e:
        logger.exception("Artifact ingest failed")
        raise HTTPException(500, f"Artifact ingest failed: {e}")


def _run_artifact_enrichment(run_id: str, batch_size: int = 200):
    """
    Fill ai_summary and ai_suggested_tags for artifacts that have not been
    manually edited. Uses a single ai_query() over a staged batch -- same
    pattern as schema enrichment above.
    """

    def _log(msg: str):
        print(f"[artifact-enrich {run_id}] {msg}", flush=True)

    silver = get_silver_schema()
    table = fqn(silver, "silver_artifacts")

    company = _sql_escape(_get_company_name())

    prompt_col = (
        "CONCAT("
        f"'You are a data catalog expert for {company}. "
        "Given this BI/AI artifact metadata, return a JSON object with: "
        "ai_summary (2-3 sentence plain-English summary of what this artifact does and who should use it), "
        "ai_suggested_tags (3-6 comma-separated topic tags appropriate for search), "
        "ai_data_quality_notes (short note on data freshness or completeness concerns, or empty string if none). "
        "Artifact: name=', a.artifact_name, "
        "' type=', COALESCE(a.artifact_type, ''), "
        "' platform=', COALESCE(a.platform, ''), "
        "' team=', COALESCE(a.business_team, ''), "
        "' description=', COALESCE(LEFT(a.description, 300), ''), "
        "' topics=', COALESCE(a.topics, ''), "
        "' refresh=', COALESCE(a.refresh_frequency, ''), "
        "' last_refreshed=', COALESCE(a.last_refreshed, '')"
        ")"
    )

    json_schema = (
        "ai_summary STRING, ai_suggested_tags STRING, ai_data_quality_notes STRING"
    )

    try:
        _active_runs[run_id]["status"] = "RUNNING"
        _log("Starting AI enrichment of silver_artifacts...")

        count_rows = execute_query(
            f"""
            SELECT COUNT(*) as cnt FROM {table}
            WHERE (ai_summary IS NULL OR ai_summary = '')
              AND (is_user_edited IS NULL OR is_user_edited = false)
              AND artifact_name != ''
            """
        )
        to_enrich = int(count_rows[0]["cnt"]) if count_rows else 0
        _log(f"Found {to_enrich} un-enriched artifacts (processing up to {batch_size})")
        if to_enrich == 0:
            _active_runs[run_id]["status"] = "TERMINATED"
            _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()
            return

        sql = f"""
            MERGE INTO {table} AS target
            USING (
                WITH candidates AS (
                    SELECT artifact_id, artifact_name, artifact_type, platform,
                           business_team, description, topics, refresh_frequency,
                           last_refreshed
                    FROM {table}
                    WHERE (ai_summary IS NULL OR ai_summary = '')
                      AND (is_user_edited IS NULL OR is_user_edited = false)
                      AND artifact_name != ''
                    LIMIT {batch_size}
                ),
                raw_ai AS (
                    SELECT
                        a.artifact_id,
                        ai_query(
                            '{LLM_ENDPOINT}',
                            {prompt_col},
                            failOnError => false
                        ) AS ai_resp
                    FROM candidates a
                ),
                cleaned AS (
                    SELECT artifact_id,
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(ai_resp.result, '^```\\\\w*\\\\n?', ''),
                            '\\\\n?```$', ''
                        ) AS clean_json
                    FROM raw_ai WHERE ai_resp.result IS NOT NULL
                ),
                parsed AS (
                    SELECT artifact_id,
                           from_json(TRIM(clean_json), '{json_schema}') AS c,
                           ROW_NUMBER() OVER (PARTITION BY artifact_id ORDER BY artifact_id) AS rn
                    FROM cleaned
                )
                SELECT artifact_id, c FROM parsed
                WHERE rn = 1 AND c.ai_summary IS NOT NULL AND c.ai_summary != ''
            ) AS src
            ON target.artifact_id = src.artifact_id
            WHEN MATCHED THEN UPDATE SET
                target.ai_summary = src.c.ai_summary,
                target.ai_suggested_tags = src.c.ai_suggested_tags,
                target.ai_data_quality_notes = src.c.ai_data_quality_notes,
                target.enriched_at = CAST(current_timestamp() AS STRING)
        """

        execute_query(sql, poll_timeout=1800)

        done = execute_query(
            f"SELECT COUNT(*) as cnt FROM {table} WHERE ai_summary != '' AND ai_summary IS NOT NULL"
        )
        _log(f"Artifact enrichment complete. Total enriched: {done[0]['cnt'] if done else 0}")
        _active_runs[run_id]["status"] = "TERMINATED"
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()

    except Exception as e:
        import traceback
        _log(f"Artifact enrichment FAILED: {e}")
        traceback.print_exc()
        _active_runs[run_id]["status"] = "FAILED"
        _active_runs[run_id]["error"] = str(e)
        _active_runs[run_id]["end_time"] = datetime.utcnow().isoformat()


@router.post("/jobs/enrich-artifacts", operation_id="triggerArtifactEnrichment")
async def trigger_artifact_enrichment(batch_size: int = Query(500, le=2000)) -> JobTriggerOut:
    """Kick off AI enrichment of silver_artifacts (ai_summary, ai_suggested_tags)."""
    _ensure_artifacts_table()
    run_id = str(uuid.uuid4())[:8]
    _active_runs[run_id] = {
        "status": "PENDING",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": None,
        "error": None,
    }
    thread = threading.Thread(
        target=_run_artifact_enrichment, args=(run_id, batch_size), daemon=True
    )
    thread.start()
    return JobTriggerOut(run_id=run_id, job_id="enrich-artifacts", status="QUEUED")


def _populate_artifact_summary() -> int:
    """Build bhe_gold.artifact_summary from silver_artifacts. Returns row count."""
    silver = get_silver_schema()
    gold = get_gold_schema()
    src = _ensure_artifacts_table()
    target = fqn(gold, "artifact_summary")

    execute_query(
        f"""
        CREATE TABLE IF NOT EXISTS {target} (
          grouping_dimension STRING,
          grouping_value STRING,
          artifact_count INT,
          certified_count INT,
          stale_count INT,
          last_activity STRING
        ) USING DELTA
        """
    )
    execute_query(f"DELETE FROM {target}")

    for dim_col, dim_name in [
        ("platform", "platform"),
        ("artifact_type", "type"),
        ("business_team", "team"),
        ("data_domain", "domain"),
        ("status", "status"),
    ]:
        execute_query(
            f"""
            INSERT INTO {target}
            SELECT
              '{dim_name}' AS grouping_dimension,
              COALESCE(NULLIF({dim_col}, ''), 'Unspecified') AS grouping_value,
              COUNT(*) AS artifact_count,
              CAST(SUM(CASE WHEN certified = true THEN 1 ELSE 0 END) AS INT) AS certified_count,
              CAST(SUM(CASE
                WHEN last_refreshed IS NOT NULL AND last_refreshed != ''
                 AND try_cast(last_refreshed AS TIMESTAMP) < date_sub(current_date(), 30)
                THEN 1 ELSE 0 END) AS INT) AS stale_count,
              MAX(COALESCE(last_modified, last_refreshed, updated_at)) AS last_activity
            FROM {src}
            GROUP BY COALESCE(NULLIF({dim_col}, ''), 'Unspecified')
            """
        )

    count = execute_query(f"SELECT COUNT(*) as cnt FROM {target}")
    return int(count[0]["cnt"]) if count else 0


@router.post("/jobs/populate-artifact-summary", operation_id="populateArtifactSummary")
async def populate_artifact_summary() -> dict:
    """Rebuild the gold artifact_summary aggregation from silver_artifacts."""
    try:
        n = _populate_artifact_summary()
        return {"status": "ok", "rows": n, "table": "artifact_summary"}
    except Exception as e:
        logger.exception("populate_artifact_summary failed")
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Knowledge Articles
#
# Tree of folders + articles. Bodies live in UC Volume; this layer stores
# only metadata + cross-app references. node_id is a stable UUID — anything
# else in the catalog (table page, artifact page, use-case page) can later
# look up associated articles via /knowledge/links?target_type=...&target_key=.
# ---------------------------------------------------------------------------

_KNOWLEDGE_VOLUME_ROOT = "knowledge"
_KNOWLEDGE_TABLES_ENSURED = False
_KNOWLEDGE_NODE_DDL = """CREATE TABLE IF NOT EXISTS {fqn} (
    node_id STRING,
    parent_id STRING,
    node_type STRING,
    title STRING,
    summary STRING,
    content_format STRING,
    volume_path STRING,
    original_filename STRING,
    mime_type STRING,
    file_size_bytes BIGINT,
    tags STRING,
    sort_order INT,
    version INT,
    created_by STRING,
    updated_by STRING,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    is_deleted BOOLEAN
) USING DELTA"""

_KNOWLEDGE_LINK_DDL = """CREATE TABLE IF NOT EXISTS {fqn} (
    link_id STRING,
    node_id STRING,
    target_type STRING,
    target_key STRING,
    created_by STRING,
    created_at TIMESTAMP
) USING DELTA"""

# Allowed file extensions for binary uploads. Markdown can be created either
# inline (no upload) or via a .md upload.
_KNOWLEDGE_EXT_FORMAT = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".docx": "docx",
}
_KNOWLEDGE_EXT_MIME = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_KNOWLEDGE_MAX_BYTES = 25 * 1024 * 1024  # 25 MB per article


def _ensure_knowledge_tables() -> tuple[str, str]:
    """Create knowledge tables on first use. Returns (nodes_fqn, links_fqn)."""
    global _KNOWLEDGE_TABLES_ENSURED
    silver = get_silver_schema()
    nodes_fqn = fqn(silver, "knowledge_nodes")
    links_fqn = fqn(silver, "knowledge_links")
    if _KNOWLEDGE_TABLES_ENSURED:
        return nodes_fqn, links_fqn
    try:
        execute_query(_KNOWLEDGE_NODE_DDL.format(fqn=nodes_fqn))
        execute_query(_KNOWLEDGE_LINK_DDL.format(fqn=links_fqn))
        _KNOWLEDGE_TABLES_ENSURED = True
    except Exception as e:
        logger.warning(f"Could not ensure knowledge tables: {e}")
    return nodes_fqn, links_fqn


def _knowledge_volume_dir(node_id: str) -> str:
    """Per-node folder so we can later attach multiple files / versions."""
    return f"/Volumes/{get_catalog()}/{get_raw_schema()}/uploads/{_KNOWLEDGE_VOLUME_ROOT}/{node_id}"


def _knowledge_ext(filename: str) -> str:
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


def _knowledge_safe_filename(name: str) -> str:
    """Strip path components and unsafe chars for Volume storage."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:200] or "file"


def _knowledge_user() -> str:
    return os.environ.get("USER") or os.environ.get("DATABRICKS_USER") or "system"


def _knowledge_volume_put(target: str, content: bytes) -> None:
    from .db import _get_headers, _get_host
    import requests as req
    resp = req.put(
        f"{_get_host()}/api/2.0/fs/files{target}?overwrite=true",
        headers={**_get_headers(), "Content-Type": "application/octet-stream"},
        data=content,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(502, f"Volume upload failed: {resp.status_code} {resp.text[:200]}")


def _knowledge_volume_delete(path: str) -> None:
    from .db import _get_headers, _get_host
    import requests as req
    try:
        req.delete(f"{_get_host()}/api/2.0/fs/files{path}", headers=_get_headers())
    except Exception as e:
        logger.warning(f"Could not delete knowledge file {path}: {e}")


def _knowledge_volume_get(path: str) -> bytes:
    from .db import _get_headers, _get_host
    import requests as req
    resp = req.get(f"{_get_host()}/api/2.0/fs/files{path}", headers=_get_headers())
    if resp.status_code != 200:
        raise HTTPException(502, f"Failed to read {path}: {resp.status_code}")
    return resp.content


def _knowledge_row_to_node(r: dict) -> KnowledgeNodeOut:
    tags_raw = (r.get("tags") or "").strip()
    return KnowledgeNodeOut(
        node_id=r.get("node_id", "") or "",
        parent_id=r.get("parent_id") or "",
        node_type=r.get("node_type", "folder") or "folder",
        title=r.get("title", "") or "",
        summary=r.get("summary", "") or "",
        content_format=r.get("content_format", "") or "",
        volume_path=r.get("volume_path", "") or "",
        original_filename=r.get("original_filename", "") or "",
        mime_type=r.get("mime_type", "") or "",
        file_size_bytes=int(r.get("file_size_bytes") or 0),
        tags=[t.strip() for t in tags_raw.split(",") if t.strip()],
        sort_order=int(r.get("sort_order") or 0),
        version=int(r.get("version") or 1),
        created_by=r.get("created_by", "") or "",
        updated_by=r.get("updated_by", "") or "",
        created_at=str(r.get("created_at", "") or ""),
        updated_at=str(r.get("updated_at", "") or ""),
    )


def _knowledge_get_node(nodes_fqn: str, node_id: str) -> dict | None:
    rows = execute_query(
        f"SELECT * FROM {nodes_fqn} "
        f"WHERE node_id = '{_sql_escape(node_id)}' AND COALESCE(is_deleted, false) = false"
    )
    return rows[0] if rows else None


@router.get("/knowledge/tree", operation_id="getKnowledgeTree")
async def get_knowledge_tree() -> list[KnowledgeNodeOut]:
    """Return all (non-deleted) nodes as a flat list. Client builds the tree.

    Flat-list-and-build-on-client is intentional: the tree is small (10s–
    1000s of nodes) and a flat payload makes it trivial to filter/search
    without re-fetching.
    """
    nodes_fqn, _ = _ensure_knowledge_tables()
    try:
        rows = execute_query(
            f"SELECT * FROM {nodes_fqn} "
            f"WHERE COALESCE(is_deleted, false) = false "
            f"ORDER BY node_type DESC, COALESCE(sort_order, 0), LOWER(title)"
        )
    except Exception as e:
        logger.warning(f"get_knowledge_tree failed: {e}")
        return []
    return [_knowledge_row_to_node(r) for r in rows]


@router.get(
    "/knowledge/articles/{node_id}",
    response_model=KnowledgeArticleContentOut,
    operation_id="getKnowledgeArticle",
)
async def get_knowledge_article(node_id: str) -> KnowledgeArticleContentOut:
    nodes_fqn, _ = _ensure_knowledge_tables()
    row = _knowledge_get_node(nodes_fqn, node_id)
    if not row:
        raise HTTPException(404, "Article not found")
    if row.get("node_type") != "article":
        raise HTTPException(400, "Node is not an article")

    node = _knowledge_row_to_node(row)
    body_md = ""
    raw_url = f"/api/knowledge/articles/{node_id}/raw"
    if node.content_format == "markdown" and node.volume_path:
        try:
            body_md = _knowledge_volume_get(node.volume_path).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Could not read markdown body for {node_id}: {e}")
    return KnowledgeArticleContentOut(node=node, body_markdown=body_md, raw_url=raw_url)


@router.get("/knowledge/articles/{node_id}/raw", operation_id="getKnowledgeArticleRaw")
async def get_knowledge_article_raw(node_id: str):
    """Stream the raw file (PDF/DOCX/MD) from the Volume."""
    nodes_fqn, _ = _ensure_knowledge_tables()
    row = _knowledge_get_node(nodes_fqn, node_id)
    if not row or not row.get("volume_path"):
        raise HTTPException(404, "Article file not found")

    from .db import _get_headers, _get_host
    import requests as req
    resp = req.get(
        f"{_get_host()}/api/2.0/fs/files{row['volume_path']}",
        headers=_get_headers(),
        stream=True,
    )
    if resp.status_code != 200:
        raise HTTPException(502, f"Failed to read article file: {resp.status_code}")

    mime = row.get("mime_type") or "application/octet-stream"
    filename = row.get("original_filename") or "article"
    # Inline disposition lets browsers render PDFs in <iframe>; downloads still
    # work via the explicit Download button on the client.
    disposition = "inline" if mime in ("application/pdf", "text/markdown") else "attachment"

    def _iter():
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, max-age=30",
        },
    )


@router.post(
    "/knowledge/folders",
    response_model=KnowledgeNodeOut,
    operation_id="createKnowledgeFolder",
)
async def create_knowledge_folder(body: KnowledgeFolderCreateIn) -> KnowledgeNodeOut:
    nodes_fqn, _ = _ensure_knowledge_tables()
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "Title required")
    parent_id = (body.parent_id or "").strip()
    if parent_id:
        parent = _knowledge_get_node(nodes_fqn, parent_id)
        if not parent or parent.get("node_type") != "folder":
            raise HTTPException(400, "parent_id must reference an existing folder")

    node_id = str(uuid.uuid4())
    summary = (body.summary or "").strip()
    user = _knowledge_user()
    execute_query(
        f"INSERT INTO {nodes_fqn} ("
        f"node_id, parent_id, node_type, title, summary, content_format, "
        f"volume_path, original_filename, mime_type, file_size_bytes, tags, "
        f"sort_order, version, created_by, updated_by, created_at, updated_at, is_deleted"
        f") VALUES ("
        f"'{node_id}', '{_sql_escape(parent_id)}', 'folder', "
        f"'{_sql_escape(title)}', '{_sql_escape(summary)}', '', "
        f"'', '', '', 0, '', "
        f"0, 1, '{_sql_escape(user)}', '{_sql_escape(user)}', "
        f"current_timestamp(), current_timestamp(), false)"
    )
    row = _knowledge_get_node(nodes_fqn, node_id)
    return _knowledge_row_to_node(row or {"node_id": node_id, "title": title, "node_type": "folder"})


@router.post(
    "/knowledge/articles",
    response_model=KnowledgeNodeOut,
    operation_id="createKnowledgeArticle",
)
async def create_knowledge_article(body: KnowledgeArticleCreateIn) -> KnowledgeNodeOut:
    """Create a markdown article authored in-app. Content is written to the
    Volume so the storage model is uniform with uploaded files."""
    nodes_fqn, _ = _ensure_knowledge_tables()
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "Title required")
    parent_id = (body.parent_id or "").strip()
    if parent_id:
        parent = _knowledge_get_node(nodes_fqn, parent_id)
        if not parent or parent.get("node_type") != "folder":
            raise HTTPException(400, "parent_id must reference an existing folder")

    node_id = str(uuid.uuid4())
    content = (body.content_md or "").encode("utf-8")
    if len(content) > _KNOWLEDGE_MAX_BYTES:
        raise HTTPException(413, f"Article exceeds max size ({_KNOWLEDGE_MAX_BYTES} bytes)")

    filename = "article.md"
    target = f"{_knowledge_volume_dir(node_id)}/{filename}"
    _knowledge_volume_put(target, content)

    summary = (body.summary or "").strip()
    tags = (body.tags or "").strip()
    user = _knowledge_user()
    execute_query(
        f"INSERT INTO {nodes_fqn} ("
        f"node_id, parent_id, node_type, title, summary, content_format, "
        f"volume_path, original_filename, mime_type, file_size_bytes, tags, "
        f"sort_order, version, created_by, updated_by, created_at, updated_at, is_deleted"
        f") VALUES ("
        f"'{node_id}', '{_sql_escape(parent_id)}', 'article', "
        f"'{_sql_escape(title)}', '{_sql_escape(summary)}', 'markdown', "
        f"'{_sql_escape(target)}', '{filename}', 'text/markdown', {len(content)}, "
        f"'{_sql_escape(tags)}', "
        f"0, 1, '{_sql_escape(user)}', '{_sql_escape(user)}', "
        f"current_timestamp(), current_timestamp(), false)"
    )
    row = _knowledge_get_node(nodes_fqn, node_id)
    return _knowledge_row_to_node(row or {"node_id": node_id, "title": title, "node_type": "article"})


@router.post(
    "/knowledge/articles/upload",
    response_model=KnowledgeNodeOut,
    operation_id="uploadKnowledgeArticle",
)
async def upload_knowledge_article(
    file: UploadFile = File(...),
    title: str = Form(...),
    parent_id: str = Form(""),
    summary: str = Form(""),
    tags: str = Form(""),
) -> KnowledgeNodeOut:
    """Upload a binary article (PDF/DOCX/MD) to the Volume."""
    nodes_fqn, _ = _ensure_knowledge_tables()
    if not file.filename:
        raise HTTPException(400, "Filename required")
    ext = _knowledge_ext(file.filename)
    if ext not in _KNOWLEDGE_EXT_FORMAT:
        raise HTTPException(
            400, f"Unsupported file type: {ext or 'unknown'}. Allowed: {sorted(_KNOWLEDGE_EXT_FORMAT)}"
        )

    title = (title or "").strip() or file.filename
    parent_id = (parent_id or "").strip()
    if parent_id:
        parent = _knowledge_get_node(nodes_fqn, parent_id)
        if not parent or parent.get("node_type") != "folder":
            raise HTTPException(400, "parent_id must reference an existing folder")

    content = await file.read()
    if len(content) > _KNOWLEDGE_MAX_BYTES:
        raise HTTPException(413, f"File exceeds max size ({_KNOWLEDGE_MAX_BYTES} bytes)")

    node_id = str(uuid.uuid4())
    safe_name = _knowledge_safe_filename(file.filename)
    target = f"{_knowledge_volume_dir(node_id)}/{safe_name}"
    _knowledge_volume_put(target, content)

    fmt = _KNOWLEDGE_EXT_FORMAT[ext]
    mime = _KNOWLEDGE_EXT_MIME[ext]
    user = _knowledge_user()
    execute_query(
        f"INSERT INTO {nodes_fqn} ("
        f"node_id, parent_id, node_type, title, summary, content_format, "
        f"volume_path, original_filename, mime_type, file_size_bytes, tags, "
        f"sort_order, version, created_by, updated_by, created_at, updated_at, is_deleted"
        f") VALUES ("
        f"'{node_id}', '{_sql_escape(parent_id)}', 'article', "
        f"'{_sql_escape(title)}', '{_sql_escape(summary)}', '{fmt}', "
        f"'{_sql_escape(target)}', '{_sql_escape(safe_name)}', '{mime}', {len(content)}, "
        f"'{_sql_escape(tags)}', "
        f"0, 1, '{_sql_escape(user)}', '{_sql_escape(user)}', "
        f"current_timestamp(), current_timestamp(), false)"
    )
    row = _knowledge_get_node(nodes_fqn, node_id)
    return _knowledge_row_to_node(row or {"node_id": node_id, "title": title, "node_type": "article"})


@router.put(
    "/knowledge/nodes/{node_id}",
    response_model=KnowledgeNodeOut,
    operation_id="updateKnowledgeNode",
)
async def update_knowledge_node(node_id: str, body: KnowledgeNodeUpdateIn) -> KnowledgeNodeOut:
    """Rename, move, retag, or rewrite a node.

    For markdown articles, ``content_md`` rewrites the file in the Volume.
    For binary articles, content edits aren't supported via this endpoint —
    re-upload as a new article.
    """
    nodes_fqn, _ = _ensure_knowledge_tables()
    row = _knowledge_get_node(nodes_fqn, node_id)
    if not row:
        raise HTTPException(404, "Node not found")

    sets: list[str] = []

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "Title cannot be empty")
        sets.append(f"title = '{_sql_escape(title)}'")
    if body.summary is not None:
        sets.append(f"summary = '{_sql_escape(body.summary.strip())}'")
    if body.tags is not None:
        sets.append(f"tags = '{_sql_escape(body.tags.strip())}'")
    if body.sort_order is not None:
        sets.append(f"sort_order = {int(body.sort_order)}")
    if body.parent_id is not None:
        new_parent = body.parent_id.strip()
        if new_parent:
            parent = _knowledge_get_node(nodes_fqn, new_parent)
            if not parent or parent.get("node_type") != "folder":
                raise HTTPException(400, "parent_id must reference an existing folder")
            if new_parent == node_id:
                raise HTTPException(400, "Cannot move a node into itself")
        sets.append(f"parent_id = '{_sql_escape(new_parent)}'")

    if body.content_md is not None:
        if row.get("node_type") != "article" or row.get("content_format") != "markdown":
            raise HTTPException(400, "content_md only valid for markdown articles")
        content = body.content_md.encode("utf-8")
        if len(content) > _KNOWLEDGE_MAX_BYTES:
            raise HTTPException(413, f"Article exceeds max size ({_KNOWLEDGE_MAX_BYTES} bytes)")
        target = row.get("volume_path") or f"{_knowledge_volume_dir(node_id)}/article.md"
        _knowledge_volume_put(target, content)
        sets.append(f"volume_path = '{_sql_escape(target)}'")
        sets.append(f"file_size_bytes = {len(content)}")
        sets.append(f"version = COALESCE(version, 1) + 1")

    if not sets:
        raise HTTPException(400, "No fields to update")

    user = _knowledge_user()
    sets.append(f"updated_by = '{_sql_escape(user)}'")
    sets.append("updated_at = current_timestamp()")
    execute_query(
        f"UPDATE {nodes_fqn} SET {', '.join(sets)} "
        f"WHERE node_id = '{_sql_escape(node_id)}'"
    )
    fresh = _knowledge_get_node(nodes_fqn, node_id)
    return _knowledge_row_to_node(fresh or row)


@router.delete("/knowledge/nodes/{node_id}", operation_id="deleteKnowledgeNode")
async def delete_knowledge_node(node_id: str, hard: bool = Query(False)) -> dict:
    """Soft-delete a node (default) or hard-delete (also removes Volume files
    and all descendants). For folders, soft-delete propagates to descendants."""
    nodes_fqn, links_fqn = _ensure_knowledge_tables()
    row = _knowledge_get_node(nodes_fqn, node_id)
    if not row:
        raise HTTPException(404, "Node not found")

    # Collect this node + all descendants. Adjacency list traversal in app
    # code is fine for the expected scale; if articles ever grow into 10k+
    # we'd push this to a recursive CTE.
    all_rows = execute_query(
        f"SELECT node_id, parent_id, node_type, volume_path FROM {nodes_fqn} "
        f"WHERE COALESCE(is_deleted, false) = false"
    )
    children_by_parent: dict[str, list[dict]] = {}
    for r in all_rows:
        children_by_parent.setdefault(r.get("parent_id") or "", []).append(r)

    to_delete: list[dict] = []
    stack = [row]
    while stack:
        cur = stack.pop()
        to_delete.append(cur)
        stack.extend(children_by_parent.get(cur["node_id"], []))

    ids_quoted = ", ".join(f"'{_sql_escape(n['node_id'])}'" for n in to_delete)
    if hard:
        for n in to_delete:
            vp = n.get("volume_path")
            if vp:
                _knowledge_volume_delete(vp)
            # Also try to clean the per-node directory; ignore if missing.
            _knowledge_volume_delete(_knowledge_volume_dir(n["node_id"]))
        execute_query(f"DELETE FROM {nodes_fqn} WHERE node_id IN ({ids_quoted})")
        execute_query(f"DELETE FROM {links_fqn} WHERE node_id IN ({ids_quoted})")
    else:
        execute_query(
            f"UPDATE {nodes_fqn} SET is_deleted = true, "
            f"updated_at = current_timestamp() WHERE node_id IN ({ids_quoted})"
        )
    return {"status": "deleted", "count": len(to_delete), "hard": hard}


@router.get("/knowledge/search", operation_id="searchKnowledge")
async def search_knowledge(q: str = Query(...), limit: int = Query(50, le=200)) -> list[KnowledgeNodeOut]:
    """Title / summary / tag substring search.

    Body content search is intentionally out-of-scope for the MVP — full-text
    over Volume files wants either an external indexer or Vector Search,
    which is Phase-2.
    """
    nodes_fqn, _ = _ensure_knowledge_tables()
    needle = _sql_escape(q.strip().lower())
    if not needle:
        return []
    rows = execute_query(
        f"SELECT * FROM {nodes_fqn} "
        f"WHERE COALESCE(is_deleted, false) = false AND ("
        f"  LOWER(title) LIKE '%{needle}%' "
        f"  OR LOWER(summary) LIKE '%{needle}%' "
        f"  OR LOWER(tags) LIKE '%{needle}%'"
        f") "
        f"ORDER BY node_type DESC, LOWER(title) LIMIT {int(limit)}"
    )
    return [_knowledge_row_to_node(r) for r in rows]


@router.get("/knowledge/links", operation_id="listKnowledgeLinks")
async def list_knowledge_links(
    node_id: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    target_key: Optional[str] = Query(None),
) -> list[KnowledgeLinkOut]:
    """List links by node (articles attached from a node) or by target
    (articles attached to a given catalog entity)."""
    _, links_fqn = _ensure_knowledge_tables()
    where: list[str] = []
    if node_id:
        where.append(f"node_id = '{_sql_escape(node_id)}'")
    if target_type:
        if target_type not in KNOWLEDGE_LINK_TARGETS:
            raise HTTPException(400, f"Invalid target_type. Allowed: {KNOWLEDGE_LINK_TARGETS}")
        where.append(f"target_type = '{_sql_escape(target_type)}'")
    if target_key:
        where.append(f"target_key = '{_sql_escape(target_key)}'")
    sql = f"SELECT * FROM {links_fqn}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 500"
    try:
        rows = execute_query(sql)
    except Exception as e:
        logger.warning(f"list_knowledge_links failed: {e}")
        return []
    return [
        KnowledgeLinkOut(
            link_id=r.get("link_id", ""),
            node_id=r.get("node_id", ""),
            target_type=r.get("target_type", ""),
            target_key=r.get("target_key", ""),
            created_by=r.get("created_by", "") or "",
            created_at=str(r.get("created_at", "") or ""),
        )
        for r in rows
    ]


@router.post(
    "/knowledge/links",
    response_model=KnowledgeLinkOut,
    operation_id="createKnowledgeLink",
)
async def create_knowledge_link(body: KnowledgeLinkCreateIn) -> KnowledgeLinkOut:
    nodes_fqn, links_fqn = _ensure_knowledge_tables()
    if body.target_type not in KNOWLEDGE_LINK_TARGETS:
        raise HTTPException(400, f"Invalid target_type. Allowed: {KNOWLEDGE_LINK_TARGETS}")
    if not body.target_key.strip():
        raise HTTPException(400, "target_key required")
    if not _knowledge_get_node(nodes_fqn, body.node_id):
        raise HTTPException(404, "node_id not found")

    # Prevent duplicate (node_id, target_type, target_key) triples — they
    # represent the same association.
    existing = execute_query(
        f"SELECT link_id FROM {links_fqn} "
        f"WHERE node_id = '{_sql_escape(body.node_id)}' "
        f"  AND target_type = '{_sql_escape(body.target_type)}' "
        f"  AND target_key = '{_sql_escape(body.target_key)}'"
    )
    if existing:
        return KnowledgeLinkOut(
            link_id=existing[0].get("link_id", ""),
            node_id=body.node_id,
            target_type=body.target_type,
            target_key=body.target_key,
        )

    link_id = str(uuid.uuid4())
    user = _knowledge_user()
    execute_query(
        f"INSERT INTO {links_fqn} (link_id, node_id, target_type, target_key, created_by, created_at) "
        f"VALUES ('{link_id}', '{_sql_escape(body.node_id)}', "
        f"'{_sql_escape(body.target_type)}', '{_sql_escape(body.target_key)}', "
        f"'{_sql_escape(user)}', current_timestamp())"
    )
    return KnowledgeLinkOut(
        link_id=link_id,
        node_id=body.node_id,
        target_type=body.target_type,
        target_key=body.target_key,
        created_by=user,
    )


@router.delete("/knowledge/links/{link_id}", operation_id="deleteKnowledgeLink")
async def delete_knowledge_link(link_id: str) -> dict:
    _, links_fqn = _ensure_knowledge_tables()
    execute_query(f"DELETE FROM {links_fqn} WHERE link_id = '{_sql_escape(link_id)}'")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Use Case Proposal Generator
#
# One-click LLM-generated KB proposal article from a use case. Reuses the
# existing knowledge_nodes / knowledge_links infra so the result shows up in
# the Knowledge tree like any other article and can be opened from the use
# case detail drawer via target_type='use_case'.
#
# v2+ generations overwrite the same article (incrementing `version`) rather
# than littering the tree with duplicates.
# ---------------------------------------------------------------------------

_PROPOSAL_FOLDER_TITLE = "Use Case Proposals"


def _ensure_proposal_folder(nodes_fqn: str) -> str:
    """Find or create the auto-managed top-level folder for proposal articles.

    Returns the folder's node_id. Idempotent — safe to call on every request.
    """
    rows = execute_query(
        f"SELECT node_id FROM {nodes_fqn} "
        f"WHERE node_type = 'folder' "
        f"  AND title = '{_sql_escape(_PROPOSAL_FOLDER_TITLE)}' "
        f"  AND COALESCE(parent_id, '') = '' "
        f"  AND COALESCE(is_deleted, false) = false "
        f"LIMIT 1"
    )
    if rows:
        return rows[0]["node_id"]

    folder_id = str(uuid.uuid4())
    user = _knowledge_user()
    summary = "Auto-generated proposals for use cases. Created by the proposal generator."
    execute_query(
        f"INSERT INTO {nodes_fqn} ("
        f"node_id, parent_id, node_type, title, summary, content_format, "
        f"volume_path, original_filename, mime_type, file_size_bytes, tags, "
        f"sort_order, version, created_by, updated_by, created_at, updated_at, is_deleted"
        f") VALUES ("
        f"'{folder_id}', '', 'folder', "
        f"'{_sql_escape(_PROPOSAL_FOLDER_TITLE)}', '{_sql_escape(summary)}', '', "
        f"'', '', '', 0, 'auto-generated,proposal', "
        f"0, 1, '{_sql_escape(user)}', '{_sql_escape(user)}', "
        f"current_timestamp(), current_timestamp(), false)"
    )
    return folder_id


_PROPOSAL_SECTIONS: tuple[str, ...] = (
    "Executive Summary",
    "Business Case & Value",
    "Deliverables",
    "High-Level Design",
    "Assumptions & Dependencies",
    "Risks & Mitigations",
    "Timeline",
    "Open Items & Next Steps",
)

# Tokens budget for proposal generation. Sized for a full 8-section article
# with bullet-heavy content; well below Claude Sonnet/Opus output limits.
# Bigger than _ai_query's hardcoded 4000 (which truncates proposals mid-
# section) but capped so a runaway model can't blow up the article size.
_PROPOSAL_MAX_TOKENS = 8000


def _build_proposal_prompt(detail: dict, additional_context: str) -> str:
    """Compose the LLM prompt from the full use case detail payload.

    The prompt is split into three clearly fenced regions: instructions,
    input data (key:value lines, no markdown headings — so the model can't
    accidentally echo input structure into the output), and a final
    "begin output" marker. This dramatically reduces the duplicate-section
    failure mode we hit with header-based context blocks.
    """
    uc = detail.get("use_case") or {}
    readiness = detail.get("readiness") or {}
    present = detail.get("present_sources") or []
    missing = detail.get("missing_sources") or []
    unmapped = detail.get("unmapped_needs") or []
    affs = detail.get("applicable_affiliates") or []

    def _fmt_rows(label: str, rows: list[dict], keys: list[str]) -> str:
        if not rows:
            return f"{label}: (none)"
        lines = [f"{label}:"]
        for r in rows:
            parts = [f"{k}={r.get(k)!r}" for k in keys if r.get(k) is not None]
            lines.append("  - " + ", ".join(parts))
        return "\n".join(lines)

    data_reqs = uc.get("data_requirements")
    if isinstance(data_reqs, str):
        try:
            data_reqs = json.loads(data_reqs)
        except Exception:
            data_reqs = [data_reqs]
    data_reqs_lines = (
        "data_requirements:\n" + "\n".join(f"  - {x}" for x in data_reqs)
        if data_reqs else "data_requirements: (none)"
    )

    extra = (additional_context or "").strip()
    extra_line = (
        f"requester_additional_context: |\n  " + extra.replace("\n", "\n  ")
        if extra else "requester_additional_context: (none)"
    )

    section_list = "\n".join(
        f"  {i+1}. {name}" for i, name in enumerate(_PROPOSAL_SECTIONS)
    )

    return f"""You are a senior data & analytics solution architect at a utility holding
company. You write **KB proposal articles** that IT leadership and business
sponsors use as the working document for the next delivery phase.

=== OUTPUT FORMAT (STRICT) ===
- Output PURE Markdown only. No code fences. No preamble or closing notes.
- Start with exactly one H1: `# {uc.get('use_case_name') or 'Use Case'} — Proposal`
- Then emit EXACTLY these 8 sections, each as a single H2 (`## Name`),
  in this order, with these exact names:
{section_list}
- Each section appears EXACTLY ONCE. Do not split, repeat, or rename a
  section. Do not add additional H2 sections.
- Sub-headings within a section must use H3 (`###`) and MUST NOT reuse any
  of the H2 section names above.
- Body style: short paragraphs and bullet lists. No filler. Concrete over
  generic. Cite specific affiliates / source names from the input where
  relevant.

=== CONTENT GUIDANCE ===
- Executive Summary: 2-4 sentences for an exec audience. State the use
  case, the value, and the headline readiness gap.
- Business Case & Value: tie back to estimated_value_usd_per_year and the
  value_rationale. Do not invent numbers.
- Deliverables: list the concrete artifacts (datasets, dashboards, models,
  enablement, documentation) the next phase will produce.
- High-Level Design: data flow at the conceptual level. Reference present
  vs. missing canonical sources. Avoid tool/vendor names unless given.
- Assumptions & Dependencies: list assumptions and named dependencies
  (teams, source systems, prior work).
- Risks & Mitigations: explicitly call out the missing and unmapped data
  sources as data risks. Include delivery / adoption / data-quality risks.
- Timeline: propose a phased plan (Phase 0 / 1 / 2) with rough durations
  in weeks, calibrated to the readiness state. Phase 0 typically covers
  closing source gaps. Use a Markdown table or bullet list.
- Open Items & Next Steps: bulleted list of decisions needed and who owns
  the next move.
- If requester_additional_context is provided, treat it as authoritative
  constraints / priorities and integrate it across the relevant sections.

=== INPUT DATA (do not copy these field names into the output) ===
use_case_name: {uc.get('use_case_name') or '(unnamed)'}
use_case_id: {uc.get('id') or ''}
department: {uc.get('department') or '(unspecified)'}
category: {uc.get('category') or '(unspecified)'}
priority: {uc.get('priority') or '(unspecified)'}
delivery_status: {uc.get('status') or 'not_started'}
estimated_value_usd_per_year: {uc.get('estimated_value_usd') or 0}

description: |
  {(uc.get('description') or '(no description provided)').replace(chr(10), chr(10) + '  ')}

business_value: |
  {(uc.get('business_value') or '(none captured)').replace(chr(10), chr(10) + '  ')}

value_rationale: |
  {(uc.get('value_rationale') or '(none captured)').replace(chr(10), chr(10) + '  ')}

{data_reqs_lines}

readiness:
  total_required_sources: {readiness.get('total_required')}
  present_count: {readiness.get('present_count')}
  missing_count: {readiness.get('missing_count')}
  must_have_total: {readiness.get('must_total')}
  must_have_present: {readiness.get('must_present')}
  unmapped_needs: {readiness.get('unmapped_count')}
  readiness_pct_simple: {readiness.get('readiness_pct_simple')}
  readiness_pct_must: {readiness.get('readiness_pct_must')}

{_fmt_rows('present_sources', present, ['required_canonical', 'necessity', 'data_need_excerpt'])}

{_fmt_rows('missing_sources', missing, ['required_canonical', 'necessity', 'data_need_excerpt'])}

{_fmt_rows('unmapped_needs', unmapped, ['data_need_excerpt', 'necessity'])}

{_fmt_rows('applicable_affiliates', affs, ['affiliate_name', 'applicability', 'rationale'])}

{extra_line}

=== BEGIN OUTPUT ===
"""


def _strip_md_fences(text: str) -> str:
    """LLMs occasionally wrap output in ```markdown ... ``` despite the rule.
    Strip a single outer fence if present so the rendered article is clean.
    """
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _dedupe_h2_sections(md: str) -> str:
    """Belt-and-suspenders: if the model still emits two `## <Same Name>`
    blocks despite the prompt, keep only the first occurrence of each.

    Splits on H2 boundaries, preserves any preamble (H1 + intro), then
    rebuilds in original order without the duplicates. Case-insensitive
    comparison since models occasionally vary capitalization.
    """
    if not md:
        return md
    parts = re.split(r"(?m)^(##[^\n]*)$", md)
    if len(parts) < 3:
        return md
    out: list[str] = [parts[0]]  # everything before the first H2
    seen: set[str] = set()
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        key = re.sub(r"[^a-z0-9]+", "", heading.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(heading)
        out.append(body)
    return "".join(out).strip() + "\n"


def _call_proposal_llm(prompt: str) -> str:
    """Direct non-streaming call to the chat LLM serving endpoint.

    Bypasses `_ai_query` because that helper hardcodes `max_tokens=4000`,
    which truncates proposal articles mid-section. Uses the chat endpoint
    (Claude Opus 4.7 by default) which handles long structured generation
    better than the smaller Sonnet endpoint used for batch enrichment.
    """
    from .db import _get_headers, _get_host
    from .llm import CHAT_LLM_ENDPOINT
    import requests as _req

    url = f"{_get_host()}/serving-endpoints/{CHAT_LLM_ENDPOINT}/invocations"
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": _PROPOSAL_MAX_TOKENS,
        "stream": False,
    }
    try:
        resp = _req.post(url, json=body, headers=_get_headers(), timeout=180)
    except _req.RequestException as e:
        raise RuntimeError(f"LLM transport error: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LLM endpoint {resp.status_code}: {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"LLM non-JSON response: {resp.text[:200]}") from e
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        # Some endpoints return content as a list of {type, text} parts;
        # concatenate the text parts.
        content = "".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        )
    return content or ""


@router.post(
    "/knowledge/use-cases/{use_case_id}/generate-proposal",
    response_model=KnowledgeNodeOut,
    operation_id="generateUseCaseProposal",
)
async def generate_use_case_proposal(
    use_case_id: str, body: ProposalGenerateIn
) -> KnowledgeNodeOut:
    """Generate (or regenerate) a KB proposal article for a use case.

    First call creates a markdown article in the auto-managed
    "Use Case Proposals" folder and links it via knowledge_links
    (target_type=use_case, target_key=<id>). Subsequent calls with
    ``regenerate=True`` overwrite the same article and bump its version.
    Without ``regenerate``, returns 409 if a proposal already exists so the
    UI can show a "View Proposal" button instead of silently duplicating.
    """
    detail = await value_use_case_detail(use_case_id, affiliate=None)
    if not detail or detail.get("error") == "not_found":
        raise HTTPException(404, f"Use case {use_case_id!r} not found")
    if detail.get("error"):
        raise HTTPException(500, f"Could not load use case detail: {detail['error']}")

    uc = detail.get("use_case") or {}
    uc_name = (uc.get("use_case_name") or "Untitled use case").strip()
    article_title = f"{uc_name} - Proposal"

    nodes_fqn, links_fqn = _ensure_knowledge_tables()
    folder_id = _ensure_proposal_folder(nodes_fqn)

    existing_link = execute_query(
        f"SELECT link_id, node_id FROM {links_fqn} "
        f"WHERE target_type = 'use_case' "
        f"  AND target_key = '{_sql_escape(use_case_id)}' "
        f"LIMIT 1"
    )
    existing_node_id: str | None = None
    if existing_link:
        candidate = existing_link[0].get("node_id")
        if candidate and _knowledge_get_node(nodes_fqn, candidate):
            existing_node_id = candidate

    if existing_node_id and not body.regenerate:
        raise HTTPException(
            409,
            {
                "message": "Proposal already exists for this use case. "
                           "Pass regenerate=true to create v2.",
                "node_id": existing_node_id,
            },
        )

    prompt = _build_proposal_prompt(detail, body.additional_context)
    try:
        raw_md = _call_proposal_llm(prompt)
    except Exception as e:
        logger.exception("generate_use_case_proposal: LLM call failed")
        raise HTTPException(502, f"LLM generation failed: {e}")

    body_md = _dedupe_h2_sections(_strip_md_fences(raw_md))
    if not body_md:
        raise HTTPException(502, "LLM returned empty content")

    content = body_md.encode("utf-8")
    if len(content) > _KNOWLEDGE_MAX_BYTES:
        raise HTTPException(413, f"Generated article exceeds max size ({_KNOWLEDGE_MAX_BYTES} bytes)")

    user = _knowledge_user()
    summary = (uc.get("description") or "").strip()
    if len(summary) > 500:
        summary = summary[:497] + "..."
    tags = "auto-generated,proposal,use-case"

    if existing_node_id:
        # Overwrite content + bump version. Keep the same node_id so all
        # existing links / bookmarks continue to resolve.
        existing_row = _knowledge_get_node(nodes_fqn, existing_node_id) or {}
        target = existing_row.get("volume_path") or f"{_knowledge_volume_dir(existing_node_id)}/article.md"
        _knowledge_volume_put(target, content)
        execute_query(
            f"UPDATE {nodes_fqn} SET "
            f"  title = '{_sql_escape(article_title)}', "
            f"  summary = '{_sql_escape(summary)}', "
            f"  volume_path = '{_sql_escape(target)}', "
            f"  file_size_bytes = {len(content)}, "
            f"  tags = '{_sql_escape(tags)}', "
            f"  version = COALESCE(version, 1) + 1, "
            f"  updated_by = '{_sql_escape(user)}', "
            f"  updated_at = current_timestamp() "
            f"WHERE node_id = '{_sql_escape(existing_node_id)}'"
        )
        fresh = _knowledge_get_node(nodes_fqn, existing_node_id) or {}
        return _knowledge_row_to_node(fresh)

    # First-time generation: create node + volume file + link.
    node_id = str(uuid.uuid4())
    target = f"{_knowledge_volume_dir(node_id)}/article.md"
    _knowledge_volume_put(target, content)
    execute_query(
        f"INSERT INTO {nodes_fqn} ("
        f"node_id, parent_id, node_type, title, summary, content_format, "
        f"volume_path, original_filename, mime_type, file_size_bytes, tags, "
        f"sort_order, version, created_by, updated_by, created_at, updated_at, is_deleted"
        f") VALUES ("
        f"'{node_id}', '{_sql_escape(folder_id)}', 'article', "
        f"'{_sql_escape(article_title)}', '{_sql_escape(summary)}', 'markdown', "
        f"'{_sql_escape(target)}', 'article.md', 'text/markdown', {len(content)}, "
        f"'{_sql_escape(tags)}', "
        f"0, 1, '{_sql_escape(user)}', '{_sql_escape(user)}', "
        f"current_timestamp(), current_timestamp(), false)"
    )

    link_id = str(uuid.uuid4())
    execute_query(
        f"INSERT INTO {links_fqn} (link_id, node_id, target_type, target_key, created_by, created_at) "
        f"VALUES ('{link_id}', '{node_id}', 'use_case', "
        f"'{_sql_escape(use_case_id)}', '{_sql_escape(user)}', current_timestamp())"
    )

    fresh = _knowledge_get_node(nodes_fqn, node_id) or {}
    return _knowledge_row_to_node(
        fresh or {"node_id": node_id, "title": article_title, "node_type": "article"}
    )
