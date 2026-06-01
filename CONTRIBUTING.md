# Contributing

Developer guide for anyone working on the codebase internals, extending a pipeline stage,
or adding benchmark questions. Assumes you have already read README.md and have the
environment set up and the pipeline running.

---

## Architecture Overview

### Data preparation (shared by both pipelines)

```
pipelines/extract.py    →  data/extracted/         (per-document JSONL, one file per PDF)
pipelines/chunk.py      →  data/chunked/            (per-document JSONL, one file per PDF)
pipelines/embed.py      →  data/vector_store.db     (SQLite: chunks table + chunk_embeddings table)
```

### Single-shot pipeline

```
data/vector_store.db
     │
     ▼  pipelines/retrieval.py: load_index() → hybrid_retrieve() → select_context_chunks()
     │
     ▼  pipelines/single_shot_rag.py
     │   ├── database.insert_question()     →  db/benchmark.db: questions
     │   ├── database.create_run()          →  db/benchmark.db: runs (status='running')
     │   ├── database.insert_step()         →  db/benchmark.db: steps (step_index=1, status='pending')
     │   ├── _run_generation() → NEBIUS
     │   ├── database.complete_step()       →  db/benchmark.db: steps (status='complete')
     │   ├── database.insert_final_answer() →  db/benchmark.db: final_answers
     │   └── database.complete_run()        →  db/benchmark.db: runs (status='complete')
     │
     └──  logs/token_spend.jsonl            (one generation record appended per call)
```

### Multi-step pipeline

```
data/vector_store.db
     │
     ▼  pipelines/retrieval.py: load_index() → get_corpus_inventory()
     │
     ▼  pipelines/multi_step_rag.py
     │
     ├── [Phase 1: Decompose]
     │   ├── database.create_decomposition()  →  db/benchmark.db: decompositions (status='pending')
     │   ├── _chat() → NEBIUS (LtM prompt)
     │   ├── database.complete_decomposition() → db/benchmark.db: decompositions (status='complete')
     │   └── database.insert_step() × N       →  db/benchmark.db: N steps (status='pending')
     │
     ├── [Phase 2: Sub-question loop — repeats until no pending steps]
     │   ├── SQLite READ:  get_pending_steps()   → next step to process
     │   ├── SQLite READ:  get_completed_steps()  → established-facts context
     │   ├── hybrid_retrieve() + select_context_chunks()
     │   ├── _chat() → NEBIUS (sub-question prompt)
     │   └── SQLite WRITE: complete_step()        → steps (status='complete')
     │
     ├── [Phase 3: Assemble]
     │   ├── SQLite READ:  get_completed_steps()  → all step answers
     │   ├── _chat() → NEBIUS (assembly prompt, max 2048 tokens)
     │   └── SQLite WRITE: insert_final_answer()  → db/benchmark.db: final_answers
     │
     └── database.complete_run()               →  db/benchmark.db: runs (status='complete')
         logs/token_spend.jsonl                    (one record per generation call, per phase)
```

Both pipelines share `pipelines/retrieval.py` entirely — the same `load_index()`,
`hybrid_retrieve()`, `select_context_chunks()`, and `log_generation_spend()` functions are
called identically in both. They also share the same `data/vector_store.db` vector index and
the same `evaluation/ground_truth.md` question set. They diverge at the generation stage:
the single-shot pipeline issues one retrieval pass and one generation call per question and
writes a single step row; the multi-step pipeline issues a decomposition call, then a separate
retrieval pass and generation call for each sub-question, then an assembly call — persisting
every intermediate answer to SQLite before advancing.

---

## Module Responsibilities

### `config.py`

Central constants for the entire codebase. Every directory path, chunking parameter,
embedding model name and dimension, NEBIUS pricing rate, and base URL is defined here.
No other module defines its own path constants or pricing rates — they import from
`config.py` or read from environment variables declared here.

**Key decision — single source of truth over scattered constants in each module:**
When a constant appears in multiple modules (chunk size, model name, cost rate), a change
in one place silently leaves stale values elsewhere. `config.py` is the single file a
developer touches to adjust any tunable parameter. The `GENERATION_COST_RATES` dict maps
model strings to their per-million-token rates; all generation cost accounting in
`retrieval.py:log_generation_spend()` reads from this dict.

**Known limitation — GENERATION_COST_RATES model string drift** (`config.py:49–53`): The
model strings in this dict include a date suffix (`-2507`). If NEBIUS updates a model and
changes the string, cost accounting silently falls back to the generic
`GENERATION_COST_PER_1M_TOKENS = 0.02` rate without any warning. There is no check that the
model string being used matches a key in the dict. Proposed fix: add a warning log in
`retrieval.py:log_generation_spend()` when the model string does not match any key in
`GENERATION_COST_RATES`, so rate drift is surfaced at generation time.

**Key constants**

*Path constants* (all hardcoded as `Path` objects): `RAW_DIR`, `EXTRACTED_DIR`, `CHUNKED_DIR`, `VECTOR_STORE_PATH`, `BENCHMARK_DB_PATH`, `LOG_DIR`, `TOKEN_SPEND_LOG`

*Chunking parameters* (all hardcoded ints): `MAX_CHUNK_SIZE=1000`, `SENTENCE_OVERLAP=2`, `MIN_CHUNK_LENGTH=100`, `MAX_CHUNK_LENGTH=2000`

*Embedding configuration* (all hardcoded): `EMBEDDING_MODEL="Qwen/Qwen3-Embedding-8B"`, `EMBEDDING_DIM=4096`, `EMBED_BATCH_SIZE=16`, `EMBED_RETRY_ATTEMPTS=3`, `EMBED_RETRY_SLEEP_S=5.0`, `EMBED_COST_PER_1M_TOKENS=0.01`

*NEBIUS configuration*:
- `NEBIUS_BASE_URL` — `os.environ.get` with hardcoded fallback `"https://api.studio.nebius.com/v1/"`
- `RAG_GENERATION_MODEL` — `os.environ.get` with hardcoded fallback `"Qwen/Qwen3-30B-A3B-Instruct-2507"`
- `GENERATION_COST_RATES` — hardcoded dict of per-model input/output rates for `Qwen3-30B-A3B-Instruct-2507` and `Qwen3-235B-A22B-Instruct-2507`
- `GENERATION_COST_PER_1M_TOKENS=0.02` — `os.environ.get` fallback rate for models not in `GENERATION_COST_RATES`

*PDF extraction configuration* (all hardcoded — these were moved from `.env` to `config.py`; change them here, not in `.env`): `PDF_RASTERISE_DPI=200`, `GEMINI_BATCH_PAGE_LIMIT=30`, `PDF_PARTITION_STRATEGY="hi_res"`, `PDF_HI_RES_MODEL="yolox"`

To change any pipeline tuning parameter, start here. Do not add these to `.env` — they are not secrets and do not vary between environments.

---

### `pipelines/extract.py`

Extracts text and table content from source PDFs and writes per-document JSONL to
`data/extracted/`. Uses `unstructured` hi_res with the yolox layout model for text elements
and section headings, and Gemini Vision for table pages.

**CLI flags:** `--company` restricts extraction to one company's PDFs. `PDF_PARTITION_STRATEGY`, `PDF_HI_RES_MODEL`, `PDF_RASTERISE_DPI`, and `GEMINI_BATCH_PAGE_LIMIT` are hardcoded constants in `config.py` — change them there, not in `.env`.

**Key decision — Gemini Vision for table pages over relying on `unstructured` alone:**
`unstructured` hi_res extracts Table elements as flattened text strings, losing column
alignment. Dense multi-column tables — emissions intensity trajectories, production
inventories — are the primary data of interest in this corpus, and a misread column value
corrupts the generated answer. Gemini Vision (`gemini-2.5-flash`) provides structured
markdown output for table content that preserves column alignment in a format generation
models can read directly. `gemini-2.5-flash` was chosen for cost-effectiveness; no other
Gemini model versions were benchmarked against it. 200 DPI was sufficient for Gemini to read
table text reliably; higher DPI values were not evaluated. The 30-page batch limit was set
because larger batches caused consistent extraction failures on long documents with dense
tables — the exact failure mode was not isolated, but the cap resolved the failures
consistently. Implemented at `extract.py:33–37` (`_GEMINI_MODEL`, `_RASTERISE_DPI`,
`_BATCH_PAGE_LIMIT`).

**Known limitation — Gemini/unstructured table count mismatch** (`extract.py:310–319`): If
Gemini returns fewer tables than `unstructured` detected on the same page (e.g. because
Gemini merged two adjacent tables into one), all remaining table slots on that page silently
duplicate `tables_on_page[-1]` — the last Gemini-extracted table. No warning is logged when
`idx >= len(tables_on_page)`. Proposed fix: add a `logging.warning()` call inside that branch
logging the page number, the number of `unstructured` tables detected, and the number of
Gemini tables returned.

**Known limitation — table classification fallback** (`chunk.py:233–235`): If a chunk is
classified as a Table element by `unstructured` but contains no pipe characters (because
Gemini was not called for that page or returned plain text), `chunk.py` silently falls back
to sentence splitting as if it were body text. No warning is emitted. Proposed fix: add a
`logging.warning()` call in `chunk.py` when an element typed as Table fails the
pipe-character check.

---

### `pipelines/chunk.py`

Reads per-document JSONL from `data/extracted/` and writes chunked JSONL to `data/chunked/`.
Implements sentence-window chunking with table-aware splitting.

**CLI flags:** `--company` restricts chunking to one company. Chunking parameters
(`MAX_CHUNK_SIZE`, `SENTENCE_OVERLAP`, `MIN_CHUNK_LENGTH`, `MAX_CHUNK_LENGTH`) are set in
`config.py`.

**Key decision — sentence-window chunking over fixed character splitting:**
Fixed character splitting (e.g. `RecursiveCharacterTextSplitter`) can cut mid-sentence,
breaking the subject-verb-value link that emission statements depend on ("Scope 1 intensity
was | 0.57 tCO2e/MWh in FY2022"). Sentence-window chunking builds chunks sentence-by-sentence,
carrying a configurable overlap buffer of complete sentences into the next chunk, so no fact
is split across a boundary. Table chunks — identified by the presence of pipe characters from
Gemini's markdown output — are kept intact regardless of length rather than being split at
all.

**Known limitation — silent table classification fallback** (`chunk.py:233–235`): When the
pipe-character check fails (Gemini was not called or returned plain text for that page), the
`is_table` flag is set to `False` without any log output, and the chunk is sentence-split as
if it were body text. This silently degrades table fidelity. See `extract.py` limitation
above for the proposed fix location.

---

### `pipelines/embed.py`

Reads per-document JSONL from `data/chunked/` and writes chunk text and float32 BLOB
embeddings to `data/vector_store.db`. Uses NEBIUS Qwen3-Embedding-8B via the OpenAI-compatible
client. Token spend is logged to `logs/token_spend.jsonl` after each batch.

**CLI flags:** `--company` restricts embedding to one company. `--batch-size` overrides the
default batch size of 16 chunks per API call. `--force` re-embeds even if a company's chunks
already appear in the database.

**Key decision — NEBIUS Qwen3-Embedding-8B over local sentence-transformers:**
Keeping embedding on NEBIUS avoids a cross-model embedding space mismatch. If the corpus were
embedded with a local model (e.g. `multi-qa-MiniLM-L6-cos-v1`) but queries at retrieval time
were embedded with a different model — or with NEBIUS's default ONNX embedder — the cosine
similarity scores would be meaningless. Using a single NEBIUS embedding model for both
indexing and retrieval guarantees the vector spaces are identical. The 4096-dimension Qwen3
model also provides higher representational capacity than 384-dimension MiniLM alternatives.

**Known limitation — 0.3s inter-batch sleep, rate limit not formally identified**
(`embed.py:415`): A 300ms pause is inserted between every batch of 16 chunks. No comment
identifies the NEBIUS Qwen3-Embedding-8B rate limit that this pause is protecting against,
or whether 0.3s was empirically determined or is a conservative default. Proposed fix: add a
comment identifying the NEBIUS rate limit (requests per minute or tokens per minute), or
explicitly document it as a conservative default pending formal measurement.

**Known limitation — `_load_sqlite_vec` no-op** (`embed.py:91–93`): `embed.py` contains a
`_load_sqlite_vec` function whose docstring says "compatibility hook retained for older docs."
It does nothing. `retrieval.py` has a fully functional version of the same helper that loads
the sqlite-vec extension. The embed pipeline does not use sqlite-vec virtual tables, so the
no-op is functionally harmless — but it misleads any developer who searches for sqlite-vec
usage. Proposed fix: remove the no-op function from `embed.py` entirely.

**Token budget:** total embed cost is tracked in `logs/token_spend.jsonl`. The pipeline's `check_budget()` function (called from `retrieval.py`) sums `cost_usd` across all record types and warns to stderr when cumulative spend reaches 90% of the `TOKEN_BUDGET_USD` (set to $100.0 in `config.py`). Embedding the full corpus cost $0.0409 in the most recent embedding run.

---

### `pipelines/retrieval.py`

Shared retrieval module imported by both pipelines and by `pipelines/evaluate_retrieval.py`.
Provides index loading, hybrid BM25+dense retrieval, value-aware context selection, citation
formatting, NEBIUS client construction, cost accounting, and the corpus inventory query used
by the decomposition prompt.

**Key decision — hybrid BM25 + dense retrieval with RRF over dense-only:**
Dense-only retrieval ranks chunks by cosine similarity to the query embedding. On this corpus
— sustainability reports with company names, year references (e.g. "FY2023"), unit labels
(e.g. "tCO2e/MWh"), and emissions scope terminology — a query using standard English phrasing
does not reliably match the verbatim vocabulary used in the source documents. BM25 captures
exact keyword matches that semantic similarity misses. The hybrid retrieval combines dense
cosine scores with BM25 scores using Reciprocal Rank Fusion. The BM25 weight was empirically
determined: `BM25_WEIGHT` was swept across 0 (dense-only), 1, 2, and 3; weight=2 produced
the best retrieval results across the benchmark question set and is hardcoded at
`retrieval.py:48`. Implemented in `hybrid_retrieve()`.

**Key decision — RRF_K = 60 adopted from the original paper over corpus-specific sweeping:**
`RRF_K` controls the smoothing constant in the reciprocal rank formula `1/(k + rank)`. The
value 60 is taken directly from Cormack et al. (2009), the paper that introduced RRF.
Retrieval evaluation showed results are insensitive to this parameter on the six-question
benchmark set, so the paper default was accepted as final without corpus-specific sweeping.
Implemented at `retrieval.py:48`.

**Key decision — three-pass value-aware context selection over rank-fill alone:**
Initial retrieval by RRF score alone returned contextually relevant chunks for trajectory
questions that did not contain the specific numerical values needed to answer the question —
the model received chunks about the right company and year but without the key figures.
`select_context_chunks()` runs three sequential passes before returning the context window:
Pass 1 (value-aware) reserves one chunk per (company, year) pair, preferring candidates that
match `_INTENSITY_VALUE_RE`, `_TARGET_YEAR_RE`, or `_PERCENTAGE_VALUE_RE`; this was validated
on Q1, where it promoted chunks containing specific carbon intensity figures that rank-fill
alone would not have selected. Pass 2 is a company-only fallback for cases where no
value-bearing candidate exists. Pass 3 fills remaining context slots in retrieval-rank order.
Implemented at `retrieval.py:558–654`.

**Key decision — `ENABLE_BOILERPLATE_FILTER = False` (disabled) over active boilerplate
removal:**
A filter (`filter_boilerplate_chunks()`) that removes running-headers and low-information
chunks was implemented and tested against a 2024 emissions activity query. It successfully
removed boilerplate running-headers from the candidate pool, but the ground-truth chunk
rankings were unaffected — the filter changed which chunks appeared in the pool but did not
improve the final ranked output. Disabling it was a conservative final decision: the marginal
retrieval benefit did not justify the risk of false positives on other query types or on
documents added later. The flag is hardcoded to `False` at `retrieval.py:351` rather than
exposed as a config variable, signalling it is not expected to be toggled at runtime.

**Known limitation — boilerplate filter thresholds set by corpus inspection, not formal sweep**
(`retrieval.py:274–329`): The four thresholds in `filter_boilerplate_chunks`
(`max_chunk_length=300`, `min_digit_count=8`, `prefix_chars=80`, `min_duplicate_count=3`)
were calibrated by inspecting boilerplate examples from the TNB/DEWA/CenterPoint corpus. No
retrieval eval run measures sensitivity to them. Proposed fix: add inline comments citing the
specific corpus examples that drove each threshold value, so a future maintainer can assess
whether they remain appropriate when new documents are added.

**Token budget:** `check_budget(phase_label)` sums `cost_usd` across all records in
`logs/token_spend.jsonl` and logs a warning when cumulative spend exceeds 90% of
`TOKEN_BUDGET_USD` (default $100.0). Call it after any generation phase to surface budget
overruns before the next API call.

---

### `pipelines/single_shot_rag.py`

Runs one question through a single retrieval pass and one generation call, then persists
results to `db/benchmark.db`. This is the baseline pipeline.

**CLI flags:** `--question` (required), `--model` (NEBIUS model string, defaults to
`RAG_GENERATION_MODEL` env var), `--top-n` (candidate pool size before context selection),
`--context-chunks` (final context window size), `--temperature`, `--max-output-tokens` (default
1024), `--verbose`, `--dry-run`.

**Key decision — one retrieval pass and one generation call per question:**
The single-shot pipeline writes exactly two rows to `db/benchmark.db` per question: one row
in `runs` (pipeline_type='single_shot') and one row in `steps` at `step_index=1`. The step
row stores the full retrieved chunk list and the generated answer. This symmetric schema —
where single-shot and multi-step both write to `steps` — means `dump_runs.py` and the
evaluation scripts can join across both pipeline types without branching.

**Known limitation — `stable_qid` function duplicated across three files:** The function that
generates a 16-hex-character SHA-1 question ID is independently defined in
`single_shot_rag.py:92–96`, `multi_step_rag.py:92–96`, and `scripts/migrate_qn_ids.py`. A
change to the hashing logic in one file will not propagate to the others. Proposed fix: move
`stable_qid` to `utils.py` and import it from there in both pipeline files.
(`migrate_qn_ids.py` may retain its own copy since it is a one-time script that must not be
run again.)

---

### `pipelines/multi_step_rag.py`

Runs one question through decomposition, a sequential sub-question loop, and an assembly call.
Persists every intermediate answer to `db/benchmark.db` before advancing to the next step.

**CLI flags:** `--question`, `--model`, `--top-n`, `--context-chunks`, `--temperature`,
`--max-output-tokens` (applies to sub-question calls), `--verbose`, `--dry-run`,
`--resume` (resume from last completed step), `--review` (inspect and edit sub-questions
before answering begins).

**Key decision — Least-to-Most (LtM) decomposition over Chain-of-Thought, Self-Ask, or
ReAct:**
LtM decomposes each question upfront into an ordered list of sub-questions, then answers them
sequentially with prior answers carried forward as context. We chose it for three properties:
predictable token cost (N+1 NEBIUS calls per question — one decomposition, N sub-questions,
one assembly), a clean mapping onto the SQLite schema (each sub-question becomes a
pre-inserted `steps` row at `status='pending'`), and a natural fit with trajectory and
change-over-time questions where earlier sub-steps (retrieve 2019 intensity) enable later ones
(compute percentage change). ReAct was retained as a stretch goal; the research notes
comparing all four strategies are in `docs/decomposition_research.md`.

**Key decision — assembly `max_tokens = 2048` over 1024 for sub-question calls:**
Sub-question answering uses a 1024-token output limit, and no truncation was observed at that
limit during benchmark runs. Assembly receives 2048 tokens because its task is qualitatively
different: it must synthesise findings across all sub-questions into a single coherent final
response — a longer output task than any individual sub-question answer. The higher limit for
assembly is a design choice grounded in the nature of the task, not a reaction to observed
truncation. Implemented at `multi_step_rag.py:77–79`
(`DEFAULT_MAX_OUTPUT_TOKENS_ASSEMBLY = 2048`, `DEFAULT_MAX_OUTPUT_TOKENS_SUB = 1024`).

**Key decision — reranking removed from scope before implementation over running a local
cross-encoder:**
The original `DECISIONS.md` planned to run `cross-encoder/ms-marco-MiniLM-L-6-v2` locally on
Nuvolos. This was never implemented. NEBIUS does not offer reranker models, so a local
cross-encoder would have been required — breaking the clean NEBIUS-only compute model that the
rest of the pipeline maintains and introducing a local dependency on Nuvolos hardware that
would fail in any other environment. The hybrid BM25+dense retrieval with three-pass
value-aware context selection was found sufficient for the benchmark question set, making the
reranker unnecessary.

**Resume mechanics:** `--resume` causes the pipeline to skip decomposition entirely and query
`db/benchmark.db` for steps belonging to this run where `status = 'pending'`. The sub-question
loop (`get_pending_steps()`) returns these rows ordered by `step_index` and processes them one
at a time. A run interrupted after step 4 of 9 resumes at step 5 without re-spending NEBIUS
tokens on steps 1–4. `get_completed_steps()` is called at the start of each iteration to
reconstruct the established-facts context from prior steps.

**Known limitation — assembly token counts not stored on resume** (`multi_step_rag.py:706–
710`): When a multi-step run is resumed across two separate process invocations, the assembly
token counts are not stored in the `runs` table — the comment at that line acknowledges this
as a "known gap" for the 6-question benchmark. Proposed fix: capture `assembly_tokens` as a
local variable inside the assembly phase and pass it explicitly to
`database.complete_run()`, independent of whether the run was resumed.

---

### `db/database.py` and `db/schema.sql`

`db/database.py` is the only file in the repository that imports `sqlite3` directly for the
benchmark store. All pipeline code calls functions from this module rather than constructing
SQL directly. This constraint means that a schema change requires updating only `db/schema.sql`
and `db/database.py`, not every pipeline file.

`db/schema.sql` contains the canonical `CREATE TABLE IF NOT EXISTS` definitions. The database
is initialised by calling `database.init_db(conn)`, which executes the full schema file. This
is called automatically on the first pipeline invocation — no manual setup is needed.

**Write sequence per run:**

1. `database.insert_question(conn, question_id, question_text)` — `INSERT OR IGNORE` so
   re-running the same question is safe.
2. `database.create_run(conn, question_id, pipeline_type, model)` — inserts with
   `status='running'`; returns `run_id`.
3. *(Multi-step only)* `database.create_decomposition(conn, run_id)` — inserts into
   `decompositions` with `status='pending'`; updated to `'complete'` after the LLM call.
4. *(Multi-step only)* `database.insert_step(conn, run_id, step_index, sub_question)` — N
   rows inserted as `status='pending'` before any retrieval or generation begins.
5. For each step: `database.complete_step(conn, step_id, answer, chunks, in_toks, out_toks)`
   — updates `status='complete'`; this must happen before the next step begins.
6. `database.insert_final_answer(conn, run_id, answer)` — inserts into `final_answers`.
7. `database.complete_run(conn, run_id, latency, total_tokens, cost)` — updates `runs` to
   `status='complete'`. On failure, set `status='failed'`.
8. *(After manual evaluation)* `load_evaluations.py` inserts one row into `evaluations` per
   run — separate from pipeline execution so results can be re-scored independently.

**Resume mechanism:** `database.get_pending_steps(conn, run_id)` executes
`SELECT * FROM steps WHERE run_id=? AND status='pending' ORDER BY step_index`. The
multi-step loop calls this at the top of each iteration; when it returns an empty list, the
loop exits and assembly begins.

---

### `evaluation/` (scoring aids)

These scripts are operated by a human evaluator after pipeline runs complete. They are not
pipeline stages and are not invoked by `pipelines/run_all.py`.

`evaluation/make_worksheet.py` — reads `db/benchmark.db` and writes
`evaluation/scoring_worksheet.md`, one block per run. The `--force` flag is required to
overwrite an existing worksheet that contains manual scores; without it, the script exits
rather than risk destroying completed scores that cannot be regenerated from the database.

`evaluation/make_faithfulness_sheet.py` — accepts optional run IDs as positional arguments;
writes `evaluation/faithfulness_all.md` showing each answer beside the full text of every
retrieved chunk, grouped by sub-question step, for claim-by-claim comparison.

`evaluation/load_evaluations.py` — reads completed scores from `evaluation/scoring_worksheet.md`
and inserts one row per run into the `evaluations` table. Idempotent; `--dry-run` validates
without writing.

`evaluation/inspect_intermediate_answers.py` — read-only rendering tool; accepts run IDs and
prints sub-step questions, answers, and retrieved chunks as markdown.

`evaluation/analysis.ipynb` — produces all comparison tables, cost and latency figures, verdict
aggregates, and inspectability examples. Run all cells after every `load_evaluations.py` call.
Figures are written to `docs/images/`.

---

## SQLite Schema

### `db/benchmark.db` — created by `db/schema.sql`, managed by `db/database.py`

```sql
CREATE TABLE IF NOT EXISTS questions (
    question_id   TEXT PRIMARY KEY,  -- SHA-1 hex of normalised question text, first 16 chars
    question_text TEXT NOT NULL,
    ground_truth  TEXT,              -- expected answer; NULL in practice (not populated by pipelines)
    question_type TEXT               -- 'trajectory'|'change_over_time'|'comparative'|'mixed'; NULL in practice
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     TEXT    NOT NULL REFERENCES questions(question_id),
    pipeline_type   TEXT    NOT NULL,       -- 'single_shot' | 'multi_step'
    model_name      TEXT,                   -- NEBIUS model string used for generation
    status          TEXT    NOT NULL DEFAULT 'pending',  -- 'pending'|'running'|'complete'|'failed'
    started_at      TEXT    DEFAULT (datetime('now')),
    completed_at    TEXT,
    latency_seconds REAL,
    total_tokens    INTEGER,                -- prompt + completion tokens across all phases
    total_cost_usd  REAL                   -- generation cost only; see logs/token_spend.jsonl for embedding
);

-- One row per multi-step run; single_shot never writes here
CREATE TABLE IF NOT EXISTS decompositions (
    decomp_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL UNIQUE REFERENCES runs(run_id),
    sub_questions TEXT,    -- JSON array of parsed sub-question strings; '[]' on parse failure
    raw_response  TEXT,    -- unparsed LLM output, stored for debugging parse failures
    status        TEXT    NOT NULL DEFAULT 'pending',  -- 'pending'|'complete'|'failed'
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at    TEXT    DEFAULT (datetime('now'))
);

-- Both pipelines write here; single_shot writes exactly one row at step_index=1
CREATE TABLE IF NOT EXISTS steps (
    step_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    step_index       INTEGER NOT NULL,    -- 1-indexed position in sub-question sequence
    sub_question     TEXT,
    retrieved_chunks TEXT,                -- JSON array: [{chunk_id, page_number, document_label}]
    answer           TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',  -- 'pending'|'complete'
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    created_at       TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS final_answers (
    answer_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    final_answer TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- 'pending'|'complete'
    created_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS evaluations (
    eval_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(run_id),
    correctness        TEXT,   -- fraction string e.g. '3/5', or verdict 'correct'|'wrong'|'abstained'
    faithfulness_score TEXT,   -- fraction string e.g. '3/4' or decimal '0.75'
    evaluator_notes    TEXT,   -- free text; inspectability observations go here
    evaluated_at       TEXT    DEFAULT (datetime('now'))
);
```

### `data/vector_store.db` — created by `pipelines/embed.py`

```sql
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT    PRIMARY KEY,
    company     TEXT    NOT NULL,
    document_id TEXT    NOT NULL,    -- filename stem without _chunks suffix, e.g. "TNB_2024"
    year        INTEGER,             -- extracted from filename; NULL if not found
    chunk_index INTEGER NOT NULL,    -- 0-indexed position within the document
    page_number INTEGER,             -- from unstructured element metadata
    text        TEXT    NOT NULL,
    metadata    TEXT    NOT NULL     -- JSON string of full metadata dict from chunk.py
);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id  TEXT PRIMARY KEY,
    embedding BLOB NOT NULL          -- little-endian float32 array; 4096 × 4 = 16384 bytes
);
```

**Two-database architecture:** `data/vector_store.db` is created once by `embed.py` and
treated as read-only at pipeline runtime — neither `single_shot_rag.py` nor
`multi_step_rag.py` writes to it. `db/benchmark.db` is written throughout pipeline execution:
every run, step, decomposition, and final answer is persisted there. The two databases serve
different purposes and live at different paths; they must not be confused. Foreign key
enforcement (`PRAGMA foreign_keys = ON`) is enabled on every connection to `benchmark.db` via
`database.get_connection()`.

---

## Development Conventions

### Code style

- Type hints throughout: `Path`, `list[dict]`, `str | None`. Use `from __future__ import
  annotations` at the top of each module for forward compatibility.
- `logging` not `print` — use `logger = logging.getLogger(__name__)` at module level.
  INFO for normal progress; WARNING for degraded operation (fallbacks, skipped items);
  ERROR for failures that will abort the run.
- `pathlib.Path` not string concatenation for all file paths.
- Environment variables loaded via `python-dotenv` at module level through `utils.py` —
  never hardcoded, never read with `os.environ` directly in pipeline files.
- [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Configuration lives in `pyproject.toml`.
   - Before pushing, run:

      ```bash
      ruff check . --fix    # lint and auto-fix
      ruff format .         # format
      ```
   - Both checks also run in CI on every push and pull request via `.github/workflows/lint.yml`. If CI is red, fix it locally and push again rather than overriding the check.
- [pytest](https://docs.pytest.org/) for unit tests. Tests live in
`tests/` and configuration is in `pytest.ini`.
   - The suite covers deterministic logic only — chunker helpers, utility
   functions, and database schema. It does NOT test the LLM pipelines or
   retrieval quality; those are evaluated separately via the benchmark
   runs.

### Commit conventions

Commits follow the conventional commits format:

| Prefix | Use for |
|---|---|
| `feat:` | New functionality |
| `fix:` | Bug fix |
| `chore:` | Maintenance — dependency updates, file moves |
| `docs:` | Documentation only |
| `refactor:` | Code change with no behaviour change |
| `eval:` | Evaluation runs and result commits |

**Good commit message:**

```
fix: store assembly_tokens in runs.total_tokens on resumed multi-step runs

Previously, resuming a run across two processes left assembly token counts
unaccounted in the runs table. Capture assembly_tokens locally in the assembly
phase and pass to complete_run() regardless of resume status.
```

The message names the specific problem, where it manifested, and what the fix does.

**Avoid:**

```
Update multi_step_rag.py
```

This tells a reviewer nothing about what changed or why.

---

## How to Add a New Benchmark Question

1. **Add the question to `evaluation/ground_truth.md`.** Follow the existing format:
   question text, expected answer (with specific numerical values), and for each source a
   document name, page number, section heading, exact quote, and chunk_id. The ground-truth
   file is the benchmark — every claim in the expected answer should be traceable to a
   specific chunk.

2. **Verify the relevant chunk is retrievable.**

   ```bash
   python pipelines/evaluate_retrieval.py
   ```

   This reports Recall@k and MRR against `evaluation/ground_truth.md`. If the new question's
   ground-truth chunks do not appear in the top k results, adjust the question text or
   investigate retrieval before investing NEBIUS tokens in generation runs.

3. **Run the question through both pipelines.**

   ```bash
   python pipelines/single_shot_rag.py --question "your question text here" \
     --model Qwen/Qwen3-30B-A3B-Instruct-2507

   python pipelines/multi_step_rag.py --question "your question text here" \
     --model Qwen/Qwen3-30B-A3B-Instruct-2507
   ```

   Use `--review` on the multi-step run to inspect the decomposition before answering begins.

4. **Generate the scoring worksheet and score the new run.**

   ```bash
   python evaluation/make_worksheet.py --force
   ```

   The `--force` flag is required because an existing scored worksheet is present. Open
   `evaluation/scoring_worksheet.md` and fill in the correctness fraction, faithfulness
   score, and evaluator notes for the new run.

5. **Load the score into the database.**

   ```bash
   python evaluation/load_evaluations.py --dry-run   # validate first
   python evaluation/load_evaluations.py
   ```

6. **Refresh `logs/runs.jsonl`.**

   ```bash
   python pipelines/dump_runs.py
   ```

7. **Re-run `evaluation/analysis.ipynb`** to update all comparison tables, cost figures, and
   verdict aggregates to include the new question.

---

## How to Extend the Decomposition

**Where the decomposition logic lives:** The LLM prompt that drives decomposition is
`DECOMP_USER_PROMPT` in `pipelines/multi_step_rag.py`. It contains four worked examples
with 7, 7, 9, and 8 sub-questions respectively. These examples are the primary signal the
LLM uses to determine how many sub-questions to generate and how to structure them for a
given question type. The number of sub-questions is dynamic — not hardcoded — so observed
counts range from 4 (simple single-company trajectory) to 13 (three-company comparison).

**How to change the decomposition for one question without re-running others:** Pass
`--review` when running `multi_step_rag.py`. The pipeline pauses after the decomposition
call, prints the generated sub-questions to the terminal, and opens an interactive edit loop.
Edit the sub-questions before typing `done` to proceed with answering. The edited
sub-questions are written to the `decompositions` table and then to N `steps` rows before
any retrieval begins.

**If adding a new question type** (not trajectory, comparative, or change-over-time): add a
worked example to `DECOMP_USER_PROMPT` in `multi_step_rag.py` that demonstrates the correct
sub-question structure for that type. Run the first new question of that type with `--review`
to validate the decomposition before committing to a full run.

**What downstream code must be updated:** Nothing. The sub-question count is determined at
runtime by the LLM and stored in `decompositions.sub_questions`. The step loop in Phase 2
reads `get_pending_steps()` until it returns empty — it does not depend on a hardcoded step
count. The assembly call reads `get_completed_steps()` regardless of how many steps exist.
Adding a new question type requires no changes outside `DECOMP_USER_PROMPT`.

---

## Known Limitations

**Assembly token counts not stored on resume**
What it is: When a multi-step run is resumed across two separate process invocations, the
assembly token counts are not stored in the `runs` table.
Where it manifests: `multi_step_rag.py:706–710` — the comment acknowledges this as a "known
gap" for the 6-question benchmark.
Proposed fix: Capture `assembly_tokens` as a local variable inside the assembly phase and pass
it explicitly to `database.complete_run()` in `multi_step_rag.py`, independent of whether
the run was resumed.

---

**Gemini/unstructured table count mismatch — silent fallback to last table**
What it is: If Gemini returns fewer tables than `unstructured` detected on the same page (e.g.
because Gemini merged two adjacent tables), all remaining table slots on that page silently
duplicate `tables_on_page[-1]` — the last Gemini-extracted table.
Where it manifests: `extract.py:310–319` — the `page_table_cursor` assignment logic; no
warning is emitted when `idx >= len(tables_on_page)`.
Proposed fix: Add a `logging.warning()` call in `extract.py` inside the branch that fires
when `idx >= len(tables_on_page)`, logging the page number, the number of `unstructured`
tables detected, and the number of Gemini tables returned.

---

**Table classification fallback — silent `is_table = False`**
What it is: If a chunk is classified as a Table element by `unstructured` but contains no pipe
characters (because Gemini was not called for that page or returned plain text), the chunk
silently falls back to sentence splitting as if it were body text.
Where it manifests: `chunk.py:233–235` — the `is_table` flag is set to False without any
warning.
Proposed fix: Add a `logging.warning()` call in `chunk.py` when an element typed as Table
fails the pipe-character check, logging the document, page number, and chunk index.

---

**`_load_sqlite_vec` no-op in `embed.py` diverged from functional version in `retrieval.py`**
What it is: `embed.py` contains a `_load_sqlite_vec` function that does nothing — its
docstring says "compatibility hook retained for older docs." `retrieval.py` has a fully
functional version of the same helper that loads the sqlite-vec extension. The two have
diverged silently.
Where it manifests: `embed.py:91–93`.
Proposed fix: Remove the no-op function from `embed.py` entirely. The embed pipeline does not
use sqlite-vec virtual tables, so the function serves no purpose and misleads any developer
searching for sqlite-vec usage.

---

**`stable_qid` function duplicated across three files**
What it is: The function that generates a 16-hex-character SHA-1 question ID is independently
defined in three files. A change to the hashing logic in one file will not propagate to the
others.
Where it manifests: `single_shot_rag.py:92–96`, `multi_step_rag.py:92–96`,
`scripts/migrate_qn_ids.py`.
Proposed fix: Move `stable_qid` to `utils.py` and import it from there in both pipeline files.
`migrate_qn_ids.py` may retain its own copy since it is a one-time script that must not be
run again.

---

**`GENERATION_COST_RATES` model string drift**
What it is: `config.py` lists explicit per-token rates for two model strings that include a
date suffix (`-2507`). If NEBIUS updates the model and changes the string, cost accounting
silently falls back to the generic `GENERATION_COST_PER_1M_TOKENS = 0.02` rate for both input
and output, with no warning.
Where it manifests: `config.py:49–53`.
Proposed fix: Add a warning log in `log_generation_spend()` in `retrieval.py` when the model
string passed to the function does not match any key in `GENERATION_COST_RATES`, so that rate
drift is surfaced at generation time rather than discovered in post-hoc cost accounting.

---

**Boilerplate filter thresholds set by corpus inspection, not formal sweep**
What it is: The four numerical thresholds in `filter_boilerplate_chunks`
(`max_chunk_length=300`, `min_digit_count=8`, `prefix_chars=80`, `min_duplicate_count=3`)
were calibrated by inspecting boilerplate examples from the TNB/DEWA/CenterPoint corpus. No
retrieval eval run explicitly measures sensitivity to them.
Where it manifests: `retrieval.py:274–329`.
Proposed fix: Add inline comments in `retrieval.py` for each threshold value citing the
specific corpus examples that drove the choice, so that a future maintainer can assess whether
the thresholds remain appropriate when new documents are added.

---

**Embedding batch sleep 0.3s — NEBIUS rate limit not formally identified**
What it is: A 300ms pause is inserted between every embedding batch. No comment explains what
NEBIUS rate limit this is protecting against, or whether 0.3s was empirically determined or
is a conservative default.
Where it manifests: `embed.py:415`.
Proposed fix: Add a comment in `embed.py` at the sleep call identifying the NEBIUS
Qwen3-Embedding-8B rate limit (requests per minute or tokens per minute), or if the limit has
not been formally identified, explicitly documenting it as a conservative default pending
formal measurement.

---

**`migrate_qn_ids.py` must not be run again**
What it is: A one-time data repair script that collapsed duplicate question rows to stable
SHA-1 hash IDs. It disabled foreign key enforcement (`PRAGMA foreign_keys = OFF`) during the
migration to perform updates that would otherwise violate foreign key constraints.
Where it manifests: `scripts/migrate_qn_ids.py`.
The script has already been applied (commit `ba48de4`). Running it again would corrupt the
database by applying the ID transformation a second time. Proposed fix: add a prominent
warning comment at the top of the script — `# THIS SCRIPT HAS ALREADY BEEN APPLIED (commit
ba48de4). DO NOT RUN AGAIN.` — so that a future maintainer does not accidentally invoke it.
