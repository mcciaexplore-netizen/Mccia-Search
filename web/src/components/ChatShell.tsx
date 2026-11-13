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

// Whether the person collapsed the sidebar on a wide window, remembered in this browser (a convenience: it works without it).
const COLLAPSE_KEY = "mccia-sidebar-collapsed";

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
  /** `hubUrl`: from the hub's sign-in token, for the "‹ App Hub" link at the top of the sidebar. */
  user?: { name: string; email: string; role: "user" | "admin"; hubUrl?: string | null } | null;
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

  // The desktop sidebar can be collapsed (the mobile one is the slide-out drawer, `sidebarOpen`). A remembered "collapsed" is applied
  // first without animation, and the slide is switched on a moment later, so a returning visitor does not see it slide shut on load.
  const [collapsed, setCollapsed] = useState(false);
  const [animate, setAnimate] = useState(false);
  useEffect(() => {
    try {
      if (window.localStorage.getItem(COLLAPSE_KEY) === "1") setCollapsed(true);
    } catch {
      /* storage can be blocked: the sidebar then starts open */
    }
    const frame = window.requestAnimationFrame(() => setAnimate(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const changeCollapsed = (value: boolean) => {
    setCollapsed(value);
    try {
      window.localStorage.setItem(COLLAPSE_KEY, value ? "1" : "0");
    } catch {
      /* not remembered, still works */
    }
  };

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

  const initials = user
    ? user.name.split(/\s+/).filter(Boolean).slice(0, 2).map((word) => Array.from(word)[0]).join("").toUpperCase() || "?"
    : "";

  return (
    <div className="relative isolate min-h-dvh bg-page">
      {/* The soft shapes the glass panels blur. */}
      <div aria-hidden="true" className="app-backdrop pointer-events-none fixed inset-0 -z-10" />

      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 animate-fade-in bg-primary/40 backdrop-blur-[2px] md:hidden"
          aria-hidden="true"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        id="sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex w-[280px] max-w-[85vw] flex-col border-r border-white/70 bg-white/85 shadow-glass backdrop-blur-xl will-change-transform md:bg-white/70 ${
          animate ? "transition-[transform,visibility] duration-sidebar ease-out" : ""
        } ${sidebarOpen ? "translate-x-0" : "invisible -translate-x-full"} ${
          collapsed ? "md:invisible md:-translate-x-full" : "md:visible md:translate-x-0"
        }`}
      >
        <div className="px-4 pb-3 pt-4">
          {user?.hubUrl && (
            <a href={user.hubUrl} className="mb-2 inline-flex items-center gap-1 rounded text-xs font-medium text-muted transition-colors hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-3.5 w-3.5"><path d="M15 18l-6-6 6-6" /></svg>
              App Hub
            </a>
          )}
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1 px-0.5 py-1">
              <Logo width={112} />
            </div>
            {/* Wide windows collapse the sidebar; narrow ones close the drawer. */}
            <button
              type="button"
              onClick={() => changeCollapsed(true)}
              aria-label="Collapse sidebar"
              aria-controls="sidebar"
              aria-expanded={!collapsed}
              className="hidden h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-black/5 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent md:flex"
            >
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5"><path d="M11 17l-5-5 5-5M18 17l-5-5 5-5" /></svg>
            </button>
            <button
              type="button"
              onClick={() => setSidebarOpen(false)}
              aria-label="Close menu"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-black/5 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent md:hidden"
            >
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="h-5 w-5"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
          <p className="mt-2.5 px-0.5 text-sm font-semibold tracking-wider text-accent">AI Search</p>

          <button
            type="button"
            onClick={startNewChat}
            className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors duration-150 hover:bg-accent-dark focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" className="h-4 w-4"><path d="M12 5v14M5 12h14" /></svg>
            New Chat
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3 pt-1">
          <HistoryList
            chats={chats}
            activeId={view === "chat" ? activeId : null}
            onOpen={openChat}
            onDelete={deleteChat}
          />
        </div>

        <div className="space-y-1 border-t border-black/5 p-3">
          {chats.length > 0 && (
            <button
              type="button"
              onClick={clearHistory}
              className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm font-medium text-muted transition-colors hover:bg-black/[0.04] hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            >
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                <path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3" />
              </svg>
              Clear history
            </button>
          )}
          {canOpenSettings && (
            <button
              type="button"
              onClick={openSettings}
              aria-current={view === "settings" ? "page" : undefined}
              className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${
                view === "settings" ? "bg-accent/10 text-primary" : "text-muted hover:bg-black/[0.04] hover:text-primary"
              }`}
            >
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" />
              </svg>
              Settings
            </button>
          )}
          {user && (
            <div className="mt-1 rounded-xl bg-white/70 p-2.5 ring-1 ring-black/5">
              <div className="flex items-center gap-3">
                <span aria-hidden="true" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-semibold text-white">
                  {initials}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-primary" title={user.name}>{user.name}</p>
                  <p className="truncate text-xs text-muted" title={user.email}>{user.email}</p>
                </div>
              </div>
              <div className="mt-2 flex items-center gap-3 text-xs font-medium">
                {hubUrl && !user.hubUrl && (
                  <a href={hubUrl} className="rounded text-muted underline-offset-2 hover:text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
                    Back to App Hub
                  </a>
                )}
                <form method="post" action="/api/logout" className="ml-auto">
                  <button type="submit" className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-muted transition-colors hover:bg-black/5 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
                    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9" /></svg>
                    Log out
                  </button>
                </form>
              </div>
            </div>
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
          <div className="fixed inset-0 z-40 animate-fade-in bg-primary/40 backdrop-blur-[2px]" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
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
          className="fixed bottom-36 right-4 z-20 flex animate-fade-in items-center gap-2 rounded-full bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-lift transition-colors hover:bg-primary/90 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" />
          </svg>
          Scanned pages ({panel.sources.length})
        </button>
      )}

      {/* With the sidebar collapsed on a wide window, this brings it back. */}
      {mounted && collapsed && (
        <button
          type="button"
          onClick={() => changeCollapsed(false)}
          aria-label="Expand sidebar"
          aria-controls="sidebar"
          aria-expanded={false}
          className="fixed left-3 top-2 z-30 hidden h-10 w-10 animate-fade-in items-center justify-center rounded-xl bg-white/80 text-primary shadow-glass ring-1 ring-black/5 backdrop-blur-xl transition-colors hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent md:flex"
        >
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5"><path d="M13 17l5-5-5-5M6 17l5-5-5-5" /></svg>
        </button>
      )}

      <main
        className={`flex h-dvh flex-col ${animate ? "md:transition-[padding] md:duration-sidebar md:ease-out" : ""} ${collapsed ? "md:pl-0 md:pt-14" : "md:pl-[280px]"} ${
          sidebarShown ? "lg:pr-[22rem] xl:pr-[26rem]" : ""
        }`}
      >
        <header className="flex items-center gap-3 border-b border-black/5 bg-white/80 px-4 py-2.5 backdrop-blur-xl md:hidden">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open menu"
            aria-controls="sidebar"
            aria-expanded={sidebarOpen}
            className="-ml-1 rounded-lg p-1.5 text-primary hover:bg-black/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
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
          <div className="px-0.5">
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
