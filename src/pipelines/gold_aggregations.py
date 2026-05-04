"""
BHE Data Catalog - Gold Aggregation Pipeline
Materialized views aggregating silver data for fast app consumption.
"""

from pyspark import pipelines as dp
from pyspark.sql import functions as F

silver_catalog = spark.conf.get("silver_catalog")
silver_schema = spark.conf.get("silver_schema")


@dp.materialized_view(
    name="catalog_summary",
    comment="Aggregated stats per catalog for dashboard consumption",
)
def catalog_summary():
    tables = spark.read.table(f"{silver_catalog}.{silver_schema}.silver_tables")
    schemas = spark.read.table(f"{silver_catalog}.{silver_schema}.silver_schemas")

    table_stats = (
        tables.groupBy("table_catalog")
        .agg(
            F.count("*").alias("total_tables"),
            F.countDistinct("table_schema").alias("schema_count"),
            F.sum(F.when(F.col("table_type") == "MANAGED", 1).otherwise(0)).alias("managed_count"),
            F.sum(F.when(F.col("table_type") == "VIEW", 1).otherwise(0)).alias("view_count"),
            F.sum(F.when(F.col("table_type") == "EXTERNAL", 1).otherwise(0)).alias("external_count"),
            F.sum(F.when(F.col("table_type") == "STREAMING_TABLE", 1).otherwise(0)).alias("streaming_count"),
            F.sum(F.when(F.col("table_type") == "MATERIALIZED_VIEW", 1).otherwise(0)).alias("mv_count"),
            F.sum(F.when(F.col("ai_definition") != "", 1).otherwise(0)).alias("enriched_count"),
            F.sum(F.when(F.col("comment").isNotNull() & (F.col("comment") != ""), 1).otherwise(0)).alias("documented_count"),
            F.countDistinct("table_owner").alias("unique_owners"),
        )
    )

    schema_stats = (
        schemas.groupBy("catalog_name")
        .agg(
            F.first("environment").alias("environment"),
            F.first("zone").alias("zone"),
            F.first("program").alias("program"),
            F.collect_set("suggested_domain").alias("domains"),
            F.collect_set("suggested_department").alias("departments"),
        )
    )

    return table_stats.join(
        schema_stats,
        table_stats.table_catalog == schema_stats.catalog_name,
        "left",
    ).select(
        F.col("table_catalog").alias("catalog_name"),
        "total_tables",
        "schema_count",
        "managed_count",
        "view_count",
        "external_count",
        "streaming_count",
        "mv_count",
        "enriched_count",
        "documented_count",
        "unique_owners",
        "environment",
        "zone",
        "program",
        "domains",
        "departments",
    )


@dp.materialized_view(
    name="domain_summary",
    comment="Aggregated stats per business domain",
)
def domain_summary():
    schemas = spark.read.table(f"{silver_catalog}.{silver_schema}.silver_schemas")
    tables = spark.read.table(f"{silver_catalog}.{silver_schema}.silver_tables")

    schema_agg = (
        schemas.groupBy("suggested_domain")
        .agg(
            F.count("*").alias("schema_count"),
            F.countDistinct("catalog_name").alias("catalog_count"),
            F.collect_set("suggested_department").alias("departments"),
            F.collect_set("environment").alias("environments"),
            F.avg(
                F.when(F.col("ai_definition") != "", 1.0).otherwise(0.0)
            ).alias("enrichment_coverage"),
        )
    )

    joined = schemas.select(
        F.concat_ws(".", "catalog_name", "schema_name").alias("schema_key"),
        "suggested_domain",
    )
    table_counts = (
        tables.withColumn(
            "schema_key",
            F.concat_ws(".", "table_catalog", "table_schema"),
        )
        .join(joined, "schema_key", "left")
        .groupBy("suggested_domain")
        .agg(F.count("*").alias("table_count"))
    )

    return schema_agg.join(table_counts, "suggested_domain", "left").select(
        F.col("suggested_domain").alias("domain"),
        "schema_count",
        "catalog_count",
        "table_count",
        "departments",
        "environments",
        "enrichment_coverage",
    )


@dp.materialized_view(
    name="environment_summary",
    comment="Aggregated stats per environment",
)
def environment_summary():
    schemas = spark.read.table(f"{silver_catalog}.{silver_schema}.silver_schemas")
    return (
        schemas.groupBy("environment")
        .agg(
            F.count("*").alias("schema_count"),
            F.countDistinct("catalog_name").alias("catalog_count"),
            F.collect_set("suggested_domain").alias("domains"),
            F.collect_set("program").alias("programs"),
        )
    )


@dp.materialized_view(
    name="sankey_view",
    comment="Pre-joined Sankey data for the Data Sources -> Use Cases -> Departments visualization",
)
def sankey_view():
    mappings = spark.read.table(f"{silver_catalog}.{silver_schema}.sankey_mappings")
    return mappings.select(
        "id",
        "source_system",
        "source_category",
        "use_case",
        "department",
        "relevance",
        "company_name",
        "is_user_edited",
    )
