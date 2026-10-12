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
