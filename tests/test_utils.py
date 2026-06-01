"""
Unit tests for utils.py pure functions.

Covers:
  - derive_year       (4-digit 20xx year extraction from filename stems)
  - save_jsonl_atomic / load_jsonl  (round-trip + parent dir creation)
"""

from __future__ import annotations

import pytest

from utils import derive_year, load_jsonl, save_jsonl_atomic

# ---------------------------------------------------------------------------
# derive_year
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem, expected",
    [
        # Standard cases — year embedded in a typical filename pattern
        ("TNB_2023_carbon_performance", 2023),
        ("dewa_2021_sustainability_report", 2021),
        ("CenterPoint_2024_q1", 2024),
        # No 20xx year present
        ("annual_report_no_year", None),
        ("", None),
        # 1999 must NOT match — regex is anchored at 20\d{2}
        ("v2 report from 1999", None),
        # When multiple 20xx years appear, the FIRST one wins (re.search)
        ("combined_2020_and_2022_report", 2020),
    ],
)
def test_derive_year(stem, expected):
    assert derive_year(stem) == expected


# ---------------------------------------------------------------------------
# save_jsonl_atomic / load_jsonl
# ---------------------------------------------------------------------------


def test_jsonl_round_trip_preserves_records(tmp_path):
    """Records written by save_jsonl_atomic should load back identically."""
    records = [
        {"chunk_id": "a_001", "text": "hello", "page_number": 1},
        {"chunk_id": "a_002", "text": "world with unicode é ü 中文", "page_number": 2},
        {"chunk_id": "a_003", "metadata": {"nested": True, "list": [1, 2, 3]}},
    ]
    out_path = tmp_path / "out.jsonl"

    save_jsonl_atomic(records, out_path)
    loaded = load_jsonl(out_path)

    assert loaded == records


def test_save_jsonl_atomic_creates_parent_dirs(tmp_path):
    """Parent directories should be created automatically if they don't exist."""
    nested_path = tmp_path / "a" / "b" / "c" / "out.jsonl"

    save_jsonl_atomic([{"x": 1}], nested_path)

    assert nested_path.exists()
    # One record, one line
    assert nested_path.read_text(encoding="utf-8").count("\n") == 1


def test_save_jsonl_atomic_leaves_no_tmp_file(tmp_path):
    """The .tmp swap file should not remain after a successful write."""
    out_path = tmp_path / "out.jsonl"
    save_jsonl_atomic([{"a": 1}], out_path)

    assert out_path.exists()
    assert not (tmp_path / "out.tmp").exists()
