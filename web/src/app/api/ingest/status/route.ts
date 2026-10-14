import { proxyToBackend } from "@/lib/proxy";

export const dynamic = "force-dynamic"; // the numbers change as documents are ingested

export async function GET() {
  return proxyToBackend("/ingest/status");
}
