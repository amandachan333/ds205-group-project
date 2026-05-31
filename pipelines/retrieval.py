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
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from openai import OpenAI
from rank_bm25 import BM25Okapi

from config import (
    EMBED_COST_PER_1M_TOKENS,
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
# BM25_WEIGHT=2 is empirically optimal on this corpus per the sweep documented
# in DECISIONS.md. After fixing punctuation handling in `remove_stopwords`, a
# 0/1/2/3 sweep on the 6 GT questions showed weight=2 best at R@40=0.283 vs
# 0.250 (weight=1), 0.147 (weight=0), 0.255 (weight=3).
BM25_WEIGHT: int = 2
RRF_K: int = 60

STOPWORDS: set[str] = {
    "what",
    "are",
    "the",
    "for",
    "is",
    "a",
    "an",
    "of",
    "in",
    "to",
    "how",
    "much",
    "did",
    "this",
    "that",
    "and",
    "or",
    "by",
    "which",
    "their",
    "each",
    "between",
    "from",
    "its",
    "both",
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


_BM25_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def remove_stopwords(text: str) -> list[str]:
    """
    Tokenise text for BM25: lowercase, split on word boundaries (so
    punctuation does not get glued to tokens), drop common stopwords.
    """
    return [token for token in _BM25_TOKEN_RE.findall(text.lower()) if token not in STOPWORDS]


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
        chunks.append(
            {
                "chunk_id": row[0],
                "company": row[1],
                "document_id": row[2],
                "year": row[3],
                "chunk_index": row[4],
                "page_number": row[5],
                "text": row[6],
                "metadata": json.loads(row[7]) if row[7] else {},
            }
        )
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
        raise ImportError("sqlite-vec is not installed. Run: pip install sqlite-vec") from exc
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def get_corpus_inventory(db_path: Path) -> str:
    """
    Return a human-readable inventory of which report years are available for
    each company in the vector store.

    Injected at the top of the decomposition prompt so the LLM can resolve
    "most recent year", "latest", or similar relational time references in
    the question into concrete years before writing sub-questions. Retrieval
    cannot answer "what is the most recent year for company X" - that is a
    metadata question, and this helper supplies the metadata directly.
    """
    if not db_path.exists():
        log.warning(
            "Vector store not found at %s; corpus inventory will be empty.",
            db_path,
        )
        return "(no corpus inventory available)"

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT company, year
            FROM chunks
            WHERE year IS NOT NULL
            GROUP BY company, year
            ORDER BY company, year
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return "(no corpus inventory available)"

    by_company: dict[str, list[int]] = {}
    for company, year in rows:
        by_company.setdefault(company, []).append(int(year))

    lines = ["Corpus context - sustainability reports available in the document store:"]
    for company in sorted(by_company.keys()):
        years_str = ", ".join(str(y) for y in by_company[company])
        lines.append(f"- {company}: {years_str}")
    lines.append("")
    lines.append(
        "Use this inventory to resolve any 'most recent year', 'latest', or similar "
        "relational time references in the question into concrete years before writing "
        "sub-questions."
    )
    return "\n".join(lines)


_WORD_FOR_DEDUP_RE = re.compile(r"\b[a-z]{3,}\b")


def _normalize_for_dedup(text: str) -> str:
    """
    Build a content signature by keeping only alphabetic words of 3+ letters.

    Drops page-number tokens ("PG. 24", "Pc. 34" -> "pg", "pc" are 2 letters
    so excluded), em dashes, punctuation, and digits. The result is a
    concatenation of substantive words only, so "Tenaga Nasional Berhad
    ---- Sustainability Report 2019 EMPOWERING ..." and "Tenaga Nasional
    Berhad Sustainability Report 2019 PG. 24 EMPOWERING ..." collapse to
    identical signatures - exactly what we need to detect repeated running
    headers across pages.
    """
    return "".join(_WORD_FOR_DEDUP_RE.findall(text.lower()))


def _count_digits(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


def filter_boilerplate_chunks(
    chunks: list[dict],
    *,
    max_chunk_length: int = 300,
    min_digit_count: int = 8,
    prefix_chars: int = 80,
    min_duplicate_count: int = 3,
) -> tuple[list[dict], int]:
    """
    Drop chunks that look like running-header / cover-page boilerplate.

    Many PDF extractors emit per-page running-header chunks ("Tenaga Nasional
    Berhad Sustainability Report 2019 EMPOWERING THE NATION ...") that
    contain no real content yet match queries on company/year/topic terms
    perfectly, pushing actual data chunks out of the top-K.

    A chunk is considered boilerplate iff ALL of:
      1. Its length is below `max_chunk_length` (short).
      2. It contains fewer than `min_digit_count` digit characters. Page-
         header chunks typically have ~4 digits (the report year, occasional
         page number) while real data chunks have 8+ (year + multiple values).
      3. Its content signature (first `prefix_chars` chars of the alphabetic-
         word-concatenation, see `_normalize_for_dedup`) is shared with at
         least `min_duplicate_count` chunks total, indicating the same
         running-header text recurs across pages.

    The conjunction protects two legitimate-but-short chunk shapes:
      - Short data chunks ("GHG intensity 2024: 0.5571 ...") - pass condition 1
        but fail condition 2 (digit-rich), so kept.
      - Unique short prose ("Refer to section 4 for details.") - pass
        conditions 1 and 2 but fail condition 3 (not duplicated), so kept.

    Returns (kept_chunks, n_dropped).
    """
    if not chunks:
        return chunks, 0

    # Count how many chunks share each content-signature prefix.
    sig_counts: dict[str, int] = {}
    for chunk in chunks:
        sig = _normalize_for_dedup(chunk.get("text", ""))[:prefix_chars]
        if sig:
            sig_counts[sig] = sig_counts.get(sig, 0) + 1

    kept: list[dict] = []
    dropped = 0
    for chunk in chunks:
        text = chunk.get("text", "")
        if len(text) < max_chunk_length and _count_digits(text) < min_digit_count:
            sig = _normalize_for_dedup(text)[:prefix_chars]
            if sig and sig_counts.get(sig, 0) >= min_duplicate_count:
                dropped += 1
                continue
        kept.append(chunk)

    return kept, dropped


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

        # Boilerplate filter: removes running-header chunks from the index.
        # Disabled by default. Empirical testing under the (then-broken)
        # naive-split BM25 tokeniser found this regressed TNB 2024 retrieval;
        # worth retesting under the current word-regex tokeniser. Flip the
        # flag below to True to re-enable, then run the eval script to
        # measure. See DECISIONS.md for the write-up.
        ENABLE_BOILERPLATE_FILTER = False

        if ENABLE_BOILERPLATE_FILTER:
            n_before = len(chunks)
            chunks, n_dropped = filter_boilerplate_chunks(chunks)
            if n_dropped:
                log.info(
                    "Filtered out %d boilerplate chunks (%.1f%% of corpus); %d remain",
                    n_dropped,
                    100 * n_dropped / n_before,
                    len(chunks),
                )

        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        embeddings_map = load_embeddings(conn, chunk_ids)
    finally:
        conn.close()

    return build_index(chunks, embeddings_map)


def embed_query(client: OpenAI, query: str, question_id: str = "") -> np.ndarray:
    """
    Embed the query using the same instruction prefix as retrieval evaluation.

    When question_id is non-empty, also append one record to TOKEN_SPEND_LOG
    tagged with `record_type: "retrieval_eval_query"`. Pipelines that don't
    need per-query embedding spend tracking can omit the argument and pay no
    extra cost; the evaluation script always passes it.
    """
    instruction = (
        f"Instruct: Given a question, retrieve passages that answer the question\nQuery: {query}"
    )
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=[instruction],
    )
    vec = np.array(response.data[0].embedding, dtype=np.float32)
    vec /= np.linalg.norm(vec) + 1e-10

    if question_id:
        usage = response.usage
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        log_embedding_spend(question_id, total_tokens)

    return vec


def hybrid_retrieve(
    query: str,
    client: OpenAI,
    index: RetrievalIndex,
    k: int,
    bm25_weight: int = BM25_WEIGHT,
    rrf_k: int = RRF_K,
    question_id: str = "",
) -> list[dict]:
    """BM25 + dense hybrid retrieval with RRF fusion.

    If question_id is non-empty, embedding-spend for this call is logged to
    TOKEN_SPEND_LOG. Pipelines that don't need per-call embedding tracking
    can omit the argument.
    """
    dense_query = embed_query(client, query, question_id=question_id)
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
            "\n".join(
                [
                    f"({format_document_label(chunk)}, p.{chunk.get('page_number', '?')})",
                    f"chunk_id={chunk.get('chunk_id', '')}",
                    "text:",
                    text,
                ]
            )
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


# ---------------------------------------------------------------------------
# Value-content patterns (used to prefer value-bearing chunks at selection)
# ---------------------------------------------------------------------------
#
# Many of our sub-questions ask for a specific quantity ("emissions intensity
# in 2024", "net-zero target year", "percentage reduction"). Without help,
# `select_context_chunks` will reserve the first chunk that matches the
# (company, year) constraint, which is often a long target-discussion chunk
# that mentions the topic but contains no actual numbers. The patterns below
# detect chunks that contain the *kind of value* a sub-question is asking for.
# `_has_value_pattern` ties the patterns to keywords in the question so the
# check is a no-op for questions that aren't value-seeking.

# Decimal-near-intensity-context: matches "0.5571 tCO2e/MWh", "intensity ... 0.4045",
# "tCO2e/MWh ... 2024: 0.5571", etc. Used when the question asks for an emissions
# intensity figure.
_INTENSITY_VALUE_RE = re.compile(
    r"\b\d+\.\d{2,4}\b[^.\n]{0,80}?(?:intensity|tco[2\u2082]?\s*e?\s*/?\s*mwh)"
    r"|(?:intensity|tco[2\u2082]?\s*e?\s*/?\s*mwh)[^.\n]{0,80}?\b\d+\.\d{2,4}\b",
    re.IGNORECASE,
)

# Explicit percentage value (e.g. "35%", "5% annually"). Used when the question
# asks for a percentage reduction or annual rate.
_PERCENTAGE_VALUE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*%")

# Net-zero / long-term target year mentioned with an actual year nearby.
_TARGET_YEAR_RE = re.compile(
    r"\bnet[ -]?zero[^.\n]{0,80}\b(?:2030|2035|2040|2045|2050|2060)\b"
    r"|\b(?:2030|2035|2040|2045|2050|2060)\b[^.\n]{0,80}?\bnet[ -]?zero",
    re.IGNORECASE,
)


def _has_value_pattern(text: str, question: str) -> bool:
    """
    Heuristic: does `text` look like it contains the kind of value `question`
    is asking for? Returns False if no trigger word is present in `question`,
    making this a no-op for non-value-seeking sub-questions.
    """
    q = question.lower()
    if "intensity" in q and _INTENSITY_VALUE_RE.search(text):
        return True
    if ("net zero" in q or "net-zero" in q or "target year" in q) and _TARGET_YEAR_RE.search(text):
        return True
    if (
        "percentage" in q or "reduction" in q or "annual reduction" in q
    ) and _PERCENTAGE_VALUE_RE.search(text):
        return True
    return False


def mentioned_years(question: str) -> list[int]:
    """
    Return any 20xx years explicitly named in the question, preserving
    first-mention order with duplicates removed.

    Used by select_context_chunks to reserve at least one chunk per
    (company, year) pair the query is asking about. Years that don't
    correspond to a published report year in the corpus are still extracted
    here; the reservation loop simply finds no matching chunk and falls
    through to the next candidate, so unrelated years (target years like
    2050, base years like 2020 when no 2020 chunk is retrieved, etc.) are
    self-cleaning no-ops.
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for match in re.finditer(r"\b(20\d{2})\b", question):
        year = int(match.group(1))
        if year not in seen:
            seen.add(year)
            ordered.append(year)
    return ordered


def select_context_chunks(question: str, retrieved: list[dict], max_chunks: int) -> list[dict]:
    """
    Select up to `max_chunks` chunks for the prompt.

    Three reservation passes run before filling the remaining slots by rank:

    1. Value-aware year boost: for every year (and optionally company) the
       question mentions, prefer chunks that match the (company, year) AND
       contain a value pattern relevant to the question (an intensity
       figure, a net-zero target year, a percentage value, etc.). Falls
       back to the plain (company, year) match if no value-bearing chunk
       is in the candidate set.
    2. Company boost: when two or more companies are mentioned, ensure
       each named company has at least one chunk in the prompt even if
       it wasn't already covered by the year-boost step.
    3. Rank fill: remaining slots filled in retrieval order.

    For queries with no mentioned year and zero or one mentioned company
    (e.g. simple sub-questions about a single fact) all passes are
    no-ops except the rank fill.

    The same selection logic is applied in both pipelines so the only
    difference between them is the wording of the query, not how chunks
    are chosen.
    """
    mentioned_cos = mentioned_companies(question)
    mentioned_yrs = mentioned_years(question)

    # Fast path: no constraints to enforce.
    if len(mentioned_cos) < 2 and not mentioned_yrs:
        return retrieved[:max_chunks]

    selected: list[dict] = []
    seen_ids: set[str] = set()

    def _reserve(predicate) -> bool:
        """Find the first retrieved chunk matching predicate and reserve it.
        Returns True if a chunk was reserved, False otherwise."""
        if len(selected) >= max_chunks:
            return False
        for chunk in retrieved:
            if chunk.get("chunk_id") in seen_ids:
                continue
            if predicate(chunk):
                selected.append(chunk)
                seen_ids.add(chunk["chunk_id"])
                return True
        return False

    # Pass 1: (company, year) pairs when both kinds of constraint are present.
    # Try value-aware match first; fall back to plain (company, year) if no
    # value-bearing candidate exists.
    if mentioned_cos and mentioned_yrs:
        for company in mentioned_cos:
            for year in mentioned_yrs:
                reserved = _reserve(
                    lambda c, co=company, yr=year: (
                        c.get("company") == co
                        and c.get("year") == yr
                        and _has_value_pattern(c.get("text", ""), question)
                    )
                )
                if not reserved:
                    _reserve(
                        lambda c, co=company, yr=year: (
                            c.get("company") == co and c.get("year") == yr
                        )
                    )

    # Pass 1b: year-only reservations when no company is named.
    if mentioned_yrs and not mentioned_cos:
        for year in mentioned_yrs:
            reserved = _reserve(
                lambda c, yr=year: (
                    c.get("year") == yr and _has_value_pattern(c.get("text", ""), question)
                )
            )
            if not reserved:
                _reserve(lambda c, yr=year: c.get("year") == yr)

    # Pass 2: ensure each named company has at least one chunk, even if no
    # (company, year) reservation succeeded above.
    if len(mentioned_cos) >= 2:
        for company in mentioned_cos:
            if not any(s.get("company") == company for s in selected):
                _reserve(lambda c, co=company: c.get("company") == co)

    # Fill remaining slots by rank.
    for chunk in retrieved:
        if len(selected) >= max_chunks:
            break
        if chunk.get("chunk_id") in seen_ids:
            continue
        selected.append(chunk)
        seen_ids.add(chunk["chunk_id"])

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


def log_embedding_spend(
    question_id: str,
    total_tokens: int,
    extra: dict | None = None,
) -> float:
    """
    Append one embedding-spend record to TOKEN_SPEND_LOG and return computed cost.

    Tagged with `record_type: "retrieval_eval_query"` to match the convention
    the old eval script used, so existing token-spend dashboards keep working.
    """
    cost = round(total_tokens / 1_000_000 * EMBED_COST_PER_1M_TOKENS, 6)
    record = {
        "record_type": "retrieval_eval_query",
        "timestamp": datetime.now(UTC).isoformat(),
        "model": EMBEDDING_MODEL,
        "question_id": question_id,
        "total_tokens": int(total_tokens),
        "cost_usd": cost,
    }
    if extra:
        record.update(extra)
    TOKEN_SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TOKEN_SPEND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return cost


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
        "timestamp": datetime.now(UTC).isoformat(),
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
