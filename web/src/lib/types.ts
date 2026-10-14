export interface Source {
  filename: string;
  page_number: number | null;
  similarity: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  found?: boolean;
  timestamp: Date;
}

export interface ChatResponse {
  answer: string;
  sources: Source[];
  found: boolean;
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
}

export interface AuditResponse {
  total: number;
  items: AuditEntry[];
}
