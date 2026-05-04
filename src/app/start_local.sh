#!/bin/bash
# Local development startup script
# Refreshes Databricks token and starts backend + frontend
#
# Usage:
#   DATABRICKS_PROFILE=<your-cli-profile> ./start_local.sh
#
# or set DATABRICKS_PROFILE in src/app/.env so you don't have to repeat it.

set -e

cd "$(dirname "$0")"

# Load .env first so it can set DATABRICKS_PROFILE, BHE_CATALOG, etc.
# Inline-set vars on the command line (DATABRICKS_PROFILE=foo ./start_local.sh)
# still win because the env-from-shell is exported before this script runs.
set -a
source .env 2>/dev/null || true
set +a

PROFILE="${DATABRICKS_PROFILE:-DEFAULT}"

echo "==> Refreshing Databricks token (profile: $PROFILE)..."
export DATABRICKS_TOKEN=$(databricks auth token --profile "$PROFILE" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

if [ -z "$DATABRICKS_TOKEN" ]; then
    echo "ERROR: Failed to get Databricks token for profile '$PROFILE'."
    echo "       Run: databricks auth login --profile $PROFILE"
    echo "       Or:  DATABRICKS_PROFILE=<your-profile> ./start_local.sh"
    echo "       Or:  add DATABRICKS_PROFILE=<your-profile> to src/app/.env"
    exit 1
fi
echo "==> Token acquired (expires in ~1 hour)"

export PYTHONPATH="$PWD/src:$PYTHONPATH"

echo "==> Starting FastAPI backend on port 8000..."
python3 -m uvicorn bhe_catalog.backend.app:app --reload --port 8000 --host 0.0.0.0 &
BACKEND_PID=$!

echo "==> Starting Vite frontend on port 5173..."
npx vite --port 5173 --host 0.0.0.0 &
FRONTEND_PID=$!

echo ""
echo "=========================================="
echo "  Backend:  http://localhost:8000/api/version"
echo "  Frontend: http://localhost:5173"
echo "  API docs: http://localhost:8000/docs"
echo "=========================================="
echo ""

cleanup() {
    echo "Shutting down..."
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

wait
