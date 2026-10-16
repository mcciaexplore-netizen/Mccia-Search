"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getDocuments, triggerSync } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import type { DocumentsResponse } from "@/lib/types";

type Filter = "all" | "completed" | "failed" | "pending";
type SyncState = "idle" | "syncing" | "started" | "error";

const REFRESH_INTERVAL_MS = 30_000;
const SYNC_MESSAGE_MS = 3_000;

const SYNC_LABELS: Record<SyncState, string> = {
  idle: "Sync Drive",
  syncing: "Syncing...",
  started: "Sync started",
  error: "Sync failed",
};

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "completed", label: "Ready" },
  { value: "pending", label: "In progress" },
  { value: "failed", label: "Failed" },
];

const STATUS_BADGE: Record<string, { label: string; className: string }> = {
  completed: { label: "Ready", className: "bg-emerald-100 text-emerald-800" },
  failed: { label: "Failed", className: "bg-accent/15 text-ink" },
  processing: { label: "In progress", className: "bg-sky-100 text-sky-800" },
  pending: { label: "In progress", className: "bg-sky-100 text-sky-800" },
  // The text is saved but not yet embedded, so search cannot find it.
  text_saved: { label: "Waiting for embeddings", className: "bg-sky-100 text-sky-800" },
};

// Make sure the payload really is a document list before showing it.
function isDocumentsResponse(value: unknown): value is DocumentsResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as { summary?: Record<string, unknown>; documents?: unknown };
  const s = v.summary;
  return (
    typeof s === "object" &&
    s !== null &&
    ["total", "completed", "failed", "pending", "chunks"].every((k) => typeof s[k] === "number") &&
    Array.isArray(v.documents) &&
    v.documents.every(
      (d) => typeof d === "object" && d !== null && typeof (d as { filename?: unknown }).filename === "string"
    )
  );
}

function SummaryCard({ label, value, tone = "normal" }: { label: string; value: string; tone?: "normal" | "warn" }) {
  return (
    <div className="rounded-xl bg-surface px-4 py-3 shadow-sm ring-1 ring-black/5">
      <p className="text-sm text-muted">{label}</p>
      <p className={`mt-0.5 text-2xl font-semibold ${tone === "warn" ? "text-accent" : "text-primary"}`}>{value}</p>
    </div>
  );
}

export default function KnowledgeBase() {
  const [data, setData] = useState<DocumentsResponse | null>(null);
  const [error, setError] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [syncState, setSyncState] = useState<SyncState>("idle");
  const resetTimer = useRef<ReturnType<typeof setTimeout>>();

  const load = useCallback(async () => {
    try {
      const result: unknown = await getDocuments();
      if (!isDocumentsResponse(result)) throw new Error("Unexpected response");
      setData(result);
      setError(false);
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [load]);

  useEffect(() => () => clearTimeout(resetTimer.current), []);

  const handleSync = async () => {
    if (syncState !== "idle") return;
    setSyncState("syncing");
    let next: SyncState = "error";
    try {
      const response = await triggerSync();
      if (response?.status === "started") next = "started";
    } catch {
      // Network or server error: leave `next` as "error".
    }
    setSyncState(next);
    if (next === "started") void load();
    clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => setSyncState("idle"), SYNC_MESSAGE_MS);
  };

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (data?.documents ?? []).filter((d) => {
      const group = d.status === "processing" || d.status === "text_saved" ? "pending" : d.status;
      return (filter === "all" || group === filter) && (!needle || d.filename.toLowerCase().includes(needle));
    });
  }, [data, filter, search]);

  const readyPages = useMemo(
    () => (data?.documents ?? []).filter((d) => d.status === "completed").reduce((sum, d) => sum + (d.total_pages ?? 0), 0),
    [data]
  );

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-primary">Knowledge base</h2>
          <p className="mt-1 max-w-xl text-base text-muted">
            The documents MCCIA Search answers from. Only documents marked Ready can be searched.
          </p>
        </div>
        <button
          type="button"
          onClick={handleSync}
          disabled={syncState !== "idle"}
          className="rounded-lg bg-accent px-5 py-2.5 text-sm font-semibold text-ink transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-60"
        >
          {SYNC_LABELS[syncState]}
        </button>
      </div>

      {error && !data && (
        <div role="alert" className="mt-6 rounded-xl border border-accent/40 bg-accent/10 px-4 py-4 text-base text-ink">
          Could not load the knowledge base.{" "}
          <button type="button" onClick={() => void load()} className="font-semibold text-primary underline underline-offset-2">
            Try again
          </button>
        </div>
      )}

      {!data && !error && <p className="mt-6 text-base text-muted">Loading knowledge base...</p>}

      {data && (
        <>
          <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
            <SummaryCard label="Ready to search" value={formatNumber(data.summary.completed)} />
            <SummaryCard label="Pages indexed" value={formatNumber(readyPages)} />
            <SummaryCard label="In progress" value={formatNumber(data.summary.pending)} />
            <SummaryCard label="Failed" value={formatNumber(data.summary.failed)} tone={data.summary.failed > 0 ? "warn" : "normal"} />
          </div>

          {error && (
            <p role="status" className="mt-3 text-sm text-muted">
              Could not refresh just now; showing the last list.
            </p>
          )}

          <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
            <div role="group" aria-label="Filter documents" className="flex flex-wrap gap-1.5">
              {FILTERS.map((f) => (
                <button
                  key={f.value}
                  type="button"
                  onClick={() => setFilter(f.value)}
                  aria-pressed={filter === f.value}
                  className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary ${
                    filter === f.value ? "bg-primary text-white" : "bg-surface text-primary ring-1 ring-black/10 hover:ring-primary/40"
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search documents..."
              aria-label="Search documents"
              className="w-full rounded-lg bg-surface px-3.5 py-2 text-sm ring-1 ring-black/10 placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-primary/40 sm:w-64"
            />
          </div>

          <div className="mt-4 overflow-x-auto rounded-xl bg-surface shadow-sm ring-1 ring-black/5">
            <table className="w-full min-w-[34rem] text-left text-sm">
              <thead className="border-b border-black/5 text-muted">
                <tr>
                  <th scope="col" className="px-4 py-3 font-medium">Document</th>
                  <th scope="col" className="px-4 py-3 font-medium">Status</th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">Pages</th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">Chunks</th>
                  <th scope="col" className="px-4 py-3 font-medium">Added</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-black/5">
                {rows.map((d) => {
                  const badge = STATUS_BADGE[d.status] ?? { label: d.status, className: "bg-gray-100 text-ink" };
                  return (
                    <tr key={d.id}>
                      <td className="max-w-[20rem] truncate px-4 py-3 font-medium text-primary" title={d.filename}>
                        {d.filename}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${badge.className}`}>
                          {badge.label}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums">{d.total_pages ?? "–"}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{d.status === "completed" ? formatNumber(d.chunks) : "–"}</td>
                      <td className="px-4 py-3 text-muted">{formatDateTime(d.added)}</td>
                    </tr>
                  );
                })}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-4 py-8 text-center text-muted">
                      {data.documents.length === 0 ? "No documents yet." : "No documents match."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-sm text-muted">
            Showing {formatNumber(rows.length)} of {formatNumber(data.documents.length)} documents ·{" "}
            {formatNumber(data.summary.chunks)} searchable passages
          </p>
        </>
      )}
    </div>
  );
}
