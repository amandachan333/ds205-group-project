"""
dump_runs.py
------------
Read completed runs from the benchmark SQLite database and emit one JSONL
record per run, in a shape that is symmetric across pipelines so single-shot
and multi-step results can be analysed side-by-side.

Both pipelines now persist their retrieved chunks to the `steps` table, so
chunk-level info comes from a single source of truth.

Output schema (one JSON object per line):

    run_id                  int      SQLite primary key for the run
    pipeline_type           str      'single_shot' | 'multi_step'
    model                   str      generation model name
    timestamp_utc           str      completed_at or started_at
    question_id             str      uuid
    question                str      original question text
    answer                  str      final assembled answer
    citations               list     [(source, page) tuples extracted from answer]
    total_tokens            int      prompt + completion across the run
    latency_seconds         float    wall-clock for the run
    total_cost_usd          float    cost from token spend
    status                  str      'complete' | 'failed' | 'running'
    n_sub_questions         int      0 for single_shot, N for multi_step
    sub_questions           list     decomposition (empty for single_shot)
    selected_chunk_ids      list     union of chunks that reached the LLM
    selected_pages          list     page nsumbers, aligned with chunk_ids
    selected_document_labels list    document labels, aligned with chunk_ids
    correctness             str|null fraction of key claims correct, e.g. "3/5"
                                     (null for unscored runs); compare WITHIN a
                                     question only, never averaged across questions
    faithfulness_score      str|null (supported + corpus-error)/total claims as a
                                     fraction (null for unscored runs)
    evaluator_notes         str|null verdict tag + free-text notes (null if unscored)
    step_chunks             list     multi_step only: per-step chunk breakdown
                                     [{step_index, chunk_id, page_number, document_label}, ...]

Usage
~~~~~

    # Default: dump all complete runs to logs/runs.jsonl, overwriting
    python pipelines/dump_runs.py

    # Filter by pipeline / model / specific run_ids
    python pipelines/dump_runs.py --pipeline multi_step
    python pipelines/dump_runs.py --model "Qwen/Qwen3-30B-A3B-Instruct-2507"
    python pipelines/dump_runs.py --run-ids 31,32,33

    # Append rather than overwrite (useful between batches)
    python pipelines/dump_runs.py --append

    # Custom output path
    python pipelines/dump_runs.py --output benchmark_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import LOG_DIR

log = logging.getLogger(__name__)

DEFAULT_DB = Path("db/benchmark.db")
DEFAULT_OUTPUT = LOG_DIR / "runs.jsonl"

_CITATION_RE = re.compile(r"\(([^()]+?),\s*p\.?\s*(\d+)\)")


def extract_citations(answer: str | None) -> list[str]:
    """Pull parenthetical (source, page) citations from a final answer."""
    out: list[str] = []
    if not answer:
        return out
    for m in _CITATION_RE.finditer(answer):
        c = f"{m.group(1).strip()}, p.{m.group(2)}"
        if c not in out:
            out.append(c)
    return out


def get_run_sub_questions(conn: sqlite3.Connection, run_id: int) -> list[str]:
    """Return the decomposition sub_questions for a run, [] if none.

    Only multi_step runs have a decompositions row; single_shot returns [].
    """
    decomp = conn.execute(
        "SELECT sub_questions FROM decompositions WHERE run_id = ? AND status = 'complete'",
        (run_id,),
    ).fetchone()
    if not decomp or not decomp["sub_questions"]:
        return []
    try:
        return json.loads(decomp["sub_questions"])
    except json.JSONDecodeError:
        return []


def get_run_step_chunks(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """
    Return the chunks consulted by a run, aggregated across all completed
    steps. Works the same way for both pipelines:

      - single_shot writes one step row (step_index=1) with the chunks
        that went into the single prompt.
      - multi_step writes one step row per sub-question; this function
        returns the union across all of them, each entry tagged with
        the step_index it came from.

    Each entry: {step_index, chunk_id, page_number, document_label}.
    """
    chunk_summary: list[dict] = []
    rows = conn.execute(
        """
        SELECT step_index, retrieved_chunks
        FROM steps
        WHERE run_id = ? AND status = 'complete'
        ORDER BY step_index ASC
        """,
        (run_id,),
    ).fetchall()
    for r in rows:
        rc = r["retrieved_chunks"]
        if not rc:
            continue
        try:
            chunks = json.loads(rc)
        except json.JSONDecodeError:
            continue
        for c in chunks:
            chunk_summary.append(
                {
                    "step_index": r["step_index"],
                    "chunk_id": c.get("chunk_id"),
                    "page_number": c.get("page_number"),
                    "document_label": c.get("document_label"),
                }
            )
    return chunk_summary


def get_run_evaluation(conn: sqlite3.Connection, run_id: int) -> dict:
    """Return the manual evaluation for a run, or null fields if unscored.

    The evaluations table holds the 24 manually-scored benchmark runs.
    Any run without a matching row (e.g. the dev-iteration runs) gets
    None fields — i.e. LEFT-JOIN semantics, not an inner join that would
    silently drop unscored runs.
    """
    row = conn.execute(
        """
        SELECT correctness, faithfulness_score, evaluator_notes
        FROM evaluations
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if not row:
        return {"correctness": None, "faithfulness_score": None, "evaluator_notes": None}
    return {
        "correctness": row["correctness"],
        "faithfulness_score": row["faithfulness_score"],
        "evaluator_notes": row["evaluator_notes"],
    }


def build_record(conn: sqlite3.Connection, run: sqlite3.Row) -> dict:
    """Construct one JSONL record from a runs row."""
    q_row = conn.execute(
        "SELECT question_text FROM questions WHERE question_id = ?",
        (run["question_id"],),
    ).fetchone()
    question_text = q_row["question_text"] if q_row else ""

    final = conn.execute(
        "SELECT final_answer FROM final_answers WHERE run_id = ?",
        (run["run_id"],),
    ).fetchone()
    answer = final["final_answer"] if final else ""

    record: dict = {
        "run_id": run["run_id"],
        "pipeline_type": run["pipeline_type"],
        "model": run["model_name"],
        "timestamp_utc": run["completed_at"] or run["started_at"],
        "question_id": run["question_id"],
        "question": question_text,
        "answer": answer,
        "citations": extract_citations(answer),
        "total_tokens": run["total_tokens"],
        "latency_seconds": run["latency_seconds"],
        "total_cost_usd": run["total_cost_usd"],
        "status": run["status"],
        "n_sub_questions": 0,
        "sub_questions": [],
        "selected_chunk_ids": [],
        "selected_pages": [],
        "selected_document_labels": [],
    }

    # Manual evaluation scores LEFT-JOINed from the evaluations table.
    # Unscored runs carry null fields rather than being dropped.
    record.update(get_run_evaluation(conn, run["run_id"]))

    # Sub-questions only exist for multi_step (decompositions table).
    if run["pipeline_type"] == "multi_step":
        sub_qs = get_run_sub_questions(conn, run["run_id"])
        record["n_sub_questions"] = len(sub_qs)
        record["sub_questions"] = sub_qs

    # Chunk-level info comes from the steps table for both pipelines. For
    # single_shot, one step row at step_index=1 holds the prompt's chunks.
    # For multi_step, multiple step rows are aggregated.
    chunk_summary = get_run_step_chunks(conn, run["run_id"])
    if chunk_summary:
        record["selected_chunk_ids"] = [c["chunk_id"] for c in chunk_summary]
        record["selected_pages"] = [c["page_number"] for c in chunk_summary]
        record["selected_document_labels"] = [c["document_label"] for c in chunk_summary]
        if run["pipeline_type"] == "multi_step":
            record["step_chunks"] = chunk_summary

    return record


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump completed runs from benchmark.db to JSONL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="Path to the benchmark SQLite database."
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=DEFAULT_OUTPUT, help="Output JSONL path."
    )
    parser.add_argument(
        "--append", action="store_true", help="Append to output instead of overwriting."
    )
    parser.add_argument(
        "--pipeline",
        choices=["single_shot", "multi_step"],
        default=None,
        help="Only dump runs from one pipeline.",
    )
    parser.add_argument(
        "--model", type=str, default=None, help="Only dump runs from one model (exact match)."
    )
    parser.add_argument(
        "--run-ids",
        type=str,
        default=None,
        help="Comma-separated run_ids to include (otherwise all).",
    )
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include runs with status != 'complete' (failed, running).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.db.exists():
        sys.exit(f"Database not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    where: list[str] = []
    params: list = []
    if not args.include_incomplete:
        where.append("status = 'complete'")
    if args.pipeline:
        where.append("pipeline_type = ?")
        params.append(args.pipeline)
    if args.model:
        where.append("model_name = ?")
        params.append(args.model)
    if args.run_ids:
        ids = [int(x.strip()) for x in args.run_ids.split(",") if x.strip()]
        if ids:
            placeholders = ",".join("?" * len(ids))
            where.append(f"run_id IN ({placeholders})")
            params.extend(ids)

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    runs = conn.execute(
        f"SELECT * FROM runs{where_clause} ORDER BY run_id ASC",
        tuple(params),
    ).fetchall()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    with args.output.open(mode, encoding="utf-8") as fh:
        for run in runs:
            rec = build_record(conn, run)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    conn.close()

    action = "Appended" if args.append else "Wrote"
    log.info("%s %d records to %s", action, len(runs), args.output)

    if runs:
        cells: Counter = Counter()
        for run in runs:
            cells[(run["pipeline_type"], run["model_name"])] += 1
        log.info("\nBreakdown by (pipeline, model):")
        for (pipe, model), n in sorted(cells.items()):
            log.info("  %-12s  %-50s  %d runs", pipe, model or "(unknown)", n)


if __name__ == "__main__":
    main()
