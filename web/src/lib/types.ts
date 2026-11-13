export interface Source {
  filename: string;
  page_number: number | null;
  // Missing on chats saved before "View page" existed: such a source has no page link.
  document_id?: number | null;
  similarity: number;
}

/** A photograph from a scanned page, found for a question that asked for one. Its picture is at /api/photos/{id} (see lib/photos.ts). */
export interface Photo {
  id: number;
  document_id: number;
  page_number: number;
  filename: string;
  /** The issue's years as they read in an answer, such as "1964-65". */
  year: string | null;
  /** The caption printed beside it; "" when none was found. */
  caption: string;
  kind: "photo" | "advertisement" | "illustration";
  contains_face: boolean;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  found?: boolean;
  /** The answer is a question back ("Could you clarify who/what you are referring to?"), not a result. */
  clarify?: boolean;
  /** Photographs matching the question, when it asked for photographs, pictures or images. */
  photos?: Photo[];
  timestamp: Date;
}

export interface ChatResponse {
  answer: string;
  sources: Source[];
  found: boolean;
  clarify?: boolean;
  photos?: Photo[];
}

export interface Chat {
  id: string;
  title: string;
  updatedAt: Date;
  messages: Message[];
}

export interface DocumentItem {
  id: number;
  filename: string;
  status: string; // completed | failed | processing | pending
  total_pages: number | null;
  language: string | null;
  chunks: number;
  added: string; // ISO timestamp
}

export interface DocumentsSummary {
  total: number;
  completed: number;
  failed: number;
  pending: number;
  chunks: number;
}

export interface DocumentsResponse {
  summary: DocumentsSummary;
  documents: DocumentItem[];
}

export interface AuditEntry {
  id: number;
  occurred_at: string; // ISO timestamp
  event: string; // chat | sync
  query: string | null;
  found: boolean | null;
  sources: number | null;
  status: number | null;
  duration_ms: number | null;
  client: string | null;
  detail: string | null;
  user_id?: string | null;
  user_email?: string | null; // who asked (the signed-in person's email)
}

export interface AuditResponse {
  total: number;
  items: AuditEntry[];
}
