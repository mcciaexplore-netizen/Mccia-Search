"use client";

import { useState } from "react";
import type { KeyboardEvent } from "react";
import AuditLog from "./AuditLog";
import KnowledgeBase from "./KnowledgeBase";

type Tab = "knowledge" | "audit";

const TABS: { id: Tab; label: string }[] = [
  { id: "knowledge", label: "Knowledge base" },
  { id: "audit", label: "Audit log" },
];

export default function SettingsPanel() {
  const [tab, setTab] = useState<Tab>("knowledge");

  // Left/Right arrows move between the tabs, as people expect from a tab list.
  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const next = TABS[(index + (event.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
    setTab(next.id);
    document.getElementById(`settings-tab-${next.id}`)?.focus();
  };

  return (
    <section aria-label="Settings" className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6 md:px-8 md:py-8">
        <h1 className="text-2xl font-semibold text-primary md:text-3xl">Settings</h1>

        <div role="tablist" aria-label="Settings sections" className="mt-5 flex gap-1 border-b border-black/10">
          {TABS.map((t, index) => (
            <button
              key={t.id}
              id={`settings-tab-${t.id}`}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              aria-controls={`settings-panel-${t.id}`}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => setTab(t.id)}
              onKeyDown={(event) => handleKeyDown(event, index)}
              className={`-mb-px border-b-2 px-4 py-2.5 text-base font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary ${
                tab === t.id ? "border-accent text-primary" : "border-transparent text-muted hover:text-primary"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div
          role="tabpanel"
          id={`settings-panel-${tab}`}
          aria-labelledby={`settings-tab-${tab}`}
          className="pt-6"
        >
          {tab === "knowledge" ? <KnowledgeBase /> : <AuditLog />}
        </div>
      </div>
    </section>
  );
}
