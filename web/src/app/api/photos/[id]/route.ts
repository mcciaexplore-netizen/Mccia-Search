import { NextResponse } from "next/server";
import { proxyFileFromBackend } from "@/lib/proxy";

export const dynamic = "force-dynamic"; // the picture is read from storage on demand; caching is left to the image's own headers (a day, private to the browser)

// A photograph's id is a small whole number. Anything else is turned away here and never reaches the backend.
const WHOLE_NUMBER = /^\d{1,9}$/;

export async function GET(_req: Request, { params }: { params: { id: string } }) {
  if (!WHOLE_NUMBER.test(params.id)) {
    return NextResponse.json({ detail: "That photograph was not found." }, { status: 404 });
  }
  return proxyFileFromBackend(`/photos/${params.id}/image`);
}
