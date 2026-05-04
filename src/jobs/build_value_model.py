"""
Value-model build: link use cases to (a) canonical source systems and
(b) BHE affiliates, and seed the supporting dimension tables.

Why this job exists
-------------------
The Source System Browser (Phase 0 prior work) tells business users
"what data we have." This job answers the next question:
"what use cases can we deliver with that data, for whom, and what's missing?"

It produces four gold tables:

  bhe_gold.affiliates                  (seed, customer-editable)
  bhe_gold.program_affiliate_map       (seed, customer-editable)
  bhe_gold.use_case_source_requirements (LLM-derived + manual override)
  bhe_gold.use_case_affiliates          (LLM-derived + manual override)

Pipeline (5 stages, each tagged + idempotent)
---------------------------------------------
1. seed_affiliates
   Upserts bhe_gold.affiliates from src/data/affiliates_seed.csv.
   Customer edits to the CSV propagate on next run; manual edits in the
   table (is_user_edited=true) are NEVER overwritten.

2. seed_program_affiliate_map
   Upserts bhe_gold.program_affiliate_map from
   src/data/program_affiliate_map_seed.csv. Same is_user_edited contract.

3. extract_unresolved_use_cases
   Selects every use_case in bhe_silver.use_cases that does not yet have
   any rows in use_case_source_requirements. This bounds LLM cost to
   NEW use cases only.

4. apply_llm_mapping
   For each unresolved use case, runs ONE ai_query call that returns BOTH
   - required_sources (canonical, necessity, excerpt)
   - applicable_affiliates (affiliate, applicability, rationale)
   The canonical source list and affiliate list are passed as closed
   vocabularies in the prompt. EXPLODEs the arrays and MERGEs into the
   two child tables. Manual rows (is_user_edited=true) are preserved.

5. validate
   Prints summary stats per table for quick sanity in job logs.

CLI
---
    --catalog                  default your_catalog
    --silver-schema            default bhe_silver
    --gold-schema              default bhe_gold
    --affiliates-seed          path to affiliates seed CSV
    --program-map-seed         path to program_affiliate_map seed CSV
    --reseed                   re-import seed CSVs (idempotent; respects is_user_edited)
    --remap-unmapped           re-evaluate use cases that previously mapped only to 'Unmapped'
    --skip-llm                 skip stage 4 (seed only)
    --llm-endpoint             default databricks-claude-sonnet-4

Re-run safety
-------------
Idempotent. Manual edits (is_user_edited=true) are preserved across runs.
"""
from __future__ import annotations

import argparse
import csv
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
logger = logging.getLogger("build_value_model")

MODULE = "build_value_model"
DEFAULT_LLM_ENDPOINT = "databricks-claude-sonnet-4"


def _sql_str(s: str) -> str:
    return "'" + (s or "").replace("'", "''") + "'"


# =====================================================================
# Stage 1: seed bhe_gold.affiliates from CSV
# =====================================================================
def _resolve_csv(explicit: str, filename: str) -> str:
    if explicit:
        return explicit
    candidates = [
        os.path.join(os.path.dirname(_here), "data", filename),
        os.path.join(os.getcwd(), "src", "data", filename),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        f"Could not locate {filename} in: " + ", ".join(candidates)
    )


def _read_affiliates_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name = (r.get("affiliate_name") or r.get("canonical") or "").strip()
            if not name:
                continue
            rows.append({
                "affiliate_name": name,
                "affiliate_code": (r.get("affiliate_code") or "").strip(),
                "business_type": (r.get("business_type") or "").strip(),
                "region": (r.get("region") or "").strip(),
                "description": (r.get("description") or "").strip(),
                "is_active": (r.get("is_active") or "true").strip().lower() == "true",
            })
    return rows


def stage_seed_affiliates(spark: SparkSession, *, catalog: str, gold: str,
                          seed_csv: str, reseed: bool) -> None:
    tbl = f"`{catalog}`.`{gold}`.`affiliates`"

    with tag_block(spark, module=MODULE, submodule="seed_affiliates_ddl"):
        spark.sql(tagged_sql(f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
                affiliate_name STRING NOT NULL COMMENT 'Canonical affiliate name (primary key)',
                affiliate_code STRING          COMMENT 'Short code (PAC, NVE, MEC, ...)',
                business_type  STRING          COMMENT 'electric_utility | electric_gas_utility | renewables_developer | natural_gas_pipeline | electric_transmission | electric_distribution | corporate',
                region         STRING          COMMENT 'Geographic footprint',
                description    STRING          COMMENT 'Short human-readable description',
                is_active      BOOLEAN         COMMENT 'Soft-delete flag',
                is_user_edited BOOLEAN         COMMENT 'true = manual edit; never overwritten by job',
                created_at     TIMESTAMP,
                updated_at     TIMESTAMP
            )
            USING DELTA
            COMMENT 'Customer-editable BHE affiliates (operating subsidiaries). Seeded from src/data/affiliates_seed.csv on every run; manual edits survive.'
        """, module=MODULE, submodule="seed_affiliates_ddl"))

    rows = _read_affiliates_csv(seed_csv)
    if not rows:
        logger.warning("affiliates seed CSV produced 0 rows; skipping merge.")
        return

    with tag_block(spark, module=MODULE, submodule="seed_affiliates_merge"):
        values = ",\n".join(
            f"({_sql_str(r['affiliate_name'])}, {_sql_str(r['affiliate_code'])}, "
            f"{_sql_str(r['business_type'])}, {_sql_str(r['region'])}, "
            f"{_sql_str(r['description'])}, {str(r['is_active']).lower()})"
            for r in rows
        )
        spark.sql(tagged_sql(f"""
            MERGE INTO {tbl} AS t
            USING (
                SELECT * FROM (VALUES {values})
                AS v(affiliate_name, affiliate_code, business_type,
                     region, description, is_active)
            ) AS s
            ON t.affiliate_name = s.affiliate_name
            WHEN MATCHED AND COALESCE(t.is_user_edited, false) = false THEN
                UPDATE SET
                    t.affiliate_code = s.affiliate_code,
                    t.business_type  = s.business_type,
                    t.region         = s.region,
                    t.description    = s.description,
                    t.is_active      = s.is_active,
                    t.updated_at     = current_timestamp()
            WHEN NOT MATCHED THEN
                INSERT (affiliate_name, affiliate_code, business_type, region,
                        description, is_active, is_user_edited,
                        created_at, updated_at)
                VALUES (s.affiliate_name, s.affiliate_code, s.business_type,
                        s.region, s.description, s.is_active, false,
                        current_timestamp(), current_timestamp())
        """, module=MODULE, submodule="seed_affiliates_merge"))
    logger.info(f"Stage 1: seeded {len(rows)} affiliates into {tbl}")


# =====================================================================
# Stage 2: seed bhe_gold.program_affiliate_map from CSV
# =====================================================================
def _read_program_map_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            program = (r.get("program") or "").strip()
            affiliate = (r.get("affiliate") or "").strip()
            if not program or not affiliate:
                continue
            rows.append({
                "program": program,
                "affiliate_name": affiliate,
                "affiliation_strength": (r.get("affiliation_strength") or "primary").strip(),
                "notes": (r.get("notes") or "").strip(),
            })
    return rows


def stage_seed_program_map(spark: SparkSession, *, catalog: str, gold: str,
                           seed_csv: str) -> None:
    tbl = f"`{catalog}`.`{gold}`.`program_affiliate_map`"

    with tag_block(spark, module=MODULE, submodule="seed_program_map_ddl"):
        spark.sql(tagged_sql(f"""
            CREATE TABLE IF NOT EXISTS {tbl} (
                program              STRING NOT NULL COMMENT 'silver_schemas.program value',
                affiliate_name       STRING NOT NULL COMMENT 'FK -> affiliates.affiliate_name',
                affiliation_strength STRING          COMMENT 'primary | secondary',
                notes                STRING,
                is_user_edited       BOOLEAN,
                updated_at           TIMESTAMP
            )
            USING DELTA
            COMMENT 'Bridge: catalog program -> BHE affiliate. One program may map to N affiliates. Customer-editable; manual edits survive.'
        """, module=MODULE, submodule="seed_program_map_ddl"))

    rows = _read_program_map_csv(seed_csv)
    if not rows:
        logger.warning("program_affiliate_map seed CSV produced 0 rows; skipping merge.")
        return

    with tag_block(spark, module=MODULE, submodule="seed_program_map_merge"):
        values = ",\n".join(
            f"({_sql_str(r['program'])}, {_sql_str(r['affiliate_name'])}, "
            f"{_sql_str(r['affiliation_strength'])}, {_sql_str(r['notes'])})"
            for r in rows
        )
        spark.sql(tagged_sql(f"""
            MERGE INTO {tbl} AS t
            USING (
                SELECT * FROM (VALUES {values})
                AS v(program, affiliate_name, affiliation_strength, notes)
            ) AS s
            ON t.program = s.program AND t.affiliate_name = s.affiliate_name
            WHEN MATCHED AND COALESCE(t.is_user_edited, false) = false THEN
                UPDATE SET
                    t.affiliation_strength = s.affiliation_strength,
                    t.notes                = s.notes,
                    t.updated_at           = current_timestamp()
            WHEN NOT MATCHED THEN
                INSERT (program, affiliate_name, affiliation_strength, notes,
                        is_user_edited, updated_at)
                VALUES (s.program, s.affiliate_name, s.affiliation_strength,
                        s.notes, false, current_timestamp())
        """, module=MODULE, submodule="seed_program_map_merge"))
    logger.info(f"Stage 2: seeded {len(rows)} program->affiliate rows into {tbl}")


# =====================================================================
# Stage 3 + 4: LLM-derive use_case_source_requirements + use_case_affiliates
# =====================================================================
def _dedup_target_table(spark: SparkSession, *, table_fqn: str,
                        partition_keys: list[str], order_clause: str,
                        submodule: str) -> None:
    """
    Self-healing dedup: collapse rows that share the same composite key.
    Always preserves the manual / highest-priority row per group.
    """
    with tag_block(spark, module=MODULE, submodule=submodule):
        n_before = spark.sql(tagged_sql(
            f"SELECT COUNT(*) AS c FROM {table_fqn}",
            module=MODULE, submodule=submodule,
        )).collect()[0]["c"]
        if n_before == 0:
            return
        partition = ", ".join(partition_keys)
        spark.sql(tagged_sql(f"""
            INSERT OVERWRITE {table_fqn}
            SELECT * EXCEPT (rn) FROM (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY {partition}
                        ORDER BY {order_clause}
                    ) AS rn
                FROM {table_fqn}
            ) WHERE rn = 1
        """, module=MODULE, submodule=submodule))
        n_after = spark.sql(tagged_sql(
            f"SELECT COUNT(*) AS c FROM {table_fqn}",
            module=MODULE, submodule=submodule,
        )).collect()[0]["c"]
        removed = n_before - n_after
        if removed > 0:
            logger.warning(
                f"Self-healing dedup on {table_fqn}: "
                f"{n_before} -> {n_after} (removed {removed})"
            )


def _ensure_llm_target_tables(spark: SparkSession, *, catalog: str, gold: str) -> None:
    src_req_tbl = f"`{catalog}`.`{gold}`.`use_case_source_requirements`"
    uc_aff_tbl = f"`{catalog}`.`{gold}`.`use_case_affiliates`"

    with tag_block(spark, module=MODULE, submodule="llm_targets_ddl"):
        spark.sql(tagged_sql(f"""
            CREATE TABLE IF NOT EXISTS {src_req_tbl} (
                use_case_id        STRING NOT NULL COMMENT 'FK -> use_cases.id',
                required_canonical STRING NOT NULL COMMENT 'FK -> source_system_canonical.canonical, or "Unmapped"',
                necessity          STRING          COMMENT 'must_have | nice_to_have',
                data_need_excerpt  STRING          COMMENT 'Source phrase from data_requirements that triggered the mapping',
                confidence         STRING          COMMENT 'high | med | low',
                mapped_by          STRING          COMMENT 'llm | manual',
                is_user_edited     BOOLEAN,
                mapped_at          TIMESTAMP
            )
            USING DELTA
            COMMENT 'Use case -> required source systems. LLM-derived with closed-vocab prompt; analyst overrides preserved.'
        """, module=MODULE, submodule="llm_targets_ddl"))

        spark.sql(tagged_sql(f"""
            CREATE TABLE IF NOT EXISTS {uc_aff_tbl} (
                use_case_id    STRING NOT NULL COMMENT 'FK -> use_cases.id',
                affiliate_name STRING NOT NULL COMMENT 'FK -> affiliates.affiliate_name',
                applicability  STRING          COMMENT 'primary | secondary',
                rationale      STRING,
                mapped_by      STRING          COMMENT 'llm | manual',
                is_user_edited BOOLEAN,
                mapped_at      TIMESTAMP
            )
            USING DELTA
            COMMENT 'Use case -> applicable BHE affiliates. LLM-derived; analyst overrides preserved.'
        """, module=MODULE, submodule="llm_targets_ddl"))


def _build_prompt_template(canon: list[str], affiliates: list[str]) -> str:
    canon_list = "\n".join(f"- {c}" for c in canon)
    aff_list = "\n".join(f"- {a}" for a in affiliates)
    return (
        "You are a data governance assistant for Berkshire Hathaway Energy (BHE), "
        "an electric and gas utility holding company.\n\n"
        "TASK: Given a use case (name + description + data requirements), output a JSON object "
        "with TWO arrays:\n"
        "  1. required_sources: each item is "
        '{"canonical": <one of allowed sources OR "Unmapped">, '
        '"necessity": "must_have"|"nice_to_have", '
        '"excerpt": <the short data-requirement phrase that maps to this source>, '
        '"confidence": "high"|"med"|"low"}\n'
        "  2. applicable_affiliates: each item is "
        '{"affiliate": <one of allowed affiliates>, '
        '"applicability": "primary"|"secondary", '
        '"rationale": <1 short sentence>}\n\n'
        "RULES:\n"
        "- The `canonical` value MUST be exactly one of the strings in the ALLOWED SOURCES list, "
        'or the literal string "Unmapped" if no source matches.\n'
        "- The `affiliate` value MUST be exactly one of the strings in the ALLOWED AFFILIATES list. "
        'If the use case is BHE-wide or spans more than two affiliates, use "Multi-Affiliate".\n'
        "- Mark a source must_have if the use case cannot be delivered without it; otherwise nice_to_have.\n"
        "- Mark an affiliate primary if the use case is most directly relevant to that operating company.\n"
        "- Output minified JSON only. No markdown. No explanation.\n\n"
        "ALLOWED SOURCES:\n" + canon_list + "\n\n"
        "ALLOWED AFFILIATES:\n" + aff_list + "\n\n"
        "USE CASE:\n"
    )


def stage_llm_mapping(spark: SparkSession, *, catalog: str, silver: str,
                      gold: str, llm_endpoint: str, remap_unmapped: bool) -> None:
    uc_tbl = f"`{catalog}`.`{silver}`.`use_cases`"
    canon_tbl = f"`{catalog}`.`{gold}`.`source_system_canonical`"
    aff_tbl = f"`{catalog}`.`{gold}`.`affiliates`"
    src_req_tbl = f"`{catalog}`.`{gold}`.`use_case_source_requirements`"
    uc_aff_tbl = f"`{catalog}`.`{gold}`.`use_case_affiliates`"

    _ensure_llm_target_tables(spark, catalog=catalog, gold=gold)

    # Self-heal any existing duplicates in the target tables. Same composite-key
    # contract as the MERGE: (use_case_id, required_canonical) and
    # (use_case_id, affiliate_name). Manual + must_have rows win.
    _dedup_target_table(
        spark,
        table_fqn=src_req_tbl,
        partition_keys=["use_case_id", "required_canonical"],
        order_clause=(
            "CASE WHEN COALESCE(is_user_edited,false) THEN 0 ELSE 1 END, "
            "CASE WHEN mapped_by='manual' THEN 0 ELSE 1 END, "
            "CASE WHEN necessity='must_have' THEN 0 ELSE 1 END, "
            "CASE confidence WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, "
            "mapped_at DESC NULLS LAST"
        ),
        submodule="dedup_source_requirements",
    )
    _dedup_target_table(
        spark,
        table_fqn=uc_aff_tbl,
        partition_keys=["use_case_id", "affiliate_name"],
        order_clause=(
            "CASE WHEN COALESCE(is_user_edited,false) THEN 0 ELSE 1 END, "
            "CASE WHEN mapped_by='manual' THEN 0 ELSE 1 END, "
            "CASE WHEN applicability='primary' THEN 0 ELSE 1 END, "
            "mapped_at DESC NULLS LAST"
        ),
        submodule="dedup_use_case_affiliates",
    )

    with tag_block(spark, module=MODULE, submodule="llm_load_vocab"):
        canon = [
            r["canonical"] for r in spark.sql(tagged_sql(
                f"SELECT canonical FROM {canon_tbl} WHERE is_active = true "
                "ORDER BY canonical",
                module=MODULE, submodule="llm_load_vocab",
            )).collect()
        ]
        affiliates = [
            r["affiliate_name"] for r in spark.sql(tagged_sql(
                f"SELECT affiliate_name FROM {aff_tbl} WHERE is_active = true "
                "ORDER BY affiliate_name",
                module=MODULE, submodule="llm_load_vocab",
            )).collect()
        ]
    if not canon or not affiliates:
        logger.error("Stage 4: canonical or affiliate vocab is empty. "
                     "Run stages 1-2 first. Skipping LLM stage.")
        return
    logger.info(f"Loaded {len(canon)} canonical sources, {len(affiliates)} affiliates.")

    where_unresolved = (
        "uc.id NOT IN (SELECT DISTINCT use_case_id FROM " + src_req_tbl + ") "
        "OR uc.id NOT IN (SELECT DISTINCT use_case_id FROM " + uc_aff_tbl + ")"
    )
    if remap_unmapped:
        where_unresolved += (
            f" OR uc.id IN (SELECT use_case_id FROM {src_req_tbl} "
            "GROUP BY use_case_id "
            "HAVING MAX(CASE WHEN required_canonical = 'Unmapped' THEN 1 ELSE 0 END) = 1 "
            "AND COUNT(DISTINCT required_canonical) = 1)"
        )

    with tag_block(spark, module=MODULE, submodule="llm_extract_unresolved"):
        unresolved = spark.sql(tagged_sql(f"""
            SELECT
                uc.id                     AS use_case_id,
                COALESCE(uc.use_case_name, '') AS use_case_name,
                COALESCE(uc.description, '')   AS description,
                COALESCE(uc.data_requirements, '[]') AS data_requirements
            FROM {uc_tbl} uc
            WHERE COALESCE(uc.id, '') != ''
              AND ({where_unresolved})
        """, module=MODULE, submodule="llm_extract_unresolved")).collect()

    if not unresolved:
        logger.info("Stage 4: no unresolved use cases. Skipping LLM stage.")
        return
    logger.info(f"Stage 4: {len(unresolved)} use cases need LLM resolution.")

    prompt_template = _build_prompt_template(canon, affiliates)

    values = ",\n".join(
        f"({_sql_str(r['use_case_id'])}, "
        f"{_sql_str(r['use_case_name'])}, "
        f"{_sql_str(r['description'])}, "
        f"{_sql_str(r['data_requirements'])})"
        for r in unresolved
    )

    with tag_block(spark, module=MODULE, submodule="llm_ai_query"):
        t0 = time.time()
        spark.sql(tagged_sql(f"""
            CREATE OR REPLACE TEMPORARY VIEW _val_llm AS
            SELECT use_case_id,
                   ai_query(
                       {_sql_str(llm_endpoint)},
                       concat(
                           {_sql_str(prompt_template)},
                           'Name: ', use_case_name, '\\n',
                           'Description: ', description, '\\n',
                           'Data requirements: ', data_requirements
                       ),
                       failOnError => false,
                       modelParameters => named_struct(
                           'max_tokens', 1200,
                           'temperature', 0.0
                       )
                   ) AS resp
            FROM (
                SELECT * FROM (VALUES {values})
                AS v(use_case_id, use_case_name, description, data_requirements)
            )
        """, module=MODULE, submodule="llm_ai_query"))
        cnt = spark.sql(tagged_sql(
            "SELECT COUNT(*) AS c FROM _val_llm",
            module=MODULE, submodule="llm_ai_query_count",
        )).collect()[0]["c"]
        logger.info(f"LLM stage produced {cnt} responses in {time.time()-t0:.1f}s")

    canon_in_list = ",".join(_sql_str(c) for c in canon) + ",'Unmapped'"
    aff_in_list = ",".join(_sql_str(a) for a in affiliates)

    with tag_block(spark, module=MODULE, submodule="llm_parse"):
        spark.sql(tagged_sql(f"""
            CREATE OR REPLACE TEMPORARY VIEW _val_parsed AS
            WITH cleaned AS (
                SELECT use_case_id,
                       regexp_replace(
                           regexp_replace(resp.result, '^```\\\\w*\\\\n?', ''),
                           '\\\\n?```$', ''
                       ) AS clean_json
                FROM _val_llm
                WHERE resp.result IS NOT NULL
            )
            SELECT use_case_id,
                   from_json(TRIM(clean_json),
                             'required_sources ARRAY<STRUCT<canonical:STRING, necessity:STRING, excerpt:STRING, confidence:STRING>>, '
                             'applicable_affiliates ARRAY<STRUCT<affiliate:STRING, applicability:STRING, rationale:STRING>>'
                   ) AS p
            FROM cleaned
        """, module=MODULE, submodule="llm_parse"))

    with tag_block(spark, module=MODULE, submodule="llm_write_sources"):
        spark.sql(tagged_sql(f"""
            MERGE INTO {src_req_tbl} AS t
            USING (
                WITH exploded AS (
                    SELECT
                        use_case_id,
                        CASE
                            WHEN s.canonical IN ({canon_in_list}) THEN s.canonical
                            ELSE 'Unmapped'
                        END AS required_canonical,
                        CASE
                            WHEN lower(s.necessity) IN ('must_have','must','required') THEN 'must_have'
                            ELSE 'nice_to_have'
                        END AS necessity,
                        s.excerpt   AS data_need_excerpt,
                        COALESCE(s.confidence, 'med') AS confidence
                    FROM _val_parsed
                    LATERAL VIEW explode(COALESCE(p.required_sources, array())) AS s
                )
                SELECT use_case_id, required_canonical, necessity,
                       data_need_excerpt, confidence FROM (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY use_case_id, required_canonical
                            ORDER BY
                                CASE WHEN necessity='must_have' THEN 0 ELSE 1 END,
                                CASE confidence WHEN 'high' THEN 0
                                                WHEN 'med' THEN 1 ELSE 2 END
                        ) AS rn
                    FROM exploded
                ) WHERE rn = 1
            ) AS src
            ON t.use_case_id = src.use_case_id
               AND t.required_canonical = src.required_canonical
            WHEN MATCHED AND COALESCE(t.is_user_edited, false) = false
                          AND COALESCE(t.mapped_by, '') != 'manual' THEN
                UPDATE SET
                    t.necessity         = src.necessity,
                    t.data_need_excerpt = src.data_need_excerpt,
                    t.confidence        = src.confidence,
                    t.mapped_by         = 'llm',
                    t.mapped_at         = current_timestamp()
            WHEN NOT MATCHED THEN
                INSERT (use_case_id, required_canonical, necessity,
                        data_need_excerpt, confidence, mapped_by,
                        is_user_edited, mapped_at)
                VALUES (src.use_case_id, src.required_canonical, src.necessity,
                        src.data_need_excerpt, src.confidence, 'llm',
                        false, current_timestamp())
        """, module=MODULE, submodule="llm_write_sources"))

    with tag_block(spark, module=MODULE, submodule="llm_write_affiliates"):
        spark.sql(tagged_sql(f"""
            MERGE INTO {uc_aff_tbl} AS t
            USING (
                WITH exploded AS (
                    SELECT
                        use_case_id,
                        CASE
                            WHEN a.affiliate IN ({aff_in_list}) THEN a.affiliate
                            ELSE 'Multi-Affiliate'
                        END AS affiliate_name,
                        CASE
                            WHEN lower(a.applicability) = 'primary' THEN 'primary'
                            ELSE 'secondary'
                        END AS applicability,
                        a.rationale
                    FROM _val_parsed
                    LATERAL VIEW explode(COALESCE(p.applicable_affiliates, array())) AS a
                )
                SELECT use_case_id, affiliate_name, applicability, rationale FROM (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY use_case_id, affiliate_name
                            ORDER BY CASE WHEN applicability='primary' THEN 0 ELSE 1 END
                        ) AS rn
                    FROM exploded
                ) WHERE rn = 1
            ) AS src
            ON t.use_case_id = src.use_case_id
               AND t.affiliate_name = src.affiliate_name
            WHEN MATCHED AND COALESCE(t.is_user_edited, false) = false
                          AND COALESCE(t.mapped_by, '') != 'manual' THEN
                UPDATE SET
                    t.applicability = src.applicability,
                    t.rationale     = src.rationale,
                    t.mapped_by     = 'llm',
                    t.mapped_at     = current_timestamp()
            WHEN NOT MATCHED THEN
                INSERT (use_case_id, affiliate_name, applicability,
                        rationale, mapped_by, is_user_edited, mapped_at)
                VALUES (src.use_case_id, src.affiliate_name, src.applicability,
                        src.rationale, 'llm', false, current_timestamp())
        """, module=MODULE, submodule="llm_write_affiliates"))


# =====================================================================
# Stage 5: validate
# =====================================================================
def stage_validate(spark: SparkSession, *, catalog: str, silver: str, gold: str) -> None:
    uc_tbl = f"`{catalog}`.`{silver}`.`use_cases`"
    aff_tbl = f"`{catalog}`.`{gold}`.`affiliates`"
    pmap_tbl = f"`{catalog}`.`{gold}`.`program_affiliate_map`"
    src_req_tbl = f"`{catalog}`.`{gold}`.`use_case_source_requirements`"
    uc_aff_tbl = f"`{catalog}`.`{gold}`.`use_case_affiliates`"

    with tag_block(spark, module=MODULE, submodule="validate"):
        rows = spark.sql(tagged_sql(f"""
            SELECT
                (SELECT COUNT(*) FROM {uc_tbl})         AS use_cases,
                (SELECT COUNT(*) FROM {aff_tbl})        AS affiliates,
                (SELECT COUNT(*) FROM {pmap_tbl})       AS program_map_rows,
                (SELECT COUNT(*) FROM {src_req_tbl})    AS source_requirement_rows,
                (SELECT COUNT(DISTINCT use_case_id) FROM {src_req_tbl}) AS uc_with_sources,
                (SELECT COUNT(*) FROM {src_req_tbl}
                 WHERE required_canonical = 'Unmapped') AS unmapped_source_rows,
                (SELECT COUNT(*) FROM {uc_aff_tbl})     AS uc_affiliate_rows,
                (SELECT COUNT(DISTINCT use_case_id) FROM {uc_aff_tbl}) AS uc_with_affiliates
        """, module=MODULE, submodule="validate")).collect()
    r = rows[0].asDict() if rows else {}
    logger.info("=== Value model summary ===")
    for k, v in r.items():
        logger.info(f"  {k:30s} = {v}")


# =====================================================================
# Main
# =====================================================================
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="your_catalog")
    parser.add_argument("--silver-schema", default="bhe_silver")
    parser.add_argument("--gold-schema", default="bhe_gold")
    parser.add_argument("--affiliates-seed", default="")
    parser.add_argument("--program-map-seed", default="")
    parser.add_argument("--reseed", action="store_true",
                        help="(reserved) re-import seed CSVs; current behavior is "
                             "always-merge with is_user_edited preservation.")
    parser.add_argument("--remap-unmapped", action="store_true",
                        help="Re-evaluate use cases whose only source mapping is 'Unmapped'.")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--llm-endpoint", default=DEFAULT_LLM_ENDPOINT)
    args = parser.parse_args()

    aff_csv = _resolve_csv(args.affiliates_seed, "affiliates_seed.csv")
    pmap_csv = _resolve_csv(args.program_map_seed, "program_affiliate_map_seed.csv")

    spark = SparkSession.builder.appName("build_value_model").getOrCreate()

    logger.info(f"catalog={args.catalog} silver={args.silver_schema} gold={args.gold_schema}")
    logger.info(f"affiliates seed:    {aff_csv}")
    logger.info(f"program-map seed:   {pmap_csv}")
    logger.info(f"skip_llm={args.skip_llm} remap_unmapped={args.remap_unmapped}")

    stage_seed_affiliates(spark, catalog=args.catalog, gold=args.gold_schema,
                          seed_csv=aff_csv, reseed=args.reseed)
    stage_seed_program_map(spark, catalog=args.catalog, gold=args.gold_schema,
                           seed_csv=pmap_csv)

    if not args.skip_llm:
        stage_llm_mapping(spark, catalog=args.catalog, silver=args.silver_schema,
                          gold=args.gold_schema, llm_endpoint=args.llm_endpoint,
                          remap_unmapped=args.remap_unmapped)
    else:
        logger.info("Skipping LLM stage (--skip-llm).")

    stage_validate(spark, catalog=args.catalog, silver=args.silver_schema,
                   gold=args.gold_schema)


if __name__ == "__main__":
    main()
