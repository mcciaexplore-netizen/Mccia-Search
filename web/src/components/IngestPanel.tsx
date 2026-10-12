"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getIngestStatus, triggerSync } from "@/lib/api";

interface IngestStatus {
  total: number;
  completed: number;
  failed: number;
  pending: number;
}

type SyncState = "idle" | "syncing" | "started" | "error";

const REFRESH_INTERVAL_MS = 30_000;
const SYNC_MESSAGE_MS = 3_000;

const SYNC_LABELS: Record<SyncState, string> = {
  idle: "Sync Drive",
  syncing: "Syncing...",
  started: "Sync started",
  error: "Sync failed",
};

// The status endpoint is not checked for HTTP errors in lib/api.ts, so make sure
// the payload really is a status before showing it.
function isIngestStatus(value: unknown): value is IngestStatus {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.total === "number" &&
    typeof v.completed === "number" &&
    typeof v.failed === "number" &&
    typeof v.pending === "number"
  );
}

export default function IngestPanel() {
  const [status, setStatus] = useState<IngestStatus | null>(null);
  const [statusError, setStatusError] = useState(false);
  const [syncState, setSyncState] = useState<SyncState>("idle");
  const resetTimer = useRef<ReturnType<typeof setTimeout>>();

  const loadStatus = useCallback(async () => {
    try {
      const data: unknown = await getIngestStatus();
      if (!isIngestStatus(data)) throw new Error("Unexpected status response");
      setStatus(data);
      setStatusError(false);
    } catch {
      setStatusError(true);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") void loadStatus();
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadStatus]);

  useEffect(() => () => clearTimeout(resetTimer.current), []);

  const handleSync = async () => {
    if (syncState !== "idle") return;
    setSyncState("syncing");
    let next: SyncState = "error";
    try {
      const data = await triggerSync();
      if (data?.status === "started") next = "started";
    } catch {
      // Network error: leave `next` as "error".
    }
    setSyncState(next);
    if (next === "started") void loadStatus();
    clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => setSyncState("idle"), SYNC_MESSAGE_MS);
  };

  return (
    <div className="mt-3">
      {status && !statusError ? (
        <ul className="space-y-1.5 text-sm text-white">
          <li>Total: {status.total} docs</li>
          <li>
            <span aria-hidden="true">✓ </span>
            {status.completed} ready
          </li>
          <li className={status.failed > 0 ? "text-accent" : "text-white/60"}>
            <span aria-hidden="true">✗ </span>
            {status.failed} failed
          </li>
          {status.pending > 0 && (
            <li className="text-white/70">{status.pending} in progress</li>
          )}
        </ul>
      ) : (
        <p className="text-sm text-white/60">
          {statusError ? "Status unavailable" : "Loading status..."}
        </p>
      )}

      <button
        type="button"
        onClick={handleSync}
        disabled={syncState !== "idle"}
        className="mt-4 w-full rounded-lg border border-accent px-4 py-2 text-sm font-semibold text-white transition hover:bg-accent/15 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:cursor-not-allowed disabled:opacity-60"
      >
        {SYNC_LABELS[syncState]}
      </button>
    </div>
  );
}
