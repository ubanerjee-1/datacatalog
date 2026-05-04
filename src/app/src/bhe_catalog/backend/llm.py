"""Databricks Foundation Model API streaming client.

Thin HTTP wrapper around the OpenAI-compatible
`/serving-endpoints/{name}/invocations` route. Stream-only; Phase A1 has
no batch use case.

Why hand-rolled instead of `openai` SDK? The serving endpoint already
speaks the OpenAI wire format and we already have `requests` + auth
helpers in `db.py`. Adding `openai` would just be a second auth path to
keep in sync with `db._get_headers()`. When/if we add tool calling in
Phase A1 PR #2 we can revisit.

The streaming generator yields **structured event dicts**, not raw SSE
bytes. The chat router is responsible for re-encoding to SSE for the
HTTP response. This keeps the client testable without parsing SSE in
the test.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from typing import Any, TypedDict

import requests

from .db import _get_headers, _get_host

logger = logging.getLogger(__name__)


CHAT_LLM_ENDPOINT = os.environ.get("CHAT_LLM_ENDPOINT", "databricks-claude-opus-4-7")


class ChatStreamEvent(TypedDict, total=False):
    """Wire-format event yielded by `stream_chat`.

    `type` field discriminates:
      - "token"             : `text` is an incremental string fragment
      - "tool_call_delta"   : an incremental tool-call piece. `index` is
                              the slot (so the caller can buffer multiple
                              parallel tool calls), `id` may appear once,
                              `name` may appear once, `arguments` is the
                              streamed JSON-string fragment of args.
      - "done"              : `finish_reason`, `prompt_tokens`,
                              `completion_tokens`, `latency_ms` are the
                              final aggregates. `finish_reason="tool_calls"`
                              signals the caller should execute the
                              buffered tool calls and loop.
      - "error"             : `error` is a human-readable message
    """
    type: str
    text: str
    index: int
    id: str
    name: str
    arguments: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    error: str


def stream_chat(
    messages: list[dict[str, Any]],
    *,
    endpoint: str = CHAT_LLM_ENDPOINT,
    max_tokens: int = 4000,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> Iterator[ChatStreamEvent]:
    """Yield structured streaming events from a Databricks chat endpoint.

    `messages` follows the OpenAI shape: [{"role": "...", "content": "..."}].
    Tool-result messages use the OpenAI shape too:
        {"role": "tool", "tool_call_id": "...", "content": "<json string>"}
    Assistant messages with tool calls use:
        {"role": "assistant", "content": "...", "tool_calls": [...]}

    The function never raises mid-stream; transport errors are surfaced as
    a final `{"type": "error", ...}` event so callers can persist a failed
    assistant message uniformly.

    `temperature` is opt-in because some Anthropic models on Databricks AI
    Gateway (notably Claude Opus 4.7) reject the parameter outright with
    HTTP 400. Default behavior is to omit it and let the endpoint pick.

    `tools` is a list of OpenAI function-tool definitions; pass None to
    disable tool calling entirely. `tool_choice` follows the OpenAI
    convention ("auto" | "none" | {"type":"function","function":{"name":...}})
    and defaults to None (model decides) when tools are present.
    """
    host = _get_host()
    headers = _get_headers()
    url = f"{host}/serving-endpoints/{endpoint}/invocations"

    body: dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    if extra_params:
        body.update(extra_params)

    started = time.time()
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason = ""
    saw_any_token = False

    try:
        with requests.post(url, json=body, headers=headers, stream=True, timeout=120) as resp:
            if resp.status_code >= 400:
                detail = resp.text[:1000]
                logger.error(f"FM stream {resp.status_code}: {detail}")
                yield {
                    "type": "error",
                    "error": f"Model endpoint returned {resp.status_code}: {detail[:200]}",
                }
                return

            # Pin UTF-8 explicitly. The Databricks SSE response Content-Type is
            # "text/event-stream" with no charset parameter, and `requests`
            # falls back to ISO-8859-1 in that case — which mangles em-dashes
            # and any non-ASCII the model emits ("—" -> "â€"" mojibake).
            resp.encoding = "utf-8"

            # The serving endpoint returns SSE: each line is `data: {json}` with
            # a final `data: [DONE]`. We use iter_lines to avoid buffering the
            # entire response.
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if raw.startswith(":"):
                    # Comment / keep-alive
                    continue
                if not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    logger.warning(f"FM stream: unparsable chunk: {payload[:200]}")
                    continue

                # OpenAI-compatible chat.completion.chunk shape:
                #   {"choices":[{"delta":{"content":"...", "tool_calls":[...]},
                #                "finish_reason":null}], "usage": {...}}
                # Anthropic-on-Databricks shows the same shape. Tool calls
                # arrive as deltas on `delta.tool_calls[]`, where each
                # element has `index` (slot), optionally `id` (only on
                # first delta of a slot), `function.name` (also only
                # first-time), and `function.arguments` (JSON-string,
                # streamed across many deltas — caller must concatenate).
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        saw_any_token = True
                        yield {"type": "token", "text": text}
                    for tc in delta.get("tool_calls") or []:
                        # Track that the model produced *something* even
                        # if it never emitted plain-text tokens (pure
                        # tool-call turn).
                        saw_any_token = True
                        ev: dict[str, Any] = {
                            "type": "tool_call_delta",
                            "index": tc.get("index", 0),
                        }
                        if tc.get("id"):
                            ev["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            ev["name"] = fn["name"]
                        if "arguments" in fn:
                            # Empty string is a valid delta (some endpoints
                            # send "" before populating); always forward.
                            ev["arguments"] = fn["arguments"]
                        yield ev
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr

                usage = chunk.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens) or prompt_tokens
                    completion_tokens = (
                        usage.get("completion_tokens", completion_tokens) or completion_tokens
                    )
    except requests.RequestException as e:
        logger.exception("FM stream transport error")
        yield {"type": "error", "error": f"Transport error: {e}"}
        return

    latency_ms = int((time.time() - started) * 1000)

    if not saw_any_token and not finish_reason:
        yield {
            "type": "error",
            "error": "Model endpoint closed the stream without producing any tokens.",
        }
        return

    yield {
        "type": "done",
        "finish_reason": finish_reason or "stop",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
    }
