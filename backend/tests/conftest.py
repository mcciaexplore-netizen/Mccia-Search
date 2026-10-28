import sys
from pathlib import Path

# The backend modules import each other by plain name (from db.connection import ...), so tests run with backend/ on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
