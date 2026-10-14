"""Audit log: who asked what and when, and what happened to each sync."""

from fastapi import Request

from db.connection import get_pooled_connection

MAX_QUERY_CHARS = 2000
MAX_DETAIL_CHARS = 500
MAX_CLIENT_CHARS = 100

_COLUMNS = "id, occurred_at, event, query, found, sources, status, duration_ms, client, detail"


def client_of(request: Request) -> str:
    """The caller's address: the first X-Forwarded-For entry (the web app's proxy sets it), else the peer."""
    first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    peer = request.client.host if request.client else ""
    return (first or peer or "unknown")[:MAX_CLIENT_CHARS]


def record(
    event: str,
    *,
    query: str | None = None,
    found: bool | None = None,
    sources: int | None = None,
    status: int | None = None,
    duration_ms: int | None = None,
    client: str | None = None,
    detail: str | None = None,
) -> None:
    """Write one audit row. Never raises: a logging problem must not break a request."""
    try:
        with get_pooled_connection() as conn:
            conn.execute(
                "INSERT INTO audit_log (event, query, found, sources, status, duration_ms, client, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event,
                    query[:MAX_QUERY_CHARS] if query is not None else None,
                    found,
                    sources,
                    status,
                    duration_ms,
                    client[:MAX_CLIENT_CHARS] if client is not None else None,
                    detail[:MAX_DETAIL_CHARS] if detail is not None else None,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] WARNING: could not record a {event} event ({type(exc).__name__})")


def recent(limit: int, offset: int, event: str | None = None) -> dict:
    """The newest audit rows first, and the total number of rows that match."""
    where = "WHERE event = %s" if event else ""
    params = [event] if event else []
    with get_pooled_connection() as conn:
        total = conn.execute(f"SELECT count(*) FROM audit_log {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM audit_log {where} "
            "ORDER BY occurred_at DESC, id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        ).fetchall()
    keys = [c.strip() for c in _COLUMNS.split(",")]
    items = []
    for row in rows:
        item = dict(zip(keys, row))
        item["occurred_at"] = item["occurred_at"].isoformat()
        items.append(item)
    return {"total": total, "items": items}
