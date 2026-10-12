"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { sendChat } from "@/lib/api";
import type { Message } from "@/lib/types";
import MessageBubble from "./MessageBubble";

const EXAMPLE_QUESTIONS = [
  "MCCIA च्या स्थापनेची माहिती द्या",
  "What were the key industries in Pune in 1960?",
  "MCCIA membership benefits",
];

const MAX_INPUT_HEIGHT_PX = 160;

// crypto.randomUUID() only exists in secure contexts, and this app may be served
// over plain HTTP on the office network.
const newId = () =>
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

function describeError(err: unknown): string {
  if (err instanceof TypeError) {
    // fetch() rejects with a TypeError when the server cannot be reached.
    return "Could not reach the search server. Please check that the backend is running and try again.";
  }
  return err instanceof Error && err.message
    ? err.message
    : "Something went wrong. Please try again.";
}

function EmptyState({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6 py-10 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary">
        <svg
          aria-hidden="true"
          viewBox="0 0 32 32"
          fill="none"
          className="h-8 w-8"
        >
          <circle cx="14" cy="14" r="7" stroke="white" strokeWidth="2" />
          <path d="M19.5 19.5 25 25" stroke="white" strokeWidth="2" strokeLinecap="round" />
          <path
            d="M24 3l1.2 3.8L29 8l-3.8 1.2L24 13l-1.2-3.8L19 8l3.8-1.2L24 3z"
            fill="#E8871A"
          />
        </svg>
      </div>
      <h1 className="mt-5 text-2xl font-semibold text-primary">MCCIA Search</h1>
      <p className="mt-2 max-w-md text-base text-muted">
        Ask questions about Sampada magazine and MCCIA documents
      </p>
      <div className="mt-8 flex max-w-3xl flex-wrap justify-center gap-3">
        {EXAMPLE_QUESTIONS.map((question) => (
          <button
            key={question}
            type="button"
            onClick={() => onPick(question)}
            className="rounded-full border border-primary/20 bg-surface px-4 py-2 text-sm text-primary shadow-sm transition hover:border-accent hover:shadow focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          >
            {question}
          </button>
        ))}
      </div>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex" aria-hidden="true">
      <div className="flex items-center gap-1.5 rounded-2xl bg-surface px-4 py-3.5 shadow-sm ring-1 ring-black/5">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-2 w-2 animate-bounce rounded-full bg-primary/60 motion-reduce:animate-none"
            style={{ animationDelay: `${i * 150}ms` }}
          />
        ))}
      </div>
    </div>
  );
}

export default function ChatWindow() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wasLoading = useRef(false);

  // Keep the newest message (or the typing indicator) in view.
  useEffect(() => {
    const reduceMotion = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    bottomRef.current?.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "end",
    });
  }, [messages, loading]);

  // Grow the textarea with its content, up to a limit.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_HEIGHT_PX)}px`;
  }, [input]);

  // The textarea is disabled while waiting, which drops focus; restore it after.
  useEffect(() => {
    if (wasLoading.current && !loading) inputRef.current?.focus();
    wasLoading.current = loading;
  }, [loading]);

  const submit = async () => {
    const query = input.trim();
    if (!query || loading) return;

    const userMessage: Message = {
      id: newId(),
      role: "user",
      content: query,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setLoading(true);

    // Build the reply before touching state: a state updater that throws while
    // React renders would take the whole page down instead of showing an error.
    let reply: Message;
    try {
      const response = await sendChat(query);
      if (typeof response?.answer !== "string") {
        throw new Error("The server sent an unexpected response.");
      }
      reply = {
        id: newId(),
        role: "assistant",
        content: response.answer,
        sources: response.sources,
        found: response.found,
        timestamp: new Date(),
      };
    } catch (err) {
      reply = {
        id: newId(),
        role: "assistant",
        content: `Sorry, I couldn't get an answer. ${describeError(err)}`,
        timestamp: new Date(),
      };
    }
    setMessages((prev) => [...prev, reply]);
    setLoading(false);
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter adds a line. Enter while an input method editor is
    // composing text (common for Marathi/Hindi) just confirms the composition.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  };

  const pickExample = (question: string) => {
    setInput(question);
    inputRef.current?.focus();
  };

  return (
    <section className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <EmptyState onPick={pickExample} />
        ) : (
          <div
            role="log"
            aria-label="Conversation"
            className="mx-auto flex max-w-4xl flex-col gap-5 px-4 py-6 md:px-8"
          >
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
            {loading && <TypingIndicator />}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="px-4 pb-4 pt-2 md:px-8">
        <form
          onSubmit={handleSubmit}
          className="mx-auto flex max-w-3xl items-end gap-2 rounded-2xl bg-surface p-2 shadow-lg shadow-primary/10 ring-1 ring-black/5 focus-within:ring-primary/30"
        >
          <textarea
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading}
            rows={1}
            placeholder="Ask anything about MCCIA documents..."
            aria-label="Your question"
            className="min-h-[2.5rem] flex-1 resize-none bg-transparent px-3 py-2 text-base leading-relaxed text-ink placeholder:text-muted focus:outline-none disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={loading || !input.trim()}
            aria-label="Send question"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent text-ink transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-40"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-5 w-5"
            >
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          </button>
        </form>
        <p
          role="status"
          className="mx-auto mt-1.5 h-4 max-w-3xl text-center text-xs text-muted"
        >
          {loading ? "Searching documents..." : ""}
        </p>
      </div>
    </section>
  );
}
