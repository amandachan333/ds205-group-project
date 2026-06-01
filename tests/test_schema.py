"""
Schema integrity tests for db/schema.sql.

We apply the schema to a fresh in-memory SQLite database for each test and
assert that:
  - All five tables are created
  - The runs table has the columns we depend on elsewhere
  - Foreign key constraints are actually enforced (PRAGMA foreign_keys = ON)
  - A valid insert through the runs → questions FK works end-to-end

These are NOT data tests — they just lock in the schema contract so a careless
edit to schema.sql is caught before it silently breaks pipeline writes.
"""

from __future__ import annotations

import sqlite3

import pytest

# ---------------------------------------------------------------------------
# Helper fixture — fresh DB per test, no shared state
# ---------------------------------------------------------------------------


@pytest.fixture
def db(schema_sql):
    """
    Fresh in-memory SQLite with the project schema applied and FK checks ON.
    `schema_sql` is provided by conftest.py.
    """
    conn = sqlite3.connect(":memory:")
    # SQLite ships with foreign keys OFF by default — must be enabled per connection.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_schema_creates_all_expected_tables(db):
    expected = {"questions", "runs", "steps", "final_answers", "evaluations"}
    actual = {
        row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = expected - actual
    assert not missing, f"schema missing tables: {missing}"


def test_runs_table_has_required_columns(db):
    """Columns the pipeline code writes to must exist on the runs table."""
    cols = {
        row[1]  # PRAGMA table_info: column name is at index 1
        for row in db.execute("PRAGMA table_info(runs)").fetchall()
    }
    required = {
        "run_id",
        "question_id",
        "pipeline_type",
        "model_name",
        "status",
        "started_at",
        "completed_at",
        "latency_seconds",
        "total_tokens",
        "total_cost_usd",
    }
    missing = required - cols
    assert not missing, f"runs table missing columns: {missing}"


def test_foreign_key_violation_is_rejected(db):
    """Inserting a run referencing a non-existent question_id must fail."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO runs (question_id, pipeline_type) VALUES ('does_not_exist', 'single_shot')"
        )


def test_valid_run_insert_succeeds(db):
    """Inserting a question first and then a run referencing it should work."""
    db.execute(
        "INSERT INTO questions (question_id, question_text) VALUES (?, ?)",
        ("q1", "What is the emissions target?"),
    )
    db.execute(
        "INSERT INTO runs (question_id, pipeline_type) VALUES (?, ?)",
        ("q1", "single_shot"),
    )
    rows = db.execute("SELECT question_id, pipeline_type, status FROM runs").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "q1"
    assert rows[0][1] == "single_shot"
    # status has a DEFAULT of 'pending' — confirm it's applied
    assert rows[0][2] == "pending"
