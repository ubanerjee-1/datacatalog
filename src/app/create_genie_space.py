"""Create or update the bhe_catalog_explorer Genie Space.

Reads the canonical space definition from
`resources/genie_spaces/bhe_catalog_explorer.json` and POSTs it to
`/api/2.0/genie/spaces` (or PATCHes if GENIE_SPACE_ID is set).

Single source of truth: the same JSON file is referenced by the DAB
resource at `resources/bhe_genie_catalog_explorer.genie_space.yml` so
reseeding via this script and promoting via DAB produce the same space.

Usage:
    # Create a new space (first run)
    python src/app/create_genie_space.py

    # Update an existing space (idempotent re-seed)
    GENIE_SPACE_ID=<id> python src/app/create_genie_space.py

Env:
    DATABRICKS_HOST       (required)
    DATABRICKS_TOKEN      (required)
    DATABRICKS_WAREHOUSE_ID (required)
    GENIE_SPACE_ID        (optional; PATCH instead of POST)
    GENIE_PARENT_PATH     (optional; /Workspace path for the space folder)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import requests

HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "your-warehouse-id")
SPACE_ID = os.environ.get("GENIE_SPACE_ID", "").strip()
PARENT_PATH = os.environ.get("GENIE_PARENT_PATH", "").strip()

TITLE = "BHE Catalog Explorer"
DESCRIPTION = (
    "Natural-language exploration of the BHE Data Catalog. "
    "Covers Unity Catalog inventory (schemas, tables), business use cases, "
    "affiliates, source systems, and cross-environment consistency. "
    "Backing store for the in-app chatbot's fallback data-query tool."
)

SPACE_DEF_PATH = (
    Path(__file__).resolve().parents[2]
    / "resources"
    / "genie_spaces"
    / "bhe_catalog_explorer.json"
)


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _require_env() -> None:
    if not HOST.startswith("http"):
        _fail("DATABRICKS_HOST must be set (e.g., https://<workspace>.cloud.databricks.com)")
    if not TOKEN:
        _fail("DATABRICKS_TOKEN must be set")
    if not WAREHOUSE_ID:
        _fail("DATABRICKS_WAREHOUSE_ID must be set")
    if not SPACE_DEF_PATH.exists():
        _fail(f"Space definition not found at {SPACE_DEF_PATH}")


def _normalize_space(obj: dict) -> dict:
    """Enforce the invariants the /genie/spaces API requires.

    Learned the hard way by failed POST attempts; documenting them in code
    so reseeds in a new workspace don't hit 400s:
      1. data_sources.tables[] must be sorted by identifier.
      2. Every text_instructions[] and example_question_sqls[] entry must have
         a lowercase 32-hex UUID `id` (no hyphens).
      3. Both lists must be sorted by id.
    We mutate `obj` in place for idempotency on reseed.
    """
    tables = obj.get("data_sources", {}).get("tables", [])
    tables.sort(key=lambda t: t["identifier"])

    instructions = obj.setdefault("instructions", {})
    for key in ("text_instructions", "example_question_sqls"):
        items = instructions.get(key, [])
        for item in items:
            if not item.get("id"):
                item["id"] = uuid.uuid4().hex
        items.sort(key=lambda d: d["id"])
    return obj


def _load_serialized_space() -> str:
    """Load canonical space JSON, normalize it, and return as a JSON string."""
    with SPACE_DEF_PATH.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    obj = _normalize_space(obj)
    # Persist any newly-assigned IDs back so the canonical file stays in sync
    # with what was sent — future reseeds will be byte-stable.
    with SPACE_DEF_PATH.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    return json.dumps(obj, indent=2)


def _post_create(headers: dict[str, str], payload: dict) -> dict:
    url = f"{HOST}/api/2.0/genie/spaces"
    r = requests.post(url, json=payload, headers=headers, timeout=60)
    if not r.ok:
        _fail(f"POST {url} -> {r.status_code}: {r.text[:600]}")
    return r.json()


def _patch_update(headers: dict[str, str], space_id: str, payload: dict) -> dict:
    url = f"{HOST}/api/2.0/genie/spaces/{space_id}"
    r = requests.patch(url, json=payload, headers=headers, timeout=60)
    if not r.ok:
        _fail(f"PATCH {url} -> {r.status_code}: {r.text[:600]}")
    return r.json()


def main() -> None:
    _require_env()

    serialized_space = _load_serialized_space()

    payload: dict[str, object] = {
        "title": TITLE,
        "description": DESCRIPTION,
        "warehouse_id": WAREHOUSE_ID,
        "serialized_space": serialized_space,
    }
    if PARENT_PATH:
        payload["parent_path"] = PARENT_PATH

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    if SPACE_ID:
        print(f"Updating Genie space {SPACE_ID}...")
        result = _patch_update(headers, SPACE_ID, payload)
        print(f"  OK — space_id={result.get('space_id', SPACE_ID)}")
    else:
        print("Creating new Genie space...")
        result = _post_create(headers, payload)
        new_id = result.get("space_id")
        print(f"  OK — space_id={new_id}")
        print("  Save this ID for your .env as GENIE_SPACE_ID to enable updates.")

    bytes_sent = len(serialized_space.encode("utf-8"))
    obj = json.loads(serialized_space)
    n_tables = len(obj.get("data_sources", {}).get("tables", []))
    n_questions = len(obj.get("instructions", {}).get("example_question_sqls", []))
    print(f"  Tables: {n_tables}")
    print(f"  Example questions: {n_questions}")
    print(f"  Serialized payload: {bytes_sent:,} bytes")


if __name__ == "__main__":
    main()
