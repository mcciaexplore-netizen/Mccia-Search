"""Pydantic models for API request and response bodies."""

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    query: str
    top_k: int | None = None  # None: the server's default (search.TOP_K)


class SourceItem(BaseModel):
    filename: str
    page_number: int | None = None
    document_id: int | None = None  # lets the client link to GET /documents/{id}/pages/{page}
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
    user_id: str | None = None
    user_email: str | None = None


class AuditResponse(BaseModel):
    total: int
    items: list[AuditEntry]


class SsoLoginRequest(BaseModel):
    """A verified hub sign-in, sent by the web app's server (never a browser) to record it."""

    jti: str = Field(min_length=8, max_length=100)
    exp: int  # when the token expires, seconds since 1970
    hub_user_id: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    name: str = Field(min_length=1, max_length=200)
    role: Literal["user", "admin"]


class SsoLoginResponse(BaseModel):
    id: int
    hub_user_id: str
    email: str
    name: str
    role: str
    first_seen: str
    last_seen: str
