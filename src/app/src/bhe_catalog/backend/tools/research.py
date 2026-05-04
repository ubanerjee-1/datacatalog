"""Research tools — pure-read aggregators that chain multiple SQL
lookups + heuristics into ONE structured brief, so the model gets
peer-grounded suggestions instead of having to manually fan out
multiple read tools and aggregate the results in its head.

  - `app_research_use_case` (Phase A2-5) — for use case drafts.
  - `app_research_schema`   (Phase A3-1) — for schema metadata edits.

Both share the same module-level helpers (tokenizer, stopword list,
counters), the same "suggestions, not prescriptions" philosophy, and
return shapes that mirror their corresponding propose_* tool's args
so the model can copy fields directly into the next call.

---

`app_research_use_case` — chain compression for the "draft a new
use case from a topic" workflow. Today the model has to call:

  1. app_search_use_cases  — find similar UCs (collision check + style)
  2. app_list_affiliates   — verify affiliate names
  3. app_list_source_systems — verify canonical names
  4. app_get_use_case (xN) — pull mappings off similar UCs to seed
                              suggested affiliates / canonicals / value

…and then mentally aggregate the results into a propose call. That works
but is slow (4-6 tool calls), brittle (the model can miss obvious
adjacent UCs), and bloats the system prompt with "remember to also do X".

This tool does the chain server-side and returns a STRUCTURED brief
whose top-level keys mirror `ProposeUseCaseArgs` (see proposals.py) so
the model can copy fields directly into a downstream
`app_propose_use_case` call.

Output shape (stable contract — FE preview card depends on it):

  {
    "kind": "research_brief",
    "topic": "<the input>",
    "tokens_used": ["..."],          # for transparency
    "matched_count": int,
    "similar_use_cases": [{...}],    # top-N matches, score-ordered
    "suggestions": {
      "departments":  [{name, count}],
      "categories":   [{name, count}],
      "priority":     "Medium",
      "affiliates":   [{name, count, primary_count}],
      "canonicals":   [{canonical, count, must_have_count, sample_excerpts}],
      "value": {min, median, max, count}
    },
    "warnings": ["..."],             # e.g. "topic too vague", "exact name match exists"
  }

Design notes:
  - SQL, not Genie. Search space is hundreds of UCs; substring + keyword
    scoring is plenty. Genie adds latency + non-determinism for no win.
  - Suggestions, not prescriptions. The model still has to call
    app_propose_use_case and the user still has to confirm — this tool
    only feeds the model better context.
  - Tokenization is intentionally simple (lowercase + word split + 4-char
    stopword list). A pre-trained tokenizer would be over-engineering;
    we'd rather miss a synonym than ship a flaky scoring function.
  - Score weights: name-hit = 3, description-hit = 1. Empirically, a
    substring in the name is a much stronger signal than one in a
    paragraph of description text.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from ..db import execute_query, fqn, get_gold_schema, get_silver_schema
from ._base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


# Tokens shorter than this contribute almost no signal but blow up the
# CASE-WHEN clause (every token = 2 LIKE comparisons). Keep it small.
_MIN_TOKEN_LEN = 4
# A small stopword list — common English filler that would otherwise
# match nearly every UC. Industry-specific tokens (energy, grid, asset)
# are intentionally NOT here; those ARE meaningful catalog signals.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "from", "this", "that", "into",
        "over", "about", "across", "case", "cases", "uses",
        "data", "system", "systems",  # too generic in a data catalog
        "new", "create", "build", "make", "want", "need", "needs",
        "should", "would", "could", "please", "show", "find", "list",
    }
)
# Cap on tokens passed to SQL. Each token adds 2 LIKE clauses; a topic
# with 30 tokens would generate a 60-clause WHERE. 8 is plenty for the
# user phrasing we see ("AI-driven wildfire mitigation in PNW").
_MAX_TOKENS = 8
# Cap on similar UCs we pull mappings for. Each adds rows to the child
# IN-list queries; 10 is the sweet spot for signal vs latency.
_MAX_SIMILAR = 10


class ResearchUseCaseArgs(BaseModel):
    """Inputs for `app_research_use_case`.

    `topic` is the only required arg. Pass it as a short, descriptive
    phrase the way the user said it: 'wildfire mitigation drone imagery'
    rather than 'wildfire OR mitigation OR drone OR imagery' — the tool
    handles tokenization.
    """

    topic: str = Field(
        ...,
        min_length=3,
        max_length=400,
        description=(
            "Free-text description of the use case the user wants to "
            "create. Pass the user's own phrasing verbatim where "
            "possible — the tool will tokenize and search across "
            "existing use_case_name and description columns."
        ),
    )
    target_affiliate: str | None = Field(
        default=None,
        description=(
            "Optional. If the user named an affiliate ('for "
            "PacifiCorp'), pass it here. The brief will narrow value "
            "stats and surface that affiliate as the suggested primary."
        ),
    )
    limit_similar: int = Field(
        default=5,
        ge=1,
        le=_MAX_SIMILAR,
        description=(
            f"Max similar use cases to return (1-{_MAX_SIMILAR}). "
            "Aggregations always use ALL matches the SQL returned, so "
            "a small limit doesn't bias the suggestions — it just "
            "trims the visible list."
        ),
    )


def _q(s: str) -> str:
    return s.replace("'", "''")


def _tokenize(topic: str) -> list[str]:
    """Lowercase → word split → stopword/length filter → dedupe → cap.

    Uses a regex that splits on non-alphanumerics so 'AI-driven'
    becomes ['ai', 'driven'] and 'wildfire mitigation' becomes
    ['wildfire', 'mitigation']. Order is preserved for reproducible
    SQL generation.
    """
    raw = re.split(r"[^a-z0-9]+", topic.lower())
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if len(t) < _MIN_TOKEN_LEN:
            continue
        if t in _STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= _MAX_TOKENS:
            break
    return out


def _build_score_sql(
    silver: str, gold: str, tokens: list[str], target_affiliate: str | None
) -> str:
    """Generate the one-shot scoring SQL.

    Score = 3 × (#tokens hitting name) + 1 × (#tokens hitting description).
    A token that appears in BOTH name and description still scores 4 (3+1)
    — that's intentional, multiple positions = stronger signal.

    If `target_affiliate` is set, we DON'T filter by it (would over-narrow
    when the topic is novel for that affiliate); we just bonus +2 to UCs
    that already include it. Order-preserving and explicit.
    """
    name_clauses: list[str] = []
    desc_clauses: list[str] = []
    for t in tokens:
        ts = _q(t)
        name_clauses.append(
            f"CASE WHEN LOWER(uc.use_case_name) LIKE '%{ts}%' THEN 3 ELSE 0 END"
        )
        desc_clauses.append(
            f"CASE WHEN LOWER(COALESCE(uc.description,'')) LIKE '%{ts}%' "
            "THEN 1 ELSE 0 END"
        )
    score_expr = " + ".join(name_clauses + desc_clauses)
    affiliate_bonus = ""
    if target_affiliate:
        aff_q = _q(target_affiliate)
        # +2 if the UC is already mapped to the named affiliate. Done as
        # a correlated subquery (cheap; one-row LIMIT) so we don't have
        # to add another JOIN to the main query.
        affiliate_bonus = f"""
            + CASE WHEN EXISTS (
                SELECT 1 FROM {fqn(gold, 'use_case_affiliates')} uca
                WHERE uca.use_case_id = uc.id
                  AND uca.affiliate_name = '{aff_q}'
            ) THEN 2 ELSE 0 END
        """
    return f"""
        WITH affs AS (
            SELECT use_case_id, COLLECT_SET(affiliate_name) AS applicable_affiliates
            FROM {fqn(gold, 'use_case_affiliates')}
            GROUP BY use_case_id
        ),
        scored AS (
            SELECT
                uc.id AS use_case_id,
                uc.use_case_name,
                uc.description,
                uc.department,
                uc.category,
                uc.priority,
                COALESCE(uc.status, 'not_started') AS status,
                COALESCE(uc.estimated_value_usd, 0) AS estimated_value_usd,
                COALESCE(affs.applicable_affiliates, array()) AS applicable_affiliates,
                ({score_expr}{affiliate_bonus}) AS score
            FROM {fqn(silver, 'use_cases')} uc
            LEFT JOIN affs ON affs.use_case_id = uc.id
        )
        SELECT *
        FROM scored
        WHERE score > 0
        -- Tie-break by value (DESC) then name (ASC stable). Keeps
        -- high-value matches at the top of ties.
        ORDER BY score DESC, estimated_value_usd DESC NULLS LAST, use_case_name
        LIMIT {_MAX_SIMILAR}
    """


def _aggregate_suggestions(
    matches: list[dict[str, Any]],
    affiliate_rows: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
    target_affiliate: str | None,
) -> dict[str, Any]:
    """Compute the suggestions block from the matched UCs + their mappings.

    Counts are over UCs (not rows), so e.g. a canonical mapped to 3 of
    the matches gets count=3 even if it's also mapped to a 4th UC that
    didn't match the topic. This keeps the suggestions topic-relevant
    instead of biased toward globally-popular sources.
    """
    dept_counter: Counter[str] = Counter()
    cat_counter: Counter[str] = Counter()
    prio_counter: Counter[str] = Counter()

    for m in matches:
        d = (m.get("department") or "").strip()
        if d:
            dept_counter[d] += 1
        c = (m.get("category") or "").strip()
        if c:
            cat_counter[c] += 1
        p = (m.get("priority") or "").strip()
        if p:
            prio_counter[p] += 1

    # Affiliates: one row per (use_case_id, affiliate_name). Count
    # distinct UCs each affiliate appears in, plus how many of those
    # were 'primary' (signal of "owns this kind of use case").
    aff_uc: dict[str, set[str]] = {}
    aff_primary: dict[str, set[str]] = {}
    for r in affiliate_rows:
        n = r.get("affiliate_name")
        uc_id = r.get("use_case_id")
        if not n or not uc_id:
            continue
        aff_uc.setdefault(n, set()).add(uc_id)
        if (r.get("applicability") or "").lower() == "primary":
            aff_primary.setdefault(n, set()).add(uc_id)
    affiliate_suggestions = sorted(
        (
            {
                "name": n,
                "count": len(aff_uc[n]),
                "primary_count": len(aff_primary.get(n, set())),
                # If the user said "for PacifiCorp", PacifiCorp wins
                # the primary slot regardless of frequency.
                "applicability_hint": (
                    "primary"
                    if (target_affiliate and n == target_affiliate)
                    or len(aff_primary.get(n, set())) >= len(aff_uc[n]) / 2
                    else "secondary"
                ),
            }
            for n in aff_uc
        ),
        key=lambda r: (-r["count"], -r["primary_count"], r["name"]),
    )

    # Canonicals: same idea but tracking must_have ratio + sample
    # data_need_excerpts. Only keep up to 2 excerpt samples per canonical
    # (the model just needs a flavor; full excerpts bloat the payload).
    can_uc: dict[str, set[str]] = {}
    can_must: dict[str, set[str]] = {}
    can_excerpts: dict[str, list[str]] = {}
    for r in canonical_rows:
        c = r.get("required_canonical")
        uc_id = r.get("use_case_id")
        if not c or not uc_id:
            continue
        can_uc.setdefault(c, set()).add(uc_id)
        if (r.get("necessity") or "").lower() == "must_have":
            can_must.setdefault(c, set()).add(uc_id)
        ex = (r.get("data_need_excerpt") or "").strip()
        if ex and len(can_excerpts.setdefault(c, [])) < 2 and ex not in can_excerpts[c]:
            can_excerpts[c].append(ex[:140])
    canonical_suggestions = sorted(
        (
            {
                "canonical": c,
                "count": len(can_uc[c]),
                "must_have_count": len(can_must.get(c, set())),
                "necessity_hint": (
                    "must_have"
                    if len(can_must.get(c, set())) >= len(can_uc[c]) / 2
                    else "nice_to_have"
                ),
                "sample_excerpts": can_excerpts.get(c, []),
            }
            for c in can_uc
        ),
        key=lambda r: (-r["count"], -r["must_have_count"], r["canonical"]),
    )

    # Value stats: ignore zeros — they signal "not yet estimated"
    # rather than "literally zero", and they'd skew the median.
    # Defensive coercion: the SEA driver sometimes returns DECIMAL
    # columns as Python strings (e.g. '5000000'), which breaks `> 0`
    # comparisons. Float-cast in the filter, not just the projection.
    def _as_float(v: Any) -> float:
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    values = sorted(
        _as_float(m.get("estimated_value_usd"))
        for m in matches
        if _as_float(m.get("estimated_value_usd")) > 0
    )
    if values:
        n = len(values)
        median = (
            values[n // 2]
            if n % 2 == 1
            else (values[n // 2 - 1] + values[n // 2]) / 2
        )
        value_stats = {
            "min": values[0],
            "median": median,
            "max": values[-1],
            "count": n,
        }
    else:
        value_stats = {"min": None, "median": None, "max": None, "count": 0}

    return {
        # Top 3 of each (more is noise on the FE card; the model can
        # still see all of them in the FULL result if it expands).
        "departments": [
            {"name": n, "count": c} for n, c in dept_counter.most_common(3)
        ],
        "categories": [
            {"name": n, "count": c} for n, c in cat_counter.most_common(3)
        ],
        "priority": (
            prio_counter.most_common(1)[0][0] if prio_counter else "Medium"
        ),
        "affiliates": affiliate_suggestions[:5],
        "canonicals": canonical_suggestions[:6],
        "value": value_stats,
    }


def _research_use_case(
    args: ResearchUseCaseArgs, ctx: ToolContext
) -> ToolResult:
    silver = get_silver_schema()
    gold = get_gold_schema()

    tokens = _tokenize(args.topic)
    if not tokens:
        # Topic was all stopwords / too-short tokens. Surface the
        # condition in the error so the model can ask the user to
        # be more specific instead of returning empty silently.
        return ToolResult(
            ok=False,
            summary=(
                "Topic produced no usable search tokens (only stopwords "
                f"or short words). Try a more descriptive phrase like "
                "'drone imagery for vegetation management' instead of "
                f"'{args.topic}'."
            ),
            data={"error": "no_tokens", "topic": args.topic},
        )

    # 1. Score + fetch top similar UCs.
    try:
        score_rows = execute_query(
            _build_score_sql(silver, gold, tokens, args.target_affiliate),
            tag_overrides={"submodule": "chat.tool.research_use_case"},
        )
    except Exception as e:
        logger.exception("app_research_use_case scoring SQL failed")
        return ToolResult(
            ok=False, summary=f"Research lookup failed: {e}",
            data={"error": str(e)},
        )

    # Detect exact-name match BEFORE building the brief — surface as a
    # warning so the model can route the user to update instead of
    # create. We don't reject (the user might still want to research
    # adjacent UCs); just flag.
    warnings: list[str] = []
    topic_lower = args.topic.strip().lower()
    for row in score_rows:
        if (row.get("use_case_name") or "").strip().lower() == topic_lower:
            warnings.append(
                f"A use case named exactly {row.get('use_case_name')!r} "
                f"already exists (id={row.get('use_case_id')}). If the "
                "user wants to edit it, use app_propose_use_case_update."
            )
            break

    # 2. For ALL matches (capped at _MAX_SIMILAR), pull child mappings
    #    in two parallel-able queries. We do these even when the visible
    #    similar-list is small because aggregations use the full set.
    matched_ids = [r.get("use_case_id") for r in score_rows if r.get("use_case_id")]
    affiliate_rows: list[dict[str, Any]] = []
    canonical_rows: list[dict[str, Any]] = []
    if matched_ids:
        ids_sql = ", ".join(f"'{_q(str(i))}'" for i in matched_ids)
        try:
            affiliate_rows = execute_query(
                f"""
                SELECT use_case_id, affiliate_name, applicability
                FROM {fqn(gold, 'use_case_affiliates')}
                WHERE use_case_id IN ({ids_sql})
                """,
                tag_overrides={"submodule": "chat.tool.research_use_case"},
            )
            canonical_rows = execute_query(
                f"""
                SELECT use_case_id, required_canonical, necessity, data_need_excerpt
                FROM {fqn(gold, 'use_case_source_requirements')}
                WHERE use_case_id IN ({ids_sql})
                """,
                tag_overrides={"submodule": "chat.tool.research_use_case"},
            )
        except Exception as e:
            # Don't fail the whole brief — return what we have with a
            # warning. The model can still propose; it just won't have
            # mapping suggestions.
            logger.exception("app_research_use_case mapping fetch failed")
            warnings.append(f"Could not fetch mappings: {e}")

    # 3. Build the visible similar_use_cases list (truncated descriptions
    #    and Spark array normalization, same as search tool).
    visible_similar: list[dict[str, Any]] = []
    citations: list[dict[str, str]] = []
    for r in score_rows[: args.limit_similar]:
        affs = r.get("applicable_affiliates") or []
        if isinstance(affs, str):
            try:
                import json as _json

                affs = _json.loads(affs) or []
            except Exception:
                affs = []
        item = {
            "use_case_id": r.get("use_case_id"),
            "use_case_name": r.get("use_case_name"),
            "description": (r.get("description") or "").strip()[:240] or None,
            "department": r.get("department"),
            "category": r.get("category"),
            "priority": r.get("priority"),
            "status": r.get("status"),
            "estimated_value_usd": float(r.get("estimated_value_usd") or 0),
            "applicable_affiliates": affs,
            # Surface the score so the model can weigh "this is a
            # near-twin" (high) vs "this is loosely adjacent" (low).
            "match_score": int(r.get("score") or 0),
        }
        visible_similar.append(item)
        if item["use_case_id"]:
            citations.append({
                "label": item["use_case_name"] or item["use_case_id"],
                "deeplink": f"/value-readiness?uc={item['use_case_id']}",
            })

    suggestions = _aggregate_suggestions(
        score_rows, affiliate_rows, canonical_rows, args.target_affiliate
    )

    # If the user named a target affiliate but it didn't show up in any
    # matched UC's mapping, surface a hint — they'd be the FIRST UC
    # for that affiliate in this space. Useful context for the model.
    if args.target_affiliate:
        named = args.target_affiliate
        in_suggestions = any(a["name"] == named for a in suggestions["affiliates"])
        if not in_suggestions and visible_similar:
            warnings.append(
                f"None of the matched similar use cases are mapped to "
                f"{named}. This would be the first one in this topic "
                "area for that affiliate."
            )
        # Make sure the named affiliate appears in the suggestion list
        # even if no similar UC uses it — the model still needs to know
        # to add it.
        if not in_suggestions:
            suggestions["affiliates"].insert(
                0,
                {
                    "name": named,
                    "count": 0,
                    "primary_count": 0,
                    "applicability_hint": "primary",
                },
            )

    # Compose the summary. Keep it dense — this is what the model sees
    # in the next-turn message context if the result is too big to
    # include in full.
    summary_bits = [f"{len(score_rows)} similar use case{'s' if len(score_rows) != 1 else ''}"]
    if suggestions["value"]["count"] > 0:
        v = suggestions["value"]
        summary_bits.append(
            f"value range ${v['min']:,.0f}–${v['max']:,.0f} "
            f"(median ${v['median']:,.0f})"
        )
    if suggestions["affiliates"]:
        top_aff = suggestions["affiliates"][0]
        summary_bits.append(f"top affiliate {top_aff['name']} ({top_aff['count']})")
    if suggestions["canonicals"]:
        top_can = suggestions["canonicals"][0]
        summary_bits.append(
            f"top canonical {top_can['canonical']} ({top_can['count']})"
        )

    return ToolResult(
        ok=True,
        summary=f"Research brief — {', '.join(summary_bits)}",
        data={
            "kind": "research_brief",
            "topic": args.topic,
            "target_affiliate": args.target_affiliate,
            "tokens_used": tokens,
            "matched_count": len(score_rows),
            "similar_use_cases": visible_similar,
            "suggestions": suggestions,
            "warnings": warnings,
        },
        citations=citations,
    )


RESEARCH_USE_CASE = Tool(
    name="app_research_use_case",
    description=(
        "Research existing use cases similar to a free-text topic and "
        "return a STRUCTURED brief that can seed an `app_propose_use_case` "
        "call. The brief contains: (1) top similar use cases with their "
        "departments, categories, value, and affiliate scope; (2) "
        "suggested fields (top departments + categories + priority + "
        "value range pulled from those similar UCs); (3) suggested "
        "affiliate and canonical mappings ranked by frequency across "
        "the matches. Use this as the FIRST step whenever the user asks "
        "to create a new use case, especially if they gave only a name "
        "or vague topic — the brief tells you whether a near-twin "
        "already exists (warn the user before duplicating) and gives "
        "you concrete starting values for every field. Pass the user's "
        "verbatim phrasing as `topic`; pass the named affiliate (if "
        "any) as `target_affiliate`."
    ),
    args_model=ResearchUseCaseArgs,
    handler=_research_use_case,
)


# ===========================================================================
# app_research_schema  (Phase A3-1)
#
# Sibling of app_research_use_case but for schema metadata edits. Returns
# a brief whose `suggestions` block mirrors the `ProposeSchemaUpdateArgs`
# field set so the model can copy values directly into a downstream
# `app_propose_schema_update` call.
#
# Output shape (stable; FE depends on it):
#
#   {
#     "kind": "schema_research_brief",
#     "schema_name": "...",
#     "current": {                          # what the row(s) say today
#       "catalogs": ["dev_cat", ...],
#       "ai_definition": str | null,
#       "business_friendly_name": str | null,
#       "suggested_department": str | null,
#       "suggested_domain": str | null,
#       "data_sensitivity": str | null,
#       "table_count": int,
#       "tables_sample": [{name, ai_definition?}],
#     },
#     "tokens_used": ["..."],
#     "peer_count": int,
#     "peer_schemas": [{schema_name, business_friendly_name,
#                       suggested_domain, suggested_department,
#                       data_sensitivity, definition_excerpt, score}],
#     "suggestions": {
#       "suggested_domain":     [{name, count}],
#       "suggested_department": [{name, count}],
#       "data_sensitivity":     [{name, count}],
#       "sample_definitions":   ["...", "..."],   # 1-3 high-quality peer defs
#     },
#     "warnings": ["..."],
#   }
#
# Design parallels with app_research_use_case (don't reinvent):
#  - Same regex tokenizer + stopword filter, run over schema_name.
#    Schema names like 'maximo_assets' tokenize to ['maximo', 'assets'].
#  - Score peers by token-name-match=3, +2 if same program, +1 if same
#    suggested_domain (only if target HAS one).
#  - Aggregations are over the FULL match set (capped at _MAX_PEER_SCHEMAS),
#    not the visible top-N.
#  - "Suggestions, not prescriptions" — the model still has to call
#    propose_schema_update.
#
# Differences from app_research_use_case:
#  - We pull a sample of TABLES inside the target schema. Table names +
#    short defs are the strongest signal for inferring what a schema is
#    actually for ('customer_accounts' + 'invoices' → Customer/Billing
#    domain). The use-case tool didn't need this because UC descriptions
#    already carry that signal.
#  - For sample_definitions we PREFER user-edited peer defs over AI
#    drafts (is_user_edited=true gets sorted first), since hand-curated
#    text is much higher quality than first-pass LLM output. The use-case
#    tool didn't need this filter because UC descriptions were
#    hand-written end-to-end.
# ===========================================================================


# Cap on peer schemas pulled for aggregation. 12 is plenty — schema
# names cluster tightly (lots of 'crm_*', 'maximo_*' patterns), so
# the top-12 is usually all that's relevant.
_MAX_PEER_SCHEMAS = 12
# Cap on sample tables we surface inside the target schema. The model
# only needs a flavor; pulling all 200 tables of a Maximo schema would
# bloat the brief.
_MAX_SAMPLE_TABLES = 8


class ResearchSchemaArgs(BaseModel):
    """Inputs for `app_research_schema`."""

    schema_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "Logical schema name (e.g. 'maximo', 'pi_historian'). "
            "Same convention as `app_get_schema`. Tokenizer splits on "
            "underscores + hyphens so 'maximo_assets' matches both "
            "'maximo' and 'assets' peer schemas."
        ),
    )
    peer_limit: int = Field(
        default=8,
        ge=1,
        le=_MAX_PEER_SCHEMAS,
        description=(
            f"Max peer schemas to surface (1-{_MAX_PEER_SCHEMAS}). "
            "Aggregations always use ALL peers the SQL returned, "
            "so this only trims the visible list."
        ),
    )


def _format_definition_excerpt(text: str | None, max_len: int = 220) -> str | None:
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _research_schema(
    args: ResearchSchemaArgs, ctx: ToolContext
) -> ToolResult:
    silver = get_silver_schema()
    name = args.schema_name.strip()

    tokens = _tokenize(name)
    # Schema names can be very short (e.g. 'crm') and our 4-char filter
    # would reject everything. Fall back to the raw schema name as a
    # single search token so we still match peers.
    if not tokens:
        bare = name.lower()
        # Only fall back if the bare name is at least 2 chars (avoid
        # matching everything when someone passes a single letter).
        if len(bare) >= 2:
            tokens = [bare]
        else:
            return ToolResult(
                ok=False,
                summary=(
                    f"schema_name {name!r} is too short to search. "
                    "Pass at least 2 characters."
                ),
                data={"error": "schema_name_too_short"},
            )

    # 1. Pull the target schema's per-catalog rows. Need the current
    #    values for the brief's `current` block AND program/domain
    #    for the peer-scoring bonus.
    sn_q = _q(name)
    try:
        target_rows = execute_query(
            f"""
            SELECT catalog_name, schema_name, environment, program,
                   ai_definition, business_friendly_name,
                   suggested_department, suggested_domain, data_sensitivity
            FROM {fqn(silver, 'silver_schemas')}
            WHERE schema_name = '{sn_q}'
            ORDER BY catalog_name
            """,
            tag_overrides={"submodule": "chat.tool.research_schema"},
        )
    except Exception as e:
        logger.exception("app_research_schema target lookup failed")
        return ToolResult(
            ok=False, summary=f"Target lookup failed: {e}",
            data={"error": str(e)},
        )

    if not target_rows:
        # Even if the target doesn't exist, peer search may still be
        # useful (the user could be researching a name they're about
        # to create). But return early with a clear signal.
        return ToolResult(
            ok=True,
            summary=f"No schema named {name!r} found.",
            data={
                "kind": "schema_research_brief",
                "schema_name": name,
                "current": None,
                "tokens_used": tokens,
                "peer_count": 0,
                "peer_schemas": [],
                "suggestions": {
                    "suggested_domain": [],
                    "suggested_department": [],
                    "data_sensitivity": [],
                    "sample_definitions": [],
                },
                "warnings": [
                    f"Schema {name!r} doesn't exist in silver_schemas."
                ],
            },
        )

    # Pick a representative target row (prefer prod for the "current"
    # block, fall back to any). Per-catalog divergence is the propose
    # tool's job to surface; the research brief just needs ONE
    # current-state snapshot.
    representative = next(
        (r for r in target_rows if (r.get("environment") or "").lower() == "prod"),
        target_rows[0],
    )
    target_program = (representative.get("program") or "").strip() or None
    target_domain = (representative.get("suggested_domain") or "").strip() or None

    # 2. Pull a sample of tables in the schema. The chat infers domain
    #    largely from the table names + short defs (more signal than
    #    schema_name alone for generically-named schemas like 'core').
    #    Tables exist in EVERY catalog the schema is in; dedupe by name
    #    so 'customer_accounts' from dev/qa/prod shows once.
    tables_sample: list[dict] = []
    try:
        rows = execute_query(
            f"""
            SELECT table_name,
                   MAX(ai_definition) AS ai_definition,
                   MAX(business_friendly_name) AS business_friendly_name
            FROM {fqn(silver, 'silver_tables')}
            WHERE table_schema = '{sn_q}'
            GROUP BY table_name
            ORDER BY table_name
            LIMIT {_MAX_SAMPLE_TABLES}
            """,
            tag_overrides={"submodule": "chat.tool.research_schema"},
        )
        for r in rows:
            tables_sample.append({
                "name": r.get("table_name"),
                "business_friendly_name": (
                    (r.get("business_friendly_name") or "").strip() or None
                ),
                "ai_definition": _format_definition_excerpt(
                    r.get("ai_definition"), max_len=140
                ),
            })
        # Get total table count separately so the brief can show
        # "8 of 247 tables" instead of just "8 tables".
        count_rows = execute_query(
            f"""
            SELECT COUNT(DISTINCT table_name) AS n
            FROM {fqn(silver, 'silver_tables')}
            WHERE table_schema = '{sn_q}'
            """,
            tag_overrides={"submodule": "chat.tool.research_schema"},
        )
        table_count = int((count_rows[0] or {}).get("n") or 0) if count_rows else 0
    except Exception as e:
        logger.warning(f"research_schema tables lookup failed: {e}")
        table_count = 0

    # 3. Score + fetch peer schemas. Score = name-token-match
    #    (cumulative) + program-bonus + domain-bonus. Exclude the
    #    target schema itself.
    name_clauses: list[str] = []
    for t in tokens:
        ts = _q(t)
        name_clauses.append(
            f"CASE WHEN LOWER(s.schema_name) LIKE '%{ts}%' THEN 3 ELSE 0 END"
        )
    score_expr = " + ".join(name_clauses)
    program_bonus = ""
    if target_program:
        pg_q = _q(target_program)
        program_bonus = (
            f" + CASE WHEN s.program = '{pg_q}' THEN 2 ELSE 0 END"
        )
    domain_bonus = ""
    if target_domain:
        dm_q = _q(target_domain)
        domain_bonus = (
            f" + CASE WHEN COALESCE(s.suggested_domain, '') = '{dm_q}' "
            "THEN 1 ELSE 0 END"
        )

    # Group by schema_name so a peer that lives in dev+qa+prod shows
    # once (collapse to MAX/COALESCE per field, preferring user-edited
    # rows where possible).
    peers: list[dict] = []
    try:
        peers = execute_query(
            f"""
            WITH ranked AS (
                SELECT
                    s.schema_name,
                    MAX(s.business_friendly_name) AS business_friendly_name,
                    MAX(s.suggested_domain) AS suggested_domain,
                    MAX(s.suggested_department) AS suggested_department,
                    MAX(s.data_sensitivity) AS data_sensitivity,
                    MAX(s.ai_definition) AS ai_definition,
                    -- Prefer user-edited defs when ranking sample text:
                    -- 1 if any catalog has is_user_edited=true.
                    MAX(CASE WHEN COALESCE(s.is_user_edited, false)
                             THEN 1 ELSE 0 END) AS any_user_edited,
                    MAX({score_expr}{program_bonus}{domain_bonus}) AS score
                FROM {fqn(silver, 'silver_schemas')} s
                WHERE s.schema_name <> '{sn_q}'
                GROUP BY s.schema_name
            )
            SELECT *
            FROM ranked
            WHERE score > 0
            ORDER BY score DESC, any_user_edited DESC, schema_name
            LIMIT {_MAX_PEER_SCHEMAS}
            """,
            tag_overrides={"submodule": "chat.tool.research_schema"},
        )
    except Exception as e:
        logger.exception("app_research_schema peer scoring failed")
        return ToolResult(
            ok=False, summary=f"Peer lookup failed: {e}",
            data={"error": str(e)},
        )

    # 4. Aggregate suggestions across ALL peers. Same Counter pattern
    #    as research_use_case for consistency.
    domain_counter: Counter[str] = Counter()
    dept_counter: Counter[str] = Counter()
    sens_counter: Counter[str] = Counter()
    user_edited_defs: list[str] = []
    other_defs: list[str] = []
    for p in peers:
        d = (p.get("suggested_domain") or "").strip()
        if d:
            domain_counter[d] += 1
        dept = (p.get("suggested_department") or "").strip()
        if dept:
            dept_counter[dept] += 1
        s = (p.get("data_sensitivity") or "").strip()
        if s:
            sens_counter[s] += 1
        defn = _format_definition_excerpt(p.get("ai_definition"), max_len=240)
        if not defn:
            continue
        if int(p.get("any_user_edited") or 0) == 1:
            user_edited_defs.append(defn)
        else:
            other_defs.append(defn)

    # Sample 3 definitions — user-edited first (higher quality),
    # then padded with AI drafts if we don't have 3.
    sample_definitions = (user_edited_defs + other_defs)[:3]

    # 5. Build current snapshot.
    current_snapshot = {
        "catalogs": [str(r.get("catalog_name")) for r in target_rows],
        "ai_definition": _format_definition_excerpt(
            representative.get("ai_definition"), max_len=400
        ),
        "business_friendly_name": (
            (representative.get("business_friendly_name") or "").strip() or None
        ),
        "suggested_department": (
            (representative.get("suggested_department") or "").strip() or None
        ),
        "suggested_domain": target_domain,
        "data_sensitivity": (
            (representative.get("data_sensitivity") or "").strip() or None
        ),
        "table_count": table_count,
        "tables_sample": tables_sample,
    }

    # 6. Visible peer rows (first peer_limit; the model can still see
    #    the rest in the FULL result if it expands).
    visible_peers: list[dict] = []
    citations: list[dict[str, str]] = []
    for p in peers[: args.peer_limit]:
        nm = p.get("schema_name")
        item = {
            "schema_name": nm,
            "business_friendly_name": (
                (p.get("business_friendly_name") or "").strip() or None
            ),
            "suggested_domain": (
                (p.get("suggested_domain") or "").strip() or None
            ),
            "suggested_department": (
                (p.get("suggested_department") or "").strip() or None
            ),
            "data_sensitivity": (
                (p.get("data_sensitivity") or "").strip() or None
            ),
            "definition_excerpt": _format_definition_excerpt(
                p.get("ai_definition"), max_len=200
            ),
            "user_edited": bool(int(p.get("any_user_edited") or 0)),
            "score": int(p.get("score") or 0),
        }
        visible_peers.append(item)
        if nm:
            citations.append({
                "label": str(nm),
                "deeplink": f"/explorer?schema={nm}",
            })

    # 7. Warnings: surface "no peers" and "current is empty" — both
    #    affect what the model can confidently suggest.
    warnings: list[str] = []
    if not peers:
        warnings.append(
            f"No peer schemas found by token match on {tokens!r}. "
            "Suggestions will be empty; the model should ask the user "
            "for the domain/department directly."
        )
    if not current_snapshot["ai_definition"]:
        warnings.append(
            f"Schema {name!r} has no current ai_definition. Editing it "
            "is the most impactful change you can make."
        )

    suggestions = {
        "suggested_domain": [
            {"name": n, "count": c} for n, c in domain_counter.most_common(3)
        ],
        "suggested_department": [
            {"name": n, "count": c} for n, c in dept_counter.most_common(3)
        ],
        "data_sensitivity": [
            {"name": n, "count": c} for n, c in sens_counter.most_common(3)
        ],
        "sample_definitions": sample_definitions,
    }

    summary_bits = [
        f"{len(peers)} peer schema{'s' if len(peers) != 1 else ''}",
    ]
    if suggestions["suggested_domain"]:
        top = suggestions["suggested_domain"][0]
        summary_bits.append(f"top domain {top['name']} ({top['count']})")
    if suggestions["suggested_department"]:
        top = suggestions["suggested_department"][0]
        summary_bits.append(f"top dept {top['name']} ({top['count']})")
    summary_bits.append(f"{table_count} table{'s' if table_count != 1 else ''}")

    return ToolResult(
        ok=True,
        summary=f"Schema research — {', '.join(summary_bits)}",
        data={
            "kind": "schema_research_brief",
            "schema_name": name,
            "current": current_snapshot,
            "tokens_used": tokens,
            "peer_count": len(peers),
            "peer_schemas": visible_peers,
            "suggestions": suggestions,
            "warnings": warnings,
        },
        citations=citations,
    )


RESEARCH_SCHEMA = Tool(
    name="app_research_schema",
    description=(
        "Research a schema's metadata: pulls the schema's CURRENT "
        "values across catalogs (definition / business name / "
        "department / domain / sensitivity), a sample of its tables, "
        "and PEER schemas (by name-keyword + same program + same "
        "domain). The brief returns aggregated suggestions for "
        "domain / department / sensitivity ranked by frequency among "
        "peers, plus 1-3 sample peer definitions to mimic the "
        "catalog's writing style. Use this as the FIRST step before "
        "any `app_propose_schema_update` call — the model is much "
        "better at writing definitions when it can read peer "
        "definitions of similar schemas. The brief also surfaces "
        "warnings (e.g. 'schema has no current definition — high-"
        "impact edit', 'no peers found — ask user directly')."
    ),
    args_model=ResearchSchemaArgs,
    handler=_research_schema,
)
