"""Tool primitives shared across the chat tool registry.

Design choices we live with for the whole tool family:

1. **Tools are plain Python callables, not async**, because the underlying
   service layer (db.execute_query, Genie REST) is sync and we run inside
   a regular FastAPI thread anyway. If we ever move the streaming chat
   loop onto an async generator, we can lift these to async without
   changing the call sites — the registry shape is agnostic.

2. **Args are validated by Pydantic** before the handler runs. The
   registry serializes `Args.model_json_schema()` to feed the model's
   tool-definition slot, so the schema the LLM sees is exactly the one
   we validate against. No drift possible.

3. **Errors are surfaced as ToolResult(ok=False, ...)**, not raised. The
   dispatcher catches the synchronous exception path too, but normalizing
   on a single shape keeps the assistant's follow-up message coherent
   ("the tool failed because X" → model can recover).

4. **No tool ever writes** in Phase A1. We don't enforce this in code
   here because `app_propose_*` tools in A2 will use the same primitives
   but will return *proposals* (still no writes from the handler — the
   write happens later when the user clicks Confirm in the UI). The
   property "tool handlers do not mutate state" therefore holds for the
   entire Phase A by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel


class ToolError(Exception):
    """Raised by tool handlers for expected failures.

    Use this when the failure has a useful message for the LLM (e.g.
    "no use cases matched"). Unexpected exceptions bubble up and get
    wrapped by the dispatcher with a generic "tool crashed" message so
    we don't leak stack traces to the user-visible chat.
    """


@dataclass
class ToolContext:
    """Per-call context threaded through every tool invocation.

    The chat router populates this once per user turn so tools don't
    need to re-derive identity, conversation state, or the user's
    OBO token (Genie tool uses it). Keep it small — anything bigger
    than primitives belongs in a service module the tool imports.
    """
    user_key: str                       # lowercased username/email; never None
    user_email: str | None              # raw forwarded email (for Genie attribution)
    conversation_id: str                # our chat thread id
    genie_conversation_id: str | None   # mutates across the turn (set on first Genie call)
    user_obo_token: str | None          # forwarded user access token, if available


@dataclass
class ToolResult:
    """What every tool returns.

    `summary` is the short string the UI tool-call card shows by default
    ("Found 12 use cases"). `data` is the structured payload the LLM
    sees on its tool-result message and that we persist for replay /
    debugging. `citations` are deep-link entries the FE renders as
    clickable chips below the assistant message ("PacifiCorp Outage
    Prediction" → /use-cases/abc123).
    """
    ok: bool
    summary: str
    data: Any = None
    citations: list[dict[str, str]] = field(default_factory=list)
    chart_spec: dict[str, Any] | None = None  # Vega-Lite (used by genie tool)


@dataclass
class Tool:
    """Registry entry. The dispatcher reads `name`, `description`, and
    `args_model.model_json_schema()` to build the tool definition the
    model sees, then calls `handler(args, context)` when the model picks
    this tool.
    """
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], ToolResult]

    def llm_definition(self) -> dict[str, Any]:
        """Serialize to the OpenAI function-calling shape.

        Databricks AI Gateway speaks the OpenAI wire format for both
        Llama and Anthropic models, so a single shape works across
        endpoints. We intentionally do NOT include `additionalProperties:
        false` even though strict mode is available — Claude 4.x has
        been observed to ignore optional fields when they're declared
        absent vs. just missing, and the looser schema is more tolerant
        of model evolution.
        """
        schema = self.args_model.model_json_schema()
        # Drop pydantic's "title" cruft so the model isn't biased by our
        # internal class names (e.g. "SearchUseCasesArgs").
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": schema,
            },
        }
