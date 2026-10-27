"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ChatWindow from "./ChatWindow";
import HistoryList from "./HistoryList";
import Logo from "./Logo";
import PagePanel from "./PagePanel";
import SettingsPanel from "./SettingsPanel";
import { loadChats, removeChat, saveChats, upsertChat } from "@/lib/history";
import { panelFor, sourceKey } from "@/lib/sources";
import type { PanelPin, ViewableSource } from "@/lib/sources";
import type { Chat, Message } from "@/lib/types";
import { useMediaQuery } from "@/lib/useMediaQuery";

type View = "chat" | "settings";

// The page panel sits beside the chat from this width up (slimmer below 1280px). On a
// narrower window there is no room for it, so it slides over the chat instead: by itself when
// an answer arrives, from this width up (where the answer stays in view), and otherwise with
// the "Scanned pages" button.
const WIDE_WINDOW = "(min-width: 1024px)";
const MEDIUM_WINDOW = "(min-width: 768px)";

// crypto.randomUUID() only exists in secure contexts, and this app may be served
// over plain HTTP on the office network.
const newChatId = () =>
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** The signed-in person (when single sign-on is on) and where the App Hub is. Both are null when the site is open. */
interface ChatShellProps {
  user?: { name: string; email: string; role: "user" | "admin" } | null;
  hubUrl?: string | null;
}

export default function ChatShell({ user = null, hubUrl = null }: ChatShellProps) {
  // Settings (the knowledge base and the audit log) are for administrators; with no sign-in they stay open as before.
  const canOpenSettings = !user || user.role === "admin";
  const [chats, setChats] = useState<Chat[]>([]);
  const chatsRef = useRef<Chat[]>([]);
  const [activeId, setActiveId] = useState(newChatId);
  const [view, setView] = useState<View>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const wide = useMediaQuery(WIDE_WINDOW);
  const medium = useMediaQuery(MEDIUM_WINDOW);
  // The page the user picked, the answer whose sidebar the user closed, and the slide-over's state.
  const [pin, setPin] = useState<PanelPin | null>(null);
  const [closedFor, setClosedFor] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // The window's width is only known once mounted; until then no button, so it does not flash.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // The history comes from this browser's storage, so it can only be read after mounting.
  useEffect(() => {
    const saved = loadChats();
    chatsRef.current = saved;
    setChats(saved);
  }, []);

  useEffect(() => {
    if (!sidebarOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);

  const store = useCallback((next: Chat[]) => {
    chatsRef.current = next;
    setChats(next);
    saveChats(next);
  }, []);

  // A chat is saved from its first message on; this runs after every message.
  const handleMessages = useCallback(
    (id: string, messages: Message[]) => {
      if (messages.length > 0) store(upsertChat(chatsRef.current, id, messages));
    },
    [store]
  );

  const startNewChat = () => {
    // A new id remounts ChatWindow, which clears its messages and input and drops
    // any answer that is still on its way.
    setActiveId(newChatId());
    setView("chat");
    setSidebarOpen(false);
  };

  const openChat = (id: string) => {
    setActiveId(id);
    setView("chat");
    setSidebarOpen(false);
  };

  const deleteChat = (id: string) => {
    store(removeChat(chatsRef.current, id));
    if (id === activeId) startNewChat();
  };

  const clearHistory = () => {
    if (!window.confirm("Delete all saved conversations from this browser?")) return;
    store([]);
    startNewChat();
  };

  const openSettings = () => {
    setView("settings");
    setSidebarOpen(false);
  };

  const activeMessages = chats.find((chat) => chat.id === activeId)?.messages ?? [];

  // The page panel shows the pages the newest answer was taken from, the best match first. A
  // new answer, or another chat, starts it afresh.
  const panel = panelFor(activeMessages, pin);
  const newestAnswerId = panelFor(activeMessages, null)?.messageId ?? null;
  useEffect(() => {
    setPin(null);
    setClosedFor(null);
    setDrawerOpen(false);
  }, [activeId, newestAnswerId]);

  const inChat = view === "chat" && panel !== null;
  const sidebarShown = inChat && wide && closedFor !== panel.messageId;
  const drawerShown = inChat && !wide && drawerOpen;

  const choosePage = (source: ViewableSource) => {
    if (panel) setPin({ messageId: panel.messageId, key: sourceKey(source) });
  };

  // A window made wide shows the sidebar instead, so the slide-over starts closed next time.
  useEffect(() => {
    if (wide) setDrawerOpen(false);
  }, [wide]);

  // On a medium window the pages slide in by themselves when an answer arrives (after the
  // reset above), so the images are seen without having to find the button.
  useEffect(() => {
    if (newestAnswerId && medium && !wide) setDrawerOpen(true);
  }, [activeId, newestAnswerId, medium, wide]);

  // Escape closes the slide-over.
  useEffect(() => {
    if (!drawerShown) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawerShown]);

  return (
    <div className="min-h-dvh bg-page">
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          aria-hidden="true"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        id="sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col bg-primary text-white transition-[transform,visibility] duration-200 md:visible md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "invisible -translate-x-full"
        }`}
      >
        <div className="p-5 pb-3">
          <div className="rounded-xl bg-white px-4 py-3 shadow-sm">
            <Logo width={176} />
          </div>
          <p className="mt-2.5 text-base font-semibold tracking-wider text-accent">AI Search</p>

          <button
            type="button"
            onClick={startNewChat}
            className="mt-4 w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-ink transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
          >
            New Chat
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3 pt-1">
          <HistoryList
            chats={chats}
            activeId={view === "chat" ? activeId : null}
            onOpen={openChat}
            onDelete={deleteChat}
            onClearAll={clearHistory}
          />
        </div>

        <div className="border-t border-white/15 p-3">
          {user && (
            <div className="mb-2 rounded-lg bg-white/10 px-3 py-2.5 text-sm">
              <p className="truncate font-semibold" title={user.name}>{user.name}</p>
              <p className="truncate text-xs text-white/70" title={user.email}>{user.email}</p>
              <div className="mt-2 flex items-center gap-3 text-xs font-medium">
                {hubUrl && (
                  <a href={hubUrl} className="rounded underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-white">
                    Back to App Hub
                  </a>
                )}
                <form method="post" action="/api/logout" className="ml-auto">
                  <button type="submit" className="rounded underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-white">
                    Log out
                  </button>
                </form>
              </div>
            </div>
          )}
          {canOpenSettings && (
          <button
            type="button"
            onClick={openSettings}
            aria-current={view === "settings" ? "page" : undefined}
            className={`flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-white ${
              view === "settings" ? "bg-white/15" : "hover:bg-white/10"
            }`}
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" />
            </svg>
            Settings
          </button>
          )}
        </div>
      </aside>

      {sidebarShown && panel && (
        <PagePanel
          layout="sidebar"
          sources={panel.sources}
          selectedKey={panel.selectedKey}
          citedKeys={panel.citedKeys}
          onSelect={choosePage}
          onClose={() => setClosedFor(panel.messageId)}
        />
      )}

      {drawerShown && panel && (
        <>
          <div className="fixed inset-0 z-40 bg-black/40" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
          <PagePanel
            layout="drawer"
            sources={panel.sources}
            selectedKey={panel.selectedKey}
            citedKeys={panel.citedKeys}
            onSelect={choosePage}
            onClose={() => setDrawerOpen(false)}
          />
        </>
      )}

      {/* How to see the pages when the panel is not on screen: it was closed, or the window is narrow. */}
      {mounted && inChat && panel && !sidebarShown && !drawerShown && (
        <button
          type="button"
          onClick={() => (wide ? setClosedFor(null) : setDrawerOpen(true))}
          className="fixed bottom-28 right-4 z-20 flex items-center gap-2 rounded-full bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-lg transition hover:bg-primary/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" />
          </svg>
          Scanned pages ({panel.sources.length})
        </button>
      )}

      <main className={`flex h-dvh flex-col md:pl-72 ${sidebarShown ? "lg:pr-[22rem] xl:pr-[26rem]" : ""}`}>
        <header className="flex items-center gap-3 bg-primary px-4 py-3 text-white md:hidden">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open menu"
            aria-controls="sidebar"
            aria-expanded={sidebarOpen}
            className="-ml-1 rounded-md p-1.5 hover:bg-white/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-white"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="h-6 w-6"
            >
              <path d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <div className="rounded-md bg-white px-2.5 py-1.5">
            <Logo width={88} />
          </div>
          <span className="text-sm font-semibold text-accent">AI Search</span>
        </header>

        <div className="min-h-0 flex-1">
          {view === "settings" && canOpenSettings ? (
            <SettingsPanel />
          ) : (
            <ChatWindow
              key={activeId}
              initialMessages={activeMessages}
              onMessagesChange={(messages) => handleMessages(activeId, messages)}
            />
          )}
        </div>
      </main>
    </div>
  );
}
