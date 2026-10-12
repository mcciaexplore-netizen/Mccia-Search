"use client";

import { useEffect, useState } from "react";
import ChatWindow from "./ChatWindow";
import IngestPanel from "./IngestPanel";

export default function ChatShell() {
  // Changing the key remounts ChatWindow, which clears its messages and input
  // and drops any answer that is still on its way.
  const [chatId, setChatId] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    if (!sidebarOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sidebarOpen]);

  const startNewChat = () => {
    setChatId((id) => id + 1);
    setSidebarOpen(false);
  };

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
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col overflow-y-auto bg-primary p-5 text-white transition-[transform,visibility] duration-200 md:visible md:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "invisible -translate-x-full"
        }`}
      >
        <div>
          <p className="text-2xl font-bold tracking-wide text-white">MCCIA</p>
          <p className="text-base font-semibold tracking-wider text-accent">
            AI Search
          </p>
        </div>

        <hr className="my-5 border-white/15" />

        <button
          type="button"
          onClick={startNewChat}
          className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-ink transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
        >
          New Chat
        </button>

        <hr className="my-5 border-white/15" />

        <p className="text-xs font-medium uppercase tracking-wider text-white/60">
          Knowledge Base
        </p>
        <IngestPanel />
      </aside>

      <main className="flex h-dvh flex-col md:pl-64">
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
          <span className="text-lg font-bold">MCCIA</span>
          <span className="text-sm font-semibold text-accent">AI Search</span>
        </header>

        <div className="min-h-0 flex-1">
          <ChatWindow key={chatId} />
        </div>
      </main>
    </div>
  );
}
