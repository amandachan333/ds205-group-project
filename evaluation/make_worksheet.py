"""Phase 1: build a human-readable scoring worksheet from runs.jsonl + ground_truth.md.
Reads only from logs/ and evaluation/. Writes evaluation/scoring_worksheet.md.
No DB access, no model calls.

Re-runnable. If the worksheet already contains filled-in scores, it will NOT
overwrite unless you pass --force, so you can't lose scoring work by accident.

Usage (from repo root):
    python evaluation/make_worksheet.py
    python evaluation/make_worksheet.py --force   # overwrite even if scores exist
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root (one level above evaluation/)
RUNS = ROOT / "logs" / "runs.jsonl"
GT = ROOT / "evaluation" / "ground_truth.md"
OUT = ROOT / "evaluation" / "scoring_worksheet.md"


def fix_mojibake(s: str) -> str:
    """Repair UTF-8-as-Latin1 artifacts so the worksheet is readable."""
    if not s:
        return s
    try:
        s = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    s = s.replace("???", "'")
    return s


def parse_ground_truth(text: str) -> dict:
    """Return {q_number: {'question':..., 'expected':...}} keyed by Q number (1-based)."""
    text = fix_mojibake(text)
    blocks = re.split(r"\n##\s+Question\s+(\d+)\s*\n", text)
    out = {}
    for i in range(1, len(blocks), 2):
        qnum = int(blocks[i])
        body = blocks[i + 1]
        m_q = re.search(r"\*\*Question\*\*:\s*(.+?)(?=\n\s*\*\*Expected answer\*\*)", body, re.DOTALL)
        m_a = re.search(r"\*\*Expected answer\*\*:\s*(.+?)(?=\n\s*\*\*Sources\*\*)", body, re.DOTALL)
        out[qnum] = {
            "question": (m_q.group(1).strip() if m_q else "(could not parse question)"),
            "expected": (m_a.group(1).strip() if m_a else "(could not parse expected answer)"),
        }
    return out


def load_runs() -> list:
    runs = []
    with RUNS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    return runs


def short_model(m: str) -> str:
    return "235B" if "235" in str(m) else "30B"


def worksheet_has_scores(path: Path) -> bool:
    """Heuristic: detect if the existing worksheet has any filled-in score lines."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    # Any 'correctness (...)' line that has non-whitespace after the colon = filled in
    for m in re.finditer(r"correctness \(correct / partial / incorrect\):(.*)", text):
        if m.group(1).strip():
            return True
    # Or any data row in the answers table with a non-empty correctness cell
    for line in text.splitlines():
        if line.startswith("| ") and "|" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            # header/separator rows won't have a numeric run_id in cell 0
            if len(cells) >= 5 and cells[0].isdigit() and cells[4]:
                return True
    return False


def build(runs: list, gt: dict) -> str:
    def norm(s):
        return " ".join(fix_mojibake(s).split()).lower()

    gt_by_norm = {norm(v["question"]): qn for qn, v in gt.items()}
    for r in runs:
        r["_qnum"] = gt_by_norm.get(norm(r.get("question", "")), None)

    runs.sort(key=lambda r: (r["_qnum"] or 99,
                             r.get("pipeline_type", ""),
                             short_model(r.get("model", ""))))

    lines = []
    lines.append("# Scoring Worksheet\n")
    lines.append("For each run: read the model answer against the ground truth, then record "
                 "your scores in the **answers table at the very bottom** of this file.\n")
    lines.append(f"- Total runs: {len(runs)}")
    unmatched = [r['run_id'] for r in runs if r['_qnum'] is None]
    if unmatched:
        lines.append(f"- WARNING: runs whose question did NOT match ground truth: {unmatched}")
    lines.append("\n---\n")

    for r in runs:
        qnum, rid = r["_qnum"], r["run_id"]
        pipe = r.get("pipeline_type", "?")
        model = short_model(r.get("model", ""))
        lines.append(f"## run {rid} — Q{qnum} · {pipe} · {model}")

        if qnum and qnum in gt:
            lines.append(f"\n**Question:** {gt[qnum]['question']}\n")
            lines.append(f"**Ground-truth answer:**\n\n> {gt[qnum]['expected']}\n")
        else:
            lines.append(f"\n**Question (from run, no GT match):** {fix_mojibake(r.get('question',''))}\n")

        if pipe == "multi_step" and r.get("sub_questions"):
            lines.append(f"**Sub-questions ({r.get('n_sub_questions', len(r['sub_questions']))}):**\n")
            for i, sq in enumerate(r["sub_questions"], 1):
                lines.append(f"{i}. {fix_mojibake(sq)}")
            lines.append("")

        lines.append("**Model answer:**\n")
        lines.append(fix_mojibake(r.get("answer", "")) + "\n")

        chunks = list(dict.fromkeys(r.get("selected_chunk_ids", [])))
        lines.append(f"**Chunks that reached the prompt ({len(chunks)} unique):**\n")
        lines.append(", ".join(chunks) if chunks else "(none)")
        lines.append("")

        lines.append(f"**Cost/latency:** {r.get('total_tokens','?'):,} tokens · "
                     f"{r.get('latency_seconds',0):.1f}s · ${r.get('total_cost_usd',0):.4f}\n")

        lines.append("> **My scores for this run:**")
        lines.append("> - correctness (correct / partial / incorrect): ")
        lines.append("> - faithfulness (claims supported / total claims, e.g. 3/4): ")
        lines.append("> - notes: ")
        lines.append("\n---\n")

    lines.append("## ANSWERS TABLE (fill this in — Phase 3 reads this)\n")
    lines.append("Copy your scores here once you've read every run above.\n")
    lines.append("| run_id | Q | pipeline | model | correctness | faithfulness | notes |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in runs:
        lines.append(f"| {r['run_id']} | Q{r['_qnum']} | {r.get('pipeline_type','')} "
                     f"| {short_model(r.get('model',''))} |  |  |  |")
    lines.append("")
    return "\n".join(lines), unmatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite even if the existing worksheet has filled-in scores")
    args = ap.parse_args()

    if worksheet_has_scores(OUT) and not args.force:
        print(f"REFUSING to overwrite: {OUT} already contains filled-in scores.")
        print("Re-run with --force if you really want to regenerate and lose them.")
        sys.exit(1)

    gt = parse_ground_truth(GT.read_text(encoding="utf-8"))
    runs = load_runs()
    content, unmatched = build(runs, gt)
    OUT.write_text(content, encoding="utf-8")

    print(f"Wrote {OUT}")
    print(f"  {len(runs)} runs")
    print(f"  matched to ground truth: {sum(1 for r in runs if r['_qnum'])}/{len(runs)}")
    if unmatched:
        print(f"  WARNING unmatched run_ids: {unmatched}")


if __name__ == "__main__":
    main()