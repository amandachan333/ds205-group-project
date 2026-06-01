# AI_USAGE.md

A retrospective record of how AI coding tools were used during development of the
tpi-rag-decomposition project. Written for two audiences: future contributors who want
to use AI tools effectively on this codebase, and the module marker who needs evidence
of intentional human oversight of AI-generated code.

**Looking for operating instructions for an agent working in this repo** — setup,
commands, conventions, what not to touch? That lives in [`AGENTS.md`](../AGENTS.md) at the repo root,
not this file. That one is forward-looking instructions; this one is a backward-looking
record.

## 1. Session conventions

Every Claude Code session opened with the same standing instruction:

```
Read these files before doing anything:

"README.md"
"CONTRIBUTING.md"
"DECISIONS.md"

These establish what the project is and what conventions were followed.
Confirm you have read all three before continuing.
```

This was not a one-off instruction. It was given at the start of every session,
before any code or documentation task was described.

The three files cover different scopes and together constrain the space of
acceptable outputs:

- **README.md** establishes user-facing behaviour — what the pipelines do, how to
  run them, and what outputs they produce. Without it, AI tools default to generic
  patterns: a `main.py` entry point, a `requirements.txt`, argparse where we use
  YAML config. Reading README.md first means generated code targets the actual
  interface, not a reasonable guess at one.

- **CONTRIBUTING.md** establishes implementation conventions — the repo structure,
  how data flows through the SQLite schema, the write sequence per run, and which
  modules own which responsibilities. Without it, AI tools will make sensible but
  inconsistent choices: importing `sqlite3` directly in pipeline files instead of
  routing through `db/database.py`, or using `print()` instead of `logging`.

- **DECISIONS.md** records architectural rationale — why we chose LtM over ReAct,
  why reranking was dropped, why the vector store is SQLite and not ChromaDB. Without
  it, AI tools will re-introduce removed components or argue for alternatives we
  already considered and rejected, which wastes time and risks re-opening settled
  questions.

Together, these three files tell the AI what the project does, how it is built, and
why it is built that way. Any two of the three leaves a gap.

We also consistently asked for tasks to be broken into steps before any code or
documentation was written. The prompt pattern was:

```
Before writing any code, describe in plain English:

- What the function/module will do
- What its inputs and outputs are
- What edge cases it needs to handle

Then wait for confirmation before writing the code.
```

Planning before implementation surfaces misunderstandings about scope before tokens
are spent on the wrong thing. On this project, the planning step for the multi-step
pipeline orchestrator revealed that the original description implied the decomposer
and solver were separate processes rather than sequential calls within one run —
catching that before implementation saved a complete rewrite of the step-insert logic.

## 2. Where AI tools helped most

**Task 1 — Repurposing the Problem Set 2 pipeline**

The core retrieval, embedding, and chunking stages were adapted from a working
pipeline built in Problem Set 2 for a different document corpus. AI tools handled
this translation cleanly because the architecture was already established — the
task was adaptation, not design. The embedding call signatures, chunking parameters,
and BM25 hybrid merge logic from Problem Set 2 transferred directly into the
`pipelines/` directory, with changes limited to swapping document IDs, adjusting chunk
size to match the TPI PDF structure, and updating the vector store schema column
names. The output was verifiable against the source pipeline: we ran both on the same
test query and compared retrieved chunks by cosine similarity score, confirming the
ported version produced identical rankings. When the task is adaptation rather than
design and the source is a verified working implementation, AI-generated output is
trustworthy with light inspection.

**Task 2 — Auditing for duplicate functions**

We used AI to audit the codebase for duplicated logic across pipeline files. This
surfaced the `stable_qid` function — a SHA-1 hash used to derive stable question IDs
from question text — existing independently in `single_shot_rag.py`,
`multi_step_rag.py`, and `scripts/migrate_qn_ids.py`. The audit output was used
directly: the duplication is documented in CONTRIBUTING.md as a known limitation
rather than silently refactored, because removing it would have required changes
across files that had no test coverage at the point the audit ran. This task is
well-suited to AI tools because it requires reading many files in sequence without
making design judgements. The AI was not asked to fix the duplication — it was asked
to find it. The decision about what to do was made by the team.

**Task 3 — Generating structured boilerplate**

The SQLite schema (`db/schema.sql`), the token spend JSONL record structure
(`logs/token_spend.jsonl`), and the database read/write functions (`db/database.py`)
were generated with AI assistance. These are high-structure, low-ambiguity tasks:
the schema tables, column names, and types had been defined in CONTRIBUTING.md
before any code was generated, so the output could be verified by inspection against
the spec. The generated `CREATE TABLE` statements, `INSERT OR IGNORE` setup logic,
and `PRAGMA foreign_keys = ON` connection setup were adopted as-is. Light editing
was needed to align a handful of column names — `total_tokens` vs `token_count` in
the `runs` table — that differed between the spec in CONTRIBUTING.md and the first
draft. The structure required no redesign.

## 3. Where AI tools needed correction

**Case 1 — Assembly `max_tokens` set without rationale**

When scaffolding `multi_step_rag.py`, the assembly LLM call was given
`max_tokens=2048` while sub-question calls received `max_tokens=1024`. The AI
generated both values without comment or rationale. A maintainer reading the file
later would have no way to know whether the difference was intentional or a copy
error. We retained the values after verifying that no truncation was observed at
1024 during sub-question answering across the six benchmark questions — 1024 tokens
is sufficient for a focused factual answer to a single sub-question. We then added
a comment and a note to CONTRIBUTING.md documenting the reasoning: assembly
synthesises findings across all sub-questions and is a longer output task than any
individual step, so it warrants a higher ceiling. The AI produced working values but
not the justification a maintainer would need.

**Case 2 — CONTRIBUTING.md repo structure reflected the plan, not the codebase**

The initial CONTRIBUTING.md was generated with a repo structure that matched the
early planned layout: `pipelines/single_shot/` and `pipelines/multi_step/`
subdirectories, `evaluation/harness.py`, `evaluation/scorer.py`. By the time the
documentation was written, the actual structure had diverged significantly — flat
`pipelines/*.py` files rather than subdirectories, and evaluation scripts used as
scoring aids rather than as pipeline stages with their own module boundary. The error
was caught by running a codebase audit before accepting the output: listing actual
files and comparing them against the generated structure. The repo structure section
in CONTRIBUTING.md was rewritten against the filesystem, not the mental model the
prompt had implied. AI-generated repo documentation must always be validated against
the actual filesystem. It will describe what a well-structured project of this type
typically looks like, which is not the same as what this specific project contains.

**Case 3 — `report.md` listed components that were never implemented**

A draft of `report.md` described the shared stack as including a "local ms-marco-
MiniLM-L-6-v2 cross-encoder rerank" and "ChromaDB vector store". Neither was used
in the final implementation. The reranker was cut from scope before implementation
(documented in DECISIONS.md). The vector store is SQLite, not ChromaDB. The error
originated in early planning notes that the AI incorporated into the report draft
without checking whether they described the current implementation or a superseded
plan. The error was caught during a cross-document consistency review — reading
DECISIONS.md against the report draft and finding the contradiction. Fixed in
DECISIONS.md and the report before submission. Any document that describes the
technical stack must be checked against the actual codebase, not against earlier
planning notes or design documents that may have been superseded.

**Case 4 — Self-contained scripts created duplicate utility functions**

When generating pipeline scripts independently in separate sessions, the AI kept
each script self-contained by implementing `stable_qid` directly rather than
importing it from a shared location. The result was three copies of the same
SHA-1 hash function across `single_shot_rag.py`, `multi_step_rag.py`, and
`scripts/migrate_qn_ids.py`. This is documented in CONTRIBUTING.md as known tech
debt. The pattern recurs because each session started from the same context files
but the AI had no way to know what utility functions had been written in previous
sessions unless explicitly told. The fix is a standing instruction: when adding any
new script, explicitly ask the AI to search for existing utility functions before
implementing new ones. Generating scripts in isolation produces locally coherent but
globally inconsistent code.

## 4. Decisions kept out of AI scope

The following decisions were made by the team without AI assistance.

**Decomposition design.** The choice of Least-to-Most over Chain-of-Thought,
Self-Ask, and ReAct was made after the team read and discussed the analysis in
`decomposition_research.md`. The sub-question boundaries for each question type —
what sub-questions a trajectory question decomposes into, what sub-questions a
change-over-time question decomposes into — were defined by the team because they
depend on understanding what a TPI Carbon Performance assessor actually needs to
establish: which intensity figures, across which years, against which baselines,
and whether targets were met or revised. That judgment is about the domain, not the
technology. AI tools could have proposed a decomposition structure, but the team
would have had no reliable way to evaluate whether the proposed sub-questions were
the right ones without already understanding the domain well enough to do it
themselves.

**Ground truth construction.** The six benchmark questions and their reference
answers were written before any pipeline was built, derived manually from the source
PDFs with specific page numbers, section titles, and chunk IDs recorded for
traceability. Two team members cross-checked each answer independently before
freezing the set. This could not be delegated to AI tools: the ground truth defines
what "correct" means for the entire benchmark, and AI-generated ground truth would
make the evaluation circular. A model that misreads a density table and produces a
plausible but wrong intensity figure would score well against a reference answer
derived from the same misread.

**The key-claim fraction metric.** The decision to score correctness as k/n key
claims rather than the brief's proposed three-level label (correct / partial /
incorrect) was made during actual scoring, after finding that the three-level label
could not cleanly separate the four pipeline configurations. Several answers were
partially correct in ways that the three-level label collapsed together — two
conditions might both be "partial" while one matched six of eight key claims and the
other matched two of eight. The fraction metric made these differences visible and
the comparisons meaningful. This methodological adjustment required seeing real
results: it cannot be anticipated in advance and was not suggested by AI.

**The three-way faithfulness classification.** Distinguishing supported, unsupported,
and corpus-error claims was a methodological decision made during scoring when
extraction artifacts in Q5 caused answers to be faithful to wrong text. A pipeline
that accurately reproduced a misextracted table value should not receive the same
faithfulness score as one that hallucinated a figure with no source in the retrieved
chunks. Collapsing corpus-error into "unsupported" would have misattributed
data-quality failures to model hallucination and made the faithfulness scores
misleading as a measure of pipeline quality. The distinction required understanding
the cause of specific observed failures, not pattern-matching to a scoring rubric.

**The recommendation to Sylvan.** The conclusion that multi-step decomposition is
worth adopting selectively for high-stakes analytical questions, and that the largest
available gain for both pipelines is upstream extraction quality, was written by the
team. It required interpreting what the benchmark results mean for TPI's actual use
case — how often assessors ask trajectory and change-over-time questions, what the
cost of a wrong answer is, and where the marginal return on additional engineering
effort is highest. That is a domain and stakeholder judgment, not a summarisation
task.

## 5. Prompt patterns that worked

**Pattern 1 — Context-first instruction**

```
Read [file A], [file B], and [file C] before doing anything.
Confirm you have read all three before continuing.
```

Loading the relevant context files before any task prevents AI tools from defaulting
to generic patterns. On this project, reading CONTRIBUTING.md before generating any
code meant the output used `logging` not `print()`, `pathlib` not string
concatenation, and typed function signatures — without needing to repeat these
requirements in every prompt. The confirmation step matters: it creates a checkpoint
where the AI summarises what it found, which surfaces cases where a file was read but
its constraints were not understood.

**Pattern 2 — Plan before implement**

```
Before writing any code, describe in plain English:

- What the function/module will do
- What its inputs and outputs are
- What edge cases it needs to handle

Then wait for confirmation before writing the code.
```

This pattern surfaces misunderstandings before implementation. On this project it
was used for the multi-step pipeline orchestrator and the token spend logging design.
For the orchestrator, the planning step revealed that the described flow implied
the decomposer returned sub-questions as a flat list when the schema required them
inserted as rows before retrieval began — the plan was corrected first, then
implementation followed with the right insert sequence. For token spend logging, the
plan revealed ambiguity about whether to log per-step token counts or only the run
total; the decision to log both (steps table stores per-step counts, runs table
stores the total) was made in the planning exchange, not discovered during debugging.
