"""Pydantic models for API request and response bodies."""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    query: str
    top_k: int | None = None  # None: the server's default (search.TOP_K)


class SourceItem(BaseModel):
    filename: str
    page_number: int | None = None
    similarity: float


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    found: bool


class IngestStatusResponse(BaseModel):
    total: int
    completed: int
    failed: int
    pending: int


class IngestTriggerResponse(BaseModel):
    status: str
    message: str


class HealthResponse(BaseModel):
    status: str
    service: str


class DocumentItem(BaseModel):
    id: int
    filename: str
    status: str
    total_pages: int | None = None
    language: str | None = None
    chunks: int
    added: str  # ISO timestamp


class DocumentsSummary(BaseModel):
    total: int
    completed: int
    failed: int
    pending: int
    chunks: int


class DocumentsResponse(BaseModel):
    summary: DocumentsSummary
    documents: list[DocumentItem]


class AuditEntry(BaseModel):
    id: int
    occurred_at: str  # ISO timestamp
    event: str
    query: str | None = None
    found: bool | None = None
    sources: int | None = None
    status: int | None = None
    duration_ms: int | None = None
    client: str | None = None
    detail: str | None = None


class AuditResponse(BaseModel):
    total: int
    items: list[AuditEntry]
