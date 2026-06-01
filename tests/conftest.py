"""
pytest configuration and shared fixtures.

This file does two jobs:

1. **sys.path setup** — adds the project root to sys.path so tests can import
   `config`, `utils`, etc. directly, the same way the pipeline scripts do.

2. **`chunk_module` fixture** — loads `pipelines/chunk.py` via importlib rather
   than a normal `import chunk`.  Two reasons:
     - `chunk` is the name of a (deprecated) Python stdlib module, and we want
       to be sure we never accidentally pick up the stdlib one.
     - Importing the chunker has side effects (creates `logs/` directory,
       opens a log file handle).  Doing the import inside a session-scoped
       fixture means it happens exactly once per test session.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — runs at conftest import time (before any test collection)
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
PIPELINES_DIR: Path = PROJECT_ROOT / "pipelines"

# Project root first so `import config` and `import utils` resolve to the
# top-level files used by the pipeline scripts.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def chunk_module():
    """
    Load `pipelines/chunk.py` once per test session and yield the module.

    Uses spec_from_file_location with a non-stdlib module name to bypass any
    risk of picking up Python's deprecated `chunk` stdlib module.
    """
    chunk_path = PIPELINES_DIR / "chunk.py"
    if not chunk_path.exists():
        pytest.skip(f"chunk.py not found at {chunk_path}")

    spec = importlib.util.spec_from_file_location("project_chunk", chunk_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def schema_sql() -> str:
    """
    Read schema.sql from its likely location(s) and return its contents.

    Searches both `db/schema.sql` (per the schema file's own header comment)
    and `schema.sql` at the project root.
    """
    candidates = [
        PROJECT_ROOT / "db" / "schema.sql",
        PROJECT_ROOT / "schema.sql",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    pytest.skip(f"schema.sql not found at any of: {[str(p) for p in candidates]}")
