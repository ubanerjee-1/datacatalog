"""
BHE Data Catalog - Build Glossary at grain (affiliate, source_system, data_domain).

Joins per-table source_system from silver_tables with per-schema affiliate
and data_domain from schema_inventory, then aggregates the catalog/schema
combos that contribute to each (affiliate, source_system, data_domain) tuple.

This is preparation for the Sankey: each row is a "glossary entry" that will
later be linked to upstream source/affiliate nodes and downstream
domain/use-case nodes in the diagram.

Empty values are bucketed into 'Unknown' so gaps remain visible (the Sankey
will highlight these as missing data).
"""

import argparse
import logging
import os
import sys
import time

from pyspark.sql import SparkSession

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
logger = logging.getLogger("build_glossary")

MODULE = "glossary"


def build_glossary_sql(catalog: str, silver_schema: str, gold_schema: str) -> str:
    stables = f"`{catalog}`.`{silver_schema}`.`silver_tables`"
    inv = f"`{catalog}`.`{gold_schema}`.`schema_inventory`"
    glossary = f"`{catalog}`.`{gold_schema}`.`glossary_system_domain`"

    return f"""
        CREATE OR REPLACE TABLE {glossary}
        USING DELTA
        COMMENT 'Gold layer glossary: affiliate x source_system x data_domain rollup of catalog/schema combos. Will feed the Sankey.'
        AS
        WITH si_dedup AS (
            SELECT catalog_name, schema_name, affiliate, data_domain,
                   program, zone, environment
            FROM (
                SELECT
                    catalog_name, schema_name, affiliate, data_domain,
                    program, zone, environment,
                    ROW_NUMBER() OVER (
                        PARTITION BY catalog_name, schema_name
                        ORDER BY
                            CASE WHEN COALESCE(affiliate,'') != '' THEN 0 ELSE 1 END,
                            CASE WHEN COALESCE(data_domain,'') != '' THEN 0 ELSE 1 END,
                            catalog_name
                    ) AS rn
                FROM {inv}
                -- DQ filters (system catalogs, information_schema/default)
                -- are now applied at the silver-build step. We still
                -- exclude classification='SYSTEM' here because
                -- schema_inventory may classify other rows as SYSTEM
                -- via business rules (e.g. operational metadata schemas).
                WHERE COALESCE(classification, '') NOT IN ('SYSTEM')
            )
            WHERE rn = 1
        ),
        joined AS (
            SELECT
                COALESCE(NULLIF(TRIM(si.affiliate), ''), 'Unknown')      AS affiliate,
                -- Prefer normalized canonical (from normalize_source_systems
                -- WF). Falls back to the raw LLM label, then 'Unknown', so
                -- this query stays correct whether or not normalization has
                -- run yet.
                COALESCE(
                    NULLIF(TRIM(t.source_system_canonical), ''),
                    NULLIF(TRIM(t.source_system), ''),
                    'Unknown'
                )                                                        AS source_system,
                COALESCE(NULLIF(TRIM(si.data_domain), ''), 'Unknown')    AS data_domain,
                t.table_catalog                                          AS catalog,
                t.table_schema                                           AS schema,
                t.table_name,
                NULLIF(TRIM(si.program), '')                             AS program,
                NULLIF(TRIM(si.zone), '')                                AS zone,
                NULLIF(TRIM(si.environment), '')                         AS environment
            FROM {stables} t
            LEFT JOIN si_dedup si
                ON t.table_catalog = si.catalog_name
               AND t.table_schema = si.schema_name
            -- DQ-level schema filter handled in silver build; only the
            -- semantic 'PRODUCTION' classification matters here.
            WHERE t.classification = 'PRODUCTION'
        ),
        per_schema AS (
            SELECT
                affiliate, source_system, data_domain,
                catalog, schema,
                COUNT(*)                              AS table_count,
                MAX(program)                          AS program,
                MAX(zone)                             AS zone,
                MAX(environment)                      AS environment,
                SLICE(COLLECT_LIST(table_name), 1, 5) AS sample_tbls
            FROM joined
            GROUP BY affiliate, source_system, data_domain, catalog, schema
        )
        SELECT
            affiliate,
            source_system,
            data_domain,
            COLLECT_LIST(NAMED_STRUCT(
                'catalog', catalog,
                'schema', schema,
                'table_count', CAST(table_count AS INT)
            )) AS catalog_schemas,
            CAST(COUNT(*) AS INT)                    AS schema_count,
            CAST(SUM(table_count) AS INT)            AS table_count,
            ARRAY_DISTINCT(COLLECT_LIST(program))    AS programs,
            ARRAY_DISTINCT(COLLECT_LIST(zone))       AS zones,
            ARRAY_DISTINCT(COLLECT_LIST(environment)) AS environments,
            SLICE(
                ARRAY_DISTINCT(FLATTEN(COLLECT_LIST(sample_tbls))),
                1, 10
            ) AS sample_table_names,
            CURRENT_TIMESTAMP() AS updated_at
        FROM per_schema
        GROUP BY affiliate, source_system, data_domain
    """


def main():
    parser = argparse.ArgumentParser(description="Build glossary_system_domain")
    parser.add_argument("--catalog", default="your_catalog")
    parser.add_argument("--silver-schema", default="bhe_silver")
    parser.add_argument("--gold-schema", default="bhe_gold")
    args = parser.parse_args()

    spark = SparkSession.builder.getOrCreate()

    stables = f"`{args.catalog}`.`{args.silver_schema}`.`silver_tables`"
    inv = f"`{args.catalog}`.`{args.gold_schema}`.`schema_inventory`"
    glossary = f"`{args.catalog}`.`{args.gold_schema}`.`glossary_system_domain`"

    with tag_block(spark, module=MODULE, submodule="input_counts"):
        enriched_count = spark.sql(tagged_sql(f"""
            SELECT COUNT(*) AS cnt FROM {stables}
            WHERE COALESCE(source_system, '') != ''
        """, module=MODULE, submodule="input_counts.silver")).collect()[0]["cnt"]
        inv_count = spark.sql(tagged_sql(
            f"SELECT COUNT(*) AS cnt FROM {inv}",
            module=MODULE, submodule="input_counts.inventory",
        )).collect()[0]["cnt"]
    logger.info(f"Inputs: silver_tables enriched={enriched_count}, schema_inventory rows={inv_count}")

    if enriched_count == 0:
        logger.warning("silver_tables.source_system is empty — glossary will be all 'Unknown'. "
                       "Run BHE AI Table Enrichment first.")

    logger.info("Building glossary_system_domain...")
    start = time.time()
    with tag_block(spark, module=MODULE, submodule="build_glossary_table"):
        spark.sql(tagged_sql(
            build_glossary_sql(args.catalog, args.silver_schema, args.gold_schema),
            module=MODULE, submodule="build_glossary_table",
        ))
    elapsed = time.time() - start
    logger.info(f"Build complete in {elapsed:.1f}s")

    with tag_block(spark, module=MODULE, submodule="glossary_stats"):
        stats = spark.sql(tagged_sql(f"""
        SELECT
            COUNT(*)                                                              AS rows,
            SUM(CASE WHEN affiliate     = 'Unknown' THEN 1 ELSE 0 END)            AS rows_unknown_affiliate,
            SUM(CASE WHEN source_system = 'Unknown' THEN 1 ELSE 0 END)            AS rows_unknown_system,
            SUM(CASE WHEN data_domain   = 'Unknown' THEN 1 ELSE 0 END)            AS rows_unknown_domain,
            SUM(schema_count)                                                     AS total_schema_combos,
            SUM(table_count)                                                      AS total_tables,
            COUNT(DISTINCT affiliate)                                             AS distinct_affiliates,
            COUNT(DISTINCT source_system)                                         AS distinct_systems,
            COUNT(DISTINCT data_domain)                                           AS distinct_domains
        FROM {glossary}
    """, module=MODULE, submodule="glossary_stats")).collect()[0]
    logger.info("=" * 60)
    logger.info("Glossary build summary:")
    for k in ("rows", "rows_unknown_affiliate", "rows_unknown_system",
              "rows_unknown_domain", "total_schema_combos", "total_tables",
              "distinct_affiliates", "distinct_systems", "distinct_domains"):
        logger.info(f"  {k:30s} = {stats[k]}")
    logger.info("=" * 60)

    logger.info("Top 10 (affiliate, source_system, data_domain) by table_count:")
    with tag_block(spark, module=MODULE, submodule="glossary_top10"):
        top = spark.sql(tagged_sql(f"""
            SELECT affiliate, source_system, data_domain, schema_count, table_count
            FROM {glossary}
            ORDER BY table_count DESC
            LIMIT 10
        """, module=MODULE, submodule="glossary_top10")).collect()
    for r in top:
        logger.info(
            f"  {r['affiliate']:25s} | {r['source_system']:20s} | "
            f"{r['data_domain']:25s} | schemas={r['schema_count']:4d} tables={r['table_count']:6d}"
        )


if __name__ == "__main__":
    main()
