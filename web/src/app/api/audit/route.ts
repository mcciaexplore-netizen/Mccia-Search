import { NextRequest } from "next/server";
import { proxyToBackend } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  // limit, offset and event pass straight through; the backend checks them.
  return proxyToBackend("/audit" + req.nextUrl.search, undefined, "admin");
}
