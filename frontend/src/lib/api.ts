export type Model = {
  id: string;
  name?: string;
  provider?: string;
  mode?: "local" | "cloud" | string;
  group?: string;
  description?: string;
  size?: string;
};

export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
};

export type SessionMessage = {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
};

export type ConversationSession = {
  id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  messages: SessionMessage[];
};

export type SessionSummary = {
  id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  message_count: number;
};

type ModelsResponse = {
  models?: Model[];
  default?: string;
  data?: Model[];
};

type ChatResponse = {
  reply: string;
  model: string;
  provider: string;
  session_id: string;
  title: string;
};

const BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");
const CHAT = import.meta.env.VITE_CHAT_ENDPOINT || "/api/chat";
const STREAM_CHAT = import.meta.env.VITE_CHAT_STREAM_ENDPOINT || `${CHAT.replace(/\/+$/, "")}/stream`;
const MODELS = import.meta.env.VITE_MODELS_ENDPOINT || "/api/models";
const HEALTH = import.meta.env.VITE_HEALTH_ENDPOINT || "/api/health";
const SESSIONS = import.meta.env.VITE_SESSIONS_ENDPOINT || "/api/sessions";

async function request(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (typeof body.message === "string") detail = body.message;
    } catch {
      // Keep the HTTP status message if the response isn't JSON.
    }
    throw new Error(detail);
  }

  return response;
}

export async function getModels(): Promise<Model[]> {
  const response = await request(MODELS);
  const data = (await response.json()) as ModelsResponse | Model[];
  const list = Array.isArray(data) ? data : data.models || data.data || [];
  const defaultModel = Array.isArray(data) ? undefined : data.default;

  const models = list.map((item) => ({
    ...item,
    id: item.id,
    name: item.name || item.id,
    mode:
      item.mode ||
      (item.provider?.toLowerCase() === "ollama" ? "local" : "cloud"),
  }));

  if (defaultModel) {
    models.sort((a, b) => {
      if (a.id === defaultModel) return -1;
      if (b.id === defaultModel) return 1;
      return 0;
    });
  }
  return models;
}

export async function getHealth(): Promise<boolean> {
  try {
    await request(HEALTH);
    return true;
  } catch {
    return false;
  }
}

export async function getSessions(): Promise<SessionSummary[]> {
  const response = await request(SESSIONS);
  const data = await response.json();
  return Array.isArray(data.sessions) ? data.sessions : [];
}

export async function getSession(sessionId: string): Promise<ConversationSession> {
  const response = await request(`${SESSIONS}/${encodeURIComponent(sessionId)}`);
  return response.json();
}

export async function deleteSession(sessionId: string): Promise<void> {
  await request(`${SESSIONS}/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

/**
 * Send only the latest user turn. The FastAPI backend loads the rest of the
 * conversation from its file-backed memory using session_id.
 */
export async function sendChat(
  message: string,
  model: string,
  sessionId?: string | null,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const response = await request(CHAT, {
    method: "POST",
    body: JSON.stringify({
      message,
      model,
      ...(sessionId ? { session_id: sessionId } : {}),
    }),
    signal,
  });

  const data = (await response.json()) as ChatResponse;
  if (
    typeof data.reply !== "string" ||
    typeof data.session_id !== "string"
  ) {
    throw new Error("The LLM service returned an incomplete chat response.");
  }
  return data;
}


type ChatStreamEvent =
  | { type: "token"; token: string }
  | {
      type: "done";
      reply: string;
      model: string;
      provider: string;
      session_id: string;
      title: string;
    }
  | { type: "error"; error: string };

/**
 * Streams assistant tokens from FastAPI using Server-Sent Events.
 * onToken is called as soon as each complete word is available to the UI.
 */
export async function sendChatStream(
  message: string,
  model: string,
  sessionId: string | null | undefined,
  onToken: (token: string) => void,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const response = await request(STREAM_CHAT, {
    method: "POST",
    body: JSON.stringify({
      message,
      model,
      ...(sessionId ? { session_id: sessionId } : {}),
    }),
    signal,
    headers: {
      Accept: "text/event-stream",
    },
  });

  if (!response.body) {
    throw new Error("Streaming is not supported by this response.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed: ChatResponse | null = null;

  const consumeLine = (line: string) => {
    if (!line.startsWith("data:")) return;

    const raw = line.slice(5).trim();
    if (!raw) return;

    const event = JSON.parse(raw) as ChatStreamEvent;

    if (event.type === "token") {
      onToken(event.token);
    } else if (event.type === "done") {
      completed = {
        reply: event.reply,
        model: event.model,
        provider: event.provider,
        session_id: event.session_id,
        title: event.title,
      };
    } else if (event.type === "error") {
      throw new Error(event.error || "The streaming request failed.");
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });

    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";

    for (const line of lines) {
      consumeLine(line);
    }

    if (done) break;
  }

  if (buffer.trim()) {
    consumeLine(buffer);
  }

  if (!completed) {
    throw new Error("The stream ended before the assistant completed its response.");
  }

  return completed;
}
