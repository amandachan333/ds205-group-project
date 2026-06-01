"""
run_all.py
----------
Run one of the pipelines against every question in ground_truth.md
sequentially. Each pipeline invocation is a fresh subprocess so SQLite
state, logging, and error handling work exactly as they would for a manual
`--question "..."` call.

Usage
~~~~~

    # Multi-step on all 6 GT questions with the default model
    python pipelines/run_all.py --pipeline multi_step

    # Single-shot with 235B
    python pipelines/run_all.py --pipeline single_shot \\
        --model "Qwen/Qwen3-235B-A22B-Instruct-2507"

    # Only specific questions
    python pipelines/run_all.py --pipeline multi_step --questions Q1,Q3,Q5

    # Pass through extra flags to the pipeline (--resume etc)
    python pipelines/run_all.py --pipeline multi_step --extra-args "--resume"

    # Dry run: print commands but don't execute
    python pipelines/run_all.py --pipeline multi_step --dry-run

Failures don't abort the run - other questions continue. A summary at the end
lists which question_ids failed. Exit code is non-zero iff any question failed.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

GROUND_TRUTH_MD = Path("evaluation/ground_truth.md")


def parse_questions(md_path: Path) -> list[tuple[str, str]]:
    """Return [(qid, question_text), ...] parsed from ground_truth.md."""
    if not md_path.exists():
        sys.exit(f"Ground truth file not found: {md_path}")

    questions: list[tuple[str, str]] = []
    current_qid: str | None = None
    header_re = re.compile(r"^##\s+Question\s*(\d+)")

    with md_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            m = header_re.match(stripped)
            if m:
                current_qid = f"Q{m.group(1)}"
                continue
            if current_qid and stripped.startswith("**Question**:"):
                q_text = stripped.split("**Question**:", 1)[1].strip()
                if q_text:
                    questions.append((current_qid, q_text))
                current_qid = None
    return questions


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a pipeline over every question in ground_truth.md.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--pipeline",
        required=True,
        choices=["single_shot", "multi_step"],
        help="Which pipeline to invoke.",
    )
    parser.add_argument(
        "--model", default=None, help="Generation model name (passed through to the pipeline)."
    )
    parser.add_argument(
        "--questions",
        default=None,
        help="Comma-separated qids to run (e.g., 'Q1,Q3,Q5'). Default: all.",
    )
    parser.add_argument(
        "--ground-truth", type=Path, default=GROUND_TRUTH_MD, help="Path to ground_truth.md."
    )
    parser.add_argument(
        "--extra-args",
        type=str,
        default="",
        help="Extra args to pass to the pipeline (shell-quoted).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the commands without executing them."
    )
    args = parser.parse_args()

    questions = parse_questions(args.ground_truth)
    if not questions:
        sys.exit("No questions parsed from ground_truth.md.")

    if args.questions:
        wanted = {q.strip().upper() for q in args.questions.split(",") if q.strip()}
        questions = [(qid, q) for qid, q in questions if qid in wanted]
        if not questions:
            sys.exit(f"No questions match: {args.questions}")

    pipeline_script = Path(__file__).parent / f"{args.pipeline}_rag.py"
    if not pipeline_script.exists():
        sys.exit(f"Pipeline script not found: {pipeline_script}")

    extra_args = shlex.split(args.extra_args) if args.extra_args else []

    print(f"Pipeline:    {args.pipeline}")
    print(f"Model:       {args.model or '(pipeline default)'}")
    print(f"Questions:   {len(questions)}  ({', '.join(qid for qid, _ in questions)})")
    if extra_args:
        print(f"Extra args:  {' '.join(extra_args)}")
    print()

    failures: list[tuple[str, int]] = []
    total_start = time.time()

    for i, (qid, q) in enumerate(questions, 1):
        cmd: list[str] = [sys.executable, str(pipeline_script), "--question", q]
        if args.model:
            cmd.extend(["--model", args.model])
        cmd.extend(extra_args)

        print("=" * 70)
        print(f"  [{i}/{len(questions)}]  {qid}: {q[:80]}{'...' if len(q) > 80 else ''}")
        print("=" * 70)

        if args.dry_run:
            print("   " + " ".join(shlex.quote(c) for c in cmd))
            continue

        start = time.time()
        result = subprocess.run(cmd)
        elapsed = time.time() - start

        if result.returncode != 0:
            failures.append((qid, result.returncode))
            print(f"\n   !!! {qid} FAILED  (exit {result.returncode}, {elapsed:.1f}s)\n")
        else:
            print(f"\n   >>> {qid} OK  ({elapsed:.1f}s)\n")

    total_elapsed = time.time() - total_start

    print("=" * 70)
    print(
        f"  Done in {total_elapsed:.1f}s.  {len(questions) - len(failures)}/{len(questions)} succeeded."
    )
    if failures:
        print(f"  Failures: {', '.join(f'{qid}(exit {rc})' for qid, rc in failures)}")
    print("=" * 70)

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
