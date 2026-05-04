"""Chatbot Phase A2 — confirmation token registry + write executors.

Lifecycle of a chat-driven write:

  1. The model calls a `propose_*` tool. The tool validates the inputs,
     fetches the current entity state (so the FE can render a diff),
     and calls `issue_token(intent, payload, ...)`. The tool result
     contains the token plus the proposed payload.
  2. The FE renders an inline confirm card with Confirm/Cancel buttons.
  3. On Confirm, the FE POSTs `/api/chat/confirm/{token}`. We
     `consume_token()` (atomic single-use check) and then dispatch to
     the matching executor in `_INTENT_EXECUTORS`, which performs the
     actual write against the silver layer.
  4. Cancel never writes. The token simply expires after `_TOKEN_TTL`.

Why server-side payload storage instead of FE re-sending it?
The chat thread is the source of truth for what was proposed. If the FE
re-sent the payload on confirm, a malicious page-side mutation could
slip a different value past the user's eye. Storing the payload at
issue time means the only thing the FE controls on confirm is "yes/no
to the thing the chat showed me".

Why Delta and not redis/in-memory?
We already run on Delta for everything else; one more table with a
~10min TTL is cheaper than introducing a second store. Volume is low
(a confirm card costs at minimum two human turns) and latency is
human-paced. The TTL cleanup is best-effort lazy: expired tokens are
simply rejected at consume time and stay in the table for audit.
"""
from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from .core._headers import DatabricksAppsHeaders, HeadersDependency  # noqa: F401  (DatabricksAppsHeaders is imported only for type clarity in _resolve_user_key)
from .db import execute_query, execute_update, fqn, get_silver_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_TOKEN_TTL = timedelta(minutes=10)
"""How long a propose can sit before the user must re-issue it.

Short enough that a stolen token has limited replay window; long enough
that the user can read the card, ask a clarifying question, and still
confirm. The chat UI also disables the button after expiry.
"""


# Intent names. These string-match the `intent` column in chat_confirm_tokens
# and the keys in `_INTENT_EXECUTORS`. We use kebab/camel matching the
# FastAPI operation_id of the underlying write so logs are easy to grep.
INTENT_UPDATE_USE_CASE_STATUS = "updateUseCaseStatus"
INTENT_UPDATE_USE_CASE = "updateUseCase"
INTENT_UPDATE_USE_CASE_AFFILIATES = "updateUseCaseAffiliates"
INTENT_UPDATE_USE_CASE_CANONICALS = "updateUseCaseCanonicals"
INTENT_CREATE_USE_CASE = "createUseCase"
# A3-1: schema editing. One intent covers both single- and
# multi-catalog updates because the executor reads the catalog list
# from the payload (the chat always passes the explicit list it
# pre-flighted, so a UI write happening between propose and confirm
# can't sneak in extra rows).
INTENT_UPDATE_SCHEMA = "updateSchema"


# ---------------------------------------------------------------------------
# DB helpers — token table
# ---------------------------------------------------------------------------


def _tokens_table() -> str:
    return fqn(get_silver_schema(), "chat_confirm_tokens")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _q(s: str) -> str:
    return s.replace("'", "''")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def issue_token(
    *,
    intent: str,
    target_id: str,
    payload: dict[str, Any],
    user_key: str,
    conversation_id: str,
) -> dict[str, Any]:
    """Persist a single-use confirmation token.

    Returns the row we just wrote (token + expires_at + intent + payload)
    so the propose tool can include it directly in its `data` payload.

    The caller is expected to have already validated the payload — this
    function does NOT re-check it. We do verify the intent is known so a
    typo in a propose tool can't issue a token nothing can consume.
    """
    if intent not in _INTENT_EXECUTORS:
        raise ValueError(f"Unknown confirm intent: {intent!r}")
    token = secrets.token_hex(16)
    now = datetime.now(timezone.utc)
    expires = now + _TOKEN_TTL
    payload_json = json.dumps(payload, default=str)
    sql = f"""
        INSERT INTO {_tokens_table()}
            (token, conversation_id, user_key, intent, target_id, payload,
             created_at, expires_at, consumed_at)
        VALUES (
            '{_q(token)}',
            '{_q(conversation_id)}',
            '{_q(user_key)}',
            '{_q(intent)}',
            '{_q(target_id)}',
            '{_q(payload_json)}',
            TIMESTAMP '{now.strftime("%Y-%m-%d %H:%M:%S")}',
            TIMESTAMP '{expires.strftime("%Y-%m-%d %H:%M:%S")}',
            NULL
        )
    """
    execute_update(sql, tag_overrides={"submodule": "chat.confirm.issue"})
    return {
        "token": token,
        "intent": intent,
        "target_id": target_id,
        "payload": payload,
        "expires_at": expires.isoformat(),
    }


def _fetch_token(token: str, user_key: str) -> dict[str, Any] | None:
    rows = execute_query(
        f"""
        SELECT token, conversation_id, user_key, intent, target_id, payload,
               created_at, expires_at, consumed_at
        FROM {_tokens_table()}
        WHERE token = '{_q(token)}'
          AND user_key = '{_q(user_key)}'
        LIMIT 1
        """,
        tag_overrides={"submodule": "chat.confirm.fetch"},
    )
    return rows[0] if rows else None


def _mark_consumed(token: str) -> int:
    """Atomically flip consumed_at IFF still NULL.

    Returns affected row count (0 if already consumed). The chat UI
    serializes Confirm clicks per-conversation, but we still need this
    guard against the unlikely case of two browser tabs racing on the
    same token.
    """
    return execute_update(
        f"""
        UPDATE {_tokens_table()}
        SET consumed_at = TIMESTAMP '{_now_iso()}'
        WHERE token = '{_q(token)}'
          AND consumed_at IS NULL
        """,
        tag_overrides={"submodule": "chat.confirm.consume"},
    )


# ---------------------------------------------------------------------------
# Intent executors — one function per write the chat can perform
# ---------------------------------------------------------------------------
#
# Each executor receives (validated_payload, user_key) and returns a dict
# describing the resulting state (what the FE should optimistically
# render after confirm). They go straight to the warehouse — no HTTP
# hop — so audit and timing stay clean. New propose_* tools register
# their executor here; the chat router itself doesn't grow.
#
# `user_key` is threaded through so the upcoming audit columns
# (created_by/updated_by — see chatbot-track-a.md A2 follow-ups) can be
# wired without changing every executor signature again.


# Type alias kept here so the FE/BE contract is obvious.
ExecutorFn = Callable[[dict[str, Any], str], dict[str, Any]]


def _exec_update_use_case_status(
    payload: dict[str, Any], user_key: str
) -> dict[str, Any]:
    """Mirror of router.update_use_case_status() but called in-process.

    Kept as a private function so the propose/confirm code path can run
    without importing FastAPI Depends machinery. We import the existing
    helpers to avoid drift in the SQL.
    """
    # Local import to avoid a circular import with router.py at module load.
    from .router import _ensure_use_case_status_columns, _normalize_status

    use_case_id = payload.get("use_case_id")
    new_status = payload.get("status")
    notes = payload.get("status_notes")
    if not use_case_id or not new_status:
        raise ValueError("payload missing use_case_id or status")

    silver = get_silver_schema()
    _ensure_use_case_status_columns()
    status = _normalize_status(new_status)
    updates = [
        f"status = '{status}'",
        "status_updated_at = current_timestamp()",
        "is_user_edited = true",
    ]
    if notes is not None:
        updates.append(f"status_notes = '{_q(str(notes))}'")
    execute_update(
        f"UPDATE {fqn(silver, 'use_cases')} "
        f"SET {', '.join(updates)} "
        f"WHERE id = '{_q(str(use_case_id))}'",
        tag_overrides={
            "submodule": "chat.confirm.update_use_case_status",
            "user_key": user_key,
        },
    )
    return {
        "use_case_id": use_case_id,
        "new_status": status,
        "status_notes": notes,
    }


def _exec_update_use_case(
    payload: dict[str, Any], user_key: str
) -> dict[str, Any]:
    """Mirror of router.update_use_case() but called in-process.

    Re-uses `build_use_case_update_set_clause` (router.py) so the chat
    write and the UI write run identical SQL — no drift on which fields
    are writable or how each one is escaped. The `patch` key carries the
    sub-dict of fields to update.
    """
    from .router import (
        _ensure_use_case_status_columns,
        build_use_case_update_set_clause,
    )

    use_case_id = payload.get("use_case_id")
    patch = payload.get("patch") or {}
    if not use_case_id or not patch:
        raise ValueError("payload missing use_case_id or patch")

    silver = get_silver_schema()
    _ensure_use_case_status_columns()
    set_clause = build_use_case_update_set_clause(patch)
    if set_clause is None:
        raise ValueError("patch reduced to no recognized fields")

    execute_update(
        f"UPDATE {fqn(silver, 'use_cases')} "
        f"SET {set_clause} "
        f"WHERE id = '{_q(str(use_case_id))}'",
        tag_overrides={
            "submodule": "chat.confirm.update_use_case",
            "user_key": user_key,
        },
    )
    return {
        "use_case_id": use_case_id,
        "fields_changed": sorted(patch.keys()),
    }


def _exec_update_use_case_affiliates(
    payload: dict[str, Any], user_key: str
) -> dict[str, Any]:
    """Apply add/remove deltas to use_case_affiliates.

    Each item in `add` runs through `merge_use_case_affiliate` (idempotent
    upsert); each name in `remove` runs through `delete_use_case_affiliate_row`.
    Operations are independent — one failed delete doesn't block subsequent
    upserts. We collect per-item errors and surface them in the result so
    the FE can show a partial-failure message instead of just "500".
    """
    from .router import (
        delete_use_case_affiliate_row,
        merge_use_case_affiliate,
    )

    use_case_id = payload.get("use_case_id")
    add = payload.get("add") or []
    remove = payload.get("remove") or []
    if not use_case_id or (not add and not remove):
        raise ValueError("payload missing use_case_id or empty add+remove")

    added: list[str] = []
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for item in add:
        name = (item or {}).get("affiliate_name") or (item or {}).get("name")
        if not name:
            errors.append({"op": "add", "name": "?", "error": "missing name"})
            continue
        try:
            merge_use_case_affiliate(
                use_case_id=str(use_case_id),
                affiliate_name=str(name),
                applicability=str(item.get("applicability") or "primary"),
                rationale=str(item.get("rationale") or ""),
            )
            added.append(str(name))
        except Exception as e:
            logger.exception("merge_use_case_affiliate failed for %s", name)
            errors.append({"op": "add", "name": str(name), "error": str(e)})
    for name in remove:
        if not name:
            continue
        try:
            delete_use_case_affiliate_row(str(use_case_id), str(name))
            removed.append(str(name))
        except Exception as e:
            logger.exception(
                "delete_use_case_affiliate_row failed for %s", name
            )
            errors.append({"op": "remove", "name": str(name), "error": str(e)})

    return {
        "use_case_id": use_case_id,
        "added": added,
        "removed": removed,
        "errors": errors,
        # Surfaced again in the FE result banner so the user sees the
        # caveat after they confirm, not just before.
        "user_key": user_key,
    }


def _exec_update_use_case_canonicals(
    payload: dict[str, Any], user_key: str
) -> dict[str, Any]:
    """Apply add/remove deltas to use_case_source_requirements.

    Same shape as `_exec_update_use_case_affiliates` but for canonicals.
    """
    from .router import (
        delete_use_case_source_requirement_row,
        merge_use_case_source_requirement,
    )

    use_case_id = payload.get("use_case_id")
    add = payload.get("add") or []
    remove = payload.get("remove") or []
    if not use_case_id or (not add and not remove):
        raise ValueError("payload missing use_case_id or empty add+remove")

    added: list[str] = []
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for item in add:
        canonical = (item or {}).get("canonical")
        if not canonical:
            errors.append(
                {"op": "add", "name": "?", "error": "missing canonical"}
            )
            continue
        try:
            merge_use_case_source_requirement(
                use_case_id=str(use_case_id),
                required_canonical=str(canonical),
                necessity=str(item.get("necessity") or "must_have"),
                data_need_excerpt=str(item.get("data_need_excerpt") or ""),
                confidence=str(item.get("confidence") or "high"),
            )
            added.append(str(canonical))
        except Exception as e:
            logger.exception(
                "merge_use_case_source_requirement failed for %s", canonical
            )
            errors.append(
                {"op": "add", "name": str(canonical), "error": str(e)}
            )
    for canonical in remove:
        if not canonical:
            continue
        try:
            delete_use_case_source_requirement_row(
                str(use_case_id), str(canonical)
            )
            removed.append(str(canonical))
        except Exception as e:
            logger.exception(
                "delete_use_case_source_requirement_row failed for %s",
                canonical,
            )
            errors.append(
                {"op": "remove", "name": str(canonical), "error": str(e)}
            )

    return {
        "use_case_id": use_case_id,
        "added": added,
        "removed": removed,
        "errors": errors,
        "user_key": user_key,
    }


def _exec_create_use_case(
    payload: dict[str, Any], user_key: str
) -> dict[str, Any]:
    """Three-table create: parent (silver.use_cases) + child mappings.

    Atomicity policy (per A2-4 design notes in chatbot-track-a.md):
    we do a best-effort write with errors surfaced, NOT a hard
    transactional rollback:

      1. Insert parent row first via `insert_use_case_row`. If this
         raises, the whole call fails (no orphan child rows possible).
      2. Loop child affiliate adds, each in its own try/except.
         Successful upserts go in `affiliates_added`; failures land in
         `errors[]`.
      3. Same loop for canonical adds.
      4. Return everything — parent id + per-resource counts + errors.

    A failed child mapping is recoverable (the user can re-add it via
    app_propose_affiliate_mapping / app_propose_canonical_mapping on
    the new use case id), so rolling back the parent on a child
    failure would leave the user worse off than just surfacing the
    issue and letting them retry the mapping.
    """
    from .router import (
        find_use_case_by_name,
        insert_use_case_row,
        merge_use_case_affiliate,
        merge_use_case_source_requirement,
    )

    fields = payload.get("fields") or {}
    affiliates = payload.get("affiliates") or []
    canonicals = payload.get("canonicals") or []
    name = fields.get("use_case_name")
    if not name:
        raise ValueError("payload.fields.use_case_name is required")

    # One last collision check at confirm time. Between propose and
    # confirm a UI user could have created the same name; we re-check
    # so two clicks racing don't both succeed silently. The propose
    # tool already does this guard pre-token-issue.
    existing = find_use_case_by_name(name)
    if existing:
        raise ValueError(
            f"A use case named {name!r} already exists "
            f"(id={existing.get('id')})"
        )

    new_id = insert_use_case_row(
        use_case_name=str(name),
        description=str(fields.get("description") or ""),
        department=str(fields.get("department") or ""),
        category=str(fields.get("category") or ""),
        priority=str(fields.get("priority") or "Medium"),
        business_value=str(fields.get("business_value") or ""),
        estimated_value_usd=fields.get("estimated_value_usd"),
        value_rationale=str(fields.get("value_rationale") or ""),
        status=str(fields.get("status") or "not_started"),
        status_notes=str(fields.get("status_notes") or ""),
    )

    affiliates_added: list[str] = []
    canonicals_added: list[str] = []
    errors: list[dict[str, str]] = []

    for item in affiliates:
        a_name = (item or {}).get("affiliate_name") or (item or {}).get("name")
        if not a_name:
            errors.append({"op": "affiliate", "name": "?", "error": "missing name"})
            continue
        try:
            merge_use_case_affiliate(
                use_case_id=new_id,
                affiliate_name=str(a_name),
                applicability=str(item.get("applicability") or "primary"),
                rationale=str(item.get("rationale") or ""),
            )
            affiliates_added.append(str(a_name))
        except Exception as e:
            logger.exception(
                "create_use_case child merge_affiliate failed for %s", a_name
            )
            errors.append(
                {"op": "affiliate", "name": str(a_name), "error": str(e)}
            )

    for item in canonicals:
        c_name = (item or {}).get("canonical")
        if not c_name:
            errors.append({"op": "canonical", "name": "?", "error": "missing canonical"})
            continue
        try:
            merge_use_case_source_requirement(
                use_case_id=new_id,
                required_canonical=str(c_name),
                necessity=str(item.get("necessity") or "must_have"),
                data_need_excerpt=str(item.get("data_need_excerpt") or ""),
                confidence=str(item.get("confidence") or "high"),
            )
            canonicals_added.append(str(c_name))
        except Exception as e:
            logger.exception(
                "create_use_case child merge_canonical failed for %s", c_name
            )
            errors.append(
                {"op": "canonical", "name": str(c_name), "error": str(e)}
            )

    # `user_key` echoed back so the FE can show "created by you" once
    # audit columns land on use_cases.
    return {
        "use_case_id": new_id,
        "use_case_name": name,
        "affiliates_added": affiliates_added,
        "canonicals_added": canonicals_added,
        "errors": errors,
        "user_key": user_key,
    }


def _exec_update_schema(
    payload: dict[str, Any], user_key: str
) -> dict[str, Any]:
    """Apply a multi-field patch to all (or a filtered subset of)
    silver_schemas rows for one logical schema_name.

    Reuses `update_silver_schema_rows` so the chat write path goes
    through exactly the same SQL helpers as the UI PUT endpoint.
    The propose tool pre-flights the affected catalog list and
    passes it explicitly here — we don't re-derive it inside the
    executor, because between propose and confirm a UI write could
    have changed the silver_schemas membership and we'd update
    something the user didn't see in the diff card.
    """
    from .router import update_silver_schema_rows

    schema_name = payload.get("schema_name")
    if not schema_name:
        raise ValueError("payload.schema_name is required")
    patch = payload.get("patch") or {}
    if not patch:
        raise ValueError("payload.patch must be non-empty")
    catalogs = payload.get("catalogs") or None  # None → all matching rows
    affected = update_silver_schema_rows(
        schema_name=str(schema_name),
        patch=dict(patch),
        catalog_filter=list(catalogs) if catalogs else None,
    )
    return {
        "schema_name": schema_name,
        "catalogs": catalogs,
        "fields_changed": sorted(patch.keys()),
        "rows_affected": affected,
        "user_key": user_key,
    }


_INTENT_EXECUTORS: dict[str, ExecutorFn] = {
    INTENT_UPDATE_USE_CASE_STATUS: _exec_update_use_case_status,
    INTENT_UPDATE_USE_CASE: _exec_update_use_case,
    INTENT_UPDATE_USE_CASE_AFFILIATES: _exec_update_use_case_affiliates,
    INTENT_UPDATE_USE_CASE_CANONICALS: _exec_update_use_case_canonicals,
    INTENT_CREATE_USE_CASE: _exec_create_use_case,
    INTENT_UPDATE_SCHEMA: _exec_update_schema,
}


# ---------------------------------------------------------------------------
# FastAPI router — exposed under /api/chat/confirm
# ---------------------------------------------------------------------------


confirm_router = APIRouter(prefix="/chat/confirm", tags=["chat"])


class ConfirmOut(BaseModel):
    ok: bool
    intent: str
    target_id: str
    result: dict[str, Any] = Field(default_factory=dict)


def _resolve_user_key(headers: DatabricksAppsHeaders) -> str:
    # Duplicated from chat.py to avoid importing the chat module here
    # (which would create an import cycle: chat -> tools -> confirm).
    raw = headers.user_name or headers.user_email or headers.user_id or "local"
    return raw.strip().lower()


# Path parameter is named `confirm_token` (not `token`) on purpose: the
# `get_databricks_headers` Depends has a parameter literally named `token`
# (X-Forwarded-Access-Token). FastAPI's name-based "is this a path param"
# heuristic would mis-flag THAT header param as a path param if our path
# also said `{token}`, then trip the "Path parameters cannot have default
# values" assertion because the header has `= None`. Renaming sidesteps it.
@confirm_router.post(
    "/{confirm_token}",
    operation_id="chatConfirm",
    response_model=ConfirmOut,
)
def confirm(
    headers: HeadersDependency,
    confirm_token: str = Path(..., min_length=8, max_length=64),
) -> ConfirmOut:
    """Validate + consume a token, then perform the proposed write.

    Failure modes (each returns 4xx so the FE can show a useful banner):
      404 - token not found, OR not bound to this user
      409 - token already consumed
      410 - token expired
      400 - intent has no executor (defensive — issue_token guards too)
      500 - executor raised; payload is logged but not echoed in the body
    """
    user_key = _resolve_user_key(headers)
    row = _fetch_token(confirm_token, user_key)
    if not row:
        raise HTTPException(404, "token not found")
    if row.get("consumed_at"):
        raise HTTPException(409, "token already consumed")

    expires_at = row.get("expires_at")
    # Spark returns a python datetime; normalize to UTC for comparison.
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(410, "token expired")

    intent = str(row.get("intent") or "")
    executor = _INTENT_EXECUTORS.get(intent)
    if not executor:
        raise HTTPException(400, f"no executor for intent {intent!r}")

    try:
        payload = json.loads(row.get("payload") or "{}")
    except json.JSONDecodeError as e:
        logger.exception("confirm: payload JSON decode failed")
        raise HTTPException(500, f"corrupt payload: {e}") from e

    # Single-use guard. If we lose the race (two tabs), bail before doing work.
    consumed = _mark_consumed(confirm_token)
    if consumed == 0:
        raise HTTPException(409, "token already consumed")

    try:
        result = executor(payload, user_key)
    except Exception as e:
        logger.exception("confirm executor failed for intent=%s", intent)
        raise HTTPException(500, f"write failed: {e}") from e

    return ConfirmOut(
        ok=True,
        intent=intent,
        target_id=str(row.get("target_id") or ""),
        result=result,
    )
