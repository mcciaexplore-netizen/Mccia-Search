"""The photo search's long-lived parts inside the running backend: the storage client and CLIP's text side, each made once, on first use."""

import threading

from photos.clip_text import ClipText
from photos.store import SupabaseStore, from_env

_lock = threading.Lock()
_store: SupabaseStore | None = None
_store_checked = False
_clip: ClipText | None = None


def get_store() -> SupabaseStore | None:
    """The storage client, or None if this server has no Supabase service-role key (photo features are then off, and nothing else is affected)."""
    global _store, _store_checked
    with _lock:
        if not _store_checked:
            _store, _store_checked = from_env(), True
        return _store


def get_clip() -> ClipText:
    global _clip
    with _lock:
        if _clip is None:
            _clip = ClipText()
        return _clip


def warm_up() -> None:
    """Load CLIP's text side now (a few seconds), so the first photo request is not slow. Never raises: photo search is optional."""
    try:
        get_clip().warm_up()
        print("[startup] photo search ready (CLIP text side loaded)" + ("" if get_store() else "; WARNING: no Supabase key, photos cannot be shown"))
    except Exception as exc:
        print(f"[startup] photo search unavailable: {type(exc).__name__}: {str(exc)[:200]}")
