# Generic-Company / Subdivision-Aware Research Plan

**Status:** Draft
**Owner:** TBD
**Last updated:** 2026-05-05
**Related:** `src/jobs/company_research.py`, `src/jobs/build_value_model.py`, `src/app/src/bhe_catalog/backend/router.py` (research orchestrator)

---

## 1. Problem

The catalog generates company-specific use cases from a company name, but the LLM is **never told about the company's operating subdivisions** before generating departments and use cases. The result:

- Use cases come back generic ("Predictive Maintenance", "Customer Churn Prediction") instead of subdivision-specific ("Predictive Maintenance for PacifiCorp transmission lines").
- Subdivision association is bolted on **after the fact** by `build_value_model.py` — a separate LLM pass that asks "which of these affiliates does this UC apply to?". Lossy, expensive, and produces low-confidence mappings.
- The seed lives in a hardcoded BHE CSV (`src/data/affiliates_seed.csv`). For any other company the table stays empty unless someone hand-edits it.
- The terminology is BHE-specific. Other companies use **divisions** (GE, Honeywell), **business units** (Berkshire), **operating companies** (Constellation), or **none at all** (a single-entity startup).

The catalog is supposed to work for any company you point it at. Today it really only works for BHE.

## 2. The three layers of customer-specific knowledge

Subdivision-aware research is one piece of a bigger deployment-customization story. The catalog has **three** separable forms of customer-specific data, and they all need to work for any company:

| Layer | Where | What it answers | BHE-specific? | Currently seeded? |
|---|---|---|---|---|
| **A. Platform filters** | Hardcoded in `bootstrap_tables.py` | "Drop `__databricks_*`, `system`, `samples`, `information_schema`, `default`" | No, universal | Yes |
| **B. Naming conventions** | `bhe_gold.classification_rules` table (6 categories) | "Given catalog `pacxedam_dev02_landing`, what's the program/env/zone/affiliate?" | Yes (patterns like `pacxedam` → PacifiCorp) | **No — table not even created** |
| **C. Business entities** | `src/data/*.csv` (affiliates, program→affiliate map, source-system canonical) | "Who are the operating units? What do they do?" | Yes | Partial (`build_value_model` job exists but isn't wired into the wizard) |

This plan addresses **B and C together** — they're both "what makes this customer different from BHE?". A is fine as-is.

The relationship between B and C is the key insight:
- B says *"`pacxedam_*` catalogs belong to the PacifiCorp subdivision"*.
- C says *"PacifiCorp is a multi-state electric utility serving 2M customers."*

Both must exist and stay consistent. For BHE, both are CSV-seeded. For any other customer, both must be either auto-discovered or empty-but-explicit.

## 3. Goals

1. **The LLM knows about subdivisions before generating use cases** (Layer C). Use cases come back pre-tagged with the subdivisions they apply to — no second LLM pass.
2. **Subdivision discovery is automatic** (Layer C). Given just a company name, research figures out whether the company has subdivisions, what they're called, and what they are.
3. **Naming-convention rules can be discovered from the actual catalogs** (Layer B). After data ingestion, an LLM pass proposes `classification_rules` based on the catalog/schema names actually present, cross-referenced with the discovered subdivisions.
4. **Universal "ignore" rules are pre-seeded** (Layer B). Every fresh deploy starts with `ignore_catalog: __databricks_*`, `ignore_schema: information_schema`, etc. — already hardcoded in bootstrap; just promote to the DB so users can edit/extend.
5. **Terminology is per-company.** "Affiliate" / "Division" / "Business Unit" / "Operating Company" — drives UI labels.
6. **Single-entity companies are first-class.** No fake "Default Affiliate" row.
7. **BHE deploys don't regress.** Existing CSV seeds keep working.

## 4. Non-goals

- Replacing the source-system canonical list (`source_system_canonical_seed.csv`). Same problem, separate (though analogous) plan.
- Letting users edit the subdivision list during research (it's a discovered artifact; edits happen in the Edit Center after).
- Backfilling already-generated use cases with subdivision tags. Re-running research is cheap; auto-migration isn't worth the complexity.

## 5. Design

### 5.1 New step in research: "Identify Subdivisions" (Layer C)

Insert as **Step 2** in `_run_company_research` (between Profile and Departments).

Two paths, decided at runtime:

| Trigger | Path | When |
|---|---|---|
| `src/data/affiliates_seed.csv` exists | **CSV seed** — load directly, skip LLM | BHE today |
| CSV missing or `--llm-discover` flag | **LLM discovery** — ask the LLM | Any other customer |

Both paths produce the same output schema, so downstream prompts don't care which fired.

**LLM discovery prompt** (added to `company_research.py`):

```
You have just researched: {company_name}
Company profile: {profile_json}

Identify this company's operating subdivisions — the named entities below
the corporate parent that run distinct businesses, geographies, or
regulatory portfolios. Use the company's own terminology.

Return JSON, no markdown fences:
{
  "has_subdivisions": true | false,
  "subdivision_label_singular": "Affiliate" | "Division" | "Business Unit"
                                | "Operating Company" | "Subsidiary" | ...,
  "subdivision_label_plural": "Affiliates" | "Divisions" | ...,
  "subdivisions": [
    {
      "name": "Canonical name as the company uses it",
      "code": "Short code if any, else empty",
      "business_type": "industry / function descriptor",
      "region": "Geographic footprint",
      "description": "1-2 sentence summary"
    }
  ]
}

If the company is a single entity with no meaningful subdivisions,
return has_subdivisions=false and an empty subdivisions array. DO NOT
fabricate divisions to fill the array.
```

### 5.2 Schema changes (Layer C)

Goal: support per-company terminology **without renaming the existing `affiliates` table** (avoids a Delta migration on existing BHE deploys).

Add three columns to `bhe_silver.company_profile`:

```sql
ALTER TABLE bhe_silver.company_profile ADD COLUMNS (
  subdivision_label_singular STRING COMMENT 'e.g. "Affiliate", "Division"',
  subdivision_label_plural   STRING COMMENT 'e.g. "Affiliates", "Divisions"',
  has_subdivisions           BOOLEAN COMMENT 'false = single-entity company'
);
```

Keep `bhe_gold.affiliates` as-is — the column names (`affiliate_name`, `affiliate_code`, etc.) become **internal identifiers only**. The user-facing label is whatever `company_profile.subdivision_label_*` says.

**For single-entity companies** (`has_subdivisions=false`): we still write **one row** to `bhe_gold.affiliates` named after the company itself, so downstream FKs stay clean. The UI hides subdivision filters / facets when `has_subdivisions=false`.

### 5.3 Subdivision-aware use-case generation (Layer C)

Modify `USE_CASES_PER_DEPT_PROMPT` in `company_research.py`:

```
... existing prompt ...

This company has the following operating {subdivision_label_plural}:
{subdivision_list_with_descriptions}

For EACH use case, identify which {subdivision_label_plural} it applies to.
Some use cases are corporate-wide (apply to all). Others are specific
(e.g. only the natural-gas pipeline subsidiary cares about pipeline
integrity monitoring).

Each element of the response array MUST include:
  ...existing fields...
  "applicable_subdivisions": ["Name 1", "Name 2"]  // canonical names
                                                    // from the list above
  "subdivision_applicability_rationale": "Why these and not others"
```

Where `{subdivision_list_with_descriptions}` is rendered from the Step 2 output.

**For single-entity companies**: the prompt skips the subdivision section entirely; `applicable_subdivisions` defaults to `[<company_name>]`.

### 5.4 Direct insert into `use_case_affiliates` (no LLM mapping job) (Layer C)

`build_value_model.py` Stage 4 currently runs a **second** LLM call to map use cases to affiliates. With subdivision tags arriving from research, this becomes a simple deterministic insert:

```sql
INSERT INTO bhe_gold.use_case_affiliates
SELECT
  uc.use_case_id,
  exploded.subdivision_name AS affiliate_name,
  'derived_from_research' AS applicability,
  uc.subdivision_applicability_rationale AS rationale,
  false AS is_user_edited,
  current_timestamp()
FROM bhe_silver.use_cases uc
LATERAL VIEW EXPLODE(uc.applicable_subdivisions) exploded AS subdivision_name
WHERE uc.use_case_id NOT IN (
  SELECT use_case_id FROM bhe_gold.use_case_affiliates WHERE is_user_edited = true
)
```

Stage 4 of `build_value_model.py` deletes its LLM-mapping code and replaces it with this insert. Source-system mapping (Stage 3) stays as-is — it's orthogonal to subdivisions.

### 5.5 UI labels driven by company profile (Layer C)

Add a tiny hook on the frontend:

```typescript
// src/app/src/bhe_catalog/ui/lib/use-subdivision-labels.ts
export function useSubdivisionLabels(): { singular: string; plural: string } {
  const { data: profile } = useQuery({
    queryKey: ["companyProfile"],
    queryFn: fetchCompanyProfile,
  });
  return {
    singular: profile?.subdivision_label_singular ?? "Affiliate",
    plural:   profile?.subdivision_label_plural ?? "Affiliates",
  };
}
```

Replace every hardcoded "Affiliate" / "Affiliates" string in the UI with this hook. Files affected (grep `affiliate` case-insensitive in `ui/`):

- `routes/_sidebar/edit.tsx` (Affiliates tab + Use Case→Affiliates section)
- `routes/_sidebar/value-readiness.tsx` (filter labels, KPI cards)
- `routes/_sidebar/company.tsx`
- `routes/_sidebar/about.tsx`
- `routes/_sidebar/source-systems.tsx`
- `components/sankey-diagram.tsx` (legend)

Schema/column names (`affiliate_name`, `affiliateCode`, etc.) stay — they're API-internal.

### 5.6 Single-entity gracefully (Layer C)

When `has_subdivisions = false`:

- Edit Center → hide the entire Affiliates tab.
- Value & Readiness → hide the affiliate filter.
- Sankey → drop the affiliate node layer (Source → Entity → UC → Department only).
- About page → omit the "by Affiliate" question.
- Backend → still write a single row for FK integrity, but mark it `is_default_subdivision = true`.

Add `is_default_subdivision` boolean to `bhe_gold.affiliates`, default false.

### 5.7 Wizard wiring (Layer C)

Step 7 (Enrichment Pipeline) currently has 4 cards. Add a 5th:

```
[5] Map Use Cases to Operating Units
    Run the value-model job: link each UC to source systems and subdivisions
    via the rules + LLM source mapping.
    Last run: ...
```

This triggers `bhe_build_value_model` via a new endpoint. With Stage 4 of that job replaced by the deterministic insert (§5.4), it's now a fast, predictable run.

### 5.8 Classification rules: create the table + seed universal ignore rules (Layer B)

Two bugs to fix immediately, regardless of whether subdivision-aware research ships:

1. **Add `classification_rules` to `_SETUP_GOLD_DDL`** in `router.py`:

   ```sql
   CREATE TABLE IF NOT EXISTS {fqn} (
     rule_id        STRING NOT NULL,
     category       STRING NOT NULL,  -- program | zone | environment | ignore_catalog | ignore_schema | federated_source
     pattern        STRING NOT NULL,  -- glob pattern
     label          STRING,           -- canonical name produced when pattern matches
     description    STRING,
     metadata       STRING,           -- JSON blob (e.g. affiliate name for program rules)
     is_active      BOOLEAN,
     display_order  INT,
     created_at     TIMESTAMP,
     updated_at     TIMESTAMP
   ) USING DELTA COMMENT 'User-editable parsing rules: catalog/schema names -> programs, environments, zones, ignore lists.'
   ```

2. **Seed universal ignore rules** at bootstrap-tables time, only if the table is empty:

   ```sql
   INSERT INTO classification_rules
   VALUES
     ('ig_dbx',   'ignore_catalog', '__databricks_internal_*', '', 'Databricks platform internals',  '{}', true, 10, ...),
     ('ig_sys',   'ignore_catalog', 'system',                   '', 'Databricks system catalog',     '{}', true, 11, ...),
     ('ig_smp',   'ignore_catalog', 'samples',                  '', 'Databricks sample data',        '{}', true, 12, ...),
     ('ig_inf',   'ignore_schema',  'information_schema',       '', 'INFORMATION_SCHEMA metadata',   '{}', true, 10, ...),
     ('ig_def',   'ignore_schema',  'default',                  '', 'Empty default schema',          '{}', true, 11, ...);
   ```

   These match the hardcoded filters in `bootstrap_tables.py` exactly. The hardcoded filters stay (defense in depth) but users can now see/edit/extend them.

This unblocks the Rules page for any deploy, BHE or otherwise, and is independent of subdivision-aware research. **Ship this in its own PR before Phase 2A.**

### 5.9 LLM-discovered classification rules (Layer B + C bridge)

Once both the subdivision list (§5.1) and the customer's actual catalogs are loaded (Wizard Step 6), a new optional pipeline pass can propose Layer B rules:

**Step 7 of wizard, new card "Discover Naming Rules":**

```
Inputs:
  - Distinct catalog names from silver_schemas
  - Distinct schema-name suffixes / prefixes
  - Subdivision list from research

Prompt:
  "Here's the company's actual catalog list: [pacxedam_dev02_landing,
   pacxedam_qa02_published, nveedl_dev_standardized, ...]
   Here are their operating units: [PacifiCorp, NV Energy, MidAmerican, ...]
   Propose classification rules:
     - program patterns (catalog prefix -> subdivision)
     - environment patterns (e.g. _dev02_, _qa02_)
     - zone patterns (suffix -> raw/silver/gold layer)
   For each, give a confidence score 0-1.
   Return JSON only."

Output:
  Inserts into classification_rules with is_active=false (proposed),
  surfaced in the Rules page with an "Approve" / "Reject" workflow.
```

This is real "auto-onboarding": user enters company name → research generates subdivisions → user ingests their catalogs → rules engine proposes the mapping → user approves with one click.

For BHE specifically, the proposed rules should match the curated CSVs (or the curated CSVs replace the proposals — they win on conflict).

### 5.10 What about source-system canonical (Layer C, source side)?

`source_system_canonical_seed.csv` is BHE-specific in the same way `affiliates_seed.csv` is. Same fix shape applies but is **explicitly out of scope for this plan** — the source-system list is consumed by AI table enrichment (which is its own complex pipeline) and changing its seeding semantics has wider blast radius. Track separately in a follow-up doc.

For now: BHE deploys keep using the CSV; non-BHE deploys get an empty list and AI enrichment falls back to free-text source-system labels (the existing graceful-degradation path).

## 6. Migration / compatibility

| Scenario | Behavior |
|---|---|
| BHE running CSV seed | Step 2 detects CSV, loads it, sets `subdivision_label = "Affiliate"`. No prompt change visible to BHE. |
| BHE re-runs research after upgrade | New use cases get pre-tagged; old use cases keep their `build_value_model`-mapped tags until re-research. |
| New customer (e.g. Constellation Energy) | Step 2 runs LLM discovery, sets label to "Operating Company", generates 5–10 subsidiaries. |
| Single-entity customer (small startup) | Step 2 returns `has_subdivisions=false`. UI hides subdivision concept entirely. |
| Existing deploy with `bhe_gold.affiliates` already populated | New `subdivision_label_*` columns default to "Affiliate"/"Affiliates" for back-compat. |

No destructive DDL. New columns are additive with safe defaults. No table rename. The `classification_rules` table only gets created if absent (Layer B fix is purely additive).

## 7. Implementation phases

### Phase 2-pre — Layer B fix (ship first, independently)
**Why first:** unblocks the Rules page for everyone, fixes a latent bug where a referenced table doesn't exist. No dependency on subdivision research. ~30 min.

1. Add `classification_rules` DDL to `_SETUP_GOLD_DDL` in `router.py`.
2. In `setup_bootstrap_tables()`, after table creation, run a one-time `INSERT INTO classification_rules ... WHERE NOT EXISTS` for the 5 universal ignore rules (§5.8).
3. Confirm Rules page renders with the seeded rows on a fresh deploy.

### Phase 2A — Layer C backend (subdivision-aware research)
1. ALTER `company_profile` to add `subdivision_label_singular`, `subdivision_label_plural`, `has_subdivisions`.
2. Add `_research_subdivisions` step to `_run_company_research` (CSV-or-LLM, §5.1).
3. Add subdivision context to `USE_CASES_PER_DEPT_PROMPT`; update response schema (§5.3).
4. Add `applicable_subdivisions`, `subdivision_applicability_rationale` columns to `bhe_silver.use_cases`.
5. Replace Stage 4 of `build_value_model.py` with deterministic insert (§5.4).
6. Add `/api/jobs/value-model/run` endpoint (triggers the bundle job).

**Verifies via:** Run research against a non-BHE company (e.g. "Duke Energy"). Confirm `bhe_gold.affiliates` populates with Duke's subsidiaries, `use_case_affiliates` populates from research output, labels say "Operating Company" or whatever Duke uses.

### Phase 2B — Frontend
7. `useSubdivisionLabels` hook + replace hardcoded "Affiliate"/"Affiliates" strings (§5.5).
8. Hide subdivision UI when `has_subdivisions=false` (§5.6).
9. Add 5th pipeline card "Map Use Cases to Operating Units" to wizard Step 7 (§5.7).
10. Update Sankey legend to use dynamic label.

**Verifies via:** Same Duke run looks correct end-to-end. Then point at a small single-entity company and confirm subdivision UI disappears entirely.

### Phase 2C — LLM-discovered naming rules (Layer B + C bridge)
11. Build "Discover Naming Rules" pass (§5.9): cross-references catalog list × subdivisions, proposes `classification_rules`, surfaces in Rules page with approve/reject.
12. Wire into wizard Step 7 as a 6th card.

**Verifies via:** ingest a non-BHE catalog list, confirm proposed rules align with subdivisions; approve them and confirm `silver_schemas.program/affiliate/zone/environment` populate correctly.

### Phase 2D — Cleanup
13. Backfill script for existing deploys (re-run subdivision identification only; no full re-research).
14. Update README with generic-company story; remove BHE-only framing.
15. Deprecate LLM-mapping docs in `build_value_model.py`.

## 8. Risks & open questions

- **Token budget per UC prompt**: adding subdivision context inflates the prompt. For a company with 30 subdivisions × 20 departments × 5 use cases each, could push us over context. **Mitigation:** subdivision list is just names + 1-line descriptions; ~10 extra lines is negligible. If it becomes a problem, pass top-N most relevant subdivisions per department.
- **LLM hallucinating subdivisions**: for a small private company the LLM might invent fake subsidiaries. **Mitigation:** prompt says "DO NOT fabricate." Add confidence field; flag low-confidence rows for human review in Edit Center.
- **LLM hallucinating naming rules**: same concern for Phase 2C. **Mitigation:** rules are inserted with `is_active=false` and require user approval before they take effect.
- **Override discovered terminology**: e.g. LLM says "Division" but user wants "BU". **Mitigation:** labels editable in Edit Center → Company Settings.
- **Source-system canonical list**: same problem, deferred (see §5.10).

## 9. What this plan does NOT change

- Schema/column names in `bhe_gold.affiliates`, `use_case_affiliates`, `program_affiliate_map`. They stay `affiliate_*` internally.
- The `build_value_model` job still exists and runs — just deterministic now and wired into the wizard.
- The `affiliates_seed.csv` file. Still works for BHE; just no longer required for everyone else.
- Any existing user-edited rows (`is_user_edited=true` preserved end-to-end).
- Hardcoded ignore filters in `bootstrap_tables.py` (kept as defense-in-depth even though they're now also in `classification_rules`).

## 10. Estimate

- **Phase 2-pre** (Layer B fix): ~30 min.
- **Phase 2A** (Layer C backend): ~3 hours (mostly prompt iteration + non-BHE verification).
- **Phase 2B** (frontend): ~2 hours (string replacement + conditional rendering).
- **Phase 2C** (rule discovery): ~3 hours (new endpoint + approve/reject UI).
- **Phase 2D** (cleanup): ~1 hour, deferrable.

Total: ~9.5 hours, but Phase 2-pre and 2A/2B can ship as separate PRs along the way. Phase 2C is genuinely incremental — the system works without it; users just need to add naming rules manually.
