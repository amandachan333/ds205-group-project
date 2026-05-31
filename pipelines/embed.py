"""
Embedding pipeline.

Reads:   data/chunked/<company>/<doc_id>_chunks.jsonl
Embeds:  Qwen/Qwen3-Embedding-8B via NEBIUS (OpenAI-compatible API)
Stores:  SQLite at data/vector_store.db
Logs:    Per-batch token spend to logs/token_spend.jsonl

Schema (two tables):
    chunks            – chunk text and metadata
    chunk_embeddings  – chunk_id + 4096-dim float32 blob

NOTE — query-time embedding format:
    When embedding *query* strings for retrieval (not done here), Qwen3-Embedding
    expects an instruction prefix:
        "Instruct: <task description>\\nQuery: <query text>"
    Passage embeddings stored here use the raw text without any prefix.
    See the retrieval script for the correct format.

Dependencies:
    pip install openai python-dotenv

Usage:
    python pipelines/embed.py                         # all companies
    python pipelines/embed.py --company TNB           # one company
    python pipelines/embed.py --force                 # re-embed existing
    python pipelines/embed.py --batch-size 8          # smaller batches

Environment variables (place in .env):
    NEBIUS_API_KEY    Required.
    NEBIUS_BASE_URL   Optional (default: https://api.studio.nebius.com/v1/)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import struct
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI

from config import (
    CHUNKED_DIR,
    DB_PATH,
    EMBED_BATCH_SIZE,
    EMBED_COST_PER_1M_TOKENS,
    EMBED_RETRY_ATTEMPTS,
    EMBED_RETRY_SLEEP_S,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    LOG_DIR,
    NEBIUS_BASE_URL,
    TOKEN_SPEND_LOG,
)
from utils import bootstrap_runtime_env, ensure_stage_dirs, load_jsonl

bootstrap_runtime_env()  # loads .env before any network/torch imports

# ---------------------------------------------------------------------------
# Logging – writes to logs/embed.log AND stderr
# ---------------------------------------------------------------------------
ensure_stage_dirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "embed.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Compatibility hook retained for older docs; no-op in this build."""
    return None


def _serialize_float32(vector: list[float]) -> bytes:
    """Pack a list of Python floats as little-endian 32-bit floats."""
    return struct.pack(f"{len(vector)}f", *vector)


def init_db(db_path: Path) -> sqlite3.Connection:
    """
    Open (or create) the SQLite vector store and ensure the schema exists.
    Idempotent – safe to call on every run.

    Schema
    ------
    chunks
        chunk_id TEXT PK, company, document_id, year, chunk_index,
        page_number, text, metadata (JSON string)

    chunk_embeddings
        chunk_id TEXT PK, embedding BLOB (little-endian float32[4096])
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id    TEXT    PRIMARY KEY,
            company     TEXT    NOT NULL,
            document_id TEXT    NOT NULL,
            year        INTEGER,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER,
            text        TEXT    NOT NULL,
            metadata    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunk_embeddings (
            chunk_id  TEXT PRIMARY KEY,
            embedding BLOB NOT NULL
        );
    """)
    conn.commit()

    logger.info("Vector store ready at %s  (dim=%d)", db_path, EMBEDDING_DIM)
    return conn


# ---------------------------------------------------------------------------
# Token spend logging
# ---------------------------------------------------------------------------


def _append_spend_record(record: dict[str, Any]) -> None:
    """Append a single dict as one JSONL line to TOKEN_SPEND_LOG."""
    with TOKEN_SPEND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _log_batch_spend(
    *,
    run_id: str,
    company: str,
    document_id: str,
    batch_index: int,
    n_chunks: int,
    prompt_tokens: int,
    total_tokens: int,
) -> None:
    """Write one batch-level token-spend record."""
    _append_spend_record(
        {
            "record_type": "batch",
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "model": EMBEDDING_MODEL,
            "company": company,
            "document_id": document_id,
            "batch_index": batch_index,
            "n_chunks": n_chunks,
            "prompt_tokens": prompt_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(total_tokens / 1_000_000 * EMBED_COST_PER_1M_TOKENS, 6),
        }
    )


def _log_run_summary(
    *,
    run_id: str,
    total_chunks: int,
    total_prompt_tokens: int,
    total_total_tokens: int,
) -> None:
    """Write one run-level summary record."""
    _append_spend_record(
        {
            "record_type": "run_summary",
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "model": EMBEDDING_MODEL,
            "total_chunks_embedded": total_chunks,
            "total_prompt_tokens": total_prompt_tokens,
            "total_total_tokens": total_total_tokens,
            "cost_usd": round(total_total_tokens / 1_000_000 * EMBED_COST_PER_1M_TOKENS, 6),
        }
    )


# ---------------------------------------------------------------------------
# NEBIUS client and embedding
# ---------------------------------------------------------------------------


def _make_client() -> OpenAI:
    api_key = os.environ.get("NEBIUS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "NEBIUS_API_KEY is not set.  Add it to your .env file:\n"
            "    NEBIUS_API_KEY=your_key_here"
        )
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=api_key)


def _embed_batch_with_retry(
    client: OpenAI,
    texts: list[str],
    *,
    run_id: str,
    company: str,
    document_id: str,
    batch_index: int,
) -> tuple[list[list[float]], int, int]:
    """
    Call the NEBIUS embedding API for one batch of *texts*.

    Returns (embeddings, prompt_tokens, total_tokens).
    Retries up to EMBED_RETRY_ATTEMPTS times on transient errors with
    exponential back-off.  Raises on final failure.
    """
    last_exc: Exception | None = None

    for attempt in range(EMBED_RETRY_ATTEMPTS):
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            embeddings: list[list[float]] = [item.embedding for item in response.data]
            pt: int = response.usage.prompt_tokens
            tt: int = response.usage.total_tokens

            _log_batch_spend(
                run_id=run_id,
                company=company,
                document_id=document_id,
                batch_index=batch_index,
                n_chunks=len(texts),
                prompt_tokens=pt,
                total_tokens=tt,
            )
            return embeddings, pt, tt

        except Exception as exc:
            last_exc = exc
            wait = EMBED_RETRY_SLEEP_S * (2**attempt)
            logger.warning(
                "Attempt %d/%d failed for batch %d (%s): %s — retrying in %.1fs",
                attempt + 1,
                EMBED_RETRY_ATTEMPTS,
                batch_index,
                document_id,
                exc,
                wait,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"All {EMBED_RETRY_ATTEMPTS} attempts failed for {document_id} batch {batch_index}"
    ) from last_exc


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _already_embedded(conn: sqlite3.Connection, chunk_id: str) -> bool:
    """Return True if *chunk_id* already has an entry in chunk_embeddings."""
    row = conn.execute("SELECT 1 FROM chunk_embeddings WHERE chunk_id = ?", (chunk_id,)).fetchone()
    return row is not None


def _delete_embeddings_for_chunks(conn: sqlite3.Connection, chunk_ids: list[str]) -> None:
    """Remove existing rows for *chunk_ids* from both tables."""
    if not chunk_ids:
        return
    placeholders = ",".join("?" * len(chunk_ids))
    conn.execute(
        f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    )
    conn.execute(
        f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    )
    conn.commit()


def _write_batch_to_db(
    conn: sqlite3.Connection,
    records: list[dict],
    embeddings: list[list[float]],
    company: str,
    document_id: str,
) -> None:
    """
    Persist one batch of chunks + their embeddings in a single transaction.
    """
    with conn:
        for record, embedding in zip(records, embeddings, strict=True):
            chunk_id: str = record["chunk_id"]

            conn.execute(
                """
                INSERT OR REPLACE INTO chunks
                    (chunk_id, company, document_id, year, chunk_index,
                     page_number, text, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    record.get("company", company),
                    record.get("document_id", document_id),
                    record.get("year"),
                    record.get("chunk_index", 0),
                    record.get("page_number"),
                    record["text"],
                    json.dumps(record.get("metadata", {})),
                ),
            )

            conn.execute(
                "INSERT OR REPLACE INTO chunk_embeddings (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, _serialize_float32(embedding)),
            )


# ---------------------------------------------------------------------------
# File-level processing
# ---------------------------------------------------------------------------


def process_file(
    jsonl_path: Path,
    conn: sqlite3.Connection,
    client: OpenAI,
    run_id: str,
    batch_size: int = EMBED_BATCH_SIZE,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Embed every chunk in *jsonl_path* and store results in *conn*.

    Returns (n_embedded, total_prompt_tokens, total_total_tokens).
    """
    company: str = jsonl_path.parent.name
    document_id: str = jsonl_path.stem.replace("_chunks", "")

    logger.info("Embedding  %s / %s", company, jsonl_path.name)

    # Load all chunk records from the JSONL
    records: list[dict] = load_jsonl(jsonl_path)

    if not records:
        logger.warning("  No records in %s — skipping", jsonl_path.name)
        return 0, 0, 0

    # Determine which chunks actually need embedding
    if force:
        chunk_ids = [r["chunk_id"] for r in records]
        _delete_embeddings_for_chunks(conn, chunk_ids)
        to_embed = records
    else:
        to_embed = [r for r in records if not _already_embedded(conn, r["chunk_id"])]

    if not to_embed:
        logger.info(
            "  All %d chunks already embedded — skipped (use --force to redo)",
            len(records),
        )
        return 0, 0, 0

    logger.info("  Chunks to embed: %d / %d", len(to_embed), len(records))

    n_embedded = 0
    run_pt = 0
    run_tt = 0

    for batch_idx, start in enumerate(range(0, len(to_embed), batch_size)):
        batch = to_embed[start : start + batch_size]
        texts = [r["text"] for r in batch]

        try:
            embeddings, pt, tt = _embed_batch_with_retry(
                client,
                texts,
                run_id=run_id,
                company=company,
                document_id=document_id,
                batch_index=batch_idx,
            )
        except RuntimeError as exc:
            logger.error("  Skipping batch %d after all retries: %s", batch_idx, exc)
            continue

        _write_batch_to_db(conn, batch, embeddings, company, document_id)

        n_embedded += len(batch)
        run_pt += pt
        run_tt += tt

        logger.info(
            "  Batch %02d: %d chunks  |  cumulative: %d embedded, %d prompt tokens",
            batch_idx,
            len(batch),
            n_embedded,
            run_pt,
        )

        # Small pause to avoid hammering the rate limit
        time.sleep(0.3)

    logger.info(
        "  %s done — %d chunks, %d prompt tokens",
        document_id,
        n_embedded,
        run_pt,
    )
    return n_embedded, run_pt, run_tt


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embed chunks into SQLite vector store (Project C, DS205).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--chunked-dir",
        type=Path,
        default=CHUNKED_DIR,
        help="Root directory containing chunked JSONL files.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DB_PATH,
        help="Path to the SQLite vector store.",
    )
    parser.add_argument(
        "--company",
        type=str,
        default=None,
        help="Embed only this company subfolder (default: all companies).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=EMBED_BATCH_SIZE,
        help="Number of chunks per NEBIUS API call.",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Re-embed chunks that are already stored.",
    )
    args = parser.parse_args()

    run_id = str(uuid.uuid4())
    logger.info("Embed run started  run_id=%s  model=%s", run_id, EMBEDDING_MODEL)

    conn = init_db(args.db_path)
    client = _make_client()

    chunked_dir: Path = args.chunked_dir
    if not chunked_dir.exists():
        logger.error("Chunked directory not found: %s", chunked_dir)
        conn.close()
        return

    company_dirs: list[Path] = (
        [chunked_dir / args.company]
        if args.company
        else sorted(p for p in chunked_dir.iterdir() if p.is_dir())
    )

    grand_total_embedded = 0
    grand_total_pt = 0
    grand_total_tt = 0

    for company_dir in company_dirs:
        if not company_dir.is_dir():
            logger.warning("Directory not found: %s", company_dir)
            continue
        for jsonl_path in sorted(company_dir.glob("*_chunks.jsonl")):
            n, pt, tt = process_file(
                jsonl_path=jsonl_path,
                conn=conn,
                client=client,
                run_id=run_id,
                batch_size=args.batch_size,
                force=args.force,
            )
            grand_total_embedded += n
            grand_total_pt += pt
            grand_total_tt += tt

    conn.close()

    _log_run_summary(
        run_id=run_id,
        total_chunks=grand_total_embedded,
        total_prompt_tokens=grand_total_pt,
        total_total_tokens=grand_total_tt,
    )

    # Human-readable summary to stdout (print is acceptable here for UX)
    print(
        f"\n{'=' * 60}\n"
        f"  Embed run complete\n"
        f"  Run ID        : {run_id}\n"
        f"  Chunks stored : {grand_total_embedded}\n"
        f"  Prompt tokens : {grand_total_pt}\n"
        f"  Total tokens  : {grand_total_tt}\n"
        f"  Spend log     : {TOKEN_SPEND_LOG}\n"
        f"{'=' * 60}\n"
    )


if __name__ == "__main__":
    main()
