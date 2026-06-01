# TPI Carbon Performance RAG Pipeline — Electrical Utilities

This repository implements two Retrieval-Augmented Generation pipelines for answering
structured questions about the carbon performance of electrical utility companies — DEWA,
CenterPoint Energy, and Tenaga Nasional (TNB) — using sustainability reports sourced from the
Transition Pathway Initiative (TPI) Centre. The pipelines are designed to support TPI Carbon
Performance assessments, which require structured extraction of emissions intensity figures,
reduction targets, and trajectory data across multiple companies and reporting years. The first
pipeline answers each question in a single retrieval-and-generation call using Qwen3-30B or
Qwen3-235B via NEBIUS; the second decomposes each question into ordered sub-questions, answers
them sequentially using Least-to-Most prompting, and assembles a final response — trading
compute cost for measurably higher correctness on complex multi-company questions.

---

## What These Pipelines Do

### Single-shot pipeline

```
PDF documents
     │
     ▼
[1] Extract      unstructured hi_res (yolox) for text elements; Gemini Vision
     │           (gemini-2.5-flash, 200 DPI, 30-page batches) for table pages
     ▼
[2] Chunk        sentence-window chunking with table-aware splitting
     │           (pipelines/chunk.py)
     ▼
[3] Embed        NEBIUS Qwen3-Embedding-8B, float32 BLOBs stored in
     │           data/vector_store.db (pipelines/embed.py)
     ▼
[4] Retrieve     BM25 + dense hybrid, RRF fusion (k=60, BM25 weight=2),
     │           three-pass value-aware context selection
     │           (pipelines/retrieval.py)
     ▼
[5] Generate     one NEBIUS call, Qwen3-30B or Qwen3-235B, max 1024 tokens
     │           (pipelines/single_shot_rag.py)
     ▼
[6] Persist      final answer → db/benchmark.db (runs + steps tables)
                 token cost  → logs/token_spend.jsonl
```

The single-shot pipeline retrieves once and generates once per question. Its specific failure
mode on complex questions is under-retrieval: when a question spans multiple companies, a
single retrieval pass may not surface all required chunks, causing the model to abstain or
answer for only one company.

### Multi-step pipeline

```
PDF documents → [Extract → Chunk → Embed] (identical to single-shot)
     │
     ▼
[1] Decompose    LtM prompt → numbered sub-question list (NEBIUS, max 1024 tokens)
     │           SQLite WRITE: one decompositions row + N pending steps rows
     │           (pipelines/multi_step_rag.py)
     ▼
[2] Sub-question loop  (one iteration per pending step, in order)
     │   SQLite READ:  prior completed steps → established-facts context
     │   Retrieve:     hybrid BM25+dense per sub-question
     │   Generate:     NEBIUS call, max 1024 tokens
     │   SQLite WRITE: complete step row before advancing
     ▼
[3] Assemble     SQLite READ: all completed step answers
     │           one NEBIUS call, max 2048 tokens (pipelines/multi_step_rag.py)
     │           SQLite WRITE: final_answers row
     ▼
[4] Persist      complete run → db/benchmark.db (runs table)
                 token cost   → logs/token_spend.jsonl
```

The multi-step pipeline decomposes, retrieves, and generates independently for each
sub-question, with each result persisted to SQLite before the next step begins — making the
run fully resumable if interrupted. Its specific failure mode is different from single-shot:
at full three-company retrieval coverage, the model can still produce incorrect reasoning in
the assembly phase, meaning the failure occurs in synthesis rather than in retrieval.

### The core empirical question

Does decomposing a complex question into ordered sub-questions produce more correct answers
than answering it in one pass, and at what cost? Both pipelines use the same question set,
ground-truth answers, vector store, retrieval configuration, and prompt templates. The only
variables are pipeline type (single-shot vs multi-step) and model size (Qwen3-30B vs
Qwen3-235B). This 2×2 design lets us ask whether decomposition helps, whether model size
helps, and whether a smaller model with decomposition can outperform a larger model without it.

---

## Documents and Questions

### Source documents

| Company | Document type | Years in corpus |
|---|---|---|
| DEWA (Dubai Electricity and Water Authority) | Sustainability Report | 2016, 2019, 2020, 2021, 2022, 2023, 2024 |
| Tenaga Nasional (TNB) | Sustainability Report | 2019, 2020, 2021, 2022, 2023, 2024 |
| Tenaga Nasional (TNB) | Integrated Annual Report | 2021, 2022, 2024 |
| CenterPoint Energy | Corporate Sustainability Report | 2020, 2022, 2023, 2024 |

Source PDFs are gitignored. See [Preparing the Data](#preparing-the-data) for how to obtain
and place them.

### Benchmark questions

| ID | Question | Type |
|---|---|---|
| Q1 | Comparing Tenaga Nasional and DEWA's emissions intensity (both reported in tCO2e/MWh) from 2019 to their most recent reported year, which company achieved the larger reduction in absolute terms and in percentage terms, and do the two measures agree on the ranking? | Comparative |
| Q2 | Given each company's most recent reported emissions intensity and their respective net-zero target year, which company faces the steepest required annual percentage reduction in emissions intensity to reach net-zero on schedule? | Comparative |
| Q3 | Has Tenaga set intermediate emissions reduction targets between now and its long-term target year? What are they? | Trajectory |
| Q4 | Has DEWA's carbon emission intensity for electricity (reported in tCO₂e/MWh) improved consistently since 2010, and what was the net reduction from 2010 to 2024? | Trajectory |
| Q5 | What has changed in each company's stated emissions targets between its 2020 and 2023/2024 assessments? | Change-over-time |
| Q6 | For each of the three companies, compare the actual annual reduction in emissions intensity achieved from their base year to their most recently reported year against the annual reduction required to reach net-zero by their stated target year. Based on this comparison, is each company's net-zero commitment credible on current trajectory? | Change-over-time |

Full ground-truth answers with source page numbers and chunk IDs are in
`evaluation/ground_truth.md`.

---

## Repository Structure

```
repo/
├── .github/
│   └── workflows/
│       ├── lint.yml                   # CI: ruff check + ruff format --check on push and PR;
│       │                              #   fails the build if any file would be reformatted or
│       │                              #   any lint rule fires
│       └── tests.yml                  # CI: pytest on push and PR; runs the suite under
│                                      #   tests/ against the conda environment
│
├── config.py                          # all constants: paths, chunking params, embedding model,
│                                      #   NEBIUS pricing rates, base URL — single source of truth
├── utils.py                           # shared helpers: .env loading, directory creation, year
│                                      #   extraction from filenames, atomic JSONL read/write
│
├── pipelines/
│   ├── extract.py                     # PDF extraction — unstructured hi_res for text elements,
│   │                                  #   Gemini Vision for table pages; writes JSONL to data/extracted/
│   ├── chunk.py                       # sentence-window chunking with table-aware splitting;
│   │                                  #   writes JSONL to data/chunked/
│   ├── embed.py                       # NEBIUS Qwen3-Embedding-8B in batches; stores chunk text +
│   │                                  #   float32 BLOB embeddings in data/vector_store.db;
│   │                                  #   logs token spend to logs/token_spend.jsonl
│   ├── retrieval.py                   # shared by both pipelines: hybrid BM25+dense retrieval,
│   │                                  #   RRF fusion, three-pass context selection, cost accounting
│   ├── single_shot_rag.py             # single-shot pipeline: one retrieval + one generation call;
│   │                                  #   persists run and step to db/benchmark.db
│   ├── multi_step_rag.py              # LtM multi-step pipeline: decompose → sub-question loop →
│   │                                  #   assembly; full resume support via SQLite step status
│   ├── evaluate_retrieval.py          # retrieval-only eval against evaluation/ground_truth.md;
│   │                                  #   reports Recall@k and MRR; no generation calls
│   ├── run_all.py                     # orchestrator: runs either pipeline over all questions
│   │                                  #   in ground_truth.md via subprocess; collects failures
│   └── dump_runs.py                   # reads db/benchmark.db and exports completed runs to
│                                      #   logs/runs.jsonl in a symmetric schema for both pipelines
│
├── tests/
│   ├── conftest.py                    # shared pytest fixtures: in-memory SQLite with schema.sql
│   │                                  #   applied, sample chunk records, temp working directory
│   ├── test_chunker.py                # tests for chunk.py: table-aware splitting, pipe-delimited
│   │                                  #   row preservation, tCO,e normalisation, boilerplate detection
│   └── test_utils.py                  # tests for utils.py: year derivation from filenames,
│                                      #   atomic JSONL read/write round-trip
│
├── db/
│   ├── schema.sql                     # canonical CREATE TABLE definitions; executed once at startup
│   ├── database.py                    # every SQLite read/write function for db/benchmark.db;
│   │                                  #   the only file that imports sqlite3 for the benchmark store
│   └── benchmark.db                   # benchmark results: runs, steps, decompositions,
│                                      #   final_answers, evaluations; gitignored; created automatically
│                                      #   by database.init_db() on first pipeline call; separate
│                                      #   database from data/vector_store.db
│
├── evaluation/                        # scoring aids — not pipeline stages
│   ├── ground_truth.md                # six benchmark questions with expected answers, source
│   │                                  #   documents, page numbers, section headings, chunk_id refs
│   ├── make_worksheet.py              # generates evaluation/scoring_worksheet.md for human scoring;
│   │                                  #   --force required to overwrite a scored worksheet
│   ├── make_faithfulness_sheet.py     # generates evaluation/faithfulness_all.md: answers beside
│   │                                  #   full retrieved chunk text for claim-by-claim tracing
│   ├── load_evaluations.py            # inserts human scores into evaluations table in benchmark.db;
│   │                                  #   idempotent; --dry-run flag available
│   ├── inspect_intermediate_answers.py # read-only: renders sub-step questions, answers, retrieved
│   │                                  #   chunks for specified run_ids as markdown
│   ├── analysis.ipynb                 # produces comparison tables, cost/latency figures, verdict
│   │                                  #   aggregates, inspectability examples; figures → docs/images/
│   ├── scoring_worksheet.md           # human-scored output from make_worksheet.py (committed)
│   └── faithfulness_all.md            # faithfulness sheet output (committed)
│
├── docs/
│   ├── decomposition_research.md      # decision artifact: research notes comparing CoT, LtM,
│   │                                  #   Self-Ask, ReAct; not documentation of the final system
│   └── images/                        # figures produced by analysis.ipynb
│
├── scripts/                           # diagnostic and one-time migration tools — not pipeline stages
│   ├── check_embeddings.py            # diagnostic: counts rows, checks BLOB lengths, deserialises
│   │                                  #   one embedding to verify L2 norm
│   └── migrate_qn_ids.py              # ONE-TIME data repair: collapsed duplicate question rows to
│                                      #   stable SHA-1 hash IDs; bypassed FK constraints during run;
│                                      #   already applied (commit ba48de4); MUST NOT BE RUN AGAIN
│
├── data/
│   ├── raw/                           # source PDFs organised by company; gitignored
│   ├── extracted/                     # per-document JSONL from extract.py
│   ├── chunked/                       # per-document JSONL from chunk.py
│   └── vector_store.db                # vector store: chunk text + float32 BLOB embeddings;
│                                      #   created by embed.py; separate database from benchmark.db;
│                                      #   gitignored
│
├── logs/
│   ├── token_spend.jsonl              # append-only JSONL: one record per embedding batch, per
│   │                                  #   generation call, per retrieval eval query; used for
│   │                                  #   budget accounting and cost analysis; not a database table
│   └── runs.jsonl                     # refreshed by dump_runs.py: symmetric export of both
│                                      #   pipelines' results with evaluation scores joined
│
├── DECISIONS.md                       # architectural decision log
├── CONTRIBUTING.md                    # developer guide
├── pyproject.toml                     # tool config: ruff (lint + format) and pytest;
│                                      #   single config file for all tooling
├── environment_windows.yml            # conda environment for local development (Windows)
├── environment_nuvolos.yml            # conda environment for Nuvolos (Linux; uses libmagic
│                                      #   instead of python-magic-bin)
└── .env.example                       # template for required API keys
```

**Two separate databases:** `data/vector_store.db` stores chunk text and float32 embedding
BLOBs; it is created by `embed.py` and read by both pipelines during retrieval. `db/benchmark.db`
stores pipeline runs, sub-question steps, decompositions, final answers, and evaluation scores;
it is created by `database.init_db()` on the first pipeline call. They serve different
purposes and must not be confused.

**`logs/token_spend.jsonl`** is an append-only flat file, not a database table. It is the
authoritative source for total embedding cost; `runs.total_cost_usd` in `benchmark.db` stores
per-run generation cost only. Cross-checking total cost requires reading the JSONL directly.

**`evaluation/`** contains scoring aids that a human operator runs after pipeline execution
to produce and load evaluation scores. They are not pipeline stages and are not invoked by
`run_all.py`.

**`docs/decomposition_research.md`** is a decision artifact documenting the research that led
to the LtM choice. It is not documentation of the implemented system.

---

## Setting Up

### Prerequisites

- Conda (Miniconda or Anaconda)
- A NEBIUS account with an API key — required for embedding and all generation calls
  ([nebius.com](https://nebius.com))
- A Google Cloud account with Gemini API access — required for PDF table extraction only;
  not needed if `data/extracted/` already exists or is provided by a teammate
- Git

### Installation

1. **Clone the repository.**

   ```bash
   git clone <repo-url>
   cd group-project-it_works_on_my_machine
   ```

2. **Create and activate the conda environment.** This installs all Python dependencies
   including `unstructured`, `rank_bm25`, `google-genai`, and the OpenAI-compatible client
   used to call NEBIUS.

   ```bash
   conda env create -f environment.yml
   conda activate tpi-rag
   ```

   On Nuvolos (Linux), use `environment_nuvolos.yml` instead — it substitutes `libmagic`
   for `python-magic-bin`, which is not available on the Nuvolos OS image.

   ```bash
   conda env create -f environment_nuvolos.yml
   conda activate tpi-rag
   ```

3. **Copy `.env.example` to `.env` and fill in your API keys.** The pipeline reads all
   credentials from `.env` at startup; no script accepts keys as command-line arguments.

   ```bash
   cp .env.example .env
   # Open .env in a text editor and fill in at minimum NEBIUS_API_KEY and GEMINI_API_KEY
   ```

### Setting up your .env file

| Variable | Required for | Example / default |
|---|---|---|
| `NEBIUS_API_KEY` | Embedding and all generation calls | `your-nebius-key` |
| `NEBIUS_BASE_URL` | NEBIUS API endpoint | `https://api.studio.nebius.com/v1/` |
| `GEMINI_API_KEY` | PDF table extraction (`pipelines/extract.py`) only | `your-gemini-key` |
| `RAG_GENERATION_MODEL` | Override the default generation model | `Qwen/Qwen3-30B-A3B-Instruct-2507` |
| `TOKEN_BUDGET_USD` | Budget cap; pipeline warns at 90% of this value | `100.0` |

`.env` is gitignored and must never be committed. It contains credentials that would grant
full NEBIUS and Gemini API access to anyone who obtains the file.

---

## Preparing the Data

### Source PDFs

Source PDFs are gitignored and must be placed manually in `data/raw/<company>/` before running
extraction. The expected directory layout is:

```
data/raw/
├── DEWA/
│   └── *.pdf
├── TNB/
│   └── *.pdf
└── Centerpoint/
    └── *.pdf
```

If the PDFs are available on the Nuvolos shared mount, copy them directly:

```bash
cp -r "/space_mounts/ds205/TPI-CP/Sources - Electrical Utilities/DEWA/"* data/raw/DEWA/
cp -r "/space_mounts/ds205/TPI-CP/Sources - Electrical Utilities/TNB/"* data/raw/TNB/
cp -r "/space_mounts/ds205/TPI-CP/Sources - Electrical Utilities/CenterPoint/"* data/raw/Centerpoint/
```

### Running extraction and embedding

Run these three steps in order. Each step reads from the previous step's output directory.

**Step 1 — Extract text and tables from PDFs.**

```bash
python pipelines/extract.py
```

Output: per-document JSONL files in `data/extracted/`, one per PDF.

**Why this two-system approach?** `unstructured` hi_res with the yolox layout model accurately
detects text elements, section headings, and table boundaries, but it extracts Table elements
as flattened text strings — losing column alignment entirely. Dense multi-column tables
(intensity trajectories, emissions inventories) are the primary data of interest in this
corpus, and a misread column value corrupts the generated answer. Gemini Vision
(`gemini-2.5-flash`, 200 DPI) provides structured markdown output for table pages, preserving
column alignment in a format generation models can read directly.

Known limitation: the 30-page Gemini batch limit is deliberate — larger batches caused
consistent extraction failures on long documents with dense tables. The `--company` flag
restricts extraction to one company at a time if needed.

**Step 2 — Chunk extracted text into retrievable segments.**

```bash
python pipelines/chunk.py
```

Output: per-document JSONL files in `data/chunked/`, one per PDF.

The chunker uses a sentence-window strategy: chunks are built sentence-by-sentence up to a
maximum character limit, with a sentence-overlap buffer to avoid cutting facts across chunk
boundaries. Table chunks — identified by the presence of pipe characters from Gemini's
markdown output — are kept intact rather than split mid-row.

**Step 3 — Embed chunks and write to the vector store.**

```bash
python pipelines/embed.py
```

Output: `data/vector_store.db`, a SQLite database containing chunk text and float32 BLOB
embeddings produced by NEBIUS Qwen3-Embedding-8B.

`NEBIUS_API_KEY` is required. Chunks are sent to the NEBIUS embedding API in batches of 16; a
300ms pause between batches is applied as a conservative rate-limit guard. Token spend is
logged to `logs/token_spend.jsonl` as each batch completes, so you can monitor cost in real
time. Embedding the full corpus cost $0.0393 (3,926,175 tokens).

If `data/vector_store.db` already exists — from a previous embed run or shared by a teammate
— this step can be skipped. Both pipelines read the vector store at runtime but never write
to it.

---

## Running the Benchmark

### Quickstart

The following commands take you from a clean clone (assuming `data/vector_store.db` already
exists) to full benchmark output. Each pipeline command is idempotent — safe to re-run if
interrupted.

```bash
# Run all six questions through the single-shot pipeline (Qwen3-30B)
python pipelines/run_all.py --pipeline single_shot --model Qwen/Qwen3-30B-A3B-Instruct-2507

# Run all six questions through the multi-step pipeline (Qwen3-30B)
python pipelines/run_all.py --pipeline multi_step --model Qwen/Qwen3-30B-A3B-Instruct-2507

# Repeat with the larger model to complete the 2×2 experiment
python pipelines/run_all.py --pipeline single_shot --model Qwen/Qwen3-235B-A22B-Instruct-2507
python pipelines/run_all.py --pipeline multi_step --model Qwen/Qwen3-235B-A22B-Instruct-2507

# Export completed runs with evaluation scores to logs/runs.jsonl
python pipelines/dump_runs.py
```

Results are written to `db/benchmark.db` as each question completes. Token spend is appended
to `logs/token_spend.jsonl` after each generation call.

### Single-shot pipeline

To run a single question:

```bash
python pipelines/single_shot_rag.py \
  --question "Has DEWA's carbon emission intensity for electricity improved consistently since 2010?" \
  --model Qwen/Qwen3-30B-A3B-Instruct-2507
```

`--question` accepts the full question text. `--model` accepts any NEBIUS model string;
it defaults to `RAG_GENERATION_MODEL` from `.env` if not specified. Results are written to
the `runs` and `steps` tables in `db/benchmark.db`, and token spend to
`logs/token_spend.jsonl`.

To run all six questions at once:

```bash
python pipelines/run_all.py --pipeline single_shot --model Qwen/Qwen3-30B-A3B-Instruct-2507
```

### Multi-step pipeline

To run a single question:

```bash
python pipelines/multi_step_rag.py \
  --question "Has DEWA's carbon emission intensity for electricity improved consistently since 2010?" \
  --model Qwen/Qwen3-30B-A3B-Instruct-2507
```

`--resume`: if the run is interrupted mid-way through the sub-question loop, re-running the
same command with `--resume` continues from the last completed step. The pipeline queries
`steps WHERE status = 'pending'` to find where it left off, without re-spending NEBIUS tokens
on steps that already completed.

`--review`: pauses after the decomposition phase and prints the generated sub-questions to the
terminal. You can edit them interactively before answering begins — useful when you suspect the
decomposition missed a key sub-question.

To run all six questions at once:

```bash
python pipelines/run_all.py --pipeline multi_step --model Qwen/Qwen3-30B-A3B-Instruct-2507
```

### Why?

**Why decompose into sub-questions rather than answering in one prompt?**

The benchmark questions require comparing data from up to three companies across multiple
reporting years. A single retrieval pass ranks chunks by overall similarity to the full
question — it surfaces chunks about the most salient company or concept, leaving other
required companies under-represented in the context window. The single-shot pipeline then
abstains or answers for only one company. The multi-step pipeline avoids this by issuing one
targeted retrieval per sub-question (e.g. "what was DEWA's emissions intensity in 2019?"),
so each company-year pair gets its own retrieval pass. We found this produces 6 correct
verdicts versus 4 for single-shot, with abstentions dropping from 5 to 2.

**Why SQLite for intermediate results rather than passing answers in memory?**

Writing each sub-question answer to `db/benchmark.db` before moving to the next step makes
the pipeline resumable, inspectable, and auditable. If a run crashes mid-way — network
timeout, rate limit, manual interrupt — `--resume` restarts from the last completed step
without re-spending NEBIUS tokens on completed work. After a run finishes, the retrieved
chunks and intermediate answers for every step remain queryable in the database, enabling the
claim-by-claim faithfulness analysis in `evaluation/make_faithfulness_sheet.py`.

**Why these two models (Qwen3-30B and Qwen3-235B)?**

Both use a Mixture-of-Experts architecture, so their active parameter counts (3B and 22B
respectively) are substantially lower than the total parameter counts suggest — making them
more cost-efficient per token than their names imply. We chose two sizes to answer three
empirical questions: does decomposition help at the same model size? Does a larger model help
at the same pipeline type? And does multi-step on the smaller model outperform single-shot on
the larger model? If so, decomposition is a cost-effective alternative to scaling model size.
We run the 30B conditions first because they are cheaper, surfacing pipeline bugs before
spending tokens on the 235B model.

---

## Evaluating Results

The evaluation scripts are scoring aids operated by a human evaluator after pipeline runs
complete. They are not pipeline stages and are not invoked by `run_all.py`.

**Stage E1 — Generate the scoring worksheet.**

```bash
python evaluation/make_worksheet.py
```

Writes `evaluation/scoring_worksheet.md` — one block per run containing the question, ground
truth, model answer, and retrieved chunk IDs. The `--force` flag is required to overwrite an
existing worksheet that contains manual scores. This protection is deliberate: completed manual
scores cannot be regenerated from the database, so accidental overwrites must require an
explicit flag.

**Stage E2 — MANUAL GATE: human scoring.**

A human scorer reads each run in the worksheet and fills in correctness (as a key-claim
fraction, e.g. 3/5 for three correct claims out of five), faithfulness, and evaluator notes.
Before or after scoring, the faithfulness sheet can be generated for detailed claim tracing:

```bash
python evaluation/make_faithfulness_sheet.py              # all runs
python evaluation/make_faithfulness_sheet.py 40 53 57     # specific run IDs
```

Writes `evaluation/faithfulness_all.md` — each answer displayed beside the full text of every
retrieved chunk, grouped by sub-question step, for claim-by-claim comparison.

**Stage E3 — Load scores into the database.**

```bash
python evaluation/load_evaluations.py --dry-run    # validate before inserting
python evaluation/load_evaluations.py
```

Inserts one row per run into the `evaluations` table in `db/benchmark.db`. Idempotent — safe
to re-run.

**Stage E4 — Refresh the runs export.**

```bash
python pipelines/dump_runs.py
```

Refreshes `logs/runs.jsonl` with correctness, faithfulness_score, and evaluator_notes joined
from the `evaluations` table. Must be re-run after every `load_evaluations` call to keep
`runs.jsonl` current.

**Stage E5 — Run the analysis notebook.**

```
evaluation/analysis.ipynb  —  run all cells
```

Produces comparison tables, cost and latency figures, verdict aggregates, and the two
inspectability examples (Runs 57 and 53). Figures are saved to `docs/images/`.

---

## Benchmark Results

### Token spend

| Item | Cost | Tokens |
|---|---|---|
| Embedding — Qwen3-Embedding-8B | $0.0817 | 3,926,175 tokens |
| Generation — Qwen3-30B | $0.1771 | 433 calls; 1.54M prompt + 76K completion |
| Generation — Qwen3-235B | $0.0924 | 126 calls; 395K prompt + 22K completion |
| Retrieval eval queries | $0.0001 | — |
| **Grand total** | **$0.3512** | **0.4% of $100 NEBIUS budget** |

### Generation cost by pipeline phase

| Phase | Cost |
|---|---|
| sub_question loop | $0.2269 (dominant cost centre) |
| assembly | $0.0155 |
| single_shot | $0.0107 |
| decomposition | $0.0099 |

### Key findings

1. **multi_step: 6 correct verdicts; single_shot: 4 correct.** Abstentions: 2 (multi_step)
   vs 5 (single_shot).
2. multi_step weakly dominates per question — better on Q1, Q4, Q5; tied elsewhere; never
   worse.
3. **Faithfulness:** all four configurations score 93–96% pooled; single_shot marginally
   higher.
4. **Cost and latency:** multi_step is approximately 12–13× slower, uses approximately 19×
   the tokens, and costs approximately 14× more per question.
5. **Different failure components:** single_shot abstains when it under-retrieves (typically
   returning chunks for only one company when three are required). multi_step failures occur
   at full three-company retrieval coverage — the failure is in assembly-phase reasoning, not
   retrieval.
6. **Corpus corruption flags:** 5 runs flag corpus-level issues. Q5 collapses to 0.12 mean
   correctness across conditions.

### Inspectability examples

#### Run 57 — model ignores a contradicting figure in its own context

The model claimed DEWA improved "consistently." The retrieved chunk contained the figure
sequence 0.4818 → 0.4744 → 0.4834, showing a 2021 reversal. The contradicting figure was
present in the prompt context; the model ignored it.

#### Run 53 — faithful to text, wrong reasoning

The model called TNB "credible" and cited a 5% annual reduction. The retrieved chunk states
this is a target "from 2024", not an achieved reduction. The answer is faithful to the
retrieved text but the reasoning is wrong — the model treated a forward-looking target as an
accomplished fact.

### Recommendation

multi_step is recommended for trajectory and cross-company comparison questions where the cost
of a wrong answer exceeds the 14× compute premium. single_shot is appropriate for simpler
factual retrieval where retrieval coverage of a single company or year is sufficient.

---

## Environment Variables

| Variable | Description | Default | Required |
|---|---|---|---|
| `NEBIUS_API_KEY` | NEBIUS API authentication | — | Yes |
| `NEBIUS_BASE_URL` | NEBIUS API endpoint | `https://api.studio.nebius.com/v1/` | No |
| `GEMINI_API_KEY` | Gemini API authentication for table extraction | — | Only for `extract.py` |
| `RAG_GENERATION_MODEL` | Default generation model for both pipelines | `Qwen/Qwen3-30B-A3B-Instruct-2507` | No |
| `TOKEN_BUDGET_USD` | Total budget cap; `check_budget()` warns at 90% | `100.0` | No |
| `GENERATION_COST_PER_1M_TOKENS` | Fallback cost rate for model strings not in the pricing dict | `0.02` | No |
| `PDF_DIR` | Root directory for source PDFs | `data/raw` | No |
| `PDF_GLOB` | Glob pattern for PDF discovery within `PDF_DIR` | `**/*.pdf` | No |
| `PDF_PARTITION_STRATEGY` | `unstructured` partition strategy | `hi_res` | No |
| `PDF_HI_RES_MODEL` | Layout model used by hi_res | `yolox` | No |
| `PDF_RASTERISE_DPI` | DPI for PDF rasterisation before Gemini | `200` | No |
| `GEMINI_BATCH_PAGE_LIMIT` | Max pages per Gemini API call | `30` | No |

All variables are read from `.env` at startup via `python-dotenv`. Variables with a non-blank
default are optional — the pipeline uses the default if the variable is absent from `.env`.

---

## Reproducibility Notes

**What is gitignored and why:**
- `data/` — source PDFs cannot be redistributed; extracted and chunked JSONL is fully
  reproducible from the PDFs; `data/vector_store.db` is large binary data
- `db/benchmark.db` — runtime state; recreated automatically on the first pipeline call
- `logs/` — append-only operational output; not source-controlled
- `.env` — contains API credentials

**What is committed and tracked:**
- `evaluation/ground_truth.md` — the frozen benchmark question set and expected answers
- `evaluation/scoring_worksheet.md` — completed human-scored worksheet
- `evaluation/faithfulness_all.md` — completed faithfulness sheet
- `DECISIONS_EXTRACTED.md` — factual record of architectural decisions and post-hoc rationale
- `db/schema.sql` — canonical database schema

**Sharing the vector store:** `data/vector_store.db` is gitignored. A teammate can share it
directly (e.g. via the Nuvolos shared mount or `scp`). If no shared copy is available,
recreate it by running `embed.py` — this cost $0.0393 in our benchmark run and takes as long
as the embedding API allows for the full corpus.

**Recreating the benchmark database from scratch:**

```bash
sqlite3 db/benchmark.db < db/schema.sql
```

`db/benchmark.db` is also created automatically by `database.init_db()` on the first call to
either pipeline, so this manual step is only needed if you want to reset the database to an
empty state without running the pipeline.

**`scripts/migrate_qn_ids.py`** has already been run (commit `ba48de4`). It collapsed
duplicate question rows to stable SHA-1 hash IDs and bypassed foreign key constraints during
the migration. Running it again would corrupt the database. Do not run it.
