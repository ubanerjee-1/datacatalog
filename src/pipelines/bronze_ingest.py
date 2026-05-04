"""
BHE Data Catalog - Bronze Ingestion Pipeline
Loads raw schema and table metadata CSVs from a Unity Catalog Volume
into streaming tables in the bhe_raw schema.
"""

from pyspark import pipelines as dp
from pyspark.sql.functions import current_timestamp, col, trim

# All Volume paths come from the pipeline `configuration:` block in
# resources/bhe_ingest.pipeline.yml so deployments to a different catalog
# don't have to fork this Python file.
schema_location_base = spark.conf.get("schema_location_base")
landing_schemas_path = spark.conf.get("landing_schemas_path")
landing_tables_path = spark.conf.get("landing_tables_path")


@dp.table(
    name="raw_schemas",
    comment="Raw schema metadata extracted from BHE Databricks workspaces",
    cluster_by=["catalog_name"],
)
def raw_schemas():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaLocation", f"{schema_location_base}/raw_schemas")
        .option("header", "true")
        .option("multiLine", "true")
        .option("escape", '"')
        .load(landing_schemas_path)
        .select(
            trim(col("workspace_url")).alias("workspace_url"),
            trim(col("catalog_name")).alias("catalog_name"),
            trim(col("schema_name")).alias("schema_name"),
            trim(col("schema_owner")).alias("schema_owner"),
            col("comment"),
            col("created"),
            col("created_by"),
            col("last_altered"),
            col("last_altered_by"),
            current_timestamp().alias("_ingested_at"),
            col("_metadata.file_path").alias("_source_file"),
        )
    )


@dp.table(
    name="raw_tables",
    comment="Raw table metadata extracted from BHE Databricks workspaces",
    cluster_by=["table_catalog", "table_schema"],
)
def raw_tables():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaLocation", f"{schema_location_base}/raw_tables")
        .option("header", "true")
        .option("multiLine", "true")
        .option("escape", '"')
        .load(landing_tables_path)
        .select(
            trim(col("workspace_url")).alias("workspace_url"),
            trim(col("table_catalog")).alias("table_catalog"),
            trim(col("table_schema")).alias("table_schema"),
            trim(col("table_name")).alias("table_name"),
            trim(col("table_type")).alias("table_type"),
            col("is_insertable_into"),
            col("commit_action"),
            trim(col("table_owner")).alias("table_owner"),
            col("comment"),
            col("created"),
            col("created_by"),
            col("last_altered"),
            col("last_altered_by"),
            trim(col("data_source_format")).alias("data_source_format"),
            col("storage_sub_directory"),
            col("storage_path"),
            current_timestamp().alias("_ingested_at"),
            col("_metadata.file_path").alias("_source_file"),
        )
    )
