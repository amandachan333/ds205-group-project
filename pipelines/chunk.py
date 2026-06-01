"""
Sentence-based chunking pipeline for Project C.

Reads:  data/extracted/<company>/<company>_<year>_elements.jsonl
Writes: data/chunked/<company>/<company>_<year>_chunks.jsonl

Chunking strategy is adapted from the TPI CLEAR reference
implementation (chunker.py / cleaner.py).  Design rationale and parameter
choices are documented in DECISIONS.md.

Usage:
    python pipelines/chunk.py                        # all companies
    python pipelines/chunk.py --company TNB          # one company
    python pipelines/chunk.py --force                # re-chunk existing
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any

import nltk
from nltk.tokenize import sent_tokenize

from config import (
    CHUNKED_DIR,
    EXTRACTED_DIR,
    LOG_DIR,
    MAX_CHUNK_LENGTH,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_LENGTH,
    SENTENCE_OVERLAP,
)
from utils import derive_year, ensure_stage_dirs, load_jsonl, save_jsonl_atomic

# ---------------------------------------------------------------------------
# Logging – writes to logs/chunk.log AND stderr
# ---------------------------------------------------------------------------
ensure_stage_dirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "chunk.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# unstructured element types emitted as standalone chunks (never sentence-split).
# "Table" is included because Gemini extracts tables as single pipe-delimited
# text blocks; splitting mid-table destroys row/column context.
_HEADING_TYPES: frozenset[str] = frozenset({"Title", "Header", "Heading", "SubHeading", "Table"})

# Boilerplate patterns that appear verbatim across many company reports
# (e.g. legal cautionary statements).  Chunks matching any of these are
# dropped during cleaning
_BOILERPLATE_PATTERNS: list[str] = [
    "forward-looking statements",
]

BOILERPLATE_PAGE_WINDOW: int = 5  # only match within last N pages of the doc


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _ensure_nltk_punkt() -> None:
    """Download the punkt tokeniser data if it is not already present."""
    try:
        sent_tokenize("Warm-up sentence.")
    except LookupError:
        logger.info("Downloading NLTK punkt tokeniser data…")
        nltk.download("punkt", quiet=True)
        nltk.download("punkt_tab", quiet=True)


# ---------------------------------------------------------------------------
# Element conversion
# ---------------------------------------------------------------------------


def _convert_element(
    record: dict[str, Any],
    company: str,
    document_title: str,
    page_para_counter: dict[int, int],
) -> dict[str, Any]:
    """
    Map one record from extract.py JSONL format into the
    internal element dict expected by the chunking functions below.

    Extracted record keys: text, element_type, element_id, parent_id,
                           page_number, filename, languages, category_depth

    Internal element keys: type, text, metadata{page_number,
                           paragraph_number, filename, company,
                           document_title}

    Paragraph numbers are assigned as page-relative counters because
    extract.py does not emit them.
    """
    page: int = record.get("page_number") or 0
    page_para_counter.setdefault(page, 0)
    page_para_counter[page] += 1

    return {
        "type": record.get("element_type", "NarrativeText"),
        "text": record.get("text", "").strip(),
        "metadata": {
            "page_number": page,
            "paragraph_number": page_para_counter[page],
            "filename": record.get("filename") or "",
            "company": company,
            "document_title": document_title,
        },
    }


# ---------------------------------------------------------------------------
# Cleaner helpers  (adapted from TPI cleaner.py)
# ---------------------------------------------------------------------------


def _merge_short_chunks(chunks: list[dict], min_length: int = MIN_CHUNK_LENGTH) -> list[dict]:
    """
    Merge a chunk into the one before it when the preceding chunk is
    shorter than *min_length* characters.

    Table chunks are never merged regardless of length — a two-row table
    is still a self-contained unit and should not be glued to the
    following paragraph.  Mirrors TPI merge_short_chunks.
    """
    if not chunks:
        return chunks

    merged: list[dict] = []
    current: dict | None = None

    for chunk in chunks:
        if current is None:
            current = chunk
            continue

        current_is_table = "Table" in current.get("metadata", {}).get("element_types", [])

        if len(current["text"]) < min_length and not current_is_table:
            current["text"] = current["text"] + " " + chunk["text"]
            meta_c = current.get("metadata", {})
            meta_n = chunk.get("metadata", {})
            for key in ("element_types", "paragraph_numbers"):
                if key in meta_n and key in meta_c:
                    for val in meta_n[key]:
                        if val not in meta_c[key]:
                            meta_c[key].append(val)
        else:
            merged.append(current)
            current = chunk

    if current is not None:
        merged.append(current)
    return merged


def _extract_table_header(rows: list[str]) -> str:
    """
    Extract header context from a list of table rows, for use when a
    large table must be split across multiple chunks.

    In Gemini's pipe-delimited output format:
    - Rows with no '|' are section labels / captions (full-width markers).
    - The first row with '|' encodes the column structure as 'Col: value'.

    Returns a string to prepend to sub-chunks 2, 3, ... so each one
    carries enough context to be independently interpretable.
    """
    header_lines: list[str] = []

    # Collect leading label/caption rows (no pipe = not a data row)
    first_data_idx = 0
    for i, row in enumerate(rows):
        if "|" not in row:
            header_lines.append(row)
            first_data_idx = i + 1
        else:
            break

    # Include the first data row — its keys name the columns
    if first_data_idx < len(rows):
        header_lines.append(rows[first_data_idx])

    return "\n".join(header_lines)


def _split_long_chunks(chunks: list[dict], max_length: int = MAX_CHUNK_LENGTH) -> list[dict]:
    """
    Hard-split any chunk longer than *max_length* chars.

    - Regular text: split on sentence boundaries (original behaviour).
    - Table chunks: split on row boundaries (newlines) and prepend the
      table header to every continuation sub-chunk so each one is
      independently interpretable without the rows that came before it.
    """
    result: list[dict] = []
    counter = 0

    for chunk in chunks:
        text = chunk.get("text", "")
        if len(text) <= max_length:
            result.append(chunk)
            continue

        is_table = "Table" in chunk.get("metadata", {}).get("element_types", [])

        if is_table:
            rows = [r for r in text.split("\n") if r.strip()]
            # unstructured sometimes misclassifies dense narrative blocks as
            # "Table".  If the text has no pipe characters it is not a
            # Gemini-extracted table — fall back to sentence splitting.
            has_pipe_format = any("|" in r for r in rows)
            if not has_pipe_format:
                is_table = False

        if is_table:
            rows = [r for r in text.split("\n") if r.strip()]
            header_text = _extract_table_header(rows)

            cur_rows: list[str] = []
            cur_len: int = 0
            is_continuation: bool = False  # True for sub-chunks 2, 3, …

            for row in rows:
                row_len = len(row) + 1  # +1 for the joining newline
                if cur_rows and cur_len + row_len > max_length:
                    result.append(
                        {
                            "id": f"_split_{counter}",
                            "text": "\n".join(cur_rows),
                            "metadata": {
                                **chunk.get("metadata", {}).copy(),
                                "table_continuation": is_continuation,
                            },
                        }
                    )
                    counter += 1
                    # Prepend header rows to continuation sub-chunks
                    is_continuation = True
                    cur_rows = [*header_text.split("\n"), row] if header_text else [row]
                    cur_len = len(header_text) + 1 + row_len if header_text else row_len
                else:
                    cur_rows.append(row)
                    cur_len += row_len

            if cur_rows:
                result.append(
                    {
                        "id": f"_split_{counter}",
                        "text": "\n".join(cur_rows),
                        "metadata": {
                            **chunk.get("metadata", {}).copy(),
                            "table_continuation": is_continuation,
                        },
                    }
                )
                counter += 1

        else:
            # Original sentence-boundary split for non-table chunks
            sentences: list[str] = chunk.get("sentences") or sent_tokenize(text)
            cur_text = ""
            cur_sentences: list[str] = []

            for sentence in sentences:
                would_exceed = cur_text and len(cur_text) + 1 + len(sentence) > max_length
                if would_exceed:
                    result.append(
                        {
                            "id": f"_split_{counter}",
                            "text": cur_text.strip(),
                            "sentences": cur_sentences,
                            "metadata": chunk.get("metadata", {}).copy(),
                        }
                    )
                    counter += 1
                    cur_text = sentence
                    cur_sentences = [sentence]
                else:
                    cur_text = (cur_text + " " + sentence).strip() if cur_text else sentence
                    cur_sentences.append(sentence)

            if cur_text:
                result.append(
                    {
                        "id": f"_split_{counter}",
                        "text": cur_text.strip(),
                        "sentences": cur_sentences,
                        "metadata": chunk.get("metadata", {}).copy(),
                    }
                )
                counter += 1

    return result


def _is_severely_corrupted(text: str) -> bool:
    """
    Return True when text appears to be corrupted OCR output.
    Simplified relative to TPI because our extractor uses Gemini VLM
    for tables and unstructured hi_res for text, giving cleaner output.
    """
    if not text or len(text) < 10:
        return True
    cid_count = len(re.findall(r"\(cid:\d+\)", text))
    word_count = max(len(text.split()), 1)
    if cid_count / word_count > 0.3:
        return True
    if text.count("\ufffd") > 5:  # Unicode replacement chars
        return True
    return False


def _fix_co2e(text: str) -> str:
    """
    Normalise tCO,e / tco,e -> tCO2e.

    unstructured renders the subscript '2' in CO2e as a comma when it
    fails to parse the glyph, producing artefacts like:
        '14.7 MtCO,e'  '0.5 tco,e'  '3.2 ktCO,e'

    Matches optional SI prefix (k/M/G) + t/T, then CO/co, comma, e/eq.
    """
    return re.sub(
        r"\b([kKmMgG]?[tT])[Cc][Oo],[Ee][Qq]?\b",
        lambda m: m.group(1) + "CO2e",
        text,
    )


def _is_boilerplate(text: str) -> bool:
    """
    Return True when *text* matches a known boilerplate pattern.
    """
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in _BOILERPLATE_PATTERNS)


def _remove_gibberish(chunks: list[dict]) -> list[dict]:
    """
    Drop severely corrupted chunks; lightly clean the rest.
    Mirrors TPI remove_gibberish but without heavy Latin-script heuristics
    that are unnecessary for our corpus quality.

    Cleaning steps applied to each surviving chunk:
      1. Strip CID encoding artefacts  e.g. (cid:42)
      2. Remove non-printable control characters
      3. Normalise tCO,e -> tCO2e  (subscript-2 rendered as comma)
      4. Collapse whitespace
    """
    clean: list[dict] = []
    for chunk in chunks:
        text = chunk.get("text", "")
        if _is_severely_corrupted(text):
            logger.debug("Dropping corrupted chunk (first 80 chars): %s", text[:80])
            continue
        text = re.sub(r"\(cid:\d+\)", " ", text)
        text = re.sub(r"[\x00-\x08\x0e-\x1f\x7f]", " ", text)
        text = _fix_co2e(text)
        text = " ".join(text.split())
        if len(text.strip()) > 5:
            chunk["text"] = text.strip()
            clean.append(chunk)
    return clean


# ---------------------------------------------------------------------------
# Core sentence-based chunker  (adapted from TPI DocChunker.chunk_document_by_sentences)
# ---------------------------------------------------------------------------


def _chunk_by_sentences(
    elements: list[dict[str, Any]],
    max_chunk_size: int = MAX_CHUNK_SIZE,
    overlap: int = SENTENCE_OVERLAP,
) -> list[dict[str, Any]]:
    """
    Split *elements* into overlapping sentence-window chunks.

    Headings become standalone single-sentence chunks so that section
    boundaries are preserved.  All other elements accumulate sentences
    until *max_chunk_size* is reached, then a new chunk begins with
    *overlap* sentences carried forward.

    Mirrors the logic in TPI's DocChunker.chunk_document_by_sentences
    but adapted to our element format.
    """
    chunks: list[dict] = []
    cur_text: str = ""
    cur_sentences: list[str] = []
    cur_meta: dict = {}
    cur_element_types: set[str] = set()
    chunk_idx: int = 0

    for element in elements:
        el_type: str = element.get("type", "NarrativeText")
        el_text: str = element.get("text", "")
        el_meta: dict = element.get("metadata", {})

        if not el_text.strip():
            continue

        # ── Headings: flush current chunk, then emit standalone heading chunk ──
        if el_type in _HEADING_TYPES:
            if cur_sentences:
                chunks.append(
                    {
                        "id": f"chunk_{chunk_idx}",
                        "text": cur_text.strip(),
                        "sentences": cur_sentences,
                        "metadata": cur_meta,
                    }
                )
                chunk_idx += 1
                cur_text = ""
                cur_sentences = []
                cur_meta = {}
                cur_element_types = set()

            chunks.append(
                {
                    "id": f"chunk_{chunk_idx}",
                    "text": el_text.strip(),
                    "sentences": [el_text.strip()],
                    "metadata": {
                        "element_types": [el_type],
                        "page_number": el_meta.get("page_number", 0),
                        "paragraph_numbers": [el_meta.get("paragraph_number")],
                        "filename": el_meta.get("filename", ""),
                        "company": el_meta.get("company", ""),
                        "document_title": el_meta.get("document_title", ""),
                    },
                }
            )
            chunk_idx += 1
            continue

        # ── Regular text: accumulate sentences ──
        if not cur_sentences:
            # Start a fresh chunk
            cur_meta = {
                "element_types": [el_type],
                "page_number": el_meta.get("page_number", 0),
                "paragraph_numbers": [el_meta.get("paragraph_number")],
                "filename": el_meta.get("filename", ""),
                "company": el_meta.get("company", ""),
                "document_title": el_meta.get("document_title", ""),
            }
            cur_element_types = {el_type}
        else:
            # Merge element-type and paragraph-number metadata
            cur_element_types.add(el_type)
            if el_type not in cur_meta["element_types"]:
                cur_meta["element_types"].append(el_type)
            pn = el_meta.get("paragraph_number")
            if pn is not None and pn not in cur_meta["paragraph_numbers"]:
                cur_meta["paragraph_numbers"].append(pn)

        for sentence in sent_tokenize(el_text):
            sentence = sentence.strip()
            if not sentence:
                continue

            cur_text = (cur_text + " " + sentence).strip() if cur_text else sentence
            cur_sentences.append(sentence)

            # Emit chunk when size limit is reached
            if len(cur_text) >= max_chunk_size and len(cur_sentences) > overlap + 1:
                chunks.append(
                    {
                        "id": f"chunk_{chunk_idx}",
                        "text": cur_text.strip(),
                        "sentences": list(cur_sentences),
                        "metadata": cur_meta,
                    }
                )
                chunk_idx += 1

                # Carry overlap sentences into the new chunk
                cur_sentences = cur_sentences[-overlap:] if overlap > 0 else []
                cur_text = " ".join(cur_sentences)
                cur_meta = {
                    "element_types": [el_type],
                    "page_number": el_meta.get("page_number", cur_meta.get("page_number", 0)),
                    "paragraph_numbers": [el_meta.get("paragraph_number")],
                    "filename": cur_meta.get("filename", ""),
                    "company": cur_meta.get("company", ""),
                    "document_title": cur_meta.get("document_title", ""),
                }
                cur_element_types = {el_type}

    # Flush any remaining sentences
    if cur_sentences:
        chunks.append(
            {
                "id": f"chunk_{chunk_idx}",
                "text": cur_text.strip(),
                "sentences": cur_sentences,
                "metadata": cur_meta,
            }
        )

    return chunks


def _clean_chunks(
    chunks: list[dict],
    min_length: int = MIN_CHUNK_LENGTH,
    max_length: int = MAX_CHUNK_LENGTH,
) -> list[dict]:
    """Apply merge → split → gibberish-removal in sequence."""
    chunks = _merge_short_chunks(chunks, min_length)
    chunks = _split_long_chunks(chunks, max_length)
    chunks = _remove_gibberish(chunks)
    return chunks


# ---------------------------------------------------------------------------
# File-level processing
# ---------------------------------------------------------------------------
def _find_boilerplate_page(chunks: list[dict]) -> int | None:
    """
    Return the first page number where boilerplate content begins, or None.

    Only considers chunks within the last BOILERPLATE_PAGE_WINDOW pages of
    the document.  This prevents false matches on legitimate mentions of
    "forward-looking statements" in the body of the report — those phrases
    can appear in strategy sections but the legal disclaimer is always at
    the very end.
    """
    if not chunks:
        return None
    last_page = max(c.get("page_number") or 0 for c in chunks)
    window_start = last_page - BOILERPLATE_PAGE_WINDOW + 1
    for chunk in chunks:
        page = chunk.get("page_number") or 0
        if page >= window_start and _is_boilerplate(chunk.get("text", "")):
            return page
    return None


def process_file(
    jsonl_path: Path,
    output_dir: Path,
    force: bool = False,
) -> Path | None:
    """
    Chunk one extracted JSONL file and write the result to *output_dir*.

    Returns the output path on success, None if the file was skipped.
    """
    company: str = jsonl_path.parent.name
    # Remove the "_elements" suffix that extract.py appends
    doc_id: str = re.sub(r"_elements$", "", jsonl_path.stem)
    year: int | None = derive_year(doc_id)

    out_path: Path = output_dir / company / f"{doc_id}_chunks.jsonl"

    if out_path.exists() and not force:
        logger.info(
            "SKIP  %s/%s  (output exists; use --force to reprocess)",
            company,
            jsonl_path.name,
        )
        return None

    logger.info("Processing  %s / %s", company, jsonl_path.name)

    # Read extracted elements
    raw_records: list[dict] = load_jsonl(jsonl_path)

    if not raw_records:
        logger.warning("No records found in %s — skipping", jsonl_path)
        return None

    # Convert to internal element format
    page_para_counter: dict[int, int] = {}
    elements: list[dict] = [
        _convert_element(r, company, doc_id, page_para_counter)
        for r in raw_records
        if r.get("text", "").strip()
    ]

    logger.info("  Input elements: %d", len(elements))

    # Chunk
    raw_chunks = _chunk_by_sentences(elements)
    logger.info("  Raw chunks:     %d", len(raw_chunks))

    # Clean
    clean_chunks = _clean_chunks(raw_chunks)
    logger.info("  Clean chunks:   %d", len(clean_chunks))

    if not clean_chunks:
        logger.warning("No chunks produced for %s — skipping output", jsonl_path.name)
        return None

    # Build output records with stable, globally unique chunk IDs
    output_records: list[dict] = [
        {
            "chunk_id": f"{doc_id}_chunk_{idx:04d}",
            "company": company,
            "document_id": doc_id,
            "year": year,
            "chunk_index": idx,
            "page_number": chunk.get("metadata", {}).get("page_number"),
            "text": chunk["text"],
            "metadata": chunk.get("metadata", {}),
        }
        for idx, chunk in enumerate(clean_chunks)
    ]

    # Drop all chunks from the first boilerplate page onwards.
    # The trigger page is only detected within the last BOILERPLATE_PAGE_WINDOW
    # pages, so earlier legitimate mentions are not affected.
    boilerplate_page = _find_boilerplate_page(output_records)
    if boilerplate_page is not None:
        before = len(output_records)
        output_records = [
            r for r in output_records if (r.get("page_number") or 0) < boilerplate_page
        ]
        logger.info(
            "  Dropped %d boilerplate chunk(s) from page %d onwards",
            before - len(output_records),
            boilerplate_page,
        )

    if not output_records:
        logger.warning("All chunks dropped as boilerplate for %s", jsonl_path.name)
        return None

    save_jsonl_atomic(output_records, out_path)
    logger.info("  Written → %s  (%d chunks)", out_path, len(output_records))
    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sentence-based chunking for Project C (DS205).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--extracted-dir",
        type=Path,
        default=EXTRACTED_DIR,
        help="Root directory containing extracted JSONL files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=CHUNKED_DIR,
        help="Root directory for chunked JSONL output.",
    )
    parser.add_argument(
        "--company",
        type=str,
        default=None,
        help="Process only this company subfolder (default: all companies).",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Re-chunk files even if output already exists.",
    )
    args = parser.parse_args()

    _ensure_nltk_punkt()

    extracted_dir: Path = args.extracted_dir
    if not extracted_dir.exists():
        logger.error("Extracted directory not found: %s", extracted_dir)
        return

    company_dirs: list[Path] = (
        [extracted_dir / args.company]
        if args.company
        else sorted(p for p in extracted_dir.iterdir() if p.is_dir())
    )

    total_files = 0
    total_chunks = 0

    for company_dir in company_dirs:
        if not company_dir.is_dir():
            logger.warning("Directory not found: %s", company_dir)
            continue
        for jsonl_path in sorted(company_dir.glob("*.jsonl")):
            out_path = process_file(jsonl_path, args.output_dir, force=args.force)
            if out_path is not None:
                total_files += 1
                with out_path.open(encoding="utf-8") as fh:
                    total_chunks += sum(1 for line in fh if line.strip())

    logger.info(
        "Chunking complete — %d file(s) processed, %d total chunks",
        total_files,
        total_chunks,
    )


if __name__ == "__main__":
    main()
