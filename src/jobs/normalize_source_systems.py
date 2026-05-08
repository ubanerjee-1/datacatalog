"""
Source-system normalization workflow.

Background
----------
src/jobs/ai_enrich_tables.py uses ai_query() to populate
silver_tables.source_system. The LLM produces a free-text label per table,
which yields ~1,000 distinct values for what is realistically ~50 source
systems (e.g. "PI Historian", "OSIsoft PI Historian", "AVEVA PI" all refer
to the same system). That noise propagates into the gold glossary and
makes the Sankey unusable.

Pipeline (5 stages, each individually tagged + idempotent)
----------------------------------------------------------
1. derive_canonical (LLM)
   Generates source_system_canonical + the seed/alias_seed rows of
   source_system_aliases from (a) the company industry/profile and
   (b) the distinct raw values currently in silver_tables.source_system
   (refines the list as more data is enriched). Closes B-003 — the
   bundled `source_system_canonical_seed.csv` was retired in 2026-05-05.
   Manual edits (mapped_by='manual' or is_user_edited=true) are NEVER
   overwritten.

2. extract_unmapped
   Selects every distinct silver_tables.source_system that is not yet
   present in source_system_aliases. This bounds the LLM cost to NEW
   raw values only.

3. apply_deterministic
   For each unmapped raw value, try (in order):
     - exact match against canonical (case/whitespace insensitive)
     - exact match against alias.raw
     - normalized match (lower, strip parens, version suffixes)
   Successful matches are written to the alias table with
   mapped_by='exact' or 'normalized'.

4. apply_llm_fallback
   For the long tail still unmapped, run a SINGLE ai_query batch with
   the canonical list as a closed vocabulary in the prompt. Returns
   {canonical, confidence}. If confidence='low' or no canonical fits,
   maps to 'Other'. Written with mapped_by='llm'.

5. apply_to_silver
   Adds silver_tables.source_system_canonical column if missing,
   then MERGEs the alias mapping into silver_tables. Raw value is
   preserved; canonical is added alongside.

CLI
---
    --catalog                  default your_catalog
    --silver-schema            default bhe_silver
    --gold-schema              default bhe_gold
    --reseed-aliases           re-derive seed/alias_seed alias rows from
                                the LLM (manual edits always preserved)
    --remap-other              re-evaluate raws currently mapped to 'Other'
                                (use after expanding the canonical list)
    --skip-llm                 skip stage 4 (deterministic only). Stage 1
                                always runs because downstream stages
                                depend on the canonical list existing.
    --llm-endpoint             default databricks-claude-sonnet-4

Re-run safety
-------------
Idempotent. Manual edits (mapped_by='manual') are preserved across runs.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from pyspark.sql import SparkSession

# Sibling import for query-tag helpers (serverless-safe).
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
logger = logging.getLogger("normalize_source_systems")

MODULE = "normalize_source_systems"
DEFAULT_LLM_ENDPOINT = "databricks-claude-sonnet-4"


# =====================================================================
# Stage 1: derive canonical + aliases via LLM
#
# Closes B-003 (source_system_canonical_seed.csv portion). Replaces the
# bundled CSV seed path with an LLM-driven generator that uses:
#   (a) the company industry / business description from
#       bhe_silver.company_profile (always present after company
#       research), and
#   (b) the distinct raw values seen in bhe_silver.silver_tables (used
#       as a hint when present — empty on a fresh deploy, populated
#       after AI table enrichment runs at least once).
#
# Same is_user_edited / mapped_by='manual' preservation contract as the
# old CSV-seed flow: rerunning this stage NEVER overwrites a row a human
# touched in the Edit Center.
# =====================================================================
def _sql_str(s: str) -> str:
    return "'" + (s or "").replace("'", "''") + "'"


def _load_industry_context(spark: SparkSession, *, catalog: str,
                           silver: str) -> dict:
    """Pull a small profile blob from bhe_silver.company_profile.

    Returns {} when the table doesn't exist or is empty (greenfield
    deploy). The LLM falls back to a generic enterprise vocabulary
    in that case — still useful, just less targeted.

    Schema reference (must stay in sync with the company-research
    wizard's writer in router.py):
        id, company_name, industry, sub_industry, description,
        headquarters, key_business_units, strategic_priorities,
        regulatory_environment, catalog_name, logo_url, primary_domain,
        branding_user_edited
    """
    profile_tbl = f"`{catalog}`.`{silver}`.`company_profile`"
    try:
        rows = spark.sql(tagged_sql(f"""
            SELECT
                COALESCE(company_name, '')           AS company_name,
                COALESCE(industry, '')               AS industry,
                COALESCE(sub_industry, '')           AS sub_industry,
                COALESCE(description, '')            AS description,
                COALESCE(regulatory_environment, '') AS regulatory_environment
            FROM {profile_tbl}
            LIMIT 1
        """, module=MODULE, submodule="llm_canonical_profile")).collect()
        if not rows:
            return {}
        r = rows[0]
        return {
            "company_name": r["company_name"],
            "industry": r["industry"],
            "sub_industry": r["sub_industry"],
            "description": r["description"],
            "regulatory_environment": r["regulatory_environment"],
        }
    except Exception as e:
        logger.warning(f"Could not load company profile (using generic prompt): {e}")
        return {}


def _load_silver_raw_hints(spark: SparkSession, *, catalog: str,
                           silver: str, limit: int = 200) -> list[str]:
    """Pull up to `limit` distinct raw source_system values from silver_tables.

    Returns [] when the column doesn't exist yet (pre-enrichment) or
    silver_tables is empty (pre-ingest). This is a HINT to the LLM, not
    a hard input, so empty is fine.
    """
    silver_tbl = f"`{catalog}`.`{silver}`.`silver_tables`"
    try:
        rows = spark.sql(tagged_sql(f"""
            SELECT DISTINCT TRIM(source_system) AS raw
            FROM {silver_tbl}
            WHERE COALESCE(source_system, '') != ''
            ORDER BY raw
            LIMIT {int(limit)}
        """, module=MODULE, submodule="llm_canonical_hints")).collect()
        return [r["raw"] for r in rows if r["raw"]]
    except Exception as e:
        # Most common cause: silver_tables.source_system column missing
        # because ai_enrich_tables hasn't run yet on a fresh deploy.
        logger.info(f"No raw hints from silver_tables ({e}); LLM will rely on industry only.")
        return []


def _build_canonical_prompt(profile: dict, raw_hints: list[str]) -> str:
    """Build the prompt for ai_query()."""
    industry = profile.get("industry") or "general enterprise"
    sub_industry = profile.get("sub_industry") or ""
    company = profile.get("company_name") or "the customer"
    desc = profile.get("description") or ""
    regulatory = profile.get("regulatory_environment") or ""

    # Surface sub-industry in the industry line ("Energy / Electric Utility")
    # so the LLM keys on it. Without sub_industry, "Energy" alone could
    # produce O&G or renewables-only systems and miss CIS/SCADA/EAM.
    industry_line = industry + (f" ({sub_industry})" if sub_industry else "")

    hint_block = ""
    if raw_hints:
        sample = raw_hints[:80]
        hint_block = (
            "\n\nThe data catalog already contains the following distinct "
            "raw source-system labels (these are noisy LLM-generated labels "
            "from individual table inspection — use them as HINTS to ground "
            "the canonical list, but de-duplicate aggressively. e.g. "
            "'PI Historian', 'OSIsoft PI', 'AVEVA PI' are all the same "
            "canonical 'PI Historian'):\n- "
            + "\n- ".join(sample)
        )

    return (
        f"You are building the canonical source-system vocabulary for a data "
        f"catalog at {company}, a company in the {industry_line} industry.\n\n"
        f"Company description: {desc}\n"
        + (f"Regulatory environment: {regulatory}\n\n" if regulatory else "\n")
        + "Task: produce a list of 30-80 canonical source systems that this "
        "company most likely operates, including ERP, CRM, billing/CIS, EAM, "
        "historian/SCADA, GIS, document management, HRIS, financial planning, "
        "data warehouse, and other category-defining systems typical for the "
        "industry. Include common per-platform variants (e.g. 'SAP ECC' and "
        "'SAP S/4HANA' as separate canonicals)."
        + hint_block
        + "\n\nRules:\n"
        "1. Each canonical must be a SHORT, RECOGNIZABLE product name "
        "(e.g. 'SAP ECC', 'Maximo', 'PI Historian', 'Salesforce', "
        "'ServiceNow') — NOT a category description.\n"
        "2. Aliases are alternate spellings/marketing names/version "
        "suffixes for the same product (e.g. canonical='PI Historian' "
        "aliases=['OSIsoft PI', 'AVEVA PI', 'PI System']).\n"
        "3. Categories: pick from this closed list — "
        "{ERP, CRM, EAM, CIS, Historian, SCADA, GIS, HRIS, FP&A, "
        "Document, DataWarehouse, DataLake, BI, ITSM, MDM, Other}.\n"
        "4. Descriptions: 1 sentence, plain English.\n"
        "5. NO duplicates: each canonical MUST be unique.\n"
        "6. Return JSON ONLY, no prose, no fenced code block, no "
        "leading/trailing commentary.\n\n"
        "Return JSON with EXACTLY this shape (the top-level key MUST be "
        "\"canonicals\"; do NOT invent other keys):\n"
        "{\n"
        '  "canonicals": [\n'
        '    {"canonical": "SAP ECC", "category": "ERP", '
        '"description": "On-prem SAP ERP suite for finance, supply chain, and HR.", '
        '"aliases": ["SAP", "SAP R/3", "SAP ERP"]},\n'
        '    {"canonical": "Maximo", "category": "EAM", '
        '"description": "IBM enterprise asset management platform.", '
        '"aliases": ["IBM Maximo", "Maximo Asset Management"]}\n'
        "  ]\n"
        "}"
    )


def stage_llm_canonical(spark: SparkSession, *, catalog: str, silver: str,
                        gold: str, llm_endpoint: str,
                        reseed_aliases: bool) -> None:
    canonical_tbl = f"`{catalog}`.`{gold}`.`source_system_canonical`"
    alias_tbl = f"`{catalog}`.`{gold}`.`source_system_aliases`"

    with tag_block(spark, module=MODULE, submodule="seed_canonical_ddl"):
        spark.sql(tagged_sql(f"""
            CREATE TABLE IF NOT EXISTS {canonical_tbl} (
                canonical   STRING NOT NULL COMMENT 'Canonical source-system name (primary key)',
                category    STRING          COMMENT 'Category bucket (ERP, CIS, Historian, etc.)',
                description STRING          COMMENT 'Short human-readable description',
                is_active   BOOLEAN         COMMENT 'Soft-delete flag',
                created_at  TIMESTAMP,
                updated_at  TIMESTAMP
            )
            USING DELTA
            COMMENT 'Customer-editable canonical list of source systems. LLM-derived from company industry + observed raw values; manual edits survive subsequent re-runs.'
        """, module=MODULE, submodule="seed_canonical_ddl"))

        spark.sql(tagged_sql(f"""
            CREATE TABLE IF NOT EXISTS {alias_tbl} (
                raw            STRING NOT NULL COMMENT 'Source-system value as it appears in silver_tables (primary key)',
                raw_normalized STRING          COMMENT 'lower(trim(raw)) for case-insensitive matching',
                canonical      STRING          COMMENT 'Resolved canonical name (FK to source_system_canonical)',
                mapped_by      STRING          COMMENT 'seed | exact | normalized | alias_seed | llm | manual | fallback_other',
                confidence     STRING          COMMENT 'high | med | low | NULL',
                mapped_at      TIMESTAMP,
                is_user_edited BOOLEAN
            )
            USING DELTA
            COMMENT 'Persistent raw->canonical mapping. Idempotent: only re-resolves new raws. Manual edits (mapped_by=manual) are never overwritten by the job.'
        """, module=MODULE, submodule="seed_canonical_ddl"))

    # Self-healing dedup: enforce one row per raw_normalized in the alias
    # table. Prefer is_user_edited=true, then most-trusted mapped_by source,
    # then most recent mapped_at. Keeps the table queryable even if a prior
    # run inserted case-variant duplicates (e.g. 'Cosmos' vs 'COSMOS').
    with tag_block(spark, module=MODULE, submodule="dedup_aliases"):
        n_before_row = spark.sql(tagged_sql(
            f"SELECT COUNT(*) AS c FROM {alias_tbl}",
            module=MODULE, submodule="dedup_aliases",
        )).collect()
        n_before = n_before_row[0]["c"] if n_before_row else 0
        if n_before > 0:
            spark.sql(tagged_sql(f"""
                INSERT OVERWRITE {alias_tbl}
                SELECT raw, raw_normalized, canonical, mapped_by,
                       confidence, mapped_at, is_user_edited
                FROM (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY raw_normalized
                            ORDER BY
                                CASE WHEN is_user_edited THEN 0 ELSE 1 END,
                                CASE mapped_by
                                    WHEN 'manual'         THEN 1
                                    WHEN 'seed'           THEN 2
                                    WHEN 'alias_seed'     THEN 3
                                    WHEN 'exact'          THEN 4
                                    WHEN 'normalized'     THEN 5
                                    WHEN 'llm'            THEN 6
                                    WHEN 'fallback_other' THEN 7
                                    ELSE 8
                                END,
                                mapped_at DESC NULLS LAST
                        ) AS rn
                    FROM {alias_tbl}
                ) WHERE rn = 1
            """, module=MODULE, submodule="dedup_aliases"))
            n_after = spark.sql(tagged_sql(
                f"SELECT COUNT(*) AS c FROM {alias_tbl}",
                module=MODULE, submodule="dedup_aliases",
            )).collect()[0]["c"]
            removed = n_before - n_after
            if removed > 0:
                logger.warning(
                    f"Dedup pass removed {removed} duplicate alias rows "
                    f"({n_before} -> {n_after})."
                )

    # Short-circuit: if the canonical table already has rows AND we're
    # not in --reseed-aliases mode, skip the LLM call. The downstream
    # stages will use the existing list. Saves one LLM round-trip per
    # job run after first deploy.
    existing_canon = spark.sql(tagged_sql(
        f"SELECT COUNT(*) AS c FROM {canonical_tbl}",
        module=MODULE, submodule="seed_canonical_check",
    )).collect()[0]["c"]
    if existing_canon > 0 and not reseed_aliases:
        logger.info(f"Stage 1: canonical already has {existing_canon} rows; "
                    "skipping LLM (use --reseed-aliases to refresh).")
        return

    profile = _load_industry_context(spark, catalog=catalog, silver=silver)
    raw_hints = _load_silver_raw_hints(spark, catalog=catalog, silver=silver)
    prompt = _build_canonical_prompt(profile, raw_hints)
    response_schema = (
        "canonicals ARRAY<STRUCT<canonical:STRING, category:STRING, "
        "description:STRING, aliases:ARRAY<STRING>>>"
    )

    with tag_block(spark, module=MODULE, submodule="llm_canonical_query"):
        t0 = time.time()
        spark.sql(tagged_sql(f"""
            CREATE OR REPLACE TEMPORARY VIEW _canon_llm AS
            SELECT ai_query(
                       {_sql_str(llm_endpoint)},
                       {_sql_str(prompt)},
                       failOnError => false,
                       modelParameters => named_struct(
                           'max_tokens', 8000,
                           'temperature', 0.0
                       )
                   ) AS resp
        """, module=MODULE, submodule="llm_canonical_query"))
        logger.info(f"Stage 1: LLM canonical query finished in {time.time()-t0:.1f}s")

    parsed = spark.sql(tagged_sql(f"""
        WITH cleaned AS (
            SELECT regexp_replace(
                       regexp_replace(resp.result, '^```\\\\w*\\\\n?', ''),
                       '\\\\n?```$', ''
                   ) AS clean_json
            FROM _canon_llm
            WHERE resp.result IS NOT NULL
        )
        SELECT from_json(TRIM(clean_json), {_sql_str(response_schema)}) AS p
        FROM cleaned
    """, module=MODULE, submodule="llm_canonical_parse")).collect()

    if not parsed or parsed[0]["p"] is None:
        logger.error("Stage 1: LLM returned no parseable canonical list. "
                     "Aborting (downstream stages need this).")
        return

    raw_canonicals = parsed[0]["p"]["canonicals"] or []
    # Dedup canonicals + aliases by lowercased name. Canonical self-map
    # always wins; alias_seed loses to a canonical sharing the same key.
    seen_norm: set[str] = set()
    seed_rows: list[dict] = []
    for c in raw_canonicals:
        canonical = (c["canonical"] or "").strip()
        if not canonical:
            continue
        key = canonical.lower().strip()
        if key in seen_norm:
            continue
        seen_norm.add(key)
        seed_rows.append({
            "canonical": canonical,
            "category": (c["category"] or "Other").strip(),
            "description": (c["description"] or "").strip(),
            "aliases": [a.strip() for a in (c["aliases"] or []) if a and a.strip()],
        })

    if not seed_rows:
        logger.error("Stage 1: LLM returned 0 valid canonicals after dedup. "
                     "Aborting.")
        return
    logger.info(f"Stage 1: LLM produced {len(seed_rows)} canonicals "
                f"(industry={profile.get('industry') or 'unknown'}, "
                f"raw_hints={len(raw_hints)})")

    canonical_values = ",\n".join(
        f"({_sql_str(r['canonical'])}, {_sql_str(r['category'])}, "
        f"{_sql_str(r['description'])}, true, current_timestamp(), current_timestamp())"
        for r in seed_rows
    )
    with tag_block(spark, module=MODULE, submodule="seed_canonical_merge"):
        spark.sql(tagged_sql(f"""
            MERGE INTO {canonical_tbl} AS target
            USING (
                SELECT * FROM (
                    VALUES {canonical_values}
                ) AS v(canonical, category, description, is_active,
                       created_at, updated_at)
            ) AS src
            ON target.canonical = src.canonical
            WHEN MATCHED THEN UPDATE SET
                target.category    = src.category,
                target.description = src.description,
                target.is_active   = true,
                target.updated_at  = current_timestamp()
            WHEN NOT MATCHED THEN INSERT *
        """, module=MODULE, submodule="seed_canonical_merge"))

    # Self-map every canonical to itself (mapped_by='seed') and add
    # alias entries (mapped_by='alias_seed') from the LLM-emitted alias
    # arrays. Dedup by raw_normalized; canonical self-mapping wins ties.
    alias_rows: list[tuple[str, str, str]] = []
    seen_alias_norm: set[str] = set()
    for r in seed_rows:
        key = r["canonical"].lower().strip()
        if key not in seen_alias_norm:
            alias_rows.append((r["canonical"], r["canonical"], "seed"))
            seen_alias_norm.add(key)
    for r in seed_rows:
        for a in r["aliases"]:
            key = a.lower().strip()
            if not key or key in seen_alias_norm:
                continue
            alias_rows.append((a, r["canonical"], "alias_seed"))
            seen_alias_norm.add(key)

    if reseed_aliases:
        logger.info("--reseed-aliases set: overwriting non-manual alias rows.")
    alias_values = ",\n".join(
        f"({_sql_str(raw)}, {_sql_str(raw.lower().strip())}, "
        f"{_sql_str(canon)}, {_sql_str(by)}, "
        f"'high', current_timestamp(), false)"
        for raw, canon, by in alias_rows
    )
    # --reseed-aliases precedence: when set, the freshly-derived list is
    # source of truth for anything not manually edited. Overrides
    # llm/fallback_other mappings so a refreshed canonical list pulls
    # raws out of 'Other'. Manual edits (is_user_edited=true or
    # mapped_by='manual') are NEVER overwritten.
    update_clause = (
        "WHEN MATCHED AND target.is_user_edited = false "
        "AND target.mapped_by NOT IN ('manual') THEN UPDATE SET "
        "target.canonical = src.canonical, "
        "target.mapped_by = src.mapped_by, "
        "target.mapped_at = current_timestamp() "
        if reseed_aliases else ""
    )
    with tag_block(spark, module=MODULE, submodule="seed_aliases_merge"):
        spark.sql(tagged_sql(f"""
            MERGE INTO {alias_tbl} AS target
            USING (
                SELECT * FROM (
                    VALUES {alias_values}
                ) AS v(raw, raw_normalized, canonical, mapped_by,
                       confidence, mapped_at, is_user_edited)
            ) AS src
            ON target.raw_normalized = src.raw_normalized
            {update_clause}
            WHEN NOT MATCHED THEN INSERT *
        """, module=MODULE, submodule="seed_aliases_merge"))

    with tag_block(spark, module=MODULE, submodule="seed_canonical_count"):
        cnt = spark.sql(tagged_sql(
            f"SELECT COUNT(*) AS c FROM {canonical_tbl}",
            module=MODULE, submodule="seed_canonical_count",
        )).collect()[0]["c"]
        a_cnt = spark.sql(tagged_sql(
            f"SELECT COUNT(*) AS c FROM {alias_tbl}",
            module=MODULE, submodule="seed_canonical_count",
        )).collect()[0]["c"]
    logger.info(f"Stage 1 complete. canonical={cnt}, aliases={a_cnt}")


# =====================================================================
# Stages 2-3: extract unmapped + deterministic resolution
# =====================================================================
def stage_extract_and_deterministic(spark: SparkSession, *, catalog: str,
                                    silver: str, gold: str,
                                    remap_other: bool) -> int:
    silver_tbl = f"`{catalog}`.`{silver}`.`silver_tables`"
    canonical_tbl = f"`{catalog}`.`{gold}`.`source_system_canonical`"
    alias_tbl = f"`{catalog}`.`{gold}`.`source_system_aliases`"

    # Build the candidate set: distinct silver source_system values not
    # already mapped (or mapped to 'Other' if --remap-other).
    other_clause = "" if remap_other else (
        " AND (a.raw IS NULL)"
    )
    if remap_other:
        # When remapping Other, also include rows currently mapped to Other.
        other_clause = " AND (a.raw IS NULL OR a.canonical = 'Other')"

    with tag_block(spark, module=MODULE, submodule="extract_unmapped"):
        unmapped = spark.sql(tagged_sql(f"""
            WITH distinct_raws AS (
                -- Dedup by raw_normalized: case-different raws
                -- (e.g. 'Cosmos' vs 'COSMOS') collapse to one record so
                -- downstream MERGEs don't insert dup keys.
                SELECT raw, raw_normalized FROM (
                    SELECT TRIM(source_system) AS raw,
                           lower(TRIM(source_system)) AS raw_normalized,
                           ROW_NUMBER() OVER (
                               PARTITION BY lower(TRIM(source_system))
                               ORDER BY TRIM(source_system)
                           ) AS rn
                    FROM {silver_tbl}
                    WHERE COALESCE(source_system, '') != ''
                ) WHERE rn = 1
            )
            SELECT d.raw, d.raw_normalized
            FROM distinct_raws d
            LEFT JOIN {alias_tbl} a
                ON a.raw_normalized = d.raw_normalized
            WHERE 1=1 {other_clause}
        """, module=MODULE, submodule="extract_unmapped")).collect()

    logger.info(
        f"Stage 2 complete. {len(unmapped)} distinct raw values to resolve."
    )
    if not unmapped:
        return 0

    # Deterministic stage: try exact (already in alias table covers this),
    # then normalized match against canonical names. We do the normalized
    # match in one SQL pass so it scales without round-trips.
    raw_values = ",\n".join(
        f"({_sql_str(r['raw'])}, {_sql_str(r['raw_normalized'])})"
        for r in unmapped
    )

    with tag_block(spark, module=MODULE, submodule="apply_deterministic"):
        det_matches = spark.sql(tagged_sql(f"""
            WITH unmapped AS (
                SELECT * FROM (
                    VALUES {raw_values}
                ) AS v(raw, raw_normalized)
            ),
            canonical AS (
                SELECT canonical,
                       lower(trim(canonical)) AS canonical_norm,
                       -- aggressively normalized: strip parens, version
                       -- numbers, common suffixes for fuzzier matching.
                       regexp_replace(
                         regexp_replace(
                           regexp_replace(lower(trim(canonical)), '\\\\(.*?\\\\)', ''),
                           '\\\\b(system|database|db|platform|software|application|app)\\\\b',
                           ''
                         ),
                         '[\\\\s]+', ' '
                       ) AS canonical_fuzz
                FROM {canonical_tbl}
                WHERE is_active = true
            ),
            unmapped_norm AS (
                SELECT raw, raw_normalized,
                       regexp_replace(
                         regexp_replace(
                           regexp_replace(raw_normalized, '\\\\(.*?\\\\)', ''),
                           '\\\\b(system|database|db|platform|software|application|app)\\\\b',
                           ''
                         ),
                         '[\\\\s]+', ' '
                       ) AS raw_fuzz
                FROM unmapped
            )
            -- Dedup on raw_normalized: the fuzzy JOIN can produce N
            -- canonical matches per raw (e.g. "Oracle EBS" and
            -- "Oracle EBS Legacy" both fuzz to "oracle ebs"), and the
            -- downstream MERGE source must be unique per ON-key or
            -- Delta raises DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW.
            -- Tiebreaker: exact normalized match > fuzzy match,
            -- shorter canonical wins, alphabetical as final fallback.
            SELECT raw, raw_normalized, canonical, mapped_by, confidence
            FROM (
                SELECT u.raw, u.raw_normalized, c.canonical,
                       CASE WHEN u.raw_normalized = c.canonical_norm
                            THEN 'exact'
                            ELSE 'normalized'
                       END AS mapped_by,
                       'high' AS confidence,
                       ROW_NUMBER() OVER (
                           PARTITION BY u.raw_normalized
                           ORDER BY
                               CASE WHEN u.raw_normalized = c.canonical_norm
                                    THEN 0 ELSE 1 END,
                               LENGTH(c.canonical),
                               c.canonical
                       ) AS rn
                FROM unmapped_norm u
                JOIN canonical c
                    ON trim(u.raw_fuzz) = trim(c.canonical_fuzz)
            )
            WHERE rn = 1
        """, module=MODULE, submodule="apply_deterministic")).collect()

    logger.info(f"Stage 3 deterministic matches: {len(det_matches)}")

    if det_matches:
        det_values = ",\n".join(
            f"({_sql_str(m['raw'])}, {_sql_str(m['raw_normalized'])}, "
            f"{_sql_str(m['canonical'])}, {_sql_str(m['mapped_by'])}, "
            f"{_sql_str(m['confidence'])}, current_timestamp(), false)"
            for m in det_matches
        )
        with tag_block(spark, module=MODULE, submodule="write_deterministic"):
            spark.sql(tagged_sql(f"""
                MERGE INTO {alias_tbl} AS target
                USING (
                    SELECT * FROM (
                        VALUES {det_values}
                    ) AS v(raw, raw_normalized, canonical, mapped_by,
                           confidence, mapped_at, is_user_edited)
                ) AS src
                ON target.raw_normalized = src.raw_normalized
                WHEN MATCHED AND target.is_user_edited = false
                                AND target.mapped_by NOT IN ('manual') THEN
                    UPDATE SET
                        target.canonical  = src.canonical,
                        target.mapped_by  = src.mapped_by,
                        target.confidence = src.confidence,
                        target.mapped_at  = current_timestamp()
                WHEN NOT MATCHED THEN INSERT *
            """, module=MODULE, submodule="write_deterministic"))

    return len(unmapped) - len(det_matches)


# =====================================================================
# Stage 4: LLM fallback for the long tail
# =====================================================================
def _build_llm_prompt_expr(canonical_list: list[str]) -> str:
    """SQL expression that builds the per-row prompt for ai_query()."""
    canon_csv = ", ".join(_sql_str(c) for c in canonical_list)
    return (
        "concat(\n"
        "  'You are normalizing source-system labels for a utility-industry data catalog.\\n',\n"
        "  'Map the RAW label to the CLOSEST canonical source-system from the list, '\n"
        "  'or to ', _sql_str_other := \"'Other'\", \" if no canonical fits well.\"\n"
        ")"
    )


def _llm_prompt_template(canonical_list: list[str]) -> str:
    """Build the literal prompt string used inside ai_query()."""
    return (
        "You are normalizing source-system labels for a utility-industry "
        "data catalog. Map the RAW label below to the SINGLE closest "
        "canonical source-system from the list. If no canonical fits "
        "reasonably well, return \"Other\".\n\n"
        "Canonical list:\n- "
        + "\n- ".join(canonical_list)
        + "\n\nRAW label: "
    )


def stage_llm_fallback(spark: SparkSession, *, catalog: str, silver: str,
                       gold: str, llm_endpoint: str) -> None:
    silver_tbl = f"`{catalog}`.`{silver}`.`silver_tables`"
    canonical_tbl = f"`{catalog}`.`{gold}`.`source_system_canonical`"
    alias_tbl = f"`{catalog}`.`{gold}`.`source_system_aliases`"

    with tag_block(spark, module=MODULE, submodule="llm_load_canonical"):
        canon = [
            r["canonical"] for r in spark.sql(tagged_sql(
                f"SELECT canonical FROM {canonical_tbl} "
                "WHERE is_active = true ORDER BY canonical",
                module=MODULE, submodule="llm_load_canonical",
            )).collect()
        ]
    logger.info(f"Loaded {len(canon)} canonical names for LLM prompt.")

    # Pull only raws that are still unmapped after deterministic stage.
    # Dedup by raw_normalized: case-different raws (e.g. "Oracle" vs
    # "oracle") must collapse to one source row before the downstream
    # MERGE on raw_normalized — otherwise Delta raises
    # DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW. Same reason we do
    # this in stage_extract_and_deterministic and Stage 3.
    with tag_block(spark, module=MODULE, submodule="llm_extract_unmapped"):
        rows = spark.sql(tagged_sql(f"""
            WITH distinct_raws AS (
                SELECT raw, raw_normalized FROM (
                    SELECT TRIM(source_system) AS raw,
                           lower(TRIM(source_system)) AS raw_normalized,
                           ROW_NUMBER() OVER (
                               PARTITION BY lower(TRIM(source_system))
                               ORDER BY TRIM(source_system)
                           ) AS rn
                    FROM {silver_tbl}
                    WHERE COALESCE(source_system, '') != ''
                ) WHERE rn = 1
            )
            SELECT d.raw, d.raw_normalized
            FROM distinct_raws d
            LEFT JOIN {alias_tbl} a
                ON a.raw_normalized = d.raw_normalized
            WHERE a.raw IS NULL
        """, module=MODULE, submodule="llm_extract_unmapped")).collect()

    if not rows:
        logger.info("Stage 4: nothing left for LLM. Skipping.")
        return
    logger.info(f"Stage 4: {len(rows)} raw values for LLM resolution.")

    # Build a temp view of the unmapped raws, then call ai_query() in SQL
    # so Spark parallelizes the model serving calls.
    raw_values = ",\n".join(
        f"({_sql_str(r['raw'])}, {_sql_str(r['raw_normalized'])})"
        for r in rows
    )
    prompt_lit = _llm_prompt_template(canon)

    with tag_block(spark, module=MODULE, submodule="llm_ai_query"):
        t0 = time.time()
        spark.sql(tagged_sql(f"""
            CREATE OR REPLACE TEMPORARY VIEW _src_norm_llm AS
            SELECT raw, raw_normalized,
                   ai_query(
                       {_sql_str(llm_endpoint)},
                       concat({_sql_str(prompt_lit)}, raw,
                              '\\n\\nReply ONLY with valid JSON: {{"canonical": "<one of list or Other>", "confidence": "high"|"med"|"low"}}'),
                       failOnError => false,
                       modelParameters => named_struct(
                           'max_tokens', 64,
                           'temperature', 0.0
                       )
                   ) AS resp
            FROM (
                SELECT * FROM (VALUES {raw_values})
                AS v(raw, raw_normalized)
            )
        """, module=MODULE, submodule="llm_ai_query"))
        # Force materialization so we can time + count.
        cnt = spark.sql(tagged_sql(
            "SELECT COUNT(*) AS c FROM _src_norm_llm",
            module=MODULE, submodule="llm_ai_query_count",
        )).collect()[0]["c"]
        elapsed = time.time() - t0
    logger.info(
        f"LLM stage produced {cnt} rows in {elapsed:.1f}s "
        f"({cnt/max(elapsed,1):.1f} rows/sec)"
    )

    # Parse the JSON response and merge into alias table. Anything that
    # didn't parse, or whose canonical isn't in the canonical list, is
    # mapped to 'Other' with mapped_by='fallback_other'.
    canon_in_list = ",".join(_sql_str(c) for c in canon)
    with tag_block(spark, module=MODULE, submodule="llm_write_aliases"):
        spark.sql(tagged_sql(f"""
            MERGE INTO {alias_tbl} AS target
            USING (
                WITH cleaned AS (
                    SELECT raw, raw_normalized,
                           regexp_replace(
                               regexp_replace(resp.result, '^```\\\\w*\\\\n?', ''),
                               '\\\\n?```$', ''
                           ) AS clean_json
                    FROM _src_norm_llm
                ),
                parsed AS (
                    SELECT raw, raw_normalized,
                           from_json(TRIM(clean_json),
                                     'canonical STRING, confidence STRING') AS p
                    FROM cleaned
                ),
                shaped AS (
                    SELECT raw, raw_normalized,
                           CASE
                               WHEN p.canonical IN ({canon_in_list})
                                    THEN p.canonical
                               ELSE 'Other'
                           END AS canonical,
                           CASE
                               WHEN p.canonical IN ({canon_in_list})
                                    THEN 'llm'
                               ELSE 'fallback_other'
                           END AS mapped_by,
                           COALESCE(p.confidence, 'low') AS confidence,
                           current_timestamp() AS mapped_at,
                           false AS is_user_edited
                    FROM parsed
                )
                -- Defense-in-depth dedup on raw_normalized. The Stage 4
                -- input is now dedup'd upstream in llm_extract_unmapped,
                -- but we keep this filter so any future change that
                -- accidentally lets duplicates through doesn't crash
                -- the MERGE with DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW.
                -- Tiebreaker: prefer non-'Other' (= LLM picked a real
                -- canonical), then 'high' confidence over 'low'.
                SELECT raw, raw_normalized, canonical, mapped_by,
                       confidence, mapped_at, is_user_edited
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY raw_normalized
                               ORDER BY
                                   CASE WHEN canonical = 'Other' THEN 1 ELSE 0 END,
                                   CASE confidence
                                        WHEN 'high' THEN 0
                                        WHEN 'med'  THEN 1
                                        ELSE 2
                                   END,
                                   raw
                           ) AS rn
                    FROM shaped
                ) WHERE rn = 1
            ) AS src
            ON target.raw_normalized = src.raw_normalized
            WHEN MATCHED AND target.is_user_edited = false
                            AND target.mapped_by NOT IN ('manual') THEN
                UPDATE SET
                    target.canonical  = src.canonical,
                    target.mapped_by  = src.mapped_by,
                    target.confidence = src.confidence,
                    target.mapped_at  = current_timestamp()
            WHEN NOT MATCHED THEN INSERT *
        """, module=MODULE, submodule="llm_write_aliases"))


# =====================================================================
# Stage 5: apply alias mapping back to silver_tables
# =====================================================================
def stage_apply_to_silver(spark: SparkSession, *, catalog: str,
                          silver: str, gold: str) -> None:
    silver_tbl = f"`{catalog}`.`{silver}`.`silver_tables`"
    alias_tbl = f"`{catalog}`.`{gold}`.`source_system_aliases`"

    with tag_block(spark, module=MODULE, submodule="add_canonical_column"):
        cols = {
            r["col_name"].lower()
            for r in spark.sql(tagged_sql(
                f"DESCRIBE TABLE {silver_tbl}",
                module=MODULE, submodule="add_canonical_column",
            )).collect()
            if r["col_name"] and not r["col_name"].startswith("#")
        }
        if "source_system_canonical" not in cols:
            spark.sql(tagged_sql(
                f"ALTER TABLE {silver_tbl} ADD COLUMNS ("
                "source_system_canonical STRING "
                "COMMENT 'Normalized source-system from gold.source_system_aliases'"
                ")",
                module=MODULE, submodule="add_canonical_column",
            ))
            logger.info("Added source_system_canonical column to silver_tables.")
        else:
            logger.info("source_system_canonical column already exists.")

    with tag_block(spark, module=MODULE, submodule="merge_canonical_to_silver"):
        spark.sql(tagged_sql(f"""
            MERGE INTO {silver_tbl} AS t
            USING (
                SELECT raw, raw_normalized, canonical
                FROM {alias_tbl}
                WHERE canonical IS NOT NULL AND canonical != ''
            ) AS a
            ON lower(trim(t.source_system)) = a.raw_normalized
            WHEN MATCHED THEN UPDATE SET
                t.source_system_canonical = a.canonical
        """, module=MODULE, submodule="merge_canonical_to_silver"))

    with tag_block(spark, module=MODULE, submodule="silver_canonical_stats"):
        stats = spark.sql(tagged_sql(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN COALESCE(source_system,'') != '' THEN 1 ELSE 0 END) AS has_raw,
                SUM(CASE WHEN COALESCE(source_system_canonical,'') != '' THEN 1 ELSE 0 END) AS has_canon,
                COUNT(DISTINCT source_system_canonical) AS distinct_canon
            FROM {silver_tbl}
        """, module=MODULE, submodule="silver_canonical_stats")).collect()[0]
    logger.info(
        f"Stage 5 complete. silver_tables: total={stats['total']}, "
        f"raw_populated={stats['has_raw']}, canonical_populated={stats['has_canon']}, "
        f"distinct_canonical={stats['distinct_canon']}"
    )


# =====================================================================
# Driver
# =====================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Source-system normalization (canonical seed + alias cache + LLM fallback)"
    )
    parser.add_argument("--catalog", default="your_catalog")
    parser.add_argument("--silver-schema", default="bhe_silver")
    parser.add_argument("--gold-schema", default="bhe_gold")
    # `--seed-csv` retired in favor of LLM-driven canonical generation
    # (B-003 closure). Kept as a hidden no-op so existing job
    # configurations don't fail on `unrecognized argument` after a
    # bundle redeploy. New callers should drop the flag.
    parser.add_argument("--seed-csv", default="", help=argparse.SUPPRESS)
    parser.add_argument("--reseed-aliases", action="store_true",
                        help="Re-derive seed/alias_seed alias rows from the "
                             "LLM (manual edits always preserved). Use after "
                             "AI table enrichment has populated more raw "
                             "source-system labels and you want a tighter list.")
    parser.add_argument("--remap-other", action="store_true",
                        help="Re-evaluate raws currently mapped to 'Other'. "
                             "Use after expanding the canonical list.")
    parser.add_argument("--skip-llm", action="store_true",
                        help="Skip the long-tail LLM fallback (stage 4). "
                             "Stage 1 (canonical generation) always runs.")
    parser.add_argument("--llm-endpoint", default=DEFAULT_LLM_ENDPOINT)
    args = parser.parse_args()

    if args.seed_csv:
        logger.warning("--seed-csv is deprecated and ignored (B-003 closure). "
                       "source_system_canonical is now LLM-derived from "
                       "company_profile + silver_tables raw values.")

    spark = SparkSession.builder.getOrCreate()

    logger.info(f"Catalog/silver:  {args.catalog}.{args.silver_schema}")
    logger.info(f"Catalog/gold:    {args.catalog}.{args.gold_schema}")
    logger.info(f"LLM endpoint:    {args.llm_endpoint}")
    logger.info(f"Skip LLM:        {args.skip_llm}")
    logger.info(f"Remap 'Other':   {args.remap_other}")

    logger.info("=" * 60)
    logger.info("Stage 1: derive canonical + seed aliases via LLM")
    logger.info("=" * 60)
    stage_llm_canonical(
        spark,
        catalog=args.catalog,
        silver=args.silver_schema,
        gold=args.gold_schema,
        llm_endpoint=args.llm_endpoint,
        reseed_aliases=args.reseed_aliases,
    )

    logger.info("=" * 60)
    logger.info("Stages 2-3: extract unmapped + deterministic matching")
    logger.info("=" * 60)
    remaining = stage_extract_and_deterministic(
        spark,
        catalog=args.catalog,
        silver=args.silver_schema,
        gold=args.gold_schema,
        remap_other=args.remap_other,
    )
    logger.info(f"After deterministic: {remaining} raws still unresolved.")

    if not args.skip_llm:
        logger.info("=" * 60)
        logger.info("Stage 4: LLM fallback for long tail")
        logger.info("=" * 60)
        stage_llm_fallback(
            spark,
            catalog=args.catalog,
            silver=args.silver_schema,
            gold=args.gold_schema,
            llm_endpoint=args.llm_endpoint,
        )
    else:
        logger.info("Stage 4 skipped (--skip-llm).")

    logger.info("=" * 60)
    logger.info("Stage 5: apply mapping to silver_tables")
    logger.info("=" * 60)
    stage_apply_to_silver(
        spark,
        catalog=args.catalog,
        silver=args.silver_schema,
        gold=args.gold_schema,
    )

    logger.info("=" * 60)
    logger.info("Source-system normalization complete.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
