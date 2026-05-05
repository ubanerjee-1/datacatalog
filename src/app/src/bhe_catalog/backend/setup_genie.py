"""Deploy / update the BHE Catalog Explorer Genie space from the running app.

The Genie space defined in `resources/genie_spaces/bhe_catalog_explorer.json`
references tables in `<catalog>.<silver>.*` and `<catalog>.<gold>.*`. The
public template uses `your_catalog` placeholders; this module substitutes
the running app's actual catalog/silver/gold names at deploy time before
POSTing to the Genie API.

Why we do this in the app rather than at `databricks bundle deploy` time:
    1. The Genie space cannot be created until the silver/gold tables exist.
       The Setup Wizard creates those tables interactively, so the wizard is
       also the natural place to deploy the space.
    2. Bundle-managed Genie spaces require Databricks CLI v0.287+ AND the
       direct-deploy engine. Many BHE workspaces are still on older CLI
       versions, so the bundle resource silently no-ops.

Returned `space_id` is persisted to `app_config` (key: `genie_space_id`)
so the chatbot's `genie_ask` tool can read it at request time without
needing a restart or a bundle redeploy.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import requests

from . import app_config
from .db import (
    _get_headers,
    _get_host,
    get_catalog,
    get_gold_schema,
    get_silver_schema,
)

logger = logging.getLogger(__name__)


# Path to the canonical space definition.
#
# Two locations are checked, in order:
#   1. The canonical file at `resources/genie_spaces/bhe_catalog_explorer.json`
#      (loaded preferentially in local dev, where the whole repo is on disk).
#   2. The shipped copy at `src/app/src/bhe_catalog/_genie_space_template.json`,
#      which IS uploaded with the app source (hence available on the deployed
#      Databricks App, where `resources/` is not).
#
# Whoever edits the JSON should keep BOTH copies in sync; `scripts/deploy.py`
# verifies and copies before deploy. From this module we just look up either.
_SPACE_DEF_PATH = (
    Path(__file__).resolve().parents[5]
    / "resources"
    / "genie_spaces"
    / "bhe_catalog_explorer.json"
)
_SPACE_DEF_FALLBACK = (
    Path(__file__).resolve().parents[1] / "_genie_space_template.json"
)

_PLACEHOLDER_CATALOG = "your_catalog"
# The JSON ships with these schema names. We re-rewrite them based on the
# user's actual silver/gold schema env vars in case they renamed.
_PLACEHOLDER_SILVER = "bhe_silver"
_PLACEHOLDER_GOLD = "bhe_gold"

TITLE = "BHE Catalog Explorer"
DESCRIPTION = (
    "Natural-language exploration of the BHE Data Catalog. "
    "Covers Unity Catalog inventory (schemas, tables), business use cases, "
    "affiliates, source systems, and cross-environment consistency. "
    "Backing store for the in-app chatbot's fallback data-query tool."
)


# ----------------------------------------------------------------------------
# JSON loading + catalog substitution
# ----------------------------------------------------------------------------

def _load_template() -> dict:
    for path in (_SPACE_DEF_PATH, _SPACE_DEF_FALLBACK):
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(
        f"Genie space template not found. Looked in: "
        f"{_SPACE_DEF_PATH} and {_SPACE_DEF_FALLBACK}."
    )


def _walk_replace(obj: Any, replacements: list[tuple[str, str]]) -> Any:
    """Recursively replace substrings in every string leaf of a JSON object.

    Order matters: replacements are applied in sequence per leaf string. We
    use this for the (catalog, silver, gold) substitutions below.
    """
    if isinstance(obj, str):
        out = obj
        for old, new in replacements:
            out = out.replace(old, new)
        return out
    if isinstance(obj, dict):
        return {k: _walk_replace(v, replacements) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_replace(v, replacements) for v in obj]
    return obj


def _normalize(obj: dict) -> dict:
    """Enforce Genie API invariants discovered the hard way (cf. create_genie_space.py).

    - data_sources.tables[] sorted by identifier
    - Every text_instructions[] / example_question_sqls[] item has a 32-hex id
    - Both lists sorted by id
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


def build_serialized_space(catalog: str, silver: str, gold: str) -> tuple[str, dict]:
    """Return (serialized_json_string, parsed_dict) ready to POST to Genie.

    The catalog placeholder is `your_catalog.` (note the trailing dot — that
    avoids accidental matches inside an example_question's prose like "from
    your_catalog management"). Silver/gold are rewritten so workspaces that
    customize BHE_SILVER_SCHEMA / BHE_GOLD_SCHEMA still get a working space.
    """
    obj = _load_template()
    repls = [
        # Order matters: catalog first (compound qualifier), then schemas
        (f"{_PLACEHOLDER_CATALOG}.{_PLACEHOLDER_SILVER}.", f"{catalog}.{silver}."),
        (f"{_PLACEHOLDER_CATALOG}.{_PLACEHOLDER_GOLD}.", f"{catalog}.{gold}."),
        # Catch any leftover `your_catalog.<other_schema>.` references too
        (f"{_PLACEHOLDER_CATALOG}.", f"{catalog}."),
    ]
    obj = _walk_replace(obj, repls)
    obj = _normalize(obj)
    return json.dumps(obj, indent=2), obj


# ----------------------------------------------------------------------------
# Genie API
# ----------------------------------------------------------------------------

def _genie_post(payload: dict) -> dict:
    url = f"{_get_host()}/api/2.0/genie/spaces"
    r = requests.post(url, headers=_get_headers(), json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"POST {url} -> {r.status_code}: {r.text[:600]}")
    return r.json()


def _genie_patch(space_id: str, payload: dict) -> dict:
    url = f"{_get_host()}/api/2.0/genie/spaces/{space_id}"
    r = requests.patch(url, headers=_get_headers(), json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"PATCH {url} -> {r.status_code}: {r.text[:600]}")
    return r.json()


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------

def deploy_genie_space(*, principal: str = "", force_new: bool = False) -> dict:
    """Create-or-update the Genie space for the running app's catalog.

    Reads the existing space ID from `app_config` (falling back to
    GENIE_SPACE_ID env var); PATCHes if known, POSTs otherwise. Persists
    the resulting `space_id` to `app_config` so subsequent calls are
    idempotent updates.

    `force_new=True` ignores any existing space ID and always POSTs a fresh
    space. Use this when an earlier PATCH failed (e.g. 409 export-format
    drift) and the user wants to abandon the old space.

    Returns: {space_id, mode: 'created'|'updated', tables, example_questions, url?}
    """
    catalog = get_catalog()
    silver = get_silver_schema()
    gold = get_gold_schema()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
    if not warehouse_id:
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID is not set")

    serialized, obj = build_serialized_space(catalog, silver, gold)

    if force_new:
        existing_id = ""
    else:
        existing_id = (
            app_config.get_config_value("genie_space_id")
            or os.environ.get("GENIE_SPACE_ID", "").strip()
            or ""
        ).strip()

    if existing_id:
        # PATCH: only send fields that should actually change. Sending `title`
        # on PATCH triggers a sibling-name collision check that fails when
        # another space at the same parent_path has the same title -- easy
        # to hit if you re-ran POST during setup and ended up with two.
        patch_payload: dict[str, Any] = {
            "serialized_space": serialized,
            "warehouse_id": warehouse_id,
            "description": DESCRIPTION,
        }
        logger.info("Updating existing Genie space %s", existing_id)
        result = _genie_patch(existing_id, patch_payload)
        space_id = result.get("space_id") or existing_id
        mode = "updated"
    else:
        post_payload: dict[str, Any] = {
            "title": TITLE,
            "description": DESCRIPTION,
            "warehouse_id": warehouse_id,
            "serialized_space": serialized,
        }
        parent_path = os.environ.get("GENIE_PARENT_PATH", "").strip()
        if parent_path:
            post_payload["parent_path"] = parent_path
        logger.info("Creating new Genie space (force_new=%s)", force_new)
        result = _genie_post(post_payload)
        space_id = result.get("space_id") or result.get("id") or ""
        if not space_id:
            raise RuntimeError(f"Genie POST returned no space_id: {result!r}")
        mode = "created"

    # Persist back so the chatbot's genie_ask tool picks it up without restart.
    try:
        app_config.set_config_value("genie_space_id", space_id, principal=principal)
    except Exception as e:
        # Don't fail the wizard step if the writeback failed -- the space
        # was already created successfully. Surface a warning instead.
        logger.warning("Could not persist genie_space_id to app_config: %s", e)

    host = _get_host().rstrip("/")
    return {
        "space_id": space_id,
        "mode": mode,
        "tables": len(obj.get("data_sources", {}).get("tables", [])),
        "example_questions": len(obj.get("instructions", {}).get("example_question_sqls", [])),
        "url": f"{host}/genie/rooms/{space_id}" if host and space_id else "",
    }
