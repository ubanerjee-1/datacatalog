# Onboarding Bug Backlog

Living tracker of bugs found during the BHE deploy + first non-BHE onboarding (Gabe / FDM Dev workspace) on 2026-05-05. Items marked **FIXED** are already on `main`. Everything else is queued for the next iteration.

Cross-references:
- Architectural plan: [generic-company-onboarding.md](generic-company-onboarding.md)

---

## B-001 — `deploy.py` per-schema GRANTs fail because schemas don't exist

| | |
|---|---|
| **Severity** | High (blocks deploy) |
| **Status** | ✅ **FIXED** — commit `2a008e6` |
| **Where** | `scripts/deploy.py` |

**Symptom:** `Error: Schema 'fdm_dev02_analytics.bhe_silver' does not exist.`

**Root cause:** Chicken-and-egg between `deploy.py` (which grants on schemas) and the in-app Setup Wizard (which creates schemas). The wizard couldn't run until the app was reachable; the app couldn't usefully start without GRANTs.

**Fix:** Added `ensure_schemas_exist()` to `deploy.py`, runs `CREATE SCHEMA IF NOT EXISTS` via the SQL warehouse before granting.

---

## B-002 — `deploy.py` doesn't create the `uploads` Volume

| | |
|---|---|
| **Severity** | High (first upload fails with 404) |
| **Status** | ✅ **FIXED** — commit `6fe0fe6` |
| **Where** | `scripts/deploy.py` |

**Symptom:** First CSV / KB / logo upload returns 404 from the Files API.

**Root cause:** No code path created `/Volumes/{catalog}/{raw}/uploads`. Neither the deploy script nor the in-app wizard had `CREATE VOLUME` logic.

**Fix:** Added `ensure_volumes_exist()` to `deploy.py`. **Defense-in-depth follow-up:** also add to `setup_bootstrap_tables()` so the in-app path covers this too. *(See B-013.)*

---

## B-003 — Seed CSVs gitignored; jobs fail with `FileNotFoundError`

| | |
|---|---|
| **Severity** | High (blocks `BHE Value Model Build` and `BHE Source-System Normalization` jobs) |
| **Status** | ⏳ **PENDING** |
| **Where** | `src/data/*.csv`, `.gitignore` line 10 |

**Symptom:** 
```
FileNotFoundError: Could not locate affiliates_seed.csv in:
  /Workspace/.../src/data/affiliates_seed.csv,
  /Workspace/.../src/jobs/src/data/affiliates_seed.csv
```

**Root cause:** `*.csv` is gitignored globally (intentional — protects customer data from being committed). Three required seeds (`affiliates_seed.csv`, `program_affiliate_map_seed.csv`, `source_system_canonical_seed.csv`) live only on the original author's machine.

**Workaround for Gabe:** email the 3 CSVs out-of-band; he drops them in `src/data/` and re-runs `python scripts/deploy.py --skip-build --yes` (DABS `sync.include: src/data/**` then uploads them with the bundle).

**Permanent fix options** (pick one in next iteration):
1. **Move BHE seed CSVs out of the customer-installed repo** to a separate `bhe-seed-data` package or workspace volume. Ship empty stubs in `src/data/`. Job auto-detects empty stubs and either skips seeding or LLM-discovers (see [generic-company-onboarding.md §5.1](generic-company-onboarding.md)).
2. **Un-gitignore the BHE seeds specifically** with `!src/data/affiliates_seed.csv` etc. Fine for a private repo; bad if this ever goes public.
3. **Make the jobs tolerate missing seeds** — log a warning, skip seeding, let users edit in the Edit Center. Combined with B-005 (subdivision-aware research) this is the right end state.

**Recommended:** option 3 + the broader B-005 fix.

---

## B-004 — `bhe_gold.classification_rules` table never created or seeded

| | |
|---|---|
| **Severity** | **HIGH** (blocks "Populate Gold Layer") |
| **Status** | ✅ **FIXED** (Phase 1 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` (`_SETUP_GOLD_DDL` line ~385, `_seed_classification_rules_if_empty` ~line 6275) |

**Fix recap:** Added DDL to `_SETUP_GOLD_DDL`, defined `_CLASSIFICATION_RULES_SEED` with the 5 universal ignore patterns, added `_seed_classification_rules_if_empty()` helper called from `setup_bootstrap_tables`, and hardened `_load_rules()` with try/except that returns `{}` on missing table.

---

## B-005 — Subdivisions not known to the LLM during use-case generation

| | |
|---|---|
| **Severity** | High (use cases generic; affiliate mapping is lossy post-hoc LLM pass) |
| **Status** | ⏳ **PENDING** (full plan: [generic-company-onboarding.md](generic-company-onboarding.md)) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` (`_run_company_research`), `src/jobs/build_value_model.py` (Stage 4) |

**Symptom:** Use cases come back generic ("Predictive Maintenance") instead of subdivision-aware ("Predictive Maintenance for PacifiCorp transmission lines"). Affiliate associations require a second LLM pass that's expensive and lossy.

**Root cause:** `_run_company_research` has zero affiliate context in any of its prompts. `build_value_model.py` Stage 4 bolts on a post-hoc LLM mapping. The whole architecture treats affiliates as a separate concern instead of a research input.

**Fix:** Phased rewrite — see [generic-company-onboarding.md](generic-company-onboarding.md) Phases 2A → 2B → 2C. ~5 hours of focused work split across several PRs.

---

## B-006 — Wizard "Resume Research (1 step left)" button is a lie

| | |
|---|---|
| **Severity** | Medium (UX confusion + wasted LLM budget) |
| **Status** | 🟡 **PARTIAL FIX** — short-circuit when complete (Phase 7, 2026-05-05); per-step skip still pending |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `_run_company_research` (~line 5125), `trigger_company_research` (~line 6155) |

**What's fixed:**
- Trigger now passes `steps` and `force_flag` to the inline runner (was previously dropped on the floor — `args=(run_id, body.company_name)`).
- `_run_company_research` accepts `steps` + `force` parameters and honors them at function entry.
- Added an "all-requested-steps-complete" short-circuit at the top of the function: if the user clicks Resume after everything is already populated, the function returns immediately instead of re-burning 5+ minutes of LLM calls.

**What's still pending:**
- Per-step skipping when only a *subset* is missing (e.g. user resumes after sankey failed mid-run). Requires wrapping each of the 5 step bodies in `if _should_run(step, table):` guards — the helper exists, but the existing monolithic step bodies need to be re-indented (deferred to avoid bundling a risky refactor with the other circular-dep fixes).
- For the common case (research run completes cleanly with the entities step now implemented in B-007), the partial fix is sufficient.

**Follow-up effort:** ~45 min to wrap the 5 step bodies + load `dept_names` / `all_uc_names` from DB after their respective steps so a partial-resume can pick up mid-flow.

---

## B-007 — `entities` step declared in `ALL_RESEARCH_STEPS` but never implemented

| | |
|---|---|
| **Severity** | High (every research run perpetually reported "1 step left") |
| **Status** | ✅ **FIXED** (Phase 6 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `_run_company_research` Step 5 (~line 5435) |

**Fix recap:** Added a "Step 5: Use-case Entities derived from Sankey" block after the existing sankey insert. Flattens `sankey_mappings` rows into `use_case_entities` via `INSERT INTO ... SELECT` joining `silver.use_cases` to resolve `use_case_id`. `is_matched` is true when the entity has a non-UNMAPPED, non-empty source_system in any of its sankey arcs. Updated `total_steps = n_depts + 4` (was `n_depts + 3`).

**Symptom:** Status endpoint always reports `state="partial"`, `missing_steps=["entities"]`. UI forever shows "1 step left" with the (broken — see B-006) Resume button. `bhe_silver.use_case_entities` table is empty after every fresh research run.

**Root cause:** `ALL_RESEARCH_STEPS = ["profile", "departments", "usecases", "entities", "sankey"]` declares 5 steps, but the worker only implements 4 (profile / departments / use_cases / sankey). The only `INSERT INTO use_case_entities` in the entire codebase is the manual Edit Center endpoint at line 4197.

The data does exist — entity names are populated as a column in `sankey_mappings`. They're just never denormalized into `use_case_entities`.

**Fix options:**
1. **Quick (5 min):** drop `"entities"` from `ALL_RESEARCH_STEPS`. Status endpoint stops looking for it. `use_case_entities` stays empty (only manual entries via Edit Center).
2. **Real (~30 min):** add an entities-derivation step right after sankey:

   ```python
   execute_query(f"""
       INSERT INTO {fqn(silver, 'use_case_entities')}
           (entity_id, use_case_id, use_case_name, entity_name,
            entity_type, description, is_matched, matched_source)
       SELECT
           concat('uce_', substr(uuid(), 1, 12)),
           uc.id,
           sm.use_case,
           sm.entity_name,
           'entity',
           '',
           sm.source_system != 'UNMAPPED',
           CASE WHEN sm.source_system != 'UNMAPPED' THEN sm.source_system ELSE NULL END
       FROM {fqn(silver, 'sankey_mappings')} sm
       LEFT JOIN {fqn(silver, 'use_cases')} uc ON uc.use_case_name = sm.use_case
       WHERE sm.entity_name IS NOT NULL AND sm.entity_name != ''
       GROUP BY sm.use_case, sm.entity_name, sm.source_system, uc.id
   """)
   ```

**Recommended:** option 2 — `use_case_entities` becomes a useful, editable table instead of a dead one.

---

## B-008 — Three deployed jobs are orphaned (no UI trigger)

| | |
|---|---|
| **Severity** | Medium (users have to manually trigger from Workflows) |
| **Status** | ⏳ **PENDING** |
| **Where** | `src/app/src/bhe_catalog/ui/routes/_sidebar/company.tsx` (Step 7 cards) |

**Symptom:** After deploy, the wizard exposes 4 enrichment cards but 3 deployed Databricks jobs have no UI button: `BHE Value Model Build`, `BHE Source-System Normalization`, `BHE Glossary Builder`. Users discover them only by going to the Workflows UI directly.

**Root cause:** Only `BHE AI Table Enrichment` and `BHE Company Research` got endpoints. The pattern exists (`triggerTableEnrichJob` in `router.py` line 5475, ~30 lines) but wasn't replicated for the other three.

**Fix:** ~2 hours total
1. Three new endpoints:
   - `POST /jobs/value-model/run`
   - `POST /jobs/normalize-sources/run`
   - `POST /jobs/glossary/run`
2. Three status endpoints mirroring `/jobs/enrich-tables/status`.
3. Three new `PipelineJobCard` components in wizard Step 7, sequenced as cards 5/6/7 in this strict order:
   - **5. Source-System Normalization** (creates `source_system_canonical`)
   - **6. Value Model Build** (depends on 5 — see B-014)
   - **7. Glossary Builder** (optional, depends on 5 + 6)
4. Extend "Run All (Sequential)" to chain cards 5/6 too — this makes B-014's hidden dependency invisible to the user.

**Full pipeline order this enforces** (top-down through the wizard):

```
Step 5  Company Research  →  silver.use_cases
Step 6  Data ingestion    →  silver.silver_schemas / silver_tables
Step 7
  card 1  Populate Gold        →  gold.schema_inventory etc.
  card 2  AI Enrich Schemas    →  gold.schema_inventory descriptions
  card 3  AI Enrich Tables     →  silver_tables.source_system (free text)
  card 4  Generate Taxonomy    →  gold.schema_taxonomy
  card 5  Source-System Norm.  →  gold.source_system_canonical + classified silver_tables  ◄── NEW
  card 6  Value Model Build    →  gold.affiliates + use_case_affiliates + use_case_source_requirements  ◄── NEW
  card 7  Glossary Builder     →  gold.glossary_system_domain                                ◄── NEW
```

Pure additive — no breaking changes.

---

## B-009 — `_active_runs` lives in process memory only

| | |
|---|---|
| **Severity** | Medium (FastAPI restart loses run state; UI may show stuck "Researching…") |
| **Status** | ⏳ **PENDING** |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` (`_active_runs` global dict) |

**Symptom:** If the app restarts (deploy, scale event, crash) while a research run is mid-flight, the run state vanishes from the dict. The UI's `activeResearch` query never resolves to "completed" because the run_id is gone.

**Root cause:** `_active_runs: dict[str, dict] = {}` is a module-level Python dict. No persistence.

**Fix:** ~1 hour
1. Persist run state to `bhe_silver.job_runs` (mirrors what `bhe_silver.job_progress` already does for granular progress).
2. On startup, reconcile any `RUNNING` rows older than ~10 min as `FAILED` (orphaned).
3. Active-run lookups query the table instead of the dict.

Alternative: switch all in-process jobs to bundled Databricks jobs (which have durable run history via the Jobs API). Bigger lift but cleaner long-term.

---

## B-010 — `python3` not on PATH on Windows

| | |
|---|---|
| **Severity** | Low (Windows-only deploy friction) |
| **Status** | ✅ **FIXED** — commit `9a41762` |
| **Where** | `scripts/deploy.py` `ensure_tools()` |

**Fix recap:** Replaced `shutil.which("python3")` with `sys.version_info` so the running interpreter's version is checked directly regardless of OS.

---

## B-011 — `npx vite build` false-fails on Windows

| | |
|---|---|
| **Severity** | Low (Windows-only deploy friction) |
| **Status** | ✅ **FIXED** — commit `c5d214b` |
| **Where** | `scripts/deploy.py` |

**Fix recap:** `_resolve_exe()` resolves `.cmd` shims; `vite build` runs with `check=False` and we validate `__dist__/` artifacts post-execution.

---

## B-012 — `npm install` not auto-run on first build

| | |
|---|---|
| **Severity** | Low (deploy friction; user gets confusing vite error) |
| **Status** | ✅ **FIXED** — commit `093acfa` |
| **Where** | `scripts/deploy.py` |

**Fix recap:** Auto-runs `npm install` if `node_modules/` is missing before `vite build`.

---

## B-015 — `bhe_gold.schema_taxonomy` table never created

| | |
|---|---|
| **Severity** | **HIGH** (blocks Generate Taxonomy + analytics endpoints) |
| **Status** | ✅ **FIXED** (Phase 1 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` (`_SETUP_GOLD_DDL` ~line 405) |

**Fix recap:** Added the `schema_taxonomy` DDL to `_SETUP_GOLD_DDL` so the wizard creates it on bootstrap. Audit also caught 7 other gold tables that were created lazily by jobs (affiliates, program_affiliate_map, use_case_source_requirements, use_case_affiliates, source_system_canonical, source_system_aliases) — all 8 now declared in `_SETUP_GOLD_DDL` so they exist before any UI page tries to read them.

**Symptoms:**
- Wizard Step 7 → "Generate Taxonomy" job fails with `TABLE_OR_VIEW_NOT_FOUND ... schema_taxonomy`.
- Continuous noisy errors in app logs from `/api/jobs/pipeline-status` polling (handled gracefully via try/except, but spammy).
- Schema Explorer page renders empty.

**Root cause:** Table is referenced 13 times in `router.py` (read by analytics endpoints, written by Generate Taxonomy job) but **no DDL exists anywhere** — not in `_SETUP_GOLD_DDL`, not in the job code, not in `bootstrap_tables.py`. `_run_taxonomy_generation` goes straight to `SELECT FROM {tax}` and `INSERT INTO {tax}` with no `CREATE TABLE IF NOT EXISTS` first.

**Fix:** ~10 minutes — add to `_SETUP_GOLD_DDL`:

```sql
CREATE TABLE IF NOT EXISTS {fqn} (
    taxonomy_id    STRING,
    schema_key     STRING NOT NULL,
    dimension      STRING NOT NULL,
    value          STRING,
    source         STRING,
    confidence     FLOAT,
    ai_reasoning   STRING,
    effective_from TIMESTAMP,
    effective_to   TIMESTAMP,
    created_by     STRING,
    created_at     TIMESTAMP
) USING DELTA
COMMENT 'AI-classified taxonomy dimensions per schema (8 dimensions, SCD Type 2).'
```

Bundle this with B-004 in the same PR — same class of bug, same fix shape, same urgency.

**Manual SQL workaround** (run alongside the B-004 SQL):

```sql
CREATE TABLE IF NOT EXISTS <catalog>.bhe_gold.schema_taxonomy (
  taxonomy_id    STRING,
  schema_key     STRING NOT NULL,
  dimension      STRING NOT NULL,
  value          STRING,
  source         STRING,
  confidence     FLOAT,
  ai_reasoning   STRING,
  effective_from TIMESTAMP,
  effective_to   TIMESTAMP,
  created_by     STRING,
  created_at     TIMESTAMP
) USING DELTA;
```

**Audit completed 2026-05-05** (sweep of all `fqn(gold, ...)` / `fqn(silver, ...)` references):

**Silver tables — all accounted for** ✅
- `_SETUP_SILVER_DDL` covers 11 tables.
- `silver_schemas` / `silver_tables` CTAS'd by `bootstrap_tables.py`.
- `silver_artifacts` lazy-created by `_ensure_artifacts_table()`.

**Gold tables — 8 referenced but missing from `_SETUP_GOLD_DDL`:**

| Table | Created by today | Recommended action |
|---|---|---|
| `classification_rules` | nothing (B-004) | Add to `_SETUP_GOLD_DDL` + seed universal ignore rules |
| `schema_taxonomy` | nothing (B-015) | Add to `_SETUP_GOLD_DDL` |
| `affiliates` | `build_value_model` job (Stage 1) | Add to `_SETUP_GOLD_DDL` (job's CREATE-IF-NOT-EXISTS becomes no-op) |
| `program_affiliate_map` | `build_value_model` job (Stage 2) | Same |
| `use_case_source_requirements` | `build_value_model` job (Stage 4) | Same |
| `use_case_affiliates` | `build_value_model` job (Stage 4) | Same |
| `source_system_canonical` | `normalize_source_systems` job | Same |
| `source_system_aliases` | `normalize_source_systems` job | Same |

**The 6 "created by job" tables** are technically OK in the happy path, but multiple Edit Center / Source Systems / Value & Readiness pages query them eagerly. Pre-creating them in `_SETUP_GOLD_DDL` removes a class of "click-edit-page-before-job-ran → 500" bugs. The jobs' DDL becomes a no-op (intentional).

**Combined fix scope:** ~1 hour
1. Add 8 entries to `_SETUP_GOLD_DDL`.
2. Add seed-rule INSERT for `classification_rules` after table creation in `setup_bootstrap_tables()`.
3. Wrap `_load_rules()` in try/except returning `{}` on missing table (defense-in-depth).
4. Spot-check that the build_value_model and normalize_source_systems jobs still run cleanly when the tables already exist.

---

## B-014 — Hidden dependency order between bundled jobs

| | |
|---|---|
| **Severity** | Medium (job fails with cryptic `TABLE_OR_VIEW_NOT_FOUND` if run out of order) |
| **Status** | ⏳ **PENDING** |
| **Where** | `src/jobs/build_value_model.py` Stage 4, `src/jobs/normalize_source_systems.py` |

**Symptom:** Running `BHE Value Model Build` before `BHE Source-System Normalization` fails with:

```
[TABLE_OR_VIEW_NOT_FOUND] The table or view
`<catalog>`.`bhe_gold`.`source_system_canonical` cannot be found.
```

**Root cause:** `build_value_model.py` Stage 4 reads `bhe_gold.source_system_canonical` as the closed vocabulary for its LLM mapping, but doesn't create it. That table is created by `normalize_source_systems.py` from the seed CSV. Nothing — neither the README, the wizard, nor the job descriptions — surfaces this dependency.

**Fix options:**
1. **Quick (~10 min):** make `build_value_model.py` Stage 4 tolerate a missing table — if `source_system_canonical` doesn't exist, log a warning and pass an empty vocabulary to the LLM (mappings just come back as `Unmapped`, which the schema already supports).
2. **Real (~1 h):** wire job dependencies via a multi-task Databricks job, OR (better) implement B-008 properly by sequencing the three orphan jobs in the wizard's "Run All" so order is enforced from the UI.

**Recommended:** option 2 wrapped into B-008. Once the wizard runs the three jobs in the correct order, the dependency is invisible to the user.

---

## B-017 — Read paths assume `silver_tables.source_system*` columns exist before normalize job has run

| | |
|---|---|
| **Severity** | High (Schema Explorer / Source Systems page error out before user has a chance to run any job) |
| **Status** | ✅ **FIXED** (Phases 2 + 4 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `_ensure_silver_tables_columns()` (~line 1561), `_SETUP_SILVER_DDL.silver_tables` (~line 271), refactored `ingest_tables` MERGE |

**Fix recap:** Two-layer fix:
1. Defensive `_ensure_silver_tables_columns()` helper modeled on `_ensure_use_case_status_columns()` — DESCRIBE+ALTER to add `source_system` / `source_system_canonical` if missing. Wired into the 3 read endpoints (`analytics_schema_tables`, `list_source_systems`, `source_system_detail`).
2. Added the columns to the base DDL of `silver_tables` in `_SETUP_SILVER_DDL` so the wizard creates them up-front on every fresh deploy (eliminates the bug at the source going forward).
3. Refactored `ingest_tables` from DROP+CTAS to MERGE so re-uploading a fresh CSV no longer drops the columns (resolves the broader Circular dep B at the same time).
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `analytics_schema_tables` (line 6736), `list_source_systems` (line 1465+), `source_system_detail` (line 1606+); column owner `src/jobs/normalize_source_systems.py` |
| **Reported** | 2026-05-05 (Gabe — `[UNRESOLVED_COLUMN.WITH_SUGGESTION] st.source_system cannot be resolved`) |

**Symptom:** Clicking into a schema in Schema Explorer (or visiting Source Systems) returns `RuntimeError: SQL execution failed: [UNRESOLVED_COLUMN.WITH_SUGGESTION] A column ... 'st.source_system' cannot be resolved`. Page is unusable until the user runs the `BHE Source-System Normalization` job manually.

**Root cause (latent, two-stage table evolution):**
- `bootstrap_tables.py` CTAS creates `silver_tables` from the schema-extractor CSV with a fixed column set (`table_catalog`, `table_schema`, `table_name`, `table_type`, `table_owner`, `comment`, `created`, `last_altered`, `data_source_format`, `classification`, `ai_definition`, `business_friendly_name`, `is_user_edited`, `user_edited_at`). **No `source_system`, no `source_system_canonical`.**
- `normalize_source_systems.py` job lazily ALTER-ADDs `source_system` and `source_system_canonical` to `silver_tables` on first run — same DESCRIBE+ALTER pattern as `_ensure_use_case_status_columns()`.
- Three read endpoints (`analytics_schema_tables`, `list_source_systems`, `source_system_detail`) query these columns unconditionally, with no defensive ALTER and no error handling. Any user who clicks Schema Explorer **before** running the normalize job sees a 500.
- Same hidden-dependency family as **B-014** (Value Model needed Normalize first); same root cause as **B-004 / B-015** (table state assumed but not enforced upfront).

**Fix (~30 min, structural — add to next iteration):**
1. Add `_ensure_silver_tables_columns()` modeled on the existing `_ensure_use_case_status_columns()` pattern (`router.py:1378-1423`):
   - DESCRIBE TABLE silver_tables
   - If `source_system` / `source_system_canonical` not in column set, ALTER TABLE ADD COLUMNS
   - Cache via module-level `_SILVER_TABLES_COLUMNS_READY = False` flag
   - Wrap in try/except so a transient failure doesn't keep retrying
2. Call it at the top of every endpoint that touches these columns: `analytics_schema_tables`, `list_source_systems`, `source_system_detail`, plus any other `st.source_system` references the audit turns up.
3. Better yet, fold this into the silver-side bootstrap so columns exist from day-one (CTAS + immediate ALTER, or modify the CTAS to project NULL columns).

**Workaround for Gabe (now):**

Option A — run `BHE Source-System Normalization` job once (it ALTERs the table for free).

Option B — one-time SQL:
```sql
ALTER TABLE apm_dev02_published.bhe_silver.silver_tables ADD COLUMNS (
  source_system            STRING COMMENT 'Raw source-system value (added by normalize job)',
  source_system_canonical  STRING COMMENT 'Resolved canonical (added by normalize job)'
);
```

Columns are NULL until normalize runs, but the page renders.

**Audit next:** grep for other ALTER-only columns referenced by read paths. Suspect candidates: `silver_tables.ai_definition`, `silver_tables.business_friendly_name` (these ARE in the CTAS, so probably OK), `silver_schemas.affiliate` (added by populate-gold? need to verify), and anything else added via `_ensure_*_columns` patterns.

---

## B-016 — BI & AI Artifacts page has no "New" button for manual entry

| | |
|---|---|
| **Severity** | Medium (workflow gap; users can't add a single artifact without writing a CSV) |
| **Status** | ⏳ Pending |
| **Where** | `src/app/src/bhe_catalog/ui/routes/_sidebar/artifacts.tsx` (lines ~265-278); `src/app/src/bhe_catalog/backend/router.py` `silver_artifacts` endpoints |
| **Reported** | 2026-05-05 (Gabe — screenshot of empty Artifacts page) |

**Symptom:** The BI & AI Artifacts page exposes only "Enrich with AI" and "Upload CSV" buttons. There is no way to create a single artifact entry from the UI — a user wanting to register one new dashboard / report has to construct a CSV with the right header schema and upload it.

**Root cause:**
- Frontend: only `Upload CSV` and `Enrich with AI` buttons in the page header. No "New artifact" action.
- Backend: there is `PUT /artifacts/{id}` (update existing) and `DELETE /artifacts/{id}` and `POST /ingest/artifacts` (CSV bulk), but **no `POST /artifacts` for creating a single record**. The `_ensure_artifacts_table()` helper exists, so the table is fine — only the create-one path is missing.

**Fix (~1 hour):**
1. Backend: add `POST /artifacts` accepting an `ArtifactCreateIn` model (subset of editable columns: name, type, platform, owner, business_team, status, certified, data_domain, department, description, location, etc.). Generate `artifact_id` via the existing `_artifact_id(name, platform, location)` helper so duplicates collapse, set `last_modified=current_timestamp()`, INSERT into `silver_artifacts`. Return the new row.
2. Frontend: add a "New" button next to Upload CSV that opens a dialog with the editable fields (mirror the existing edit dialog). On submit, call the new endpoint and refresh the list.
3. Reuse the existing vocabulary endpoint (`GET /artifacts/vocabulary`) to populate platform / type / status dropdowns so manually entered values stay consistent with CSV-imported ones.

**Notes:**
- The deterministic `_artifact_id` (sha1 of `name|platform|location`) means re-creating an "existing" artifact will UPSERT, which is the right behavior — keeps the manual-entry and CSV-ingest paths consistent.
- Don't forget to make the dialog respect the `is_user_edited=true` flag so a subsequent CSV re-upload doesn't blow over manual edits (same pattern used by `affiliates` / `source_system_canonical`).

---

## B-018 — `populate_gold` destructive DELETE+INSERT wipes LLM enrichment (Circular dep A)

| | |
|---|---|
| **Severity** | **HIGH** (silently destroys hours of LLM-enriched schema definitions on every re-run) |
| **Status** | ✅ **FIXED** (Phase 3 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `_run_populate_gold` (~line 6515) |

**Symptom (was):** Clicking "Populate Gold Layer" a second time (e.g. after editing a classification rule) `DELETE * + INSERT FROM silver_schemas` against `schema_inventory`, dropping every column produced by the AI-enrichment job (`definition`, `business_name`, `source_system`, `data_domain`, `department_owner`, `sensitivity`, `data_quality_tier`). Users couldn't reclassify without losing their enrichment.

**Fix recap:** Replaced the DELETE+batched-INSERT pattern with a batched MERGE. Rule-derived columns (program/affiliate/zone/classification/etc.) update; **LLM-enriched columns and user-edited rows are preserved**. Added a final `DELETE WHERE schema_key NOT IN (seen) AND not user-edited` pass to clean up rows whose source schemas vanished. Also `CAST(NULL AS TIMESTAMP)` for `enriched_at` in the source VALUES clause to prevent VOID-type binding errors.

---

## B-019 — `bootstrap_tables` ingest drops ALTER-added columns (Circular dep B)

| | |
|---|---|
| **Severity** | **HIGH** (re-uploading a fresh CSV silently breaks Schema Explorer) |
| **Status** | ✅ **FIXED** (Phase 4 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `ingest_schemas` / `ingest_tables` (~line 6160), `_SETUP_SILVER_DDL` |

**Symptom (was):** `ingest_tables` did `DROP TABLE IF EXISTS silver_tables` then `CREATE TABLE AS SELECT * FROM csv`. The CTAS produced a table with only the columns the schema-extractor CSV contained — wiping the `source_system` and `source_system_canonical` columns that the `normalize_source_systems` job had added via ALTER. After every fresh CSV upload, the source-system mapping became stale and Source Systems / Schema Explorer pages 500'd until the normalize job ran again. Same pattern wiped `is_user_edited=true` rows on `silver_schemas`.

**Fix recap:**
1. Added `silver_schemas` and `silver_tables` to `_SETUP_SILVER_DDL` with explicit column lists (including `source_system` / `source_system_canonical` from day-one).
2. Replaced DROP+CTAS with `MERGE INTO ... USING read_files(...)`. Match keys are `(catalog, schema)` and `(catalog, schema, table)`.
3. `WHEN MATCHED AND not user-edited THEN UPDATE` only the CSV-derived columns. User edits stick.
4. `WHEN NOT MATCHED THEN INSERT` with empty enrichment, NULL source-system columns.
5. `WHEN NOT MATCHED BY SOURCE THEN DELETE` for rows whose schemas/tables vanished from the CSV — but only if not user-edited.

Net effect: re-uploading the schema-extractor CSV is now an idempotent UPSERT, preserves enrichment + manual edits, and cleans up genuinely-deleted entries.

---

## B-013 — In-app Setup Wizard's bootstrap doesn't create the Volume

| | |
|---|---|
| **Severity** | Medium (defense-in-depth; only matters if someone deploys without `deploy.py`) |
| **Status** | ✅ **FIXED** (Phase 5 of E2E hardening, 2026-05-05) |
| **Where** | `src/app/src/bhe_catalog/backend/router.py` `setup_bootstrap_schemas()` (~line 870), `setup_status` volume probe (~line 685) |

**Fix recap:** `setup_bootstrap_schemas` now also runs `CREATE VOLUME IF NOT EXISTS <catalog>.<raw>.uploads` after the schemas exist. Returns the new volume in `created_volumes` for UI feedback. Added a corresponding `volume` probe to `/setup/status` (queries `information_schema.volumes`) so the wizard surface can show its state and `is_setup_ready` includes volume readiness.

---

## E2E hardening pass (2026-05-05)

Seven phases shipped in a single backend-only pass before the fresh-deploy E2E test:

| Phase | Bug(s) closed | Mechanism |
|---|---|---|
| 1 | B-004, B-015 + 6 latent missing-DDL gaps | 8 DDLs added to `_SETUP_GOLD_DDL` + universal classification rule seed + `_load_rules` defensive try/except |
| 2 | B-017 (defense-in-depth) | `_ensure_silver_tables_columns()` helper + 3 read endpoint call-sites |
| 3 | Circular dep A (→ **B-018**) | `populate_gold` MERGE-preserving rewrite |
| 4 | Circular dep B + B-017 (root cause) (→ **B-019**) | `silver_schemas` / `silver_tables` added to `_SETUP_SILVER_DDL`; ingest endpoints DROP+CTAS → MERGE |
| 5 | B-013 | Volume creation folded into `setup_bootstrap_schemas` + status probe |
| 6 | B-007 | Step 5 entities-from-sankey derivation in `_run_company_research` |
| 7 | B-006 (partial) | Trigger passes `steps`/`force` to inline runner; runner short-circuits when all requested steps already populated |

## Still pending after E2E hardening

If we're picking one PR at a time post-E2E:

1. **B-006 (rest)** — per-step skip when only a subset is missing. ~45 min. Requires re-indenting each of the 5 step bodies in `_run_company_research` under `if _should_run(step, table):` guards and loading `dept_names` / `all_uc_names` from DB after their respective steps. Helper already exists.
2. **B-008 + B-014 together** (wire 3 orphan jobs into wizard, sequenced in dependency order) — ~2 h, eliminates "now manually trigger 3 jobs in the right order" + makes B-014 invisible.
3. **Circular dep D** (use_case ID stability across research re-runs) — ~4 h, deterministic IDs + FK cleanup so KB proposal links don't dangle.
4. **Circular dep E** (silver_schemas vs schema_inventory dual computation of program/zone) — partially mitigated by B-018 (rules now source-of-truth post-populate), but the silver_schemas CTAS still has hardcoded BHE patterns.
5. **B-016** (BI & AI Artifacts manual entry button) — ~1 h, needs new POST endpoint + frontend dialog.
6. **B-005** (subdivision-aware research) — multi-PR effort, see plan doc.
7. **B-003** (seed CSV strategy) — depends on B-005 outcome; possibly auto-resolved.
8. **B-009** (persist run state) — 1 h, nice-to-have until someone hits it in prod.

Total for a single "polish iteration" excluding B-005 + circular dep D: ~3.5 hours of engineering.
