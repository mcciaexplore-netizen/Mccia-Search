"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { SessionExpiredError, sendChat } from "@/lib/api";
import { historyOf } from "@/lib/conversation";
import { cleanPhotos } from "@/lib/photos";
import { loadDraft, saveDraft, startRelaunch, wasRelaunched } from "@/lib/draft";
import type { Message } from "@/lib/types";
import MessageBubble from "./MessageBubble";

const WELCOME_HEADING = "How can I help you explore MCCIA?";
const WELCOME_SUBTITLE = "Search through MCCIA\u2019s documents, history and archives.";

type PromptIcon = "photo" | "person" | "book" | "building";

// Picking one puts it in the question box (it is not sent), so it can be changed first.
const SUGGESTED_PROMPTS: { text: string; icon: PromptIcon }[] = [
  { text: "Find historical MCCIA photographs", icon: "photo" },
  { text: "Search for information about past presidents", icon: "person" },
  { text: "Explore MCCIA\u2019s historical publications", icon: "book" },
  { text: "Find information about a specific company", icon: "building" },
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

const ICON_PATHS: Record<PromptIcon, string> = {
  photo: "M3 7a2 2 0 012-2h2.5l1.2-1.6A1 1 0 019.5 3h5a1 1 0 01.8.4L16.5 5H19a2 2 0 012 2v11a2 2 0 01-2 2H5a2 2 0 01-2-2V7zm9 10a4 4 0 100-8 4 4 0 000 8z",
  person: "M16 19v-1a4 4 0 00-4-4H8a4 4 0 00-4 4v1M10 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM20 19v-1a4 4 0 00-3-3.87M15.5 4.2a3.5 3.5 0 010 6.6",
  book: "M4 5.5A2.5 2.5 0 016.5 3H20v15H6.5A2.5 2.5 0 004 20.5v-15zM4 20.5A2.5 2.5 0 006.5 23H20v-5",
  building: "M4 21V5a2 2 0 012-2h7a2 2 0 012 2v16M15 9h3a2 2 0 012 2v10M3 21h18M8 7.5h3M8 11.5h3M8 15.5h3",
};

function EmptyState({ onPick }: { onPick: (question: string) => void }) {
  return (
    <div className="mx-auto flex min-h-full w-full max-w-[760px] animate-fade-in flex-col items-center justify-center px-5 pb-40 pt-10 text-center">
      <h1 className="text-balance text-3xl font-semibold tracking-tight text-primary md:text-4xl">{WELCOME_HEADING}</h1>
      <p className="mt-3 max-w-md text-base leading-relaxed text-muted md:text-lg">{WELCOME_SUBTITLE}</p>
      <ul className="mt-9 grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
        {SUGGESTED_PROMPTS.map(({ text, icon }) => (
          <li key={text}>
            <button
              type="button"
              onClick={() => onPick(text)}
              className="group flex w-full items-center gap-3 rounded-2xl bg-white/80 p-4 text-left ring-1 ring-black/[0.06] backdrop-blur transition duration-200 ease-out hover:-translate-y-0.5 hover:bg-white hover:shadow-float hover:ring-accent/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent motion-reduce:hover:translate-y-0"
            >
              <span aria-hidden="true" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                  <path d={ICON_PATHS[icon]} />
                </svg>
              </span>
              <span className="text-[15px] font-medium leading-snug text-primary">{text}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="flex animate-message-in" aria-hidden="true">
      <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-md bg-surface px-4 py-3.5 ring-1 ring-black/[0.06]">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-2 w-2 animate-pulse-dot rounded-full bg-accent motion-reduce:animate-none"
            style={{ animationDelay: `${i * 160}ms` }}
          />
        ))}
      </div>
    </div>
  );
}

interface ChatWindowProps {
  /** Messages to start from, when a saved chat is opened. Read once, when the window mounts. */
  initialMessages?: Message[];
  /** Called with the full list after every message, so the chat can be saved. */
  onMessagesChange?: (messages: Message[]) => void;
}

export default function ChatWindow({ initialMessages = [], onMessagesChange }: ChatWindowProps) {
  const [messages, setMessages] = useState<Message[]>(initialMessages);
  // The latest list, readable from the async code below even after this window is gone.
  const messagesRef = useRef<Message[]>(initialMessages);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  // The question being typed survives a reload (see lib/draft.ts), notably the one a session ending causes.
  const [draftLoaded, setDraftLoaded] = useState(false);
  const [notice, setNotice] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wasLoading = useRef(false);

  useEffect(() => {
    const draft = loadDraft();
    if (draft) {
      setInput(draft);
      if (wasRelaunched()) setNotice("You were signed in again. Your question is back in the box: press Send.");
    }
    setDraftLoaded(true);
  }, []);

  useEffect(() => {
    if (draftLoaded) saveDraft(input);
  }, [input, draftLoaded]);

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

  const commit = (next: Message[]) => {
    messagesRef.current = next;
    setMessages(next);
    onMessagesChange?.(next);
  };

  const submit = async () => {
    const query = input.trim();
    if (!query || loading) return;

    // The last few question and answer pairs of this chat go with the question, so a follow-up ("Tell me more about him") is understood.
    // Taken before the new question is added to the chat.
    const history = historyOf(messagesRef.current);
    const userMessage: Message = {
      id: newId(),
      role: "user",
      content: query,
      timestamp: new Date(),
    };
    commit([...messagesRef.current, userMessage]);
    setInput("");
    setLoading(true);

    // Build the reply before touching state: a state updater that throws while
    // React renders would take the whole page down instead of showing an error.
    let reply: Message;
    try {
      const response = await sendChat(query, history);
      if (typeof response?.answer !== "string") {
        throw new Error("The server sent an unexpected response.");
      }
      const photos = cleanPhotos(response.photos);
      reply = {
        id: newId(),
        role: "assistant",
        content: response.answer,
        sources: response.sources,
        found: response.found,
        ...(response.clarify === true ? { clarify: true } : {}),
        ...(photos.length ? { photos } : {}),
        timestamp: new Date(),
      };
    } catch (err) {
      if (err instanceof SessionExpiredError) {
        // The sign-in ended: put the question back, then go round through the App Hub, which returns the person here
        // signed in. The question is saved first, so it is still in the box afterwards.
        saveDraft(query);
        commit(messagesRef.current.filter((m) => m.id !== userMessage.id));
        setInput(query);
        setLoading(false);
        if (startRelaunch()) {
          window.location.reload();
          return;
        }
        setNotice("Your session ended and signing in again did not work. Open MCCIA Search from the App Hub.");
        return;
      }
      reply = {
        id: newId(),
        role: "assistant",
        content: `Sorry, I couldn't get an answer. ${describeError(err)}`,
        timestamp: new Date(),
      };
    }
    // Still saved if the user opened another chat meanwhile: the answer goes to this chat.
    commit([...messagesRef.current, reply]);
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
    <section className="relative flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <EmptyState onPick={pickExample} />
        ) : (
          <div
            role="log"
            aria-label="Conversation"
            className="mx-auto flex w-full max-w-[760px] flex-col gap-6 px-4 pb-44 pt-8 md:px-6"
          >
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
            {loading && <TypingIndicator />}
            {/* The scroll margin keeps the newest message clear of the floating question bar. */}
            <div ref={bottomRef} className="scroll-mb-44" />
          </div>
        )}
      </div>

      {/* The question bar floats over the bottom of the conversation, which scrolls beneath it. */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-page via-page/90 to-transparent px-4 pb-4 pt-10 md:px-6">
        <form
          onSubmit={handleSubmit}
          className="pointer-events-auto mx-auto flex w-full max-w-[760px] items-end gap-2 rounded-[28px] bg-white/80 p-2 pl-5 shadow-float ring-1 ring-black/[0.06] backdrop-blur-xl transition-[box-shadow,background-color,transform] duration-200 ease-out focus-within:-translate-y-0.5 focus-within:bg-white focus-within:shadow-float-focus focus-within:ring-2 focus-within:ring-accent/40 motion-reduce:focus-within:translate-y-0"
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
            className="min-h-[2.5rem] flex-1 resize-none bg-transparent py-2 text-base leading-relaxed text-ink placeholder:text-muted focus:outline-none disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={loading || !input.trim()}
            aria-label="Send question"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent text-white transition duration-200 hover:bg-accent-dark focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-accent"
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
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </form>
        <p
          role="status"
          className="pointer-events-auto mx-auto mt-1.5 h-4 max-w-[760px] text-center text-xs text-muted"
        >
          {loading ? "Searching documents..." : notice}
        </p>
      </div>
    </section>
  );
}
