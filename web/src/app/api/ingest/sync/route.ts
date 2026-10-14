import { NextRequest } from "next/server";
import { clientAddress, proxyToBackend } from "@/lib/proxy";

export async function POST(req: NextRequest) {
  return proxyToBackend("/ingest/sync", { method: "POST", headers: clientAddress(req) });
}
