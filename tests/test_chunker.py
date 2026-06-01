"""
Unit tests for chunker pure-function logic in pipelines/chunk.py.

Each test exercises one helper in isolation, using small hand-crafted inputs.
The `chunk_module` fixture (defined in conftest.py) loads the module via
importlib so we sidestep any clash with Python's deprecated `chunk` stdlib
module.

Covers:
  - _fix_co2e               (tCO,e → tCO2e regex normalisation)
  - _is_boilerplate         (forward-looking statement detection)
  - _is_severely_corrupted  (OCR corruption heuristic)
  - _extract_table_header   (pipe-delimited table header extraction)
  - _merge_short_chunks     (merge policy + table exemption)
  - _split_long_chunks      (table-row vs sentence boundary splitting)
  - _find_boilerplate_page  (last-N-pages window detection)
  - _remove_gibberish       (drop + clean pipeline)

Deliberately does NOT cover:
  - _chunk_by_sentences end-to-end  (requires NLTK punkt model)
  - process_file                    (file I/O orchestrator — smoke-tested by
                                     real pipeline runs, not unit tests)
"""

from __future__ import annotations

import pytest

# ===========================================================================
# _fix_co2e — tCO,e → tCO2e normalisation
# ===========================================================================


@pytest.mark.parametrize(
    "text, expected",
    [
        # Standard forms with SI prefixes
        ("Emissions of 14.7 MtCO,e in 2023", "Emissions of 14.7 MtCO2e in 2023"),
        ("Total scope 1: 0.5 tco,e", "Total scope 1: 0.5 tCO2e"),
        ("Intensity: 3.2 ktCO,eq per MWh", "Intensity: 3.2 ktCO2e per MWh"),
        ("9 GtCO,e by 2050", "9 GtCO2e by 2050"),
        # No SI prefix
        ("emitted 100 tCO,e", "emitted 100 tCO2e"),
        # Negative cases — must NOT match
        ("This, e.g. is a normal comma", "This, e.g. is a normal comma"),
        ("Visit https://example.co,en/page", "Visit https://example.co,en/page"),
        ("", ""),
    ],
)
def test_fix_co2e_normalises_variants(chunk_module, text, expected):
    assert chunk_module._fix_co2e(text) == expected


# ===========================================================================
# _is_boilerplate
# ===========================================================================


@pytest.mark.parametrize(
    "text, expected",
    [
        # Exact phrase, lowercase
        ("forward-looking statements: this report contains projections.", True),
        # Case insensitive
        ("FORWARD-LOOKING STATEMENTS as defined by the Act.", True),
        # Substring match anywhere in the text
        ("Note: forward-looking statements appear in section 3.", True),
        # Negative cases
        ("Forward-thinking strategies for the future.", False),
        ("", False),
        ("Normal text about emissions and net-zero targets.", False),
    ],
)
def test_is_boilerplate(chunk_module, text, expected):
    assert chunk_module._is_boilerplate(text) is expected


# ===========================================================================
# _is_severely_corrupted
# ===========================================================================


@pytest.mark.parametrize(
    "text, expected, reason",
    [
        ("", True, "empty"),
        ("short", True, "below 10 chars"),
        ("This is a perfectly normal sentence here.", False, "clean text"),
        ("(cid:1) (cid:2) (cid:3) bad", True, "3/4 words are cid markers"),
        (
            "Normal text with one (cid:42) marker in it.",
            False,
            "1/8 words is cid — below 30% threshold",
        ),
        ("Lots of \ufffd\ufffd\ufffd\ufffd\ufffd\ufffd", True, "6 replacement chars"),
    ],
)
def test_is_severely_corrupted(chunk_module, text, expected, reason):
    assert chunk_module._is_severely_corrupted(text) is expected, f"failed for: {reason}"


# ===========================================================================
# _extract_table_header
# ===========================================================================


def test_extract_table_header_with_caption_rows(chunk_module):
    """Leading no-pipe rows are caption/title; first pipe row defines columns."""
    rows = [
        "Table 1: Emissions Summary",  # caption (no pipe)
        "(in tCO2e)",  # caption (no pipe)
        "Year: 2020 | Scope 1: 100 | Scope 2: 50",  # first data row → columns
        "Year: 2021 | Scope 1: 120 | Scope 2: 60",  # subsequent data row
    ]
    header = chunk_module._extract_table_header(rows)

    # Captions and first data row must be included
    assert "Table 1: Emissions Summary" in header
    assert "(in tCO2e)" in header
    assert "Year: 2020" in header
    # Second data row must NOT be in the header
    assert "Year: 2021" not in header


def test_extract_table_header_no_caption(chunk_module):
    """When there are no caption rows, only the first data row is the header."""
    rows = [
        "Year | Emissions",
        "2020 | 100",
        "2021 | 120",
    ]
    header = chunk_module._extract_table_header(rows)
    assert "Year | Emissions" in header
    # Data rows after the first should not appear
    assert "2020 | 100" not in header
    assert "2021 | 120" not in header


# ===========================================================================
# _merge_short_chunks
# ===========================================================================


def test_merge_short_chunks_merges_normal_text(chunk_module):
    """A short narrative chunk should be merged into the following one."""
    chunks = [
        {
            "text": "Tiny.",
            "metadata": {"element_types": ["NarrativeText"], "paragraph_numbers": [1]},
        },
        {
            "text": "A second chunk with more content that easily exceeds the minimum.",
            "metadata": {"element_types": ["NarrativeText"], "paragraph_numbers": [2]},
        },
    ]
    result = chunk_module._merge_short_chunks(chunks, min_length=20)

    assert len(result) == 1
    assert "Tiny." in result[0]["text"]
    assert "A second chunk" in result[0]["text"]


def test_merge_short_chunks_never_merges_tables(chunk_module):
    """Short Table chunks must remain standalone — even a 2-row table is a unit."""
    chunks = [
        {
            "text": "A | B",  # short, but a Table
            "metadata": {"element_types": ["Table"], "paragraph_numbers": [1]},
        },
        {
            "text": "Following paragraph with plenty of narrative text after the table.",
            "metadata": {"element_types": ["NarrativeText"], "paragraph_numbers": [2]},
        },
    ]
    result = chunk_module._merge_short_chunks(chunks, min_length=20)

    assert len(result) == 2  # not merged
    assert result[0]["text"] == "A | B"


# ===========================================================================
# _split_long_chunks
# ===========================================================================


def test_split_long_chunks_short_chunk_unchanged(chunk_module):
    """A chunk shorter than max_length passes through untouched."""
    chunks = [{"text": "short", "metadata": {"element_types": ["NarrativeText"]}}]
    result = chunk_module._split_long_chunks(chunks, max_length=100)
    assert len(result) == 1
    assert result[0]["text"] == "short"


def test_split_long_chunks_table_prepends_header_to_continuation(chunk_module):
    """When a Table chunk is too long, continuation sub-chunks get the header prepended."""
    rows = [
        "Emissions Table",  # caption
        "Year: 2020 | Emissions: 100 tCO2e",  # first data row → header
        "Year: 2021 | Emissions: 120 tCO2e",
        "Year: 2022 | Emissions: 130 tCO2e",
        "Year: 2023 | Emissions: 140 tCO2e",
        "Year: 2024 | Emissions: 150 tCO2e",
    ]
    text = "\n".join(rows)
    chunks = [{"text": text, "metadata": {"element_types": ["Table"]}}]

    # Small max_length forces at least one split
    result = chunk_module._split_long_chunks(chunks, max_length=80)

    assert len(result) > 1, "expected the long table to be split"
    # First sub-chunk: not a continuation
    assert result[0]["metadata"].get("table_continuation") is False
    # Subsequent sub-chunks: marked as continuation AND contain the header
    for cont in result[1:]:
        assert cont["metadata"].get("table_continuation") is True
        # Header rows must appear at the top of every continuation
        assert "Emissions Table" in cont["text"]
        assert "Year: 2020" in cont["text"]


def test_split_long_chunks_text_splits_on_sentence_boundaries(chunk_module):
    """Non-table chunks split on sentence boundaries, never mid-sentence."""
    sentences = [
        "First sentence here.",
        "Second sentence follows.",
        "Third sentence continues.",
        "Fourth sentence finishes.",
    ]
    # Pre-tokenise so the chunker doesn't have to call NLTK
    chunks = [
        {
            "text": " ".join(sentences),
            "sentences": sentences,
            "metadata": {"element_types": ["NarrativeText"]},
        }
    ]

    # max_length ~ 45 chars allows ~2 sentences per chunk
    result = chunk_module._split_long_chunks(chunks, max_length=45)

    assert len(result) > 1, "expected the long text to be split"
    # Every resulting chunk must end at a sentence boundary (no mid-sentence cuts)
    for r in result:
        assert r["text"].endswith("."), f"split mid-sentence: {r['text']!r}"


# ===========================================================================
# _find_boilerplate_page
# ===========================================================================


def test_find_boilerplate_page_detects_within_last_window(chunk_module):
    """Boilerplate in the last BOILERPLATE_PAGE_WINDOW pages should be detected."""
    chunks = [
        {"text": "Body content on page 1.", "page_number": 1},
        {"text": "More body content on page 2.", "page_number": 2},
        # Last page is 50 → window covers pages 46-50.  Page 48 is in-window.
        {
            "text": "Forward-looking statements: this report contains projections.",
            "page_number": 48,
        },
        {"text": "Additional disclaimers continue here.", "page_number": 49},
        {"text": "Final page content.", "page_number": 50},
    ]
    assert chunk_module._find_boilerplate_page(chunks) == 48


def test_find_boilerplate_page_ignores_matches_outside_window(chunk_module):
    """The page-window guard prevents false matches in the body of long reports."""
    chunks = [
        {"text": "Body content on page 1.", "page_number": 1},
        # Page 5 contains the phrase but is far outside the last-5 window (46-50)
        {"text": "Forward-looking statements appear in the strategy discussion.", "page_number": 5},
        {"text": "More body content here.", "page_number": 25},
        {"text": "Final page content.", "page_number": 50},
    ]
    assert chunk_module._find_boilerplate_page(chunks) is None


def test_find_boilerplate_page_returns_none_on_empty(chunk_module):
    assert chunk_module._find_boilerplate_page([]) is None


# ===========================================================================
# _remove_gibberish
# ===========================================================================


def test_remove_gibberish_drops_corrupt_and_applies_cleaning(chunk_module):
    """Drops severely-corrupt chunks AND applies CID-strip + tCO,e fix on survivors."""
    chunks = [
        {"text": "Normal text with sufficient length to keep around."},
        {"text": "x"},  # too short — dropped
        {"text": "(cid:1) (cid:2) (cid:3) bad"},  # CID-heavy — dropped
        {"text": "Emissions of 14.7 MtCO,e per year. (cid:42)"},  # kept, both cleanings applied
    ]
    result = chunk_module._remove_gibberish(chunks)
    surviving_texts = [c["text"] for c in result]

    # Two chunks survive
    assert len(result) == 2
    # CID artefacts are stripped from the surviving chunk
    assert not any("(cid:" in t for t in surviving_texts)
    # tCO,e normalisation is applied
    assert any("MtCO2e" in t for t in surviving_texts)
    assert not any("MtCO,e" in t for t in surviving_texts)
