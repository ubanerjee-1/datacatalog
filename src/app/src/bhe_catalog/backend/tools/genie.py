"""Fallback tool: ask the curated Genie space.

When no app_* tool fits the user's question (arbitrary aggregation, cross-
table filtering we don't have a typed handler for), the model calls this.
We POST to the Genie Conversation API, poll for the SQL + results, and
return both the rendered rows and any chart spec Genie produced.

Two API quirks worth noting:
  1. The first call in a chat thread MUST go through the "start
     conversation" endpoint to get a `conversation_id`. Subsequent calls
     in the same thread reuse it via "create message". We pin the
     conversation_id back to our `chat_conversations.genie_conversation_id`
     so multi-turn refinement ("now break that down by year") works.
  2. Genie returns SQL + a results payload. The results payload uses
     SQL Statement Execution API column manifests (see db.py for the
     same shape) — we collapse it to a list of dicts before returning.

Auth: we use the app's service-principal token (same as everything else
in db.py). Per the chat plan §9 this is acceptable because the Genie
space is read-only and the data we expose is intentionally readable by
all account users. If/when we need per-user row-level security, swap
to the OBO token already threaded into ToolContext.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from pydantic import BaseModel, Field

from ..db import _get_headers, _get_host
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID", "01f13f2d12271caeb5f26d3762ea9d75")

# Polling parameters. Genie can be slow on cold starts (warehouse spinup +
# LLM gen). 90s is generous; we surface progress to the chat as a single
# tool-call pending bubble so the user knows we're waiting.
_POLL_INTERVAL_SEC = 1.5
_POLL_TIMEOUT_SEC = 90
_MAX_RESULT_ROWS = 200      # cap rows we return to the model (token cost)
_MAX_RESULT_COLS = 30       # cap columns we return (token cost)


class GenieAskArgs(BaseModel):
    """The model only writes a question; we add the conversation context."""

    question: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description=(
            "The natural-language data question to ask the curated Genie "
            "space. Be specific — the space covers Unity Catalog inventory "
            "(schemas, tables), business use cases, affiliates, source "
            "systems, and cross-environment consistency. Do NOT use this "
            "for questions that an app_* tool can answer directly."
        ),
    )


def _genie_request(
    method: str,
    path: str,
    body: dict | None = None,
    poll_timeout: int = _POLL_TIMEOUT_SEC,
) -> dict:
    """Thin wrapper that handles the host/auth boilerplate.

    Genie endpoints live under `/api/2.0/genie/spaces/{id}/...` and use
    the same SDK auth as the SQL warehouse. Long-poll style: we expect
    the caller to interpret response status (`PENDING` / `EXECUTING_QUERY`
    / `COMPLETED` / `FAILED`).
    """
    url = f"{_get_host()}{path}"
    headers = _get_headers()
    if method == "POST":
        resp = requests.post(url, headers=headers, json=body or {}, timeout=30)
    else:
        resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Genie API {method} {path} -> {resp.status_code}: {resp.text[:400]}"
        )
    return resp.json()


def _wait_for_message(space_id: str, conv_id: str, msg_id: str) -> dict:
    """Poll a Genie message until it reaches a terminal state.

    We allowlist terminal states rather than denylisting in-progress ones
    because Genie has many transient states (PENDING, ASKING_AI,
    FILTERING_CONTEXT, EXECUTING_QUERY, FETCHING_METADATA, IN_PROGRESS,
    SUBMITTED, ...) and the API surface keeps adding new ones. Treating
    anything-not-terminal as "keep polling" is the only forward-safe
    posture — otherwise a new state silently looks like "done" and we
    parse an empty payload (we hit exactly that with ASKING_AI).
    """
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED"}
    deadline = time.time() + _POLL_TIMEOUT_SEC
    interval = _POLL_INTERVAL_SEC
    while time.time() < deadline:
        msg = _genie_request(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages/{msg_id}",
        )
        status = (msg.get("status") or msg.get("state") or "").upper()
        if status in terminal:
            return msg
        time.sleep(interval)
        interval = min(interval * 1.3, 5.0)
    raise RuntimeError(
        f"Genie message {msg_id} did not reach a terminal state within "
        f"{_POLL_TIMEOUT_SEC}s (last status: {status!r})"
    )


def _fetch_query_result(space_id: str, conv_id: str, msg_id: str, attachment_id: str) -> dict | None:
    """Fetch the SQL execution result for an attachment, if present."""
    try:
        return _genie_request(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages/{msg_id}/attachments/{attachment_id}/query-result",
        )
    except RuntimeError as e:
        logger.warning(f"Genie query-result fetch failed: {e}")
        return None


def _summarize_result(result: dict | None) -> tuple[list[dict], list[str]]:
    """Collapse the SEA-shaped result payload to (rows, columns).

    Returns ([], []) when there is no result block (e.g. the assistant
    chose to answer in pure text without running SQL).
    """
    if not result:
        return [], []
    statement_response = result.get("statement_response") or result
    manifest = statement_response.get("manifest") or {}
    schema = manifest.get("schema") or {}
    columns = [c.get("name") for c in (schema.get("columns") or [])][:_MAX_RESULT_COLS]
    data_array = (statement_response.get("result") or {}).get("data_array") or []
    rows: list[dict] = []
    for raw in data_array[:_MAX_RESULT_ROWS]:
        rows.append({columns[i]: raw[i] for i in range(min(len(columns), len(raw)))})
    return rows, columns


def _genie_ask(args: GenieAskArgs, ctx: ToolContext) -> ToolResult:
    space_id = GENIE_SPACE_ID
    if not space_id:
        return ToolResult(
            ok=False,
            summary="Genie space is not configured (set GENIE_SPACE_ID).",
            data={"error": "no_space_id"},
        )

    try:
        # Start vs. continue: one path if this is the first Genie call in
        # the chat thread, another if we've already pinned a Genie
        # conversation_id on our row.
        conv_id = ctx.genie_conversation_id
        if not conv_id:
            initial = _genie_request(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/start-conversation",
                {"content": args.question},
            )
            conv_id = initial.get("conversation_id") or (initial.get("conversation") or {}).get("id")
            msg_id = initial.get("message_id") or (initial.get("message") or {}).get("id")
            # IMPORTANT: mutate ctx so the chat router can persist the
            # conversation_id back to our chat_conversations row.
            ctx.genie_conversation_id = conv_id
        else:
            created = _genie_request(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/conversations/{conv_id}/messages",
                {"content": args.question},
            )
            msg_id = created.get("message_id") or created.get("id")

        if not conv_id or not msg_id:
            return ToolResult(
                ok=False,
                summary="Genie did not return a conversation/message id.",
                data={"raw": initial if not ctx.genie_conversation_id else created},
            )

        # Poll until the message produced an attachment (text + optional SQL).
        msg = _wait_for_message(space_id, conv_id, msg_id)
    except RuntimeError as e:
        logger.exception("Genie tool failed")
        return ToolResult(ok=False, summary=str(e)[:200], data={"error": str(e)})
    except Exception as e:
        logger.exception("Genie tool unexpected failure")
        return ToolResult(ok=False, summary=f"Genie call failed: {e}", data={"error": str(e)})

    attachments = msg.get("attachments") or []
    text_answer = ""
    sql_text = ""
    rows: list[dict] = []
    columns: list[str] = []
    for att in attachments:
        if "text" in att and not text_answer:
            text_answer = (att["text"] or {}).get("content", "") or ""
        if "query" in att:
            query = att["query"] or {}
            sql_text = query.get("query") or query.get("sql") or ""
            attachment_id = att.get("attachment_id") or att.get("id")
            if attachment_id:
                qr = _fetch_query_result(space_id, conv_id, msg_id, attachment_id)
                rows, columns = _summarize_result(qr)

    summary_bits: list[str] = []
    if rows:
        summary_bits.append(f"{len(rows)} row{'s' if len(rows) != 1 else ''}")
    if sql_text:
        summary_bits.append("SQL")
    if text_answer and not summary_bits:
        summary_bits.append("text answer")
    summary = "Genie returned " + (", ".join(summary_bits) if summary_bits else "no data")

    data = {
        "answer": text_answer or None,
        "sql": sql_text or None,
        "columns": columns or None,
        "rows": rows or None,
        "row_count": len(rows),
        "genie_conversation_id": conv_id,
        "genie_message_id": msg_id,
    }
    return ToolResult(ok=True, summary=summary, data=data)


GENIE_ASK = Tool(
    name="genie_ask",
    description=(
        "Ask the curated 'BHE Catalog Explorer' Genie space a free-form data "
        "question and get back rows + SQL. Use this ONLY when no app_* tool "
        "fits — for arbitrary aggregations, cross-table filters, or "
        "exploratory analysis over Unity Catalog inventory, use cases, "
        "affiliates, source systems, and cross-environment consistency. "
        "Does NOT take filter parameters; phrase the entire question in "
        "natural language. Multi-turn follow-ups in the same chat thread "
        "share Genie context automatically."
    ),
    args_model=GenieAskArgs,
    handler=_genie_ask,
)
