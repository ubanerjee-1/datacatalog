"""Chat tools package (Phase A1 — read-only).

The chat router imports a single name from here — `TOOLS` — which is the
ordered list of tools the LLM is allowed to call. Each tool is a thin
wrapper over the existing service layer; nothing here writes to the
warehouse in Phase A1 (writes land in Phase A2 with the confirmation
token gate).

Why a list and not a dict-by-name?
We want stable ordering when serializing tool definitions to the model —
JSON schema field order leaks into model behavior in subtle ways and
re-orders cause cache misses on the model side.

Order convention (matches Appendix A in docs/plans/chatbot-track-a.md):
  1. Specific entity reads first (use cases, schemas)
  2. Dimension lists (affiliates, source systems)
  3. Portfolio aggregates (value summary, source rollup, gaps matrix)
  4. Genie fallback last — the model picks the first tool that fits.
"""
from __future__ import annotations

from ._base import Tool, ToolContext, ToolError, ToolResult
from .catalog import GET_USE_CASE, SEARCH_USE_CASES
from .dimensions import LIST_AFFILIATES, LIST_SOURCE_SYSTEMS
from .genie import GENIE_ASK
from .proposals import (
    PROPOSE_AFFILIATE_MAPPING,
    PROPOSE_CANONICAL_MAPPING,
    PROPOSE_SCHEMA_UPDATE,
    PROPOSE_STATUS_CHANGE,
    PROPOSE_USE_CASE,
    PROPOSE_USE_CASE_UPDATE,
)
from .research import RESEARCH_SCHEMA, RESEARCH_USE_CASE
from .schemas import GET_SCHEMA, SEARCH_SCHEMAS
from .value import GAPS_MATRIX, VALUE_SOURCE_ROLLUP, VALUE_SUMMARY

# Phase A3 (Slice A3-1) — full tool roster (18 tools).
# Order matters; see module docstring.
# Reads first, then propose_* writes (most-specific edits first →
# whole-record creates after), then Genie fallback last. The create
# tool sits last among the writes because the model should prefer
# editing an existing use case (cheaper, less disruptive) when the
# user's request could be either an edit or a create.
#
# Research tools sit with the other reads (after the basic catalog
# reads) but BEFORE any propose_* tool — the system prompt tells the
# model to call research as the first step of any create OR edit
# flow, so putting them adjacent to the other reads keeps the
# "first turn = a read" pattern visible in the tool list.
#
# Schema edit + use-case edit are interleaved by entity type; both
# pair their propose with a research tool that the system prompt
# requires first.
TOOLS: list[Tool] = [
    SEARCH_USE_CASES,
    GET_USE_CASE,
    SEARCH_SCHEMAS,
    GET_SCHEMA,
    LIST_AFFILIATES,
    LIST_SOURCE_SYSTEMS,
    VALUE_SUMMARY,
    VALUE_SOURCE_ROLLUP,
    GAPS_MATRIX,
    RESEARCH_USE_CASE,
    RESEARCH_SCHEMA,
    PROPOSE_STATUS_CHANGE,
    PROPOSE_USE_CASE_UPDATE,
    PROPOSE_AFFILIATE_MAPPING,
    PROPOSE_CANONICAL_MAPPING,
    PROPOSE_USE_CASE,
    PROPOSE_SCHEMA_UPDATE,
    GENIE_ASK,
]

__all__ = [
    "TOOLS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
]
