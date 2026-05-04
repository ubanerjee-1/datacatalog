"""
BHE Data Catalog - AI Table Enrichment Job (staged pattern)

Enriches silver_tables with per-table business_friendly_name, ai_definition,
and source_system using ai_query() with schema context from schema_inventory.

WHY THE STAGED PATTERN
----------------------
Previous design ran a single MERGE whose source CTE included:

    raw_ai  : ai_query(...) AS resp
    cleaned : REGEXP_REPLACE(resp.result, ...)
    parsed  : from_json(clean_json, '... STRING, ... STRING, ... STRING') AS c
    MERGE   : SET target.field1 = src.c.field1,
                  target.field2 = src.c.field2,
                  target.field3 = src.c.field3

Multiple references to `c.<field>` push the `from_json` projection down
through the optimizer, causing it to evaluate `ai_query` ONCE PER OUTPUT
FIELD (~4x the LLM calls). EXPLAIN and wall-clock benchmarks both confirm
~3.66x slowdown.

The fix is to evaluate `ai_query` exactly once and persist the raw response
to a staging table. The MERGE then reads from a column in storage; no LLM
call is repeated.

Side benefits:
  - Visible progress: `SELECT count(*) FROM <staging>` shows row count climbing.
  - Resumable: if the MERGE fails, you don't re-pay for LLM calls.
  - Cheaper: ~4x fewer model serving inference calls.

Other design notes:
  - Endpoint: databricks-claude-sonnet-4 (batch-optimized FMAPI).
  - schema_inventory is deduped (ROW_NUMBER) BEFORE the LEFT JOIN to avoid
    the historical 2.2x fanout from multiple inventory rows per (catalog, schema).
  - failOnError => false so partial failures don't abort the run; output is
    a STRUCT<result: STRING, errorMessage: STRING>.
  - modelParameters: max_tokens=256, temperature=0 for deterministic JSON.
"""

import argparse
import logging
import os
import sys
import time

from pyspark.sql import SparkSession

# Ensure sibling _query_tag.py is importable regardless of cwd Spark uses.
# Databricks runs Python tasks via exec() in an ipykernel, so __file__
# is not always defined; fall back to argv[0] / cwd in that case.
try:
    _here = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _here = (
        os.path.dirname(os.path.abspath(sys.argv[0]))
        if sys.argv and sys.argv[0]
        else os.getcwd()
    )
sys.path.insert(0, _here)
from _query_tag import tag_block, tagged_sql  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai_enrich_tables")

MODULE = "enrichment"

LLM_ENDPOINT = "databricks-claude-sonnet-4"
STAGING_TABLE_NAME = "silver_tables_ai_staging"


def _prompt_expr(company_name: str) -> str:
    """Return the SQL fragment that builds the per-row LLM prompt."""
    esc = company_name.replace("'", "''")
    return (
        "CONCAT("
        f"'You are a data catalog expert for {esc}. "
        "Given a table name and its schema context, return ONLY a JSON object "
        "(no markdown, no prose) with: "
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


def build_staging_sql(catalog: str, silver_schema: str, gold_schema: str,
                      schema_name: str = "",
                      company_name: str = "the company",
                      max_rows: int | None = None) -> str:
    """Step 1 SQL: ai_query() exactly once per candidate row, materialize to disk."""
    inv = f"`{catalog}`.`{gold_schema}`.`schema_inventory`"
    stables = f"`{catalog}`.`{silver_schema}`.`silver_tables`"
    staging = f"`{catalog}`.`{silver_schema}`.`{STAGING_TABLE_NAME}`"

    schema_filter = f"AND t.table_schema = '{schema_name}'" if schema_name else ""
    limit_clause = f"LIMIT {int(max_rows)}" if max_rows else ""

    return f"""
        CREATE OR REPLACE TABLE {staging}
        USING DELTA
        COMMENT 'AI Table Enrichment staging: one row per candidate with raw ai_query() output. Built by src/jobs/ai_enrich_tables.py.'
        AS
        WITH si_dedup AS (
            SELECT catalog_name, schema_name, definition, business_name,
                   program, affiliate, zone
            FROM (
                SELECT
                    catalog_name, schema_name, definition, business_name,
                    program, affiliate, zone,
                    ROW_NUMBER() OVER (
                        PARTITION BY catalog_name, schema_name
                        ORDER BY LENGTH(COALESCE(definition,'')) DESC
                    ) AS rn
                FROM {inv}
            )
            WHERE rn = 1
        ),
        candidates AS (
            -- DQ filters (system/sample catalogs, information_schema/default
            -- schemas, dedup) are now handled at the silver-build step in
            -- bootstrap_tables.py / ai_enrich_metadata.py. Here we only
            -- filter on enrichment-relevant predicates.
            SELECT t.table_catalog, t.table_schema, t.table_name,
                   t.table_type, t.data_source_format, t.comment
            FROM {stables} t
            WHERE (t.ai_definition IS NULL OR t.ai_definition = ''
                   OR t.source_system IS NULL OR t.source_system = '')
              AND t.classification = 'PRODUCTION'
              AND COALESCE(t.is_user_edited, false) = false
              {schema_filter}
            {limit_clause}
        )
        SELECT
            t.table_catalog,
            t.table_schema,
            t.table_name,
            ai_query(
                '{LLM_ENDPOINT}',
                {_prompt_expr(company_name)},
                failOnError => false,
                modelParameters => named_struct(
                    'max_tokens', 256,
                    'temperature', 0.0
                )
            ) AS resp
        FROM candidates t
        LEFT JOIN si_dedup si
            ON t.table_catalog = si.catalog_name
           AND t.table_schema = si.schema_name
    """


def build_merge_sql(catalog: str, silver_schema: str) -> str:
    """Step 2 SQL: parse staging table and MERGE into silver_tables. No LLM calls."""
    stables = f"`{catalog}`.`{silver_schema}`.`silver_tables`"
    staging = f"`{catalog}`.`{silver_schema}`.`{STAGING_TABLE_NAME}`"
    json_schema = (
        "business_friendly_name STRING, ai_definition STRING, source_system STRING"
    )

    # Defensive dedup on the MERGE source: even after silver is clean,
    # the staging table can carry duplicates if it was built when silver
    # had them. ROW_NUMBER picks the longest non-empty ai_definition per
    # (catalog, schema, table) so the MERGE never sees more than one
    # source row per target key (which is what triggered the
    # DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW_IN_MERGE failure).
    return f"""
        MERGE INTO {stables} AS target
        USING (
            WITH cleaned AS (
                SELECT
                    table_catalog, table_schema, table_name,
                    REGEXP_REPLACE(
                        REGEXP_REPLACE(resp.result, '^```\\\\w*\\\\n?', ''),
                        '\\\\n?```$', ''
                    ) AS clean_json
                FROM {staging}
                WHERE resp.result IS NOT NULL AND resp.result != ''
            ),
            parsed AS (
                SELECT
                    table_catalog, table_schema, table_name,
                    from_json(TRIM(clean_json), '{json_schema}') AS c
                FROM cleaned
            ),
            valid AS (
                SELECT table_catalog, table_schema, table_name, c
                FROM parsed
                WHERE c.ai_definition IS NOT NULL AND c.ai_definition != ''
            ),
            deduped AS (
                SELECT table_catalog, table_schema, table_name, c
                FROM valid
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY table_catalog, table_schema, table_name
                    ORDER BY LENGTH(COALESCE(c.ai_definition, '')) DESC
                ) = 1
            )
            SELECT table_catalog, table_schema, table_name, c
            FROM deduped
        ) AS src
        ON target.table_catalog = src.table_catalog
           AND target.table_schema = src.table_schema
           AND target.table_name = src.table_name
        WHEN MATCHED AND COALESCE(target.is_user_edited, false) = false THEN UPDATE SET
            target.ai_definition = src.c.ai_definition,
            target.business_friendly_name = src.c.business_friendly_name,
            target.source_system = src.c.source_system
    """


def main():
    parser = argparse.ArgumentParser(description="AI Table Enrichment (staged)")
    parser.add_argument("--catalog", default="your_catalog")
    parser.add_argument("--silver-schema", default="bhe_silver")
    parser.add_argument("--gold-schema", default="bhe_gold")
    parser.add_argument("--schema-name", default="",
                        help="Limit to a specific schema (empty = all)")
    parser.add_argument("--company-name", default="the company",
                        help="Company name for LLM prompt context")
    parser.add_argument("--max-rows", type=int, default=0,
                        help="Optional cap on candidates (0 = all). Useful for "
                             "smoke tests; in production set 0 so ai_query "
                             "can parallelize over the full set.")
    parser.add_argument("--skip-staging", action="store_true",
                        help="Skip step 1 (don't rebuild staging) and only "
                             "re-run the MERGE from existing staging table.")
    parser.add_argument("--drop-staging", action="store_true",
                        help="Drop the staging table at the end. By default it "
                             "is preserved so the MERGE can be re-run without "
                             "paying for LLM calls again.")
    parser.add_argument("--prune-staging", action="store_true",
                        help="Before the MERGE, delete staging rows whose "
                             "(catalog, schema, table) keys are no longer in "
                             "silver_tables. Makes --skip-staging re-runs "
                             "cheaper because the MERGE source CTE only scans "
                             "relevant rows. Trade-off: keys later re-added to "
                             "silver will need to pay for ai_query() again.")
    # Legacy params retained for DAB compatibility (no-ops now).
    parser.add_argument("--batch-size", type=int, default=0, help="(deprecated)")
    parser.add_argument("--max-batches", type=int, default=0, help="(deprecated)")
    args = parser.parse_args()

    if args.batch_size or args.max_batches:
        logger.info(
            "Note: --batch-size / --max-batches are deprecated and ignored; "
            "the redesigned job uses a staged ai_query + MERGE pattern."
        )

    spark = SparkSession.builder.getOrCreate()

    company_name = args.company_name
    if company_name == "the company":
        try:
            with tag_block(spark, module=MODULE, submodule="lookup_company_name"):
                profile_rows = spark.sql(tagged_sql(
                    f"SELECT company_name FROM `{args.catalog}`.`{args.silver_schema}`.`company_profile` LIMIT 1",
                    module=MODULE, submodule="lookup_company_name",
                )).collect()
            if profile_rows and profile_rows[0]["company_name"]:
                company_name = profile_rows[0]["company_name"]
        except Exception:
            pass
    logger.info(f"Using company name: {company_name}")
    logger.info(f"LLM endpoint: {LLM_ENDPOINT}")

    stables = f"`{args.catalog}`.`{args.silver_schema}`.`silver_tables`"
    staging = f"`{args.catalog}`.`{args.silver_schema}`.`{STAGING_TABLE_NAME}`"

    # ---------------------------------------------------------------------
    # Step 1: rebuild staging by running ai_query once per candidate
    # ---------------------------------------------------------------------
    if args.skip_staging:
        logger.info("--skip-staging set; reusing existing staging table.")
    else:
        schema_filter = f"AND table_schema = '{args.schema_name}'" if args.schema_name else ""
        with tag_block(spark, module=MODULE, submodule="count_candidates"):
            candidate_count = spark.sql(tagged_sql(f"""
                SELECT COUNT(*) AS cnt FROM {stables}
                WHERE (ai_definition IS NULL OR ai_definition = ''
                       OR source_system IS NULL OR source_system = '')
                  AND classification = 'PRODUCTION'
                  AND COALESCE(is_user_edited, false) = false
                  {schema_filter}
            """, module=MODULE, submodule="count_candidates")).collect()[0]["cnt"]
        logger.info(f"Candidate tables to enrich: {candidate_count}")

        if candidate_count == 0:
            logger.info("Nothing to enrich. Done.")
            return

        if args.max_rows:
            logger.info(f"Capping this run at --max-rows={args.max_rows}")

        logger.info(f"Step 1/2: Building staging table {staging} via ai_query() "
                    "(this is the slow LLM phase)...")
        t0 = time.time()
        with tag_block(spark, module=MODULE, submodule="staging_build_ai_query"):
            spark.sql(tagged_sql(
                build_staging_sql(
                    catalog=args.catalog,
                    silver_schema=args.silver_schema,
                    gold_schema=args.gold_schema,
                    company_name=company_name,
                    schema_name=args.schema_name,
                    max_rows=args.max_rows or None,
                ),
                module=MODULE, submodule="staging_build_ai_query",
            ))
        elapsed = time.time() - t0
        with tag_block(spark, module=MODULE, submodule="staging_stats"):
            staging_stats = spark.sql(tagged_sql(f"""
                SELECT
                    COUNT(*)                                                         AS rows,
                    SUM(CASE WHEN resp.result IS NOT NULL AND resp.result != '' THEN 1 ELSE 0 END)
                                                                                      AS rows_ok,
                    SUM(CASE WHEN resp.errorMessage IS NOT NULL AND resp.errorMessage != '' THEN 1 ELSE 0 END)
                                                                                      AS rows_err,
                    AVG(LENGTH(COALESCE(resp.result, '')))                            AS avg_resp_chars
                FROM {staging}
            """, module=MODULE, submodule="staging_stats")).collect()[0]
        logger.info(
            f"Step 1 complete in {elapsed:.0f}s ({elapsed/60:.1f} min). "
            f"rows={staging_stats['rows']}, ok={staging_stats['rows_ok']}, "
            f"err={staging_stats['rows_err']}, "
            f"avg_resp_chars={float(staging_stats['avg_resp_chars'] or 0):.0f}"
        )
        if staging_stats["rows"] and elapsed > 0:
            rate = staging_stats["rows"] / elapsed
            logger.info(
                f"Step 1 throughput: {rate:.2f} rows/sec ({rate*60:.0f} rows/min, "
                f"{rate*3600:.0f} rows/hour)"
            )

    # ---------------------------------------------------------------------
    # Optional: prune staging to keys present in silver. Cheap to re-run.
    # ---------------------------------------------------------------------
    if args.prune_staging:
        with tag_block(spark, module=MODULE, submodule="prune_staging_count"):
            pre_prune = spark.sql(tagged_sql(
                f"SELECT COUNT(*) AS c FROM {staging}",
                module=MODULE, submodule="prune_staging_count",
            )).collect()[0]["c"]
        logger.info(f"Pruning staging: {pre_prune} rows before prune.")
        with tag_block(spark, module=MODULE, submodule="prune_staging_delete"):
            spark.sql(tagged_sql(f"""
                DELETE FROM {staging} AS s
                WHERE NOT EXISTS (
                    SELECT 1 FROM {stables} t
                    WHERE t.table_catalog = s.table_catalog
                      AND t.table_schema  = s.table_schema
                      AND t.table_name    = s.table_name
                )
            """, module=MODULE, submodule="prune_staging_delete"))
        with tag_block(spark, module=MODULE, submodule="prune_staging_count_after"):
            post_prune = spark.sql(tagged_sql(
                f"SELECT COUNT(*) AS c FROM {staging}",
                module=MODULE, submodule="prune_staging_count_after",
            )).collect()[0]["c"]
        logger.info(
            f"Pruned staging: {pre_prune - post_prune} rows removed "
            f"({post_prune} remain)."
        )

    # ---------------------------------------------------------------------
    # Step 2: MERGE the parsed staging into silver_tables (pure I/O)
    # ---------------------------------------------------------------------
    with tag_block(spark, module=MODULE, submodule="enriched_count_before"):
        enriched_before = spark.sql(tagged_sql(f"""
            SELECT COUNT(*) AS cnt FROM {stables}
            WHERE COALESCE(ai_definition,'') != ''
              AND COALESCE(source_system,'') != ''
        """, module=MODULE, submodule="enriched_count_before")).collect()[0]["cnt"]

    logger.info(f"Step 2/2: MERGE-ing parsed staging into silver_tables...")
    t1 = time.time()
    with tag_block(spark, module=MODULE, submodule="merge_into_silver"):
        spark.sql(tagged_sql(
            build_merge_sql(args.catalog, args.silver_schema),
            module=MODULE, submodule="merge_into_silver",
        ))
    elapsed_merge = time.time() - t1

    with tag_block(spark, module=MODULE, submodule="enriched_count_after"):
        enriched_after = spark.sql(tagged_sql(f"""
            SELECT COUNT(*) AS cnt FROM {stables}
            WHERE COALESCE(ai_definition,'') != ''
              AND COALESCE(source_system,'') != ''
        """, module=MODULE, submodule="enriched_count_after")).collect()[0]["cnt"]
    delta = enriched_after - enriched_before
    logger.info(f"Step 2 complete in {elapsed_merge:.1f}s. "
                f"Newly enriched rows: {delta}")

    if args.drop_staging:
        logger.info(f"Dropping staging table {staging} (--drop-staging set).")
        with tag_block(spark, module=MODULE, submodule="drop_staging"):
            spark.sql(tagged_sql(
                f"DROP TABLE IF EXISTS {staging}",
                module=MODULE, submodule="drop_staging",
            ))
    else:
        logger.info(
            f"Staging table {staging} preserved; "
            "re-run with --skip-staging to MERGE again without LLM cost."
        )

    logger.info("=" * 60)
    logger.info("AI Table Enrichment Complete")
    logger.info(f"  Endpoint:           {LLM_ENDPOINT}")
    logger.info(f"  Pattern:            staged (ai_query → MERGE)")
    logger.info(f"  Enriched (before):  {enriched_before}")
    logger.info(f"  Enriched (after):   {enriched_after}")
    logger.info(f"  New rows enriched:  {delta}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
