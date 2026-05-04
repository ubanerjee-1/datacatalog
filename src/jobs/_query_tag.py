"""
Shared query-tag helpers for Spark jobs.

WHY THIS DIFFERS FROM THE BACKEND
---------------------------------
The backend talks to a Databricks SQL warehouse via the SQL Statement
Execution API and can populate the dedicated `query_tags` column in
`system.query.history` by passing a `query_tags` field on the request body.

Spark jobs, in contrast, run on a job cluster (not a SQL warehouse). The
`query_tags` column is NOT populated for non-SQL-warehouse compute. Instead
we get traceability via three mechanisms, all of which we set together:

  1. SparkContext.setJobDescription(desc)
       -> shown in Spark UI "Description" column for every job/stage that
          runs while the property is set.
  2. SparkContext.setLocalProperty("callSite.short", short)
       -> shown in Spark UI "Description" cell tooltip.
  3. SQL comment header prepended to every spark.sql() string
       -> embedded in the textual plan and in any text-search of run logs.
          (Spark cluster query history surfaces statement text in
          system.query.history when statements run via SQL warehouses; for
          job clusters we rely on Spark UI + driver logs.)

USAGE
-----
    with tag_block(spark, module="enrichment", submodule="staging_build",
                   run_id=os.environ.get("DATABRICKS_RUN_ID")):
        spark.sql(tagged_sql(sql, module="enrichment",
                             submodule="staging_build"))

The tag_block context manager makes sure the SparkContext properties get
cleaned up so they don't bleed into the next step of the job.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Iterator

APP_NAME = "bhe_catalog"


def _tag_dict(module: str, submodule: str, run_id: str | None = None,
              **extras: str) -> dict[str, str]:
    out = {"app": APP_NAME, "module": module, "submodule": submodule}
    rid = run_id or os.environ.get("DATABRICKS_JOB_RUN_ID") or os.environ.get("DATABRICKS_RUN_ID")
    if rid:
        out["run_id"] = str(rid)
    for k, v in extras.items():
        if v:
            out[k] = str(v)
    return out


def tagged_sql(sql: str, *, module: str, submodule: str,
               run_id: str | None = None, **extras: str) -> str:
    """Prepend a structured comment header to a SQL string for traceability.

    The header is parseable JSON inside a SQL comment so log scrapers can
    pick it up without ambiguity.
    """
    tags = _tag_dict(module, submodule, run_id, **extras)
    header = "/* bhe_query_tags=" + json.dumps(tags, separators=(",", ":")) + " */"
    return header + "\n" + sql


@contextmanager
def tag_block(spark, *, module: str, submodule: str,
              run_id: str | None = None, **extras: str) -> Iterator[None]:
    """Set Spark UI job description + properties for the duration of a block.

    Restores the previous values on exit so subsequent steps in the same job
    aren't mis-tagged.

    SERVERLESS COMPATIBILITY
    ------------------------
    Serverless compute blocks direct access to ``spark.sparkContext`` and
    raises ``[JVM_ATTRIBUTE_NOT_SUPPORTED]``. Tagging is observability only,
    so we fall back to a silent no-op rather than failing the job. SQL-level
    tagging via ``tagged_sql()`` (comment headers) still works on serverless
    because it operates entirely in SQL text space and never touches the JVM.
    """
    try:
        sc = spark.sparkContext
    except Exception:
        # Serverless or any other restricted runtime -> no-op.
        yield
        return

    tags = _tag_dict(module, submodule, run_id, **extras)
    desc = " | ".join(f"{k}={v}" for k, v in tags.items())

    try:
        prev_desc = sc.getLocalProperty("spark.job.description")
        prev_short = sc.getLocalProperty("callSite.short")
        prev_long = sc.getLocalProperty("callSite.long")

        sc.setJobDescription(desc)
        sc.setLocalProperty("callSite.short", f"{module}/{submodule}")
        sc.setLocalProperty("callSite.long", desc)
    except Exception:
        # If even setting properties fails (e.g. partial JVM access),
        # don't take down the workload.
        yield
        return

    try:
        yield
    finally:
        try:
            sc.setJobDescription(prev_desc)
            sc.setLocalProperty("callSite.short", prev_short)
            sc.setLocalProperty("callSite.long", prev_long)
        except Exception:
            pass
