"""Phase A2 propose_* tools.

Each propose_* tool follows the same shape:

  1. Validate inputs.
  2. Look up the current entity state (so the confirm card can render
     a side-by-side diff of "before" vs. "after").
  3. Issue a single-use confirmation token via `confirm.issue_token`
     with the full payload the executor will need.
  4. Return a ToolResult whose `data` includes:
       - `confirm`: {token, intent, expires_at}
       - `before`: current values for fields the propose touches
       - `after`:  proposed values
     The FE renders an inline ConfirmCard from this shape.

Critically, the propose_* tools do NOT write anything. The actual write
runs only when the user clicks Confirm in the chat panel, which POSTs
to `/api/chat/confirm/{token}` and dispatches to the matching executor
in confirm._INTENT_EXECUTORS.

Slice A2-1 ships only `app_propose_status_change` because it's the
smallest write that exercises the whole propose/confirm pipeline. The
multi-field updaters (propose_use_case_update, propose_use_case) follow
once this pattern is validated end-to-end.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from ..confirm import (
    INTENT_CREATE_USE_CASE,
    INTENT_UPDATE_SCHEMA,
    INTENT_UPDATE_USE_CASE,
    INTENT_UPDATE_USE_CASE_AFFILIATES,
    INTENT_UPDATE_USE_CASE_CANONICALS,
    INTENT_UPDATE_USE_CASE_STATUS,
    issue_token,
)
from ..db import execute_query, fqn, get_gold_schema, get_silver_schema
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


_VALID_STATUSES: tuple[str, ...] = (
    "not_started",
    "in_progress",
    "delivered",
    "on_hold",
)
# UI standard set; see ui/routes/_sidebar/value-readiness.tsx PRIORITIES.
# Kept in this module so the tool's validation message is self-contained.
_VALID_PRIORITIES: tuple[str, ...] = ("High", "Medium", "Low")


def _q(s: str) -> str:
    return s.replace("'", "''")


def _normalize_priority(value: str | None) -> str | None:
    """Case-fix common forms ('high' -> 'High'); reject anything unknown.

    Returns the canonical form on match, raises on miss. We do this in
    the propose tool (not the executor) so the LLM gets actionable
    feedback on the same turn instead of a silent bad write.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    for canon in _VALID_PRIORITIES:
        if v.lower() == canon.lower():
            return canon
    raise ValueError(
        f"Invalid priority {value!r}. Allowed: {', '.join(_VALID_PRIORITIES)}"
    )


# ---------------------------------------------------------------------------
# app_propose_status_change
# ---------------------------------------------------------------------------


class ProposeStatusChangeArgs(BaseModel):
    """Propose a delivery-status change on a use case.

    The arg names track `UseCaseStatusIn` in models.py so the eventual
    executor passes the exact same shape the UI's PATCH endpoint takes.
    """

    use_case_id: str = Field(
        ...,
        description=(
            "Stable use_case_id (e.g. 'uc_a1b2c3d4'). Get this from a prior "
            "app_search_use_cases or app_get_use_case call — never guess."
        ),
    )
    status: str = Field(
        ...,
        description=(
            "Target delivery status. Allowed: 'not_started' | 'in_progress' "
            "| 'delivered' | 'on_hold'."
        ),
    )
    status_notes: str | None = Field(
        default=None,
        description=(
            "Optional free-form note explaining the change (e.g. 'shipped "
            "Q4 2026'). Shown on the confirmation card and persisted with "
            "the row when the user confirms."
        ),
    )


def _propose_status_change(
    args: ProposeStatusChangeArgs, ctx: ToolContext
) -> ToolResult:
    status = args.status.strip().lower()
    if status not in _VALID_STATUSES:
        return ToolResult(
            ok=False,
            summary=(
                f"Invalid status {args.status!r}. "
                f"Allowed: {', '.join(_VALID_STATUSES)}"
            ),
            data={"error": "invalid_status"},
        )

    silver = get_silver_schema()
    raw = args.use_case_id.strip()
    bare = raw[3:] if raw.startswith("uc_") else raw
    candidates = [raw] if raw == bare else [raw, bare]
    candidates_sql = ", ".join(f"'{_q(c)}'" for c in candidates)

    try:
        rows = execute_query(
            f"""
            SELECT id, use_case_name,
                   COALESCE(status, 'not_started') AS status,
                   status_notes,
                   COALESCE(estimated_value_usd, 0) AS estimated_value_usd,
                   department, priority
            FROM {fqn(silver, 'use_cases')}
            WHERE id IN ({candidates_sql})
            LIMIT 1
            """,
            tag_overrides={"submodule": "chat.tool.propose_status_change"},
        )
    except Exception as e:
        logger.exception("propose_status_change lookup failed")
        return ToolResult(
            ok=False, summary=f"Lookup failed: {e}", data={"error": str(e)}
        )

    if not rows:
        return ToolResult(
            ok=True,
            summary=f"No use case with id '{args.use_case_id}'",
            data={"use_case": None},
        )
    row = rows[0]
    # Persist the canonical id (whichever form actually matched) — that's
    # what the executor's UPDATE WHERE id = ... will need.
    uc_id = str(row.get("id") or candidates[0])
    current_status = (row.get("status") or "not_started").strip().lower()
    if current_status == status and (args.status_notes or "") == (row.get("status_notes") or ""):
        # Nothing to change. Don't burn a token; tell the model so it can
        # report that to the user instead of asking for confirmation.
        return ToolResult(
            ok=True,
            summary=(
                f"{row.get('use_case_name')} is already '{current_status}'. "
                "No change to confirm."
            ),
            data={
                "no_change": True,
                "use_case_id": uc_id,
                "current_status": current_status,
            },
        )

    payload: dict[str, Any] = {
        "use_case_id": uc_id,
        "status": status,
    }
    if args.status_notes is not None:
        payload["status_notes"] = args.status_notes

    try:
        token = issue_token(
            intent=INTENT_UPDATE_USE_CASE_STATUS,
            target_id=uc_id,
            payload=payload,
            user_key=ctx.user_key,
            conversation_id=ctx.conversation_id,
        )
    except Exception as e:
        logger.exception("propose_status_change token issue failed")
        return ToolResult(
            ok=False, summary=f"Could not prepare confirmation: {e}",
            data={"error": str(e)},
        )

    use_case_name = row.get("use_case_name") or uc_id

    return ToolResult(
        ok=True,
        summary=(
            f"Ready to set '{use_case_name}' to '{status}'. "
            "User must confirm in the chat panel."
        ),
        data={
            "kind": "proposal",
            "intent": INTENT_UPDATE_USE_CASE_STATUS,
            "confirm": {
                "token": token["token"],
                "expires_at": token["expires_at"],
            },
            "use_case": {
                "use_case_id": uc_id,
                "use_case_name": use_case_name,
                "department": row.get("department"),
                "priority": row.get("priority"),
                "estimated_value_usd": float(row.get("estimated_value_usd") or 0),
            },
            "before": {
                "status": current_status,
                "status_notes": row.get("status_notes") or None,
            },
            "after": {
                "status": status,
                # Mirror executor semantics: if the model didn't set notes,
                # the existing notes stay put. Showing the prior value here
                # keeps the diff card honest (no fake "→ null" rows).
                "status_notes": (
                    args.status_notes
                    if args.status_notes is not None
                    else (row.get("status_notes") or None)
                ),
            },
        },
        # Citation lets the user click through to verify before confirming.
        citations=[
            {
                "label": use_case_name,
                "deeplink": f"/value-readiness?uc={uc_id}",
            }
        ],
    )


PROPOSE_STATUS_CHANGE = Tool(
    name="app_propose_status_change",
    description=(
        "Propose a delivery-status change on a use case (e.g. mark "
        "'delivered' or 'on_hold'). Does NOT write — returns a "
        "single-use confirmation token. The chat UI renders a "
        "before/after diff card; the user must click Confirm before any "
        "change is persisted. Always call app_get_use_case first if you "
        "don't already know the current status, so the user sees the "
        "right change. Allowed statuses: not_started, in_progress, "
        "delivered, on_hold."
    ),
    args_model=ProposeStatusChangeArgs,
    handler=_propose_status_change,
)


# ---------------------------------------------------------------------------
# app_propose_use_case_update
# ---------------------------------------------------------------------------
#
# Multi-field updater. Mirrors the conversational subset of UseCaseUpdateIn
# (models.py): name, description, department, category, business_value,
# value_rationale, priority, estimated_value_usd. Status/notes intentionally
# excluded — `app_propose_status_change` already owns those (cleaner UI
# diff card; user knows "this is a status change", not a generic edit).
# data_requirements (a structured list) deferred to its own tool in slice
# A2-3, where the diff card needs add/remove semantics rather than scalar
# before/after rows.


# Fields the model can set on this tool. Keep this list in sync with the
# Pydantic args below AND with build_use_case_update_set_clause in
# router.py — the executor will silently drop fields it doesn't recognize,
# but the diff card will show them, which is misleading.
_EDITABLE_FIELDS: tuple[str, ...] = (
    "use_case_name",
    "description",
    "department",
    "category",
    "business_value",
    "value_rationale",
    "priority",
    "estimated_value_usd",
)
# Pretty labels for the diff card. The FE has its own copy too (chat-launcher
# TOOL_LABELS); duplicating intentionally because the FE shouldn't have to
# parse python field names to render a card.
_FIELD_LABELS: dict[str, str] = {
    "use_case_name": "Name",
    "description": "Description",
    "department": "Department",
    "category": "Category",
    "business_value": "Business value",
    "value_rationale": "Value rationale",
    "priority": "Priority",
    "estimated_value_usd": "Est. value (USD)",
}


class ProposeUseCaseUpdateArgs(BaseModel):
    """Patch a use case with one or more editable fields.

    All fields are optional — the model passes only the ones it wants to
    change. Empty/whitespace strings are treated as "no value provided"
    and dropped (NOT as "clear the field") to match the existing PUT
    endpoint's semantics. To clear a free-text field today, the user
    must use the UI; we'd rather refuse than misinterpret intent.
    """

    use_case_id: str = Field(
        ...,
        description=(
            "Stable use_case_id from app_search_use_cases or "
            "app_get_use_case. Tolerates both bare 8-hex and 'uc_<12hex>' "
            "forms."
        ),
    )
    use_case_name: str | None = Field(default=None, description="New display name.")
    description: str | None = Field(
        default=None, description="New long-form description."
    )
    department: str | None = Field(
        default=None,
        description=(
            "Owning department. The chat does NOT validate against the "
            "departments table — call app_list_affiliates / app_get_use_case "
            "to see existing values; pass a string that matches one of them "
            "exactly."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "Free-text category (e.g. 'Risk Management', 'Reliability'). "
            "Match an existing one if you can — categories are not "
            "enumerated."
        ),
    )
    business_value: str | None = Field(
        default=None, description="One-sentence value proposition."
    )
    value_rationale: str | None = Field(
        default=None, description="Multi-sentence justification of the dollar value."
    )
    priority: str | None = Field(
        default=None,
        description=(
            "Priority. Allowed: 'High' | 'Medium' | 'Low' (case-insensitive)."
        ),
    )
    estimated_value_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Estimated annual value in USD. Must be >= 0. Pass an absolute "
            "number (e.g. 45000000), not millions."
        ),
    )


def _propose_use_case_update(
    args: ProposeUseCaseUpdateArgs, ctx: ToolContext
) -> ToolResult:
    silver = get_silver_schema()
    raw = args.use_case_id.strip()
    bare = raw[3:] if raw.startswith("uc_") else raw
    candidates = [raw] if raw == bare else [raw, bare]
    candidates_sql = ", ".join(f"'{_q(c)}'" for c in candidates)

    # Build the proposed patch (only fields the model touched).
    proposed: dict[str, object] = {}
    try:
        norm_priority = _normalize_priority(args.priority)
    except ValueError as e:
        return ToolResult(
            ok=False, summary=str(e), data={"error": "invalid_priority"}
        )
    if norm_priority is not None:
        proposed["priority"] = norm_priority
    for f in _EDITABLE_FIELDS:
        if f in ("priority", "estimated_value_usd"):
            continue
        v = getattr(args, f)
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            # Treat empty-after-strip as "no value provided" — see docstring.
            if not v:
                continue
        proposed[f] = v
    if args.estimated_value_usd is not None:
        proposed["estimated_value_usd"] = float(args.estimated_value_usd)

    if not proposed:
        return ToolResult(
            ok=False,
            summary=(
                "No fields to change. Pass at least one editable field "
                f"({', '.join(_EDITABLE_FIELDS)})."
            ),
            data={"error": "empty_patch"},
        )

    # Pull the current row so we can render before-vs-after AND drop
    # no-op fields (proposed value identical to current).
    select_cols = ", ".join(["id", "use_case_name"] + list(_EDITABLE_FIELDS))
    try:
        rows = execute_query(
            f"""
            SELECT {select_cols}
            FROM {fqn(silver, 'use_cases')}
            WHERE id IN ({candidates_sql})
            LIMIT 1
            """,
            tag_overrides={"submodule": "chat.tool.propose_use_case_update"},
        )
    except Exception as e:
        logger.exception("propose_use_case_update lookup failed")
        return ToolResult(
            ok=False, summary=f"Lookup failed: {e}", data={"error": str(e)}
        )

    if not rows:
        return ToolResult(
            ok=True,
            summary=f"No use case with id '{args.use_case_id}'",
            data={"use_case": None},
        )
    row = rows[0]
    uc_id = str(row.get("id") or candidates[0])
    uc_name = row.get("use_case_name") or uc_id

    # Drop no-op fields. Coerce both sides for the comparison: SQL returns
    # numeric for estimated_value_usd, string for everything else.
    before: dict[str, object | None] = {}
    after: dict[str, object | None] = {}
    effective_patch: dict[str, object] = {}
    for f, new_val in proposed.items():
        cur_val = row.get(f)
        if f == "estimated_value_usd":
            cur_num = float(cur_val) if cur_val is not None else None
            new_num = float(new_val)  # type: ignore[arg-type]
            if cur_num == new_num:
                continue
            before[f] = cur_num
            after[f] = new_num
        else:
            cur_str = (str(cur_val) if cur_val is not None else "").strip()
            new_str = str(new_val).strip()
            if cur_str == new_str:
                continue
            before[f] = cur_str or None
            after[f] = new_str
        effective_patch[f] = new_val

    if not effective_patch:
        return ToolResult(
            ok=True,
            summary=(
                f"All proposed values for '{uc_name}' already match the "
                "current record. Nothing to confirm."
            ),
            data={
                "no_change": True,
                "use_case_id": uc_id,
            },
        )

    payload = {"use_case_id": uc_id, "patch": effective_patch}
    try:
        token = issue_token(
            intent=INTENT_UPDATE_USE_CASE,
            target_id=uc_id,
            payload=payload,
            user_key=ctx.user_key,
            conversation_id=ctx.conversation_id,
        )
    except Exception as e:
        logger.exception("propose_use_case_update token issue failed")
        return ToolResult(
            ok=False,
            summary=f"Could not prepare confirmation: {e}",
            data={"error": str(e)},
        )

    field_labels = {f: _FIELD_LABELS[f] for f in effective_patch if f in _FIELD_LABELS}
    n = len(effective_patch)
    summary = (
        f"Ready to update '{uc_name}' — {n} field{'s' if n != 1 else ''} "
        "changed. User must confirm in the chat panel."
    )
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "kind": "proposal",
            "intent": INTENT_UPDATE_USE_CASE,
            "confirm": {
                "token": token["token"],
                "expires_at": token["expires_at"],
            },
            "use_case": {
                "use_case_id": uc_id,
                "use_case_name": uc_name,
            },
            # Field labels keep the FE card readable without it having
            # to know the python field names. Order is the canonical
            # _EDITABLE_FIELDS order so the card is stable across turns.
            "field_order": [f for f in _EDITABLE_FIELDS if f in effective_patch],
            "field_labels": field_labels,
            "before": before,
            "after": after,
        },
        citations=[
            {
                "label": uc_name,
                "deeplink": f"/value-readiness?uc={uc_id}",
            }
        ],
    )


PROPOSE_USE_CASE_UPDATE = Tool(
    name="app_propose_use_case_update",
    description=(
        "Propose edits to one or more conversational fields on a use "
        "case (name, description, department, category, business_value, "
        "value_rationale, priority, estimated_value_usd). Does NOT write "
        "— returns a single-use confirmation token; the chat UI renders "
        "a per-field before/after card; user must Confirm. Always call "
        "app_get_use_case first so you don't propose values that "
        "already match. For status changes use app_propose_status_change "
        "instead — cleaner card. data_requirements editing is not yet "
        "exposed."
    ),
    args_model=ProposeUseCaseUpdateArgs,
    handler=_propose_use_case_update,
)


# ---------------------------------------------------------------------------
# Mapping tools (A2-3): list deltas, not scalar before/after.
#
# Both `app_propose_affiliate_mapping` and `app_propose_canonical_mapping`
# work on add/remove deltas (not "set the whole list to X") because:
#   1. Replace-whole-list is dangerous: a model that omits an entry
#      from a "set to" call would silently delete it.
#   2. Deltas keep blast radius minimal — we only touch what the model
#      explicitly listed; existing rows the model didn't mention are
#      untouched.
#   3. The user often phrases as deltas ("add X", "drop Y") and rarely
#      as "set to exactly [...]".
#
# Both tools return the same JSON shape so the FE can render them
# through one ListDiffConfirmCard. The `resource` field tells the FE
# which heading + reseed-warning copy to use.
# ---------------------------------------------------------------------------


_VALID_APPLICABILITY: tuple[str, ...] = ("primary", "secondary")
_VALID_NECESSITY: tuple[str, ...] = ("must_have", "nice_to_have")


# Shown in the confirm card AND surfaced by the LLM in its prose. Mirrors
# the docstring on router.delete_use_case_affiliate_row.
_RESEED_WARNING_AFFILIATE = (
    "The catalog reseed job may re-add a removed affiliate if the use "
    "case description still implies it. To make a removal stick, edit "
    "the use case description too."
)
_RESEED_WARNING_CANONICAL = (
    "The catalog reseed job may re-add a removed canonical if the use "
    "case description still implies it. To make a removal stick, edit "
    "the use case description too."
)


# --------------------------------------------------------------------------
# app_propose_affiliate_mapping
# --------------------------------------------------------------------------


class AffiliateAdd(BaseModel):
    """One affiliate to add (or update applicability/rationale on)."""

    name: str = Field(
        ...,
        description=(
            "Affiliate name exactly as it appears in app_list_affiliates "
            "(e.g. 'PacifiCorp', 'NV Energy', 'MidAmerican Energy')."
        ),
    )
    applicability: str = Field(
        default="secondary",
        description=(
            "'primary' (the use case is squarely owned by this affiliate) "
            "or 'secondary' (the affiliate would benefit but isn't the "
            "owner). Defaults to 'secondary' — be conservative."
        ),
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "One-sentence justification for why this affiliate maps. "
            "Optional but strongly preferred — surfaces in the UI."
        ),
    )


class ProposeAffiliateMappingArgs(BaseModel):
    """Add/remove affiliates on a use case. Pass at least one of `add`/`remove`."""

    use_case_id: str = Field(
        ...,
        description=(
            "Stable use_case_id from app_search_use_cases or "
            "app_get_use_case. Tolerates 'uc_<12hex>' or bare 8-hex."
        ),
    )
    add: list[AffiliateAdd] = Field(
        default_factory=list,
        description=(
            "Affiliates to add or update. Upsert semantics — if the "
            "affiliate is already mapped, this just updates its "
            "applicability/rationale."
        ),
    )
    remove: list[str] = Field(
        default_factory=list,
        description=(
            "Affiliate names to remove. Hard-deletes the row. The reseed "
            "job may re-add it later — surface this caveat to the user."
        ),
    )


def _propose_affiliate_mapping(
    args: ProposeAffiliateMappingArgs, ctx: ToolContext
) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()
    raw = args.use_case_id.strip()
    bare = raw[3:] if raw.startswith("uc_") else raw
    candidates = [raw] if raw == bare else [raw, bare]
    candidates_sql = ", ".join(f"'{_q(c)}'" for c in candidates)

    # Validate add[].applicability and de-dup add+remove vs each other.
    for item in args.add:
        if item.applicability not in _VALID_APPLICABILITY:
            return ToolResult(
                ok=False,
                summary=(
                    f"Invalid applicability {item.applicability!r} for "
                    f"{item.name!r}. Allowed: {', '.join(_VALID_APPLICABILITY)}"
                ),
                data={"error": "invalid_applicability"},
            )
    add_names = {it.name.strip() for it in args.add if it.name and it.name.strip()}
    remove_names = {n.strip() for n in args.remove if n and n.strip()}
    overlap = add_names & remove_names
    if overlap:
        return ToolResult(
            ok=False,
            summary=(
                "Cannot add and remove the same affiliate in one call: "
                f"{sorted(overlap)}"
            ),
            data={"error": "add_remove_overlap"},
        )
    if not add_names and not remove_names:
        return ToolResult(
            ok=False,
            summary="Pass at least one entry in add or remove.",
            data={"error": "empty_delta"},
        )

    # Look up the use case + current affiliate set in one round-trip each.
    try:
        uc_rows = execute_query(
            f"""
            SELECT id, use_case_name
            FROM {fqn(silver, 'use_cases')}
            WHERE id IN ({candidates_sql})
            LIMIT 1
            """,
            tag_overrides={
                "submodule": "chat.tool.propose_affiliate_mapping"
            },
        )
        if not uc_rows:
            return ToolResult(
                ok=True,
                summary=f"No use case with id '{args.use_case_id}'",
                data={"use_case": None},
            )
        uc_id = str(uc_rows[0].get("id") or candidates[0])
        uc_name = uc_rows[0].get("use_case_name") or uc_id

        current_rows = execute_query(
            f"""
            SELECT affiliate_name, applicability, rationale
            FROM {fqn(gold, 'use_case_affiliates')}
            WHERE use_case_id = '{_q(uc_id)}'
            ORDER BY affiliate_name
            """,
            tag_overrides={
                "submodule": "chat.tool.propose_affiliate_mapping"
            },
        )
        # Validate that affiliates the model wants to add EXIST in the
        # affiliates dim. Names like 'PacifiCorp ' would silently create
        # a junk row otherwise.
        valid_aff_rows = execute_query(
            f"""
            SELECT affiliate_name
            FROM {fqn(gold, 'affiliates')}
            WHERE COALESCE(is_active, true) = true
            """,
            tag_overrides={
                "submodule": "chat.tool.propose_affiliate_mapping"
            },
        )
    except Exception as e:
        logger.exception("propose_affiliate_mapping lookup failed")
        return ToolResult(
            ok=False, summary=f"Lookup failed: {e}", data={"error": str(e)}
        )

    valid_affiliates = {
        str(r.get("affiliate_name")) for r in valid_aff_rows if r.get("affiliate_name")
    }
    current_by_name: dict[str, dict] = {
        str(r.get("affiliate_name")): r for r in current_rows
    }

    unknown = [it.name for it in args.add if it.name not in valid_affiliates]
    if unknown:
        return ToolResult(
            ok=False,
            summary=(
                f"Unknown affiliate(s): {', '.join(unknown)}. "
                "Use app_list_affiliates to find the exact name."
            ),
            data={"error": "unknown_affiliate", "unknown": unknown},
        )
    not_present = [n for n in remove_names if n not in current_by_name]
    if not_present:
        return ToolResult(
            ok=False,
            summary=(
                f"Cannot remove affiliate(s) that aren't currently mapped: "
                f"{', '.join(not_present)}. "
                "Use app_get_use_case to see the current list."
            ),
            data={"error": "not_currently_mapped", "names": not_present},
        )

    # Drop no-op upserts: if an add proposes the EXACT same applicability
    # + rationale that's already on the row, skip it. Keeps the card honest.
    effective_add: list[dict] = []
    skipped_noop: list[str] = []
    for item in args.add:
        cur = current_by_name.get(item.name)
        if cur:
            cur_app = (cur.get("applicability") or "").strip().lower()
            cur_rat = (cur.get("rationale") or "").strip()
            new_rat = (item.rationale or "").strip()
            # Treat unset rationale as "leave it alone" so the user can
            # change applicability without having to re-state rationale.
            rationale_changed = bool(new_rat) and new_rat != cur_rat
            applicability_changed = item.applicability.lower() != cur_app
            if not (rationale_changed or applicability_changed):
                skipped_noop.append(item.name)
                continue
        effective_add.append({
            "affiliate_name": item.name,
            "applicability": item.applicability,
            "rationale": (item.rationale or "").strip() or None,
            # FE shows a tag like "update" vs "add" so the user knows
            # whether this is a modify or a brand-new mapping.
            "_was_present": cur is not None,
            "_prev_applicability": (
                str(cur.get("applicability")) if cur else None
            ),
        })
    effective_remove = sorted(remove_names)

    if not effective_add and not effective_remove:
        return ToolResult(
            ok=True,
            summary=(
                f"All proposed affiliate changes for '{uc_name}' are "
                "no-ops. Nothing to confirm."
            ),
            data={"no_change": True, "use_case_id": uc_id},
        )

    # Strip the FE-only metadata before storing the executor payload —
    # the executor only wants what it needs to write.
    payload_add = [
        {k: v for k, v in row.items() if not k.startswith("_")}
        for row in effective_add
    ]
    payload = {
        "use_case_id": uc_id,
        "add": payload_add,
        "remove": effective_remove,
    }
    try:
        token = issue_token(
            intent=INTENT_UPDATE_USE_CASE_AFFILIATES,
            target_id=uc_id,
            payload=payload,
            user_key=ctx.user_key,
            conversation_id=ctx.conversation_id,
        )
    except Exception as e:
        logger.exception("propose_affiliate_mapping token issue failed")
        return ToolResult(
            ok=False,
            summary=f"Could not prepare confirmation: {e}",
            data={"error": str(e)},
        )

    n_add, n_rem = len(effective_add), len(effective_remove)
    bits: list[str] = []
    if n_add:
        bits.append(f"+{n_add} affiliate{'s' if n_add != 1 else ''}")
    if n_rem:
        bits.append(f"−{n_rem} affiliate{'s' if n_rem != 1 else ''}")
    summary = (
        f"Ready to update '{uc_name}' affiliates ({', '.join(bits)}). "
        "User must confirm in the chat panel."
    )

    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "kind": "proposal",
            "intent": INTENT_UPDATE_USE_CASE_AFFILIATES,
            "resource": "affiliates",
            "confirm": {
                "token": token["token"],
                "expires_at": token["expires_at"],
            },
            "use_case": {
                "use_case_id": uc_id,
                "use_case_name": uc_name,
            },
            # FE renders these directly. `additions` carries the upsert
            # rows (with _was_present so the FE can show "update" tag);
            # `removals` is just names with their current applicability.
            "additions": effective_add,
            "removals": [
                {
                    "name": n,
                    "current_applicability": (
                        str(current_by_name[n].get("applicability"))
                        if current_by_name.get(n) else None
                    ),
                }
                for n in effective_remove
            ],
            "skipped_noop": skipped_noop,
            "notice": _RESEED_WARNING_AFFILIATE if effective_remove else None,
        },
        citations=[
            {
                "label": uc_name,
                "deeplink": f"/value-readiness?uc={uc_id}",
            }
        ],
    )


PROPOSE_AFFILIATE_MAPPING = Tool(
    name="app_propose_affiliate_mapping",
    description=(
        "Add or remove affiliate mappings on a use case. Pass any "
        "combination of `add` (with name + applicability + optional "
        "rationale) and `remove` (just names). Does NOT write — returns "
        "a confirmation card showing the deltas; user must Confirm. "
        "Always call app_get_use_case first to see the current "
        "affiliates so you don't add a duplicate or try to remove "
        "something that isn't mapped. Removals carry a reseed caveat: "
        "the LLM job may re-add the row on next reseed if the use case "
        "description still implies the affiliate."
    ),
    args_model=ProposeAffiliateMappingArgs,
    handler=_propose_affiliate_mapping,
)


# --------------------------------------------------------------------------
# app_propose_canonical_mapping
# --------------------------------------------------------------------------


class CanonicalAdd(BaseModel):
    """One canonical source to add (or update necessity/excerpt on)."""

    canonical: str = Field(
        ...,
        description=(
            "Canonical source name exactly as it appears in "
            "app_list_source_systems (e.g. 'Maximo', 'PI Historian', "
            "'Oracle Field Service')."
        ),
    )
    necessity: str = Field(
        default="must_have",
        description="'must_have' or 'nice_to_have'. Defaults to 'must_have'.",
    )
    data_need_excerpt: str | None = Field(
        default=None,
        description=(
            "Short snippet from the use case's data_requirements that "
            "explains WHY this canonical is needed (e.g. "
            "'Vegetation management work order completion data'). "
            "Optional but strongly preferred — drives the readiness "
            "tooltip."
        ),
    )


class ProposeCanonicalMappingArgs(BaseModel):
    """Add/remove canonical sources on a use case. Pass at least one of `add`/`remove`."""

    use_case_id: str = Field(
        ...,
        description=(
            "Stable use_case_id. Tolerates 'uc_<12hex>' or bare 8-hex."
        ),
    )
    add: list[CanonicalAdd] = Field(
        default_factory=list,
        description=(
            "Canonicals to add or update. Upsert semantics; if the "
            "canonical is already required, this just updates necessity "
            "or data_need_excerpt."
        ),
    )
    remove: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical names to remove. Hard-deletes the row; same "
            "reseed caveat as affiliate removal."
        ),
    )


def _propose_canonical_mapping(
    args: ProposeCanonicalMappingArgs, ctx: ToolContext
) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()
    raw = args.use_case_id.strip()
    bare = raw[3:] if raw.startswith("uc_") else raw
    candidates = [raw] if raw == bare else [raw, bare]
    candidates_sql = ", ".join(f"'{_q(c)}'" for c in candidates)

    for item in args.add:
        if item.necessity not in _VALID_NECESSITY:
            return ToolResult(
                ok=False,
                summary=(
                    f"Invalid necessity {item.necessity!r} for "
                    f"{item.canonical!r}. Allowed: {', '.join(_VALID_NECESSITY)}"
                ),
                data={"error": "invalid_necessity"},
            )
    add_names = {it.canonical.strip() for it in args.add if it.canonical}
    remove_names = {n.strip() for n in args.remove if n and n.strip()}
    overlap = add_names & remove_names
    if overlap:
        return ToolResult(
            ok=False,
            summary=(
                "Cannot add and remove the same canonical in one call: "
                f"{sorted(overlap)}"
            ),
            data={"error": "add_remove_overlap"},
        )
    if not add_names and not remove_names:
        return ToolResult(
            ok=False,
            summary="Pass at least one entry in add or remove.",
            data={"error": "empty_delta"},
        )

    try:
        uc_rows = execute_query(
            f"""
            SELECT id, use_case_name
            FROM {fqn(silver, 'use_cases')}
            WHERE id IN ({candidates_sql})
            LIMIT 1
            """,
            tag_overrides={
                "submodule": "chat.tool.propose_canonical_mapping"
            },
        )
        if not uc_rows:
            return ToolResult(
                ok=True,
                summary=f"No use case with id '{args.use_case_id}'",
                data={"use_case": None},
            )
        uc_id = str(uc_rows[0].get("id") or candidates[0])
        uc_name = uc_rows[0].get("use_case_name") or uc_id

        current_rows = execute_query(
            f"""
            SELECT required_canonical, necessity, data_need_excerpt
            FROM {fqn(gold, 'use_case_source_requirements')}
            WHERE use_case_id = '{_q(uc_id)}'
            ORDER BY required_canonical
            """,
            tag_overrides={
                "submodule": "chat.tool.propose_canonical_mapping"
            },
        )
        valid_canon_rows = execute_query(
            f"""
            SELECT canonical
            FROM {fqn(gold, 'source_system_canonical')}
            WHERE COALESCE(is_active, true) = true
            """,
            tag_overrides={
                "submodule": "chat.tool.propose_canonical_mapping"
            },
        )
    except Exception as e:
        logger.exception("propose_canonical_mapping lookup failed")
        return ToolResult(
            ok=False, summary=f"Lookup failed: {e}", data={"error": str(e)}
        )

    valid_canonicals = {
        str(r.get("canonical")) for r in valid_canon_rows if r.get("canonical")
    }
    current_by_name: dict[str, dict] = {
        str(r.get("required_canonical")): r for r in current_rows
    }

    unknown = [it.canonical for it in args.add if it.canonical not in valid_canonicals]
    if unknown:
        return ToolResult(
            ok=False,
            summary=(
                f"Unknown canonical source(s): {', '.join(unknown)}. "
                "Use app_list_source_systems to find the exact name."
            ),
            data={"error": "unknown_canonical", "unknown": unknown},
        )
    not_present = [n for n in remove_names if n not in current_by_name]
    if not_present:
        return ToolResult(
            ok=False,
            summary=(
                f"Cannot remove canonical(s) that aren't currently required: "
                f"{', '.join(not_present)}. "
                "Use app_get_use_case to see the current list."
            ),
            data={"error": "not_currently_mapped", "names": not_present},
        )

    effective_add: list[dict] = []
    skipped_noop: list[str] = []
    for item in args.add:
        cur = current_by_name.get(item.canonical)
        if cur:
            cur_nec = (cur.get("necessity") or "").strip().lower()
            cur_excerpt = (cur.get("data_need_excerpt") or "").strip()
            new_excerpt = (item.data_need_excerpt or "").strip()
            excerpt_changed = bool(new_excerpt) and new_excerpt != cur_excerpt
            necessity_changed = item.necessity.lower() != cur_nec
            if not (excerpt_changed or necessity_changed):
                skipped_noop.append(item.canonical)
                continue
        effective_add.append({
            "canonical": item.canonical,
            "necessity": item.necessity,
            "data_need_excerpt": (item.data_need_excerpt or "").strip() or None,
            "_was_present": cur is not None,
            "_prev_necessity": (
                str(cur.get("necessity")) if cur else None
            ),
        })
    effective_remove = sorted(remove_names)

    if not effective_add and not effective_remove:
        return ToolResult(
            ok=True,
            summary=(
                f"All proposed canonical changes for '{uc_name}' are "
                "no-ops. Nothing to confirm."
            ),
            data={"no_change": True, "use_case_id": uc_id},
        )

    payload_add = [
        {k: v for k, v in row.items() if not k.startswith("_")}
        for row in effective_add
    ]
    payload = {
        "use_case_id": uc_id,
        "add": payload_add,
        "remove": effective_remove,
    }
    try:
        token = issue_token(
            intent=INTENT_UPDATE_USE_CASE_CANONICALS,
            target_id=uc_id,
            payload=payload,
            user_key=ctx.user_key,
            conversation_id=ctx.conversation_id,
        )
    except Exception as e:
        logger.exception("propose_canonical_mapping token issue failed")
        return ToolResult(
            ok=False,
            summary=f"Could not prepare confirmation: {e}",
            data={"error": str(e)},
        )

    n_add, n_rem = len(effective_add), len(effective_remove)
    bits: list[str] = []
    if n_add:
        bits.append(f"+{n_add} canonical{'s' if n_add != 1 else ''}")
    if n_rem:
        bits.append(f"−{n_rem} canonical{'s' if n_rem != 1 else ''}")
    summary = (
        f"Ready to update '{uc_name}' source requirements "
        f"({', '.join(bits)}). User must confirm in the chat panel."
    )

    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "kind": "proposal",
            "intent": INTENT_UPDATE_USE_CASE_CANONICALS,
            "resource": "canonicals",
            "confirm": {
                "token": token["token"],
                "expires_at": token["expires_at"],
            },
            "use_case": {
                "use_case_id": uc_id,
                "use_case_name": uc_name,
            },
            "additions": effective_add,
            "removals": [
                {
                    "name": n,
                    "current_necessity": (
                        str(current_by_name[n].get("necessity"))
                        if current_by_name.get(n) else None
                    ),
                }
                for n in effective_remove
            ],
            "skipped_noop": skipped_noop,
            "notice": _RESEED_WARNING_CANONICAL if effective_remove else None,
        },
        citations=[
            {
                "label": uc_name,
                "deeplink": f"/value-readiness?uc={uc_id}",
            }
        ],
    )


PROPOSE_CANONICAL_MAPPING = Tool(
    name="app_propose_canonical_mapping",
    description=(
        "Add or remove canonical source-system requirements on a use "
        "case. Pass any combination of `add` (with canonical name + "
        "necessity + optional data_need_excerpt) and `remove` (just "
        "canonical names). Does NOT write — returns a confirmation card "
        "showing the deltas; user must Confirm. Always call "
        "app_get_use_case first to see the current required canonicals "
        "so you don't propose a duplicate or remove something that "
        "isn't required. Use app_list_source_systems if you don't know "
        "the exact canonical name. Removals carry a reseed caveat: the "
        "LLM job may re-add the row on next reseed."
    ),
    args_model=ProposeCanonicalMappingArgs,
    handler=_propose_canonical_mapping,
)


# ---------------------------------------------------------------------------
# app_propose_use_case  (A2-4: full-create flow)
#
# This is the largest write the chat ships. One propose -> one confirm
# performs THREE writes:
#   1. INSERT into bhe_silver.use_cases (the parent row)
#   2. MERGE one row per affiliate into bhe_gold.use_case_affiliates
#   3. MERGE one row per canonical into bhe_gold.use_case_source_requirements
#
# Atomicity: deliberately best-effort with errors surfaced (NOT a hard
# transactional rollback). Cross-table transactions in Delta are messy
# and a partial-success state is still recoverable — the user can re-run
# `app_propose_affiliate_mapping` / `app_propose_canonical_mapping` on
# the new id to fill in any failed child rows. Rolling back the parent
# on a child failure would be worse: the user would have to start over.
# See `_exec_create_use_case` in confirm.py for the executor.
#
# Required-vs-optional: only `use_case_name` is strictly required.
# Affiliates and canonicals are optional but strongly preferred —
# without them the new use case is an orphan with no readiness signal.
# The system prompt nudges the model to gather them via clarifying
# turns before proposing.
# ---------------------------------------------------------------------------


# Default to in-progress status when the field is omitted: a chat-created
# row almost always represents work the user is actively scoping.
_DEFAULT_CREATE_STATUS = "in_progress"


class ProposeUseCaseArgs(BaseModel):
    """Create a brand-new use case (parent + optional child mappings).

    Pass affiliates/canonicals inline whenever you have them — issuing
    one confirmation card is much cleaner UX than asking the user to
    confirm the parent and then a follow-up A2-3 mapping update.
    """

    use_case_name: str = Field(
        ...,
        min_length=3,
        max_length=200,
        description=(
            "Display name. Must be unique (case-insensitive, trimmed) — "
            "the tool will reject if a use case with the same name "
            "already exists, in which case use app_propose_use_case_update "
            "to edit the existing row instead."
        ),
    )
    description: str | None = Field(
        default=None,
        description=(
            "Multi-sentence description of what the use case does and "
            "why it matters. Strongly preferred — drives the reseed "
            "job's automatic affiliate/canonical inference."
        ),
    )
    department: str | None = Field(
        default=None,
        description=(
            "Owning department. Use app_get_use_case on a similar "
            "existing use case to see what string format the catalog "
            "uses (e.g. 'T&D Operations', 'Generation')."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "Free-text category (e.g. 'Risk Management', 'Reliability', "
            "'Asset Performance'). Match an existing one if you can."
        ),
    )
    business_value: str | None = Field(
        default=None,
        description="One-sentence value proposition.",
    )
    value_rationale: str | None = Field(
        default=None,
        description=(
            "Multi-sentence justification of the dollar value. Cite "
            "concrete drivers (e.g. 'avoided 200 truck rolls/yr at "
            "$2K each')."
        ),
    )
    priority: str | None = Field(
        default=None,
        description=(
            "Priority. Allowed: 'High' | 'Medium' | 'Low' "
            "(case-insensitive). Defaults to 'Medium'."
        ),
    )
    estimated_value_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Estimated annual value in USD. Pass an absolute number "
            "(e.g. 45000000), not millions."
        ),
    )
    status: str | None = Field(
        default=None,
        description=(
            "Initial delivery status. Allowed: 'not_started' | "
            "'in_progress' | 'delivered' | 'on_hold'. Defaults to "
            "'in_progress' for chat-created use cases."
        ),
    )
    status_notes: str | None = Field(
        default=None,
        description="Optional initial status note.",
    )
    affiliates: list[AffiliateAdd] = Field(
        default_factory=list,
        description=(
            "Affiliates this use case applies to. Strongly preferred "
            "— a use case with no affiliates won't show up on any "
            "affiliate's coverage view. Use app_list_affiliates to "
            "find the exact names."
        ),
    )
    canonicals: list[CanonicalAdd] = Field(
        default_factory=list,
        description=(
            "Canonical source systems the use case requires. Drives "
            "the readiness/Sankey views. Use app_list_source_systems "
            "to find the exact names."
        ),
    )


def _propose_use_case(
    args: ProposeUseCaseArgs, ctx: ToolContext
) -> ToolResult:
    # Local import: router imports from this module's package indirectly
    # via the tool registry, so importing at module-load time would
    # create a cycle.
    from ..router import find_use_case_by_name

    name = (args.use_case_name or "").strip()
    if not name:
        return ToolResult(
            ok=False,
            summary="use_case_name is required.",
            data={"error": "missing_name"},
        )

    # 1. Name uniqueness — reject collisions early. Stricter than the UI
    #    POST endpoint by design (the LLM might retry the same create
    #    on every regenerate, so we prevent accidental duplicates).
    existing = find_use_case_by_name(name)
    if existing:
        return ToolResult(
            ok=False,
            summary=(
                f"A use case named {name!r} already exists "
                f"(id={existing.get('id')}). To edit it, use "
                "app_propose_use_case_update. To create a different "
                "use case, pick a distinguishing name."
            ),
            data={
                "error": "name_collision",
                "existing_id": existing.get("id"),
            },
        )

    # 2. Priority + status normalization.
    try:
        norm_priority = _normalize_priority(args.priority) or "Medium"
    except ValueError as e:
        return ToolResult(
            ok=False, summary=str(e), data={"error": "invalid_priority"}
        )
    norm_status = (args.status or _DEFAULT_CREATE_STATUS).strip().lower()
    if norm_status not in _VALID_STATUSES:
        return ToolResult(
            ok=False,
            summary=(
                f"Invalid status {args.status!r}. "
                f"Allowed: {', '.join(_VALID_STATUSES)}"
            ),
            data={"error": "invalid_status"},
        )

    # 3. Per-child validation. Same checks as the A2-3 mapping tools so
    #    bad input doesn't get a token. We intentionally do these BEFORE
    #    issuing the token so the model gets corrective feedback on the
    #    same turn instead of a confirm-time failure.
    aff_seen: set[str] = set()
    for item in args.affiliates:
        if item.applicability not in _VALID_APPLICABILITY:
            return ToolResult(
                ok=False,
                summary=(
                    f"Invalid applicability {item.applicability!r} for "
                    f"{item.name!r}. Allowed: "
                    f"{', '.join(_VALID_APPLICABILITY)}"
                ),
                data={"error": "invalid_applicability"},
            )
        nm = (item.name or "").strip()
        if not nm:
            return ToolResult(
                ok=False,
                summary="Affiliate entry is missing a name.",
                data={"error": "missing_affiliate_name"},
            )
        if nm in aff_seen:
            return ToolResult(
                ok=False,
                summary=f"Duplicate affiliate {nm!r} in the list.",
                data={"error": "duplicate_affiliate"},
            )
        aff_seen.add(nm)

    canon_seen: set[str] = set()
    for item in args.canonicals:
        if item.necessity not in _VALID_NECESSITY:
            return ToolResult(
                ok=False,
                summary=(
                    f"Invalid necessity {item.necessity!r} for "
                    f"{item.canonical!r}. Allowed: "
                    f"{', '.join(_VALID_NECESSITY)}"
                ),
                data={"error": "invalid_necessity"},
            )
        cn = (item.canonical or "").strip()
        if not cn:
            return ToolResult(
                ok=False,
                summary="Canonical entry is missing a `canonical` name.",
                data={"error": "missing_canonical_name"},
            )
        if cn in canon_seen:
            return ToolResult(
                ok=False,
                summary=f"Duplicate canonical {cn!r} in the list.",
                data={"error": "duplicate_canonical"},
            )
        canon_seen.add(cn)

    # 4. Validate that every affiliate/canonical name exists in the
    #    respective dim table. Skipping the lookups when nothing was
    #    passed avoids a wasted round-trip on the common parent-only
    #    create case.
    gold = get_gold_schema()
    if args.affiliates:
        try:
            valid_aff_rows = execute_query(
                f"""
                SELECT affiliate_name
                FROM {fqn(gold, 'affiliates')}
                WHERE COALESCE(is_active, true) = true
                """,
                tag_overrides={"submodule": "chat.tool.propose_use_case"},
            )
            valid_aff = {
                str(r.get("affiliate_name"))
                for r in valid_aff_rows
                if r.get("affiliate_name")
            }
            unknown = [it.name for it in args.affiliates if it.name not in valid_aff]
            if unknown:
                return ToolResult(
                    ok=False,
                    summary=(
                        f"Unknown affiliate(s): {', '.join(unknown)}. "
                        "Use app_list_affiliates to find the exact name."
                    ),
                    data={
                        "error": "unknown_affiliate",
                        "unknown": unknown,
                    },
                )
        except Exception as e:
            logger.exception("propose_use_case affiliate validation failed")
            return ToolResult(
                ok=False,
                summary=f"Affiliate validation failed: {e}",
                data={"error": str(e)},
            )

    if args.canonicals:
        try:
            valid_canon_rows = execute_query(
                f"""
                SELECT canonical
                FROM {fqn(gold, 'source_system_canonical')}
                WHERE COALESCE(is_active, true) = true
                """,
                tag_overrides={"submodule": "chat.tool.propose_use_case"},
            )
            valid_canon = {
                str(r.get("canonical"))
                for r in valid_canon_rows
                if r.get("canonical")
            }
            unknown = [
                it.canonical for it in args.canonicals if it.canonical not in valid_canon
            ]
            if unknown:
                return ToolResult(
                    ok=False,
                    summary=(
                        f"Unknown canonical source(s): {', '.join(unknown)}. "
                        "Use app_list_source_systems to find the exact name."
                    ),
                    data={
                        "error": "unknown_canonical",
                        "unknown": unknown,
                    },
                )
        except Exception as e:
            logger.exception("propose_use_case canonical validation failed")
            return ToolResult(
                ok=False,
                summary=f"Canonical validation failed: {e}",
                data={"error": str(e)},
            )

    # 5. Build the executor payload. `fields` matches insert_use_case_row
    #    kwargs; child arrays match the executor loop's expectations.
    fields = {
        "use_case_name": name,
        "description": (args.description or "").strip(),
        "department": (args.department or "").strip(),
        "category": (args.category or "").strip(),
        "business_value": (args.business_value or "").strip(),
        "value_rationale": (args.value_rationale or "").strip(),
        "priority": norm_priority,
        "estimated_value_usd": (
            float(args.estimated_value_usd)
            if args.estimated_value_usd is not None
            else None
        ),
        "status": norm_status,
        "status_notes": (args.status_notes or "").strip(),
    }
    payload_affiliates = [
        {
            "affiliate_name": it.name.strip(),
            "applicability": it.applicability,
            "rationale": (it.rationale or "").strip(),
        }
        for it in args.affiliates
    ]
    payload_canonicals = [
        {
            "canonical": it.canonical.strip(),
            "necessity": it.necessity,
            "data_need_excerpt": (it.data_need_excerpt or "").strip(),
        }
        for it in args.canonicals
    ]
    payload = {
        "fields": fields,
        "affiliates": payload_affiliates,
        "canonicals": payload_canonicals,
    }

    # `target_id` for create is the proposed name (we don't have an id
    # yet) — only used for audit + UI; the executor mints the real id.
    try:
        token = issue_token(
            intent=INTENT_CREATE_USE_CASE,
            target_id=name,
            payload=payload,
            user_key=ctx.user_key,
            conversation_id=ctx.conversation_id,
        )
    except Exception as e:
        logger.exception("propose_use_case token issue failed")
        return ToolResult(
            ok=False,
            summary=f"Could not prepare confirmation: {e}",
            data={"error": str(e)},
        )

    n_aff = len(payload_affiliates)
    n_canon = len(payload_canonicals)
    bits = [f"+1 use case '{name}'"]
    if n_aff:
        bits.append(f"+{n_aff} affiliate{'s' if n_aff != 1 else ''}")
    if n_canon:
        bits.append(f"+{n_canon} canonical{'s' if n_canon != 1 else ''}")

    return ToolResult(
        ok=True,
        summary=(
            f"Ready to create use case '{name}' "
            f"({', '.join(bits[1:])}{' — ' if bits[1:] else ''}"
            "all writes pending). User must confirm in the chat panel."
        ),
        data={
            "kind": "proposal",
            "intent": INTENT_CREATE_USE_CASE,
            "confirm": {
                "token": token["token"],
                "expires_at": token["expires_at"],
            },
            # FE renders as a "create" card (no before/after, just
            # field rows + two grouped lists).
            "fields": fields,
            "field_order": [
                "use_case_name",
                "description",
                "department",
                "category",
                "business_value",
                "value_rationale",
                "priority",
                "estimated_value_usd",
                "status",
            ],
            "field_labels": {
                **_FIELD_LABELS,
                "status": "Status",
            },
            "affiliates": payload_affiliates,
            "canonicals": payload_canonicals,
            "counts": {
                "affiliates": n_aff,
                "canonicals": n_canon,
            },
        },
        # No deeplink yet — the use case doesn't exist until confirm.
        # The FE post-confirm flow can navigate to /value-readiness?uc=<new_id>.
        citations=[],
    )


PROPOSE_USE_CASE = Tool(
    name="app_propose_use_case",
    description=(
        "Create a brand-new use case (parent row + optional initial "
        "affiliate and canonical mappings) in ONE confirmation. Does "
        "NOT write — returns a confirmation card showing the proposed "
        "fields and any child mappings; the user must Confirm before "
        "anything is persisted. The name must be unique (case-"
        "insensitive); duplicate names are rejected. Use "
        "app_list_affiliates and app_list_source_systems to validate "
        "exact names before calling. Strongly prefer providing "
        "affiliates inline (a use case with no affiliates won't show "
        "up on any affiliate's coverage view). For editing an "
        "existing use case, use app_propose_use_case_update instead."
    ),
    args_model=ProposeUseCaseArgs,
    handler=_propose_use_case,
)


# ---------------------------------------------------------------------------
# app_propose_schema_update  (A3-1: schema editing)
#
# Multi-field update on a logical schema in bhe_silver.silver_schemas.
# Logical because a schema name typically lives in N catalogs (one per
# environment); the chat write tool defaults to "update them all" so
# users don't have to think about catalogs. The propose tool reports
# the affected catalog list AND surfaces field-level divergence
# ("dev says X, prod says Y — your patch will collapse them") so the
# user knows what they're flattening before they confirm.
#
# Re-uses the EditCenter PUT endpoint's helpers via router.py:
#   - SCHEMA_EDITABLE_FIELDS: tuple of fields the chat can touch
#   - find_silver_schema_rows: per-catalog before-values for the diff
#   - update_silver_schema_rows: the executor's write (not called here)
# ---------------------------------------------------------------------------


# Mirror SCHEMA_EDITABLE_FIELDS in router.py. Kept as a local copy so
# this tool file remains self-contained for label rendering, but the
# AUTHORITATIVE list is on the router (we import + assert below).
_SCHEMA_EDITABLE: tuple[str, ...] = (
    "ai_definition",
    "business_friendly_name",
    "suggested_department",
    "suggested_domain",
    "data_sensitivity",
)
_SCHEMA_FIELD_LABELS: dict[str, str] = {
    "ai_definition": "Definition",
    "business_friendly_name": "Business name",
    "suggested_department": "Department",
    "suggested_domain": "Domain",
    "data_sensitivity": "Sensitivity",
}
# Cap definition length so a runaway LLM can't paste a 50-page essay
# into the column. Matches the realistic UI length.
_MAX_DEFINITION_LEN = 2000
# Friendly canonical sensitivities. We DON'T enforce — the data is
# free-text today — but we tell the model what's typical so it
# doesn't invent novel labels.
_TYPICAL_SENSITIVITIES: tuple[str, ...] = ("public", "internal", "confidential", "restricted")


class ProposeSchemaUpdateArgs(BaseModel):
    """Patch a logical schema with one or more editable fields.

    All fields are optional — pass only what you want to change.
    Empty/whitespace strings are treated as "no value provided" and
    dropped (NOT as "clear the field"); to clear a field today, the
    user must use the UI. Matches the behavior of
    `app_propose_use_case_update`.
    """

    schema_name: str = Field(
        ...,
        description=(
            "Logical schema name (e.g. 'maximo', 'pi_historian'). The "
            "chat updates ALL physical rows for this name across "
            "dev/qa/prod catalogs by default — pass `catalog_filter` "
            "to narrow to specific catalogs."
        ),
    )
    catalog_filter: list[str] | None = Field(
        default=None,
        description=(
            "Optional. If the user said 'update only the prod copy' or "
            "named specific catalogs, pass them here. Names must "
            "exactly match the catalog_name column on silver_schemas. "
            "Default (omit) updates all catalogs for the schema."
        ),
    )
    ai_definition: str | None = Field(
        default=None,
        max_length=_MAX_DEFINITION_LEN,
        description=(
            "AI/business definition of the schema's purpose. 1-3 "
            "sentences typical; max 2000 chars. This is the most "
            "commonly edited field — the AI enrichment job often "
            "generates a vague first pass."
        ),
    )
    business_friendly_name: str | None = Field(
        default=None,
        description=(
            "Display name (e.g. 'Maximo Asset Management') vs. the "
            "raw schema_name ('maximo'). Title case, no underscores."
        ),
    )
    suggested_department: str | None = Field(
        default=None,
        description=(
            "Owning department. Free-text but match an existing one "
            "from app_list_affiliates / app_research_schema if you "
            "can — the schema explorer page groups on this column."
        ),
    )
    suggested_domain: str | None = Field(
        default=None,
        description=(
            "Data domain (e.g. 'Asset Management', 'Customer', "
            "'Finance', 'HR'). Match peers from app_research_schema."
        ),
    )
    data_sensitivity: str | None = Field(
        default=None,
        description=(
            "Sensitivity classification. Typical values: "
            f"{', '.join(_TYPICAL_SENSITIVITIES)}. Free-text in the "
            "DB (no enum constraint) — be conservative and reuse "
            "what peers already use."
        ),
    )


def _propose_schema_update(
    args: ProposeSchemaUpdateArgs, ctx: ToolContext
) -> ToolResult:
    # Local import to avoid the package-load cycle (router imports from
    # this module's package indirectly via the tool registry).
    from ..router import (
        SCHEMA_EDITABLE_FIELDS,
        find_silver_schema_rows,
    )

    # Sanity: keep the local label list in sync with the router's
    # source-of-truth tuple. Drifting would silently drop edits.
    assert set(_SCHEMA_EDITABLE) == set(SCHEMA_EDITABLE_FIELDS), (
        "SCHEMA_EDITABLE_FIELDS drift between router and propose tool"
    )

    schema_name = (args.schema_name or "").strip()
    if not schema_name:
        return ToolResult(
            ok=False,
            summary="schema_name is required.",
            data={"error": "missing_schema_name"},
        )

    # Build the proposed patch (only fields the model touched, with
    # whitespace stripped + empty-string dropped — same convention as
    # the use case update tool).
    proposed: dict[str, str] = {}
    for f in _SCHEMA_EDITABLE:
        v = getattr(args, f)
        if v is None:
            continue
        v = v.strip()
        if not v:
            continue
        proposed[f] = v

    if not proposed:
        return ToolResult(
            ok=False,
            summary=(
                "No fields to change. Pass at least one editable field "
                f"({', '.join(_SCHEMA_EDITABLE)})."
            ),
            data={"error": "empty_patch"},
        )

    # Soft sensitivity-vocabulary nudge. Don't reject — the column is
    # free-text and analysts may have established novel labels — but
    # warn so the model can flag the unusual choice to the user.
    sensitivity_warning: str | None = None
    if "data_sensitivity" in proposed:
        if proposed["data_sensitivity"].lower() not in _TYPICAL_SENSITIVITIES:
            sensitivity_warning = (
                f"data_sensitivity {proposed['data_sensitivity']!r} is "
                f"non-standard. Typical values: "
                f"{', '.join(_TYPICAL_SENSITIVITIES)}."
            )

    # Pull current rows so we can compute (a) per-catalog before-values,
    # (b) divergence flags, (c) no-op fields.
    rows = find_silver_schema_rows(schema_name)
    if not rows:
        return ToolResult(
            ok=True,
            summary=f"No schema named {schema_name!r} found.",
            data={"schema": None, "error": "not_found"},
        )

    # Apply catalog_filter if provided. We'd rather error early on a
    # bad catalog name than silently update zero rows.
    all_catalogs = sorted({str(r.get("catalog_name")) for r in rows if r.get("catalog_name")})
    if args.catalog_filter:
        unknown = [c for c in args.catalog_filter if c not in all_catalogs]
        if unknown:
            return ToolResult(
                ok=False,
                summary=(
                    f"Unknown catalog(s) for schema {schema_name!r}: "
                    f"{', '.join(unknown)}. Available: {', '.join(all_catalogs)}"
                ),
                data={"error": "unknown_catalog", "unknown": unknown},
            )
        target_catalogs = list(args.catalog_filter)
        target_rows = [r for r in rows if r.get("catalog_name") in target_catalogs]
    else:
        target_catalogs = all_catalogs
        target_rows = rows

    # Detect per-field divergence across the target rows AND no-op
    # fields (proposed value equals current value on EVERY target row).
    before_per_field: dict[str, dict[str, str | None]] = {}
    after_per_field: dict[str, str] = {}
    field_divergence: dict[str, bool] = {}
    effective_patch: dict[str, str] = {}

    for f, new_val in proposed.items():
        # Per-catalog current values.
        per_cat: dict[str, str | None] = {}
        all_match_new = True
        seen_values: set[str] = set()
        for r in target_rows:
            cur = (r.get(f) or "").strip() or None
            per_cat[str(r.get("catalog_name"))] = cur
            if (cur or "") != new_val:
                all_match_new = False
            seen_values.add(cur or "")
        if all_match_new:
            # Every target row already has this value; skip.
            continue
        before_per_field[f] = per_cat
        after_per_field[f] = new_val
        field_divergence[f] = len(seen_values) > 1
        effective_patch[f] = new_val

    if not effective_patch:
        return ToolResult(
            ok=True,
            summary=(
                f"All proposed values for schema {schema_name!r} already "
                "match the current rows. Nothing to confirm."
            ),
            data={
                "no_change": True,
                "schema_name": schema_name,
                "catalogs": target_catalogs,
            },
        )

    # Compose warnings list. Divergence-per-field shown in the card
    # already (via before_per_field), but we also surface a top-level
    # warning so the model can mention it in prose.
    warnings: list[str] = []
    diverged_fields = sorted(f for f, d in field_divergence.items() if d)
    if diverged_fields:
        labels = [_SCHEMA_FIELD_LABELS.get(f, f) for f in diverged_fields]
        warnings.append(
            f"This patch will collapse divergent values across catalogs "
            f"on: {', '.join(labels)}. Tell the user before confirming."
        )
    if sensitivity_warning:
        warnings.append(sensitivity_warning)

    payload = {
        "schema_name": schema_name,
        "catalogs": target_catalogs,
        "patch": effective_patch,
    }
    try:
        token = issue_token(
            intent=INTENT_UPDATE_SCHEMA,
            target_id=schema_name,
            payload=payload,
            user_key=ctx.user_key,
            conversation_id=ctx.conversation_id,
        )
    except Exception as e:
        logger.exception("propose_schema_update token issue failed")
        return ToolResult(
            ok=False,
            summary=f"Could not prepare confirmation: {e}",
            data={"error": str(e)},
        )

    n = len(effective_patch)
    n_cat = len(target_catalogs)
    summary = (
        f"Ready to update schema {schema_name!r} — "
        f"{n} field{'s' if n != 1 else ''} changed across "
        f"{n_cat} catalog{'s' if n_cat != 1 else ''}. "
        "User must confirm in the chat panel."
    )
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "kind": "proposal",
            "intent": INTENT_UPDATE_SCHEMA,
            "confirm": {
                "token": token["token"],
                "expires_at": token["expires_at"],
            },
            "schema": {
                "schema_name": schema_name,
                "catalogs": target_catalogs,
                "all_catalogs": all_catalogs,
                "narrowed": bool(args.catalog_filter)
                and len(target_catalogs) < len(all_catalogs),
            },
            # FE renders a per-field section. before_per_field is keyed
            # by catalog so the card can show "dev: X · prod: Y → Z"
            # when divergent, or just "X → Z" when all rows agree.
            "field_order": [
                f for f in _SCHEMA_EDITABLE if f in effective_patch
            ],
            "field_labels": _SCHEMA_FIELD_LABELS,
            "before_per_catalog": before_per_field,
            "after": after_per_field,
            "divergent_fields": diverged_fields,
            "warnings": warnings,
        },
        citations=[
            {
                "label": schema_name,
                "deeplink": f"/explorer?schema={schema_name}",
            }
        ],
    )


PROPOSE_SCHEMA_UPDATE = Tool(
    name="app_propose_schema_update",
    description=(
        "Propose edits to a schema's AI-generated metadata: "
        "ai_definition, business_friendly_name, suggested_department, "
        "suggested_domain, data_sensitivity. By default updates all "
        "physical catalogs the schema appears in (dev/qa/prod) so the "
        "definition stays consistent across environments — pass "
        "`catalog_filter` to narrow. Does NOT write — returns a "
        "confirmation card showing per-catalog before-values + the "
        "proposed values; user must Confirm. ALWAYS call "
        "`app_research_schema` first so the diff card has real "
        "context AND your suggestions match catalog conventions. If "
        "the schema's current values diverge across catalogs (dev "
        "says X, prod says Y), the card shows that AND the tool "
        "returns a warning — surface it to the user before confirming."
    ),
    args_model=ProposeSchemaUpdateArgs,
    handler=_propose_schema_update,
)
