"""Build a faithfulness worksheet for one or more runs.

Reads runs.jsonl + data/chunked/**/*.jsonl. For each requested run, prints the
model answer followed by the FULL TEXT of every chunk that reached the prompt,
grouped (for multi-step) by the sub-question step that retrieved it.

This does NOT score faithfulness. It lays out answer + real source text so a
human (or a judge) can trace each claim to a chunk. Reads only; writes one .md.

Usage (from repo root):
    python evaluation/make_faithfulness_sheet.py 40           # one run
    python evaluation/make_faithfulness_sheet.py 40 53 48     # several runs
    python evaluation/make_faithfulness_sheet.py --all        # all 24
"""

import argparse
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "logs" / "runs.jsonl"
CHUNK_DIR = ROOT / "data" / "chunked"
OUT_DIR = ROOT / "evaluation"


def fix_mojibake(s: str) -> str:
    if not s:
        return s
    try:
        s = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return s.replace("???", "'")


def load_chunk_text() -> dict:
    """Build {chunk_id: text} from every .jsonl under data/chunked/."""
    lookup = {}
    files = glob.glob(str(CHUNK_DIR / "**" / "*.jsonl"), recursive=True)
    for fp in files:
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cid = obj.get("chunk_id")
                if cid:
                    lookup[cid] = obj.get("text", "")
    return lookup


def load_runs() -> dict:
    runs = {}
    with RUNS.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                runs[r["run_id"]] = r
    return runs


def short_model(m: str) -> str:
    return "235B" if "235" in str(m) else "30B"


def build_sheet(run: dict, chunk_text: dict) -> str:
    rid = run["run_id"]
    pipe = run.get("pipeline_type", "?")
    model = short_model(run.get("model", ""))
    L = []
    L.append(f"# Faithfulness sheet — run {rid} · {pipe} · {model}\n")
    L.append(f"**Question:** {fix_mojibake(run.get('question', ''))}\n")
    L.append("## Model answer\n")
    L.append(fix_mojibake(run.get("answer", "")) + "\n")
    L.append("## Retrieved chunk text\n")
    L.append(
        "Trace each claim in the answer above to the text below. "
        "A claim is *faithful* only if a chunk here actually contains it.\n"
    )

    step_chunks = run.get("step_chunks")
    if pipe == "multi_step" and step_chunks:
        # group chunk text by the step that retrieved it
        by_step = {}
        for sc in step_chunks:
            by_step.setdefault(sc["step_index"], []).append(sc["chunk_id"])
        subs = run.get("sub_questions", [])
        for step in sorted(by_step):
            sq = subs[step - 1] if step - 1 < len(subs) else "(no sub-question text)"
            L.append(f"### Step {step}: {fix_mojibake(sq)}\n")
            seen = set()
            for cid in by_step[step]:
                if cid in seen:
                    continue
                seen.add(cid)
                txt = chunk_text.get(cid)
                if txt is None:
                    L.append(f"- **{cid}** — ⚠️ NOT FOUND in data/chunked")
                else:
                    L.append(f"- **{cid}**: {fix_mojibake(txt).strip()}")
            L.append("")
    else:
        # single-shot: flat list
        seen = set()
        for cid in run.get("selected_chunk_ids", []):
            if cid in seen:
                continue
            seen.add(cid)
            txt = chunk_text.get(cid)
            if txt is None:
                L.append(f"- **{cid}** — ⚠️ NOT FOUND in data/chunked")
            else:
                L.append(f"- **{cid}**: {fix_mojibake(txt).strip()}")
        L.append("")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--runs",
        nargs="*",
        type=int,
        default=None,
        help="specific run_id(s); default = all runs in runs.jsonl",
    )
    args = ap.parse_args()

    runs = load_runs()
    targets = sorted(runs) if args.runs is None else args.runs

    print("Loading chunk text from data/chunked ...")
    chunk_text = load_chunk_text()
    print(f"  loaded {len(chunk_text):,} chunks")

    sections = []
    total_missing = 0
    for rid in targets:
        if rid not in runs:
            print(f"  run {rid}: NOT in runs.jsonl, skipping")
            continue
        sheet = build_sheet(runs[rid], chunk_text)
        total_missing += sheet.count("NOT FOUND")
        sections.append(sheet)

    header = (
        "# Faithfulness evaluation — all runs\n\n"
        f"{len(sections)} runs. For each: the model answer, then the full text of "
        "every retrieved chunk (grouped by sub-question step for multi-step runs). "
        "Faithfulness = is each claim grounded in this retrieved text. Corpus "
        "extraction errors are noted separately from model behaviour.\n\n"
        "---\n\n"
    )
    combined = header + "\n\n---\n\n".join(sections) + "\n"

    out = OUT_DIR / "faithfulness_all.md"
    out.write_text(combined, encoding="utf-8")
    flag = f"  ⚠️ {total_missing} chunk(s) not found across all runs" if total_missing else ""
    print(f"\nWrote {out}  ({len(combined):,} chars){flag}")


if __name__ == "__main__":
    main()
