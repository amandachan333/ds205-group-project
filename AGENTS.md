# AGENTS.md

Operational guide for AI coding agents (and humans) working in this repo: how to set up,
run, test, and modify the codebase, and what not to touch. Read this before doing any
work here.

**Looking for how AI tools were *used to build* this project** — development history,
evidence of human oversight, prompt patterns that worked? That lives in
[`docs/AI_USAGE.md`](docs/AI_USAGE.md), not this file. This file is forward-looking instructions; that one
is a backward-looking record.

For *why* things are built this way, read `DECISIONS.md`; for module responsibilities
and the SQLite schema, read `CONTRIBUTING.md`.

## Start every session here

Before writing or changing anything, read these three files and confirm you have:

```
README.md         — what the pipelines do, how to run them, what they output
CONTRIBUTING.md   — module responsibilities, SQLite schema, conventions, known limits
DECISIONS.md      — locked architectural choices and their rationale
```

They are not interchangeable: README is user-facing behaviour, CONTRIBUTING is
implementation convention, DECISIONS is settled rationale. Skipping any one of them
leads to plausible-but-wrong defaults (a `main.py` entry point, `requirements.txt`,
`print()` instead of `logging`, or re-proposing components that were deliberately cut).

Then follow these working practices:

- **Plan before implementing.** For any non-trivial function or module, describe in
  plain English what it does, its inputs/outputs, and edge cases, and get confirmation
  before writing code.
- **Search before you write a helper.** Utility functions live in `utils.py` and
  `config.py`. Do not re-implement something that already exists — `stable_qid` is
  already duplicated across three files (see Gotchas) and must not gain a fourth copy.
- **Validate against the filesystem, not the plan.** Any description of repo structure
  or the technical stack must be checked against the actual files and against
  `DECISIONS.md`. Planning-era artifacts (`docs/decomposition_research.md`,
  `CODEBASE_AUDIT.md`) describe superseded intentions, not the running system.

## Project overview

Two RAG pipelines — **single-shot** and **multi-step (Least-to-Most decomposition)** —
answer structured carbon-performance questions about three electrical utilities (DEWA,
CenterPoint, TNB) from TPI sustainability reports. Shared prep (`extract → chunk →
embed`) builds a local SQLite vector store; at query time each pipeline does hybrid
BM25+dense retrieval with RRF fusion and value-aware context selection, then generates
via NEBIUS-hosted Qwen3 MoE models. Runs persist to `db/benchmark.db` and are scored
through a manual evaluation workflow. The benchmark is a 2×2 design (pipeline × model
size) over 6 frozen ground-truth questions.

## Environment & setup

Assumes Windows (primary) or Linux/Nuvolos. Conda + pip. Run everything from repo root.

```bash
# Windows
conda env create -f environment_windows.yml
# Linux/Nuvolos
conda env create -f environment_nuvolos.yml

conda activate tpi-rag          # Python 3.11
cp .env.example .env            # then fill in keys
```

Required `.env` keys: `NEBIUS_API_KEY` (always), `GEMINI_API_KEY` (extraction only).
All other vars have defaults — see `.env.example`. Env is loaded centrally via
`utils.bootstrap_runtime_env()`; do not call `load_dotenv` ad hoc in new files.

System deps (poppler, tesseract, pillow) come from the conda env files. Do not assume
a global install.

## Commands

All from repo root.

**Data prep (shared, one-time):**
```bash
python pipelines/extract.py     # unstructured hi_res + Gemini Vision for tables
python pipelines/chunk.py       # sentence-window chunking, tables kept intact
python pipelines/embed.py       # Qwen3-Embedding-8B → data/vector_store.db
```

**Run a pipeline:**
```bash
python pipelines/single_shot_rag.py --question "Q1" --model "Qwen/Qwen3-30B-A3B-Instruct-2507"
python pipelines/multi_step_rag.py  --question "Q1" --model "Qwen/Qwen3-30B-A3B-Instruct-2507"

# All 6 questions via orchestrator
python pipelines/run_all.py --pipeline single_shot --model "Qwen/Qwen3-30B-A3B-Instruct-2507"
# flags: --questions, --extra-args, --dry-run
```

**Retrieval-only eval (no generation calls):**
```bash
python pipelines/evaluate_retrieval.py      # Recall@k, MRR vs ground_truth.md
```

**Export runs:**
```bash
python pipelines/dump_runs.py               # db/benchmark.db → logs/runs.jsonl
# flags: --pipeline, --model, --run-ids, --append (overwrites by default)
```

**Evaluation workflow (in order; step 2 is manual):**
```bash
python evaluation/make_worksheet.py             # --force to overwrite
# 2. human scores evaluation/scoring_worksheet.md
python evaluation/make_faithfulness_sheet.py    # --run-ids optional
python evaluation/load_evaluations.py           # --dry-run to preview; idempotent
# 5. open evaluation/analysis.ipynb (figures → docs/images/)
```

**Inspect & diagnose (read-only):**
```bash
python evaluation/inspect_intermediate_answers.py --run-ids <id> [<id> ...]   # render sub-step Q/A/chunks
python scripts/check_embeddings.py                                            # row counts, BLOB integrity
```

**Tests & lint (the "done" bar):**
```bash
pytest
ruff check .
ruff format --check .
```
CI runs all three (`.github/workflows/lint.yml`, `tests.yml`); a change is not done
until they pass.

## Repo layout

```
config.py                 # single source of truth: paths, constants, pricing
utils.py                  # shared helpers: env loading, year extraction, JSONL I/O
pyproject.toml            # ruff + pytest config

pipelines/
  extract.py  chunk.py  embed.py        # data prep
  retrieval.py                          # shared: hybrid BM25+dense RRF, value-aware selection
  single_shot_rag.py  multi_step_rag.py # the two pipelines
  run_all.py  dump_runs.py  evaluate_retrieval.py

db/
  database.py             # SOLE SQLite touchpoint for benchmark.db
  schema.sql              # 6 tables; benchmark.db created on first run (not committed)

evaluation/               # ground_truth.md (frozen) + scoring/faithfulness scripts + analysis.ipynb
scripts/                  # check_embeddings.py (diagnostic); migrate_qn_ids.py (DO NOT RUN)
tests/                    # chunker, utils, schema — deterministic logic only
docs/                     # report.md, case study, AI_USAGE.md; decomposition_research.md is stale
data/                     # raw PDFs, extracted/chunked JSONL, vector_store.db  [do not read PDFs]
logs/                     # runs.jsonl, token_spend.jsonl
```

## Conventions

- **Style:** ruff (line-length 100, double quotes, py311). Type hints throughout;
  imports at top of file, never inline. Use `logging`, not `print()`; `pathlib`, not
  string path concatenation.
- **Config:** all paths, constants, and pricing rates live in `config.py` — read and
  add them there; never hardcode a path or magic number in a pipeline or script.
- **Naming:** files `snake_case.py`; constants `UPPER_SNAKE_CASE` in `config.py`; chunk
  IDs `<COMPANY>_<year>_chunk_<zero_padded_index>`; document labels `<COMPANY>_<year>`.
- **Branches:** `feature/<desc>`, `docs/<desc>`, `chore/<desc>`.
- **Commits:** conventional — `<type>: <short desc>` then body. Types: `feat`, `fix`,
  `chore`, `docs`, `refactor`, `eval`.
- **Tests cover deterministic logic only** (chunker, utils, schema). LLM/retrieval
  quality is measured by benchmark runs, not unit tests — do not add brittle LLM-output
  assertions.

## Data & persistence

Two separate SQLite DBs, do not conflate them:

- **`data/vector_store.db`** — embedding store (`chunks` + `chunk_embeddings` BLOB,
  4096-dim float32 via the `sqlite-vec` extension). Written by `embed.py`, read-only at
  pipeline runtime via `retrieval.py`.
- **`db/benchmark.db`** — run store (`questions`, `runs`, `decompositions`, `steps`,
  `final_answers`, `evaluations`). **All SQLite access goes through `db/database.py`** —
  never `import sqlite3` in a pipeline or evaluation file. Schema uses
  `CREATE TABLE IF NOT EXISTS`, so re-running is safe.

Data path patterns: `data/extracted/<company>/<company>_<year>_elements.jsonl`,
`data/chunked/<company>/<company>_<year>_chunks.jsonl`. Companies: `DEWA`, `TNB`,
`Centerpoint`.

`logs/token_spend.jsonl` is the authoritative budget record (append-only, via
`check_budget(phase_label)`). `logs/runs.jsonl` is the dumped run export.

## Locked decisions — do not relitigate

Settled in `DECISIONS.md`. Do not re-introduce removed components or re-argue these:

| Decision | Locked value |
|---|---|
| Vector store | local **SQLite** (`sqlite-vec`), not ChromaDB or any hosted DB |
| Reranking | **removed from scope** — no cross-encoder; keeps the NEBIUS-only compute model |
| Decomposition | Least-to-Most, N+1 calls |
| Retrieval | hybrid BM25+dense RRF; `BM25_WEIGHT=2`, `RRF_K=60`, `top_n=40`, `context_chunks=10` |
| Context selection | three-pass value-aware |
| Chunking | sentence-window, 2-sentence overlap, tables kept intact |
| Table extraction | Gemini Vision (`gemini-2.5-flash`), 200 DPI, 30-page batches |
| Boilerplate filter | implemented but `ENABLE_BOILERPLATE_FILTER = False` |
| Embedding | `Qwen/Qwen3-Embedding-8B` via NEBIUS |
| Ground truth | 6 questions, frozen — adding any breaks comparability |
| Evaluation | manual human scoring (no LLM judge); correctness as k/n key claims |

## Gotchas / do-not

- **Never run `scripts/migrate_qn_ids.py`** — one-time repair already applied (commit
  `ba48de4`); re-running corrupts question IDs.
- **`stable_qid` is duplicated** across `single_shot_rag.py`, `multi_step_rag.py`, and
  `scripts/migrate_qn_ids.py` (known tech debt). Import an existing copy; do not add a
  fourth.
- **Stale, not authoritative:** `docs/decomposition_research.md` and `CODEBASE_AUDIT.md`
  describe earlier plans, not the current system.
- **Benchmark subset:** `db/benchmark.db` holds more runs than the scored set. The
  scored benchmark is exactly {single_shot, multi_step} × {30B, 235B} over the 6 GT
  questions; unevaluated runs carry null score fields after `dump_runs.py`.
- Other known limitations (assembly token counts on resume, table-count mismatch, etc.)
  are catalogued in `CONTRIBUTING.md` — check there before "fixing" surprising behaviour.
