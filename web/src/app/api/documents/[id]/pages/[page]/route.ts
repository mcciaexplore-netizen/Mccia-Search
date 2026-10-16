import { NextResponse } from "next/server";
import { proxyFileFromBackend } from "@/lib/proxy";

export const dynamic = "force-dynamic"; // the page is drawn on demand; caching is left to the image's own headers

// Document ids and page numbers are small whole numbers. Anything else is turned away
// here and never reaches the backend.
const WHOLE_NUMBER = /^\d{1,9}$/;

export async function GET(
  _req: Request,
  { params }: { params: { id: string; page: string } }
) {
  if (!WHOLE_NUMBER.test(params.id) || !WHOLE_NUMBER.test(params.page)) {
    return NextResponse.json({ detail: "That page was not found." }, { status: 404 });
  }
  return proxyFileFromBackend(`/documents/${params.id}/pages/${params.page}`);
}
