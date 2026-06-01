"""
Inspect intermediate sub-step answers from the `steps` table.

Usage (run from project root):
    python evaluation/inspect_intermediate_answers.py 48                 # one run
    python evaluation/inspect_intermediate_answers.py 48 36 60 54        # several runs
    python evaluation/inspect_intermediate_answers.py 67 68 --md out.md  # also write markdown to file

At least one run_id is required.

Reads from the path defined in config.DB_PATH. Read-only — no writes.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from config import BENCHMARK_DB_PATH as DB_PATH


def fetch(run_ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in run_ids)
    sql = f"""
        SELECT
            r.run_id,
            r.pipeline_type,
            r.model_name,
            r.question_id,
            s.step_index,
            s.sub_question,
            s.answer       AS intermediate_answer,
            s.retrieved_chunks
        FROM runs r
        JOIN steps s ON s.run_id = r.run_id
        WHERE r.run_id IN ({placeholders})
          AND s.status = 'complete'
        ORDER BY r.run_id, s.step_index
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, run_ids))
    finally:
        conn.close()


def render(rows: list[sqlite3.Row]) -> str:
    out: list[str] = []
    current_run: int | None = None
    for r in rows:
        if r["run_id"] != current_run:
            current_run = r["run_id"]
            out.append("")
            out.append(
                f"## run {r['run_id']} — {r['question_id']} · {r['pipeline_type']} · {r['model_name']}"
            )
            out.append("")
        out.append(f"### step {r['step_index']}")
        out.append("")
        out.append(f"**sub-question:** {r['sub_question']}")
        out.append("")
        out.append("**intermediate answer:**")
        out.append("")
        out.append(r["intermediate_answer"] or "_(empty)_")
        out.append("")
        out.append(
            f"<details><summary>retrieved chunks (json)</summary>\n\n```json\n{r['retrieved_chunks']}\n```\n\n</details>"
        )
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_ids", nargs="+", type=int, help="one or more run_ids to inspect")
    ap.add_argument("--md", type=Path, help="optional: also write markdown to this path")
    args = ap.parse_args()

    print(f"reading {DB_PATH} | runs={args.run_ids}", file=sys.stderr)
    rows = fetch(args.run_ids)
    if not rows:
        print("no rows — check run_ids and that the runs completed", file=sys.stderr)
        sys.exit(1)

    md = render(rows)
    print(md)
    if args.md:
        args.md.write_text(md)
        print(f"\nwrote {args.md}", file=sys.stderr)


if __name__ == "__main__":
    main()
