# bhe-catalog ✨

> A modern full-stack application built with [`apx`](https://github.com/databricks-solutions/apx) 🚀

## 🛠️ Tech Stack

This application leverages a powerful, modern tech stack:

- **Backend** 🐍 Python + [FastAPI](https://fastapi.tiangolo.com/)
- **Frontend** ⚛️ React + [shadcn/ui](https://ui.shadcn.com/)
- **API Client** 🔄 Auto-generated TypeScript client from OpenAPI schema

## 🚀 Quick Start

### Development Mode

Start all development servers (backend, frontend, and OpenAPI watcher) in detached mode:

```bash
apx dev start
```

This will start an apx development server, which in it's turn runs backend, frontend and OpenAPI watcher.
All servers run in the background, with logs kept in-memory of the apx dev server.

### 📊 Monitoring & Logs

```bash
# View all logs
apx dev logs

# Stream logs in real-time
apx dev logs -f

# Check server status
apx dev status

# Stop all servers
apx dev stop
```

## ✅ Code Quality

Run type checking and linting for both TypeScript and Python:

```bash
apx dev check
```

## 📦 Build

Create a production-ready build:

```bash
apx build
```

## 🚢 Deployment

One-shot interactive deploy (recommended):

```bash
python3 scripts/deploy.py
```

The script prompts for the workspace URL, CLI profile, Unity Catalog, and SQL
warehouse ID, then:

1. Updates `databricks.yml` + `src/app/app.yml` with those values.
2. Builds the React SPA (`npx vite build`).
3. Runs `databricks bundle deploy` to push app, jobs, and pipelines.
4. Grants the app's service principal `USE_CATALOG` / `SELECT` / `MODIFY` on
   the target catalog and schemas (required — a freshly-created app SP has no
   UC privileges by default).
5. Runs `databricks bundle run bhe_catalog_app` to start/restart the app.

Non-interactive / CI mode — pass everything as flags:

```bash
python3 scripts/deploy.py --yes \
  --target dev \
  --workspace-url https://<workspace>.cloud.databricks.com \
  --profile <cli-profile> \
  --catalog <uc_catalog> \
  --warehouse-id <warehouse_id>
```

Raw / silver / gold schema names and the LLM endpoint default to
`bhe_raw` / `bhe_silver` / `bhe_gold` / `databricks-claude-sonnet-4-6`; override
with `--raw-schema`, `--silver-schema`, `--gold-schema`, `--llm-endpoint` if
different. Use `--skip-build` to reuse the last `vite build` and `--skip-grants`
if the SP already has Unity Catalog privileges.

Plain bundle deploy (won't grant UC privileges):

```bash
databricks bundle deploy -p <your-profile>
```

---

<p align="center">Built with ❤️ using <a href="https://github.com/databricks-solutions/apx">apx</a></p>
