"""
single_shot_rag.py
------------------
Single-shot RAG baseline.

Retrieves top-N chunks with the BM25 + dense hybrid retriever from
`pipelines/retrieval.py`, selects the best top-M chunks (with a company-aware
boost for multi-company questions), builds a citation-ready prompt, and asks
a Nebius-hosted Qwen model to answer in one pass.

This script logs each run to `logs/rag_runs.jsonl` and reuses the existing
vector store at `data/vector_store.db`. Behaviour is unchanged from the
pre-refactor version - the shared retrieval / formatting / cost helpers have
simply been moved into `retrieval.py` so the multi-step pipeline can call
them identically.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from config import DB_PATH, LOG_DIR
from db import database as database
from utils import bootstrap_runtime_env, ensure_stage_dirs

from retrieval import (
    BM25_WEIGHT,
    RRF_K,
    calculate_generation_cost,
    check_budget,
    extract_citations,
    format_context,
    hybrid_retrieve,
    load_index,
    log_generation_spend,
    make_client,
    remove_stopwords,
    select_context_chunks,
)

bootstrap_runtime_env()
ensure_stage_dirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "single_shot_rag.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

DEFAULT_TOP_N = 20
DEFAULT_CONTEXT_CHUNKS = 10
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_MODEL = os.environ.get("RAG_GENERATION_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")
RAG_RUNS_LOG = LOG_DIR / "rag_runs.jsonl"

SINGLE_SHOT_SYSTEM_PROMPT = (
    "You are an expert analyst specialising in corporate carbon performance and emissions reporting. "
    "You answer questions about companies' emissions intensity, reduction targets, and climate alignment using evidence retrieved from their sustainability and annual reports."
)

SINGLE_SHOT_USER_PROMPT = """You will be given:
- A question about a company's carbon performance
- A set of retrieved passages from company reports

Rules:
- Answer only from the retrieved passages. Do not use prior knowledge about any company.
- If the retrieved passages do not contain sufficient information to answer the question, say so explicitly rather than guessing.
- For every factual claim, cite the source document and page number in parentheses, e.g. (2023 Annual Report, p.45).
- If the question requires a calculation (e.g. percentage change), show your working clearly.
- Be concise. Do not pad the answer with background context that was not asked for.

Retrieved passages:
{retrieved_chunks}

Question: {question}"""


# ---------------------------------------------------------------------------
# Single-shot specific helpers
# ---------------------------------------------------------------------------

def _build_messages(question: str, context: str) -> list[dict[str, str]]:
    """Construct the chat prompt for the single-shot answer."""
    user_prompt = SINGLE_SHOT_USER_PROMPT.format(
        retrieved_chunks=context,
        question=question,
    )
    return [
        {"role": "system", "content": SINGLE_SHOT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _run_generation(
    client,
    model: str,
    question: str,
    context: str,
    temperature: float,
    max_output_tokens: int,
) -> tuple[str, dict[str, int]]:
    """Call the generation model and return the answer plus token usage."""
    response = client.chat.completions.create(
        model=model,
        messages=_build_messages(question, context),
        temperature=temperature,
        max_tokens=max_output_tokens,
    )
    answer = (response.choices[0].message.content or "").strip()
    usage = response.usage
    token_usage = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
    return answer, token_usage


def _append_run_log(record: dict[str, Any]) -> None:
    """Append one JSONL record to the shared single-shot run log."""
    RAG_RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RAG_RUNS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a single-shot RAG answer over the existing vector store.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to vector_store.db produced by embed.py.")
    parser.add_argument("--question", type=str, required=True, help="Question to answer in one pass.")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help="Nebius generation model name.")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="Number of chunks to retrieve before selecting context.")
    parser.add_argument("--context-chunks", type=int, default=DEFAULT_CONTEXT_CHUNKS, help="Number of retrieved chunks to include in the prompt.")
    parser.add_argument("--bm25-weight", type=int, default=BM25_WEIGHT, help="Relative BM25 weight in RRF fusion.")
    parser.add_argument("--rrf-k", type=int, default=RRF_K, help="Reciprocal rank fusion constant.")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="Sampling temperature for the generation model.")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS, help="Maximum output tokens for the generation model.")
    parser.add_argument("--max-chars-per-chunk", type=int, default=1400, help="Truncate each chunk to this many characters before prompting.")
    parser.add_argument("--verbose", action="store_true", help="Print retrieved chunks before generation.")
    parser.add_argument("--dry-run", action="store_true", help="Run retrieval and assemble prompt, but do not call the generation API.")
    args = parser.parse_args()

    index = load_index(args.db)
    start_time = time.perf_counter()

    if args.dry_run:
        # BM25-only retrieval for a dry run - no API keys needed.
        bm25_scores = np.array(index.bm25.get_scores(remove_stopwords(args.question)))
        top_idxs = np.argsort(bm25_scores)[::-1][: max(args.top_n, args.context_chunks)]
        retrieved = []
        for idx in top_idxs:
            chunk = dict(index.chunks[idx])
            chunk["bm25_score"] = float(bm25_scores[idx])
            chunk["dense_score"] = 0.0
            chunk["rrf_score"] = float(chunk["bm25_score"])
            retrieved.append(chunk)
        client = None
    else:
        client = make_client()
        retrieved = hybrid_retrieve(
            query=args.question,
            client=client,
            index=index,
            k=max(args.top_n, args.context_chunks),
            bm25_weight=args.bm25_weight,
            rrf_k=args.rrf_k,
        )

    context_chunks = select_context_chunks(args.question, retrieved, args.context_chunks)
    context = format_context(context_chunks, args.max_chars_per_chunk)

    if args.verbose:
        log.info("Retrieved context chunks:")
        for chunk in context_chunks:
            log.info(
                "- %s | %s | page %s",
                chunk["chunk_id"],
                chunk.get("document_id"),
                chunk.get("page_number", "?"),
            )

    if args.dry_run:
        messages = _build_messages(args.question, context)
        log.info("--- System message ---\n%s", messages[0]["content"])
        log.info("--- User message ---\n%s", messages[1]["content"])
        log.info("Dry run complete - no generation call was made.")
        return

    # Real generation path
    answer, token_usage = _run_generation(
        client=client,
        model=args.model,
        question=args.question,
        context=context,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
    )
    latency_seconds = time.perf_counter() - start_time
    citations = extract_citations(answer)
    run_uuid = str(uuid.uuid4())

    log.info("Answer:\n%s", answer)

    # JSONL audit record (human-friendly)
    record = {
        "run_id": run_uuid,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "question": args.question,
        "model": args.model,
        "db_path": str(args.db),
        "top_n": args.top_n,
        "context_chunks": args.context_chunks,
        "selected_chunk_ids": [chunk["chunk_id"] for chunk in context_chunks],
        "selected_document_ids": [chunk.get("document_id") for chunk in context_chunks],
        "selected_pages": [chunk.get("page_number") for chunk in context_chunks],
        "citations": citations,
        "answer": answer,
        "token_usage": token_usage,
        "latency_seconds": round(latency_seconds, 3),
    }
    _append_run_log(record)
    log.info("Saved run to %s", RAG_RUNS_LOG)

    # Persist to SQLite using db/database.py
    conn = database.get_connection()
    try:
        database.init_db(conn)
        question_id = str(uuid.uuid4())
        database.insert_question(conn, question_id, args.question)
        run_id = database.create_run(conn, question_id, "single_shot", args.model)

        prompt_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(token_usage.get("completion_tokens", 0) or 0)
        run_cost = log_generation_spend(
            question_id,
            args.model,
            prompt_tokens,
            completion_tokens,
            extra={"phase": "single_shot", "run_id": run_id},
        )
        total_tokens = prompt_tokens + completion_tokens

        database.insert_final_answer(conn, run_id, answer)
        database.complete_run(conn, run_id, latency_seconds, int(total_tokens), run_cost)

        check_budget("single_shot")
    finally:
        conn.close()

    log.info(
        "Completed single-shot run | latency=%.2fs | tokens=%d",
        latency_seconds,
        token_usage["total_tokens"],
    )


if __name__ == "__main__":
    main()