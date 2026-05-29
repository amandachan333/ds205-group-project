"""
single_shot_rag.py
------------------
Single-shot RAG baseline.

Retrieves top-N chunks with the existing BM25 + dense hybrid retriever,
selects the best top-M chunks, builds a citation-ready prompt, and asks a
Nebius-hosted Qwen model to answer in one pass.

The script logs each run to logs/rag_runs.jsonl and reuses the existing
vector store at data/vector_store.db.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from rank_bm25 import BM25Okapi
from openai import OpenAI

from config import (
    DB_PATH,
    LOG_DIR,
    NEBIUS_BASE_URL,
    TOKEN_SPEND_LOG,
    GENERATION_COST_PER_1M_TOKENS,
    GENERATION_COST_RATES,
)
from db import database as database
from utils import bootstrap_runtime_env, ensure_stage_dirs

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
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"
EMBEDDING_DIM = 4096
BM25_WEIGHT = 1
RRF_K = 60
STOPWORDS = {
    "what", "are", "the", "for", "is", "a", "an", "of", "in",
    "to", "how", "much", "did", "this", "that", "and", "or", "by",
    "which", "their", "each", "between", "from", "its", "both",
}

COMPANY_ALIASES = {
    "TNB": ("tnb", "tenaga nasional"),
    "DEWA": ("dewa",),
    "Centerpoint": ("centerpoint", "center point"),
}

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


def _deserialize_float32(blob: bytes) -> list[float]:
    """Decode a little-endian float32 BLOB back into Python floats."""
    return list(np.frombuffer(blob, dtype="<f4"))


def _make_client() -> OpenAI:
    """Create the Nebius OpenAI-compatible client used for generation."""
    api_key = os.environ.get("NEBIUS_API_KEY")
    if not api_key:
        sys.exit(
            "NEBIUS_API_KEY not set. Add it to your .env file or environment."
        )
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=api_key)


def _remove_stopwords(text: str) -> list[str]:
    return [token for token in text.lower().split() if token not in STOPWORDS]


def load_chunks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT chunk_id, company, document_id, year, chunk_index, page_number, text, metadata
        FROM chunks
        ORDER BY company, document_id, year, chunk_index, chunk_id
        """
    ).fetchall()
    chunks: list[dict] = []
    for row in rows:
        chunks.append({
            "chunk_id": row[0],
            "company": row[1],
            "document_id": row[2],
            "year": row[3],
            "chunk_index": row[4],
            "page_number": row[5],
            "text": row[6],
            "metadata": json.loads(row[7]) if row[7] else {},
        })
    log.info("Loaded %d chunks from vector store", len(chunks))
    return chunks


def load_embeddings(conn: sqlite3.Connection, chunk_ids: list[str]) -> dict[str, list[float]]:
    log.info("Loading %d embeddings from sqlite-vec …", len(chunk_ids))
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT chunk_id, embedding FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    result: dict[str, list[float]] = {}
    for chunk_id, blob in rows:
        result[chunk_id] = _deserialize_float32(blob)
    log.info("Loaded %d embeddings", len(result))
    return result


def embed_query(client: OpenAI, query: str) -> np.ndarray:
    """Embed the query using the same instruction prefix as retrieval evaluation."""
    instruction = (
        "Instruct: Given a question, retrieve passages that answer the question\n"
        f"Query: {query}"
    )
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[instruction],
    )
    vec = np.array(response.data[0].embedding, dtype=np.float32)
    vec /= np.linalg.norm(vec) + 1e-10
    return vec


class RetrievalIndex:
    def __init__(self, chunks: list[dict], embeddings: np.ndarray, bm25: BM25Okapi) -> None:
        self.chunks = chunks
        self.embeddings = embeddings
        self.bm25 = bm25


def build_index(chunks: list[dict], embeddings_map: dict[str, list[float]]) -> RetrievalIndex:
    log.info("Building BM25 index …")
    tokenised = [_remove_stopwords(chunk["text"]) for chunk in chunks]
    bm25 = BM25Okapi(tokenised)

    log.info("Aligning embedding matrix …")
    emb_matrix = np.zeros((len(chunks), EMBEDDING_DIM), dtype=np.float32)
    missing = 0
    for index, chunk in enumerate(chunks):
        vector = embeddings_map.get(chunk["chunk_id"])
        if vector is not None:
            emb_matrix[index] = vector
        else:
            missing += 1
    if missing:
        log.warning("%d chunks have no embedding (will score 0 in dense retrieval)", missing)

    norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True) + 1e-10
    emb_matrix /= norms
    return RetrievalIndex(chunks=chunks, embeddings=emb_matrix, bm25=bm25)


def hybrid_retrieve(
    query: str,
    client: OpenAI,
    index: RetrievalIndex,
    k: int,
    bm25_weight: int = BM25_WEIGHT,
    rrf_k: int = RRF_K,
) -> list[dict]:
    """BM25 + dense hybrid retrieval with RRF fusion."""
    dense_query = embed_query(client, query)
    dense_scores = index.embeddings @ dense_query
    dense_ranks = np.argsort(dense_scores)[::-1]

    bm25_tokens = _remove_stopwords(query)
    bm25_scores = np.array(index.bm25.get_scores(bm25_tokens))
    bm25_ranks = np.argsort(bm25_scores)[::-1]

    rrf_scores = np.zeros(len(index.chunks), dtype=np.float64)
    for rank, idx in enumerate(dense_ranks):
        rrf_scores[idx] += 1.0 / (rrf_k + rank + 1)
    for rank, idx in enumerate(bm25_ranks):
        rrf_scores[idx] += bm25_weight / (rrf_k + rank + 1)

    top_indices = np.argsort(rrf_scores)[::-1][:k]
    results: list[dict] = []
    for idx in top_indices:
        chunk = dict(index.chunks[idx])
        chunk["rrf_score"] = float(rrf_scores[idx])
        chunk["dense_score"] = float(dense_scores[idx])
        chunk["bm25_score"] = float(bm25_scores[idx])
        results.append(chunk)
    return results


def _load_index(db_path: Path) -> RetrievalIndex:
    """Load chunks and embeddings from the vector store into an in-memory index."""
    if not db_path.exists():
        sys.exit(f"Vector store not found: {db_path}\nRun embed.py first.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        chunks = load_chunks(conn)
        if not chunks:
            sys.exit("No chunks found in vector store.")

        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        embeddings_map = load_embeddings(conn, chunk_ids)
    finally:
        conn.close()

    return build_index(chunks, embeddings_map)


def _format_document_label(chunk: dict) -> str:
    """Render a human-readable source label for citation prompts."""
    company = str(chunk.get("company", "")).strip()
    year = chunk.get("year")
    if company == "Centerpoint":
        return f"CenterPoint Energy {year} Corporate Sustainability Report"
    if company == "TNB":
        return f"TNB Sustainability Report {year}"
    if company == "DEWA":
        return f"DEWA Sustainability Report {year}"
    if company:
        return f"{company} {year} Report"
    return str(chunk.get("document_id", "source"))


def _format_context(chunks: list[dict], max_chars_per_chunk: int) -> str:
    """Render retrieved chunks in the exact citation-friendly format from prompts.md."""
    blocks: list[str] = []
    for rank, chunk in enumerate(chunks, 1):
        text = chunk.get("text", "")
        if max_chars_per_chunk > 0 and len(text) > max_chars_per_chunk:
            text = text[:max_chars_per_chunk].rstrip() + "..."

        blocks.append(
            "\n".join(
                [
                    f"({ _format_document_label(chunk) }, p.{chunk.get('page_number', '?')})",
                    f"chunk_id={chunk.get('chunk_id', '')}",
                    "text:",
                    text,
                ]
            )
        )

    return "\n\n".join(blocks)


def _mentioned_companies(question: str) -> list[str]:
    question_lower = question.lower()
    mentioned: list[str] = []
    for company, aliases in COMPANY_ALIASES.items():
        if any(alias in question_lower for alias in aliases):
            mentioned.append(company)
    return mentioned


def _select_context_chunks(question: str, retrieved: list[dict], max_chunks: int) -> list[dict]:
    mentioned = _mentioned_companies(question)
    if len(mentioned) < 2:
        return retrieved[:max_chunks]

    selected: list[dict] = []
    seen_ids: set[str] = set()

    for company in mentioned:
        for chunk in retrieved:
            if chunk.get("company") == company and chunk.get("chunk_id") not in seen_ids:
                selected.append(chunk)
                seen_ids.add(chunk["chunk_id"])
                break

    for chunk in retrieved:
        chunk_id = chunk.get("chunk_id")
        if chunk_id not in seen_ids:
            selected.append(chunk)
            seen_ids.add(chunk_id)
        if len(selected) >= max_chunks:
            break

    return selected[:max_chunks]


def _extract_citations(answer: str) -> list[str]:
    """Return parenthetical source citations mentioned in the model answer."""
    citations: list[str] = []
    for match in re.finditer(r"\(([^()]+?),\s*p\.?\s*(\d+)\)", answer):
        citation = f"{match.group(1).strip()}, p.{match.group(2)}"
        if citation not in citations:
            citations.append(citation)
    return citations


def _log_generation_spend(
    question_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Append one generation spend record to the shared token_spend.jsonl and return computed cost.

    Cost is computed using per-model input/output rates from `GENERATION_COST_RATES`.
    Falls back to `GENERATION_COST_PER_1M_TOKENS` if model not listed.
    """
    from datetime import datetime, timezone

    input_rate, output_rate, cost = _calculate_generation_cost(
        model,
        prompt_tokens,
        completion_tokens,
    )

    record = {
        "record_type": "generation",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "question_id": question_id,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "input_rate_per_1M": input_rate,
        "output_rate_per_1M": output_rate,
        "cost_usd": cost,
    }
    TOKEN_SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_SPEND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return cost


def _calculate_generation_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[float, float, float]:
    """Return (input_rate, output_rate, cost_usd) for a generation request."""
    rates = GENERATION_COST_RATES.get(model)
    if rates is None:
        input_rate = output_rate = GENERATION_COST_PER_1M_TOKENS
    else:
        input_rate = float(rates.get("input", GENERATION_COST_PER_1M_TOKENS))
        output_rate = float(rates.get("output", GENERATION_COST_PER_1M_TOKENS))

    cost = round((prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000, 6)
    return input_rate, output_rate, cost


def _get_cumulative_spend() -> float:
    """Sum the cost_usd field in TOKEN_SPEND_LOG to compute cumulative spend."""
    if not TOKEN_SPEND_LOG.exists():
        return 0.0
    total = 0.0
    for line in TOKEN_SPEND_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            total += float(rec.get("cost_usd", 0.0) or 0.0)
        except Exception:
            continue
    return total


def _build_messages(question: str, context: str) -> list[dict[str, str]]:
    """Construct the chat prompt for the single-shot answer."""
    system_prompt = SINGLE_SHOT_SYSTEM_PROMPT
    user_prompt = SINGLE_SHOT_USER_PROMPT.format(
        retrieved_chunks=context,
        question=question,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _run_generation(
    client: OpenAI,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a single-shot RAG answer over the existing vector store.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to vector_store.db produced by embed.py.",
    )
    parser.add_argument(
        "--question",
        type=str,
        required=True,
        help="Question to answer in one pass.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help="Nebius generation model name.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help="Number of chunks to retrieve before selecting context.",
    )
    parser.add_argument(
        "--context-chunks",
        type=int,
        default=DEFAULT_CONTEXT_CHUNKS,
        help="Number of retrieved chunks to include in the prompt.",
    )
    parser.add_argument(
        "--bm25-weight",
        type=int,
        default=BM25_WEIGHT,
        help="Relative BM25 weight in RRF fusion.",
    )
    parser.add_argument(
        "--rrf-k",
        type=int,
        default=RRF_K,
        help="Reciprocal rank fusion constant.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help="Sampling temperature for the generation model.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        help="Maximum output tokens for the generation model.",
    )
    parser.add_argument(
        "--max-chars-per-chunk",
        type=int,
        default=1400,
        help="Truncate each chunk to this many characters before prompting.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print retrieved chunks before generation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run retrieval and assemble prompt, but do not call the generation API.",
    )
    args = parser.parse_args()
    index = _load_index(args.db)

    start_time = time.perf_counter()

    if args.dry_run:
        # Use BM25-only retrieval for a dry run so no API keys are required.
        bm25_scores = np.array(index.bm25.get_scores(_remove_stopwords(args.question)))
        top_idxs = np.argsort(bm25_scores)[::-1][: max(args.top_n, args.context_chunks)]
        retrieved = []
        for idx in top_idxs:
            chunk = dict(index.chunks[idx])
            chunk["bm25_score"] = float(bm25_scores[idx])
            chunk["dense_score"] = 0.0
            chunk["rrf_score"] = float(chunk["bm25_score"]) if "bm25_score" in chunk else 0.0
            retrieved.append(chunk)
    else:
        client = _make_client()
        retrieved = hybrid_retrieve(
            query=args.question,
            client=client,
            index=index,
            k=max(args.top_n, args.context_chunks),
            bm25_weight=args.bm25_weight,
            rrf_k=args.rrf_k,
        )
    context_chunks = _select_context_chunks(args.question, retrieved, args.context_chunks)
    context = _format_context(context_chunks, args.max_chars_per_chunk)

    if args.verbose:
        log.info("Retrieved context chunks:")
        for chunk in context_chunks:
            log.info("- %s | %s | page %s", chunk["chunk_id"], chunk.get("document_id"), chunk.get("page_number", "?"))

    if args.dry_run:
        messages = _build_messages(args.question, context)
        log.info("--- System message ---\n%s", messages[0]["content"])
        log.info("--- User message ---\n%s", messages[1]["content"])
        log.info("Dry run complete — no generation call was made.")
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
    citations = _extract_citations(answer)
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

        # Log generation token spend and compute cost for this run (per-model input/output rates)
        prompt_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(token_usage.get("completion_tokens", 0) or 0)
        run_cost = _log_generation_spend(question_id, args.model, prompt_tokens, completion_tokens)
        total_tokens = prompt_tokens + completion_tokens

        # Persist final answer and complete run
        database.insert_final_answer(conn, run_id, answer)
        database.complete_run(conn, run_id, latency_seconds, int(total_tokens), run_cost)

        # Budget guard — report cumulative spend
        cumulative = _get_cumulative_spend()
        budget_cap = float(os.environ.get("TOKEN_BUDGET_USD", "100.0"))
        log.info("Budget used: $%.2f of $%.2f", cumulative, budget_cap)
        if cumulative >= 0.9 * budget_cap:
            log.warning("Cumulative spend >= 90%% of budget (%s).", budget_cap)
    finally:
        conn.close()

    log.info(
        "Completed single-shot run | latency=%.2fs | tokens=%d",
        latency_seconds,
        token_usage["total_tokens"],
    )


if __name__ == "__main__":
    main()