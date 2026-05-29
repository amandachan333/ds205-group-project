"""
db/database.py
 
All SQLite read/write functions for the benchmark pipeline.
This is the only file in the codebase that imports sqlite3 directly.
Schema is defined in db/schema.sql and executed once via init_db().
"""

from __future__ import annotations
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(db_path: str = "db/benchmark.db") -> sqlite3.Connection:
    """
    Open a connection to the SQLite database.
    Sets row_factory so rows are accessible by column name.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    """
    Execute schema.sql against the connection.
    Safe to call on every startup — all tables use CREATE TABLE IF NOT EXISTS.
    """
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)
    conn.commit()
    logger.info("Database initialised from %s", SCHEMA_PATH)


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------

def insert_question(
    conn: sqlite3.Connection,
    question_id: str,
    question_text: str,
    ground_truth: str | None = None,
    question_type: str | None = None,
) -> None:
    """
    Insert a question into the questions table.
    Uses INSERT OR IGNORE so re-running setup does not raise on duplicates.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO questions
            (question_id, question_text, ground_truth, question_type)
        VALUES (?, ?, ?, ?)
        """,
        (question_id, question_text, ground_truth, question_type),
    )
    conn.commit()
    logger.debug("Inserted question %s", question_id)


def get_all_questions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all rows from the questions table."""
    return conn.execute("SELECT * FROM questions").fetchall()


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def create_run(
    conn: sqlite3.Connection,
    question_id: str,
    pipeline_type: str,
    model_name: str,
) -> int:
    """
    Insert a new run record with status 'running'.
    Returns the auto-assigned run_id.
    """
    cursor = conn.execute(
        """
        INSERT INTO runs (question_id, pipeline_type, model_name, status)
        VALUES (?, ?, ?, 'running')
        """,
        (question_id, pipeline_type, model_name),
    )
    conn.commit()
    run_id: int = cursor.lastrowid
    logger.info(
        "Created run %d | question=%s pipeline=%s model=%s",
        run_id, question_id, pipeline_type, model_name,
    )
    return run_id


def complete_run(
    conn: sqlite3.Connection,
    run_id: int,
    latency_seconds: float,
    total_tokens: int,
    total_cost_usd: float,
) -> None:
    """Mark a run as complete and record final metrics."""
    conn.execute(
        """
        UPDATE runs
        SET status          = 'complete',
            completed_at    = datetime('now'),
            latency_seconds = ?,
            total_tokens    = ?,
            total_cost_usd  = ?
        WHERE run_id = ?
        """,
        (latency_seconds, total_tokens, total_cost_usd, run_id),
    )
    conn.commit()
    logger.info(
        "Completed run %d | %.1fs | %d tokens | $%.4f",
        run_id, latency_seconds, total_tokens, total_cost_usd,
    )


def fail_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Mark a run as failed, e.g. on an unhandled exception."""
    conn.execute(
        "UPDATE runs SET status = 'failed' WHERE run_id = ?",
        (run_id,),
    )
    conn.commit()
    logger.warning("Marked run %d as failed", run_id)


def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    """Return a single run row by run_id."""
    return conn.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()


def get_incomplete_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return all runs with status 'pending' or 'running'.
    Used to identify runs that can be resumed after a crash.
    """
    return conn.execute(
        "SELECT * FROM runs WHERE status IN ('pending', 'running')"
    ).fetchall()


# ---------------------------------------------------------------------------
# decompositions  (multi-step pipeline only)
# ---------------------------------------------------------------------------
 
def create_decomposition(conn: sqlite3.Connection, run_id: int) -> int:
    """
    Insert a pending decomposition row for a run.
    Returns the auto-assigned decomp_id.
    Called immediately after create_run, before the decomposition LLM call,
    so the run is resumable even if the decomposition call itself fails.
    """
    cursor = conn.execute(
        """
        INSERT INTO decompositions (run_id, status)
        VALUES (?, 'pending')
        """,
        (run_id,),
    )
    conn.commit()
    decomp_id: int = cursor.lastrowid
    logger.debug("Created pending decomposition %d for run %d", decomp_id, run_id)
    return decomp_id
 
 
def complete_decomposition(
    conn: sqlite3.Connection,
    decomp_id: int,
    sub_questions_json: str,
    raw_response: str,
    input_tokens: int,
    output_tokens: int,
    status: str = "complete",
) -> None:
    """
    Persist the result of a decomposition call.
 
    Pass status='complete' on a successful parse with one or more sub-questions.
    Pass status='failed' when the model output could not be parsed; in that
    case sub_questions_json should be '[]' and raw_response holds the full
    model output for debugging.
    """
    conn.execute(
        """
        UPDATE decompositions
        SET sub_questions = ?,
            raw_response  = ?,
            input_tokens  = ?,
            output_tokens = ?,
            status        = ?
        WHERE decomp_id = ?
        """,
        (sub_questions_json, raw_response, input_tokens, output_tokens, status, decomp_id),
    )
    conn.commit()
    logger.info(
        "Decomposition %d %s | %d+%d tokens",
        decomp_id, status, input_tokens, output_tokens,
    )
 
 
def get_decomposition(
    conn: sqlite3.Connection, run_id: int
) -> sqlite3.Row | None:
    """Return the decomposition row for a run, or None if none exists yet."""
    return conn.execute(
        "SELECT * FROM decompositions WHERE run_id = ?", (run_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# steps  (multi-step pipeline only)
# ---------------------------------------------------------------------------

def insert_step(
    conn: sqlite3.Connection,
    run_id: int,
    step_index: int,
    sub_question: str,
) -> int:
    """
    Insert a pending step record immediately after decomposition.
    Returns the auto-assigned step_id.
    All sub-question steps are written before any retrieval or generation
    begins, so the full decomposition is visible in the database upfront.
    """
    cursor = conn.execute(
        """
        INSERT INTO steps (run_id, step_index, sub_question, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (run_id, step_index, sub_question),
    )
    conn.commit()
    step_id: int = cursor.lastrowid
    logger.debug("Inserted step %d (index %d) for run %d", step_id, step_index, run_id)
    return step_id


def complete_step(
    conn: sqlite3.Connection,
    step_id: int,
    answer: str,
    retrieved_chunks: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """
    Persist the result of a completed sub-question step.
    Must be called before moving to the next step in the pipeline loop.
    """
    conn.execute(
        """
        UPDATE steps
        SET answer           = ?,
            retrieved_chunks = ?,
            input_tokens     = ?,
            output_tokens    = ?,
            status           = 'complete'
        WHERE step_id = ?
        """,
        (answer, retrieved_chunks, input_tokens, output_tokens, step_id),
    )
    conn.commit()
    logger.debug("Completed step %d | %d+%d tokens", step_id, input_tokens, output_tokens)


def get_pending_steps(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """
    Return all pending steps for a run, ordered by step_index.
    Used to resume a run that was interrupted mid-way.
    """
    return conn.execute(
        """
        SELECT * FROM steps
        WHERE run_id = ? AND status = 'pending'
        ORDER BY step_index ASC
        """,
        (run_id,),
    ).fetchall()


def get_completed_steps(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """
    Return all completed steps for a run, ordered by step_index.
    Used by the assembler to build the findings block for the assembly prompt.
    """
    return conn.execute(
        """
        SELECT * FROM steps
        WHERE run_id = ? AND status = 'complete'
        ORDER BY step_index ASC
        """,
        (run_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# final_answers
# ---------------------------------------------------------------------------

def insert_final_answer(
    conn: sqlite3.Connection,
    run_id: int,
    final_answer: str,
) -> int:
    """
    Persist the final assembled answer for a run.
    Returns the auto-assigned answer_id.
    """
    cursor = conn.execute(
        """
        INSERT INTO final_answers (run_id, final_answer, status)
        VALUES (?, ?, 'complete')
        """,
        (run_id, final_answer),
    )
    conn.commit()
    answer_id: int = cursor.lastrowid
    logger.info("Inserted final answer %d for run %d", answer_id, run_id)
    return answer_id


def get_final_answer(
    conn: sqlite3.Connection, run_id: int
) -> sqlite3.Row | None:
    """Return the final answer row for a run."""
    return conn.execute(
        "SELECT * FROM final_answers WHERE run_id = ?", (run_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# evaluations
# ---------------------------------------------------------------------------

def insert_evaluation(
    conn: sqlite3.Connection,
    run_id: int,
    correctness: str,
    faithfulness_score: str,
    evaluator_notes: str | None = None,
) -> int:
    """
    Record a manual evaluation score for a run's final answer.
    correctness must be one of: 'correct', 'partial', 'incorrect'.
    faithfulness_score is a string fraction e.g. '3/4' or a decimal '0.75'.
    Returns the auto-assigned eval_id.
    """
    cursor = conn.execute(
        """
        INSERT INTO evaluations
            (run_id, correctness, faithfulness_score, evaluator_notes)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, correctness, faithfulness_score, evaluator_notes),
    )
    conn.commit()
    eval_id: int = cursor.lastrowid
    logger.info(
        "Inserted evaluation %d for run %d | %s | faithfulness=%s",
        eval_id, run_id, correctness, faithfulness_score,
    )
    return eval_id


# ---------------------------------------------------------------------------
# Benchmark summary queries
# ---------------------------------------------------------------------------

def get_run_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return a joined summary of all complete runs with their final answers
    and evaluation scores. Used by the evaluation harness and report generator.
    """
    return conn.execute(
        """
        SELECT
            r.run_id,
            r.question_id,
            r.pipeline_type,
            r.model_name,
            r.latency_seconds,
            r.total_tokens,
            r.total_cost_usd,
            f.final_answer,
            e.correctness,
            e.faithfulness_score,
            e.evaluator_notes
        FROM runs r
        LEFT JOIN final_answers f ON f.run_id = r.run_id
        LEFT JOIN evaluations   e ON e.run_id = r.run_id
        WHERE r.status = 'complete'
        ORDER BY r.question_id, r.pipeline_type
        """
    ).fetchall()


def get_token_spend_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return total token usage and cost grouped by pipeline type.
    Used to produce the token spend summary required by the project brief.
    """
    return conn.execute(
        """
        SELECT
            pipeline_type,
            COUNT(*)            AS total_runs,
            SUM(total_tokens)   AS total_tokens,
            SUM(total_cost_usd) AS total_cost_usd,
            AVG(latency_seconds) AS avg_latency_seconds
        FROM runs
        WHERE status = 'complete'
        GROUP BY pipeline_type
        """
    ).fetchall()