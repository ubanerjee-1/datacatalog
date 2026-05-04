/**
 * Chatbot (Track A) — floating chat panel with a bottom-RIGHT FAB trigger.
 *
 * Phase A1 — Slice 2 (PR #2): tool-calling enabled. Two tools live now:
 * `app_search_use_cases` (typed catalog read) and `genie_ask` (free-form
 * data question against the curated Genie space). Tool calls render
 * inline as collapsible cards before the assistant text bubble.
 *
 * UX shape:
 *  - The FAB lives in the bottom-RIGHT of the viewport on every route.
 *  - The panel is a Sheet sliding in from the RIGHT.
 *    Width is sm:max-w-md = ~28rem on desktop.
 *  - History is collapsed behind a "Conversations" button.
 *
 * Streaming:
 *  - On submit we optimistically render the user bubble + an empty assistant
 *    bubble. Tool calls arriving on the SSE stream get rendered as pending
 *    cards; tool results upgrade them to "ok" with a summary; tokens fill
 *    the assistant text bubble.
 *  - We DO NOT use TanStack Query for the stream itself (it's not designed
 *    for streaming responses). We invalidate the conversations query after
 *    `done` so the history list reflects the new turn.
 *
 * History rehydration:
 *  - On conversation reload we get back individual rows: user, tool (with
 *    `parts: [{type:"tool_result", ...}]`), assistant. We render tool rows
 *    as the same `ToolCallCard` so the live and replayed views look
 *    identical. See `groupHistoryIntoTurns` for the layout logic.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  ExternalLink,
  Loader2,
  MessageSquare,
  Minus,
  Plus,
  SendHorizontal,
  Sparkles,
  Trash2,
  Wrench,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { Markdown } from "./markdown";
import {
  type ChatConversation,
  type ChatMessage,
  type ChatStreamEvent,
  type Citation,
  type MessagePart,
  type ToolInvocation,
  confirmProposal,
  deleteConversation,
  getConversation,
  listConversations,
  streamChatMessage,
} from "@/lib/chat-api";

// ---------------------------------------------------------------------------
// Hook: drive a single in-flight chat turn
// ---------------------------------------------------------------------------

interface UseChatTurnArgs {
  onConversationStarted: (conversationId: string) => void;
  onComplete: () => void;
}

/**
 * Optimistic state for the current in-flight turn.
 *
 * We keep the user prompt, the in-progress tool invocations (rendered as
 * cards), and the in-progress assistant text in one bag because they all
 * belong to the same logical "turn" and get cleared together when the
 * persisted version catches up.
 */
interface OptimisticTurn {
  userMessageId: string;
  assistantMessageId: string;
  conversationId: string;
  userContent: string;
  toolInvocations: ToolInvocation[];   // ordered by arrival
  assistantText: string;               // accumulated tokens
  finished: boolean;                   // true once `done` arrives
  finishReason?: string;
}

function emptyTurn(content: string, conversationId: string | null): OptimisticTurn {
  return {
    userMessageId: `temp_user_${Date.now()}`,
    assistantMessageId: `temp_asst_${Date.now()}`,
    conversationId: conversationId ?? "pending",
    userContent: content,
    toolInvocations: [],
    assistantText: "",
    finished: false,
  };
}

/**
 * Owns the optimistic state for an in-progress turn. The hook only knows
 * about ONE turn at a time — the current one. Past turns live in the
 * detail query result.
 *
 * Kept separate from the panel component so we can reset cleanly when
 * switching conversations and so the streaming logic stays testable.
 */
function useChatTurn({ onConversationStarted, onComplete }: UseChatTurnArgs) {
  const [turn, setTurn] = useState<OptimisticTurn | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reset = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setTurn(null);
    setStreaming(false);
    setStreamError(null);
  };

  const send = async (content: string, conversationId: string | null) => {
    if (streaming) return;
    setStreamError(null);
    setStreaming(true);
    setTurn(emptyTurn(content, conversationId));

    const controller = new AbortController();
    abortRef.current = controller;

    let resolvedConversationId = conversationId;

    const handle = (ev: ChatStreamEvent) => {
      if (ev.type === "start") {
        if (!resolvedConversationId) {
          resolvedConversationId = ev.conversation_id;
          onConversationStarted(ev.conversation_id);
        }
        setTurn((prev) =>
          prev
            ? {
                ...prev,
                conversationId: ev.conversation_id,
                userMessageId: ev.user_message_id,
                assistantMessageId: ev.assistant_message_id,
              }
            : prev,
        );
      } else if (ev.type === "token") {
        setTurn((prev) =>
          prev
            ? { ...prev, assistantText: prev.assistantText + ev.text }
            : prev,
        );
      } else if (ev.type === "tool_call") {
        setTurn((prev) =>
          prev
            ? {
                ...prev,
                toolInvocations: [
                  ...prev.toolInvocations,
                  {
                    tool_call_id: ev.tool_call_id,
                    name: ev.name,
                    args: ev.args,
                    status: "pending",
                  },
                ],
              }
            : prev,
        );
      } else if (ev.type === "tool_result") {
        setTurn((prev) =>
          prev
            ? {
                ...prev,
                toolInvocations: prev.toolInvocations.map((inv) =>
                  inv.tool_call_id === ev.tool_call_id
                    ? {
                        ...inv,
                        status: ev.ok ? "ok" : "error",
                        summary: ev.summary,
                        data: ev.data,
                        citations: ev.citations,
                        chart_spec: ev.chart_spec,
                      }
                    : inv,
                ),
              }
            : prev,
        );
      } else if (ev.type === "done") {
        setTurn((prev) =>
          prev
            ? { ...prev, finished: true, finishReason: ev.finish_reason }
            : prev,
        );
      } else if (ev.type === "error") {
        setStreamError(ev.error);
      }
    };

    try {
      await streamChatMessage({
        content,
        conversationId: conversationId ?? undefined,
        signal: controller.signal,
        onEvent: handle,
      });
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setStreamError((e as Error).message);
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      onComplete();
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
  };

  return { turn, streaming, streamError, send, cancel, reset };
}

// ---------------------------------------------------------------------------
// FAB + Panel (single exported component)
// ---------------------------------------------------------------------------

export function ChatLauncher() {
  const [open, setOpen] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(
    null,
  );
  const [historyOpen, setHistoryOpen] = useState(false);
  const queryClient = useQueryClient();

  const conversationsQuery = useQuery({
    queryKey: ["chat", "conversations"],
    queryFn: listConversations,
    enabled: open,
    staleTime: 10_000,
  });

  // Auto-select the most recent conversation when the panel opens for the
  // first time per session. Re-opens after explicit "New chat" clicks
  // shouldn't override, hence the explicit null check.
  useEffect(() => {
    if (!open) return;
    if (activeConversationId !== null) return;
    const list = conversationsQuery.data;
    if (list && list.length > 0) {
      setActiveConversationId(list[0].conversation_id);
    }
  }, [open, conversationsQuery.data, activeConversationId]);

  const detailQuery = useQuery({
    queryKey: ["chat", "conversation", activeConversationId],
    queryFn: () => getConversation(activeConversationId as string),
    enabled: open && Boolean(activeConversationId),
    staleTime: 5_000,
  });

  const turn = useChatTurn({
    onConversationStarted: (conversationId) => {
      setActiveConversationId(conversationId);
    },
    onComplete: () => {
      // Refresh both lists so the new turn shows up in history and so we
      // pick up the persisted message IDs / token counts.
      void queryClient.invalidateQueries({ queryKey: ["chat", "conversations"] });
      if (activeConversationId) {
        void queryClient.invalidateQueries({
          queryKey: ["chat", "conversation", activeConversationId],
        });
      }
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (conversationId: string) => deleteConversation(conversationId),
    onSuccess: (_, conversationId) => {
      if (activeConversationId === conversationId) {
        setActiveConversationId(null);
        turn.reset();
      }
      void queryClient.invalidateQueries({ queryKey: ["chat", "conversations"] });
    },
  });

  const startNewConversation = () => {
    turn.reset();
    setActiveConversationId(null);
    setHistoryOpen(false);
  };

  const persistedMessages = detailQuery.data?.messages ?? [];

  // Group history into "render units" — a sequence of either user bubbles,
  // tool cards (from role=tool rows), or assistant bubbles (with optional
  // citation chips). Order is preserved as-stored. We append the optimistic
  // turn at the end while the model is working.
  const renderUnits = useMemo<RenderUnit[]>(() => {
    const units = historyToRenderUnits(persistedMessages);
    if (turn.turn) {
      units.push(...optimisticTurnToUnits(turn.turn));
    }
    return units;
  }, [persistedMessages, turn.turn]);

  // Clear optimistic once the persisted version catches up.
  useEffect(() => {
    if (turn.streaming) return;
    if (!turn.turn) return;
    const matchInPersisted = persistedMessages.find(
      (m) => m.message_id === turn.turn?.assistantMessageId,
    );
    if (matchInPersisted) {
      turn.reset();
    }
  }, [persistedMessages, turn]);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      {/* The FAB. Fixed to the viewport bottom-right — out of the way of the
          sidebar and below the right-edge of any open right-side drawer. */}
      <SheetTrigger asChild>
        <Button
          aria-label="Open BHE Catalog assistant"
          size="icon"
          className={cn(
            "fixed bottom-6 right-6 z-40 h-14 w-14 rounded-full shadow-lg",
            "bg-primary hover:bg-primary/90 text-primary-foreground",
          )}
        >
          <Sparkles className="h-6 w-6" />
        </Button>
      </SheetTrigger>

      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 p-0 sm:max-w-md"
      >
        <header className="flex items-center justify-between border-b px-4 py-3">
          <SheetTitle className="flex items-center gap-2 text-base font-semibold">
            <Sparkles className="h-4 w-4 text-primary" />
            Catalog Assistant
          </SheetTitle>
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setHistoryOpen((v) => !v)}
              title="Show conversation history"
            >
              <MessageSquare className="h-4 w-4" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={startNewConversation}
              title="Start a new conversation"
            >
              <Plus className="h-4 w-4" />
            </Button>
          </div>
        </header>

        {historyOpen ? (
          <ConversationHistoryList
            conversations={conversationsQuery.data ?? []}
            isLoading={conversationsQuery.isLoading}
            activeId={activeConversationId}
            onSelect={(id) => {
              turn.reset();
              setActiveConversationId(id);
              setHistoryOpen(false);
            }}
            onDelete={(id) => deleteMutation.mutate(id)}
            isDeleting={deleteMutation.isPending}
          />
        ) : (
          <>
            <MessageList
              units={renderUnits}
              isStreaming={turn.streaming}
              isLoading={Boolean(activeConversationId) && detailQuery.isLoading}
              streamError={turn.streamError}
            />
            <Composer
              disabled={turn.streaming}
              onSend={(content) => turn.send(content, activeConversationId)}
              onCancel={turn.streaming ? turn.cancel : undefined}
            />
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function ConversationHistoryList(props: {
  conversations: ChatConversation[];
  isLoading: boolean;
  activeId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  isDeleting: boolean;
}) {
  const { conversations, isLoading, activeId, onSelect, onDelete, isDeleting } =
    props;
  return (
    <div className="flex-1 overflow-y-auto">
      {isLoading ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : conversations.length === 0 ? (
        <div className="px-4 py-12 text-center text-sm text-muted-foreground">
          No conversations yet.
        </div>
      ) : (
        <ul className="divide-y">
          {conversations.map((c) => {
            const isActive = c.conversation_id === activeId;
            return (
              <li
                key={c.conversation_id}
                className={cn(
                  "group flex items-center gap-2 px-4 py-3 hover:bg-muted/50",
                  isActive && "bg-muted",
                )}
              >
                <button
                  type="button"
                  className="flex-1 text-left"
                  onClick={() => onSelect(c.conversation_id)}
                >
                  <div className="line-clamp-1 text-sm font-medium">
                    {c.title || "(untitled)"}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {c.message_count} message{c.message_count === 1 ? "" : "s"}
                    {c.last_message_at
                      ? ` · ${formatRelative(c.last_message_at)}`
                      : ""}
                  </div>
                </button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="opacity-0 group-hover:opacity-100"
                  disabled={isDeleting}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (
                      window.confirm("Delete this conversation? This cannot be undone.")
                    ) {
                      onDelete(c.conversation_id);
                    }
                  }}
                  title="Delete conversation"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RenderUnit: a single thing the message list draws.
// ---------------------------------------------------------------------------
//
// We collapse the (user, tool*, assistant) sequence into a flat list of
// units so the list doesn't need to inspect message roles inline. Streaming
// optimistic state and persisted history both produce the same unit shapes
// (see `historyToRenderUnits` and `optimisticTurnToUnits`), so the renderer
// stays uniform regardless of source.

type RenderUnit =
  | { kind: "user"; key: string; content: string }
  | { kind: "tool"; key: string; invocation: ToolInvocation }
  | {
      kind: "assistant";
      key: string;
      content: string;
      pending: boolean;          // true while streaming, before final text
      error?: string | null;
      citations?: Citation[];    // collected from preceding tool invocations
    };

function historyToRenderUnits(messages: ChatMessage[]): RenderUnit[] {
  const units: RenderUnit[] = [];
  // Buffer the most recent run of tool messages so we can attach their
  // citations to the assistant unit they precede. This matters because
  // the tool result row is its own DB row but the citations conceptually
  // belong to the assistant turn.
  let pendingCitations: Citation[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      units.push({ kind: "user", key: m.message_id, content: m.content });
      pendingCitations = [];
    } else if (m.role === "tool") {
      const invocation = toolPartToInvocation(m);
      if (invocation) {
        units.push({ kind: "tool", key: m.message_id, invocation });
        if (invocation.citations) {
          pendingCitations.push(...invocation.citations);
        }
      }
    } else if (m.role === "assistant") {
      units.push({
        kind: "assistant",
        key: m.message_id,
        content: m.content,
        pending: false,
        error: m.error ?? null,
        citations: pendingCitations.length ? pendingCitations : undefined,
      });
      pendingCitations = [];
    }
  }
  return units;
}

function toolPartToInvocation(m: ChatMessage): ToolInvocation | null {
  // The persisted shape stores the full tool result as the only entry of
  // `parts`. Defensive against legacy rows that lack the structured part.
  const part = (m.parts || []).find(
    (p): p is Extract<MessagePart, { type: "tool_result" }> =>
      p.type === "tool_result",
  );
  if (!part) return null;
  return {
    tool_call_id: part.tool_call_id,
    name: part.name,
    args: part.args,
    status: part.ok ? "ok" : "error",
    summary: part.summary,
    data: part.data,
    citations: part.citations,
    chart_spec: part.chart_spec,
  };
}

function optimisticTurnToUnits(turn: OptimisticTurn): RenderUnit[] {
  const units: RenderUnit[] = [
    { kind: "user", key: turn.userMessageId, content: turn.userContent },
  ];
  for (const inv of turn.toolInvocations) {
    units.push({ kind: "tool", key: inv.tool_call_id, invocation: inv });
  }
  // Aggregate citations from all completed tool invocations of this turn
  // so the assistant card can show them as chips.
  const citations: Citation[] = [];
  for (const inv of turn.toolInvocations) {
    if (inv.status === "ok" && inv.citations) {
      citations.push(...inv.citations);
    }
  }
  units.push({
    kind: "assistant",
    key: turn.assistantMessageId,
    content: turn.assistantText,
    // Only "pending" if no text has come in yet AND the turn isn't done.
    // Once tokens start flowing we want the bubble visible immediately.
    pending: !turn.finished && turn.assistantText.length === 0,
    citations: citations.length ? citations : undefined,
  });
  return units;
}

// ---------------------------------------------------------------------------
// MessageList — renders RenderUnit[]
// ---------------------------------------------------------------------------

function MessageList(props: {
  units: RenderUnit[];
  isStreaming: boolean;
  isLoading: boolean;
  streamError: string | null;
}) {
  const { units, isStreaming, isLoading, streamError } = props;
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [units, isStreaming]);

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
      </div>
    );
  }

  if (units.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
        <Sparkles className="mb-3 h-8 w-8 text-primary/60" />
        <h3 className="text-sm font-semibold">Ask the Catalog Assistant</h3>
        <p className="mt-1 max-w-[18rem] text-xs text-muted-foreground">
          Try: "search for use cases about renewable energy" or "how many
          schemas are in PacifiCorp QA?"
        </p>
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
      {units.map((u) => {
        if (u.kind === "user") {
          return <UserBubble key={u.key} content={u.content} />;
        }
        if (u.kind === "tool") {
          return <ToolCallCard key={u.key} invocation={u.invocation} />;
        }
        return (
          <AssistantBubble
            key={u.key}
            content={u.content}
            pending={u.pending}
            error={u.error}
            citations={u.citations}
          />
        );
      })}
      {streamError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {streamError}
        </div>
      )}
    </div>
  );
}

function UserBubble({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-3 py-2 text-sm text-primary-foreground whitespace-pre-wrap">
        {content}
      </div>
    </div>
  );
}

function AssistantBubble(props: {
  content: string;
  pending: boolean;
  error?: string | null;
  citations?: Citation[];
}) {
  const { content, pending, error, citations } = props;
  // While pending with no text yet, render nothing — the tool card above
  // already shows progress, no need for an empty bubble that flashes "…".
  if (pending && !content) return null;
  return (
    <div className="flex justify-start">
      <div className="flex max-w-[85%] flex-col gap-2">
        <div className="rounded-2xl rounded-bl-sm bg-muted px-3 py-2 text-sm text-foreground">
          {content ? (
            <Markdown content={content} />
          ) : (
            <span className="text-muted-foreground italic">…</span>
          )}
          {error && (
            <div className="mt-1 text-xs text-destructive">{error}</div>
          )}
        </div>
        {citations && citations.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {citations.map((c, i) => (
              // Plain anchor (not TanStack `Link`) because tool deep-links
              // can include arbitrary search params not registered on the
              // typed routes. SPA navigation still happens — the router
              // intercepts in-app paths transparently.
              <a
                key={`${c.deeplink}-${i}`}
                href={c.deeplink}
                className={cn(
                  "inline-flex items-center gap-1 rounded-full border bg-background px-2 py-0.5",
                  "text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
                title={c.deeplink}
              >
                <ExternalLink className="h-2.5 w-2.5" />
                <span className="line-clamp-1 max-w-[14rem]">{c.label}</span>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolCallCard — collapsed-by-default tool invocation card
// ---------------------------------------------------------------------------

function ToolCallCard({ invocation }: { invocation: ToolInvocation }) {
  const [expanded, setExpanded] = useState(false);
  const isPending = invocation.status === "pending";
  const isError = invocation.status === "error";
  const Icon = invocation.name === "genie_ask" ? Database : Wrench;
  const headerLabel = TOOL_LABELS[invocation.name] ?? invocation.name;

  return (
    <div
      className={cn(
        "rounded-lg border text-xs",
        isPending && "border-primary/40 bg-primary/5",
        !isPending && !isError && "border-border bg-background",
        isError && "border-destructive/40 bg-destructive/5",
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        {isPending ? (
          <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />
        ) : isError ? (
          <AlertCircle className="h-3 w-3 shrink-0 text-destructive" />
        ) : (
          <Icon className="h-3 w-3 shrink-0 text-primary" />
        )}
        <span className="font-medium">{headerLabel}</span>
        <span className="ml-auto truncate text-muted-foreground">
          {isPending
            ? "running…"
            : invocation.summary || (isError ? "failed" : "done")}
        </span>
      </button>
      {/* Compact result preview shown inline, no expansion needed. Picks
          a per-tool layout so the user can read the answer without
          scrolling through raw JSON. Falls back to nothing when there's
          no useful preview shape (e.g. pending or error states). */}
      {!isPending && !isError && invocation.data && (
        <ToolResultPreview
          name={invocation.name}
          data={invocation.data}
        />
      )}
      {expanded && (
        <div className="border-t bg-background/40 px-2.5 py-2">
          <div className="mb-1.5">
            <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              Arguments
            </div>
            <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap break-words text-[11px] leading-snug">
              {JSON.stringify(invocation.args, null, 2)}
            </pre>
          </div>
          {invocation.data !== undefined && invocation.data !== null && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                Result
              </div>
              <pre className="mt-0.5 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-snug">
                {JSON.stringify(invocation.data, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Header label per tool name. Keep in sync with TOOLS in tools/__init__.py
// — drift here just shows the raw tool name, which is annoying but harmless.
const TOOL_LABELS: Record<string, string> = {
  app_search_use_cases: "Search use cases",
  app_get_use_case: "Use case detail",
  app_search_schemas: "Search schemas",
  app_get_schema: "Schema detail",
  app_list_affiliates: "List affiliates",
  app_list_source_systems: "List source systems",
  app_value_summary: "Value summary",
  app_value_source_rollup: "Source ROI",
  app_gaps_matrix: "Coverage gaps",
  app_propose_status_change: "Propose status change",
  app_propose_use_case_update: "Propose use case edit",
  app_propose_affiliate_mapping: "Propose affiliate change",
  app_propose_canonical_mapping: "Propose source mapping change",
  app_propose_use_case: "Propose new use case",
  app_propose_schema_update: "Propose schema edit",
  app_research_use_case: "Research similar use cases",
  app_research_schema: "Research schema peers",
  genie_ask: "Ask Genie",
};

// ---------------------------------------------------------------------------
// Tool result previews
// ---------------------------------------------------------------------------
//
// Each tool gets a small, opinionated inline view that shows the most
// useful 5-10 fields. The model's text bubble below carries the prose
// summary; this gives the user something concrete to skim while it
// streams. Anything not previewed here is still available in the raw
// JSON via the expand chevron.

interface ToolResultPreviewProps {
  name: string;
  data: unknown;
}

function ToolResultPreview({ name, data }: ToolResultPreviewProps) {
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;

  switch (name) {
    case "genie_ask":
      return <GenieResultPreview data={obj} />;
    case "app_search_use_cases":
      return <UseCaseListPreview items={obj.use_cases} />;
    case "app_get_use_case":
      return <UseCaseDetailPreview data={obj} />;
    case "app_search_schemas":
      return <SchemaListPreview items={obj.schemas} total={obj.total} />;
    case "app_get_schema":
      return <SchemaDetailPreview data={obj} />;
    case "app_list_affiliates":
      return <AffiliateListPreview items={obj.affiliates} />;
    case "app_list_source_systems":
      return <SourceListPreview items={obj.source_systems} />;
    case "app_value_summary":
      return <ValueSummaryPreview data={obj} />;
    case "app_value_source_rollup":
      return <SourceRollupPreview items={obj.sources} />;
    case "app_gaps_matrix":
      return <GapsMatrixPreview data={obj} />;
    case "app_propose_status_change":
      return <StatusChangeConfirmCard data={obj} />;
    case "app_propose_use_case_update":
      return <UseCaseUpdateConfirmCard data={obj} />;
    case "app_propose_affiliate_mapping":
      return <ListDiffConfirmCard data={obj} resourceLabel="affiliates" />;
    case "app_propose_canonical_mapping":
      return <ListDiffConfirmCard data={obj} resourceLabel="canonicals" />;
    case "app_propose_use_case":
      return <CreateUseCaseConfirmCard data={obj} />;
    case "app_propose_schema_update":
      return <SchemaUpdateConfirmCard data={obj} />;
    case "app_research_use_case":
      return <ResearchBriefPreview data={obj} />;
    case "app_research_schema":
      return <SchemaResearchBriefPreview data={obj} />;
    default:
      return null;
  }
}

const PreviewShell = ({ children }: { children: React.ReactNode }) => (
  <div className="border-t bg-background/40 px-2.5 py-2 text-[11px]">
    {children}
  </div>
);

const fmtMoneyChat = (n: number): string => {
  if (!Number.isFinite(n)) return "$0";
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${Math.round(n)}`;
};

// genie_ask: small results table (first ~5 rows) + collapsed SQL
function GenieResultPreview({ data }: { data: Record<string, unknown> }) {
  const rows = (data.rows as Array<Record<string, unknown>> | null) ?? [];
  const cols = (data.columns as string[] | null) ?? [];
  const sql = (data.sql as string | null) ?? "";
  const answer = (data.answer as string | null) ?? "";
  if (rows.length === 0 && !sql && !answer) return null;
  const previewRows = rows.slice(0, 5);
  const previewCols = cols.slice(0, 4); // 4 cols fit comfortably in the 28rem panel
  return (
    <PreviewShell>
      {answer && (
        <p className="mb-1.5 line-clamp-3 text-foreground/80">{answer}</p>
      )}
      {previewRows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="text-left text-muted-foreground">
                {previewCols.map((c) => (
                  <th key={c} className="border-b py-1 pr-2 font-medium">
                    {c}
                  </th>
                ))}
                {cols.length > previewCols.length && (
                  <th className="border-b py-1 pr-2 font-medium text-muted-foreground">
                    +{cols.length - previewCols.length}
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {previewRows.map((r, i) => (
                <tr key={i} className="border-b last:border-b-0">
                  {previewCols.map((c) => (
                    <td key={c} className="py-1 pr-2 align-top">
                      <span className="line-clamp-1">{formatCell(r[c])}</span>
                    </td>
                  ))}
                  {cols.length > previewCols.length && (
                    <td className="py-1 pr-2 text-muted-foreground">…</td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length > previewRows.length && (
            <div className="mt-1 text-[10px] text-muted-foreground">
              Showing {previewRows.length} of {rows.length} rows
            </div>
          )}
        </div>
      )}
      {sql && (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            SQL
          </summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-1.5 text-[10px] leading-snug">
            {sql}
          </pre>
        </details>
      )}
    </PreviewShell>
  );
}

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toFixed(2);
  }
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function UseCaseListPreview({ items }: { items: unknown }) {
  const list = (items as Array<Record<string, unknown>>) ?? [];
  if (list.length === 0) return null;
  return (
    <PreviewShell>
      <ul className="space-y-1">
        {list.slice(0, 5).map((u, i) => (
          <li key={i} className="flex items-center justify-between gap-2">
            <span className="line-clamp-1 font-medium">
              {String(u.use_case_name ?? "?")}
            </span>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {fmtMoneyChat(Number(u.estimated_value_usd) || 0)}
            </span>
          </li>
        ))}
      </ul>
      {list.length > 5 && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          +{list.length - 5} more
        </div>
      )}
    </PreviewShell>
  );
}

function UseCaseDetailPreview({ data }: { data: Record<string, unknown> }) {
  const uc = data.use_case as Record<string, unknown> | null;
  const r = data.readiness as Record<string, unknown> | null;
  if (!uc) return null;
  const value = Number(uc.estimated_value_usd) || 0;
  const ready = (r?.readiness_pct_simple ?? null) as number | null;
  return (
    <PreviewShell>
      <div className="grid grid-cols-3 gap-2">
        <Pill label="Value" value={fmtMoneyChat(value)} />
        <Pill
          label="Ready"
          value={ready == null ? "—" : `${ready.toFixed(0)}%`}
        />
        <Pill
          label="Sources"
          value={`${r?.present_count ?? 0}/${r?.total_required ?? 0}`}
        />
      </div>
      {uc.priority || uc.department ? (
        <div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted-foreground">
          {uc.department && <span>{String(uc.department)}</span>}
          {uc.priority && <span>· {String(uc.priority)}</span>}
          {uc.status && <span>· {String(uc.status)}</span>}
        </div>
      ) : null}
    </PreviewShell>
  );
}

function SchemaListPreview({
  items,
  total,
}: {
  items: unknown;
  total: unknown;
}) {
  const list = (items as Array<Record<string, unknown>>) ?? [];
  if (list.length === 0) return null;
  return (
    <PreviewShell>
      <ul className="space-y-1">
        {list.slice(0, 5).map((s, i) => (
          <li key={i} className="flex items-center justify-between gap-2">
            <span className="line-clamp-1 font-medium">
              {String(s.catalog_name ?? "")}.{String(s.schema_name ?? "")}
            </span>
            <span className="shrink-0 text-[10px] text-muted-foreground">
              {String(s.environment || "")}
              {s.has_definition === false && (
                <span className="ml-1 text-amber-600">no def</span>
              )}
            </span>
          </li>
        ))}
      </ul>
      {Number(total) > list.length && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          {list.length} of {Number(total)} shown
        </div>
      )}
    </PreviewShell>
  );
}

function SchemaDetailPreview({ data }: { data: Record<string, unknown> }) {
  const s = data.schema as Record<string, unknown> | null;
  if (!s) return null;
  const envs = (s.environments as string[] | null) ?? [];
  return (
    <PreviewShell>
      <div className="grid grid-cols-3 gap-2">
        <Pill label="Tables" value={String(s.total_tables ?? 0)} />
        <Pill label="Envs" value={envs.length ? envs.join("/") : "—"} />
        <Pill label="Affiliate" value={String(s.affiliate ?? "—")} />
      </div>
      {s.definition ? (
        <p className="mt-1.5 line-clamp-2 text-foreground/80">
          {String(s.definition)}
        </p>
      ) : (
        <div className="mt-1.5 text-amber-600">No AI definition</div>
      )}
    </PreviewShell>
  );
}

function AffiliateListPreview({ items }: { items: unknown }) {
  const list = (items as Array<Record<string, unknown>>) ?? [];
  if (list.length === 0) return null;
  return (
    <PreviewShell>
      <ul className="space-y-1">
        {list.slice(0, 6).map((a, i) => (
          <li key={i} className="flex items-center justify-between gap-2">
            <span className="line-clamp-1 font-medium">
              {String(a.affiliate_name ?? "?")}
            </span>
            <span className="shrink-0 text-[10px] text-muted-foreground">
              {String(a.use_case_count ?? 0)} UCs
            </span>
          </li>
        ))}
      </ul>
      {list.length > 6 && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          +{list.length - 6} more
        </div>
      )}
    </PreviewShell>
  );
}

function SourceListPreview({ items }: { items: unknown }) {
  const list = (items as Array<Record<string, unknown>>) ?? [];
  if (list.length === 0) return null;
  return (
    <PreviewShell>
      <ul className="space-y-1">
        {list.slice(0, 6).map((s, i) => (
          <li key={i} className="flex items-center justify-between gap-2">
            <span className="line-clamp-1 font-medium">
              {String(s.canonical ?? "?")}
              {s.is_present === false && (
                <span className="ml-1 text-[10px] text-amber-600">gap</span>
              )}
            </span>
            <span className="shrink-0 text-[10px] text-muted-foreground tabular-nums">
              {String(s.table_count ?? 0)} tbls
            </span>
          </li>
        ))}
      </ul>
      {list.length > 6 && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          +{list.length - 6} more
        </div>
      )}
    </PreviewShell>
  );
}

function ValueSummaryPreview({ data }: { data: Record<string, unknown> }) {
  const s = data.summary as Record<string, unknown> | null;
  if (!s) return null;
  return (
    <PreviewShell>
      <div className="grid grid-cols-3 gap-2">
        <Pill
          label="Total"
          value={fmtMoneyChat(Number(s.total_value) || 0)}
        />
        <Pill
          label="Ready"
          value={fmtMoneyChat(Number(s.ready_value) || 0)}
          tone="good"
        />
        <Pill
          label="Gap"
          value={fmtMoneyChat(Number(s.gap_value) || 0)}
          tone="warn"
        />
      </div>
      <div className="mt-1.5 text-[10px] text-muted-foreground">
        {String(s.total_use_cases ?? 0)} use cases
        {s.ready_pct != null && ` · ${Number(s.ready_pct).toFixed(0)}% ready`}
      </div>
    </PreviewShell>
  );
}

function SourceRollupPreview({ items }: { items: unknown }) {
  const list = (items as Array<Record<string, unknown>>) ?? [];
  if (list.length === 0) return null;
  const max = list.reduce(
    (m, r) => Math.max(m, Number(r.total_value) || 0),
    1,
  );
  return (
    <PreviewShell>
      <ul className="space-y-1">
        {list.slice(0, 6).map((s, i) => {
          const v = Number(s.total_value) || 0;
          const w = Math.round((v / max) * 100);
          return (
            <li key={i}>
              <div className="flex items-center justify-between gap-2">
                <span className="line-clamp-1 font-medium">
                  {String(s.canonical ?? "?")}
                  {s.is_present === false && (
                    <span className="ml-1 text-[10px] text-amber-600">gap</span>
                  )}
                </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {fmtMoneyChat(v)}
                </span>
              </div>
              <div className="mt-0.5 h-1 w-full overflow-hidden rounded-sm bg-muted">
                <div
                  className={cn(
                    "h-full",
                    s.is_present
                      ? "bg-emerald-500/70"
                      : "bg-amber-500/70",
                  )}
                  style={{ width: `${w}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </PreviewShell>
  );
}

function GapsMatrixPreview({ data }: { data: Record<string, unknown> }) {
  const totals = data.totals as Record<string, unknown> | null;
  const cells = (data.cells as Array<Record<string, unknown>>) ?? [];
  return (
    <PreviewShell>
      <div className="grid grid-cols-3 gap-2">
        <Pill
          label="Gaps"
          value={String(totals?.gap_count ?? 0)}
          tone="warn"
        />
        <Pill
          label="Covered"
          value={String(totals?.covered_count ?? 0)}
          tone="good"
        />
        <Pill
          label="At risk"
          value={fmtMoneyChat(Number(totals?.total_gap_value) || 0)}
        />
      </div>
      {cells.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {cells.slice(0, 4).map((c, i) => (
            <li
              key={i}
              className="flex items-center justify-between gap-2 text-[10px]"
            >
              <span className="line-clamp-1">
                <span
                  className={cn(
                    "mr-1 inline-block h-1.5 w-1.5 rounded-full",
                    c.state === "gap" && "bg-amber-500",
                    c.state === "covered" && "bg-emerald-500",
                    c.state === "available" && "bg-sky-500",
                  )}
                />
                <span className="font-medium">{String(c.canonical ?? "")}</span>
                <span className="text-muted-foreground">
                  {" "}
                  → {String(c.affiliate ?? "")}
                </span>
              </span>
              <span className="shrink-0 text-muted-foreground">
                {fmtMoneyChat(Number(c.total_value) || 0)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </PreviewShell>
  );
}

// ---------------------------------------------------------------------------
// ConfirmCard variants — render proposal payloads with Confirm/Cancel
// ---------------------------------------------------------------------------
//
// The backend's propose_* tools return `data.kind === "proposal"` with a
// short-lived single-use token. We render an inline card with Confirm /
// Cancel. Confirm POSTs /api/chat/confirm/{token}; Cancel just hides the
// card (token simply expires server-side after ~10min).
//
// We deliberately scope the click handler to a per-instance React state
// machine — once the token is consumed, the card collapses to a tidy
// "Done" footer that the rehydrated history view can mirror. The
// no-change path (handler returns kind != "proposal") falls through to
// nothing — the model's text bubble already explains there's nothing to
// confirm.

type ConfirmStatus = "idle" | "submitting" | "ok" | "error";

function StatusChangeConfirmCard({
  data,
}: {
  data: Record<string, unknown>;
}) {
  if (data.no_change) {
    // Model proposed a change that's identical to the current state;
    // backend short-circuited. Don't show a card, just a small badge.
    return (
      <PreviewShell>
        <div className="text-muted-foreground">
          No change needed — already at this status.
        </div>
      </PreviewShell>
    );
  }
  if (data.kind !== "proposal") return null;

  const uc = (data.use_case as Record<string, unknown> | null) ?? null;
  const before = (data.before as Record<string, unknown> | null) ?? null;
  const after = (data.after as Record<string, unknown> | null) ?? null;
  const confirm = (data.confirm as Record<string, unknown> | null) ?? null;
  const token = String(confirm?.token ?? "");
  const expiresAt = String(confirm?.expires_at ?? "");

  return (
    <ConfirmCardShell
      token={token}
      expiresAt={expiresAt}
      title={`Set "${String(uc?.use_case_name ?? "")}" status`}
      subline={
        uc?.department || uc?.priority
          ? [uc?.department, uc?.priority]
              .filter(Boolean)
              .map(String)
              .join(" · ")
          : undefined
      }
    >
      <DiffRow
        label="Status"
        before={String(before?.status ?? "—")}
        after={String(after?.status ?? "—")}
      />
      <DiffRow
        label="Notes"
        before={(before?.status_notes as string | null) || "—"}
        after={(after?.status_notes as string | null) || "—"}
      />
    </ConfirmCardShell>
  );
}

// Multi-field updater (app_propose_use_case_update). One DiffRow per
// field the model wants to change; reuses the same shell + queryClient
// invalidation strategy as the status card.
function UseCaseUpdateConfirmCard({
  data,
}: {
  data: Record<string, unknown>;
}) {
  if (data.no_change) {
    return (
      <PreviewShell>
        <div className="text-muted-foreground">
          All proposed values already match — nothing to confirm.
        </div>
      </PreviewShell>
    );
  }
  if (data.kind !== "proposal") return null;

  const uc = (data.use_case as Record<string, unknown> | null) ?? null;
  const before = (data.before as Record<string, unknown> | null) ?? {};
  const after = (data.after as Record<string, unknown> | null) ?? {};
  const confirm = (data.confirm as Record<string, unknown> | null) ?? null;
  const labels = (data.field_labels as Record<string, string> | null) ?? {};
  // Backend sends a stable order; fall back to Object.keys(after) for
  // older payloads or unexpected shapes.
  const order = Array.isArray(data.field_order)
    ? (data.field_order as string[])
    : Object.keys(after);

  const token = String(confirm?.token ?? "");
  const expiresAt = String(confirm?.expires_at ?? "");

  return (
    <ConfirmCardShell
      token={token}
      expiresAt={expiresAt}
      title={`Edit "${String(uc?.use_case_name ?? "")}"`}
      subline={
        order.length > 0
          ? `${order.length} field${order.length === 1 ? "" : "s"} changed`
          : undefined
      }
    >
      {order.map((field) => (
        <DiffRow
          key={field}
          label={labels[field] ?? field}
          before={formatFieldValue(field, before[field])}
          after={formatFieldValue(field, after[field])}
        />
      ))}
    </ConfirmCardShell>
  );
}

// Generic list-delta confirm card. Used by both
// `app_propose_affiliate_mapping` and `app_propose_canonical_mapping`
// because their JSON shapes are intentionally identical:
//
//   { kind: "proposal",
//     resource: "affiliates" | "canonicals",
//     additions: [{name?|canonical?, applicability?|necessity?, ...}],
//     removals:  [{name, current_applicability?|current_necessity?}],
//     skipped_noop: string[],
//     notice?: string,
//     confirm: { token, expires_at }, ... }
//
// The card renders a green "Adding" block + a red "Removing" block,
// optionally followed by the reseed-warning notice. The
// resourceLabel prop only affects copy ("3 affiliates" vs
// "3 canonicals").
function ListDiffConfirmCard({
  data,
  resourceLabel,
}: {
  data: Record<string, unknown>;
  resourceLabel: "affiliates" | "canonicals";
}) {
  if (data.no_change) {
    return (
      <PreviewShell>
        <div className="text-muted-foreground">
          All proposed {resourceLabel} changes are no-ops — nothing to
          confirm.
        </div>
      </PreviewShell>
    );
  }
  if (data.kind !== "proposal") return null;

  const uc = (data.use_case as Record<string, unknown> | null) ?? null;
  const additions = Array.isArray(data.additions)
    ? (data.additions as Array<Record<string, unknown>>)
    : [];
  const removals = Array.isArray(data.removals)
    ? (data.removals as Array<Record<string, unknown>>)
    : [];
  const skipped = Array.isArray(data.skipped_noop)
    ? (data.skipped_noop as string[])
    : [];
  const notice =
    typeof data.notice === "string" && data.notice ? data.notice : null;
  const confirm = (data.confirm as Record<string, unknown> | null) ?? null;
  const token = String(confirm?.token ?? "");
  const expiresAt = String(confirm?.expires_at ?? "");

  const isAffiliates = resourceLabel === "affiliates";
  // Singular/plural noun used in summary lines.
  const itemNoun = isAffiliates ? "affiliate" : "canonical";

  // Pretty subline like "+2 affiliates · −1 affiliate". Pluralizes per
  // count, not per total, because (+2, −0) reads weirdly as "−0 affiliates".
  const sublineBits: string[] = [];
  if (additions.length > 0) {
    sublineBits.push(
      `+${additions.length} ${itemNoun}${additions.length === 1 ? "" : "s"}`,
    );
  }
  if (removals.length > 0) {
    sublineBits.push(
      `−${removals.length} ${itemNoun}${removals.length === 1 ? "" : "s"}`,
    );
  }

  return (
    <ConfirmCardShell
      token={token}
      expiresAt={expiresAt}
      title={`Update ${resourceLabel} on "${String(uc?.use_case_name ?? "")}"`}
      subline={sublineBits.join(" · ")}
    >
      {additions.length > 0 && (
        <div className="space-y-1">
          {additions.map((row, i) => (
            <ListDiffRow
              key={`add-${i}`}
              kind="add"
              name={String(
                isAffiliates
                  ? row.affiliate_name ?? row.name ?? ""
                  : row.canonical ?? row.name ?? "",
              )}
              tag={String(
                isAffiliates
                  ? row.applicability ?? ""
                  : row.necessity ?? "",
              )}
              extra={
                isAffiliates
                  ? (row.rationale as string | null) ?? null
                  : (row.data_need_excerpt as string | null) ?? null
              }
              isUpdate={Boolean(row._was_present)}
              prevTag={String(
                isAffiliates
                  ? row._prev_applicability ?? ""
                  : row._prev_necessity ?? "",
              )}
            />
          ))}
        </div>
      )}
      {removals.length > 0 && (
        <div className="mt-1 space-y-1">
          {removals.map((row, i) => (
            <ListDiffRow
              key={`rem-${i}`}
              kind="remove"
              name={String(row.name ?? "")}
              tag={String(
                isAffiliates
                  ? row.current_applicability ?? ""
                  : row.current_necessity ?? "",
              )}
              extra={null}
              isUpdate={false}
              prevTag=""
            />
          ))}
        </div>
      )}
      {notice && (
        <div className="mt-1.5 flex items-start gap-1 text-[10px] text-amber-700 dark:text-amber-300">
          <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{notice}</span>
        </div>
      )}
      {skipped.length > 0 && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          Skipped (already matches): {skipped.join(", ")}
        </div>
      )}
    </ConfirmCardShell>
  );
}

// Research-brief preview (A2-5). Shows the structured brief returned
// by `app_research_use_case` so the user can sanity-check what the
// model is about to use as starting values for a downstream
// `app_propose_use_case` call.
//
// NOT a confirm card — this is a pure read; nothing to apply.
// Uses the standard PreviewShell, not the amber ConfirmCardShell.
function ResearchBriefPreview({ data }: { data: Record<string, unknown> }) {
  if (data.kind !== "research_brief") return null;

  const topic = String(data.topic ?? "");
  const matchedCount = Number(data.matched_count ?? 0);
  const similar = Array.isArray(data.similar_use_cases)
    ? (data.similar_use_cases as Array<Record<string, unknown>>)
    : [];
  const suggestions =
    (data.suggestions as Record<string, unknown> | null) ?? {};
  const warnings = Array.isArray(data.warnings)
    ? (data.warnings as string[])
    : [];

  const value = (suggestions.value as Record<string, unknown> | null) ?? {};
  const valueCount = Number(value.count ?? 0);
  const affiliates = Array.isArray(suggestions.affiliates)
    ? (suggestions.affiliates as Array<Record<string, unknown>>)
    : [];
  const canonicals = Array.isArray(suggestions.canonicals)
    ? (suggestions.canonicals as Array<Record<string, unknown>>)
    : [];
  const departments = Array.isArray(suggestions.departments)
    ? (suggestions.departments as Array<Record<string, unknown>>)
    : [];
  const categories = Array.isArray(suggestions.categories)
    ? (suggestions.categories as Array<Record<string, unknown>>)
    : [];
  const priority = String(suggestions.priority ?? "Medium");

  if (matchedCount === 0) {
    // Distinct empty state: the topic was valid but no UC matched any
    // token. The model will likely propose a fresh create with no
    // suggestions to anchor to — surface that explicitly so the user
    // knows the "no anchor" risk.
    return (
      <PreviewShell>
        <div className="text-muted-foreground">
          No similar use cases found for{" "}
          <span className="font-medium text-foreground">"{topic}"</span>.
          The proposal will be drafted from scratch — confirm the
          suggested fields carefully.
        </div>
      </PreviewShell>
    );
  }

  return (
    <PreviewShell>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 font-medium">
          <Sparkles className="h-3 w-3 text-violet-600" />
          <span>Research brief</span>
        </div>
        <span className="text-[10px] text-muted-foreground">
          {matchedCount} match{matchedCount === 1 ? "" : "es"}
        </span>
      </div>

      {warnings.length > 0 && (
        <div className="mb-1.5 space-y-0.5">
          {warnings.map((w, i) => (
            <div
              key={i}
              className="flex items-start gap-1 text-amber-700 dark:text-amber-300"
            >
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {/* Similar use cases — top N as compact rows. The model can cite
          these by name in its prose, and the user can click through.
          We deliberately don't render the description here (would
          double the card height); it's available in the raw JSON. */}
      {similar.length > 0 && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Similar use cases
          </div>
          <ul className="mb-1.5 space-y-0.5">
            {similar.map((u, i) => (
              <li
                key={i}
                className="flex items-baseline justify-between gap-2"
              >
                <span className="line-clamp-1 font-medium">
                  {String(u.use_case_name ?? "?")}
                </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {fmtMoneyChat(Number(u.estimated_value_usd) || 0)}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* Suggested fields. Three-column pill row keeps it scannable.
          Value is min-max with median in the subtext (shown when we
          have at least one non-zero value). */}
      <div className="mb-1.5 grid grid-cols-3 gap-1">
        <Pill
          label="Department"
          value={departments[0] ? String(departments[0].name ?? "—") : "—"}
        />
        <Pill
          label="Category"
          value={categories[0] ? String(categories[0].name ?? "—") : "—"}
        />
        <Pill label="Priority" value={priority} />
      </div>
      {valueCount > 0 && (
        <div className="mb-1.5 rounded border bg-muted/40 px-1.5 py-1">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Value range
          </div>
          <div className="font-medium tabular-nums">
            {fmtMoneyChat(Number(value.min) || 0)} –{" "}
            {fmtMoneyChat(Number(value.max) || 0)}
          </div>
          <div className="text-[10px] text-muted-foreground">
            median {fmtMoneyChat(Number(value.median) || 0)} · n={valueCount}
          </div>
        </div>
      )}

      {/* Suggested mappings. Each row shows the name, the suggested
          tag (applicability/necessity), and the frequency (e.g. 3/5
          → "3 of 5 similar UCs use this"). */}
      {affiliates.length > 0 && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Suggested affiliates
          </div>
          <div className="mb-1.5 space-y-0.5">
            {affiliates.map((a, i) => (
              <SuggestionRow
                key={i}
                name={String(a.name ?? "")}
                tag={String(a.applicability_hint ?? "")}
                frequency={`${Number(a.count ?? 0)}/${matchedCount}`}
                tone={
                  String(a.applicability_hint ?? "") === "primary"
                    ? "good"
                    : "neutral"
                }
              />
            ))}
          </div>
        </>
      )}
      {canonicals.length > 0 && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Suggested source systems
          </div>
          <div className="space-y-0.5">
            {canonicals.map((c, i) => (
              <SuggestionRow
                key={i}
                name={String(c.canonical ?? "")}
                tag={String(c.necessity_hint ?? "")}
                frequency={`${Number(c.count ?? 0)}/${matchedCount}`}
                tone={
                  String(c.necessity_hint ?? "") === "must_have"
                    ? "good"
                    : "neutral"
                }
              />
            ))}
          </div>
        </>
      )}
    </PreviewShell>
  );
}

// One row in the "Suggested affiliates / source systems" lists.
// Compact: name + tag pill + frequency string ("3/5").
function SuggestionRow({
  name,
  tag,
  frequency,
  tone,
}: {
  name: string;
  tag: string;
  frequency: string;
  tone: "good" | "neutral";
}) {
  return (
    <div className="flex items-baseline justify-between gap-1.5">
      <div className="flex items-baseline gap-1.5 min-w-0">
        <span className="truncate font-medium">{name}</span>
        {tag && (
          <span
            className={cn(
              "rounded px-1 py-0 text-[9px] font-medium uppercase tracking-wide",
              tone === "good"
                ? "border border-emerald-300 bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200"
                : "border border-muted-foreground/30 bg-muted text-muted-foreground",
            )}
          >
            {tag}
          </span>
        )}
      </div>
      <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground">
        {frequency}
      </span>
    </div>
  );
}

// Schema-research brief preview (A3-1). Sibling of ResearchBriefPreview;
// different shape because the FOCUS is on a single existing schema's
// peers and current state, not a free-text topic.
function SchemaResearchBriefPreview({
  data,
}: {
  data: Record<string, unknown>;
}) {
  if (data.kind !== "schema_research_brief") return null;

  const schemaName = String(data.schema_name ?? "");
  const current =
    (data.current as Record<string, unknown> | null) ?? null;
  const peers = Array.isArray(data.peer_schemas)
    ? (data.peer_schemas as Array<Record<string, unknown>>)
    : [];
  const peerCount = Number(data.peer_count ?? 0);
  const suggestions =
    (data.suggestions as Record<string, unknown> | null) ?? {};
  const warnings = Array.isArray(data.warnings)
    ? (data.warnings as string[])
    : [];

  const tablesSample = current
    ? (Array.isArray(current.tables_sample)
        ? (current.tables_sample as Array<Record<string, unknown>>)
        : [])
    : [];
  const tableCount = current ? Number(current.table_count ?? 0) : 0;
  const catalogs = current
    ? (Array.isArray(current.catalogs) ? (current.catalogs as string[]) : [])
    : [];

  const domainSuggestions = Array.isArray(suggestions.suggested_domain)
    ? (suggestions.suggested_domain as Array<Record<string, unknown>>)
    : [];
  const deptSuggestions = Array.isArray(suggestions.suggested_department)
    ? (suggestions.suggested_department as Array<Record<string, unknown>>)
    : [];
  const sensSuggestions = Array.isArray(suggestions.data_sensitivity)
    ? (suggestions.data_sensitivity as Array<Record<string, unknown>>)
    : [];
  const sampleDefs = Array.isArray(suggestions.sample_definitions)
    ? (suggestions.sample_definitions as string[])
    : [];

  return (
    <PreviewShell>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 font-medium">
          <Sparkles className="h-3 w-3 text-violet-600" />
          <span>Schema research</span>
        </div>
        <span className="text-[10px] text-muted-foreground">
          {peerCount} peer{peerCount === 1 ? "" : "s"}
        </span>
      </div>

      {warnings.length > 0 && (
        <div className="mb-1.5 space-y-0.5">
          {warnings.map((w, i) => (
            <div
              key={i}
              className="flex items-start gap-1 text-amber-700 dark:text-amber-300"
            >
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {/* Current state — only if the schema actually exists. Skip
          when current=null (schema not found; warnings already cover it). */}
      {current && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Current — {schemaName}
          </div>
          <div className="mb-1.5 rounded border bg-muted/40 px-1.5 py-1 text-[10px]">
            <div className="text-muted-foreground">
              {tableCount} table{tableCount === 1 ? "" : "s"} ·{" "}
              {catalogs.length} catalog{catalogs.length === 1 ? "" : "s"}
              {catalogs.length > 0 && (
                <span className="ml-1">({catalogs.join(", ")})</span>
              )}
            </div>
            <div className="mt-1 grid grid-cols-3 gap-1">
              <SchemaCurrentField
                label="Domain"
                value={String(current.suggested_domain ?? "—")}
              />
              <SchemaCurrentField
                label="Dept"
                value={String(current.suggested_department ?? "—")}
              />
              <SchemaCurrentField
                label="Sensitivity"
                value={String(current.data_sensitivity ?? "—")}
              />
            </div>
            {current.ai_definition && (
              <div className="mt-1 border-t pt-1 text-muted-foreground">
                <span className="font-medium text-foreground">Definition: </span>
                {String(current.ai_definition)}
              </div>
            )}
          </div>
        </>
      )}

      {/* Sample tables — strong signal for inferring what the schema
          actually contains, especially when its current definition
          is empty. */}
      {tablesSample.length > 0 && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Sample tables{tableCount > tablesSample.length && ` (${tablesSample.length} of ${tableCount})`}
          </div>
          <ul className="mb-1.5 space-y-0.5">
            {tablesSample.map((t, i) => (
              <li key={i} className="text-[10px]">
                <span className="font-medium">{String(t.name ?? "?")}</span>
                {t.ai_definition && (
                  <span className="ml-1 text-muted-foreground">
                    — {String(t.ai_definition)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {/* Peer schemas. Compact list — name + domain badge + score. */}
      {peers.length > 0 && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Peer schemas
          </div>
          <ul className="mb-1.5 space-y-0.5">
            {peers.map((p, i) => (
              <li
                key={i}
                className="flex items-baseline justify-between gap-2"
              >
                <span className="line-clamp-1 font-medium">
                  {String(p.schema_name ?? "?")}
                </span>
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  {p.suggested_domain && (
                    <span className="mr-1">{String(p.suggested_domain)}</span>
                  )}
                  {p.user_edited && (
                    <span className="mr-1 rounded border border-emerald-300 bg-emerald-100 px-1 py-0 text-[9px] font-medium uppercase tracking-wide text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">
                      curated
                    </span>
                  )}
                  <span className="tabular-nums">
                    score {Number(p.score ?? 0)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* Aggregated suggestions. Frequency `N/peerCount` so the user
          knows how dominant each suggestion is. */}
      {(domainSuggestions.length > 0 ||
        deptSuggestions.length > 0 ||
        sensSuggestions.length > 0) && (
        <>
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Suggestions from peers
          </div>
          <div className="space-y-0.5">
            {domainSuggestions.map((s, i) => (
              <SuggestionRow
                key={`dom-${i}`}
                name={`Domain · ${String(s.name ?? "")}`}
                tag=""
                frequency={`${Number(s.count ?? 0)}/${peerCount}`}
                tone="good"
              />
            ))}
            {deptSuggestions.map((s, i) => (
              <SuggestionRow
                key={`dept-${i}`}
                name={`Dept · ${String(s.name ?? "")}`}
                tag=""
                frequency={`${Number(s.count ?? 0)}/${peerCount}`}
                tone="neutral"
              />
            ))}
            {sensSuggestions.map((s, i) => (
              <SuggestionRow
                key={`sens-${i}`}
                name={`Sensitivity · ${String(s.name ?? "")}`}
                tag=""
                frequency={`${Number(s.count ?? 0)}/${peerCount}`}
                tone="neutral"
              />
            ))}
          </div>
        </>
      )}

      {/* Sample definitions — high-value style references for the
          model when it composes a new ai_definition. We render them
          inline (truncated) so the user can sanity-check the
          quality. */}
      {sampleDefs.length > 0 && (
        <div className="mt-1.5">
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Sample peer definitions
          </div>
          <div className="space-y-1">
            {sampleDefs.map((d, i) => (
              <div
                key={i}
                className="rounded border bg-muted/30 px-1.5 py-1 text-[10px] italic text-muted-foreground"
              >
                "{d}"
              </div>
            ))}
          </div>
        </div>
      )}
    </PreviewShell>
  );
}

// One pill in the Current-Schema mini-summary. Compact label +
// truncated value, three to a row.
function SchemaCurrentField({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="truncate">{value}</div>
    </div>
  );
}

// Schema-update confirm card (A3-1). Multi-field diff with per-catalog
// before-values when divergent ("dev says X, prod says Y → Z"), single
// "X → Z" row when all catalogs agree.
function SchemaUpdateConfirmCard({
  data,
}: {
  data: Record<string, unknown>;
}) {
  if (data.no_change) {
    return (
      <PreviewShell>
        <div className="text-muted-foreground">
          All proposed values already match — nothing to confirm.
        </div>
      </PreviewShell>
    );
  }
  if (data.kind !== "proposal") return null;

  const schema = (data.schema as Record<string, unknown> | null) ?? {};
  const schemaName = String(schema.schema_name ?? "");
  const catalogs = Array.isArray(schema.catalogs)
    ? (schema.catalogs as string[])
    : [];
  const allCatalogs = Array.isArray(schema.all_catalogs)
    ? (schema.all_catalogs as string[])
    : catalogs;
  const narrowed = Boolean(schema.narrowed);

  const beforePerCatalog =
    (data.before_per_catalog as Record<
      string,
      Record<string, string | null>
    > | null) ?? {};
  const after = (data.after as Record<string, string> | null) ?? {};
  const labels =
    (data.field_labels as Record<string, string> | null) ?? {};
  const order = Array.isArray(data.field_order)
    ? (data.field_order as string[])
    : Object.keys(after);
  const divergent = new Set(
    Array.isArray(data.divergent_fields)
      ? (data.divergent_fields as string[])
      : [],
  );
  const warnings = Array.isArray(data.warnings)
    ? (data.warnings as string[])
    : [];
  const confirm = (data.confirm as Record<string, unknown> | null) ?? null;
  const token = String(confirm?.token ?? "");
  const expiresAt = String(confirm?.expires_at ?? "");

  const subline =
    `${order.length} field${order.length === 1 ? "" : "s"} · ` +
    `${catalogs.length} catalog${catalogs.length === 1 ? "" : "s"}` +
    (narrowed && allCatalogs.length > catalogs.length
      ? ` (of ${allCatalogs.length})`
      : "");

  return (
    <ConfirmCardShell
      token={token}
      expiresAt={expiresAt}
      title={`Edit schema "${schemaName}"`}
      subline={subline}
    >
      {warnings.length > 0 && (
        <div className="space-y-0.5">
          {warnings.map((w, i) => (
            <div
              key={i}
              className="flex items-start gap-1 text-amber-700 dark:text-amber-300"
            >
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}
      {order.map((field) => (
        <SchemaDiffRow
          key={field}
          label={labels[field] ?? field}
          beforePerCatalog={beforePerCatalog[field] ?? {}}
          after={after[field] ?? ""}
          divergent={divergent.has(field)}
        />
      ))}
    </ConfirmCardShell>
  );
}

// Single field row inside SchemaUpdateConfirmCard. When the catalogs
// agree we collapse the before-side to one value; when divergent we
// show one line per catalog (small, dimmed) so the user can see
// what's being collapsed.
function SchemaDiffRow({
  label,
  beforePerCatalog,
  after,
  divergent,
}: {
  label: string;
  beforePerCatalog: Record<string, string | null>;
  after: string;
  divergent: boolean;
}) {
  const entries = Object.entries(beforePerCatalog);
  // Pick a representative single value when not divergent.
  const singleBefore =
    entries.length > 0 ? entries[0][1] ?? "" : "";

  return (
    <div className="rounded border bg-muted/20 px-1.5 py-1">
      <div className="mb-0.5 flex items-center justify-between gap-2">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        {divergent && (
          <span className="rounded border border-amber-300 bg-amber-100 px-1 py-0 text-[9px] font-medium uppercase tracking-wide text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
            divergent
          </span>
        )}
      </div>
      {divergent ? (
        <div className="mb-1 space-y-0.5">
          {entries.map(([cat, val]) => (
            <div key={cat} className="text-[10px] line-through opacity-70">
              <span className="text-muted-foreground">{cat}:</span>{" "}
              {val || "—"}
            </div>
          ))}
        </div>
      ) : (
        <div className="mb-1 truncate text-[10px] line-through opacity-70">
          {singleBefore || "—"}
        </div>
      )}
      <div className="rounded border bg-emerald-50/50 px-1.5 py-0.5 text-[11px] dark:bg-emerald-950/20">
        {after.length > 200 ? `${after.slice(0, 198)}…` : after}
      </div>
    </div>
  );
}

// Create-use-case confirm card (A2-4). Renders the proposed parent
// fields PLUS sections for any inline affiliate / canonical
// mappings the propose tool included. Re-uses ListDiffRow for the
// child sections (with kind="add" since this is a brand-new row,
// no removal possible) and a simple key:value list for the parent
// fields (no DiffRow because there's no "before").
function CreateUseCaseConfirmCard({
  data,
}: {
  data: Record<string, unknown>;
}) {
  if (data.kind !== "proposal") return null;

  const fields = (data.fields as Record<string, unknown> | null) ?? {};
  const labels = (data.field_labels as Record<string, string> | null) ?? {};
  const order = Array.isArray(data.field_order)
    ? (data.field_order as string[])
    : Object.keys(fields);
  const affiliates = Array.isArray(data.affiliates)
    ? (data.affiliates as Array<Record<string, unknown>>)
    : [];
  const canonicals = Array.isArray(data.canonicals)
    ? (data.canonicals as Array<Record<string, unknown>>)
    : [];
  const confirm = (data.confirm as Record<string, unknown> | null) ?? null;
  const token = String(confirm?.token ?? "");
  const expiresAt = String(confirm?.expires_at ?? "");

  // Skip the name from the field rows — it's already in the title.
  const fieldRows = order.filter(
    (f) => f !== "use_case_name" && fields[f] !== undefined && fields[f] !== "" && fields[f] !== null,
  );

  // Subline: "+1 use case · +N affiliates · +M canonicals" so the
  // user can see the blast radius at a glance.
  const subBits: string[] = ["+1 use case"];
  if (affiliates.length > 0) {
    subBits.push(
      `+${affiliates.length} affiliate${affiliates.length === 1 ? "" : "s"}`,
    );
  }
  if (canonicals.length > 0) {
    subBits.push(
      `+${canonicals.length} canonical${canonicals.length === 1 ? "" : "s"}`,
    );
  }

  const ucName = String(fields.use_case_name ?? "");

  return (
    <ConfirmCardShell
      token={token}
      expiresAt={expiresAt}
      title={`Create "${ucName}"`}
      subline={subBits.join(" · ")}
    >
      {fieldRows.length > 0 && (
        <div className="space-y-0.5">
          {fieldRows.map((field) => (
            <CreateFieldRow
              key={field}
              label={labels[field] ?? field}
              value={formatFieldValue(field, fields[field])}
            />
          ))}
        </div>
      )}
      {affiliates.length > 0 && (
        <div className="mt-1.5">
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Affiliates
          </div>
          <div className="space-y-1">
            {affiliates.map((row, i) => (
              <ListDiffRow
                key={`aff-${i}`}
                kind="add"
                name={String(row.affiliate_name ?? row.name ?? "")}
                tag={String(row.applicability ?? "")}
                extra={(row.rationale as string | null) ?? null}
                isUpdate={false}
                prevTag=""
              />
            ))}
          </div>
        </div>
      )}
      {canonicals.length > 0 && (
        <div className="mt-1.5">
          <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            Source systems
          </div>
          <div className="space-y-1">
            {canonicals.map((row, i) => (
              <ListDiffRow
                key={`can-${i}`}
                kind="add"
                name={String(row.canonical ?? "")}
                tag={String(row.necessity ?? "")}
                extra={(row.data_need_excerpt as string | null) ?? null}
                isUpdate={false}
                prevTag=""
              />
            ))}
          </div>
        </div>
      )}
      {affiliates.length === 0 && canonicals.length === 0 && (
        // Soft warning: a use case with no mappings is an orphan;
        // surfaced here too in case the model didn't say it in prose.
        <div className="mt-1.5 flex items-start gap-1 text-[10px] text-amber-700 dark:text-amber-300">
          <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>
            No affiliates or source systems specified. The use case will
            be created but won't show up on any affiliate's coverage view
            or readiness score until you add them.
          </span>
        </div>
      )}
    </ConfirmCardShell>
  );
}

// Single key:value row used by CreateUseCaseConfirmCard. Compact
// flexbox layout that matches the visual rhythm of DiffRow without
// taking up a 4-column grid (no "before" side to render).
function CreateFieldRow({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="grid grid-cols-[100px_1fr] items-baseline gap-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="truncate rounded border bg-emerald-50/50 px-1.5 py-0.5 text-[11px] dark:bg-emerald-950/20">
        {value}
      </div>
    </div>
  );
}

function ListDiffRow({
  kind,
  name,
  tag,
  extra,
  isUpdate,
  prevTag,
}: {
  kind: "add" | "remove";
  name: string;
  tag: string;
  extra: string | null;
  isUpdate: boolean;
  prevTag: string;
}) {
  const isAdd = kind === "add";
  const Icon = isAdd ? Plus : Minus;
  const tagShown =
    isAdd && isUpdate && prevTag && prevTag !== tag
      ? `${prevTag} → ${tag}`
      : tag;
  return (
    <div className="flex items-start gap-1.5">
      <Icon
        className={cn(
          "mt-0.5 h-3 w-3 shrink-0",
          isAdd ? "text-emerald-600" : "text-rose-600",
        )}
      />
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-baseline gap-1.5">
          <span
            className={cn(
              "truncate font-medium",
              !isAdd && "line-through opacity-70",
            )}
          >
            {name}
          </span>
          {tagShown && (
            <span
              className={cn(
                "rounded px-1 py-0 text-[9px] font-medium uppercase tracking-wide",
                isAdd
                  ? "border border-emerald-300 bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200"
                  : "border border-rose-300 bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200",
              )}
            >
              {tagShown}
            </span>
          )}
          {isAdd && isUpdate && (
            <span className="rounded border border-amber-300 bg-amber-100 px-1 py-0 text-[9px] font-medium uppercase tracking-wide text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
              update
            </span>
          )}
        </div>
        {extra && (
          <div className="truncate text-[10px] text-muted-foreground">
            {extra.length > 100 ? `${extra.slice(0, 98)}…` : extra}
          </div>
        )}
      </div>
    </div>
  );
}

// Field-aware formatter. Money values get $-formatted; nulls/empty
// render as em-dash; long strings (description, value_rationale) get
// truncated with an ellipsis since the diff row only fits ~80 chars.
function formatFieldValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (field === "estimated_value_usd") {
    const n = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(n)) return "—";
    if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
    if (Math.abs(n) >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
    return `$${n.toFixed(0)}`;
  }
  const s = String(value);
  return s.length > 80 ? `${s.slice(0, 78)}…` : s;
}

function ConfirmCardShell({
  token,
  expiresAt,
  title,
  subline,
  children,
}: {
  token: string;
  expiresAt: string;
  title: string;
  subline?: string;
  children: React.ReactNode;
}) {
  const [status, setStatus] = useState<ConfirmStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const queryClient = useQueryClient();

  const expired = useMemo(() => {
    if (!expiresAt) return false;
    const t = Date.parse(expiresAt);
    return Number.isFinite(t) && t < Date.now();
  }, [expiresAt]);

  const onConfirm = async () => {
    if (!token || status === "submitting") return;
    setStatus("submitting");
    setError(null);
    try {
      await confirmProposal(token);
      setStatus("ok");
      // Bust caches that the write affects so any open page (e.g. the
      // Value & Readiness drawer) reflects the change immediately.
      void queryClient.invalidateQueries({ queryKey: ["valueUseCases"] });
      void queryClient.invalidateQueries({ queryKey: ["valueSummary"] });
      void queryClient.invalidateQueries({ queryKey: ["valueUseCaseDetail"] });
      void queryClient.invalidateQueries({ queryKey: ["valueSourceRollup"] });
      // Mapping changes (affiliate / canonical) ripple into the Sankey
      // and source detail panels too — invalidate them so any open page
      // reflects the change without a manual refresh.
      void queryClient.invalidateQueries({ queryKey: ["valueSankey"] });
      void queryClient.invalidateQueries({ queryKey: ["valueSourceDetail"] });
      // Schema edits (A3-1) ripple into the catalog browser, schema
      // explorer, and AI coverage stats — invalidate broadly. We
      // can't tell from the confirm response alone whether this was
      // a schema edit or a use-case edit (the FE would need to
      // inspect the intent) but cache busts are cheap on idle pages.
      void queryClient.invalidateQueries({ queryKey: ["schemas"] });
      void queryClient.invalidateQueries({ queryKey: ["silverSchemas"] });
      void queryClient.invalidateQueries({ queryKey: ["schemaInventory"] });
      void queryClient.invalidateQueries({ queryKey: ["catalogStats"] });
    } catch (e) {
      setStatus("error");
      setError((e as Error).message ?? "Confirm failed");
    }
  };

  if (cancelled) {
    return (
      <PreviewShell>
        <div className="text-muted-foreground">
          Cancelled. Nothing was written.
        </div>
      </PreviewShell>
    );
  }

  return (
    <div className="border-t bg-amber-50/40 px-2.5 py-2 text-[11px] dark:bg-amber-950/20">
      <div className="mb-1.5 flex items-center gap-1.5 text-amber-800 dark:text-amber-200">
        <AlertCircle className="h-3 w-3" />
        <span className="font-semibold">Confirm to apply</span>
      </div>
      <div className="mb-1 font-medium text-foreground">{title}</div>
      {subline && (
        <div className="mb-1.5 text-[10px] text-muted-foreground">
          {subline}
        </div>
      )}
      <div className="space-y-1">{children}</div>
      <div className="mt-2 flex items-center justify-end gap-1.5">
        {status === "ok" ? (
          <span className="flex items-center gap-1 text-emerald-700">
            <Check className="h-3 w-3" />
            Applied
          </span>
        ) : (
          <>
            {error && (
              <span className="mr-auto text-destructive">{error}</span>
            )}
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-6 px-2 text-[11px]"
              disabled={status === "submitting"}
              onClick={() => setCancelled(true)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              className="h-6 px-2 text-[11px]"
              disabled={status === "submitting" || expired}
              onClick={onConfirm}
              title={expired ? "Token expired — ask again to re-issue" : undefined}
            >
              {status === "submitting" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : expired ? (
                "Expired"
              ) : (
                "Confirm"
              )}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function DiffRow({
  label,
  before,
  after,
}: {
  label: string;
  before: string;
  after: string;
}) {
  const changed = before !== after;
  return (
    <div className="grid grid-cols-[60px_1fr_auto_1fr] items-center gap-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "truncate rounded border px-1.5 py-0.5",
          changed && "border-muted-foreground/30 bg-muted/40 line-through opacity-70",
        )}
      >
        {before}
      </div>
      <ChevronRight className="h-3 w-3 text-muted-foreground" />
      <div
        className={cn(
          "truncate rounded border px-1.5 py-0.5",
          changed
            ? "border-amber-400/60 bg-amber-100/60 font-medium dark:bg-amber-900/30"
            : "text-muted-foreground",
        )}
      >
        {after}
      </div>
    </div>
  );
}

function Pill({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "warn";
}) {
  return (
    <div className="rounded border px-1.5 py-1">
      <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div
        className={cn(
          "text-xs font-semibold tabular-nums",
          tone === "good" && "text-emerald-600",
          tone === "warn" && "text-amber-600",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function Composer(props: {
  disabled: boolean;
  onSend: (content: string) => void;
  onCancel?: () => void;
}) {
  const { disabled, onSend, onCancel } = props;
  const [draft, setDraft] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const trimmed = draft.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setDraft("");
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="border-t bg-background p-3"
    >
      <div className="flex items-end gap-2 rounded-xl border bg-background px-2 py-1.5 focus-within:ring-2 focus-within:ring-ring/40">
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter inserts newline (standard chat UX).
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Ask anything about the BHE Data Catalog…"
          rows={1}
          className="flex-1 resize-none bg-transparent px-1 py-1 text-sm outline-none placeholder:text-muted-foreground"
          style={{ maxHeight: "8rem" }}
          disabled={disabled}
        />
        {onCancel ? (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            onClick={onCancel}
            title="Stop generating"
          >
            <X className="h-4 w-4" />
          </Button>
        ) : (
          <Button
            type="submit"
            size="icon"
            disabled={disabled || draft.trim().length === 0}
            title="Send"
          >
            <SendHorizontal className="h-4 w-4" />
          </Button>
        )}
      </div>
      <p className="mt-1.5 px-1 text-[10px] text-muted-foreground">
        Enter to send · Shift+Enter for newline
      </p>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}
