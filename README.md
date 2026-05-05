# BHE Data Catalog

> An AI-powered data catalog for Berkshire Hathaway Energy. Browse every Unity
> Catalog schema across the fleet, generate business-friendly definitions and
> domain classifications with LLMs, surface use-case-to-source-system mappings,
> and run guided "company intelligence" research — all packaged as a single
> [Databricks App](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html).

Built with [`apx`](https://github.com/databricks-solutions/apx) — FastAPI
backend, React + shadcn/ui frontend, deployed via a
[Databricks Asset Bundle](https://docs.databricks.com/en/dev-tools/bundles/index.html).

---

## Table of Contents

- [What you get](#what-you-get)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick start (recommended)](#quick-start-recommended)
- [Files you need to edit before deploying](#files-you-need-to-edit-before-deploying)
- [First-run setup wizard](#first-run-setup-wizard)
- [Local development](#local-development)
- [Repository layout](#repository-layout)
- [Common operations](#common-operations)
- [Troubleshooting](#troubleshooting)

---

## What you get

Once deployed and configured, the app gives BHE users:

| Page | What it does |
|------|--------------|
| **Dashboard** | Top-level catalog stats: schemas, tables, programs, affiliates, environment consistency |
| **Data Catalog** | Browse schemas in two hierarchies — `Program → Zone → Schema` or `Workspace → Catalog → Schema`. Click any schema for AI-enriched metadata, table lists, classification |
| **Source Systems** | Sankey-style view of how source systems flow into business use cases |
| **BI & AI Artifacts** | Inventory of dashboards, models, queries, and AI agents with AI-generated descriptions |
| **Knowledge** | Wiki-style folders + articles, attachable to any catalog entity |
| **Value & Readiness** | Business-value model: estimated $ for each use case, source-system gap analysis |
| **Gaps** | Use-cases ↔ required source systems coverage matrix |
| **Source Taxonomy** | 8-dimension AI classification of every schema |
| **Company Setup** | First-run wizard that bootstraps schemas + tables, runs company-research, and gates other features until prerequisites are met |
| **Classification Rules** | User-editable rules that drive program / affiliate / environment / zone derivation |
| **Edit Center** | Curate affiliates, canonical sources, use-case ↔ source-system mappings |
| **Chat** | Ask questions about the catalog in natural language (RAG over enriched metadata + Genie space) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Databricks Workspace                          │
│                                                                     │
│   ┌──────────────────────────┐      ┌──────────────────────────┐    │
│   │   Databricks App         │      │   Unity Catalog          │    │
│   │  ┌────────────────────┐  │      │   <BHE_CATALOG>          │    │
│   │  │  React SPA (Vite)  │  │      │  ┌────────────────────┐  │    │
│   │  │       ↓            │  │ SQL  │  │  bhe_raw  (Volume) │  │    │
│   │  │  FastAPI backend   │──┼──────┼─▶│  bhe_silver        │  │    │
│   │  │       ↓            │  │      │  │  bhe_gold          │  │    │
│   │  └────────────────────┘  │      │  └────────────────────┘  │    │
│   │  Auth: app's managed     │      └──────────────────────────┘    │
│   │  service principal       │                                      │
│   └──────────┬───────────────┘      ┌──────────────────────────┐    │
│              │                      │   SQL Warehouse           │   │
│              │  Statement Exec API  │   <DATABRICKS_WAREHOUSE_  │   │
│              └─────────────────────▶│       ID>                 │   │
│              │                      └──────────────────────────┘    │
│              │                      ┌──────────────────────────┐    │
│              │  ai_query()          │   Model Serving           │   │
│              └─────────────────────▶│   <LLM_ENDPOINT>          │   │
│                                     └──────────────────────────┘    │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │  Asset-Bundle-managed Jobs (long-running enrichment)        │   │
│   │  • bhe_company_research      • bhe_table_enrich             │   │
│   │  • bhe_glossary              • bhe_normalize_source_systems │   │
│   │  • bhe_build_value_model     + Lakeflow ingest / gold       │   │
│   └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

The app runs everything as the managed service principal Databricks Apps
creates for it — no PATs, no shared credentials. Three Unity Catalog schemas
are needed (`raw` for Volume uploads, `silver` for ingest + research data,
`gold` for analytics tables); they're all created on first run.

---

## Prerequisites

### In your Databricks workspace

| Resource | Why you need it | Notes |
|----------|----------------|-------|
| **Unity Catalog enabled** | Everything reads/writes through UC | Required |
| **A Unity Catalog** | The app creates 3 schemas inside it | Pick an existing one or create a fresh one (e.g. `bhe_data_catalog`) |
| **A SQL warehouse** | The app routes every SQL statement through it | Serverless or classic; size depends on enrichment workload |
| **A model-serving endpoint** | AI enrichment + chatbot + company research | Any chat-completion endpoint the SP can `CAN_QUERY`. Default: `databricks-claude-sonnet-4`. Pay-per-token endpoints work fine |
| **Permissions to create a Databricks App** | The bundle deploys an App resource | Workspace admin or "Can Manage" on the Apps feature |
| **Permission to GRANT on the catalog** | Either you, or the deploy script, must grant the app's SP access | Metastore admin or catalog owner |

### On your local machine

| Tool | Version | Used for |
|------|---------|----------|
| [Databricks CLI](https://docs.databricks.com/en/dev-tools/cli/install.html) | ≥ 0.220 | Asset-Bundle deploy, GRANTs, app start |
| Python | ≥ 3.10 | `scripts/deploy.py` |
| Node.js | ≥ 20 | Builds the React SPA (`npx vite build`) |
| Git | any | `git clone` |

> [!NOTE]
> **Windows users**: the python.org installer ships `python.exe` and `py.exe`
> but **not** `python3.exe`. All commands in this README written as
> `python3 ...` work as `python ...` on Windows — or `py -3 ...` if you have
> the launcher. Examples below use `python3` for parity with macOS/Linux.

You also need an **authenticated Databricks CLI profile** for the workspace
you're deploying to. If you don't already have one:

```bash
databricks auth login --host https://<workspace>.cloud.databricks.com -p <profile-name>
```

Verify it works:

```bash
databricks current-user me -p <profile-name>
```

---

## Quick start (recommended)

```bash
# 1. Clone the repo
git clone https://github.com/ubanerjee-1/datacatalog.git
cd datacatalog

# 2. Run the interactive deploy script — answer the prompts
python3 scripts/deploy.py
```

The script does **all** of this for you:

1. Prompts for workspace URL, CLI profile, target catalog, warehouse ID,
   schema names, and LLM endpoint.
2. Rewrites `databricks.yml` and `src/app/app.yml` with your values.
3. Builds the React SPA (`npx vite build`).
4. Runs `databricks bundle deploy` to create the app + jobs + pipelines.
5. Resolves the app's auto-created service principal and **runs the UC
   `GRANT` statements** so the SP can read / write the target catalog +
   schemas + Volume.
6. Runs `databricks bundle run bhe_catalog_app` to start the app.
7. Prints the app URL — open it. The app lands on the **Setup Wizard**.

> [!TIP]
> Re-running `scripts/deploy.py` is safe and idempotent. The defaults in
> every prompt are pre-filled with the last values you used.

### Non-interactive (CI / scripted)

```bash
python3 scripts/deploy.py --yes \
  --target dev \
  --workspace-url https://<workspace>.cloud.databricks.com \
  --profile <cli-profile> \
  --catalog <uc_catalog> \
  --warehouse-id <warehouse_id> \
  --llm-endpoint databricks-claude-sonnet-4
```

Pass `--skip-build` to reuse the last `vite build`, `--skip-grants` if the
SP already has UC privileges. Run `python3 scripts/deploy.py --help` for the
full flag list.

---

## Files you need to edit before deploying

If you'd rather **not** use `scripts/deploy.py` (or you're tweaking after a
first deploy), only **two files** need to be aligned with your environment.
Both are simple YAML — keep them in sync.

### 1. `databricks.yml` (bundle-level)

Drives the Asset Bundle's resources (jobs, pipelines, App SQL warehouse
binding) and the workspace target.

```yaml
variables:
  catalog:
    description: "Unity Catalog that jobs/pipelines write to."
    default: "your_catalog"          # ← change this
  warehouse_id:
    description: "SQL warehouse ID the app and bundled jobs run against."
    default: "your-warehouse-id"     # ← change this

targets:
  dev:
    workspace:
      host: https://your-workspace.cloud.databricks.com   # ← change
      profile: your-cli-profile                            # ← change
    variables:
      catalog: "your_catalog"                              # ← change
```

### 2. `src/app/app.yml` (app runtime)

Sets the env vars the FastAPI backend reads at startup. The Setup Wizard
shows whatever's here and complains loudly if anything is missing.

```yaml
env:
  - name: BHE_CATALOG
    value: "your_catalog"                  # ← change to your target catalog
  - name: DATABRICKS_WAREHOUSE_ID
    value: "your-warehouse-id"             # ← change to your warehouse id
  - name: BHE_RAW_SCHEMA
    value: "bhe_raw"                       # rarely changed
  - name: BHE_SILVER_SCHEMA
    value: "bhe_silver"                    # rarely changed
  - name: BHE_GOLD_SCHEMA
    value: "bhe_gold"                      # rarely changed
  - name: LLM_ENDPOINT
    value: "databricks-claude-sonnet-4"  # any chat-completion endpoint the SP can CAN_QUERY
```

> [!IMPORTANT]
> `BHE_CATALOG` in `app.yml` and `var.catalog` in `databricks.yml` **must
> match**. Same for `DATABRICKS_WAREHOUSE_ID` ↔ `var.warehouse_id`.
> `scripts/deploy.py` keeps them in sync automatically; if you edit by hand,
> double-check both.

After editing, deploy and run:

```bash
databricks bundle deploy -t dev -p <profile>
databricks bundle run bhe_catalog_app -t dev -p <profile>
```

This **does NOT** grant Unity Catalog permissions for you. You either need
to use `scripts/deploy.py` (which does), or run the GRANTs manually — the
Setup Wizard inside the app generates the exact SQL for you (see next
section).

---

## First-run setup wizard

When a fresh deployment opens for the first time, the home page (`/`)
auto-redirects to **Company Setup** (`/company`) and you land on a guided
wizard with the following steps:

| Step | What happens |
|------|--------------|
| **Setup overview banner** | 9 colored pills at the top — `Config / Warehouse / Catalog / LLM / Schemas / Tables / Genie / Data / Company`. Green = ready, red = blocking |
| **Step 1 — Environment & Identity** | Read-only confirmation of the env vars in `app.yml`, plus the running service principal's identity (the user/SP the app authenticates as) |
| **Step 2 — Catalog, Warehouse & LLM Access** | Three live probes: `SELECT 1` on the warehouse, `SHOW SCHEMAS IN <catalog>`, and `ai_query('<endpoint>', …)`. If any fail, **a copy-pastable `GRANT` SQL block appears** that you can run as a metastore admin in the Databricks SQL editor |
| **Step 3 — Database Bootstrap** | Click one button to `CREATE SCHEMA IF NOT EXISTS` (raw / silver / gold) and `CREATE TABLE IF NOT EXISTS` for all required tables (including `bhe_gold.app_config`, the runtime key/value store the wizard uses). Idempotent — safe to re-run |
| **Step 4 — Deploy Genie Space** *(optional)* | Locked until step 3 is green. One click: substitutes your catalog into the canonical JSON template, calls `POST /api/2.0/genie/spaces` (or `PATCH` if already deployed), and persists the returned `space_id` to `bhe_gold.app_config` so the chatbot's `genie_ask` fallback tool resolves it at runtime without an app restart |
| **Step 5 — Company Intelligence** | Locked until step 3 is green. Type your company name → AI generates departments, use cases with $ values, required data entities, source-system Sankey mappings |
| **Step 6 — Data Sources** | Locked until step 3 is green. Download the schema-extractor utility (in `schema-extractor/`), run it across your workspaces, upload the resulting `all_schemas.csv` + `all_tables.csv` |
| **Step 7 — Enrichment Pipeline** | Locked until data is ingested. One-click "Run All (Sequential)": Populate Gold → AI Enrich Schemas → AI Enrich Tables → Generate Taxonomy |
| **Danger Zone** | Collapsed at the bottom. Type the catalog name to enable a `DROP SCHEMA … CASCADE` for all 3 schemas — full reset |

You'll know setup is fully complete when all 9 pills are green and the
home page redirects to `/dashboard` instead of `/company`. (The `Genie`
pill is optional — the chatbot's typed `app_*` tools work without it; only
the free-form `genie_ask` fallback needs the Genie space.)

### What if I don't want to grant the GRANTs the wizard suggests?

The minimum permission set for the app's service principal is:

```sql
-- on the configured catalog
GRANT USE_CATALOG, CREATE_SCHEMA ON CATALOG `<catalog>` TO `<sp>`;

-- on each of the 3 schemas (after they exist — wizard creates them)
GRANT USE_SCHEMA, SELECT, MODIFY, CREATE_TABLE
  ON SCHEMA `<catalog>`.`bhe_raw`    TO `<sp>`;
GRANT USE_SCHEMA, SELECT, MODIFY, CREATE_TABLE
  ON SCHEMA `<catalog>`.`bhe_silver` TO `<sp>`;
GRANT USE_SCHEMA, SELECT, MODIFY, CREATE_TABLE
  ON SCHEMA `<catalog>`.`bhe_gold`   TO `<sp>`;
```

Plus, in the Databricks UI:

- **SQL Warehouses → `<warehouse>` → Permissions:** grant `CAN USE` to `<sp>`.
- **Serving → `<llm-endpoint>` → Permissions:** grant `CAN_QUERY` to `<sp>`.

`scripts/deploy.py --skip-grants false` (the default) does all of these for
you. Click "Re-check" on Step 2 of the wizard after you've granted the
permissions to confirm they took.

---

## Local development

> [!NOTE]
> Local dev is **optional** — you don't need it to deploy. If you only want
> to deploy to your workspace, skip this section.

### Setup

```bash
# Authenticate the CLI as a workspace user (NOT an SP — the local server
# runs as you). This populates ~/.databrickscfg.
databricks auth login -p <your-cli-profile>

# Install Python + Node deps
cd src/app
uv sync                         # backend deps (or pip install -e .)
npm install                     # frontend deps

# Tell start_local.sh which CLI profile to refresh tokens from
echo 'DATABRICKS_PROFILE=<your-cli-profile>' > .env
```

### Run

```bash
cd src/app
./start_local.sh
```

This refreshes a Databricks token from the CLI profile, starts the FastAPI
backend on `:8000`, and Vite on `:5173`. Open <http://localhost:5173>.

The frontend `vite.config.ts` proxies `/api/*` → `http://localhost:8000`,
so end-to-end is the same shape as the deployed app.

### Code quality

```bash
cd src/app
apx dev check    # ruff + tsc
```

---

## Repository layout

```
bhe-data-catalog/
├── README.md                           ← you are here
├── databricks.yml                      ← bundle vars + workspace targets ⚙️
├── pyproject.toml                      ← uv workspace
├── resources/                          ← Asset Bundle resources
│   ├── bhe_catalog_app.app.yml         ← the App definition
│   ├── bhe_company_research.job.yml    ← Long-running AI jobs
│   ├── bhe_table_enrich.job.yml
│   ├── bhe_glossary.job.yml
│   ├── bhe_build_value_model.job.yml
│   ├── bhe_normalize_source_systems.job.yml
│   ├── bhe_ingest.pipeline.yml         ← Lakeflow pipelines
│   ├── bhe_gold.pipeline.yml
│   └── bhe_genie_catalog_explorer.genie_space.yml
├── scripts/
│   └── deploy.py                       ← one-shot deploy ⚙️
├── schema-extractor/                   ← Standalone utility for users to
│   ├── extract_schemas.py              │  pull schema/table metadata from
│   ├── workspaces.txt                  │  multiple workspaces (workspaces
│   └── README.md                       │  the app's SP can't reach)
├── src/
│   ├── app/                            ← The Databricks App
│   │   ├── app.yml                     ← runtime env vars ⚙️
│   │   ├── start_local.sh              ← local dev launcher
│   │   ├── pyproject.toml
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── README.md                   ← apx dev cheat-sheet
│   │   └── src/bhe_catalog/
│   │       ├── backend/                ← FastAPI
│   │       │   ├── app.py
│   │       │   ├── router.py           ← all /api/* endpoints incl. /api/setup/*
│   │       │   ├── db.py               ← UC SQL Statement API client
│   │       │   ├── chat.py             ← chatbot router
│   │       │   └── core/
│   │       └── ui/                     ← React SPA
│   │           ├── routes/
│   │           │   ├── index.tsx       ← redirects to /dashboard or /company
│   │           │   └── _sidebar/
│   │           │       ├── company.tsx ← the setup wizard
│   │           │       ├── data-catalog.tsx
│   │           │       └── …
│   │           └── lib/api-client.ts
│   ├── jobs/                           ← Job entrypoints (called by resources/*.job.yml)
│   ├── pipelines/                      ← DLT pipeline code
│   └── data/                           ← Reference / seed CSVs synced to Volume
└── docs/
    └── plans/                          ← Internal design docs
```

(⚙️ = files you may need to edit per-environment)

---

## Common operations

### Re-deploy after a code change

```bash
python3 scripts/deploy.py --yes --skip-grants \
  --target dev --profile <profile> \
  --catalog <catalog> --warehouse-id <warehouse_id>
```

…or just `databricks bundle deploy && databricks bundle run bhe_catalog_app`
once the YAMLs are correct.

### Tail app logs

```bash
databricks apps logs bhe-data-catalog-dev -p <profile>
```

### Re-run the company-research job manually

```bash
databricks bundle run bhe_company_research -t dev -p <profile>
```

…or click "Resume Research" in Step 4 of the wizard.

### Reset everything

Open the app → Company Setup → expand the **Danger Zone** at the bottom →
type the catalog name → click "Drop everything". Then click "Create schemas
+ tables" in Step 3 to start over.

---

## Troubleshooting

### `403 Forbidden` on every `/api/*` call after deploy

The app's service principal doesn't have access to the catalog or
warehouse. Open `/company` → Step 2 will show exactly which probe is
failing and a `Copy SQL` button with the GRANT statements. Run them as a
metastore admin or catalog owner, then click "Re-check".

### `ai_query()` probe fails

The SP needs `CAN_QUERY` on the model-serving endpoint. In the Databricks
UI: **Serving → `<endpoint>` → Permissions → Add `CAN_QUERY` → service
principal name = `<sp>`**. Or use the CLI command shown in the wizard's
grants panel.

### `databricks bundle deploy` fails on `uv` step

The bundle uses `uv` to resolve workspace members in `src/*`. Make sure
`src/app/pyproject.toml` is present (it is by default). If you've checked
out a partial repo, run `git status` to see if anything got `.gitignore`d.

### Vite frontend says "Loading..." forever

The backend can't reach `/api/setup/status`. Open the browser dev tools →
Network tab and check the response. Most likely:
- The SP has no warehouse access → see above.
- The configured warehouse is stopped → start it from Compute → SQL Warehouses.

### "Catalog not found"

`BHE_CATALOG` in `app.yml` doesn't exist in your workspace. Either create
it (`CREATE CATALOG <name>`) or change the value to an existing catalog
and re-deploy.

---

## Notes

- **Privacy:** The schema-extractor (`schema-extractor/extract_schemas.py`)
  runs locally and uploads only **metadata** (catalog/schema/table names +
  comments + owner) — no row data ever leaves your workspaces.
- **Cost:** AI enrichment uses the configured model-serving endpoint. A
  one-time enrichment of ~10k schemas + ~50k tables costs roughly the
  equivalent of a few hours of pay-per-token usage on Claude Sonnet — see
  Databricks billing logs for exact figures.
- **Reset:** The "Danger Zone" in the setup wizard `DROP SCHEMA … CASCADE`s
  all three BHE schemas. There's no undo. Volume contents (uploaded CSVs,
  branding logos, knowledge attachments) all go with the raw schema.

---

## License

[MIT](./LICENSE) — do whatever you like, no warranty.

## Contact

Issues, questions, PRs welcome on the GitHub repo:
<https://github.com/ubanerjee-1/datacatalog>
