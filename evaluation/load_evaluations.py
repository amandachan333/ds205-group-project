"""Load manual scores from scoring_worksheet.md into the evaluations table.

Reads the ANSWERS TABLE in evaluation/scoring_worksheet.md and inserts one
evaluations row per scored run via db.database.insert_evaluation().

- Only touches the `evaluations` table. Never modifies runs/steps/answers/etc.
- Idempotent: deletes existing evaluations for the scored run_ids before
  re-inserting, so re-running does not create duplicates.
- Validates every run_id exists and is unique in the DB before writing.

Usage (from repo root):
    python evaluation/load_evaluations.py            # apply
    python evaluation/load_evaluations.py --dry-run  # parse + validate only, no writes
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSHEET = ROOT / "evaluation" / "scoring_worksheet.md"
DB_PATH = ROOT / "db" / "benchmark.db"

# import the existing DB layer rather than touching sqlite directly
sys.path.insert(0, str(ROOT))
from db import database  # noqa: E402


def parse_answers_table(path: Path) -> list[dict]:
    """Read contiguous data rows under the ANSWERS TABLE heading. Stop at the
    first non-data row so the later aggregate table is never picked up."""
    text = path.read_text(encoding="utf-8")
    idx = text.find("ANSWERS TABLE")
    if idx == -1:
        raise SystemExit("Could not find 'ANSWERS TABLE' heading in worksheet.")

    rows = []
    started = False
    for line in text[idx:].splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if started:
                break  # table ended
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells or not cells[0].isdigit():
            # header or separator row; keep scanning until data starts
            if started:
                break
            continue
        started = True
        # columns: run_id | Q | pipeline | model | correctness | faithfulness | notes
        if len(cells) < 7:
            raise SystemExit(f"Malformed row (need 7 cells): {line}")
        rows.append({
            "run_id": int(cells[0]),
            "q": cells[1],
            "pipeline": cells[2],
            "model": cells[3],
            "correctness": cells[4],
            "faithfulness": cells[5],
            "notes": cells[6],
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and validate only; write nothing")
    args = ap.parse_args()

    rows = parse_answers_table(WORKSHEET)
    print(f"Parsed {len(rows)} scored runs from {WORKSHEET.name}")

    conn = database.get_connection(str(DB_PATH))

    # Validate: every run_id must exist exactly once in runs
    problems = []
    for r in rows:
        hit = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?", (r["run_id"],)
        ).fetchone()[0]
        if hit != 1:
            problems.append((r["run_id"], hit))
    if problems:
        for rid, n in problems:
            print(f"  run_id {rid}: found {n} matching runs (expected 1)")
        raise SystemExit("Aborting: run_id validation failed. No writes made.")

    # Basic sanity on the fraction format
    frac = re.compile(r"^\d+/\d+$")
    for r in rows:
        for field in ("correctness", "faithfulness"):
            if not frac.match(r[field]):
                print(f"  WARNING run {r['run_id']}: {field}='{r[field]}' "
                      f"is not a k/n fraction")

    if args.dry_run:
        print("\n--dry-run: validation passed, no rows written.")
        for r in rows:
            print(f"  {r['run_id']:>3} {r['q']} {r['pipeline']:<11} {r['model']:<4} "
                  f"corr={r['correctness']:<4} faith={r['faithfulness']}")
        return

    scored_ids = [r["run_id"] for r in rows]
    # Idempotent: clear only the rows this loader owns
    placeholders = ",".join("?" * len(scored_ids))
    deleted = conn.execute(
        f"DELETE FROM evaluations WHERE run_id IN ({placeholders})", scored_ids
    ).rowcount
    conn.commit()
    if deleted:
        print(f"Cleared {deleted} existing evaluation row(s) for these run_ids.")

    for r in rows:
        database.insert_evaluation(
            conn,
            run_id=r["run_id"],
            correctness=r["correctness"],
            faithfulness_score=r["faithfulness"],
            evaluator_notes=r["notes"],
        )

    total = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    print(f"\nDone. evaluations table now holds {total} rows.")


if __name__ == "__main__":
    main()