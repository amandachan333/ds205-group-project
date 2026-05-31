"""
evaluate_retrieval.py
---------------------
Offline retrieval-quality check. No generation model is called - this exists
so we can validate retrieval *before* spending tokens on LLM calls.

Modes
~~~~~

Batch (default):
    Runs every question in ground_truth.md through hybrid retrieval and reports
    Recall@k, MRR, and the rank of each expected chunk per question.

Ad-hoc:
    `--query "..." --expected "TNB_2024_chunk_0014,TNB_2024_chunk_0177"`
    Probes a single arbitrary query against a set of target chunk_ids. Useful
    for diagnosing sub-question retrieval (sub-questions are not in the
    ground-truth file).

Both modes report two retrieval views:
    PRE  - what `hybrid_retrieve` returns at `--top-n` (the candidate set).
    POST - what `select_context_chunks` reserves at `--context-chunks` (what
           actually reaches the LLM after the company/year boost).

A chunk can appear in PRE but not POST if the boost reserved something else
ahead of it. Looking at both makes the boost behaviour debuggable.

Usage
~~~~~
    # Batch over ground truth at the pipeline defaults
    python pipelines/evaluate_retrieval.py

    # Try a wider candidate set
    python pipelines/evaluate_retrieval.py --top-n 60 --context-chunks 10

    # Single question, verbose top-K printout
    python pipelines/evaluate_retrieval.py --question-id Q1 --verbose

    # Ad-hoc sub-question diagnosis
    python pipelines/evaluate_retrieval.py \
        --query "What was Tenaga Nasional's emissions intensity in 2024?" \
        --expected "TNB_2024_chunk_0014,TNB_2024_chunk_0177,TNB_2024_chunk_0929"

Environment
~~~~~~~~~~~
    NEBIUS_API_KEY required (for query embedding).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval import (
    BM25_WEIGHT,
    RRF_K,
    get_corpus_inventory,
    hybrid_retrieve,
    load_index,
    make_client,
    select_context_chunks,
)

from config import DB_PATH, LOG_DIR
from utils import bootstrap_runtime_env, ensure_stage_dirs

bootstrap_runtime_env()
ensure_stage_dirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "evaluate_retrieval.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

DEFAULT_TOP_N = 40
DEFAULT_CONTEXT_CHUNKS = 10
DEFAULT_K_VALUES = [5, 10, 20, 40]
GROUND_TRUTH_MD = Path(__file__).resolve().parents[1] / "evaluation" / "ground_truth.md"


# ---------------------------------------------------------------------------
# Ground truth parsing
# ---------------------------------------------------------------------------


@dataclass
class GTQuestion:
    """One question parsed from ground_truth.md."""

    qid: str
    question: str
    expected_chunk_ids: list[str] = field(default_factory=list)


def parse_ground_truth(md_path: Path) -> list[GTQuestion]:
    """
    Parse the ground-truth markdown into a list of GTQuestion.

    Extracts only `chunk_id` references from source lines - those are the
    most precise identifiers and are present for every question in the
    current ground_truth.md. Document/page fallback is omitted on purpose:
    if a question lacks chunk_ids we'd rather flag it than guess.

    A single source line can contain multiple chunk_ids, e.g.:
        `"chunk_id": "TNB_2021_chunk_0413", "TNB_2021_chunk_0415"`
    All such ids are collected.
    """
    if not md_path.exists():
        sys.exit(f"Ground truth file not found: {md_path}")

    questions: list[GTQuestion] = []
    current: GTQuestion | None = None
    chunk_id_re = re.compile(r'"([A-Za-z][A-Za-z_0-9]*_\d{4}_chunk_\d+)"')

    with md_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip()
            stripped = line.lstrip()

            header_match = re.match(r"^##\s+Question\s*(\d+)", stripped)
            if header_match:
                if current is not None:
                    questions.append(current)
                current = GTQuestion(qid=f"Q{header_match.group(1)}", question="")
                continue

            if current is None:
                continue

            if stripped.startswith("**Question**:"):
                current.question = stripped.split("**Question**:", 1)[1].strip()
                continue

            for cid in chunk_id_re.findall(line):
                if cid not in current.expected_chunk_ids:
                    current.expected_chunk_ids.append(cid)

    if current is not None:
        questions.append(current)

    return questions


# ---------------------------------------------------------------------------
# Per-question evaluation
# ---------------------------------------------------------------------------


@dataclass
class QResult:
    qid: str
    question: str
    n_expected: int
    pre_ranks: dict[str, int | None]  # chunk_id -> 1-indexed rank in PRE (or None)
    post_present: dict[str, bool]  # chunk_id -> True if chunk made it into POST
    pre_recall_at_k: dict[int, float]
    pre_first_hit: int | None
    pre_mrr: float
    pre_chunks: list[dict]  # top-K from hybrid_retrieve
    post_chunks: list[dict]  # what select_context_chunks picked


def evaluate_query(
    *,
    qid: str,
    query: str,
    expected_chunk_ids: list[str],
    client,
    index,
    top_n: int,
    context_chunks_n: int,
    bm25_weight: int,
    rrf_k: int,
    k_values: list[int],
) -> QResult:
    """Run retrieval and compute pre/post-boost metrics for one query."""
    retrieved = hybrid_retrieve(
        query=query,
        client=client,
        index=index,
        k=top_n,
        bm25_weight=bm25_weight,
        rrf_k=rrf_k,
        question_id=qid,
    )
    selected = select_context_chunks(query, retrieved, context_chunks_n)

    rank_of: dict[str, int | None] = {cid: None for cid in expected_chunk_ids}
    for i, chunk in enumerate(retrieved, start=1):
        cid = chunk["chunk_id"]
        if cid in rank_of and rank_of[cid] is None:
            rank_of[cid] = i

    selected_ids = {c["chunk_id"] for c in selected}
    in_post: dict[str, bool] = {cid: cid in selected_ids for cid in expected_chunk_ids}

    # PRE-recall@k: fraction of expected chunks present in top-k of PRE
    pre_recall: dict[int, float] = {}
    for k in k_values:
        if not expected_chunk_ids:
            pre_recall[k] = 0.0
            continue
        hits = sum(
            1 for cid in expected_chunk_ids if rank_of[cid] is not None and rank_of[cid] <= k
        )
        pre_recall[k] = hits / len(expected_chunk_ids)

    pre_ranks_present = [r for r in rank_of.values() if r is not None]
    pre_first_hit = min(pre_ranks_present) if pre_ranks_present else None
    pre_mrr = (1.0 / pre_first_hit) if pre_first_hit else 0.0

    return QResult(
        qid=qid,
        question=query,
        n_expected=len(expected_chunk_ids),
        pre_ranks=rank_of,
        post_present=in_post,
        pre_recall_at_k=pre_recall,
        pre_first_hit=pre_first_hit,
        pre_mrr=pre_mrr,
        pre_chunks=retrieved,
        post_chunks=selected,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_rank(rank: int | None, top_n: int) -> str:
    if rank is None:
        return f"NOT IN TOP-{top_n}"
    return f"rank {rank:>3}"


def print_question_result(r: QResult, top_n: int, k_values: list[int], verbose: bool) -> None:
    """Print compact per-question diagnostic."""
    print()
    print(f"  {r.qid}: {r.question[:90]}{'...' if len(r.question) > 90 else ''}")
    print(f"  Expected: {r.n_expected} chunks")

    if r.n_expected == 0:
        print("    (no chunk_ids in ground truth for this question - skipping rank report)")
        return

    print("    expected_chunk_id                       | PRE        | POST")
    print("    " + "-" * 76)
    for cid in r.pre_ranks:
        pre = _format_rank(r.pre_ranks[cid], top_n)
        post = "in prompt" if r.post_present[cid] else "-"
        print(f"    {cid:<40s} | {pre:<10s} | {post}")

    recall_str = "  ".join(
        f"R@{k}: {r.pre_recall_at_k.get(k, 0.0):.2f}" for k in k_values if k <= top_n
    )
    print(
        f"    PRE metrics: {recall_str}   MRR: {r.pre_mrr:.4f}   first hit: {r.pre_first_hit or 'MISS'}"
    )

    if verbose:
        print(f"    --- PRE top-{min(top_n, 15)} from hybrid_retrieve ---")
        for i, c in enumerate(r.pre_chunks[:15], start=1):
            flag = "[E]" if c["chunk_id"] in r.pre_ranks else "   "
            print(
                f"      {i:>2}. {flag} {c['chunk_id']:<35s} p.{c.get('page_number', '?')}  rrf={c.get('rrf_score', 0.0):.5f}"
            )
        print(f"    --- POST select_context_chunks ({len(r.post_chunks)}) ---")
        for i, c in enumerate(r.post_chunks, start=1):
            flag = "[E]" if c["chunk_id"] in r.pre_ranks else "   "
            print(f"      {i:>2}. {flag} {c['chunk_id']:<35s} p.{c.get('page_number', '?')}")


def print_aggregate(results: list[QResult], top_n: int, k_values: list[int]) -> None:
    n = len(results)
    if n == 0:
        return
    valid = [r for r in results if r.n_expected > 0]
    n_valid = len(valid)
    if n_valid == 0:
        print("\n  AGGREGATE: no questions with chunk_ids to score.")
        return

    print()
    print("=" * 80)
    print(f"  AGGREGATE over {n_valid} questions")
    print("=" * 80)
    for k in k_values:
        if k > top_n:
            continue
        avg = sum(r.pre_recall_at_k.get(k, 0.0) for r in valid) / n_valid
        print(f"  Mean PRE-Recall@{k:<3d}: {avg:.4f}")
    avg_mrr = sum(r.pre_mrr for r in valid) / n_valid
    print(f"  Mean PRE-MRR       : {avg_mrr:.4f}")

    # POST coverage: fraction of expected chunks that made it past selection
    total_expected = sum(r.n_expected for r in valid)
    total_in_post = sum(sum(1 for v in r.post_present.values() if v) for r in valid)
    post_coverage = total_in_post / total_expected if total_expected else 0.0
    print(
        f"  POST coverage      : {post_coverage:.4f}  ({total_in_post}/{total_expected} expected chunks made it to the prompt)"
    )

    print()
    if post_coverage >= 0.70:
        verdict = "GOOD - retrieval surfaces most expected chunks to the prompt."
    elif post_coverage >= 0.45:
        verdict = "PARTIAL - some questions are missing key chunks; consider raising --top-n or adjusting selection."
    else:
        verdict = (
            "LOW - retrieval is the bottleneck; investigate chunker, embeddings, or reranking."
        )
    print(f"  Verdict: {verdict}")


def save_results_jsonl(results: list[QResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(
                json.dumps(
                    {
                        "qid": r.qid,
                        "question": r.question,
                        "n_expected": r.n_expected,
                        "pre_ranks": r.pre_ranks,
                        "post_present": r.post_present,
                        "pre_recall_at_k": {str(k): v for k, v in r.pre_recall_at_k.items()},
                        "pre_first_hit": r.pre_first_hit,
                        "pre_mrr": r.pre_mrr,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    log.info("Results saved to %s", path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval quality against ground-truth chunk_ids. No LLM generation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to vector_store.db.")
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help="Candidate-set size for hybrid_retrieve (PRE).",
    )
    parser.add_argument(
        "--context-chunks",
        type=int,
        default=DEFAULT_CONTEXT_CHUNKS,
        help="Slots passed to select_context_chunks (POST).",
    )
    parser.add_argument(
        "--bm25-weight", type=int, default=BM25_WEIGHT, help="Relative BM25 weight in RRF fusion."
    )
    parser.add_argument("--rrf-k", type=int, default=RRF_K, help="Reciprocal rank fusion constant.")
    parser.add_argument(
        "--k",
        type=int,
        action="append",
        dest="k_values",
        help="Recall@k cutoff (repeatable). Default: %(default)s.",
    )
    parser.add_argument(
        "--ground-truth", type=Path, default=GROUND_TRUTH_MD, help="Path to ground_truth.md."
    )
    parser.add_argument(
        "--question-id", type=str, default=None, help="Run a single question (e.g. Q1)."
    )
    parser.add_argument(
        "--query", type=str, default=None, help="Ad-hoc query (overrides --question-id)."
    )
    parser.add_argument(
        "--expected",
        type=str,
        default=None,
        help="Comma-separated chunk_ids for --query (the expected hits).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=LOG_DIR / "evaluate_retrieval.jsonl",
        help="JSONL output path.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print full top-K and post-selection chunks per question.",
    )
    parser.add_argument(
        "--show-corpus", action="store_true", help="Print the corpus inventory string and exit."
    )
    args = parser.parse_args()

    k_values = sorted(set(args.k_values)) if args.k_values else DEFAULT_K_VALUES

    if args.show_corpus:
        print(get_corpus_inventory(args.db))
        return

    # Validate ad-hoc mode arguments
    if args.query is not None:
        if not args.expected:
            parser.error("--query requires --expected with at least one chunk_id.")
        expected = [c.strip() for c in args.expected.split(",") if c.strip()]
        if not expected:
            parser.error("--expected was empty after parsing.")
        questions = [GTQuestion(qid="adhoc", question=args.query, expected_chunk_ids=expected)]
    else:
        questions = parse_ground_truth(args.ground_truth)
        if args.question_id:
            questions = [q for q in questions if q.qid == args.question_id]
            if not questions:
                parser.error(f"Question id not found in ground truth: {args.question_id}")

    log.info(
        "Settings: top_n=%d, context_chunks=%d, bm25_weight=%d, rrf_k=%d",
        args.top_n,
        args.context_chunks,
        args.bm25_weight,
        args.rrf_k,
    )

    index = load_index(args.db)
    client = make_client()

    results: list[QResult] = []
    print("=" * 80)
    print("  RETRIEVAL EVALUATION  (no LLM generation)")
    print("=" * 80)
    for q in questions:
        log.info("Evaluating %s ...", q.qid)
        result = evaluate_query(
            qid=q.qid,
            query=q.question,
            expected_chunk_ids=q.expected_chunk_ids,
            client=client,
            index=index,
            top_n=args.top_n,
            context_chunks_n=args.context_chunks,
            bm25_weight=args.bm25_weight,
            rrf_k=args.rrf_k,
            k_values=k_values,
        )
        results.append(result)
        print_question_result(result, args.top_n, k_values, args.verbose)

    print_aggregate(results, args.top_n, k_values)
    save_results_jsonl(results, args.output)


if __name__ == "__main__":
    main()
