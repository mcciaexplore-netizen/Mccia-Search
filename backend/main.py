"""FastAPI application entry point for the MCCIA RAG chatbot."""

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import validate_config
from db.connection import close_pool, get_connection, get_pool, init_db
from generation.answer import rag
from ingest.pipeline import is_sync_running, run_pipeline
from models.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    IngestStatusResponse,
    IngestTriggerResponse,
    SourceItem,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    init_db()
    # A run that crashed or was killed leaves its document 'processing' for good.
    with get_connection() as conn:
        stuck = conn.execute(
            "UPDATE documents SET status = 'failed' "
            "WHERE status = 'processing' "
            "RETURNING id, filename"
        ).fetchall()
        conn.commit()
    if stuck:
        print(f"[startup] reset {len(stuck)} stuck documents to failed:")
        for row in stuck:
            print(f"  id={row[0]} {row[1]}")
    get_pool()  # warm up the pool on startup
    print("[startup] database pool ready")
    print("[startup] mccia-search ready")
    yield
    close_pool()


app = FastAPI(title="MCCIA Search", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3100"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok", service="mccia-search")


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be blank")
    try:
        result = rag(req.query, req.top_k)
        return ChatResponse(
            answer=result["answer"],
            sources=[SourceItem(**s) for s in result["sources"]],
            found=result["found"],
        )
    except Exception as exc:
        print(f"[chat] error: {exc}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while processing your request.",
        )


@app.post("/ingest/sync", response_model=IngestTriggerResponse)
def ingest_sync(background_tasks: BackgroundTasks):
    if is_sync_running():
        return IngestTriggerResponse(status="started", message="A sync is already running")
    background_tasks.add_task(run_pipeline)
    return IngestTriggerResponse(
        status="started", message="Sync started in background"
    )


@app.get("/ingest/status", response_model=IngestStatusResponse)
def ingest_status():
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE status = 'completed') AS completed,
              COUNT(*) FILTER (WHERE status = 'failed') AS failed,
              COUNT(*) FILTER (WHERE status IN ('pending','processing')) AS pending
            FROM documents
            """
        ).fetchone()
    return IngestStatusResponse(
        total=row[0], completed=row[1], failed=row[2], pending=row[3]
    )
