"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getAuditLog } from "@/lib/api";
import { formatDateTime, formatDuration, formatNumber } from "@/lib/format";
import type { AuditEntry, AuditResponse } from "@/lib/types";

type EventFilter = "all" | "chat" | "sync";

const PAGE_SIZE = 50;

function isAuditResponse(value: unknown): value is AuditResponse {
  if (typeof value !== "object" || value === null) return false;
  const v = value as { total?: unknown; items?: unknown };
  return (
    typeof v.total === "number" &&
    Array.isArray(v.items) &&
    v.items.every((i) => typeof i === "object" && i !== null && typeof (i as { event?: unknown }).event === "string")
  );
}

function outcome(entry: AuditEntry): { label: string; className: string } {
  if (entry.status !== null && entry.status >= 500) return { label: "Error", className: "bg-red-100 text-red-800" };
  if (entry.status === 400) return { label: "Rejected", className: "bg-gray-100 text-ink" };
  if (entry.event === "chat") {
    return entry.found
      ? { label: "Answered", className: "bg-emerald-100 text-emerald-800" }
      : { label: "Not found", className: "bg-accent/15 text-ink" };
  }
  return { label: "OK", className: "bg-emerald-100 text-emerald-800" };
}

export default function AuditLog() {
  const [items, setItems] = useState<AuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<EventFilter>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const request = useRef(0);

  // Load the first page, or the next one when `more` is true. A late answer for an
  // older filter is ignored.
  const load = useCallback(
    async (more: boolean, current: EventFilter, offset: number) => {
      const mine = ++request.current;
      setLoading(true);
      try {
        const result: unknown = await getAuditLog({
          limit: PAGE_SIZE,
          offset: more ? offset : 0,
          event: current === "all" ? undefined : current,
        });
        if (!isAuditResponse(result)) throw new Error("Unexpected response");
        if (mine !== request.current) return;
        setItems((prev) => (more ? [...prev, ...result.items] : result.items));
        setTotal(result.total);
        setError(false);
      } catch {
        if (mine === request.current) setError(true);
      } finally {
        if (mine === request.current) setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    void load(false, filter, 0);
  }, [filter, load]);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-primary">Audit log</h2>
          <p className="mt-1 max-w-xl text-base text-muted">
            A record of every question asked and every sync, newest first: when, what, and how it went.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor="audit-filter" className="text-sm text-muted">Show</label>
          <select
            id="audit-filter"
            value={filter}
            onChange={(event) => setFilter(event.target.value as EventFilter)}
            className="rounded-lg bg-surface px-3 py-2 text-sm ring-1 ring-black/10 focus:outline-none focus:ring-2 focus:ring-primary/40"
          >
            <option value="all">Everything</option>
            <option value="chat">Questions</option>
            <option value="sync">Syncs</option>
          </select>
          <button
            type="button"
            onClick={() => void load(false, filter, 0)}
            disabled={loading}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-60"
          >
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="mt-6 rounded-xl border border-accent/40 bg-accent/10 px-4 py-4 text-base text-ink">
          Could not load the audit log.{" "}
          <button type="button" onClick={() => void load(false, filter, 0)} className="font-semibold text-primary underline underline-offset-2">
            Try again
          </button>
        </div>
      )}

      {!error && (
        <div className="mt-6 overflow-x-auto rounded-xl bg-surface shadow-sm ring-1 ring-black/5">
          <table className="w-full min-w-[46rem] text-left text-sm">
            <thead className="border-b border-black/5 text-muted">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">When</th>
                <th scope="col" className="px-4 py-3 font-medium">Type</th>
                <th scope="col" className="px-4 py-3 font-medium">Details</th>
                <th scope="col" className="px-4 py-3 font-medium">User</th>
                <th scope="col" className="px-4 py-3 font-medium">Result</th>
                <th scope="col" className="px-4 py-3 text-right font-medium">Sources</th>
                <th scope="col" className="px-4 py-3 text-right font-medium">Took</th>
                <th scope="col" className="px-4 py-3 font-medium">From</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-black/5">
              {items.map((entry) => {
                const result = outcome(entry);
                return (
                  <tr key={entry.id} className="align-top">
                    <td className="whitespace-nowrap px-4 py-3 text-muted">{formatDateTime(entry.occurred_at)}</td>
                    <td className="px-4 py-3 text-primary">{entry.event === "chat" ? "Question" : "Sync"}</td>
                    <td className="max-w-[24rem] px-4 py-3 text-ink">
                      <span className="line-clamp-2 break-words" title={entry.query ?? ""}>{entry.query ?? ""}</span>
                      {entry.event === "sync" && entry.detail && (
                        <span className="block text-muted">{entry.detail}</span>
                      )}
                    </td>
                    <td className="max-w-[12rem] truncate px-4 py-3 text-muted" title={entry.user_email ?? ""}>{entry.user_email ?? "–"}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${result.className}`}>{result.label}</span>
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{entry.sources ?? "–"}</td>
                    <td className="whitespace-nowrap px-4 py-3 text-right tabular-nums">{formatDuration(entry.duration_ms)}</td>
                    <td className="px-4 py-3 text-muted">{entry.client ?? "–"}</td>
                  </tr>
                );
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-4 py-8 text-center text-muted">
                    {loading ? "Loading audit log..." : "Nothing recorded yet."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {!error && items.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted">
            Showing {formatNumber(items.length)} of {formatNumber(total)}
          </p>
          {items.length < total && (
            <button
              type="button"
              onClick={() => void load(true, filter, items.length)}
              disabled={loading}
              className="rounded-lg bg-surface px-4 py-2 text-sm font-semibold text-primary ring-1 ring-black/10 transition hover:ring-primary/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-60"
            >
              Load more
            </button>
          )}
        </div>
      )}
    </div>
  );
}
