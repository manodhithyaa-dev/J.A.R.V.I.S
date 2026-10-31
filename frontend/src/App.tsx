import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity, AudioLines, Bot, ChevronDown, Command, Cpu, Globe2,
  Headphones, History, Menu, Mic, MicOff, Plus, Radio, Send,
  Settings2, ShieldCheck, Volume2, Wifi, X, Trash2
} from "lucide-react";
import { deleteSession, getHealth, getModels, getSession, getSessions, sendChat, type Model } from "./lib/api";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Message = {
  id: number;
  role: "user" | "assistant";
  content: string;
  time: string;
};

type CoreState = "standby" | "listening" | "thinking" | "speaking";

type SessionSummary = {
  id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  message_count: number;
};

const stamp = () =>
  new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const formatSessionTime = (value?: string) => {
  if (!value) return "";
  const parsed = new Date(value.replace(" ", "T"));
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

export default function App() {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 1,
      role: "assistant",
      content: "Good evening. All systems are standing by. What can I do for you?",
      time: stamp(),
    },
  ]);
  const [input, setInput] = useState("");
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [core, setCore] = useState<CoreState>("standby");
  const [online, setOnline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [voiceEnabled, setVoiceEnabled] = useState(true);
  const [listening, setListening] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionTitle, setSessionTitle] = useState("New Conversation");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [showHistory, setShowHistory] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const recognitionRef = useRef<any>(null);

  const selected = models.find((item) => item.id === model);
  const localCount = models.filter((item) => item.mode === "local").length;
  const cloudCount = models.filter((item) => item.mode !== "local").length;

  const refreshSessions = async () => {
    try {
      setSessions(await getSessions());
    } catch (e) {
      console.error("Could not load conversation history:", e);
    }
  };

  const loadSession = async (id: string) => {
    try {
      const session = await getSession(id);
      const restored: Message[] = (session.messages || []).map((item, index) => ({
        id: index + 1,
        role: item.role,
        content: item.content,
        time: formatSessionTime(item.timestamp),
      }));

      setSessionId(session.id);
      setSessionTitle(session.title || "Conversation");
      setMessages(restored.length ? restored : [{
        id: Date.now(),
        role: "assistant",
        content: "This conversation is ready. What would you like to continue?",
        time: stamp(),
      }]);
      setShowHistory(false);
      setSidebarOpen(false);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load this conversation.");
    }
  };

  const removeSession = async (id: string) => {
    try {
      await deleteSession(id);
      if (sessionId === id) newChat();
      await refreshSessions();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete this conversation.");
    }
  };

  // Load the real model list and default model from the FastAPI service.
  useEffect(() => {
    let mounted = true;

    const loadModels = async () => {
      try {
        const items = await getModels();
        if (!mounted) return;

        setModels(items);
        setModel(items[0]?.id || "");

        if (items.length === 0) {
          setError("The LLM backend is online but returned no chat models.");
        }
      } catch (e) {
        if (!mounted) return;
        const message = e instanceof Error ? e.message : "Unable to load models.";
        setError(`Could not load models from the LLM backend: ${message}`);
      }
    };

    const checkHealth = () => {
      getHealth().then((status) => {
        if (mounted) setOnline(status);
      });
    };

    void loadModels();
    void refreshSessions();
    checkHealth();

    const timer = window.setInterval(checkHealth, 15000);

    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  useEffect(
    () => () => {
      window.speechSynthesis?.cancel();
      recognitionRef.current?.stop?.();
    },
    [],
  );

  const speak = (text: string) => {
    if (!voiceEnabled || !("speechSynthesis" in window)) return;

    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1;
    utterance.pitch = 0.92;
    utterance.onstart = () => setCore("speaking");
    utterance.onend = () => setCore("standby");

    window.speechSynthesis.speak(utterance);
  };

  const submit = async (value = input) => {
    const content = value.trim();

    if (!content || busy || !model) return;

    setError("");
    setInput("");
    setBusy(true);
    setCore("thinking");

    const next: Message[] = [
      ...messages,
      {
        id: Date.now(),
        role: "user",
        content,
        time: stamp(),
      },
    ];

    setMessages(next);

    try {
      // Send only the new message. The backend restores prior turns from disk.
      const result = await sendChat(content, model, sessionId);

      setSessionId(result.session_id);
      setSessionTitle(result.title || "Conversation");

      setMessages((previous) => [
        ...previous,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: result.reply,
          time: stamp(),
        },
      ]);

      void refreshSessions();
      setCore("standby");
      speak(result.reply);
    } catch (e) {
      const message =
        e instanceof Error ? e.message : "Unknown connection error";

      setError(`Backend request failed: ${message}`);
      setCore("standby");
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  const toggleMic = () => {
    const SpeechRecognition =
      (window as any).SpeechRecognition ||
      (window as any).webkitSpeechRecognition;

    if (!SpeechRecognition) {
      setError(
        "Speech recognition is not supported in this browser. Try Chrome or use text input.",
      );
      return;
    }

    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
      setCore("standby");
      return;
    }

    const recognition = new SpeechRecognition();
    recognition.lang = "en-IN";
    recognition.interimResults = true;
    recognition.continuous = false;

    recognition.onstart = () => {
      setListening(true);
      setCore("listening");
    };

    recognition.onresult = (event: any) => {
      let transcript = "";

      for (let i = event.resultIndex; i < event.results.length; i++) {
        transcript += event.results[i][0].transcript;
      }

      setInput(transcript);

      if (event.results[event.results.length - 1].isFinal) {
        setListening(false);
        setCore("standby");
      }
    };

    recognition.onerror = () => {
      setListening(false);
      setCore("standby");
      setError("Microphone recognition failed. Check browser permissions.");
    };

    recognition.onend = () => {
      setListening(false);
      setCore((state) =>
        state === "listening" ? "standby" : state,
      );
    };

    recognitionRef.current = recognition;
    recognition.start();
  };

  const newChat = () => {
    window.speechSynthesis?.cancel();
    setSessionId(null);
    setSessionTitle("New Conversation");
    setShowHistory(false);

    setMessages([
      {
        id: Date.now(),
        role: "assistant",
        content: "New session initialized. I'm listening.",
        time: stamp(),
      },
    ]);

    setError("");
    setSidebarOpen(false);
  };

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark"><Cpu size={20} /></div>
          <div>
            <strong>J.A.R.V.I.S.</strong>
            <small>PERSONAL INTELLIGENCE</small>
          </div>
          <button className="icon-btn mobile-close" onClick={() => setSidebarOpen(false)}>
            <X size={18} />
          </button>
        </div>

        <button className="new-chat" onClick={newChat}>
          <Plus size={17} /> New conversation <span>⌘ K</span>
        </button>

        <div className="side-label">WORKSPACE</div>
        <button className="side-link active"><Radio size={16} /> Command interface</button>
        <button className="side-link" onClick={() => setShowSettings(true)}>
          <Settings2 size={16} /> Preferences
        </button>
        <button
          className={`side-link ${showHistory ? "active" : ""}`}
          onClick={() => {
            setShowHistory((value) => !value);
            void refreshSessions();
          }}
        >
          <History size={16} /> Session history
        </button>

        {showHistory && (
          <div className="history-list">
            <div className="history-heading">
              <span>RECENT CONVERSATIONS</span>
              <button className="history-refresh" onClick={() => void refreshSessions()} title="Refresh history">↻</button>
            </div>
            {sessions.length ? sessions.map((session) => (
              <div className={`history-item ${sessionId === session.id ? "selected" : ""}`} key={session.id}>
                <button className="history-open" onClick={() => void loadSession(session.id)}>
                  <b>{session.title || "Untitled conversation"}</b>
                  <small>{session.message_count} messages</small>
                </button>
                <button
                  className="history-delete"
                  title="Delete conversation"
                  onClick={() => {
                    if (window.confirm("Delete this conversation permanently?")) {
                      void removeSession(session.id);
                    }
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            )) : <p className="history-empty">No saved conversations yet.</p>}
          </div>
        )}

        <div className="side-bottom">
          <div className="secure-card">
            <ShieldCheck size={17} />
            <div>
              <b>Private session</b>
              <small>Requests route through your backend</small>
            </div>
          </div>
          <div className="profile">
            <div className="avatar">M</div>
            <div><b>Operator</b><small>Administrator</small></div>
            <button className="icon-btn" onClick={() => setShowSettings(true)}>
              <Settings2 size={17} />
            </button>
          </div>
        </div>
      </aside>

      {sidebarOpen && <div className="scrim" onClick={() => setSidebarOpen(false)} />}

      <main className="main">
        <header className="topbar">
          <div className="top-left">
            <button className="icon-btn mobile-menu" onClick={() => setSidebarOpen(true)}>
              <Menu size={19} />
            </button>
            <div className="breadcrumb">SYSTEM <span>/</span> <b>COMMAND INTERFACE</b></div>
          </div>
          <div className="top-actions">
            <div className={`connection ${online ? "online" : ""}`}>
              <i />{online ? "BACKEND CONNECTED" : "BACKEND UNREACHABLE"}
            </div>
            <button className="icon-btn" title="Preferences" onClick={() => setShowSettings(true)}>
              <Settings2 size={18} />
            </button>
          </div>
        </header>

        <div className="dashboard">
          <section className="hero">
            <div className="hero-copy">
              <div className="eyebrow">
                <span className="eyebrow-line" /> PERSONAL AI SYSTEM <span className="eyebrow-line" />
              </div>
              <h1>At your command<span>.</span></h1>
              <p>Your intelligent interface is ready. Ask, explore, build — I'll handle the rest.</p>
            </div>

            <div className="core-wrap">
              <div className={`core-orb ${core}`}>
                <div className="orb-ring ring-one" />
                <div className="orb-ring ring-two" />
                <div className="orb-ring ring-three" />
                <div className="orb-grid" />
                <div className="orb-center">
                  <div className="orb-eye"><span /><span /><span /></div>
                  <small>{core === "standby" ? "J.A.R.V.I.S." : core.toUpperCase()}</small>
                </div>
                <div className="orbit-dot dot-a" />
                <div className="orbit-dot dot-b" />
                <div className="orbit-dot dot-c" />
              </div>
              <div className="core-caption">
                <span className="pulse-dot" />
                {core === "standby" ? "CORE STANDBY" : `CORE ${core.toUpperCase()}`}
              </div>
            </div>

            <div className="hero-foot">
              <span><Activity size={14} /> SYSTEM NOMINAL</span>
              <span>BACKEND SESSION <i /></span>
            </div>
          </section>

          <section className="work-area">
            <div className="chat-panel">
              <div className="panel-heading">
                <div><span className="panel-kicker">LIVE SESSION</span><h2>{sessionTitle}</h2></div>
                <button className="text-btn" onClick={newChat}><Plus size={15} /> New chat</button>
              </div>

              <div className="messages">
                <div className="date-divider"><span /> SESSION STARTED <span /></div>

                {messages.map((message) => (
                  <motion.div
                    key={message.id}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    className={`message-row ${message.role}`}
                  >
                    <div className={`message-avatar ${message.role}`}>
                      {message.role === "assistant" ? <Bot size={17} /> : "M"}
                    </div>
                    <div className="message-content">
                      <div className="message-meta">
                        {message.role === "assistant" ? "J.A.R.V.I.S." : "YOU"}
                        <time>{message.time}</time>
                      </div>
                      <div className="bubble">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {message.content}
                        </ReactMarkdown>
                      </div>
                      {message.role === "assistant" && (
                        <button className="speak-btn" onClick={() => speak(message.content)}>
                          <Volume2 size={13} /> Play response
                        </button>
                      )}
                    </div>
                  </motion.div>
                ))}

                {busy && (
                  <div className="message-row assistant">
                    <div className="message-avatar assistant"><Bot size={17} /></div>
                    <div className="message-content">
                      <div className="message-meta">J.A.R.V.I.S. <time>PROCESSING</time></div>
                      <div className="typing"><i /><i /><i /> <span>Thinking through your request</span></div>
                    </div>
                  </div>
                )}
                <div ref={bottomRef} />
              </div>

              {error && (
                <div className="error-banner">
                  <span>{error}</span>
                  <button onClick={() => setError("")}><X size={14} /></button>
                </div>
              )}

              <div className="composer">
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void submit();
                    }
                  }}
                  placeholder="Message J.A.R.V.I.S. — ask anything..."
                  rows={1}
                />
                <div className="composer-tools">
                  <div className="composer-hint">
                    <Command size={12} /> ENTER TO SEND <span>·</span> SHIFT + ENTER FOR NEW LINE
                  </div>
                  <div className="composer-actions">
                    <button
                      className={`round-tool ${listening ? "recording" : ""}`}
                      onClick={toggleMic}
                      title={listening ? "Stop listening" : "Voice input"}
                    >
                      {listening ? <MicOff size={17} /> : <Mic size={17} />}
                    </button>
                    <button className="send-btn" disabled={!input.trim() || busy || !model} onClick={() => void submit()}>
                      <Send size={16} />
                    </button>
                  </div>
                </div>
              </div>

              <div className="disclaimer">J.A.R.V.I.S. can make mistakes. Verify important information.</div>
            </div>

            <aside className="right-rail">
              <div className="rail-card model-card">
                <div className="rail-title"><span>ACTIVE MODEL</span><Cpu size={16} /></div>

                <button className="model-select" onClick={() => setMenuOpen((value) => !value)}>
                  <div className={`model-icon ${selected?.mode === "local" ? "local" : "cloud"}`}>
                    {selected?.mode === "local" ? <Cpu size={17} /> : <Globe2 size={17} />}
                  </div>
                  <div className="model-name">
                    <b>{selected?.name || "Select a model"}</b>
                    <small>{selected?.provider || selected?.mode || "MODEL ROUTING"}</small>
                  </div>
                  <ChevronDown size={16} />
                </button>

                <AnimatePresence>
                  {menuOpen && (
                    <motion.div
                      initial={{ opacity: 0, y: -5 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -5 }}
                      className="model-menu"
                    >
                      <div className="menu-label">AVAILABLE MODELS</div>
                      {models.map((item) => (
                        <button
                          key={item.id}
                          className={`model-option ${model === item.id ? "chosen" : ""}`}
                          onClick={() => {
                            setModel(item.id);
                            setMenuOpen(false);
                          }}
                        >
                          <span className={`tiny-status ${item.mode === "local" ? "local" : "cloud"}`} />
                          <span>
                            <b>{item.name || item.id}</b>
                            <small>{item.provider || item.mode || "Model"}</small>
                          </span>
                          {model === item.id && <span className="check">✓</span>}
                        </button>
                      ))}
                      {!models.length && <p className="empty-models">No models returned by API.</p>}
                    </motion.div>
                  )}
                </AnimatePresence>

                <div className="model-stats">
                  <div><span>LOCAL</span><b>{localCount.toString().padStart(2, "0")}</b></div>
                  <div><span>CLOUD</span><b>{cloudCount.toString().padStart(2, "0")}</b></div>
                  <div><span>ROUTER</span><b>LiteLLM</b></div>
                </div>
              </div>

              <div className="rail-card">
                <div className="rail-title"><span>VOICE INTERFACE</span><AudioLines size={16} /></div>
                <div className="voice-row">
                  <div className="voice-icon"><Headphones size={18} /></div>
                  <div>
                    <b>Spoken responses</b>
                    <small>{voiceEnabled ? "Voice output enabled" : "Text-only output"}</small>
                  </div>
                  <button
                    className={`toggle ${voiceEnabled ? "on" : ""}`}
                    onClick={() => {
                      setVoiceEnabled((value) => !value);
                      window.speechSynthesis?.cancel();
                    }}
                  >
                    <i />
                  </button>
                </div>
                <div className="voice-note"><Mic size={13} /> Use the microphone to speak to J.A.R.V.I.S.</div>
              </div>

              <div className="rail-card status-card">
                <div className="rail-title"><span>SYSTEM OVERVIEW</span><Activity size={16} /></div>
                <div className="status-line">
                  <span><i className={online ? "good" : "bad"} /> API CONNECTION</span>
                  <b>{online ? "ONLINE" : "OFFLINE"}</b>
                </div>
                <div className="status-line"><span><i className="good" /> MODEL ROUTER</span><b>LiteLLM</b></div>
                <div className="status-line">
                  <span><i className="good" /> VOICE ENGINE</span>
                  <b>{"speechSynthesis" in window ? "READY" : "N/A"}</b>
                </div>
                <div className="status-line">
                  <span><i className={listening ? "warn" : "good"} /> MICROPHONE</span>
                  <b>{listening ? "LISTENING" : "STANDBY"}</b>
                </div>
              </div>

              <div className="rail-footer"><Wifi size={13} /> PRIVATE INSTANCE <span>·</span> V1.0.0</div>
            </aside>
          </section>
        </div>
      </main>

      <AnimatePresence>
        {showSettings && (
          <motion.div
            className="modal-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setShowSettings(false)}
          >
            <motion.div
              className="settings-modal"
              initial={{ y: 12, opacity: 0 }}
              animate={{ y: 0, opacity: 1 }}
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-head">
                <div><span className="panel-kicker">CONFIGURATION</span><h2>Preferences</h2></div>
                <button className="icon-btn" onClick={() => setShowSettings(false)}><X size={18} /></button>
              </div>

              <div className="setting-item">
                <div>
                  <b>Spoken responses</b>
                  <small>Read assistant replies aloud using browser speech synthesis.</small>
                </div>
                <button className={`toggle ${voiceEnabled ? "on" : ""}`} onClick={() => setVoiceEnabled((value) => !value)}>
                  <i />
                </button>
              </div>

              <div className="setting-item">
                <div>
                  <b>Backend base URL</b>
                  <small>Configured using VITE_API_BASE_URL in your .env file.</small>
                </div>
                <code>VITE_API_BASE_URL</code>
              </div>

              <div className="setting-item">
                <div>
                  <b>Model routing</b>
                  <small>Model list is loaded from your FastAPI LLM service.</small>
                </div>
                <code>{import.meta.env.VITE_MODELS_ENDPOINT || "/api/models"}</code>
              </div>

              <p className="settings-footnote">
                API keys and provider credentials should remain on your backend, never in this frontend.
              </p>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
