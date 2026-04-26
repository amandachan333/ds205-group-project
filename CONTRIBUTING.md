# Contributing

Dev setup and pipeline internals for anyone working on the codebase.

## Repo structure (suggested structure for now, rmb to update)

```
repo/
├── data/
│   ├── raw/                        # source PDFs organised by company (gitignored)
│   ├── chunks/                     # chunked text output from ingestion (gitignored)
│   └── questions.json              # benchmark question set with ground truth
├── db/
│   ├── schema.sql                  # canonical CREATE TABLE definitions
│   └── benchmark.db                # generated at runtime (gitignored)
│   └── database.py                 # all SQLite read/write functions
├── docs/                           # research notes, decisions, images
├── evaluation/
│   ├── harness.py                  # runs both pipelines on all questions
│   ├── scorer.py                   # correctness and faithfulness scoring
│   └── report_generator.py         # produces comparison tables
├── pipelines/
│   ├── shared/                     # retriever, LLM client, prompts (shared by both pipelines)
│   ├── single_shot/
│   │   └── pipeline.py
│   └── multi_step/
│       ├── pipeline.py             # orchestrator
│       ├── decomposer.py           # Phase 1: decomposition call
│       ├── solver.py               # Phase 2: sub-question execution loop
│       └── assembler.py            # Phase 3: assembly call
├── reports/                        # benchmark report and token spend summary
├── tests/
├── run_benchmark.py                # entry point
├── .env.example
└── pyproject.toml
```

## Setting Up the Development Environment

### Prerequisites

- [conda](https://docs.conda.io/en/latest/) (Anaconda or Miniconda)
- A Nebius account with an API key ([nebius.com](https://nebius.com))

### Installation

```bash
# 1. Clone the repository
git clone <repo-url>
cd tpi-rag-decomposition

# 2. Create and activate the conda environment
conda env create -f environment.yml
conda activate rag

# 3. Set up environment variables
cp .env.example .env
# Then open .env and fill in your NEBIUS_API_KEY
```

### Getting a Nebius API Key

1. Sign in at [nebius.com](https://nebius.com) and open your project.
2. Navigate to **API Keys** and create a new key.
3. Copy the key into your `.env` file:
   ```
   NEBIUS_API_KEY=<your key here>
   ```

Your `.env` file is git-ignored — never commit it.

## How the Pipeline Works

> **TODO**: Describe the internal architecture of the data pipeline:
>
> - What are the main stages (ingestion, chunking, retrieval, generation, evaluation)?
> - Which modules or scripts are responsible for each stage?
> - How does data flow between the decomposition pipeline and the single-shot baseline?
> - How are intermediates persisted to SQLite?
> - Any key design decisions worth explaining.

### SQLite database schema

The pipeline persists all runs, intermediate steps, final answers, and evaluation scores to a SQLite database at `db/benchmark.db`. The canonical schema is in `db/schema.sql`. All read/write logic is in `db/database.py` — no other file imports `sqlite3` directly.

To recreate the database from scratch:

```bash
sqlite3 db/benchmark.db < db/schema.sql
```

#### Tables

| Table | Description |
|---|---|
| `questions` | The benchmark question set. Populated once at setup before any pipeline runs. Stores the question text, ground truth, and question type. |
| `runs` | One row per execution of one question through one pipeline. The central table everything else references. Tracks pipeline type, model, status, latency, and token spend. |
| `steps` | Multi-step pipeline only. One row per sub-question — inserted as `pending` after decomposition, updated to `complete` after each LLM call. Stores the retrieved chunks and intermediate answer for each sub-step. |
| `final_answers` | The assembled final output for each run. Kept separate from `runs` so the assembly call can be re-run without touching the step records. |
| `evaluations` | Manual scoring of each run's final answer. Stores correctness label, faithfulness score, and evaluator notes. Separate from pipeline data so results can be re-scored independently. |

#### Write sequence per run

1. INSERT into `questions` — once at setup, not per run. Uses `INSERT OR IGNORE` so re-running setup is safe.
2. INSERT into `runs` with `status = 'running'` → receive `run_id`.
3. _Multi-step only._ INSERT N rows into `steps` with `status = 'pending'` — one per sub-question, all inserted before any retrieval or generation begins.
4. _Multi-step only._ For each step: UPDATE `steps` to `status = 'complete'` with answer and token counts — must happen before moving to the next step.
5. INSERT into `final_answers` with the assembled output.
6. UPDATE `runs` to `status = 'complete'` with latency, total tokens, and cost. On failure, set `status = 'failed'`.
7. INSERT into `evaluations` — after manual scoring, independent of pipeline execution.

If a multi-step run crashes mid-way, querying `steps WHERE status = 'pending'` identifies exactly where to resume without re-running completed steps or spending NEBIUS tokens on work already done.

#### Key constraints for pipeline code

- `pipeline_type` on `runs` must be exactly `"single_shot"` or `"multi_step"` — use the constants in `pipelines/shared/constants.py`, never hardcode the strings.
- Foreign key enforcement is enabled on every connection (`PRAGMA foreign_keys = ON`), so `question_id` must exist in `questions` before any run is created for it.
- See `db/schema.sql` for full column definitions, types, and constraints.

## Known Bugs / Areas for Improvement

> **TODO**: List any known bugs or limitations that contributors may want to tackle. For example:
>
> - [ ] Bug: ...
> - [ ] Improvement: ...
> - [ ] Tech debt: ...

## Running Tests

> **TODO**: Describe how to run the test suite.

```bash
pytest
```

## Code Style and Guidelines

> **TODO**: Document any code style conventions, linting tools, or contribution guidelines (e.g. formatting with `black`, type annotations, commit message conventions).
