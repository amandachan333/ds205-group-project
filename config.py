"""
Central configuration for the RAG pipeline.

All tunable constants live here so they are changed in one place and
visible to every stage script (extract → chunk → embed → retrieve).
Design rationale for the values below is in DECISIONS.md.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------
RAW_DIR: Path = Path("data/raw")
EXTRACTED_DIR: Path = Path("data/extracted")
CHUNKED_DIR: Path = Path("data/chunked")
DB_PATH: Path = Path("data/vector_store.db")
LOG_DIR: Path = Path("logs")

# Token-spend audit trail (appended to by embed.py)
TOKEN_SPEND_LOG: Path = LOG_DIR / "token_spend.jsonl"

# ---------------------------------------------------------------------------
# Chunking  (mirrors TPI CLEAR production defaults – see DECISIONS.md)
# ---------------------------------------------------------------------------
MAX_CHUNK_SIZE: int = 1_000    # target max chars before starting a new chunk
SENTENCE_OVERLAP: int = 2      # sentences carried forward into the next chunk
MIN_CHUNK_LENGTH: int = 100    # merge chunks shorter than this (chars)
MAX_CHUNK_LENGTH: int = 2_000  # hard-split threshold (chars)

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-8B"
EMBEDDING_DIM: int = 4096
EMBED_BATCH_SIZE: int = 16     # conservative default for an 8B model
EMBED_RETRY_ATTEMPTS: int = 3
EMBED_RETRY_SLEEP_S: float = 5.0
# NEBIUS pricing for Qwen3-Embedding-8B (USD per 1M tokens, input only)
EMBED_COST_PER_1M_TOKENS: float = 0.01

# NEBIUS base URL
NEBIUS_BASE_URL: str = os.environ.get(
    "NEBIUS_BASE_URL", "https://api.studio.nebius.com/v1/"
)