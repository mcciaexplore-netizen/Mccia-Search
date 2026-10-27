"""FastAPI application entry point for the MCCIA RAG chatbot."""

import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import audit
import auth
import pages
import sso
from config import validate_config
from db.connection import close_pool, get_connection, get_pool, get_pooled_connection, init_db
from generation.answer import rag
from ingest.drive_sync import _chronological_key
from ingest.pipeline import is_sync_running, run_pipeline
from models.schemas import (
    AuditResponse,
    ChatRequest,
    ChatResponse,
    DocumentsResponse,
    HealthResponse,
    IngestStatusResponse,
    IngestTriggerResponse,
    SourceItem,
    SsoLoginRequest,
    SsoLoginResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    auth.check_configured()  # no keys, no start: the API must never run open
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


# No /docs, /redoc or /openapi.json: only /health may be reached without a key.
app = FastAPI(title="MCCIA Search", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3100"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok", service="mccia-search")


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(auth.require_api_key)])
def chat(
    req: ChatRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: auth.User = Depends(auth.require_user),
):
    started = time.perf_counter()
    client = audit.client_of(request)

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    if not req.query.strip():
        audit.record("chat", query=req.query, status=400, duration_ms=elapsed_ms(), client=client, detail="blank question", user_id=user.id, user_email=user.email)
        raise HTTPException(status_code=400, detail="query must not be blank")
    try:
        result = rag(req.query, req.top_k)
        response = ChatResponse(
            answer=result["answer"],
            sources=[SourceItem(**s) for s in result["sources"]],
            found=result["found"],
        )
    except Exception as exc:
        print(f"[chat] error: {exc}")
        audit.record("chat", query=req.query, status=500, duration_ms=elapsed_ms(), client=client, detail=type(exc).__name__, user_id=user.id, user_email=user.email)
        raise HTTPException(
            status_code=500,
            detail="An error occurred while processing your request.",
        )
    # Written after the answer is sent, so logging never slows the reply.
    background_tasks.add_task(
        audit.record,
        "chat",
        query=req.query,
        found=response.found,
        sources=len(response.sources),
        status=200,
        duration_ms=elapsed_ms(),
        client=client,
        user_id=user.id,
        user_email=user.email,
    )
    return response


@app.post("/sso/login", response_model=SsoLoginResponse, dependencies=[Depends(auth.require_admin_key)])
def sso_login(req: SsoLoginRequest):
    """Record a hub sign-in the web app has verified: spend its token id once and keep the user up to date.

    Called by the web app's server with the admin key, never by a browser. A token that was already used is refused
    with 409.
    """
    try:
        return sso.register_login(
            jti=req.jti, exp=req.exp, hub_user_id=req.hub_user_id, email=req.email, name=req.name, role=req.role
        )
    except sso.TokenAlreadyUsed:
        raise HTTPException(status_code=409, detail="This sign-in link was already used.")
    except sso.TokenExpiryInvalid:
        raise HTTPException(status_code=400, detail="This sign-in link is not valid.")


def run_sync_and_record() -> None:
    """Run a sync and write its outcome to the audit log."""
    started = time.perf_counter()
    try:
        result = run_pipeline() or {}
    except Exception as exc:
        audit.record(
            "sync", query="Sync failed", status=500,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise
    audit.record(
        "sync", query="Sync finished", status=200,
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=(
            f"{result.get('ingested', 0)} ingested, {result.get('skipped', 0)} skipped, "
            f"{result.get('failed', 0)} failed"
        ),
    )


@app.post("/ingest/sync", response_model=IngestTriggerResponse, dependencies=[Depends(auth.require_admin_key)])
def ingest_sync(background_tasks: BackgroundTasks, request: Request):
    client = audit.client_of(request)
    if is_sync_running():
        background_tasks.add_task(
            audit.record, "sync", query="Sync requested", status=200, client=client, detail="already running"
        )
        return IngestTriggerResponse(status="started", message="A sync is already running")
    background_tasks.add_task(
        audit.record, "sync", query="Sync requested", status=200, client=client, detail="started"
    )
    background_tasks.add_task(run_sync_and_record)
    return IngestTriggerResponse(
        status="started", message="Sync started in background"
    )


@app.get("/ingest/status", response_model=IngestStatusResponse, dependencies=[Depends(auth.require_admin_key)])
def ingest_status():
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE status = 'completed') AS completed,
              COUNT(*) FILTER (WHERE status = 'failed') AS failed,
              COUNT(*) FILTER (WHERE status IN ('pending','processing','text_saved')) AS pending
            FROM documents
            """
        ).fetchone()
    return IngestStatusResponse(
        total=row[0], completed=row[1], failed=row[2], pending=row[3]
    )


@app.get("/documents", response_model=DocumentsResponse, dependencies=[Depends(auth.require_admin_key)])
def documents():
    """Every document the pipeline has seen, oldest issue first, with its page and chunk counts."""
    with get_pooled_connection() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.filename, d.status, d.total_pages, d.language, d.ingested_at, COUNT(c.id)
            FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
            GROUP BY d.id
            """
        ).fetchall()
    items = [
        {
            "id": r[0], "filename": r[1], "status": r[2], "total_pages": r[3],
            "language": r[4], "added": r[5].isoformat(), "chunks": r[6],
        }
        for r in rows
    ]
    items.sort(key=lambda d: _chronological_key({"name": d["filename"]}))
    return {
        "summary": {
            "total": len(items),
            "completed": sum(d["status"] == "completed" for d in items),
            "failed": sum(d["status"] == "failed" for d in items),
            "pending": sum(d["status"] in ("pending", "processing", "text_saved") for d in items),
            "chunks": sum(d["chunks"] for d in items),
        },
        "documents": items,
    }


# The chat shows these, so they take the ordinary key, not the admin one.
@app.get("/documents/{document_id}/pages/{page_number}", dependencies=[Depends(auth.require_api_key)])
def document_page(document_id: int, page_number: int):
    """One page of a document as a JPEG, so a source can be viewed as it was printed.

    The first view of an issue downloads its PDF from Google Drive and can take a while;
    after that the page is served from the cache.
    """
    try:
        path = pages.get_page_image(document_id, page_number)
    except pages.PageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except pages.PageUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/audit", response_model=AuditResponse, dependencies=[Depends(auth.require_admin_key)])
def audit_log(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event: str | None = Query(None, pattern="^(chat|sync)$"),
):
    """The audit log, newest first. `event` limits it to questions ("chat") or syncs ("sync")."""
    return audit.recent(limit, offset, event)
