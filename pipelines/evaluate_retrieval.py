"""
evaluate_retrieval.py
---------------------
Offline retrieval-quality check.

Runs hybrid BM25+dense retrieval for each ground-truth question and
measures whether the expected source chunks appear in the top-k results.
No generation model is called — this is purely a retrieval sanity check.
The embedding model Qwen/Qwen3-Embedding-8B is called via NEBIUS to 
embed the queries.

Metrics reported per question and in aggregate:
  Recall@k   – fraction of expected sources found in top-k results
  MRR        – mean reciprocal rank of the first expected source hit

Usage:
    python pipelines/evaluate_retrieval.py                         # defaults
    python pipelines/evaluate_retrieval.py --db data/vector_store.db --k 20
    python pipelines/evaluate_retrieval.py --k 5 --k 10 --k 20    # multiple k values

Environment variables (place in .env, same as embed.py):
    NEBIUS_API_KEY     required for dense embedding
    NEBIUS_BASE_URL    optional
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from rank_bm25 import BM25Okapi
from openai import OpenAI
from config import (
    DB_PATH,
    EMBED_COST_PER_1M_TOKENS,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    LOG_DIR,
    NEBIUS_BASE_URL,
    TOKEN_SPEND_LOG,
)
from utils import bootstrap_runtime_env, ensure_stage_dirs

bootstrap_runtime_env()   # loads .env before any network calls

ensure_stage_dirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "retrieval_eval.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults — override via CLI flags
# (EMBEDDING_MODEL, EMBEDDING_DIM, NEBIUS_BASE_URL, DB_PATH imported from config)
# ---------------------------------------------------------------------------
DEFAULT_K_VALUES = [5, 10, 20]
BM25_WEIGHT      = 1    
RRF_K            = 60   # standard RRF constant

STOPWORDS = {
    "what", "are", "the", "for", "is", "a", "an", "of", "in",
    "to", "how", "much", "did", "this", "that", "and", "or", "by",
    "which", "their", "each", "between", "from", "its", "both",
}

# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------
# Canonical questions live in evaluation/ground_truth.json.
# The markdown file is kept as human-readable documentation.
GROUND_TRUTH_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "ground_truth.json"
STRICT_PAGE_MATCH = False   # set True for exact page-number matching


def load_ground_truth(path: Path) -> list[dict[str, Any]]:
    """Load canonical ground-truth questions from JSON."""
    if not path.exists():
        sys.exit(f"Ground-truth file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, list):
        sys.exit(f"Ground-truth file must contain a JSON list: {path}")

    questions: list[dict[str, Any]] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            sys.exit(f"Ground-truth entry {index} must be a JSON object: {path}")

        for key in ("question_id", "question", "sources"):
            if key not in item:
                sys.exit(f"Ground-truth entry {index} is missing '{key}': {path}")

        sources = item["sources"]
        if not isinstance(sources, list):
            sys.exit(f"Ground-truth entry {index} must store 'sources' as a list: {path}")

        normalized_sources: list[tuple[str, int]] = []
        for source_index, source in enumerate(sources, 1):
            if not isinstance(source, (list, tuple)) or len(source) != 2:
                sys.exit(
                    f"Ground-truth entry {index} source {source_index} must be a two-item list: {path}"
                )
            normalized_sources.append((str(source[0]), int(source[1])))

        normalized = dict(item)
        normalized["sources"] = normalized_sources
        questions.append(normalized)

    return questions
 
 
# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
 
def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Compatibility hook retained for older docs; no-op in this build."""
    return None
 
 
def _deserialize_float32(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))
 
 
def load_chunks(conn: sqlite3.Connection) -> list[dict]:
    """Load all chunks from the vector store into memory."""
    rows = conn.execute(
        "SELECT chunk_id, company, document_id, year, chunk_index, page_number, text, metadata "
        "FROM chunks"
    ).fetchall()
    chunks = []
    for row in rows:
        chunks.append({
            "chunk_id":    row[0],
            "company":     row[1],
            "document_id": row[2],
            "year":        row[3],
            "chunk_index": row[4],
            "page_number": row[5],
            "text":        row[6],
            "metadata":    json.loads(row[7]) if row[7] else {},
        })
    log.info("Loaded %d chunks from vector store", len(chunks))
    return chunks
 
 
def load_embeddings(
    conn: sqlite3.Connection,
    chunk_ids: list[str],
) -> dict[str, list[float]]:
    """Load all embeddings from chunk_embeddings virtual table."""
    log.info("Loading %d embeddings from sqlite-vec …", len(chunk_ids))
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT chunk_id, embedding FROM chunk_embeddings WHERE chunk_id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    result = {}
    for chunk_id, blob in rows:
        result[chunk_id] = _deserialize_float32(blob)
    log.info("Loaded %d embeddings", len(result))
    return result
 
 
# ---------------------------------------------------------------------------
# Embedding helper (query-time only — no passage re-embedding)
# ---------------------------------------------------------------------------
 
def _make_client() -> OpenAI:
    api_key = os.environ.get("NEBIUS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "NEBIUS_API_KEY not set. Add it to your .env file or environment."
        )
    # NEBIUS_BASE_URL is already resolved from env in config.py
    return OpenAI(base_url=NEBIUS_BASE_URL, api_key=api_key)
 
 
def _log_query_embed_spend(question_id: str, total_tokens: int) -> None:
    """Append one query-embedding spend record to the shared token_spend.jsonl."""
    from datetime import datetime, timezone
    record = {
        "record_type":   "retrieval_eval_query",
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "model":         EMBEDDING_MODEL,
        "question_id":   question_id,
        "total_tokens":  total_tokens,
        "cost_usd":      round(total_tokens / 1_000_000 * EMBED_COST_PER_1M_TOKENS, 6),
    }
    TOKEN_SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_SPEND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
 
 
def embed_query(client: OpenAI, query: str, question_id: str = "") -> np.ndarray:
    """
    Embed a single query string with the Qwen3-Embedding instruction prefix.
    Returns a normalised float32 numpy vector.
    Logs token spend to TOKEN_SPEND_LOG (shared with embed.py audit trail).
    """
    instruction = (
        "Instruct: Given a question, retrieve passages that answer the question\n"
        f"Query: {query}"
    )
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[instruction],
    )
    total_tokens = response.usage.total_tokens
    _log_query_embed_spend(question_id, total_tokens)
    log.debug("Query embed  question=%s  tokens=%d", question_id, total_tokens)
 
    vec = np.array(response.data[0].embedding, dtype=np.float32)
    vec /= np.linalg.norm(vec) + 1e-10   # L2-normalise
    return vec
 
 
# ---------------------------------------------------------------------------
# BM25 + dense hybrid retrieval (RRF fusion)
# ---------------------------------------------------------------------------
 
def _remove_stopwords(text: str) -> list[str]:
    return [t for t in text.lower().split() if t not in STOPWORDS]
 
 
@dataclass
class RetrievalIndex:
    chunks:     list[dict]
    embeddings: np.ndarray          # shape (N, DIM), row-aligned with chunks
    bm25:       BM25Okapi
 
 
def build_index(chunks: list[dict], embeddings_map: dict[str, list[float]]) -> RetrievalIndex:
    """Build BM25 index and align embedding matrix to chunks list."""
    log.info("Building BM25 index …")
    tokenised = [_remove_stopwords(c["text"]) for c in chunks]
    bm25 = BM25Okapi(tokenised)
 
    log.info("Aligning embedding matrix …")
    n = len(chunks)
    emb_matrix = np.zeros((n, EMBEDDING_DIM), dtype=np.float32)
    missing = 0
    for i, chunk in enumerate(chunks):
        vec = embeddings_map.get(chunk["chunk_id"])
        if vec is not None:
            emb_matrix[i] = vec
        else:
            missing += 1
    if missing:
        log.warning("%d chunks have no embedding (will score 0 in dense retrieval)", missing)
 
    # L2-normalise each row for cosine similarity via dot product
    norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True) + 1e-10
    emb_matrix /= norms
 
    return RetrievalIndex(chunks=chunks, embeddings=emb_matrix, bm25=bm25)
 
 
def hybrid_retrieve(
    query: str,
    client: OpenAI,
    index: RetrievalIndex,
    k: int = 20,
    bm25_weight: int = BM25_WEIGHT,
    rrf_k: int = RRF_K,
    question_id: str = "",
) -> list[dict]:
    """
    BM25 + dense hybrid retrieval with RRF fusion.
 
    bm25_weight=2 means BM25 ranks count twice in RRF — matches PS2 findings.
    Returns top-k chunks ranked by fused score, each dict includes a 'rrf_score' key.
    """
    n = len(index.chunks)
 
    # --- Dense retrieval ---
    q_vec = embed_query(client, query, question_id=question_id)
    dense_scores = index.embeddings @ q_vec           # cosine similarity
    dense_ranks  = np.argsort(dense_scores)[::-1]     # highest first
 
    # --- BM25 retrieval ---
    bm25_tokens = _remove_stopwords(query)
    bm25_scores = np.array(index.bm25.get_scores(bm25_tokens))
    bm25_ranks  = np.argsort(bm25_scores)[::-1]
 
    # --- RRF fusion ---
    rrf_scores = np.zeros(n, dtype=np.float64)
 
    for rank, idx in enumerate(dense_ranks):
        rrf_scores[idx] += 1.0 / (rrf_k + rank + 1)
 
    for rank, idx in enumerate(bm25_ranks):
        rrf_scores[idx] += bm25_weight / (rrf_k + rank + 1)
 
    top_indices = np.argsort(rrf_scores)[::-1][:k]
 
    results = []
    for idx in top_indices:
        chunk = dict(index.chunks[idx])
        chunk["rrf_score"]    = float(rrf_scores[idx])
        chunk["dense_score"]  = float(dense_scores[idx])
        chunk["bm25_score"]   = float(bm25_scores[idx])
        results.append(chunk)
 
    return results
 
 
# ---------------------------------------------------------------------------
# Matching logic: does a retrieved chunk satisfy a ground-truth source?
# ---------------------------------------------------------------------------
 
def _normalise_doc_id(doc_id: str) -> str:
    """Lower-case, replace separators with underscores for fuzzy matching."""
    return doc_id.lower().replace("-", "_").replace(" ", "_")


_DOC_TOKEN_STOPWORDS = {
    "annual",
    "corporate",
    "csr",
    "iar",
    "pdf",
    "report",
    "sustainability",
}


def _doc_signature(doc_id: str) -> tuple[str | None, str | None]:
    """Extract a coarse company/year signature from a document identifier."""
    tokens = [token for token in re.split(r"[_\W]+", _normalise_doc_id(doc_id)) if token]
    year = next((token for token in tokens if re.fullmatch(r"20\d{2}", token)), None)
    company = next(
        (token for token in tokens if token not in _DOC_TOKEN_STOPWORDS and token != year),
        None,
    )
    return company, year


def _signature_matches(left: tuple[str | None, str | None], right: tuple[str | None, str | None]) -> bool:
    """Return True if two coarse document signatures are compatible."""
    left_company, left_year = left
    right_company, right_year = right

    if left_company and right_company:
        if left_company != right_company and left_company not in right_company and right_company not in left_company:
            return False

    if left_year and right_year and left_year != right_year:
        return False

    return True
 
 
def chunk_matches_source(
    chunk: dict,
    doc_fragment: str,
    expected_page: int,
    strict: bool = STRICT_PAGE_MATCH,
) -> bool:
    """
    Returns True if *chunk* plausibly covers the ground-truth (doc, page) source.
 
    doc_fragment is matched as a substring of chunk['document_id'] after
    normalisation — this handles differences in casing, separators, year
    appended to the company name, etc.
 
    Page tolerance of ±1 handles cases where PDF logical page numbers differ
    from physical page numbers by a single page.
    """
    chunk_doc = _normalise_doc_id(chunk.get("document_id", ""))
    frag_norm = _normalise_doc_id(doc_fragment)
    chunk_sig = _doc_signature(chunk.get("document_id", ""))
    frag_sig = _doc_signature(doc_fragment)
 
    # Check document match bidirectionally — handles the case where ground truth
    # uses long names ("DEWA_Sustainability_Report_2024") but document_ids in the
    # DB are short ("DEWA_2024"), or vice versa.
    if frag_norm not in chunk_doc and chunk_doc not in frag_norm and not _signature_matches(chunk_sig, frag_sig):
        return False
 
    # Check page match
    chunk_page = chunk.get("page_number")
    if chunk_page is None:
        # No page info stored — treat as a match if doc matched
        return True
 
    if strict:
        return int(chunk_page) == expected_page
    else:
        return abs(int(chunk_page) - expected_page) <= 1
 
 
# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
 
@dataclass
class QuestionResult:
    question_id:      str
    question:         str
    n_sources:        int
    recall_at_k:      dict[int, float] = field(default_factory=dict)   # k -> recall
    mrr:              float = 0.0
    hits_at_k:        dict[int, int]   = field(default_factory=dict)   # k -> n_hits
    first_hit_rank:   int | None = None   # 1-indexed rank of first matched chunk
    retrieved:        list[dict] = field(default_factory=list)   # top-k chunks, for verbose print
 
 
def evaluate_question(
    gt: dict,
    client: OpenAI,
    index: RetrievalIndex,
    k_values: list[int],
) -> QuestionResult:
    """Run retrieval for one ground-truth question and compute metrics."""
    max_k = max(k_values)
    retrieved = hybrid_retrieve(
        query=gt["question"],
        client=client,
        index=index,
        k=max_k,
        question_id=gt["question_id"],
    )
 
    sources = gt["sources"]   # list of (doc_fragment, page) tuples
    result = QuestionResult(
        question_id=gt["question_id"],
        question=gt["question"],
        n_sources=len(sources),
    )
 
    # For each retrieved rank position, check whether it matches any source
    match_flags = []
    for chunk in retrieved:
        hit = any(
            chunk_matches_source(chunk, doc_frag, page)
            for doc_frag, page in sources
        )
        match_flags.append(hit)
 
    # MRR — reciprocal rank of first hit
    for rank_0, is_hit in enumerate(match_flags):
        if is_hit:
            result.first_hit_rank = rank_0 + 1
            result.mrr = 1.0 / (rank_0 + 1)
            break
 
    # Recall@k and hits@k
    for k in k_values:
        hits_in_k = sum(match_flags[:k])
        result.hits_at_k[k] = hits_in_k
        # Recall = unique sources found / total sources
        # Here we check per-source whether any top-k chunk covers it
        sources_found = sum(
            1 for (doc_frag, page) in sources
            if any(
                chunk_matches_source(chunk, doc_frag, page)
                for chunk in retrieved[:k]
            )
        )
        result.recall_at_k[k] = sources_found / len(sources) if sources else 0.0
 
    result.retrieved = retrieved
    return result
 
 
def print_results(results: list[QuestionResult], k_values: list[int]) -> None:
    """Pretty-print per-question results and aggregate summary."""
    sep = "─" * 80
 
    print(f"\n{'═' * 80}")
    print("  RETRIEVAL EVALUATION RESULTS  (BM25 hybrid, no reranker)")
    print(f"{'═' * 80}\n")
 
    for r in results:
        print(f"  {r.question_id}: {r.question[:80]}{'…' if len(r.question) > 80 else ''}")
        print(f"  Expected sources : {r.n_sources}")
        recall_str = "  ".join(
            f"Recall@{k}: {r.recall_at_k[k]:.2f} ({r.hits_at_k[k]} src)"
            for k in k_values
        )
        print(f"  {recall_str}")
        print(f"  MRR: {r.mrr:.4f}   First hit at rank: {r.first_hit_rank or 'MISS'}")
        print(f"  {sep}")
 
    print("\n  AGGREGATE")
    print(f"  {sep}")
    n = len(results)
    for k in k_values:
        avg_recall = sum(r.recall_at_k[k] for r in results) / n
        print(f"  Mean Recall@{k:<2} : {avg_recall:.4f}")
    avg_mrr = sum(r.mrr for r in results) / n
    print(f"  Mean MRR      : {avg_mrr:.4f}")
 
    # Verdict
    best_k = max(k_values)
    mean_recall_best = sum(r.recall_at_k[best_k] for r in results) / n
    print(f"\n  VERDICT (Recall@{best_k})")
    if mean_recall_best >= 0.70:
        verdict = "✓  Retrieval quality looks GOOD — safe to proceed to generation."
    elif mean_recall_best >= 0.45:
        verdict = "⚠  Retrieval is PARTIAL — some questions may be recall-limited."
    else:
        verdict = "✗  Retrieval quality is LOW — fix retrieval before adding generation."
    print(f"  {verdict}\n")
 
 
def save_results_jsonl(results: list[QuestionResult], path: Path) -> None:
    """Save detailed results as JSONL for later analysis."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps({
                "question_id":    r.question_id,
                "question":       r.question,
                "n_sources":      r.n_sources,
                "recall_at_k":    {str(k): v for k, v in r.recall_at_k.items()},
                "hits_at_k":      {str(k): v for k, v in r.hits_at_k.items()},
                "mrr":            r.mrr,
                "first_hit_rank": r.first_hit_rank,
            }, ensure_ascii=False) + "\n")
    log.info("Results saved to %s", path)
 
 
# ---------------------------------------------------------------------------
# Detailed chunk inspection (optional verbose mode)
# ---------------------------------------------------------------------------
 
def print_retrieved_chunks(
    gt: dict,
    result: "QuestionResult",
) -> None:
    """Print retrieved chunks already fetched by evaluate_question — no re-embedding."""
    sources = gt["sources"]
    k = len(result.retrieved)
    print(f"\n── {gt['question_id']}: top-{k} retrieved chunks ──")
    for rank, chunk in enumerate(result.retrieved, 1):
        is_hit = any(
            chunk_matches_source(chunk, doc_frag, page)
            for doc_frag, page in sources
        )
        flag = "✓ HIT " if is_hit else "      "
        print(
            f"  [{rank:2}] {flag} "
            f"chunk={chunk['chunk_id']:<30} "
            f"doc={chunk['document_id'][:40]:<40} "
            f"page={chunk.get('page_number', '?'):>4}  "
            f"rrf={chunk['rrf_score']:.5f}"
        )
 
 
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
 
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate BM25 hybrid retrieval quality against ground truth (no LLM).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db", type=Path, default=DB_PATH,
        help="Path to vector_store.db produced by embed.py",
    )
    parser.add_argument(
        "--k", type=int, action="append", dest="k_values",
        help="Recall@k cutoff. Repeat for multiple: --k 5 --k 10 --k 20",
    )
    parser.add_argument(
        "--output", type=Path, default=LOG_DIR / "retrieval_eval.jsonl",
        help="Where to write per-question JSONL results.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print top-k retrieved chunks for each question.",
    )
    parser.add_argument(
        "--strict-pages", action="store_true",
        help="Require exact page number match (default: ±1 tolerance).",
    )
    parser.add_argument(
        "--ground-truth", type=Path, default=GROUND_TRUTH_PATH,
        help="Path to the canonical JSON ground-truth file.",
    )
    parser.add_argument(
        "--question", type=str,
        help="Evaluate a single question only (useful for debugging).",
    )
    args = parser.parse_args()
 
    k_values = sorted(set(args.k_values)) if args.k_values else DEFAULT_K_VALUES
    strict   = args.strict_pages
 
    # Override module-level default if flag given
    if strict:
        global STRICT_PAGE_MATCH
        STRICT_PAGE_MATCH = True
 
    # --- Load DB ---
    if not args.db.exists():
        sys.exit(f"Vector store not found: {args.db}\nRun embed.py first.")
 
    conn = sqlite3.connect(args.db)
    # sqlite_vec is optional on some hosts (macOS without the extension installed).
    # Try to load it but continue if unavailable — embeddings are stored as BLOBs.
    try:
        import sqlite_vec as _sv  # type: ignore
        conn.enable_load_extension(True)
        _sv.load(conn)
        conn.enable_load_extension(False)
        log.info("sqlite_vec extension loaded")
    except Exception:
        log.warning("sqlite_vec extension not available — proceeding without it")
 
    chunks = load_chunks(conn)
    if not chunks:
        sys.exit("No chunks found in vector store.")
 
    chunk_ids      = [c["chunk_id"] for c in chunks]
    embeddings_map = load_embeddings(conn, chunk_ids)
    conn.close()
 
    index  = build_index(chunks, embeddings_map)
    client = _make_client()
 
    # --- Select questions ---
    questions = load_ground_truth(args.ground_truth)
    if args.question:
        questions = [q for q in questions if q["question_id"] == args.question]
        if not questions:
            sys.exit(f"Question not found in ground truth: {args.question}")
 
    # --- Evaluate ---
    results = []
    for gt in questions:
        log.info("Evaluating %s …", gt["question_id"])
        result = evaluate_question(gt, client, index, k_values)
        results.append(result)
 
        if args.verbose:
            print_retrieved_chunks(gt, result)
 
    # --- Report ---
    print_results(results, k_values)
    save_results_jsonl(results, args.output)
 
 
if __name__ == "__main__":
    main()