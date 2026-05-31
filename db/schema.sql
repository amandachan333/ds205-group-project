-- schema.sql
-- Canonical database schema for the Project C benchmark pipeline.
-- Executed once at startup via init_db() in db/database.py.
-- To recreate the database from scratch:
--   sqlite3 db/benchmark.db < db/schema.sql

CREATE TABLE IF NOT EXISTS questions (
    question_id   TEXT PRIMARY KEY,
    question_text TEXT NOT NULL,
    ground_truth  TEXT,
    question_type TEXT   -- 'trajectory' | 'change_over_time' | 'comparative' | 'mixed'
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id     TEXT    NOT NULL REFERENCES questions(question_id),
    pipeline_type   TEXT    NOT NULL,  -- 'single_shot' | 'multi_step'
    model_name      TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'complete' | 'failed'
    started_at      TEXT    DEFAULT (datetime('now')),
    completed_at    TEXT,
    latency_seconds REAL,
    total_tokens    INTEGER,
    total_cost_usd  REAL
);

-- One row per multi-step run, recording the decomposition call.
-- The single_shot pipeline never writes to this table.
CREATE TABLE IF NOT EXISTS decompositions (
    decomp_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL UNIQUE REFERENCES runs(run_id),
    sub_questions TEXT,    -- JSON array of parsed sub-questions (empty array on failure)
    raw_response  TEXT,    -- unparsed LLM output, kept for debugging parse failures
    status        TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'complete' | 'failed'
    input_tokens  INTEGER,
    output_tokens INTEGER,
    created_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS steps (
    step_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(run_id),
    step_index       INTEGER NOT NULL,  -- 1-indexed position in the sub-question sequence
    sub_question     TEXT,
    retrieved_chunks TEXT,
    answer           TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'complete'
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    created_at       TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS final_answers (
    answer_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    final_answer TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'complete'
    created_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS evaluations (
    eval_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(run_id),
    correctness        TEXT,   -- within-question key-claim match as a fraction, e.g. '3/5' (denominator fixed per question)
    faithfulness_score TEXT,   -- fraction of claims grounded in retrieved chunks e.g. '3/4'
    evaluator_notes    TEXT,
    evaluated_at       TEXT    DEFAULT (datetime('now'))
);