"""In-app chatbot (Track A) router.

Phase A1 — Slice 2 (PR #2): tool-calling Q&A. The model can call
`app_search_use_cases` (typed catalog read) or `genie_ask` (free-form
data question against the curated Genie space). The dispatcher loop
runs at most `_MAX_TOOL_ITERATIONS` model turns before forcing a stop,
so a misbehaving model can't spin forever.

Endpoints
---------
POST /api/chat/messages               - send a user message, stream assistant response (SSE)
GET  /api/chat/conversations          - list current user's conversations (most recent first)
GET  /api/chat/conversations/{id}     - get one conversation's messages (asc by created_at)
POST /api/chat/conversations/{id}/title  - rename a conversation
DELETE /api/chat/conversations/{id}   - soft-delete a conversation

Isolation contract
------------------
Every read filters by `user_key` derived from the Databricks Apps forwarded
headers. The user key resolution is centralized in `_resolve_user_key` so we
never accidentally expose another user's chats. Local dev (no forwarded
headers) collapses to a single "local" user — that's intentional, the local
DEV warehouse holds disposable data.

Streaming wire format (PR #2 expansion)
---------------------------------------
The `POST /messages` endpoint returns `text/event-stream`. Events are JSON
documents wrapped in `data: ...\n\n` frames. Event types:

  {"type": "start", "conversation_id": "...", ...}
  {"type": "token", "text": "..."}                          # 0..N text fragments
  {"type": "tool_call", "tool_call_id": "...", "name": "...", "args": {...}}
  {"type": "tool_result", "tool_call_id": "...", "name": "...",
   "ok": true, "summary": "...", "data": {...}, "citations": [...]}
  {"type": "done",  "assistant_message_id": "...", "finish_reason": "stop", ...}
  {"type": "error", "error": "..."}

Persistence is best-effort and happens AFTER the stream closes so a slow
warehouse can't slow down the user-perceived first-token latency. If the
DELETE/INSERT fails, the user still saw their answer — we log + drop the
row rather than retrying inline.

Persistence shape (one row per LLM-visible message)
---------------------------------------------------
We store one chat_messages row per OpenAI-protocol message so rehydration
is "load rows, send to model" with no reconstruction. Roles in play:

  user        - the user's prompt (role=user, parts=[{type:text,...}])
  assistant   - the model's response. May contain tool_calls in `parts`
                (parts=[{type:text,...}, {type:tool_call, id, name, args}])
  tool        - one row per tool result the model received
                (parts=[{type:tool_result, tool_call_id, name, ok, data, ...}])

We DO NOT replay tool results to the model on subsequent turns in PR #2 —
the rehydration in `_build_llm_messages` only forwards user + assistant
text, which is enough for follow-up questions and keeps token cost down.
Multi-turn references to past tool outputs ("show me more from the search
above") will be handled by Genie's own server-side conversation context
(via `genie_conversation_id`) and, if needed for app_* tools, a Phase
A1.5 polish.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .core._headers import HeadersDependency, DatabricksAppsHeaders
from .db import execute_query, execute_update, fqn, get_silver_schema
from .llm import CHAT_LLM_ENDPOINT, stream_chat
from .tools import TOOLS, ToolContext, ToolResult

logger = logging.getLogger(__name__)

chat_router = APIRouter(prefix="/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# System prompt — Phase A1 PR #2 (tool-calling enabled)
# ---------------------------------------------------------------------------
# Tracks docs/plans/chatbot-track-a.md Appendix A. Two rules matter most:
#   1. Always ground entity-level answers in tool calls.
#   2. Pick app_* tools first; reach for genie_ask only when nothing else fits.
# Keep this prompt close to the tool descriptions — drift between the two
# (e.g. recommending a tool name we don't have) silently degrades quality.

SYSTEM_PROMPT_A1 = (
    "You are the BHE Data Catalog assistant, embedded in an internal web app "
    "that catalogs Berkshire Hathaway Energy's Unity Catalog data assets, "
    "business use cases, affiliates, and source systems. You help BHE users "
    "find, understand, and reason about that catalog.\n\n"
    "Tool selection — pick the most specific app_* tool that fits before "
    "falling back to genie_ask:\n"
    "- Use cases: `app_search_use_cases` to find by text/department/affiliate; "
    "  `app_get_use_case` to drill into one by id (always do this when the "
    "  user asks for details about a use case the search returned).\n"
    "- Schemas: `app_search_schemas` to filter by environment/program/domain "
    "  (set missing_definition=true for AI-coverage gap questions); "
    "  `app_get_schema` for a single logical schema's rollup.\n"
    "- Dimensions: `app_list_affiliates` for the operating-company list; "
    "  `app_list_source_systems` for the canonical source catalog.\n"
    "- Portfolio: `app_value_summary` for total/ready/gap value KPIs; "
    "  `app_value_source_rollup` for 'which sources unlock the most $' "
    "  (pass only_missing=true for the gap shortlist); "
    "  `app_gaps_matrix` for affiliate × source coverage.\n"
    "- Research: `app_research_use_case` returns a structured brief — "
    "  similar existing use cases, plus suggested department / "
    "  category / value range / affiliates / canonical sources — for a "
    "  free-text topic. ALWAYS call this BEFORE `app_propose_use_case`; "
    "  also useful when the user asks 'what use cases do we have like "
    "  X?' as a richer alternative to `app_search_use_cases`.\n"
    "- Schema research: `app_research_schema` returns a brief for a "
    "  named schema — current values across catalogs, a sample of its "
    "  tables, peer schemas with their domain/department/sensitivity "
    "  rolled up by frequency, and 1-3 sample peer definitions to "
    "  mimic the catalog's writing style. ALWAYS call this BEFORE "
    "  `app_propose_schema_update`.\n"
    "- `genie_ask` ONLY when no app_* tool fits — arbitrary aggregations, "
    "  cross-table filters, or exploratory questions over Unity Catalog "
    "  inventory. Phrase the whole question in natural language.\n\n"
    "Grounding rules:\n"
    "- Never invent IDs, names, counts, percentages, or URLs. If you don't "
    "  have a tool result that backs a claim, do not make the claim.\n"
    "- When a tool returns entities, refer to them by name. The UI turns "
    "  the citations the tool returned into clickable links.\n"
    "- Prefer multiple targeted tool calls over one giant genie_ask. The "
    "  typed tools cite, link, and reconcile with the UI's own numbers.\n\n"
    "Write rules (propose/confirm):\n"
    "- HARD RULE: NO DELETES VIA CHAT, EVER. There is no "
    "  `app_propose_delete_*` tool and there will not be one. If the "
    "  user asks to delete a use case, schema, affiliate, mapping, or "
    "  any other entity, refuse politely and direct them to the UI: "
    "  for use cases that's the Value & Readiness drawer's delete "
    "  button; for schemas it's the Edit Center page. Explain that "
    "  destructive actions stay in the UI by policy. This is non-"
    "  negotiable; do NOT propose, do NOT 'help' by archiving or "
    "  setting status to 'deprecated' as a workaround unless the "
    "  user explicitly asks for that specific edit.\n"
    "- Six write tools are available:\n"
    "  - `app_propose_status_change` — set delivery status "
    "    (not_started / in_progress / delivered / on_hold), with optional "
    "    notes. Use this for ANY status change.\n"
    "  - `app_propose_use_case_update` — edit one or more scalar fields "
    "    on an EXISTING use case: name, description, department, "
    "    category, business_value, value_rationale, priority, "
    "    estimated_value_usd. Batch all the fields the user wants to "
    "    change into ONE call.\n"
    "  - `app_propose_affiliate_mapping` — add/remove affiliate mappings "
    "    on a use case. Pass `add` (with name + applicability) and/or "
    "    `remove` (just names). Looks up affiliate names against the "
    "    affiliates dim — use app_list_affiliates if unsure of the "
    "    exact name.\n"
    "  - `app_propose_canonical_mapping` — add/remove canonical source "
    "    requirements. Pass `add` (with canonical + necessity + "
    "    data_need_excerpt) and/or `remove` (just canonical names). "
    "    Use app_list_source_systems if unsure of the exact name.\n"
    "  - `app_propose_use_case` — CREATE a brand-new use case in ONE "
    "    confirmation. Pass `use_case_name` (required, must be unique) "
    "    plus as many of description / department / category / "
    "    business_value / value_rationale / priority / "
    "    estimated_value_usd as you can gather. STRONGLY prefer "
    "    passing initial `affiliates` (with applicability + rationale) "
    "    and `canonicals` (with necessity + data_need_excerpt) inline "
    "    — a use case with no affiliates won't show up on any "
    "    affiliate's coverage view, and one with no canonicals has no "
    "    readiness signal.\n"
    "    Workflow for create: ALWAYS call `app_research_use_case` "
    "    FIRST with the user's topic (and target_affiliate if they "
    "    named one). The brief tells you (a) whether a near-twin "
    "    already exists — warn the user before duplicating; (b) the "
    "    department/category/value conventions for this kind of work; "
    "    (c) which affiliates and canonicals similar UCs use, ranked "
    "    by frequency. Use those as your starting values, then ASK "
    "    the user to confirm/adjust before calling the propose tool. "
    "    DO NOT propose with placeholder values; it's better to ask "
    "    one or two questions than to ship a card the user has to "
    "    reject.\n"
    "  - `app_propose_schema_update` — edit a SCHEMA's AI-generated "
    "    metadata: ai_definition, business_friendly_name, "
    "    suggested_department, suggested_domain, data_sensitivity. "
    "    By default updates ALL physical catalogs the schema lives in "
    "    (dev/qa/prod) so the definition stays consistent across "
    "    environments — pass `catalog_filter` only if the user "
    "    explicitly named one or more catalogs. The diff card shows "
    "    per-catalog before-values; if catalogs hold DIFFERENT "
    "    current values for a field, the tool flags it as divergent "
    "    and you must surface that to the user before they confirm "
    "    ('this will collapse dev's X and prod's Y to your new "
    "    value Z'). Workflow for schema edits: ALWAYS call "
    "    `app_research_schema` FIRST. The brief gives you peer "
    "    schemas, their conventions, and 1-3 sample peer definitions "
    "    to mimic the catalog's writing style. Use the brief's "
    "    `suggested_domain[0]` / `suggested_department[0]` as "
    "    starting points and `sample_definitions` as style "
    "    references when proposing a new ai_definition.\n"
    "- Calling a propose_* tool does NOT change anything. It returns a "
    "  confirmation card that the user must explicitly approve in the chat "
    "  UI before the write happens. Always tell the user 'I've prepared "
    "  the change — click Confirm in the card below to apply it.' Never "
    "  claim the change has happened until you see the confirm result.\n"
    "- Before proposing edits, ALWAYS call `app_get_use_case` first so "
    "  the diff card shows real before-vs-after values, and so you don't "
    "  propose values that already match (the tools will refuse no-ops). "
    "  Before proposing a CREATE, do a quick `app_search_use_cases` on "
    "  the proposed name (or close variants) to make sure you're not "
    "  recreating something that already exists.\n"
    "- For mapping changes, when removing affiliates or canonicals, "
    "  ALSO mention to the user that the catalog reseed job may re-add "
    "  the row if the use case description still implies it — they may "
    "  want to also edit the description to make the removal stick.\n"
    "- If there is more than one plausible match, ASK which one — never "
    "  guess at writes.\n"
    "- Editing data_requirements as a JSON array on an existing use "
    "  case is not yet exposed via chat (use "
    "  `app_propose_canonical_mapping` instead — the canonical-source "
    "  requirements ARE the structured equivalent).\n\n"
    "Tone: concise, professional, no marketing fluff. Prefer short bullets "
    "over paragraphs. Plain English by default; use technical terms only "
    "when the user does first."
)


# Cap on how many model -> tool-call -> model loops we run for a single
# user turn. Tool-calling models are well-behaved with Claude 4.x but a
# bad prompt could in principle bounce forever; this is the safety net.
_MAX_TOOL_ITERATIONS = 6
# Cap on how many bytes of a tool result we feed back to the model. Genie
# can return 200 rows — that's intentional for the user-facing card, but
# we don't want the model to drown in raw rows on a follow-up turn.
_MAX_TOOL_RESULT_CHARS = 16_000


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChatPostIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None,
        description="Existing conversation to append to. Omit to start a new one.",
    )


class ChatMessageOut(BaseModel):
    message_id: str
    conversation_id: str
    role: str
    content: str
    # `parts` is the structured payload (tool_call / tool_result entries
    # for non-text turns). FE uses this to render tool-call cards on
    # conversation reload. Always present, even for plain text turns
    # (single text part); the FE shouldn't have to special-case absence.
    parts: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    created_at: str | None = None


class ChatConversationOut(BaseModel):
    conversation_id: str
    title: str
    created_at: str | None
    updated_at: str | None
    last_message_at: str | None
    message_count: int


class ChatConversationDetailOut(BaseModel):
    conversation: ChatConversationOut
    messages: list[ChatMessageOut]


class ChatTitleIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_user_key(headers: DatabricksAppsHeaders) -> str:
    """Stable lower-cased key for chat ownership.

    Order: forwarded preferred-username (the Databricks workspace username)
    > forwarded email > forwarded user_id > "local". The Apps platform
    always populates at least one of the first three when running deployed.
    """
    raw = headers.user_name or headers.user_email or headers.user_id or "local"
    return raw.strip().lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sql_quote(s: str) -> str:
    """SQL string-literal escape (single-quote doubling).

    Matches the convention used in `bootstrap_tables.py`. Spark interprets
    backslash sequences inside single-quoted literals (e.g. `\\n` -> newline),
    so user-pasted literal backslashes may render differently after
    round-tripping. That's acceptable for plain-text chat — we don't preserve
    code-block formatting at the SQL boundary today.
    """
    return s.replace("'", "''")


def _conversations_table() -> str:
    return fqn(get_silver_schema(), "chat_conversations")


def _messages_table() -> str:
    return fqn(get_silver_schema(), "chat_messages")


def _derive_title(content: str, max_len: int = 80) -> str:
    """First sentence-ish, no LLM call. We can upgrade later."""
    text = " ".join(content.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _parse_parts(raw: Any) -> list[dict[str, Any]]:
    """Decode the `parts` JSON column for a row.

    Defensive: returns [] for missing/empty/malformed values rather than
    raising, because a single bad row should not break a whole
    conversation reload.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        loaded = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(loaded, list):
            return loaded
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _fetch_conversation(conversation_id: str, user_key: str) -> dict[str, Any] | None:
    rows = execute_query(
        f"""
        SELECT conversation_id, user_key, title, genie_conversation_id,
               created_at, updated_at, last_message_at, message_count, is_deleted
        FROM {_conversations_table()}
        WHERE conversation_id = '{_sql_quote(conversation_id)}'
          AND user_key = '{_sql_quote(user_key)}'
          AND COALESCE(is_deleted, false) = false
        LIMIT 1
        """,
        tag_overrides={"submodule": "chat.fetch_conversation"},
    )
    return rows[0] if rows else None


def _fetch_messages(conversation_id: str, user_key: str) -> list[dict[str, Any]]:
    return execute_query(
        f"""
        SELECT message_id, conversation_id, role, content, parts, model,
               finish_reason, prompt_tokens, completion_tokens, latency_ms,
               error, created_at
        FROM {_messages_table()}
        WHERE conversation_id = '{_sql_quote(conversation_id)}'
          AND user_key = '{_sql_quote(user_key)}'
        ORDER BY created_at ASC, message_id ASC
        """,
        tag_overrides={"submodule": "chat.fetch_messages"},
    )


def _set_genie_conversation_id(conversation_id: str, genie_conv_id: str) -> None:
    """Pin Genie's stateful conversation_id back to our row.

    Idempotent: we only write if NULL, so the same Genie thread stays
    bound to our chat thread for its whole lifetime.
    """
    execute_update(
        f"""
        UPDATE {_conversations_table()}
        SET genie_conversation_id = '{_sql_quote(genie_conv_id)}'
        WHERE conversation_id = '{_sql_quote(conversation_id)}'
          AND genie_conversation_id IS NULL
        """,
        tag_overrides={"submodule": "chat.bind_genie_conversation"},
    )


def _insert_conversation(
    conversation_id: str, user_key: str, title: str
) -> None:
    """Create a new chat thread row.

    Uses an explicit column list so reseeded warehouses (where the DDL
    order matches bootstrap_tables.py) and existing warehouses (where
    `genie_conversation_id` was added via ALTER TABLE and lives at the
    end) both work without drift. We hit a real bug positionally
    inserting after the ALTER — the new column ended up at the tail
    rather than mid-table, mis-aligning every subsequent value.
    """
    now = _now_iso()
    execute_update(
        f"""
        INSERT INTO {_conversations_table()}
            (conversation_id, user_key, title, genie_conversation_id,
             created_at, updated_at, last_message_at, message_count, is_deleted)
        VALUES (
            '{_sql_quote(conversation_id)}',
            '{_sql_quote(user_key)}',
            '{_sql_quote(title)}',
            NULL,
            TIMESTAMP '{now}',
            TIMESTAMP '{now}',
            TIMESTAMP '{now}',
            0,
            false
        )
        """,
        tag_overrides={"submodule": "chat.insert_conversation"},
    )


def _bump_conversation(conversation_id: str, delta_messages: int) -> None:
    """Update last_message_at + counter after each new message pair.

    We use a SET expression that increments rather than re-counting because
    the alternative (SELECT COUNT(*) ... then UPDATE) is two round-trips.
    """
    now = _now_iso()
    execute_update(
        f"""
        UPDATE {_conversations_table()}
        SET last_message_at = TIMESTAMP '{now}',
            updated_at      = TIMESTAMP '{now}',
            message_count   = COALESCE(message_count, 0) + {int(delta_messages)}
        WHERE conversation_id = '{_sql_quote(conversation_id)}'
        """,
        tag_overrides={"submodule": "chat.bump_conversation"},
    )


def _insert_message(
    *,
    message_id: str,
    conversation_id: str,
    user_key: str,
    role: str,
    content: str,
    parts: list[dict[str, Any]] | None = None,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    finish_reason: str = "",
    error: str = "",
) -> None:
    """Persist one chat_messages row.

    `content` is the plain-text body (the dominant text part — empty
    string for tool messages and for assistant turns that are pure
    tool_calls). `parts` is the structured JSON-serializable payload;
    if omitted we synthesize a single text part from `content` so old
    callers keep working.
    """
    if parts is None:
        parts = [{"type": "text", "text": content}]
    parts_json = json.dumps(parts, ensure_ascii=False)
    execute_update(
        f"""
        INSERT INTO {_messages_table()}
            (message_id, conversation_id, user_key, role, content, parts,
             model, prompt_tokens, completion_tokens, latency_ms,
             finish_reason, error, created_at)
        VALUES (
            '{_sql_quote(message_id)}',
            '{_sql_quote(conversation_id)}',
            '{_sql_quote(user_key)}',
            '{_sql_quote(role)}',
            '{_sql_quote(content)}',
            '{_sql_quote(parts_json)}',
            '{_sql_quote(model)}',
            {int(prompt_tokens)},
            {int(completion_tokens)},
            {int(latency_ms)},
            '{_sql_quote(finish_reason)}',
            '{_sql_quote(error)}',
            TIMESTAMP '{_now_iso()}'
        )
        """,
        tag_overrides={"submodule": "chat.insert_message"},
    )


def _build_llm_messages(
    history: list[dict[str, Any]], new_user_content: str
) -> list[dict[str, Any]]:
    """Construct the messages array for the LLM call.

    Rehydration rules (PR #2):
      - Always prepend the system prompt.
      - Forward role=user and role=assistant text.
      - Skip role=tool rows AND assistant turns whose `content` is empty
        (those are pure tool_call frames). The cost: the model can't
        directly reference past tool outputs by id. The benefit: half
        the token cost on follow-up turns and no rehydration of the
        OpenAI tool_call_id linkage (which is fragile across providers).
      - Drop any assistant row with an error set so we don't seed the
        model with stale failures.
    """
    msgs: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT_A1}]
    for row in history:
        role = (row.get("role") or "").lower()
        if role not in ("user", "assistant"):
            continue
        if (row.get("error") or "").strip():
            continue
        content = (row.get("content") or "").strip()
        if not content:
            continue
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": new_user_content})
    return msgs


def _truncate_for_model(s: str, limit: int = _MAX_TOOL_RESULT_CHARS) -> str:
    """Cap a tool-result JSON string before feeding it to the model."""
    if len(s) <= limit:
        return s
    return s[: limit - 80] + "\n... [truncated for context limit] ..."


def _execute_tool_call(
    name: str,
    args_json: str,
    ctx: ToolContext,
) -> ToolResult:
    """Look up a tool by name, validate args, run it, normalize errors.

    Three failure modes are normalized to ToolResult(ok=False) instead of
    raising so the dispatcher's loop is straight-line:
      1. Unknown tool name — model hallucinated.
      2. Args don't validate — model produced invalid JSON or wrong shape.
      3. Handler crashed — bug in our code or upstream API.
    """
    tool = next((t for t in TOOLS if t.name == name), None)
    if tool is None:
        return ToolResult(
            ok=False,
            summary=f"Unknown tool '{name}'",
            data={"error": "unknown_tool", "available": [t.name for t in TOOLS]},
        )
    try:
        raw = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return ToolResult(
            ok=False,
            summary=f"Tool args were not valid JSON: {e}",
            data={"error": "invalid_args_json", "raw": args_json[:500]},
        )
    try:
        args = tool.args_model.model_validate(raw)
    except Exception as e:
        return ToolResult(
            ok=False,
            summary=f"Tool args failed validation: {e}",
            data={"error": "invalid_args", "raw": raw},
        )
    try:
        return tool.handler(args, ctx)
    except Exception as e:
        logger.exception(f"Tool '{name}' crashed")
        return ToolResult(
            ok=False,
            summary=f"Tool '{name}' crashed: {e}",
            data={"error": "handler_exception", "detail": str(e)},
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@chat_router.post("/messages", operation_id="chatPostMessage")
def post_message(payload: ChatPostIn, headers: HeadersDependency):
    """Send a user message and stream the assistant response.

    Returns Server-Sent Events. The first event is `start` (so the client
    can pin the conversation/message IDs even before any tokens arrive),
    followed by a stream of `token` events, terminated by either `done`
    or `error`.
    """
    user_key = _resolve_user_key(headers)
    user_content = payload.content.strip()
    if not user_content:
        raise HTTPException(status_code=400, detail="Empty message content")

    # Resolve / create conversation up front so we can include the IDs in
    # the very first SSE frame.
    conversation_id = payload.conversation_id
    is_new_conversation = False
    history: list[dict[str, Any]] = []
    if conversation_id:
        conv = _fetch_conversation(conversation_id, user_key)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        history = _fetch_messages(conversation_id, user_key)
    else:
        conversation_id = f"conv_{uuid.uuid4().hex[:16]}"
        title = _derive_title(user_content)
        try:
            _insert_conversation(conversation_id, user_key, title)
            is_new_conversation = True
        except Exception:
            logger.exception("Failed to create chat conversation row")
            raise HTTPException(status_code=500, detail="Could not create conversation")

    user_message_id = f"msg_{uuid.uuid4().hex[:16]}"
    assistant_message_id = f"msg_{uuid.uuid4().hex[:16]}"

    # Pre-existing genie thread on this conversation row, if any. The
    # ToolContext is mutated by the genie tool on first call; we read
    # the final value back after the dispatcher loop and persist it.
    initial_genie_conv_id: str | None = None
    if not is_new_conversation:
        existing = _fetch_conversation(conversation_id, user_key)
        if existing:
            initial_genie_conv_id = existing.get("genie_conversation_id") or None

    tool_ctx = ToolContext(
        user_key=user_key,
        user_email=headers.user_email,
        conversation_id=conversation_id,
        genie_conversation_id=initial_genie_conv_id,
        # `headers.token` is a SecretStr — call get_secret_value() lazily
        # in the tool that actually needs it (none in PR #2; Genie uses
        # the app SP per the plan §9). We pass the raw string here for
        # forward compatibility.
        user_obo_token=(
            headers.token.get_secret_value() if headers.token else None
        ),
    )

    llm_messages = _build_llm_messages(history, user_content)
    tool_definitions = [t.llm_definition() for t in TOOLS]

    def _event(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    def _generate() -> Iterator[bytes]:
        # 1. Frame the conversation/message IDs immediately so the client
        #    can attach them to the optimistic UI bubble.
        yield _event(
            {
                "type": "start",
                "conversation_id": conversation_id,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "is_new_conversation": is_new_conversation,
            }
        )

        # Aggregates collected across all dispatcher iterations, used for
        # the final assistant message persistence + done frame.
        assembled_text: list[str] = []           # plain-text tokens of the FINAL assistant turn
        all_tool_calls: list[dict[str, Any]] = []  # for persisting in assistant.parts
        all_tool_results: list[dict[str, Any]] = []  # one per tool execution, for persistence
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason = ""
        error_msg = ""
        started = time.time()

        # Dispatcher loop. Each iteration runs one model call. If the
        # model finishes with `tool_calls`, we execute them, append the
        # results to `llm_messages`, and loop. Otherwise we exit.
        try:
            for iteration in range(_MAX_TOOL_ITERATIONS):
                # Per-iteration buffers for streaming tool-call deltas.
                # The model can emit multiple parallel tool calls (Claude
                # sometimes does); each lives in its own slot keyed by
                # the `index` field.
                turn_text: list[str] = []
                tool_buffers: dict[int, dict[str, str]] = {}
                turn_finish_reason = ""

                for ev in stream_chat(
                    llm_messages,
                    tools=tool_definitions,
                    tool_choice="auto",
                ):
                    t = ev.get("type")
                    if t == "token":
                        text = ev.get("text") or ""
                        if text:
                            turn_text.append(text)
                            yield _event({"type": "token", "text": text})
                    elif t == "tool_call_delta":
                        idx = int(ev.get("index", 0))
                        slot = tool_buffers.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if "id" in ev:
                            slot["id"] = ev["id"]
                        if "name" in ev:
                            slot["name"] = ev["name"]
                        if "arguments" in ev:
                            slot["arguments"] += ev["arguments"]
                    elif t == "done":
                        turn_finish_reason = ev.get("finish_reason", "stop")
                        prompt_tokens += int(ev.get("prompt_tokens", 0) or 0)
                        completion_tokens += int(ev.get("completion_tokens", 0) or 0)
                    elif t == "error":
                        error_msg = ev.get("error", "Unknown model error")
                        break

                if error_msg:
                    break

                # If the model produced no tool calls this turn, it's done.
                if not tool_buffers:
                    assembled_text = turn_text
                    finish_reason = turn_finish_reason or "stop"
                    break

                # The model wants to call tools. Append its assistant
                # message (with tool_calls) to the conversation so the
                # next iteration can include the tool responses.
                assistant_tool_calls = []
                for idx in sorted(tool_buffers.keys()):
                    slot = tool_buffers[idx]
                    # Some models emit blank id when finish_reason isn't
                    # tool_calls; skip those (defensive).
                    if not slot["name"]:
                        continue
                    if not slot["id"]:
                        slot["id"] = f"call_{uuid.uuid4().hex[:12]}"
                    assistant_tool_calls.append({
                        "id": slot["id"],
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        },
                    })

                if not assistant_tool_calls:
                    assembled_text = turn_text
                    finish_reason = turn_finish_reason or "stop"
                    break

                llm_messages.append({
                    "role": "assistant",
                    "content": "".join(turn_text) or None,
                    "tool_calls": assistant_tool_calls,
                })
                # Track for the final assistant-message persistence.
                all_tool_calls.extend(assistant_tool_calls)
                if turn_text:
                    # Pre-tool reasoning text — preserve in the assistant
                    # message so the user sees the model's intent before
                    # the tool fires (Claude often narrates what it's
                    # about to do). We've already streamed it as tokens.
                    assembled_text.extend(turn_text)

                # Execute every tool call and stream the result back.
                for tc in assistant_tool_calls:
                    name = tc["function"]["name"]
                    args_json = tc["function"]["arguments"] or "{}"
                    try:
                        args_parsed = json.loads(args_json)
                    except json.JSONDecodeError:
                        args_parsed = {"_raw": args_json}
                    yield _event({
                        "type": "tool_call",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "args": args_parsed,
                    })

                    result = _execute_tool_call(name, args_json, tool_ctx)
                    yield _event({
                        "type": "tool_result",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "ok": result.ok,
                        "summary": result.summary,
                        "data": result.data,
                        "citations": result.citations,
                        "chart_spec": result.chart_spec,
                    })

                    # Feed the tool result back to the model on the next
                    # turn. We send a JSON serialization (truncated) so
                    # the model can reason over the structured payload.
                    tool_payload = {
                        "ok": result.ok,
                        "summary": result.summary,
                        "data": result.data,
                    }
                    serialized = _truncate_for_model(
                        json.dumps(tool_payload, ensure_ascii=False, default=str)
                    )
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": serialized,
                    })
                    all_tool_results.append({
                        "tool_call_id": tc["id"],
                        "name": name,
                        "args": args_parsed,
                        "ok": result.ok,
                        "summary": result.summary,
                        "data": result.data,
                        "citations": result.citations,
                        "chart_spec": result.chart_spec,
                    })

                # Loop back: model gets to see the tool results and
                # either finalize text or call more tools.
            else:
                # Loop fell through without a final assistant message.
                # Force a friendly cap message so the user isn't left hanging.
                cap_msg = (
                    "I hit my tool-call limit for this turn. "
                    "Try asking again with a more specific question."
                )
                yield _event({"type": "token", "text": cap_msg})
                assembled_text = [cap_msg]
                finish_reason = "tool_call_cap"

        except Exception as e:  # pragma: no cover - safety net
            logger.exception("Streaming generator crashed")
            error_msg = f"Internal error: {e}"

        latency_ms = int((time.time() - started) * 1000)
        full_text = "".join(assembled_text).strip()

        # 2. Persist all messages best-effort. We store the user prompt,
        #    one row per executed tool (role=tool with structured parts),
        #    and one final assistant row with the full text + a record
        #    of tool_calls in `parts`. This shape lets the UI replay the
        #    tool-call cards on conversation reload without having to
        #    parse model-specific deltas.
        message_count_inserted = 0
        try:
            _insert_message(
                message_id=user_message_id,
                conversation_id=conversation_id,
                user_key=user_key,
                role="user",
                content=user_content,
            )
            message_count_inserted += 1

            for tr in all_tool_results:
                _insert_message(
                    message_id=f"msg_{uuid.uuid4().hex[:16]}",
                    conversation_id=conversation_id,
                    user_key=user_key,
                    role="tool",
                    content=tr["summary"],
                    parts=[{
                        "type": "tool_result",
                        "tool_call_id": tr["tool_call_id"],
                        "name": tr["name"],
                        "args": tr["args"],
                        "ok": tr["ok"],
                        "summary": tr["summary"],
                        "data": tr["data"],
                        "citations": tr["citations"],
                        "chart_spec": tr["chart_spec"],
                    }],
                )
                message_count_inserted += 1

            assistant_parts: list[dict[str, Any]] = []
            if full_text:
                assistant_parts.append({"type": "text", "text": full_text})
            for tc in all_tool_calls:
                assistant_parts.append({
                    "type": "tool_call",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "args_json": tc["function"]["arguments"],
                })

            _insert_message(
                message_id=assistant_message_id,
                conversation_id=conversation_id,
                user_key=user_key,
                role="assistant",
                content=full_text,
                parts=assistant_parts or [{"type": "text", "text": ""}],
                model=CHAT_LLM_ENDPOINT,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                finish_reason=finish_reason,
                error=error_msg,
            )
            message_count_inserted += 1
            _bump_conversation(conversation_id, delta_messages=message_count_inserted)

            # If the genie tool ran for the first time this turn, pin
            # its conversation_id to our row so future Genie calls in
            # this thread reuse the same server-side context.
            if (
                tool_ctx.genie_conversation_id
                and tool_ctx.genie_conversation_id != initial_genie_conv_id
            ):
                _set_genie_conversation_id(
                    conversation_id, tool_ctx.genie_conversation_id
                )
        except Exception:
            logger.exception("Chat persistence failed (user_key=%s)", user_key)

        # 3. Final frame.
        if error_msg and not full_text:
            yield _event({"type": "error", "error": error_msg})
        else:
            yield _event(
                {
                    "type": "done",
                    "assistant_message_id": assistant_message_id,
                    "finish_reason": finish_reason or "stop",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "latency_ms": latency_ms,
                }
            )

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering; without this, nginx-style intermediaries
            # (and Vite's dev proxy) can hold the whole response until done.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@chat_router.get(
    "/conversations",
    response_model=list[ChatConversationOut],
    operation_id="chatListConversations",
)
def list_conversations(headers: HeadersDependency, limit: int = 50):
    user_key = _resolve_user_key(headers)
    limit = max(1, min(limit, 200))
    rows = execute_query(
        f"""
        SELECT conversation_id, title, created_at, updated_at,
               last_message_at, message_count
        FROM {_conversations_table()}
        WHERE user_key = '{_sql_quote(user_key)}'
          AND COALESCE(is_deleted, false) = false
        ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC
        LIMIT {limit}
        """,
        tag_overrides={"submodule": "chat.list_conversations"},
    )
    return [
        ChatConversationOut(
            conversation_id=r["conversation_id"],
            title=r.get("title") or "(untitled)",
            created_at=str(r["created_at"]) if r.get("created_at") else None,
            updated_at=str(r["updated_at"]) if r.get("updated_at") else None,
            last_message_at=(
                str(r["last_message_at"]) if r.get("last_message_at") else None
            ),
            message_count=int(r.get("message_count") or 0),
        )
        for r in rows
    ]


@chat_router.get(
    "/conversations/{conversation_id}",
    response_model=ChatConversationDetailOut,
    operation_id="chatGetConversation",
)
def get_conversation(
    headers: HeadersDependency,
    conversation_id: str = Path(..., min_length=1, max_length=64),
):
    user_key = _resolve_user_key(headers)
    conv = _fetch_conversation(conversation_id, user_key)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = _fetch_messages(conversation_id, user_key)
    return ChatConversationDetailOut(
        conversation=ChatConversationOut(
            conversation_id=conv["conversation_id"],
            title=conv.get("title") or "(untitled)",
            created_at=str(conv["created_at"]) if conv.get("created_at") else None,
            updated_at=str(conv["updated_at"]) if conv.get("updated_at") else None,
            last_message_at=(
                str(conv["last_message_at"]) if conv.get("last_message_at") else None
            ),
            message_count=int(conv.get("message_count") or 0),
        ),
        messages=[
            ChatMessageOut(
                message_id=r["message_id"],
                conversation_id=r["conversation_id"],
                role=r["role"],
                content=r.get("content") or "",
                parts=_parse_parts(r.get("parts")),
                model=r.get("model") or None,
                finish_reason=r.get("finish_reason") or None,
                prompt_tokens=int(r["prompt_tokens"]) if r.get("prompt_tokens") is not None else None,
                completion_tokens=(
                    int(r["completion_tokens"]) if r.get("completion_tokens") is not None else None
                ),
                latency_ms=int(r["latency_ms"]) if r.get("latency_ms") is not None else None,
                error=r.get("error") or None,
                created_at=str(r["created_at"]) if r.get("created_at") else None,
            )
            for r in rows
        ],
    )


@chat_router.post(
    "/conversations/{conversation_id}/title",
    response_model=ChatConversationOut,
    operation_id="chatRenameConversation",
)
def rename_conversation(
    payload: ChatTitleIn,
    headers: HeadersDependency,
    conversation_id: str = Path(..., min_length=1, max_length=64),
):
    user_key = _resolve_user_key(headers)
    if not _fetch_conversation(conversation_id, user_key):
        raise HTTPException(status_code=404, detail="Conversation not found")
    execute_update(
        f"""
        UPDATE {_conversations_table()}
        SET title = '{_sql_quote(payload.title.strip())}',
            updated_at = TIMESTAMP '{_now_iso()}'
        WHERE conversation_id = '{_sql_quote(conversation_id)}'
          AND user_key = '{_sql_quote(user_key)}'
        """,
        tag_overrides={"submodule": "chat.rename_conversation"},
    )
    conv = _fetch_conversation(conversation_id, user_key)
    assert conv is not None
    return ChatConversationOut(
        conversation_id=conv["conversation_id"],
        title=conv.get("title") or "(untitled)",
        created_at=str(conv["created_at"]) if conv.get("created_at") else None,
        updated_at=str(conv["updated_at"]) if conv.get("updated_at") else None,
        last_message_at=(
            str(conv["last_message_at"]) if conv.get("last_message_at") else None
        ),
        message_count=int(conv.get("message_count") or 0),
    )


@chat_router.delete(
    "/conversations/{conversation_id}",
    operation_id="chatDeleteConversation",
)
def delete_conversation(
    headers: HeadersDependency,
    conversation_id: str = Path(..., min_length=1, max_length=64),
):
    """Soft-delete: `is_deleted = true`. Preserves audit trail."""
    user_key = _resolve_user_key(headers)
    if not _fetch_conversation(conversation_id, user_key):
        raise HTTPException(status_code=404, detail="Conversation not found")
    execute_update(
        f"""
        UPDATE {_conversations_table()}
        SET is_deleted = true,
            updated_at = TIMESTAMP '{_now_iso()}'
        WHERE conversation_id = '{_sql_quote(conversation_id)}'
          AND user_key = '{_sql_quote(user_key)}'
        """,
        tag_overrides={"submodule": "chat.delete_conversation"},
    )
    return {"ok": True, "conversation_id": conversation_id}
