import { proxyToBackend } from "@/lib/proxy";

export async function POST() {
  return proxyToBackend("/ingest/sync", { method: "POST" });
}
