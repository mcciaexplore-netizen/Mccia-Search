import { proxyToBackend } from "@/lib/proxy";

export const dynamic = "force-dynamic"; // the list changes as documents are ingested

export async function GET() {
  return proxyToBackend("/documents");
}
