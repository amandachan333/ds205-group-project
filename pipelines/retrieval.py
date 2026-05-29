"""
pipelines/retrieval.py
----------------------
Shared retrieval, prompt-formatting, and cost-logging helpers used by both
the single-shot and multi-step RAG pipelines.

Keeping these in one module is what makes the benchmark comparison fair:
both pipelines call exactly the same `hybrid_retrieve`, `_select_context_chunks`,
`_format_context`, and cost-accounting code. The only thing that differs
between the two pipelines is whether the question is posed in one shot or
decomposed into sub-questions first.

Nothing in this module knows about LtM, sub-questions, or assembly - it is
strictly retrieval and shared utilities.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from openai import OpenAI
from rank_bm25 import BM25Okapi

from config import (
    GENERATION_COST_PER_1M_TOKENS,
    GENERATION_COST_RATES,
    NEBIUS_BASE_URL,
    TOKEN_SPEND_LOG,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retrieval configuration  (must match across both pipelines)
# ---------------------------------------------------------------------------
EMBEDDING_MODEL: str = "Qwen/Qwen3-Embedding-8B"
EMBEDDING_DIM: int = 4096
BM25_WEIGHT: int = 1
RRF_K: int = 60

STOPWORDS: set[str] = {
    "what", "are", "the", "for", "is", "a", "an", "of", "in",
    "to", "how", "much", "did", "this", "that", "and", "or", "by",
    "which", "their", "each", "between", "from", "its", "both",
}

COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "TNB": ("tnb", "tenaga nasional"),
    "DEWA": ("dewa",),
    "Centerpoint": ("centerpoint", "center point"),
}


# ---------------------------------------------------------------------------
# Client + tokenisation
# ---------------------------------------------------------------------------

def make_client() -> OpenAI:
    """Create the Nebius OpenAI-compatible client used for embedding and generation."""
    api_key = os.environ.get("NEBIUS_API_KEY")
    if not api_key:
        sys.exit("NEBIUS_API_KEY not set. Add it to your .env file or environment.")
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=api_key)


def remove_stopwords(text: str) -> list[str]:
    """Lowercase, split, and drop common stopwords. Used for BM25 tokenisation."""
    return [token for token in text.lower().split() if token not in STOPWORDS]


def _deserialize_float32(blob: bytes) -> list[float]:
    """Decode a little-endian float32 BLOB back into Python floats."""
    return list(np.frombuffer(blob, dtype="<f4"))


# ---------------------------------------------------------------------------
# Vector-store I/O
# ---------------------------------------------------------------------------

def load_chunks(conn: sqlite3.Connection) -> list[dict]:
    """Load all chunk rows from the vector store into a list of dicts."""
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
    """Load embedding BLOBs for the given chunk_ids and decode them to lists of floats."""
    log.info("Loading %d embeddings from sqlite-vec ...", len(chunk_ids))
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


# ---------------------------------------------------------------------------
# Index + retrieval
# ---------------------------------------------------------------------------

class RetrievalIndex:
    """Container for the in-memory BM25 + dense index used by hybrid_retrieve."""

    def __init__(self, chunks: list[dict], embeddings: np.ndarray, bm25: BM25Okapi) -> None:
        self.chunks = chunks
        self.embeddings = embeddings
        self.bm25 = bm25


def build_index(chunks: list[dict], embeddings_map: dict[str, list[float]]) -> RetrievalIndex:
    """Tokenise for BM25 and align embeddings into a row-normalised matrix."""
    log.info("Building BM25 index ...")
    tokenised = [remove_stopwords(chunk["text"]) for chunk in chunks]
    bm25 = BM25Okapi(tokenised)

    log.info("Aligning embedding matrix ...")
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


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """
    Load the sqlite-vec extension into conn.
    Must be called before querying chunk_embeddings (a vec0 virtual table).
    Mirrors the same helper in embed.py so both scripts use identical setup.
    """
    try:
        import sqlite_vec  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "sqlite-vec is not installed. Run: pip install sqlite-vec"
        ) from exc
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def load_index(db_path: Path) -> RetrievalIndex:
    """Load chunks and embeddings from the vector store into an in-memory index."""
    if not db_path.exists():
        sys.exit(f"Vector store not found: {db_path}\nRun embed.py first.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _load_sqlite_vec(conn)
    try:
        chunks = load_chunks(conn)
        if not chunks:
            sys.exit("No chunks found in vector store.")
        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        embeddings_map = load_embeddings(conn, chunk_ids)
    finally:
        conn.close()

    return build_index(chunks, embeddings_map)


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

    bm25_tokens = remove_stopwords(query)
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


# ---------------------------------------------------------------------------
# Prompt-side formatting
# ---------------------------------------------------------------------------

def format_document_label(chunk: dict) -> str:
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


def format_context(chunks: list[dict], max_chars_per_chunk: int) -> str:
    """Render retrieved chunks in the citation-friendly format used by both pipelines."""
    blocks: list[str] = []
    for chunk in chunks:
        text = chunk.get("text", "")
        if max_chars_per_chunk > 0 and len(text) > max_chars_per_chunk:
            text = text[:max_chars_per_chunk].rstrip() + "..."
        blocks.append(
            "\n".join([
                f"({format_document_label(chunk)}, p.{chunk.get('page_number', '?')})",
                f"chunk_id={chunk.get('chunk_id', '')}",
                "text:",
                text,
            ])
        )
    return "\n\n".join(blocks)


def mentioned_companies(question: str) -> list[str]:
    """Return the canonical names of any companies named in the question."""
    question_lower = question.lower()
    mentioned: list[str] = []
    for company, aliases in COMPANY_ALIASES.items():
        if any(alias in question_lower for alias in aliases):
            mentioned.append(company)
    return mentioned


def select_context_chunks(question: str, retrieved: list[dict], max_chunks: int) -> list[dict]:
    """
    Select up to `max_chunks` chunks for the prompt.

    If the question mentions two or more companies, guarantee at least one
    chunk per company before filling the remaining slots by rank. For single-
    company or no-company queries this is a no-op that returns retrieved[:max_chunks].

    The same selection logic is applied in both pipelines so the only difference
    between them is the wording of the query, not how chunks are chosen.
    """
    mentioned = mentioned_companies(question)
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


def extract_citations(answer: str) -> list[str]:
    """Return parenthetical source citations mentioned in the model answer."""
    citations: list[str] = []
    for match in re.finditer(r"\(([^()]+?),\s*p\.?\s*(\d+)\)", answer):
        citation = f"{match.group(1).strip()}, p.{match.group(2)}"
        if citation not in citations:
            citations.append(citation)
    return citations


def summarise_chunks_for_step(chunks: list[dict]) -> str:
    """
    Serialise the chunks retrieved for a sub-question into a compact JSON string,
    suitable for storage in `steps.retrieved_chunks`. We persist enough to audit
    the step (chunk_id, page, document label) without bloating the row with full
    chunk text.
    """
    summary = [
        {
            "chunk_id": chunk.get("chunk_id"),
            "page_number": chunk.get("page_number"),
            "document_label": format_document_label(chunk),
        }
        for chunk in chunks
    ]
    return json.dumps(summary, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Generation cost accounting  (shared between both pipelines)
# ---------------------------------------------------------------------------

def calculate_generation_cost(
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
    cost = round(
        (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000,
        6,
    )
    return input_rate, output_rate, cost


def log_generation_spend(
    question_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    extra: dict | None = None,
) -> float:
    """
    Append one generation-spend record to TOKEN_SPEND_LOG and return computed cost.

    `extra` lets callers tag the record with extra context, e.g.
    `{"phase": "decomposition", "run_id": 17}` for multi-step calls.
    """
    input_rate, output_rate, cost = calculate_generation_cost(
        model, prompt_tokens, completion_tokens
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
    if extra:
        record.update(extra)
    TOKEN_SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_SPEND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return cost


def get_cumulative_spend() -> float:
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


def check_budget(label: str) -> None:
    """Log cumulative spend and warn at 90% of TOKEN_BUDGET_USD."""
    cumulative = get_cumulative_spend()
    budget_cap = float(os.environ.get("TOKEN_BUDGET_USD", "100.0"))
    log.info("[%s] Budget used: $%.4f of $%.2f", label, cumulative, budget_cap)
    if cumulative >= 0.9 * budget_cap:
        log.warning("[%s] Cumulative spend >= 90%% of budget (%s).", label, budget_cap)