import { useState, useRef, useEffect, useCallback } from "react";
import { MessageSquare, SendHorizontal as SendHorizonal, Mic, MicOff, Trash2, Copy, Check } from "lucide-react";
import AudioControls, { useAudioSpeech } from "./AudioControls";
import { API_BASE } from "../config/api";

function buildStarterQuestions(stack, repoName) {
  const domain = stack?.domain ?? "this domain";
  const pattern = stack?.stack_pattern ?? "this pattern";
  const lang = stack?.primary_language ?? "the primary language";
  return [
    `Why was ${domain} chosen as the domain?`,
    `What does "${pattern}" mean for this repo?`,
    "Which techs were AI-inferred and why?",
    `What is the confidence score for ${lang}?`,
    "What are the notable tech combinations?",
    "What patterns did StackSniffer not detect?",
  ];
}

function useSpeechInput(onResult) {
  const [listening, setListening] = useState(false);
  const recRef = useRef(null);
  const supported =
    typeof window !== "undefined" &&
    ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

  const toggle = useCallback(() => {
    if (!supported) return;
    if (listening) {
      recRef.current?.stop();
      setListening(false);
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = (e) => onResult(e.results[0][0].transcript);
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    recRef.current = rec;
    rec.start();
    setListening(true);
  }, [listening, supported, onResult]);

  return { listening, toggle, supported };
}

function MessageBubble({ msg, audioProps }) {
  const [copied, setCopied] = useState(false);
  const isUser = msg.role === "user";

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(msg.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {}
  }

  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      <div
        className={`shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-mono font-bold mt-1 ${
          isUser
            ? "bg-accent/20 text-accent border border-accent/30"
            : "bg-surface border border-border text-muted"
        }`}
      >
        {isUser ? "U" : "AI"}
      </div>

      <div className={`flex-1 min-w-0 ${isUser ? "items-end" : "items-start"} flex flex-col gap-1`}>
        <div
          className={`px-3 py-2 rounded-lg text-sm leading-relaxed font-sans max-w-[88%] ${
            isUser
              ? "bg-accent/15 text-text border border-accent/20 self-end"
              : "bg-surface text-text border border-border self-start"
          }`}
        >
          {msg.content}
          {msg.streaming && (
            <span className="inline-block w-1.5 h-3.5 bg-accent/70 ml-1 animate-pulse rounded-sm align-middle" />
          )}
        </div>

        {!isUser && !msg.streaming && msg.content && (
          <div className="self-start flex items-center gap-1 pl-0.5">
            <AudioControls
              msgId={msg.id}
              text={msg.content}
              {...audioProps}
            />
            <button
              onClick={handleCopy}
              className="flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono text-muted border border-transparent hover:border-border hover:text-text transition-colors"
              title="Copy response"
            >
              {copied ? <Check size={10} className="text-green" /> : <Copy size={10} />}
              {copied ? "copied" : "copy"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default function ChatPanel({ analysisId, stack, repoName }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState(null);

  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const abortRef = useRef(null);

  const { speak, stop, speaking, speakingId } = useAudioSpeech();
  const audioProps = { speaking, speakingId, onSpeak: speak, onStop: stop };

  const starters = buildStarterQuestions(stack, repoName);

  const speechInput = useSpeechInput((transcript) => {
    setInput((prev) => (prev ? `${prev} ${transcript}` : transcript));
  });

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 100);
  }, [open]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function sendMessage(text) {
    const content = (text ?? input).trim();
    if (!content || isStreaming) return;
    setInput("");
    setError(null);

    const userMsg = { id: Date.now(), role: "user", content };
    const asstId = Date.now() + 1;
    setMessages((prev) => [
      ...prev,
      userMsg,
      { id: asstId, role: "assistant", content: "", streaming: true },
    ]);
    setIsStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = await fetch(`${API_BASE}/api/chat/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          analysis_id: analysisId,
          session_id: sessionId,
          message: content,
        }),
      });

      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `Request failed (${res.status})`);
      }

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let full = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const raw = dec.decode(value, { stream: true });
        for (const line of raw.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            const parsed = JSON.parse(payload);
            if (parsed.chunk) {
              full += parsed.chunk;
              setMessages((prev) =>
                prev.map((m) => (m.id === asstId ? { ...m, content: full } : m))
              );
            }
            if (parsed.session_id && !sessionId) setSessionId(parsed.session_id);
          } catch {}
        }
      }

      setMessages((prev) =>
        prev.map((m) => (m.id === asstId ? { ...m, streaming: false } : m))
      );
    } catch (err) {
      if (err.name === "AbortError") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === asstId ? { ...m, content: "[Cancelled]", streaming: false } : m
          )
        );
      } else {
        setMessages((prev) => prev.filter((m) => m.id !== asstId));
        setError(err.message);
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
    }
  }

  function clearHistory() {
    abortRef.current?.abort();
    stop();
    setMessages([]);
    setSessionId(null);
    setError(null);
    if (sessionId) {
      fetch(`${API_BASE}/api/chat/session/${sessionId}`, { method: "DELETE" }).catch(() => {});
    }
  }

  function handleSubmit(e) {
    e.preventDefault();
    sendMessage();
  }

  return (
    <div className="bg-surface border border-border rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-5 py-3.5 hover:bg-bg/50 transition-colors"
      >
        <div className="flex items-center gap-2.5">
          <MessageSquare size={15} className="text-accent shrink-0" />
          <span className="text-sm font-semibold text-text">
            Chat about{repoName ? ` ${repoName}` : " this repo"}
          </span>
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-accent/15 text-accent border border-accent/25">
            gemini-2.0-flash
          </span>
        </div>
        <div className="flex items-center gap-2">
          {messages.length > 0 && (
            <span className="text-[11px] font-mono text-muted">
              {Math.floor(messages.length / 2)} exchange{messages.length > 2 ? "s" : ""}
            </span>
          )}
          <span className="text-muted text-sm">{open ? "↑" : "↓"}</span>
        </div>
      </button>

      {open && (
        <div className="border-t border-border flex flex-col" style={{ minHeight: "400px" }}>
          {messages.length > 0 && (
            <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-bg/30">
              <span className="text-[11px] font-mono text-muted">
                Grounded in StackSniffer analysis · not a general coding chatbot
              </span>
              <button
                onClick={clearHistory}
                className="flex items-center gap-1 text-[11px] font-mono text-muted hover:text-red-400 transition-colors"
              >
                <Trash2 size={11} />
                clear
              </button>
            </div>
          )}

          <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4" style={{ maxHeight: "380px" }}>
            {messages.length === 0 && (
              <div className="space-y-3">
                <p className="text-xs text-muted font-sans text-center">
                  Ask anything about this repository's tech stack, architecture, or detection results.
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {starters.map((q) => (
                    <button
                      key={q}
                      onClick={() => sendMessage(q)}
                      className="text-left text-xs font-sans text-muted px-3 py-2.5 bg-bg border border-border rounded-lg hover:border-accent/35 hover:text-text transition-colors leading-snug"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((msg) => (
              <MessageBubble key={msg.id} msg={msg} audioProps={audioProps} />
            ))}

            {error && (
              <div className="flex items-start gap-2 px-3 py-2.5 bg-red-500/10 border border-red-500/25 rounded text-red-400 text-xs font-sans">
                <span className="shrink-0 font-bold mt-0.5">!</span>
                <span>{error}</span>
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          <div className="px-4 py-3 border-t border-border bg-bg/20">
            <form onSubmit={handleSubmit} className="flex gap-2">
              <div className="flex-1 flex items-center bg-bg border border-border rounded-lg focus-within:border-accent/50 transition-colors overflow-hidden">
                <input
                  ref={inputRef}
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  disabled={isStreaming}
                  placeholder="Ask about this stack…"
                  className="flex-1 bg-transparent font-sans text-sm text-text placeholder-muted px-3 py-2.5 outline-none disabled:opacity-50 min-w-0"
                />
                {speechInput.supported && (
                  <button
                    type="button"
                    onClick={speechInput.toggle}
                    title={speechInput.listening ? "Stop listening" : "Speak your question"}
                    className={`px-2.5 py-2 transition-colors shrink-0 ${
                      speechInput.listening
                        ? "text-red-400 animate-pulse"
                        : "text-muted hover:text-text"
                    }`}
                  >
                    {speechInput.listening ? <MicOff size={14} /> : <Mic size={14} />}
                  </button>
                )}
              </div>
              <button
                type="submit"
                disabled={!input.trim() || isStreaming}
                className="px-3 py-2.5 bg-accent text-bg rounded-lg hover:bg-accent/90 active:scale-95 transition-all disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
              >
                <SendHorizonal size={15} />
              </button>
            </form>
            <p className="mt-1.5 text-[10px] font-mono text-muted text-center">
              Powered by Gemini 2.0 Flash · Grounded in StackSniffer analysis
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
