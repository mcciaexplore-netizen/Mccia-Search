"""Application settings loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
# The repository root (the folder that contains backend/ and staging/).
PROJECT_ROOT = BACKEND_DIR.parent

# Explicit paths, so .env is found whatever the current directory is (a bare
# load_dotenv() falls back to the current directory under `python -c` or a REPL).
# Real environment variables win, then backend/.env, then the root .env.
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(PROJECT_ROOT / ".env")


def _project_path(value: str) -> Path:
    """Expand `~` and anchor relative paths at PROJECT_ROOT, not the current directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


# Required: validate_config() raises ValueError if any of these is missing or blank.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Optional model names; a blank or missing value falls back to the default.
GEMINI_OCR_MODEL = os.environ.get("GEMINI_OCR_MODEL") or "gemini-3.8-flash"
GEMINI_CHAT_MODEL = os.environ.get("GEMINI_CHAT_MODEL") or "gemini-3.1-flash-lite"
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL") or "gemini-embedding-001"
DATABASE_URL = os.environ.get("DATABASE_URL")
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

# Optional. A blank value in .env falls back to the default.
GOOGLE_SERVICE_ACCOUNT_FILE = _project_path(
    os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "./credentials.json"
)
OCR_CACHE_DIR = _project_path(os.environ.get("OCR_CACHE_DIR") or "./staging/ocr_cache")
# "gemini" (paid API, the default) or a free engine that runs on this machine: "tesseract",
# "easyocr" or "paddle". ingest.ocr rejects any other value when OCR starts, so a typo
# cannot quietly fall back to the paid engine.
OCR_ENGINE = (os.environ.get("OCR_ENGINE") or "").strip().lower() or "gemini"
# Where the "View page" images are kept once rendered (pages.py trims it to a size limit).
PAGE_CACHE_DIR = _project_path(os.environ.get("PAGE_CACHE_DIR") or "./staging/page_cache")


def validate_config() -> None:
    """Raise ValueError naming every required variable that is not set."""
    required = {
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "DATABASE_URL": DATABASE_URL,
        "GOOGLE_DRIVE_FOLDER_ID": GOOGLE_DRIVE_FOLDER_ID,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
