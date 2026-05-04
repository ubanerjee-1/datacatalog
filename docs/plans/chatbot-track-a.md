# Chatbot — Track A Plan

**Status:** Draft
**Owner:** TBD
**Last updated:** 2026-04-16
**Scope:** In-app conversational assistant. Three phases: read-only Q&A, guided creation, long-running ops & deep research.

---

## 1. Goal

Add a single, persistent chat surface to the BHE Data Catalog that lets users:

1. **Ask questions** about the catalog ("which use cases unlock the most value at PacifiCorp?", "show schemas missing definitions in NV Energy") and get answers grounded in our data, with deep links into the existing app drawers/pages.
2. **Create and edit catalog entities** through guided conversation ("create a use case for drone-image fault detection at PacifiCorp"), with an explicit human confirmation step before any write hits the database.
3. **Trigger and monitor long-running operations** (AI enrichment, deep research) from the chat, with progress shown inline.

The chat lives in **one** UI surface across all three phases — no separate Genie embed.

## 2. Non-goals

- Exposing our APIs to third-party agents (Track B is shelved per product call on 2026-04-16).
- Multi-user collaborative chat. Each conversation is single-user.
- Voice / audio input.
- Replacing the existing app navigation. The chat augments, never replaces.

## 3. Architecture (one chat, many tools)

```
   ┌────────────────────────────┐
   │  Chat panel (React)        │   bottom-left FAB → side drawer
   │  - message list + input    │
   │  - tool-call cards         │
   │  - confirmation cards      │
   │  - chart renderer          │
   └─────────────┬──────────────┘
                 │ POST /api/chat/messages (SSE stream)
                 ▼
   ┌────────────────────────────┐
   │  Chat service (FastAPI)    │
   │  - conversation persistence│
   │  - identity from           │
   │    X-Forwarded-Email       │
   │  - tool dispatcher         │
   └──────┬───────────────┬─────┘
          │               │
          ▼               ▼
   ┌─────────────┐   ┌──────────────────────────┐
   │ Foundation  │   │ Tools (Python functions) │
   │ Model       │   │  - app_search_use_cases  │
   │ (tool-call) │   │  - app_get_use_case      │
   │             │   │  - app_value_summary     │
   │             │   │  - app_create_use_case_* │
   │             │   │  - genie_ask             │
   │             │   │  - enrichment_run/status │
   │             │   │  - research_*            │
   └─────────────┘   └──────────────────────────┘
                            │
                            └─→ existing service layer / Genie REST
```

**Key decisions:**

- **Chat does NOT go through MCP.** Tools are direct Python calls into our existing service layer — faster, cheaper, easier to debug. MCP is for external agents (out of scope).
- **Genie is one tool, not the whole product.** When the model decides a question is "give me arbitrary data", it calls `genie_ask`. When it's "act on the app", it calls our typed tools.
- **Writes are two-step.** No tool ever writes directly. Tools return *proposals*; the UI renders a confirmation card; the user clicks Confirm; only then does the FE call the existing REST endpoint as the user.

## 4. LLM and Genie

| Concern | Choice | Notes |
|---|---|---|
| LLM | `databricks-claude-opus-4-7` (Claude Opus 4.7, AI Gateway, tools-enabled, 1M-token context) | Confirmed available in workspace via AI Playground; hosted by Databricks |
| Streaming | SSE from FastAPI to React | `text/event-stream`, one event per token + structured tool-call/tool-result frames |
| Genie space | `BHE Catalog Explorer` — space_id `01f13f2d12271caeb5f26d3762ea9d75` (14 tables, 7 seed questions) | Definition at `resources/genie_spaces/bhe_catalog_explorer.json`; bootstrap script at `src/app/create_genie_space.py`; DAB resource at `resources/bhe_genie_catalog_explorer.genie_space.yml` |
| Genie API | Conversation API (`/api/2.0/genie/spaces/{id}/conversations`) | Stateful conversation per chat thread; we keep `genie_conversation_id` on our `chat_conversations` row |
| Chart rendering | Vega-Lite (already a dep via shadcn / can add `react-vega`) | Genie returns column data + chart spec; we render in-chat |

**Why not put the LLM behind Model Serving as an Agent?**
We considered packaging the agent as an MLflow ChatAgent on Model Serving. Decision: not for V1. It adds deployment overhead, an extra hop, and we lose direct access to in-process helpers like `_resolve_canonical`. Revisit if/when we expose the agent externally.

## 5. Phases

### Phase A1 — Read-only Q&A — **STATUS: COMPLETE 2026-04-30**

**Deliverables**
1. Chat panel UI (FAB, side drawer, message list, input box, tool-call card with per-tool result preview). ✅
2. `chat_conversations` and `chat_messages` Delta tables in `bhe_silver`. ✅
3. `POST /api/chat/messages` SSE endpoint with `start | token | tool_call | tool_result | done | error` events. ✅
4. Tool registry with read-only tools (10 shipped): ✅
   - `app_search_use_cases(query, department?, affiliate?, limit)` ✅
   - `app_get_use_case(use_case_id, affiliate?)` ✅
   - `app_search_schemas(query, environment?, program?, domain?, missing_definition?, limit)` ✅
   - `app_get_schema(schema_name)` ✅
   - `app_list_affiliates(query?, include_inactive?)` ✅
   - `app_list_source_systems(query?, category?, only_with_data?, limit)` ✅
   - `app_value_summary(affiliate?, priority?, status?, department?, search?, formula)` ✅
   - `app_value_source_rollup(affiliate?, priority?, department?, search?, only_missing?, limit)` ✅
   - `app_gaps_matrix(affiliate?, canonical?, only_gaps?, limit)` ✅
   - `genie_ask(question)` — fallback for everything else ✅
5. Genie space `bhe_catalog_explorer` curated, with column comments on silver tables. Created programmatically via `create_genie_space.py`; resource captured in `resources/bhe_genie_catalog_explorer.genie_space.yml` for DAB promotion. ✅
6. Citation rendering: tools return `{label, deeplink}` chips. `?uc=<id>` on `/value-readiness` auto-opens the use-case drawer (Route `validateSearch` + `useNavigate` to clear on close). ✅
7. ~~Basic chart rendering for Genie responses that include chart specs.~~ **Deferred to A1.5** — the Genie Conversation API does not return chart specs (per [docs](https://docs.databricks.com/en/genie/conversation-api): "It does not return rendered charts or visualizations"). To render charts we'd have to auto-generate Vega-Lite specs from row results. Replaced with: compact in-card result previews (rows-table for `genie_ask`, key-fact pills for `app_get_*`, mini bar charts for `app_value_source_rollup`).

**Acceptance criteria — verified end-to-end 2026-04-30**
- ✅ "Which use cases unlock the most value at PacifiCorp?" → `app_value_source_rollup(affiliate=PacifiCorp, only_missing=true, limit=3)` fires; rows ranked by `total_value`; assistant cites results.
- ✅ "Find one renewable energy use case and show me what data it needs." → `app_search_use_cases(query="renewable energy")` → `app_get_use_case(use_case_id="3b6bb55b")` chained automatically; result returns `100% data-ready, 7/7 sources present`. Citation deep-links into the use-case drawer.
- ⏭️ "Plot enrichment coverage by program." → Deferred to A1.5 (chart rendering — see above).
- ✅ Conversation reload on refresh works (history persisted in `chat_messages`; `parts` JSON rehydrates tool cards identically to the live stream).
- ✅ Chat user attribution: every row has `user_key` from `X-Forwarded-Email` / `X-Forwarded-Preferred-Username`.

**Out of scope for A1**
- Multi-turn refinement state (Genie gives us this for free via `genie_conversation_id`; for our own tools, each turn is independent — past turns rehydrated as user+assistant text only).
- Streaming tool-result rendering (results render after the tool completes; the in-flight bubble shows a `running…` spinner).
- Editing or deleting past messages.

### Phase A1.5 — Polish (deferred from A1)

Small follow-ups that didn't block "A1 done":

1. **Auto-generated Vega-Lite charts from Genie results.** Heuristic: 1 categorical + 1 numeric → bar; time + numeric → line; 2 numeric → scatter. Adds `vega-embed` (~400KB, dynamic-imported). Lower priority than A2 because the in-card result preview already shows the data.
2. **Multi-turn tool memory for `app_*` tools.** Today only Genie state is stitched across turns. If users start asking "give me more of those" we'll need to replay the most recent tool result back into context.
3. **Chat daily roll-up MV** in `bhe_gold.chat_daily_stats` (conversations/day, latency p50/p95, top-failing tools).
4. **Drawer deep-links for source + schema citations.** Today only `?uc=<id>` opens a drawer; `?canonical=` and `?schema=` should follow the same pattern.

### Phase A2 — Guided creation & edits (target: 3 weeks after A1)

#### Slice A2-1 — propose/confirm spine + status change — **STATUS: COMPLETE 2026-04-30**

Smallest end-to-end write that proves the propose/confirm pattern. Ships:

1. `bhe_silver.chat_confirm_tokens` Delta table — single-use, ~10min TTL, payload stored server-side.
2. `confirm.py` module: `issue_token()` + `_mark_consumed()` (atomic UPDATE...WHERE consumed_at IS NULL) + intent dispatcher (`_INTENT_EXECUTORS`). New tools register their executor here; the chat router itself doesn't grow.
3. `app_propose_status_change(use_case_id, status, status_notes?)` tool — looks up the current row, issues a token, returns `{kind:"proposal", before:{...}, after:{...}, confirm:{token, expires_at}}`. Does NOT write.
4. `POST /api/chat/confirm/{confirm_token}` endpoint — validates user_key, expiry, single-use; runs the executor in-process (re-uses existing `_normalize_status` + `_ensure_use_case_status_columns` helpers).
5. Inline `StatusChangeConfirmCard` in chat panel: amber-tinted card with Confirm/Cancel, before/after diff rows, expired-token guarding, error inline. Cancel never writes. On Confirm, invalidates `valueUseCases`/`valueSummary`/etc. queries so any open page reflects the change immediately.
6. ID-form tolerance in `app_get_use_case` and `app_propose_status_change` — accepts both seeded `xxxxxxxx` and chat-created `uc_xxxxxxxxxxxx` forms by trying both candidates in a single `WHERE id IN (...)`.
7. `SYSTEM_PROMPT_A1` rewritten to teach the model the propose-then-confirm flow ("never claim the change has happened until you see the confirm result come back"; "if more than one match, ASK — never guess at writes").

**Acceptance criteria — verified end-to-end 2026-04-30**
- ✅ "Mark use case 76810398 as in_progress with the note A2-1 smoke test" → `app_propose_status_change` fires, returns token; `POST /api/chat/confirm/<token>` returns `{ok:true, ...}`; row updated with `status=in_progress`, `status_notes='A2-1 smoke test'`, `status_updated_at` advanced.
- ✅ Replay protection: re-confirming the same token returns 409 "token already consumed".
- ✅ Diff card honesty: when the propose tool omits `status_notes`, the `after.status_notes` mirrors the existing value (no fake "→ null" rows in the card).
- ✅ Expired-token UI guard: `confirmProposal()` rejects expired tokens client-side before the network call; button switches to "Expired".

**Architecture decisions captured here for the rest of A2**
- **Server-side payload storage** (not FE re-send): the chat thread is the source of truth for what was proposed. Storing `payload` in `chat_confirm_tokens` at issue time means a malicious page-side mutation can't slip a different value past the user's eye.
- **In-process executors** (not HTTP recursion): `_INTENT_EXECUTORS` runs the same SQL the existing `PATCH /api/company/use-cases/{id}/status` runs. No second hop, audit + timing stay clean.
- **Path param renamed `confirm_token`** (not `token`): collides with `get_databricks_headers`'s `token: Annotated[str|None, Header(alias="X-Forwarded-Access-Token")] = None` parameter under FastAPI's name-based path-param heuristic. Captured as a comment on the route so future `/{...}` paths in this module avoid the same trap.
- **No new contract on existing write endpoints**: the `X-Catalog-Confirm` header pattern in the original plan was over-engineered for a 1-month-old app. Confirm endpoint owns the write. Existing UI keeps working unchanged.

#### Slice A2-2 — multi-field use-case edits — **STATUS: COMPLETE 2026-04-30**

`app_propose_use_case_update` for the conversational edit fields (name, description, department, category, business_value, value_rationale, priority, estimated_value_usd). Same propose/confirm spine as A2-1; what's new:

1. **Single-source SQL** — `router.update_use_case` extracted to `build_use_case_update_set_clause(patch_dict) -> str | None`. Both the existing UI PUT endpoint AND the new `_exec_update_use_case` chat executor call it. New writable fields land in one place.
2. **`_INTENT_EXECUTORS` signature evolved**: `(payload) → result` became `(payload, user_key) → result`. Threaded through the confirm endpoint. Audit columns on `use_cases` aren't there yet (still on the follow-up list), but the plumbing is. Today we surface `user_key` in the SQL warehouse query tags so chat writes are traceable in `system.query.history` even before audit columns land.
3. **Field-level validation in the propose tool**: priority is normalized + restricted to High/Medium/Low (case-insensitive); `estimated_value_usd >= 0` enforced via Pydantic. Errors come back on the same turn so the LLM can reformulate (or tell the user). Empty strings are dropped, NOT treated as "clear" (matches existing PUT semantics).
4. **No-op detection**: the propose tool diffs each proposed field against the current row; identical values are dropped from `effective_patch`. If the patch reduces to nothing, the tool returns `{no_change: true}` and never burns a token.
5. **Multi-field diff card** (`UseCaseUpdateConfirmCard`): N before/after rows in `_EDITABLE_FIELDS` order, money-formatted ("$45.0M") for `estimated_value_usd`, long strings truncated at 80 chars in the row (full text shows in the model's prose). Reuses `ConfirmCardShell` + `DiffRow` from A2-1.
6. **System prompt** updated: explicit instruction to call `app_get_use_case` BEFORE proposing edits (so the diff card has real before-values + the propose tool can detect no-ops); explicit "batch multi-field changes into one call".

**Acceptance criteria — verified end-to-end 2026-04-30**
- ✅ "Change category to Regulatory Risk and bump estimated value to 50M" → single `app_propose_use_case_update` call; diff card shows 2 rows (Category, Est. value); Confirm → SQL: `category='Regulatory Risk', estimated_value_usd=50000000.0, is_user_edited=true`.
- ✅ Replay protection: re-confirming consumed token → 409.
- ✅ No-op detection: re-proposing the same category the row already has → model calls `app_get_use_case` first, sees the value matches, doesn't propose at all. (When the model does call propose with all-no-op values, the tool returns `{no_change:true}` and the FE renders a tidy banner.)
- ✅ Invalid-priority rejection: model rejects "Critical" off the system prompt before even calling the tool. (Defense in depth: if it ever sneaks through, `_normalize_priority` raises a `ToolResult(ok=false)` with the allowed set in the message.)
- ✅ Existing UI write path unbroken: `PUT /api/company/use-cases/{id}` returns 200 after the SET-clause refactor.
- ✅ Round-trip restore via chat: revert proposal → confirm → row matches original snapshot.

#### Slice A2-3 — affiliate + canonical mapping deltas — **STATUS: COMPLETE 2026-04-30**

`app_propose_affiliate_mapping` and `app_propose_canonical_mapping` — list-delta tools (not scalar before/after) for the two N-to-many gold tables backing the use-case detail drawer.

**Design decisions captured in code**
- **Delta semantics, not whole-list replace**. The tools accept `add: [...]` and `remove: [...]`. We deliberately rejected "set affiliates to [...]" because a model omission would silently delete unintended rows. Deltas keep blast radius minimal — the executor only touches what was explicitly listed; everything else is untouched.
- **Two intents, one FE card**. `INTENT_UPDATE_USE_CASE_AFFILIATES` and `INTENT_UPDATE_USE_CASE_CANONICALS` are separate (different tables, different tag fields: applicability vs necessity), but both tools return the same JSON shape (`{kind:"proposal", resource, additions, removals, notice?, skipped_noop}`) so the FE renders both through one `ListDiffConfirmCard`.
- **Reseed-warning surfaced inline**. The existing `delete_use_case_affiliate` / `delete_use_case_source_requirement` endpoints had a docstring caveat: "the LLM job MAY recreate it on next run if the use case description still implies the row". We promote this from docstring → tool result `notice` field → amber banner inside the confirm card → instruction in the system prompt telling the model to mention it to the user. The model now consistently warns "you may also want to edit the description to make this stick".
- **Server-side validation, three layers**:
  1. Pydantic args restrict `applicability` ∈ {primary, secondary} and `necessity` ∈ {must_have, nice_to_have}.
  2. Tool body checks `add ∩ remove = ∅`, drops empty `add`+`remove`, validates names against `bhe_gold.affiliates` / `source_system_canonical` so a typo can't create a junk row.
  3. Tool diffs each `add` against the current row; identical (applicability, rationale) or (necessity, excerpt) tuples land in `skipped_noop` and never burn a token. If the patch reduces to nothing, `{no_change:true}` is returned.
- **Per-item error tolerance in the executor**. Each MERGE / DELETE in `_exec_update_use_case_affiliates` and `_exec_update_use_case_canonicals` runs in its own try/except; failures land in a `result.errors[]` array. The 200 still returns; the FE can show "added 2, removed 1, 1 failed: <details>" instead of just "500".
- **Single-source SQL** (same pattern as A2-2). `merge_use_case_affiliate`, `delete_use_case_affiliate_row`, `merge_use_case_source_requirement`, `delete_use_case_source_requirement_row` extracted from their respective UI POST/DELETE endpoints. The UI handlers and chat executors call the same primitives.

**FE additions**
- `ListDiffConfirmCard` (generic, takes `resourceLabel: "affiliates" | "canonicals"`).
- `ListDiffRow` with green `+` / rose `−` icons, applicability/necessity tag, `update` badge when an add upserts an already-mapped row (e.g. promoting secondary → primary), inline rationale/excerpt under the row.
- Inline reseed-warning notice with `AlertCircle` icon, only rendered when there are removals.
- `queryClient.invalidateQueries` extended with `valueSankey` + `valueSourceDetail` (mapping changes ripple into both).

**Acceptance criteria — verified end-to-end 2026-04-30**
- ✅ "Add MidAmerican Energy as secondary, remove NV Energy" → single `app_propose_affiliate_mapping` with both deltas; card shows +1/−1 with reseed notice; Confirm → both writes land; `mapped_by=manual` on the new row.
- ✅ "Add SAP as nice_to_have with excerpt …" → `app_propose_canonical_mapping`; card shows +1, no notice (no removals); Confirm → row inserted with `mapped_by=manual` + `data_need_excerpt` preserved.
- ✅ "Remove SAP" → card shows −1 with reseed notice; Confirm → row deleted.
- ✅ Unknown affiliate "ACME Power" → model called `app_list_affiliates(query="ACME")` first, got 0 rows, never called propose, told the user "ACME isn't a BHE affiliate" + offered the actual affiliate list.
- ✅ Round-trip restore: revert affiliate change via the same chat path → row returns to NV Energy secondary (with `mapped_by=manual` now, which is correct — it WAS just touched manually).
- ✅ Existing UI POST/DELETE endpoints regression test: refactor untouched their semantics (helper extraction only).

#### Slice A2-4 — `app_propose_use_case` (full create) — **STATUS: COMPLETE 2026-04-30**

Full-create tool. Composes a brand-new use case (parent in `bhe_silver.use_cases`) plus optional initial affiliate and canonical mappings (children in `bhe_gold.use_case_affiliates` and `bhe_gold.use_case_source_requirements`) — all gated by ONE confirmation card.

**Design decisions captured in code**
- **Best-effort atomicity, not transactional rollback.** The executor (`_exec_create_use_case` in `confirm.py`) inserts the parent first; if it raises, no orphan child rows can exist. Then it loops the affiliate adds and the canonical adds, each in its own try/except. Successes go into `affiliates_added` / `canonicals_added`; failures land in a `result.errors[]` array. We deliberately rejected hard rollback because (a) cross-table transactions on Delta are not first-class, and (b) a partial-success state is recoverable — the user can re-run `app_propose_affiliate_mapping` / `app_propose_canonical_mapping` on the new id to fill gaps. Rolling back the parent on a child failure would force the user to start over, which is strictly worse.
- **Name uniqueness enforced at propose time AND at confirm time.** `find_use_case_by_name` (case-insensitive, trimmed lookup) is called by the propose tool before issuing a token (so the model gets a same-turn collision error message) AND by the executor before inserting (so two tabs racing a confirm can't both succeed). Stricter than the UI POST endpoint by design — the UI assumes the analyst owns the name; the LLM might retry the same create on every regenerate.
- **Required-vs-optional split.** Only `use_case_name` is strictly required. Description, department, category, business_value, value_rationale, priority, estimated_value_usd, status, status_notes, affiliates, and canonicals are all optional. Affiliates and canonicals are STRONGLY preferred (a use case with no mappings is an orphan), but enforced in the system prompt + a soft amber warning on the confirm card, not as a hard reject. The model is instructed to ask clarifying questions before proposing if it's missing key fields, never to ship placeholder values.
- **Default status `in_progress`.** A chat-created row almost always represents work the user is actively scoping; defaulting to `not_started` would force a follow-up edit on every create.
- **Reuse of A2-3 helpers.** The executor calls `insert_use_case_row` (extracted from `create_use_case` UI handler), `merge_use_case_affiliate`, and `merge_use_case_source_requirement` directly. No write SQL is duplicated between A2-4 and the existing UI POST/DELETE endpoints.
- **`AffiliateAdd` / `CanonicalAdd` Pydantic models reused** verbatim from the A2-3 mapping tools — same field shape, same validation semantics. The propose tool adds an extra `aff_seen` / `canon_seen` dedup pass to reject lists with duplicate names (which would not be caught by the executor's MERGE; you'd just silently get one row).
- **Three-layer validation** (matching A2-3): Pydantic enums + min_length on name + ge=0 on estimated_value_usd; tool body validates priority / status normalization, dim-table existence checks for every affiliate / canonical name, dedup within each list, and name uniqueness; executor re-checks name uniqueness as the last defense.

**FE additions**
- `CreateUseCaseConfirmCard` — combines a compact key:value list of the proposed parent fields (no "before" column because there's nothing to diff against) with two grouped sections rendering affiliates and canonicals via the existing `ListDiffRow` (kind=add). Subline shows blast radius: `+1 use case · +N affiliates · +M canonicals`.
- `CreateFieldRow` — per-field view; emerald-tinted background to signal "new value", same height as `DiffRow` for visual consistency across cards.
- Soft amber warning banner inside the card when both affiliates and canonicals are empty: "The use case will be created but won't show up on any affiliate's coverage view or readiness score until you add them."
- `TOOL_LABELS` extended (`app_propose_use_case` → "Propose new use case").

**Tool order rationale**
`app_propose_use_case` sits LAST in the propose-tool group (just before `genie_ask`). The convention is "specific edits first, whole-record creates after" so that when the user's intent is ambiguous between "edit the existing 'Smart Meter Analytics' UC" and "create a new one", the model prefers the cheaper / less-disruptive edit path.

**Acceptance criteria — verified end-to-end 2026-04-30 via `scratch_a24_smoke.py`**
- ✅ Chat: "Create a new use case named 'A2-4 Smoke Drone Inspection ...' for PacifiCorp (primary), needs PI Historian (must_have), department Transmission Operations, value $1.5M" → model called `app_search_use_cases` (collision precheck), `app_list_affiliates`, `app_list_source_systems` to validate names, then `app_propose_use_case` with a fully-formed payload. Token issued in one turn.
- ✅ Confirm via `POST /api/chat/confirm/{token}` → `200 {ok:true, intent:createUseCase, result:{use_case_id:uc_4d8af545463d, affiliates_added:["PacifiCorp"], canonicals_added:["PI Historian"], errors:[]}}`. All three tables written.
- ✅ Round-trip: subsequent `app_get_use_case` returned the new row with 1 applicable affiliate and 1 canonical (which read tool flagged as `present` because PI Historian IS in the lake — so the new use case immediately got a non-zero readiness score).
- ✅ Name collision: re-proposing the same name → propose tool returned `{ok:false, error:"name_collision", existing_id:"uc_..."}` BEFORE issuing a token. No write attempted.
- ✅ Cleanup: existing UI `DELETE /api/company/use-cases/{id}` cascaded the gold child rows (no orphans left behind). The new `insert_use_case_row` helper had no observable difference in row content vs the prior inline INSERT.
- ✅ Soft warning rendered on the FE confirm card when affiliates+canonicals are both empty (verified by inspection of card output for a propose with bare-name only).

#### Slice A2-5 — `app_research_use_case` — **STATUS: COMPLETE 2026-04-30**

A pure-read tool that compresses the "draft a new use case from a topic" workflow into ONE round-trip. Returns a STRUCTURED brief whose top-level keys mirror `ProposeUseCaseArgs` so the model can copy fields directly into a downstream `app_propose_use_case` call.

**Why this exists**
Before A2-5, a "create a use case for X" prompt typically produced:

  1. `app_search_use_cases(query=X)` — collision check + style references
  2. `app_list_affiliates` — name validation
  3. `app_list_source_systems` — name validation
  4. (sometimes) `app_get_use_case` × N — pull mappings off similar UCs
  5. `app_propose_use_case` with fields the model mentally aggregated from 1–4

That works but is slow (4–6 tool calls), brittle (the model can miss obvious adjacent UCs), and doesn't surface aggregate signal — e.g. "this kind of use case typically costs $30M and uses Maximo + Weather Service" — which is the most useful context for a draft. A2-5 moves all of that into one server-side chain that returns aggregations the model couldn't compute itself in a single turn.

**Design decisions captured in code**
- **SQL, not Genie.** Search space is hundreds of UCs; substring + keyword scoring is plenty. A regex-based tokenizer drops stopwords + tokens shorter than 4 chars, then per-token CASE WHEN scoring runs in one query: name-hit = 3, description-hit = 1, optional `+2` bonus if the UC is mapped to the user's `target_affiliate`. Tied scores break by `estimated_value_usd DESC` so high-value matches win. A 30-token topic still produces a manageable WHERE clause (capped at 8 tokens).
- **Suggestions, not prescriptions.** The brief surfaces top-N affiliates / canonicals with frequency counts (e.g. `count: 6, primary_count: 4`). The model still has to call `app_propose_use_case`, the user still has to confirm. This tool only feeds the model better context — never auto-creates.
- **Aggregations are over the FULL match set, not the visible top-N.** `limit_similar` only trims what the model SEES; the suggestions are still computed across all matched UCs (capped at 10) so a small visible list doesn't bias the value range or top affiliates.
- **Value stats ignore zeros.** A use case with `estimated_value_usd=0` is "not yet estimated" rather than "literally worthless"; including it would skew the median and mislead the model. Only positive values feed the {min, median, max, count} block.
- **`applicability_hint` / `necessity_hint` are derived, not random.** An affiliate that's `primary` on >= 50% of matched UCs gets `primary` hint; same for canonicals + `must_have`. If the user named a `target_affiliate`, that affiliate ALWAYS gets `primary` hint — overriding frequency — because user intent beats statistical aggregate.
- **Three structured warning paths**:
  1. Exact-name match → "an existing UC has this exact name; use update instead" (model routes the user to `app_propose_use_case_update` instead of trying to create a duplicate).
  2. Empty topic / all-stopwords → `{ok:false, error:"no_tokens"}` with concrete advice ("try 'drone imagery for vegetation management' instead of 'the and for from'").
  3. `target_affiliate` named but absent from matches → "this would be the first use case in this topic area for that affiliate". Also force-inserts the named affiliate into the suggestions list (so the model doesn't drop it).
- **Single-source pattern reused.** Tokenizer + score SQL builder + suggestion aggregator are all module-private; nothing leaks into the chat router or system prompt. The system prompt only knows "call this first; copy fields from the brief".

**FE additions**
- `ResearchBriefPreview` (uses `PreviewShell`, NOT `ConfirmCardShell` — this is a read, nothing to confirm). Sections:
  - Top similar UCs (compact name + value rows, deeplinked).
  - Three-pill row for Department / Category / Priority defaults.
  - Value range card (min — max + median + n).
  - Suggested affiliates / source systems lists with `SuggestionRow` (name + tag pill colored emerald for primary/must_have, neutral otherwise + frequency `N/M`).
  - Inline warnings as amber rows at the top.
  - Distinct empty state: "No similar use cases found — proposal will be drafted from scratch — confirm fields carefully."
- `SuggestionRow` is a new general-purpose row primitive (could be reused for future suggestion-style tools).
- `TOOL_LABELS` extended (`app_research_use_case` → "Research similar use cases").

**Tool order rationale**
`app_research_use_case` sits after the basic catalog reads (`app_search_use_cases`, `app_get_use_case`, etc.) but BEFORE any propose tool. The system prompt elevates it to the FIRST step of any create flow, but its physical position in the tool list keeps the convention "reads above writes" intact.

**System prompt changes**
- New explicit instruction in the create-flow paragraph: "ALWAYS call `app_research_use_case` FIRST with the user's topic (and target_affiliate if they named one). The brief tells you (a) whether a near-twin already exists; (b) the department/category/value conventions for this kind of work; (c) which affiliates and canonicals similar UCs use, ranked by frequency."
- Added research-tool entry to the tool-selection list above ("Research: `app_research_use_case` returns a structured brief…").

**Acceptance criteria — verified end-to-end 2026-04-30 via `scratch_a25_smoke.py`**
- ✅ Single research call: model called `app_research_use_case` exactly once with `topic="AI-powered drone imagery analytics for wildfire risk mitigation across transmission corridors"`. No prior `app_search_use_cases` / `app_list_affiliates` / `app_list_source_systems` calls (the brief covers all three needs in one shot).
- ✅ Brief shape: returned `kind:"research_brief"`, 8 tokens used, 10 matched UCs, suggestions for departments / categories / priority / affiliates / canonicals / value all populated.
- ✅ Aggregations sensible: top affiliates `[Multi-Affiliate, BHE Transmission, PacifiCorp]`; top canonicals `[Internal, Weather Service, Oracle Financials, Unmapped, Endur, Maximo]`; value range `$15M – $78M (median $34M, n=10)`; top category `Risk Management`.
- ✅ Model used the brief's outputs: subsequent propose call set `estimated_value_usd=34000000` (the median), `priority=High` (top suggestion), `category=Risk Management` (top suggestion), `department=Transmission & Grid Infrastructure` (a top-3 suggestion), affiliates `[PacifiCorp]` (suggested, primary), canonicals `[Weather Service, Maximo]` (both from suggestions). Most striking: the `value_rationale` field cited the brief explicitly: *"Benchmarked against peer wildfire-risk use cases in the portfolio (median ~$34M, range $15M–$78M)."* That's grounded reasoning the model couldn't have produced from raw search results alone.
- ✅ Confirm + cleanup landed: 200 OK, 1 affiliate + 2 canonicals written, then deleted via UI DELETE.
- ✅ Edge case: topic `"the and for from"` → `{ok:false, error:"no_tokens"}` with a concrete suggestion to try a more descriptive phrase. Tool didn't crash, didn't return empty results — the failure mode is informative.
- ✅ Tool count check: invocation summary in the chat panel shows `Research similar use cases` label correctly mapped via `TOOL_LABELS`.

**Defensive coercion gotcha (caught + fixed during smoke)**
The first smoke run crashed with `'>' not supported between instances of 'str' and 'int'` from the value-stats filter. Root cause: SEA can return DECIMAL columns as Python strings (`'45000000'`), and my filter `if (m.get("estimated_value_usd") or 0) > 0` was comparing the raw value, not the float-cast. Pattern to remember: when filtering on a numeric column from SEA, always coerce in BOTH the projection AND the filter expression. Added `_as_float` helper with try/except for ValueError to absorb any future weird shapes (e.g. `'NaN'`, empty string).

**Carried over to A3**
- Adjacent-by-mapping search: today the brief is keyword-driven only. A future enhancement: also surface UCs that share required canonicals with the topic's keyword matches (e.g. a UC about "vegetation management" that uses Maximo + GIS would surface even if the topic doesn't include "vegetation").
- `app_research_schema` for the schema-curation flow.
- Pre-rendered FE preview link on the brief: "Use this brief to create →" button that pre-fills a propose card. Today the user has to ask the model to chain; a one-click button would be smoother.

**Open Phase A2 deliverables (carried over from original plan)**
- Inline edit on the confirm card (today the card is read-only; user must Cancel + ask the model to re-propose to change a field).
- Audit columns on `bhe_silver.use_cases` (`created_by`, `updated_by`, `updated_at`). The plumbing is in place — `_INTENT_EXECUTORS` already receives `user_key` — we just need the DDL + SET-clause to include it.
- Query-tag enrichment with `source=chat:<conversation_id>` so chat-driven writes are filterable in `system.access.audit`. (Today we tag `user_key` and `submodule`; conversation_id is the missing piece.)

**Acceptance criteria**
- The user prompt "create a use case 'Drone Image Fault Detection' for PacifiCorp transmission inspection" produces:
  1. A clarifying turn asking about department + affiliates if not stated.
  2. A research turn that finds similar existing UCs and proposes a department, value range, suggested canonicals (PI, GIS, etc.).
  3. A review card with all fields editable inline.
  4. On Confirm, a row in `bhe_silver.use_cases` with `is_user_edited=true`, `created_by=<the user>`, the affiliate links in `bhe_gold.use_case_affiliates`, and the source links in `bhe_gold.use_case_source_requirements`.
- The user prompt "set the status of UC abc123 to delivered with note 'shipped Q4 2026'" produces a single confirm card and writes to the same endpoint the UI uses.
- Cancelling never writes.
- An attempt to call a write endpoint without a valid `X-Catalog-Confirm` token from a chat session is rejected.

**Decisions still open for A2**
- Do we let the bot edit `definition`/`business_name` on schemas? (Probably yes, with confirmation, but raises governance questions for AI-edited fields.)
- Do we allow batch operations ("create UCs for the top 5 gaps")? Recommend: no in A2; revisit in A3 with explicit batch-confirmation UX.

### Phase A3 — Schema editing, async ops, batch flows (in progress)

**Hard guardrail (locked in by product call):** **NO DELETES VIA CHAT, EVER.** There is no `app_propose_delete_*` tool and there will not be one. The chat refuses delete asks and points users to the UI. This is enforced both by the absence of the tool AND by an explicit rule in the system prompt; the smoke test asserts the model doesn't reach for any write tool when asked to delete.

**Slicing**

| Slice | Scope | Status |
|---|---|---|
| A3-1 | Schema edit (`app_propose_schema_update`) + `app_research_schema` | **COMPLETE** |
| A3-2 | Async enrichment (`app_enrich_schemas`) with progress streaming | Pending |
| A3-3 | Batch flows (`app_propose_use_cases_for_gaps`) — uses A3-2's progress UX | Pending |
| A3-4 | Audit columns + chat-vs-UI provenance | Pending |

#### Slice A3-1 — Schema editing + research (COMPLETE)

**Deliverables**
- **Backend write tool**: `app_propose_schema_update(schema_name, catalog_filter?, ai_definition?, business_friendly_name?, suggested_department?, suggested_domain?, data_sensitivity?)`. Multi-field patch on a logical schema. By default updates ALL physical `silver_schemas` rows for the schema_name (one per dev/qa/prod catalog) so the definition stays consistent across environments; `catalog_filter` narrows to specific catalogs. Returns a confirmation token + per-field, per-catalog before-values for the diff card.
- **Backend research tool**: `app_research_schema(schema_name, peer_limit?)`. Returns the schema's current values + a sample of its tables + peer schemas (scored by token-name match + same-program bonus + same-domain bonus, capped at 12) + aggregated suggestions for `suggested_domain` / `suggested_department` / `data_sensitivity` ranked by frequency + 1-3 sample peer definitions (preferring `is_user_edited=true` rows for higher-quality style references).
- **Backend executor**: `INTENT_UPDATE_SCHEMA` + `_exec_update_schema` in `confirm.py`, dispatching to the new shared `update_silver_schema_rows` helper in `router.py`. Same helper backs the existing UI PUT endpoint, so single-source-of-truth.
- **Backend refactor**: Extracted `SCHEMA_EDITABLE_FIELDS`, `build_schema_update_set_clause`, `update_silver_schema_rows`, `find_silver_schema_rows` from the existing `update_schema` endpoint. The chat propose tool asserts its local field-label tuple matches the router's authoritative tuple at handler-entry time, so the two can't drift silently.
- **System prompt**: Added the new tools, the "ALWAYS call `app_research_schema` first" workflow rule, and the hard NO-DELETES rule.
- **FE cards**:
  - `SchemaResearchBriefPreview` — current values + sample tables + peer schemas (with curated badges) + ranked suggestions + sample definitions.
  - `SchemaUpdateConfirmCard` — multi-field diff with per-catalog before-values when divergent (each catalog gets its own line, struck through), single before-value when all catalogs agree. Divergent fields get an explicit "divergent" pill.
- **TanStack invalidation**: confirm-card success now also busts `schemas`, `silverSchemas`, `schemaInventory`, `catalogStats` so the catalog browser & schema explorer reflect the change without a manual refresh.

**Design decisions**
- **Logical not physical by default.** Users say "update the maximo schema", not "update prod_catalog.maximo". The chat takes `schema_name` only and writes to all matching `silver_schemas` rows. Per-catalog narrowing is opt-in via `catalog_filter`.
- **Surface divergence honestly.** When dev / qa / prod hold different values for a field today, the propose tool flags it AND returns a top-level warning. The system prompt requires the model to mention this to the user before they confirm ("this will collapse dev's X and prod's Y to your new value Z"). Without this, a chat write could silently overwrite environment-specific differences a curator put in deliberately.
- **No `propose_table_update` yet.** The matching read tool (`app_get_table`) doesn't exist; without it the model can't validate `(catalog, schema, table)` triples before proposing. Defer to A3-1b once a table-read tool is in place.
- **Sample peer definitions prefer user-edited rows.** AI first-pass definitions are noisy; hand-curated text is much higher quality as style reference. The research tool sorts user-edited peers first when picking the 3 sample definitions surfaced to the model.
- **Token caps**: `_MAX_PEER_SCHEMAS=12`, `_MAX_SAMPLE_TABLES=8`. Schema clusters are dense (lots of `crm_*`, `maximo_*` patterns); top-12 is plenty and keeps the brief small enough that the model still pays attention to it.

**Acceptance criteria — all met**
- "Research the outage schema and improve its definition" produces: research → propose → confirm card with the proposed text, NO writes before Confirm.
- The diff card shows per-catalog before-values; when one field's current value diverges across catalogs, the divergent pill appears and the warnings array surfaces it.
- Confirming reports `rows_affected > 0`; re-reading via `app_research_schema` shows the new value.
- Proposing a value identical to current returns `no_change` (no token issued, no card shown).
- "Delete the X schema" is refused — the model never reaches for any propose tool. Verified by `scratch_a31_smoke.py` Phase 5.

#### Slice A3-2 — Async enrichment (planned)

`app_enrich_schemas(filter, dry_run?)` kicks off the existing AI enrichment job, returns a job ID, and the chat streams progress (via the same SSE channel that already carries tool events — new event type `job_progress`). Confirm-token guarded because it costs LLM credits. Reuses the JobsApi wrapper that the Edit Center page already uses. Includes a `dry_run` mode that just returns the schema list that WOULD be enriched.

#### Slice A3-3 — Batch flows (planned)

Builds on A3-2's progress UX. `app_propose_use_cases_for_gaps(top_n=5)` uses the existing gaps matrix tool to find unmet (affiliate × canonical) needs, then calls `app_research_use_case` per gap and produces a batch confirmation card with per-row confirm / skip toggles. One token per row (avoids "30 use cases got created because confirm was clicked once").

#### Slice A3-4 — Provenance (planned)

Audit columns (`created_by_chat`, `confirmed_by_email`, `confirmed_at`) on `use_cases` and `silver_schemas`, written by all chat executors so we can answer "what did the chatbot create / edit?". Required before we ship to non-admin users.

## 6. Data model

### New tables (Phase A1)

```sql
CREATE TABLE bhe_silver.chat_conversations (
  conversation_id   STRING  COMMENT 'UUID',
  user_email        STRING  COMMENT 'X-Forwarded-Email at creation time',
  title             STRING  COMMENT 'Auto-generated from first user message',
  genie_conversation_id STRING COMMENT 'Set on first Genie tool call',
  created_at        TIMESTAMP,
  updated_at        TIMESTAMP,
  message_count     INT,
  is_archived       BOOLEAN
) USING DELTA
COMMENT 'One row per chat thread.';

CREATE TABLE bhe_silver.chat_messages (
  message_id        STRING  COMMENT 'UUID',
  conversation_id   STRING  COMMENT 'FK -> chat_conversations',
  user_email        STRING  COMMENT 'Identity at the time of the message',
  role              STRING  COMMENT 'user | assistant | tool',
  content           STRING  COMMENT 'Markdown text (assistant/user) or JSON (tool)',
  tool_name         STRING  COMMENT 'Set when role=tool',
  tool_call_id      STRING  COMMENT 'Links tool result to its assistant tool_calls entry',
  tool_args_json    STRING  COMMENT 'JSON of arguments (assistant) or input (tool)',
  tool_result_json  STRING  COMMENT 'JSON of tool result',
  citations_json    STRING  COMMENT 'JSON array of {label, deeplink}',
  chart_spec_json   STRING  COMMENT 'Vega-Lite spec if present',
  latency_ms        INT,
  token_input       INT,
  token_output      INT,
  created_at        TIMESTAMP
) USING DELTA
COMMENT 'Append-only message log. One row per turn part (user, assistant, each tool call/result).';
```

### Schema additions (Phase A2)

```sql
ALTER TABLE bhe_silver.use_cases ADD COLUMNS (
  created_by  STRING COMMENT 'X-Forwarded-Email of creator (chat or UI)',
  updated_by  STRING COMMENT 'X-Forwarded-Email of last editor',
  updated_at  TIMESTAMP COMMENT 'Last edit timestamp'
);
```

(Same pattern for any other table edited via chat. Audit-column work was already on the docket from a prior session.)

### Confirmation token table

```sql
CREATE TABLE bhe_silver.chat_confirm_tokens (
  token            STRING  COMMENT 'Random opaque token',
  conversation_id  STRING,
  user_email       STRING,
  intent           STRING  COMMENT 'createUseCase | updateUseCase | ...',
  payload_hash     STRING  COMMENT 'SHA256 of the proposed payload',
  expires_at       TIMESTAMP,
  consumed_at      TIMESTAMP COMMENT 'NULL until used; single-use'
) USING DELTA;
```

## 7. APIs

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/chat/conversations` | GET | List signed-in user's conversations |
| `/api/chat/conversations` | POST | Create new conversation |
| `/api/chat/conversations/{id}` | GET | Fetch conversation + messages |
| `/api/chat/conversations/{id}` | DELETE | Archive |
| `/api/chat/messages` | POST (SSE) | Send user message; streams assistant response + tool calls |
| `/api/chat/tools` | GET | (debug) list registered tools and schemas |

The existing write endpoints (`POST /api/company/use-cases`, etc.) gain an **optional** `X-Catalog-Confirm` token header. Phase A1 doesn't change them. Phase A2 adds the header check **only when the token is present** (so the regular UI is unchanged).

## 8. UI

- **Entry point**: small floating button bottom-left of every page (matches the existing icon style; uses Sparkles icon).
- **Panel**: slides in from the left, ~480px wide, full height. Reuses the drawer pattern from `UseCaseDetailDrawer` so it feels native.
- **Persistence**: panel open/closed state in localStorage.
- **Components to build** (all in `ui/components/chat/`):
  - `ChatLauncher` — the FAB
  - `ChatPanel` — the drawer shell + history sidebar
  - `MessageList` + `MessageBubble` (markdown-rendered)
  - `ToolCallCard` — collapsed by default, expandable to show args/result
  - `ConfirmationCard` (Phase A2) — diff view + Confirm/Cancel
  - `ChartCard` — Vega-Lite renderer
  - `Composer` — input + send button + (later) attachment slot
- **Routing**: chat is global, not a route. State lives in a TanStack Query + Zustand combo (history queries via TanStack, transient panel state via Zustand).

## 9. Auth & attribution

- Identity source: same as today — `X-Forwarded-Email` + `X-Forwarded-Preferred-Username` headers (locally falls back to SCIM `/Me` via OBO token, already wired in `me()` endpoint).
- Every chat message and every chat-driven write records the user email.
- All chat traffic gets `module=chat` + `submodule=<intent>` query tags so it's filterable in `system.query.history`.
- **Genie calls** use the user's OBO token where possible so Genie's SQL inherits the user's permissions. Fallback: app service principal with read-only on `bhe_silver` and `bhe_gold` (acceptable per product call — read-only public access).

## 10. Observability

- Per-message: latency, input/output tokens, tool-call count, error code if any (all on `chat_messages`).
- Daily roll-up materialized view in `bhe_gold.chat_daily_stats` (Phase A1.5): conversations/day, avg latency, tool-call distribution, top failing tools.
- Eval (later, after enough data): MLflow GenAI evaluation with a small golden set of prompts → expected tool / expected entity referenced.

## 11. Risks & open questions

| Risk | Mitigation |
|---|---|
| Tool sprawl degrades model accuracy | Cap A1 at ~10 tools. Per-phase tool budget. |
| LLM picks `genie_ask` for things our typed tools can answer | Tight tool docstrings + system prompt that explicitly lists when to use Genie ("only when no app tool fits"). Add eval set in A1.5. |
| Bot creates garbage UCs from prompt injection in user data | Two-step confirm + the `is_user_edited=true` flag protects against reseed wipes. |
| Genie space drift (someone changes table comments) | Comments live in `bootstrap_tables.py` / `create_gold_tables.py`. Reseeds re-apply them. CI check (later) that diffs current vs. expected comments. |
| Foundation Model API rate limits | Cache Genie responses on hash(question + space_version). Surface a friendly error on 429. |
| Cost runaway | Daily token budget per user, soft warning at 80%, hard cap at 100%. Defer to A1.5. |
| User asks "what changed yesterday in PacifiCorp?" — not a question Genie can easily answer over time-travel | Out of scope for V1. Document in known-limitations. |

**Resolved decisions (2026-04-16):**
1. **LLM:** `databricks-claude-opus-4-7` — confirmed available in workspace, tools-enabled, AI Gateway, hosted by Databricks.
2. **Chat history:** persist across sessions in `bhe_silver.chat_conversations` / `chat_messages`.
3. **Visibility:** each user sees only their own chats. No admin view in V1.
4. **PII:** owner names/emails are acceptable in chat (already in our catalog metadata). No automated redaction layer in V1; revisit if other PII classes appear.

## 12. Genie space prerequisite — **COMPLETED 2026-04-16**

Before Phase A1 wiring started:

1. ✅ Applied column comments to `bhe_silver.silver_tables`, `bhe_silver.use_cases`, `bhe_silver.departments` (the only ones missing).
2. ✅ Backported the same comments into `bootstrap_tables.py` so reseeds preserve them.
3. ✅ Created Genie space `BHE Catalog Explorer` — **space_id `01f13f2d12271caeb5f26d3762ea9d75`** — with 14 curated tables via `POST /api/2.0/genie/spaces`.
4. ✅ Seeded 7 example questions covering enrichment coverage, use-case value, source requirements, cross-env consistency, etc.

### Single source of truth

The space definition (tables, text instructions, example questions) lives in **`resources/genie_spaces/bhe_catalog_explorer.json`** and is consumed by two things:

- `src/app/create_genie_space.py` — Python bootstrap that POSTs/PATCHes the space via the Conversation API. Run with `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID` set, and optionally `GENIE_SPACE_ID` to update instead of create.
- `resources/bhe_genie_catalog_explorer.genie_space.yml` — DAB resource file for BHE handoff (see §12a).

The script enforces three API quirks that are *not* documented in the public REST reference:
1. `data_sources.tables[]` must be sorted by `identifier`.
2. Every `text_instructions[].id` and `example_question_sqls[].id` must be a lowercase 32-hex UUID (no hyphens).
3. Both instruction lists must be sorted by `id`.

Editing the JSON and re-running the script is the one workflow.

### 12a. DAB promotion for BHE handoff

Genie Space support was added to Databricks Asset Bundles by [databricks/cli#4191](https://github.com/databricks/cli/pull/4191) in early 2026. The resource file `resources/bhe_genie_catalog_explorer.genie_space.yml` is ready to deploy, **with two caveats to document in the handoff:**

| Constraint | Impact |
|---|---|
| Requires Databricks CLI **v0.287.0+** (our local is v0.280.0) | BHE must upgrade their CI CLI before the first `bundle deploy` that includes this resource. |
| Works **only with the direct-deploy engine**, not Terraform (no `databricks_genie_space` TF resource exists yet) | Deploy must be invoked as `DATABRICKS_BUNDLE_ENGINE=direct databricks bundle deploy -t <target>`. If BHE's existing pipeline uses the default Terraform engine, either exclude this resource and continue using the Python bootstrap, or migrate the whole bundle to direct mode. |

The resource references the same canonical JSON (`${file('genie_spaces/bhe_catalog_explorer.json')}`) so the Python bootstrap and the DAB deploy produce byte-identical spaces. Permissions are set to `account users: CAN_RUN` by default — override per-target as needed.

**Recommended handoff sequence:**
1. BHE upgrades their CLI to v0.287.0+ in CI.
2. BHE migrates the rest of this bundle to direct-deploy (one-time).
3. BHE runs `databricks bundle deploy` — the Genie space becomes fully bundle-managed; the Python bootstrap script is retired.

Until step 1 is done, the Python script remains the supported path.

## 13. Sequencing summary

| Week | Milestone |
|---|---|
| 0 (this week) | Genie space prerequisite (comments + space + examples) |
| 1 | A1: chat panel UI scaffolding + `POST /chat/messages` skeleton |
| 2 | A1: tool dispatcher + 10 read tools + Genie integration + chart rendering |
| 3 | A1 polish + telemetry; ship to internal users |
| 4–5 | A2: propose tools + confirmation card + token gating |
| 6 | A2: full create-use-case flow end-to-end + audit columns |
| 7+ | A3 as scoped above |

## 14. Out of scope (explicit)

- Mobile UI for chat.
- Voice / dictation.
- Multi-user / shared conversations.
- Replacing the existing forms — chat is additive.
- Exposing the catalog over MCP (Track B, shelved).
- Auto-running batch creates without per-row user confirmation.

---

## Appendix A — System prompt sketch (A1)

> You are the BHE Data Catalog assistant. You help BHE users understand their data assets, use cases, affiliates, source systems, and schemas. You ALWAYS ground answers in tool results — never invent IDs, names, or numbers.
>
> Tool selection rules:
> - Use `app_*` tools when the user's question is about specific entities the app knows about.
> - Use `genie_ask` ONLY when the question requires arbitrary aggregation or filtering that no `app_*` tool covers.
> - Cite every entity by its UI deep-link so the user can open it directly.
> - When asked to create or change something, in this phase respond that "creation/edits are coming in the next release" — do not call any write tools.
>
> Tone: concise, factual, no marketing fluff. Use short bullets, never long paragraphs.

(A2 prompt expands the create/edit clause; A3 expands long-running ops.)
