"""
Data Catalog - Company Intelligence Job

Takes a company name, uses LLM to:
1. Research the company profile
2. Generate departments (10-25)
3. Generate 3-10 high-value use cases PER department (with $ estimates)
4. Generate required data entities per use case
5. Generate Sankey mappings connecting sources -> entities -> use cases -> departments

Emits progress to job_progress table so the UI can show a live tree.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime

from pyspark.sql import SparkSession
from pyspark.sql.functions import col

try:
    _here = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _here = (
        os.path.dirname(os.path.abspath(sys.argv[0]))
        if sys.argv and sys.argv[0]
        else os.getcwd()
    )
sys.path.insert(0, _here)
from _query_tag import tag_block  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("company_research")

MODULE = "company_research"


def call_model_serving(ws_client, endpoint: str, system_prompt: str, user_msg: str,
                       max_tokens: int = 4000, request_timeout: int = 300) -> str:
    """Call a Databricks model-serving endpoint directly via HTTP.

    Uses ws_client.config.authenticate() to pick up runtime credentials
    (PAT, OAuth, notebook token, service principal) without relying on
    DATABRICKS_TOKEN env var. Using requests with explicit socket timeout
    is more reliable than the SDK wrapper, which can hang indefinitely
    on slow/stuck endpoints.
    """
    import requests

    host = (ws_client.config.host or "").rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    url = f"{host}/serving-endpoints/{endpoint}/invocations"
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }

    last_err = None
    resp = None
    for attempt in range(3):
        try:
            auth_headers = ws_client.config.authenticate()
            headers = {**auth_headers, "Content-Type": "application/json"}
            resp = requests.post(url, json=body, headers=headers, timeout=request_timeout)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            finish = choice.get("finish_reason", "")
            content = choice["message"]["content"]
            if finish == "length":
                logger.warning(
                    f"Model serving returned truncated response (finish_reason=length, "
                    f"max_tokens={max_tokens}, content_len={len(content)}). "
                    f"Consider increasing max_tokens."
                )
            return content
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            logger.warning(f"Model serving attempt {attempt + 1} failed: {e}. Retrying...")
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            last_err = e
            body_preview = resp.text[:500] if resp is not None else ""
            logger.error(f"Model serving attempt {attempt + 1} errored: {e} body={body_preview}")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Model serving failed after 3 attempts: {last_err}")


def parse_json_response(raw: str):
    """Parse LLM JSON output, with a fallback that recovers from mid-string truncation.

    If the raw string was cut off (model hit max_tokens), we try to salvage a valid
    prefix by locating the last complete object/element and closing brackets.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(
            f"JSON parse failed (len={len(cleaned)}): {e}. "
            f"Preview: {cleaned[:200]}... Tail: ...{cleaned[-200:]}"
        )
        recovered = _recover_truncated_json(cleaned)
        if recovered is not None:
            logger.info(f"Recovered {len(recovered) if isinstance(recovered, list) else 1} "
                        f"items from truncated JSON")
            return recovered
        raise


def _recover_truncated_json(s: str):
    """Best-effort recovery for truncated JSON arrays or objects.

    Trims back to the last well-formed element and closes the structure.
    """
    s = s.strip()
    if not s:
        return None

    if s.startswith("["):
        # Find the last complete top-level object inside the array.
        depth = 0
        in_str = False
        esc = False
        last_good = -1
        for i, ch in enumerate(s):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_good = i
        if last_good > 0:
            candidate = s[: last_good + 1] + "]"
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None
    elif s.startswith("{"):
        # Attempt to close any unterminated string then unclosed braces.
        depth = 0
        in_str = False
        esc = False
        for ch in s:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        tail = ""
        if in_str:
            tail += '"'
        tail += "}" * max(depth, 0)
        try:
            return json.loads(s + tail)
        except json.JSONDecodeError:
            return None
    return None


def emit_progress(spark, catalog: str, silver: str, run_id: str,
                  step: str, step_index: int, total_steps: int,
                  item_name: str = "", parent_item: str = "", detail: str = ""):
    """Write a progress row to job_progress so the frontend can track steps."""
    esc = lambda s: s.replace("'", "''")
    try:
        spark.sql(
            f"INSERT INTO `{catalog}`.`{silver}`.`job_progress` "
            f"(run_id, step, step_index, total_steps, item_name, parent_item, detail, created_at) "
            f"VALUES ('{esc(run_id)}', '{esc(step)}', {step_index}, {total_steps}, "
            f"'{esc(item_name)}', '{esc(parent_item)}', '{esc(detail)}', current_timestamp())"
        )
    except Exception as e:
        logger.warning(f"Failed to emit progress for step {step}: {e}")


COMPANY_PROFILE_PROMPT = """You are a business analyst specializing in enterprise organizations.
Research the given company and provide a comprehensive profile.
Respond ONLY with valid JSON, no markdown fences:
{
  "company_name": "Full legal name",
  "industry": "Primary industry",
  "sub_industry": "Sub-industry focus",
  "description": "2-3 sentence description of the company, its operations, and market position",
  "headquarters": "City, State/Country",
  "key_business_units": ["unit1", "unit2", ...],
  "strategic_priorities": ["priority1", "priority2", ...],
  "regulatory_environment": "Brief description of regulatory context and compliance requirements"
}"""

DEPARTMENTS_PROMPT = """You are a business analyst for large enterprises.
Given this company profile, generate the key departments that would exist.
Include both business departments AND technology/data departments.
Think about departments that generate data, consume analytics, and drive decisions.
Respond ONLY with valid JSON array, no markdown fences. Each element:
{
  "department_name": "Department Name",
  "description": "What this department does at this specific company",
  "key_functions": ["function1", "function2", "function3"],
  "data_needs": "What kind of data this department typically needs for analytics and decision-making"
}
Generate 15-25 departments covering the full organizational structure."""

USE_CASES_PER_DEPT_PROMPT = """You are a data strategy consultant specializing in enterprise analytics.
Given a company profile and a specific department, generate the TOP HIGH-VALUE analytics
and data use cases for that department on a modern data platform like Databricks.

For EACH use case, you MUST provide:
- A specific dollar-value estimate of annual business value (estimated_value_usd)
- A detailed rationale explaining how you arrived at that value estimate

Be specific to this company's industry, scale, and operations. Consider:
- Revenue generation, cost reduction, risk mitigation, efficiency gains
- Regulatory compliance savings, customer retention value
- Operational optimization, predictive maintenance savings

Respond ONLY with valid JSON array, no markdown fences. Each element:
{
  "use_case_name": "Descriptive Use Case Name",
  "description": "2-3 sentence description of what this use case delivers",
  "category": "One of: Predictive Analytics, Reporting & BI, ML/AI, Data Integration, Real-Time Monitoring, Regulatory Compliance, Customer Analytics, Operational Efficiency, Risk Management, Revenue Optimization",
  "business_value": "Brief description of business impact",
  "estimated_value_usd": 5000000,
  "value_rationale": "Detailed explanation: Based on [company]'s $X revenue, Y% improvement in Z yields $N annually because...",
  "data_requirements": ["specific data type 1", "specific data type 2"],
  "priority": "One of: High, Medium, Low"
}
Generate 3-10 use cases. Focus on the HIGHEST VALUE opportunities for this specific department."""

ENTITIES_PROMPT = """You are a data architect designing a modern data platform.
Given these use cases for a company, identify the data entities, domains, and systems
that would be needed to deliver on these use cases.

These should be GENERIC data entities — not tied to specific vendor products yet.
Examples: "Customer Master Data", "Billing Transactions", "Asset Registry",
"Work Order History", "Financial General Ledger", "Weather Observations",
"Grid Topology", "Meter Readings", "Employee Records".

For each entity, classify its type:
- "domain": A broad data domain (e.g., "Customer Data", "Financial Data")
- "entity": A specific data entity (e.g., "Customer Master", "Invoice Line Items")
- "system": A type of source system needed (e.g., "ERP System", "SCADA System")

Respond ONLY with valid JSON array, no markdown fences. Each element:
{
  "use_case_name": "Name of the use case this entity supports",
  "entity_name": "Name of the data entity/domain/system",
  "entity_type": "One of: domain, entity, system",
  "description": "What this data entity contains and why it's needed"
}
Generate 3-8 entities per use case. Be specific to the company's industry."""

SANKEY_MAPPING_PROMPT = """You are a data architect mapping data sources to business use cases.
Given the company's actual data sources (from their catalog) and the generated use cases
and required entities, create mappings showing the flow:
  Data Source -> Entity/Domain -> Use Case -> Department

DATA SOURCES (from catalog):
{sources}

USE CASES:
{use_cases}

REQUIRED ENTITIES:
{entities}

DEPARTMENTS:
{departments}

Create mappings connecting actual sources to entities to use cases to departments.
If a required entity has no matching source, still include it with source_system="UNMAPPED".
Respond ONLY with valid JSON array, no markdown fences. Each element:
{{
  "source_system": "Name of the actual data source from catalog, or UNMAPPED if no source exists",
  "source_category": "Category (e.g., ERP, CRM, SCADA, IoT, GIS, Internal, Unknown)",
  "entity_name": "Name of the data entity this maps through",
  "use_case": "Name of the use case it supports",
  "department": "Department that benefits",
  "relevance": "One of: Primary, Secondary, Supporting"
}}
Generate comprehensive mappings. Include UNMAPPED entries for gap analysis."""


ALL_STEPS = ["profile", "departments", "usecases", "entities", "sankey"]


def _table_has_rows(spark, catalog: str, silver: str, table: str, where: str = "1=1") -> bool:
    """Return True if the target Delta table exists and has any rows matching `where`."""
    try:
        cnt = spark.sql(
            f"SELECT count(*) AS c FROM `{catalog}`.`{silver}`.`{table}` WHERE {where}"
        ).first()["c"]
        return cnt > 0
    except Exception:
        return False


def _load_profile(spark, catalog: str, silver: str) -> dict:
    """Load the existing company profile as a dict."""
    row = spark.sql(
        f"SELECT * FROM `{catalog}`.`{silver}`.`company_profile` LIMIT 1"
    ).first()
    if row is None:
        return {}
    d = row.asDict()
    for key in ("key_business_units", "strategic_priorities"):
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                d[key] = []
    return d


def _load_departments(spark, catalog: str, silver: str) -> list[dict]:
    """Load existing departments as a list of dicts."""
    rows = spark.sql(
        f"SELECT * FROM `{catalog}`.`{silver}`.`departments`"
    ).collect()
    out = []
    for r in rows:
        d = r.asDict()
        if isinstance(d.get("key_functions"), str):
            try:
                d["key_functions"] = json.loads(d["key_functions"])
            except Exception:
                d["key_functions"] = []
        out.append(d)
    return out


def _load_use_cases(spark, catalog: str, silver: str) -> list[dict]:
    try:
        rows = spark.sql(
            f"SELECT * FROM `{catalog}`.`{silver}`.`use_cases`"
        ).collect()
    except Exception:
        return []
    return [r.asDict() for r in rows]


def _load_entities(spark, catalog: str, silver: str) -> list[dict]:
    try:
        rows = spark.sql(
            f"SELECT * FROM `{catalog}`.`{silver}`.`use_case_entities`"
        ).collect()
    except Exception:
        return []
    return [r.asDict() for r in rows]


def _append_rows(spark, rows: list[dict], catalog: str, silver: str, table: str):
    """Append a batch of dict rows to the target Delta table (mergeSchema)."""
    if not rows:
        return
    spark.createDataFrame(rows).write.mode("append").option(
        "mergeSchema", "true"
    ).saveAsTable(f"{catalog}.{silver}.{table}")


# ==================== Step functions ====================

def step_profile(spark, ws, endpoint, catalog, silver, company, run_id, now,
                 force, _progress, total_steps_hint):
    """Step 1 — returns the company profile dict."""
    if not force and _table_has_rows(spark, catalog, silver, "company_profile"):
        profile = _load_profile(spark, catalog, silver)
        if profile:
            logger.info("Step 1: Company profile already exists - skipping LLM call.")
            _progress("profile", 1, total_steps_hint, profile.get("company_name", company),
                      "", profile.get("industry", ""))
            return profile

    logger.info(f"Step 1: Researching company: {company}")
    profile_raw = call_model_serving(ws, endpoint, COMPANY_PROFILE_PROMPT,
                                     f"Company: {company}")
    profile = parse_json_response(profile_raw)

    profile_row = {
        "id": run_id,
        "company_name": profile.get("company_name", company),
        "industry": profile.get("industry", ""),
        "sub_industry": profile.get("sub_industry", ""),
        "description": profile.get("description", ""),
        "headquarters": profile.get("headquarters", ""),
        "key_business_units": json.dumps(profile.get("key_business_units", [])),
        "strategic_priorities": json.dumps(profile.get("strategic_priorities", [])),
        "regulatory_environment": profile.get("regulatory_environment", ""),
        "is_user_edited": False,
        "created_at": now,
    }
    spark.createDataFrame([profile_row]).write.mode("overwrite").option(
        "mergeSchema", "true"
    ).saveAsTable(f"{catalog}.{silver}.company_profile")
    logger.info("Company profile saved.")

    _progress("profile", 1, total_steps_hint, company, "", profile.get("industry", ""))
    return profile


def step_departments(spark, ws, endpoint, catalog, silver, company, profile, now,
                     force, _progress):
    """Step 2 — returns list of department dicts. Updates total_steps to N+4."""
    if not force and _table_has_rows(spark, catalog, silver, "departments"):
        departments = _load_departments(spark, catalog, silver)
        if departments:
            logger.info(f"Step 2: {len(departments)} departments already exist - skipping LLM call.")
            total_steps = len(departments) + 4
            dept_names = [d.get("department_name", "") for d in departments]
            _progress("departments", 2, total_steps, "", company, f"{len(departments)} departments")
            for dn in dept_names:
                _progress("dept_item", 2, total_steps, dn, company, "")
            return departments, total_steps

    logger.info("Step 2: Generating departments...")
    time.sleep(0.5)
    dept_raw = call_model_serving(
        ws, endpoint, DEPARTMENTS_PROMPT,
        f"Company: {company}\nProfile: {json.dumps(profile)}",
        max_tokens=8000,
    )
    departments = parse_json_response(dept_raw)

    dept_rows = []
    for dept in departments:
        dept_rows.append({
            "id": str(uuid.uuid4())[:8],
            "department_name": dept.get("department_name", ""),
            "description": dept.get("description", ""),
            "key_functions": json.dumps(dept.get("key_functions", [])),
            "data_needs": dept.get("data_needs", ""),
            "company_name": company,
            "is_user_edited": False,
            "created_at": now,
        })
    spark.createDataFrame(dept_rows).write.mode("overwrite").option(
        "mergeSchema", "true"
    ).saveAsTable(f"{catalog}.{silver}.departments")
    logger.info(f"Saved {len(dept_rows)} departments.")

    dept_names = [d["department_name"] for d in departments]
    total_steps = len(dept_names) + 4

    _progress("departments", 2, total_steps, "", company, f"{len(dept_names)} departments")
    for dn in dept_names:
        _progress("dept_item", 2, total_steps, dn, company, "")
    return departments, total_steps


def step_usecases(spark, ws, endpoint, catalog, silver, company, profile, departments,
                  now, force, _progress, total_steps):
    """Step 3 — per-department granularity. Only calls LLM for depts with no existing use cases.
    Returns the full list of use-case rows (existing + new)."""
    existing = _load_use_cases(spark, catalog, silver) if not force else []
    depts_with_ucs = {r.get("department", "") for r in existing if r.get("department")}

    logger.info(
        f"Step 3: {len(depts_with_ucs)}/{len(departments)} departments already have use cases"
    )

    new_rows = []
    for dept_idx, dept in enumerate(departments):
        dept_name = dept.get("department_name", "")
        step_index = 3 + dept_idx

        if dept_name in depts_with_ucs:
            existing_n = sum(1 for r in existing if r.get("department") == dept_name)
            _progress(f"usecase:{dept_name}", step_index, total_steps,
                      dept_name, company, f"{existing_n} use cases (existing)")
            continue

        logger.info(f"  Generating use cases for: {dept_name}")
        time.sleep(0.5)
        try:
            uc_raw = call_model_serving(
                ws, endpoint, USE_CASES_PER_DEPT_PROMPT,
                (
                    f"Company: {company}\n"
                    f"Industry: {profile.get('industry', '')}\n"
                    f"Company Description: {profile.get('description', '')}\n"
                    f"Department: {dept_name}\n"
                    f"Department Description: {dept.get('description', '')}\n"
                    f"Key Functions: {json.dumps(dept.get('key_functions', []))}\n"
                    f"Data Needs: {dept.get('data_needs', '')}"
                ),
                max_tokens=6000,
            )
            use_cases = parse_json_response(uc_raw)
        except Exception as e:
            logger.warning(f"  Failed for {dept_name}: {e}")
            _progress(f"usecase:{dept_name}", step_index, total_steps,
                      dept_name, company, f"failed: {str(e)[:60]}")
            continue

        dept_rows = []
        for uc in use_cases:
            dept_rows.append({
                "id": str(uuid.uuid4())[:8],
                "use_case_name": uc.get("use_case_name", ""),
                "description": uc.get("description", ""),
                "department": dept_name,
                "category": uc.get("category", ""),
                "business_value": uc.get("business_value", ""),
                "estimated_value_usd": float(uc.get("estimated_value_usd", 0)),
                "value_rationale": uc.get("value_rationale", ""),
                "data_requirements": json.dumps(uc.get("data_requirements", [])),
                "priority": uc.get("priority", "Medium"),
                "company_name": company,
                "is_user_edited": False,
                "created_at": now,
            })

        _append_rows(spark, dept_rows, catalog, silver, "use_cases")
        new_rows.extend(dept_rows)

        _progress(f"usecase:{dept_name}", step_index, total_steps,
                  dept_name, company, f"{len(dept_rows)} use cases")
        logger.info(f"  {dept_name}: {len(dept_rows)} use cases generated")

    all_rows = (existing or []) + new_rows
    logger.info(f"Total use cases: {len(all_rows)} ({len(new_rows)} new this run)")
    return all_rows


def step_entities(spark, ws, endpoint, catalog, silver, company, profile, all_uc_rows,
                  now, force, _progress, total_steps, n_depts):
    """Step 4 — per-batch granularity. Only calls LLM for batches whose UCs have no entities."""
    existing = _load_entities(spark, catalog, silver) if not force else []
    ucs_with_entities = {r.get("use_case_name", "") for r in existing if r.get("use_case_name")}

    uc_summaries = [
        {"use_case_name": r.get("use_case_name", ""),
         "department": r.get("department", ""),
         "description": r.get("description", ""),
         "data_requirements": r.get("data_requirements", "")}
        for r in all_uc_rows
    ]
    missing = [u for u in uc_summaries if u["use_case_name"] not in ucs_with_entities]
    logger.info(
        f"Step 4: {len(ucs_with_entities)} UCs already mapped to entities, "
        f"{len(missing)} UCs still need entity generation."
    )

    entities_step_index = 3 + n_depts
    batch_size = 30
    new_rows = []
    uc_id_map = {r.get("use_case_name", ""): r.get("id", "") for r in all_uc_rows}

    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        logger.info(f"  Entity batch {i // batch_size + 1}: {len(batch)} use cases")
        time.sleep(0.5)
        try:
            ent_raw = call_model_serving(
                ws, endpoint, ENTITIES_PROMPT,
                (
                    f"Company: {company}\n"
                    f"Industry: {profile.get('industry', '')}\n"
                    f"Use Cases:\n{json.dumps(batch, indent=2)}"
                ),
                max_tokens=8000,
            )
            entities = parse_json_response(ent_raw)
        except Exception as e:
            logger.warning(f"  Entity generation failed for batch: {e}")
            continue

        batch_rows = []
        for ent in entities:
            uc_name = ent.get("use_case_name", "")
            batch_rows.append({
                "entity_id": str(uuid.uuid4())[:8],
                "use_case_id": uc_id_map.get(uc_name, ""),
                "use_case_name": uc_name,
                "entity_name": ent.get("entity_name", ""),
                "entity_type": ent.get("entity_type", "entity"),
                "description": ent.get("description", ""),
                "is_matched": False,
                "matched_source": "",
                "company_name": company,
                "created_at": now,
            })
        _append_rows(spark, batch_rows, catalog, silver, "use_case_entities")
        new_rows.extend(batch_rows)

    all_rows = (existing or []) + new_rows
    logger.info(f"Total entities: {len(all_rows)} ({len(new_rows)} new this run)")
    _progress("entities", entities_step_index, total_steps, "", company,
              f"{len(all_rows)} entities")
    return all_rows


def step_sankey(spark, ws, endpoint, catalog, silver, company, departments,
                all_uc_rows, all_entity_rows, now, force, _progress, total_steps):
    """Step 5 — builds source -> entity -> use case -> department mappings."""
    if not force and _table_has_rows(spark, catalog, silver, "sankey_mappings"):
        try:
            existing_n = spark.sql(
                f"SELECT count(*) AS c FROM `{catalog}`.`{silver}`.`sankey_mappings`"
            ).first()["c"]
        except Exception:
            existing_n = 0
        logger.info(f"Step 5: sankey_mappings already has {existing_n} rows - skipping LLM call.")
        _progress("sankey", total_steps, total_steps, "", company,
                  f"{existing_n} mappings (existing)")
        return existing_n

    logger.info("Step 5: Generating Sankey mappings...")

    try:
        silver_schemas = spark.table(f"{catalog}.{silver}.silver_schemas")
        source_rows = (
            silver_schemas.filter(
                (col("classification") == "PRODUCTION")
                & (col("environment") != "SYSTEM")
            )
            .select("catalog_name", "schema_name", "suggested_domain",
                    "program", "business_friendly_name")
            .distinct()
            .collect()
        )
        sources_text = "\n".join(
            f"- {r.business_friendly_name or r.catalog_name + '.' + r.schema_name} "
            f"(Domain: {r.suggested_domain}, Program: {r.program})"
            for r in source_rows[:100]
        )
    except Exception:
        sources_text = "No catalog data available yet. Use generic source placeholders."

    dept_names = [d.get("department_name", "") for d in departments]
    uc_names = [r.get("use_case_name", "") for r in all_uc_rows]
    entity_names = list({r.get("entity_name", "") for r in all_entity_rows
                         if r.get("entity_name")})

    time.sleep(0.5)
    mapping_prompt = SANKEY_MAPPING_PROMPT.format(
        sources=sources_text,
        use_cases=json.dumps(uc_names[:80]),
        entities=json.dumps(entity_names[:100]),
        departments=json.dumps(dept_names),
    )

    try:
        mapping_raw = call_model_serving(ws, endpoint, mapping_prompt,
                                         f"Company: {company}", max_tokens=8000)
        mappings = parse_json_response(mapping_raw)
    except Exception as e:
        logger.warning(f"Sankey mapping generation failed: {e}")
        mappings = []

    mapping_rows = []
    for m in mappings:
        mapping_rows.append({
            "id": str(uuid.uuid4())[:8],
            "source_system": m.get("source_system", ""),
            "source_category": m.get("source_category", ""),
            "entity_name": m.get("entity_name", ""),
            "use_case": m.get("use_case", ""),
            "department": m.get("department", ""),
            "relevance": m.get("relevance", "Secondary"),
            "company_name": company,
            "is_user_edited": False,
            "created_at": now,
        })

    if mapping_rows:
        spark.createDataFrame(mapping_rows).write.mode("overwrite").option(
            "mergeSchema", "true"
        ).saveAsTable(f"{catalog}.{silver}.sankey_mappings")
    logger.info(f"Saved {len(mapping_rows)} Sankey mappings.")

    _progress("sankey", total_steps, total_steps, "", company,
              f"{len(mapping_rows)} mappings")
    return len(mapping_rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="your_catalog")
    parser.add_argument("--silver-schema", default="bhe_silver")
    parser.add_argument("--model-endpoint", default="databricks-claude-sonnet-4-6")
    parser.add_argument("--company-name", default="Berkshire Hathaway Energy")
    parser.add_argument("--run-id", default="",
                        help="Run ID for progress tracking (passed by the app).")
    parser.add_argument(
        "--steps", default=",".join(ALL_STEPS),
        help=f"Comma-separated list of steps to run. Choices: {ALL_STEPS}. "
             f"Upstream steps are auto-included when needed for data dependencies.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate output even when the target Delta table already has rows.",
    )
    args = parser.parse_args()

    spark = SparkSession.builder.getOrCreate()

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config
    ws = WorkspaceClient(config=Config(retry_timeout_seconds=1800))

    catalog = args.catalog
    silver = args.silver_schema
    endpoint = args.model_endpoint
    company = args.company_name

    requested_steps = {s.strip() for s in (args.steps or "").split(",") if s.strip()}
    unknown = requested_steps - set(ALL_STEPS)
    if unknown:
        raise ValueError(f"Unknown step(s): {unknown}. Valid: {ALL_STEPS}")
    if not requested_steps:
        requested_steps = set(ALL_STEPS)
    force = bool(args.force)

    run_id = args.run_id or str(uuid.uuid4())[:8]
    with tag_block(spark, module=MODULE, submodule="bootstrap", run_id=run_id, company=company):
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{silver}")
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS `{catalog}`.`{silver}`.`job_progress` ("
            "run_id STRING, step STRING, step_index INT, total_steps INT, "
            "item_name STRING, parent_item STRING, detail STRING, "
            "created_at TIMESTAMP) USING DELTA"
        )

    logger.info(f"Using run_id={run_id}")
    logger.info(f"Requested steps: {sorted(requested_steps)}  force={force}")
    now = datetime.utcnow().isoformat()

    with tag_block(spark, module=MODULE, submodule="reset_progress", run_id=run_id, company=company):
        spark.sql(
            f"DELETE FROM `{catalog}`.`{silver}`.`job_progress` "
            f"WHERE run_id = '{run_id}'"
        )

    total_steps_hint = 5  # refined once we know n_depts

    def _progress(step, step_index, total_steps, item_name="", parent_item="", detail=""):
        emit_progress(spark, catalog, silver, run_id,
                      step, step_index, total_steps, item_name, parent_item, detail)

    # Upstream dependencies are auto-included when needed in RAM but NOT re-generated
    # unless `--force` is set. Data dependencies are filled by reading Delta tables.

    # ----- Step 1: Profile -----
    profile = None
    if "profile" in requested_steps:
        with tag_block(spark, module=MODULE, submodule="step1_profile",
                       run_id=run_id, company=company):
            profile = step_profile(spark, ws, endpoint, catalog, silver, company, run_id,
                                   now, force, _progress, total_steps_hint)

    need_profile_for_downstream = any(
        s in requested_steps for s in ("departments", "usecases", "entities", "sankey")
    )
    if profile is None and need_profile_for_downstream:
        with tag_block(spark, module=MODULE, submodule="step1_profile_load",
                       run_id=run_id, company=company):
            profile = _load_profile(spark, catalog, silver)
        if not profile:
            raise RuntimeError(
                "Cannot run downstream steps: company_profile is empty. "
                "Run with --steps profile first or include 'profile' in --steps."
            )

    # ----- Step 2: Departments -----
    departments = None
    total_steps = total_steps_hint
    if "departments" in requested_steps:
        with tag_block(spark, module=MODULE, submodule="step2_departments",
                       run_id=run_id, company=company):
            departments, total_steps = step_departments(
                spark, ws, endpoint, catalog, silver, company, profile, now,
                force, _progress,
            )

    need_depts_for_downstream = any(
        s in requested_steps for s in ("usecases", "entities", "sankey")
    )
    if departments is None and need_depts_for_downstream:
        with tag_block(spark, module=MODULE, submodule="step2_departments_load",
                       run_id=run_id, company=company):
            departments = _load_departments(spark, catalog, silver)
        if not departments:
            raise RuntimeError(
                "Cannot run downstream steps: departments is empty. "
                "Run with --steps departments first."
            )
        total_steps = len(departments) + 4

    n_depts = len(departments) if departments else 0
    if total_steps == total_steps_hint and n_depts:
        total_steps = n_depts + 4
    try:
        with tag_block(spark, module=MODULE, submodule="update_total_steps",
                       run_id=run_id, company=company):
            spark.sql(
                f"UPDATE `{catalog}`.`{silver}`.`job_progress` "
                f"SET total_steps = {total_steps} "
                f"WHERE run_id = '{run_id}'"
            )
    except Exception:
        pass

    # ----- Step 3: Use Cases -----
    all_uc_rows = None
    if "usecases" in requested_steps:
        with tag_block(spark, module=MODULE, submodule="step3_usecases",
                       run_id=run_id, company=company):
            all_uc_rows = step_usecases(
                spark, ws, endpoint, catalog, silver, company, profile, departments,
                now, force, _progress, total_steps,
            )

    need_uc_for_downstream = any(s in requested_steps for s in ("entities", "sankey"))
    if all_uc_rows is None and need_uc_for_downstream:
        with tag_block(spark, module=MODULE, submodule="step3_usecases_load",
                       run_id=run_id, company=company):
            all_uc_rows = _load_use_cases(spark, catalog, silver)
        if not all_uc_rows:
            raise RuntimeError(
                "Cannot run downstream steps: use_cases is empty. "
                "Run with --steps usecases first."
            )

    # ----- Step 4: Entities -----
    all_entity_rows = None
    if "entities" in requested_steps:
        with tag_block(spark, module=MODULE, submodule="step4_entities",
                       run_id=run_id, company=company):
            all_entity_rows = step_entities(
                spark, ws, endpoint, catalog, silver, company, profile, all_uc_rows,
                now, force, _progress, total_steps, n_depts,
            )

    need_ent_for_downstream = "sankey" in requested_steps
    if all_entity_rows is None and need_ent_for_downstream:
        with tag_block(spark, module=MODULE, submodule="step4_entities_load",
                       run_id=run_id, company=company):
            all_entity_rows = _load_entities(spark, catalog, silver)

    # ----- Step 5: Sankey -----
    n_mappings = 0
    if "sankey" in requested_steps:
        with tag_block(spark, module=MODULE, submodule="step5_sankey",
                       run_id=run_id, company=company):
            n_mappings = step_sankey(
                spark, ws, endpoint, catalog, silver, company, departments,
                all_uc_rows, all_entity_rows or [], now, force, _progress, total_steps,
            )

    logger.info("=" * 60)
    logger.info("Company Intelligence Complete!")
    logger.info(f"  Company: {company}")
    logger.info(f"  Steps run: {sorted(requested_steps)}")
    logger.info(f"  Departments: {len(departments) if departments else 0}")
    logger.info(f"  Use Cases: {len(all_uc_rows) if all_uc_rows else 0}")
    logger.info(f"  Data Entities: {len(all_entity_rows) if all_entity_rows else 0}")
    logger.info(f"  Sankey Mappings: {n_mappings}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
