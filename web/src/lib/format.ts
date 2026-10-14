/** "14 Oct, 3:41 pm": a compact local date and time for tables. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** 850 -> "0.9 s", 12500 -> "12.5 s"; a missing value is a dash. */
export function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return "–";
  return (ms / 1000).toFixed(1) + " s";
}

export function formatNumber(n: number): string {
  return n.toLocaleString("en-IN");
}
