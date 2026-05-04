/**
 * Chatbot (Track A) — frontend client + streaming hook.
 *
 * The streaming endpoint returns Server-Sent Events. We can't use axios for
 * that (axios buffers the whole response by default), so streaming uses the
 * Fetch API directly. Non-streaming endpoints reuse the shared axios client
 * for consistency with the rest of the app.
 */
import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatConversation {
  conversation_id: string;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  last_message_at: string | null;
  message_count: number;
}

/** Citation entry — turns into a clickable chip below the assistant message. */
export interface Citation {
  label: string;
  deeplink: string;
}

/**
 * Structured payload of a single tool call. Used both:
 *   - inline during streaming (one card appears per tool the model fires)
 *   - on conversation reload (we rehydrate the same card from chat_messages.parts)
 *
 * `data` is whatever the tool returned (typed catalog rows for app_*, Genie
 * rows + SQL for genie_ask). The card renders different layouts based on
 * the `name` field so each tool can have a tailored summary view.
 */
export interface ToolInvocation {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  status: "pending" | "ok" | "error";
  summary?: string;
  data?: unknown;
  citations?: Citation[];
  chart_spec?: Record<string, unknown> | null;
}

/** A single structured part of a persisted message (parsed from chat_messages.parts JSON). */
export type MessagePart =
  | { type: "text"; text: string }
  | {
      type: "tool_call";
      tool_call_id: string;
      name: string;
      args_json: string;
    }
  | {
      type: "tool_result";
      tool_call_id: string;
      name: string;
      args: Record<string, unknown>;
      ok: boolean;
      summary: string;
      data: unknown;
      citations: Citation[];
      chart_spec: Record<string, unknown> | null;
    };

export interface ChatMessage {
  message_id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  parts: MessagePart[];
  model?: string | null;
  finish_reason?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  latency_ms?: number | null;
  error?: string | null;
  created_at?: string | null;
}

export interface ChatConversationDetail {
  conversation: ChatConversation;
  messages: ChatMessage[];
}

/** Discriminated union mirroring the backend's `chat_router._event` shapes. */
export type ChatStreamEvent =
  | {
      type: "start";
      conversation_id: string;
      user_message_id: string;
      assistant_message_id: string;
      is_new_conversation: boolean;
    }
  | { type: "token"; text: string }
  | {
      type: "tool_call";
      tool_call_id: string;
      name: string;
      args: Record<string, unknown>;
    }
  | {
      type: "tool_result";
      tool_call_id: string;
      name: string;
      ok: boolean;
      summary: string;
      data: unknown;
      citations: Citation[];
      chart_spec: Record<string, unknown> | null;
    }
  | {
      type: "done";
      assistant_message_id: string;
      finish_reason: string;
      prompt_tokens: number;
      completion_tokens: number;
      latency_ms: number;
    }
  | { type: "error"; error: string };

// ---------------------------------------------------------------------------
// REST endpoints
// ---------------------------------------------------------------------------

export async function listConversations(): Promise<ChatConversation[]> {
  const { data } = await api.get<ChatConversation[]>("/chat/conversations");
  return data;
}

export async function getConversation(
  conversationId: string,
): Promise<ChatConversationDetail> {
  const { data } = await api.get<ChatConversationDetail>(
    `/chat/conversations/${encodeURIComponent(conversationId)}`,
  );
  return data;
}

export async function renameConversation(
  conversationId: string,
  title: string,
): Promise<ChatConversation> {
  const { data } = await api.post<ChatConversation>(
    `/chat/conversations/${encodeURIComponent(conversationId)}/title`,
    { title },
  );
  return data;
}

export async function deleteConversation(
  conversationId: string,
): Promise<void> {
  await api.delete(`/chat/conversations/${encodeURIComponent(conversationId)}`);
}

// ---------------------------------------------------------------------------
// Phase A2 — propose/confirm
// ---------------------------------------------------------------------------

/** Result from POST /api/chat/confirm/{token}. */
export interface ConfirmResult {
  ok: boolean;
  intent: string;
  target_id: string;
  result: Record<string, unknown>;
}

/**
 * Consume a single-use confirmation token issued by an `app_propose_*`
 * tool. The backend validates the token (not consumed, not expired,
 * bound to this user) and performs the underlying write in-process.
 *
 * Throws an Error with the server's status text + body on 4xx/5xx so
 * the ConfirmCard can show the failure inline (token expired, already
 * consumed, write failed, etc.).
 */
export async function confirmProposal(token: string): Promise<ConfirmResult> {
  try {
    const { data } = await api.post<ConfirmResult>(
      `/chat/confirm/${encodeURIComponent(token)}`,
    );
    return data;
  } catch (err) {
    if (axios.isAxiosError(err)) {
      const detail =
        (err.response?.data as { detail?: string } | undefined)?.detail ??
        err.response?.statusText ??
        err.message;
      throw new Error(detail);
    }
    throw err;
  }
}

// ---------------------------------------------------------------------------
// SSE streaming
// ---------------------------------------------------------------------------

export interface StreamMessageOptions {
  content: string;
  conversationId?: string;
  signal?: AbortSignal;
  onEvent: (event: ChatStreamEvent) => void;
}

/**
 * POST /chat/messages and dispatch each SSE event to `onEvent`.
 *
 * Returns when the stream closes. Throws on transport errors. Note that
 * model errors arrive as `{type:"error"}` events in-stream, not as exceptions.
 */
export async function streamChatMessage({
  content,
  conversationId,
  signal,
  onEvent,
}: StreamMessageOptions): Promise<void> {
  const response = await fetch("/api/chat/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      content,
      conversation_id: conversationId ?? null,
    }),
    signal,
  });

  if (!response.ok || !response.body) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `Chat request failed: ${response.status} ${response.statusText} ${detail.slice(0, 200)}`,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // Buffer because SSE frames are separated by blank lines and a single
  // network read can split a frame in half.
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Split on the SSE frame delimiter (\n\n). A frame may contain multiple
    // `data:` lines, but our backend always emits exactly one per frame.
    let delimiterIdx;
    while ((delimiterIdx = buffer.indexOf("\n\n")) !== -1) {
      const rawFrame = buffer.slice(0, delimiterIdx);
      buffer = buffer.slice(delimiterIdx + 2);

      for (const line of rawFrame.split("\n")) {
        const trimmed = line.trimStart();
        if (!trimmed.startsWith("data:")) continue;
        const payload = trimmed.slice(5).trim();
        if (!payload) continue;
        try {
          const parsed = JSON.parse(payload) as ChatStreamEvent;
          onEvent(parsed);
        } catch (err) {
          console.warn("chat: unparsable SSE frame", payload, err);
        }
      }
    }
  }
}
