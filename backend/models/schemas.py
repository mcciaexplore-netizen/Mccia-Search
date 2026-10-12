"""Pydantic models for API request and response bodies."""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    query: str
    top_k: int = 5


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
