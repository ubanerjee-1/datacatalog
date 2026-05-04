"""Create gold layer tables for BHE Data Catalog analytics.

Run once to bootstrap the gold schema and tables.
Uses the SQL Statement Execution API directly.
"""

import os
import sys
import time
import json
import requests

HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "your-warehouse-id")
CATALOG = os.environ.get("BHE_CATALOG", "your_catalog")
GOLD = os.environ.get("BHE_GOLD_SCHEMA", "bhe_gold")
SILVER = os.environ.get("BHE_SILVER_SCHEMA", "bhe_silver")


def _query_tags(label: str) -> list[dict]:
    """Build SEA-shaped query_tags. label becomes the submodule (sanitized)."""
    submodule = label.replace(",", "_").replace(":", "_").replace("-", "_") \
                     .replace("/", "_").replace("=", "_").replace(".", "_") or "create_gold_tables"
    return [
        {"key": "app", "value": "bhe_catalog"},
        {"key": "module", "value": "bootstrap"},
        {"key": "submodule", "value": submodule[:128]},
    ]


def exec_sql(stmt, label=""):
    if label:
        print(f"  [{label}] ...", end=" ", flush=True)
    r = requests.post(
        f"{HOST}/api/2.0/sql/statements/",
        json={"warehouse_id": WAREHOUSE_ID, "statement": stmt,
              "wait_timeout": "50s", "catalog": CATALOG,
              "query_tags": _query_tags(label or "unlabeled")},
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    result = r.json()
    while result.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        time.sleep(1)
        sid = result["statement_id"]
        poll = requests.get(f"{HOST}/api/2.0/sql/statements/{sid}",
                            headers={"Authorization": f"Bearer {TOKEN}"})
        poll.raise_for_status()
        result = poll.json()

    state = result.get("status", {}).get("state", "UNKNOWN")
    if state == "FAILED":
        err = result.get("status", {}).get("error", {}).get("message", "")
        print(f"FAILED: {err}")
        return None
    if label:
        print(f"OK ({state})")
    return result


def main():
    print(f"=== Creating Gold Layer Tables in {CATALOG}.{GOLD} ===\n")

    exec_sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOLD}", "create schema")

    # 1. Schema inventory - the main enriched table at SCHEMA grain
    exec_sql(f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{GOLD}.schema_inventory (
            schema_key          STRING      COMMENT 'Unique key: workspace_id|catalog|schema',
            workspace_id        STRING      COMMENT 'Databricks workspace numeric ID',
            workspace_url       STRING      COMMENT 'Full workspace URL',
            workspace_name      STRING      COMMENT 'Derived friendly workspace name',
            catalog_name        STRING      COMMENT 'Unity Catalog name',
            schema_name         STRING      COMMENT 'Schema name within catalog',
            schema_owner        STRING      COMMENT 'Schema owner from metadata',

            -- Derived attributes (rule-based)
            program             STRING      COMMENT 'Program code: apm, fdm, nvedl, pacxedam, etc.',
            affiliate           STRING      COMMENT 'BHE affiliate: PacifiCorp, Nevada Energy, GT&S, etc.',
            environment         STRING      COMMENT 'Derived environment: dev, qa, prod',
            zone                STRING      COMMENT 'Data zone: raw, standardized, published, etc.',
            classification      STRING      COMMENT 'PRODUCTION, DEVELOPMENT, SYSTEM, ORACLE_FEDERATED',

            -- Counts
            table_count         INT         COMMENT 'Number of tables in this schema',
            view_count          INT         COMMENT 'Number of views in this schema',

            -- AI-enriched attributes
            definition          STRING      COMMENT 'AI-generated business description of the schema',
            business_name       STRING      COMMENT 'AI-generated human-friendly name',
            source_system       STRING      COMMENT 'AI-identified external source system (SAP, Oracle, PI, etc.)',
            data_domain         STRING      COMMENT 'AI-classified data domain (Energy Trading, Grid Ops, etc.)',
            department_owner    STRING      COMMENT 'AI-suggested owning department',
            sensitivity         STRING      COMMENT 'Data sensitivity: Public, Internal, Confidential, Restricted',
            data_quality_tier   STRING      COMMENT 'Inferred quality tier: Raw, Cleansed, Curated, Certified',

            -- Timestamps
            created             STRING      COMMENT 'Schema creation timestamp',
            last_altered        STRING      COMMENT 'Last alteration timestamp',
            enriched_at         TIMESTAMP   COMMENT 'When AI enrichment was last run',
            is_user_edited      BOOLEAN     COMMENT 'Whether a user has manually edited this record',

            -- Metadata
            comment             STRING      COMMENT 'Original schema comment from UC metadata'
        )
        USING DELTA
        COMMENT 'Gold layer: enriched schema inventory at schema grain'
    """, "schema_inventory")

    # 2. Source summary - aggregated view of programs/sources
    exec_sql(f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{GOLD}.source_summary (
            program             STRING      COMMENT 'Program code',
            affiliate           STRING      COMMENT 'BHE affiliate',
            dev_schemas         INT         COMMENT 'Schema count in dev',
            qa_schemas          INT         COMMENT 'Schema count in qa',
            prod_schemas        INT         COMMENT 'Schema count in prod',
            dev_tables          INT         COMMENT 'Table count in dev',
            qa_tables           INT         COMMENT 'Table count in qa',
            prod_tables         INT         COMMENT 'Table count in prod',
            total_tables        INT         COMMENT 'Total tables across all environments',
            consistency_score   FLOAT       COMMENT 'Pct of schemas present in all 3 envs (0-100)',
            schemas_only_dev    STRING      COMMENT 'JSON array of schemas only in dev',
            schemas_only_qa     STRING      COMMENT 'JSON array of schemas only in qa',
            schemas_only_prod   STRING      COMMENT 'JSON array of schemas only in prod',
            updated_at          TIMESTAMP   COMMENT 'Last refresh timestamp'
        )
        USING DELTA
        COMMENT 'Gold layer: source/program summary with environment consistency'
    """, "source_summary")

    # 3. Workspace summary
    exec_sql(f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{GOLD}.workspace_summary (
            workspace_id        STRING      COMMENT 'Databricks workspace numeric ID',
            workspace_url       STRING      COMMENT 'Full workspace URL',
            workspace_name      STRING      COMMENT 'Derived friendly name',
            affiliates          STRING      COMMENT 'Comma-separated affiliates hosted',
            programs            STRING      COMMENT 'Comma-separated programs hosted',
            environments        STRING      COMMENT 'Comma-separated environments hosted',
            catalog_count       INT         COMMENT 'Number of catalogs',
            schema_count        INT         COMMENT 'Number of schemas',
            table_count         INT         COMMENT 'Number of tables',
            updated_at          TIMESTAMP   COMMENT 'Last refresh timestamp'
        )
        USING DELTA
        COMMENT 'Gold layer: workspace-level summary'
    """, "workspace_summary")

    # 4. Environment consistency report
    exec_sql(f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{GOLD}.env_consistency (
            program             STRING      COMMENT 'Program code',
            affiliate           STRING      COMMENT 'BHE affiliate',
            schema_name         STRING      COMMENT 'Schema name',
            in_dev              BOOLEAN     COMMENT 'Present in dev environment',
            in_qa               BOOLEAN     COMMENT 'Present in qa environment',
            in_prod             BOOLEAN     COMMENT 'Present in prod environment',
            dev_tables          INT         COMMENT 'Table count in dev',
            qa_tables           INT         COMMENT 'Table count in qa',
            prod_tables         INT         COMMENT 'Table count in prod',
            issue_type          STRING      COMMENT 'Type of inconsistency',
            updated_at          TIMESTAMP   COMMENT 'Last refresh timestamp'
        )
        USING DELTA
        COMMENT 'Gold layer: schema-level environment consistency report'
    """, "env_consistency")

    # 5. Glossary at grain (affiliate, source_system, data_domain) - prep for Sankey
    exec_sql(f"""
        CREATE TABLE IF NOT EXISTS {CATALOG}.{GOLD}.glossary_system_domain (
            affiliate           STRING      COMMENT 'BHE affiliate (Unknown if not classified)',
            source_system       STRING      COMMENT 'Source system from silver_tables.source_system (Unknown if not enriched)',
            data_domain         STRING      COMMENT 'Data domain from schema_inventory.data_domain (Unknown if not enriched)',
            catalog_schemas     ARRAY<STRUCT<catalog: STRING, schema: STRING, table_count: INT>>
                                            COMMENT 'All catalog.schema combos contributing to this combination',
            schema_count        INT         COMMENT 'Distinct catalog.schema combos',
            table_count         INT         COMMENT 'Total tables in this combination',
            programs            ARRAY<STRING> COMMENT 'Distinct programs',
            zones               ARRAY<STRING> COMMENT 'Distinct zones (raw, standardized, published, etc.)',
            environments        ARRAY<STRING> COMMENT 'Distinct environments (dev, qa, prod)',
            sample_table_names  ARRAY<STRING> COMMENT 'Up to 10 example table names',
            updated_at          TIMESTAMP   COMMENT 'When this row was last (re)built'
        )
        USING DELTA
        COMMENT 'Gold layer glossary: affiliate x source_system x data_domain rollup of catalog/schema combos. Will feed the Sankey.'
    """, "glossary_system_domain")

    print("\n=== Gold tables created successfully ===")
    print(f"\nTables in {CATALOG}.{GOLD}:")
    r = exec_sql(f"SHOW TABLES IN {CATALOG}.{GOLD}", "list tables")
    if r:
        rows = r.get("result", {}).get("data_array", [])
        for row in rows:
            print(f"  - {row[1]}")


if __name__ == "__main__":
    main()
