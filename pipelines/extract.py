"""
Stage 1 of the ingestion pipeline: extract structured elements from
carbon performance PDFs.

Each PDF is partitioned into discrete elements (paragraphs, headers,
lists, tables) and written as one JSON record per element to a JSONL
file, ready for chunking by chunk.py.

Inputs and outputs
------------------
Input:   data/raw/<company>/*.pdf
Output:  data/extracted/<company>/<company>_<year>_elements.jsonl

Each output record contains: text, element_type, element_id,
parent_id, page_number, filename, languages, category_depth.

Configuration
-------------
Reads from .env at the repo root.

    GEMINI_API_KEY            required (https://aistudio.google.com/apikey)
    PDF_RASTERISE_DPI         default 200  - higher = better tables, larger payloads
    GEMINI_BATCH_PAGE_LIMIT   default 30   - pages per Gemini API call
    PDF_PARTITION_STRATEGY    default "hi_res"
    PDF_HI_RES_MODEL          default "yolox"

Usage
-----
Run from the repo root:

    python pipelines/extract.py        # extracts all companies under data/raw/
"""

import io
import logging
import os
import re
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pdf2image import convert_from_path
from PIL import Image
from unstructured.partition.pdf import partition_pdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import derive_year, save_jsonl_atomic

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# ---------------------------------------------------------------------------
# Gemini client (initialised lazily, cached for reuse)
# ---------------------------------------------------------------------------

_gemini_client: genai.Client | None = None

_GEMINI_MODEL = "gemini-2.5-flash"

# DPI for rasterising PDF pages before sending to Gemini. Higher = better
# table quality at the cost of larger image payloads and slower API calls.
_RASTERISE_DPI = int(os.environ.get("PDF_RASTERISE_DPI", "200"))

# Maximum table pages per Gemini API call. Keeps output within token
# limits and avoids truncation on table-heavy documents.
_BATCH_PAGE_LIMIT = int(os.environ.get("GEMINI_BATCH_PAGE_LIMIT", "30"))

_TABLE_EXTRACTION_PROMPT = """\
You are a precise data extraction tool. Extract ALL rows from every table on this page.

Output format — one line per data row:
Row Label | Column1: value | Column2: value | Column3: value

Rules:
1. The first entry on each line is the row label (metric name) with no key prefix.
2. For multi-level column headers, abbreviate and join with " — " (e.g. "Incl RECs — Baseline 2005").
3. Keep column header names SHORT but unambiguous. Abbreviate long phrases.
4. Include every data row. Do not skip, summarise, or reorder rows.
5. Use the exact numbers, percentages, and symbols shown in the table.
6. For empty cells, write "-". For cells showing "Not Available", write "N/A".
7. If a section header row spans the full table width, output it on its own line with no values.
8. If there are MULTIPLE SEPARATE tables on one page, start each with a marker line: --- TABLE 1 ---, --- TABLE 2 ---, etc.
9. Output ONLY data lines and table markers. No preamble, no commentary, no markdown formatting."""


def _get_gemini_client() -> genai.Client:
    """Return a cached Gemini client."""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set. "
            "Get a free key at https://aistudio.google.com/apikey"
        )

    _gemini_client = genai.Client(api_key=api_key)
    logging.info("extract: Gemini client initialised.")
    return _gemini_client


# ---------------------------------------------------------------------------
# Page rasterisation
# ---------------------------------------------------------------------------


def _rasterise_page(pdf_path: Path, page_number: int, dpi: int = _RASTERISE_DPI) -> Image.Image:
    """Rasterise a single 1-indexed PDF page to a PIL Image."""
    images = convert_from_path(
        str(pdf_path),
        first_page=page_number,
        last_page=page_number,
        dpi=dpi,
    )
    if not images:
        raise ValueError(f"Failed to rasterise page {page_number} of {pdf_path}")
    return images[0]


# ---------------------------------------------------------------------------
# Gemini table extraction (batched)
# ---------------------------------------------------------------------------


def _extract_tables_for_pages(
    pdf_path: Path, page_numbers: set[int], dpi: int = _RASTERISE_DPI
) -> dict[int, list[str]]:
    """Send table pages to Gemini in batched API calls; returns {page_num: [table_text, ...]}."""
    sorted_pages = sorted(page_numbers)

    # Rasterise all pages up-front (no API cost)
    page_images: dict[int, bytes] = {}
    for pn in sorted_pages:
        try:
            img = _rasterise_page(pdf_path, pn, dpi=dpi)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            page_images[pn] = buf.getvalue()
        except Exception as exc:
            logging.error("extract: failed to rasterise page %d: %s", pn, exc)

    if not page_images:
        return {}

    # Split into batches of _BATCH_PAGE_LIMIT
    available_pages = [pn for pn in sorted_pages if pn in page_images]
    batches = [
        available_pages[i : i + _BATCH_PAGE_LIMIT]
        for i in range(0, len(available_pages), _BATCH_PAGE_LIMIT)
    ]

    logging.info(
        "extract: %d table page(s) across %d batch(es) (limit %d per batch).",
        len(available_pages),
        len(batches),
        _BATCH_PAGE_LIMIT,
    )

    merged: dict[int, list[str]] = {}
    for batch_idx, batch_pages in enumerate(batches, 1):
        contents: list = []
        for pn in batch_pages:
            contents.append(f"--- PAGE {pn} ---")
            contents.append(types.Part.from_bytes(data=page_images[pn], mime_type="image/png"))

        contents.append(
            _TABLE_EXTRACTION_PROMPT
            + "\n\nIMPORTANT: There are multiple pages. Start each page's output with "
            "a line exactly like '=== PAGE 1 ===' (using the actual page number). "
            "Extract tables from ALL pages."
        )

        logging.info(
            "extract: batch %d/%d — sending %d page(s) to Gemini.",
            batch_idx,
            len(batches),
            len(batch_pages),
        )

        output_text = _gemini_call_with_retry(
            contents, label=f"batch {batch_idx}/{len(batches)} {batch_pages}"
        )
        if not output_text:
            continue

        batch_result = _split_by_page(output_text, batch_pages)
        merged.update(batch_result)

    return merged


def _gemini_call_with_retry(contents: list, label: str = "", max_retries: int = 5) -> str:
    """Make a Gemini API call with retry on 429/503."""
    client = _get_gemini_client()

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(max_output_tokens=65536),
            )
            return response.text.strip() if response.text else ""
        except Exception as exc:
            exc_str = str(exc)
            is_rate_limit = "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str
            is_overloaded = "503" in exc_str or "UNAVAILABLE" in exc_str

            if is_rate_limit or is_overloaded:
                wait = _parse_retry_delay(exc_str) or (30 * (2**attempt))
                logging.warning(
                    "extract: %s — %s (attempt %d/%d), waiting %.0fs...",
                    label,
                    "rate limited" if is_rate_limit else "overloaded",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
                continue
            else:
                logging.error("extract: Gemini API failed for %s: %s", label, exc)
                return ""

    logging.error("extract: %s — exhausted all retries.", label)
    return ""


def _parse_retry_delay(exc_str: str) -> float | None:
    """Extract retry delay in seconds from a Gemini error message."""
    match = re.search(r"retryDelay.*?(\d+(?:\.\d+)?)s", exc_str)
    if match:
        return float(match.group(1)) + 2
    return None


def _split_by_page(text: str, page_numbers: list[int]) -> dict[int, list[str]]:
    """Split Gemini's batched output on '=== PAGE N ===' markers; returns {page_number: [table_text, ...]}."""
    page_chunks: dict[int, str] = {}
    current_page = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        page_match = re.match(r"^===\s*PAGE\s*(\d+)\s*===", line.strip())
        if page_match:
            if current_page is not None:
                page_chunks[current_page] = "\n".join(current_lines).strip()
            current_page = int(page_match.group(1))
            current_lines = []
            continue
        current_lines.append(line)

    if current_page is not None:
        page_chunks[current_page] = "\n".join(current_lines).strip()

    if not page_chunks and page_numbers:
        if len(page_numbers) == 1:
            page_chunks = {page_numbers[0]: text}
        else:
            logging.warning(
                "extract: Gemini did not use page markers for multi-page batch. "
                "Assigning all output to first table page."
            )
            page_chunks = {page_numbers[0]: text}

    result: dict[int, list[str]] = {}
    for pn, page_text in page_chunks.items():
        tables = _split_tables_on_page(page_text)
        if tables:
            result[pn] = tables

    return result


def _split_tables_on_page(page_text: str) -> list[str]:
    """Split a page's output on '--- TABLE N ---' markers; returns [whole_text] if none found."""
    parts: list[list[str]] = []
    current: list[str] | None = None

    for line in page_text.split("\n"):
        if re.match(r"^---\s*TABLE\s*\d+\s*---", line.strip()):
            if current is not None:
                parts.append(current)
            current = []
            continue

        if current is None:
            current = []
        current.append(line)

    if current:
        parts.append(current)

    tables = ["\n".join(lines).strip() for lines in parts if any(ln.strip() for ln in lines)]
    return tables if tables else ([page_text.strip()] if page_text.strip() else [])


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------


def extract_elements(pdf_path: Path) -> list[dict]:
    """Partition a PDF via unstructured; re-extract tables using Gemini VLM. Returns one dict per element."""
    strategy = os.environ.get("PDF_PARTITION_STRATEGY", "hi_res")
    hi_res_model = os.environ.get("PDF_HI_RES_MODEL", "yolox")

    logging.info(
        "extract_elements: strategy=%s  model=%s  file=%s",
        strategy,
        hi_res_model,
        pdf_path.name,
    )

    raw_elements = partition_pdf(
        filename=str(pdf_path),
        strategy=strategy,
        hi_res_model_name=hi_res_model,
        infer_table_structure=False,
    )

    table_pages: set[int] = set()
    for el in raw_elements:
        if type(el).__name__ == "Table":
            pn = getattr(getattr(el, "metadata", None), "page_number", None)
            if pn is not None:
                table_pages.add(pn)

    page_tables: dict[int, list[str]] = {}
    if table_pages:
        logging.info("extract: tables on pages %s — sending to Gemini.", sorted(table_pages))
        page_tables = _extract_tables_for_pages(pdf_path, table_pages)

    page_table_cursor: dict[int, int] = {}

    records = []
    for el in raw_elements:
        raw_text = getattr(el, "text", None)
        if not raw_text or not raw_text.strip():
            continue

        el_type = type(el).__name__
        page_num = getattr(getattr(el, "metadata", None), "page_number", None)

        if el_type == "Table" and page_num in page_tables:
            idx = page_table_cursor.get(page_num, 0)
            tables_on_page = page_tables[page_num]

            if idx < len(tables_on_page):
                text = tables_on_page[idx]
            else:
                text = tables_on_page[-1]

            page_table_cursor[page_num] = idx + 1
        else:
            text = raw_text.strip()

        records.append(
            {
                "text": text,
                "element_type": el_type,
                "element_id": getattr(el, "id", None),
                "parent_id": getattr(el.metadata, "parent_id", None),
                "page_number": page_num,
                "filename": getattr(el.metadata, "filename", None),
                "languages": getattr(el.metadata, "languages", None),
                "category_depth": getattr(el.metadata, "category_depth", None),
            }
        )

    return records


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _discover_pdfs(raw_dir: Path, company: str | None = None) -> list[Path]:
    """Return PDFs under raw_dir/<company>/ (or all companies if company is None)."""
    if company is not None:
        company_dirs = [raw_dir / company]
    else:
        if not raw_dir.exists():
            return []
        company_dirs = sorted(p for p in raw_dir.iterdir() if p.is_dir())

    pdfs: list[Path] = []
    for cdir in company_dirs:
        if not cdir.exists():
            logging.warning("extract: raw directory does not exist: %s", cdir)
            continue
        pdfs.extend(sorted(cdir.glob("*.pdf")))
    return pdfs


def _extracted_years(extracted_company_dir: Path) -> set[int]:
    """Return the set of years already present in an extracted company directory."""
    years: set[int] = set()
    if not extracted_company_dir.exists():
        return years
    for jsonl_file in extracted_company_dir.glob("*.jsonl"):
        year = derive_year(jsonl_file.stem)
        if year is not None:
            years.add(year)
    return years


def run_extraction(
    raw_dir: Path | str = Path("data/raw"),
    extracted_dir: Path | str = Path("data/extracted"),
    company: str | None = None,
    force: bool = False,
    hi_res_model: str | None = None,
) -> list[Path]:
    """
    Run PDF extraction for one company (or all companies) under raw_dir.

    Reads PDFs from:   <raw_dir>/<company>/*.pdf
    Writes JSONL to:   <extracted_dir>/<company>/<company>_<year>_elements.jsonl

    Returns the list of output paths that were written this run.
    """
    raw_dir = Path(raw_dir)
    extracted_dir = Path(extracted_dir)

    if hi_res_model is not None:
        os.environ["PDF_HI_RES_MODEL"] = hi_res_model

    pdf_candidates = _discover_pdfs(raw_dir, company=company)
    if not pdf_candidates:
        logging.warning("extract: no PDFs found under %s (company=%s)", raw_dir, company)
        return []

    written: list[Path] = []
    for pdf_path in pdf_candidates:
        company_name = pdf_path.parent.name
        year = derive_year(pdf_path.stem)
        if year is None:
            logging.warning(
                "extract: no year found in filename '%s' — omitting from output name.",
                pdf_path.name,
            )

        if not force and year is not None:
            done_years = _extracted_years(extracted_dir / company_name)
            if year in done_years:
                logging.info(
                    "extract: skipping %s — year %d already present in %s.",
                    pdf_path.name,
                    year,
                    extracted_dir / company_name,
                )
                continue

        out_name = (
            f"{company_name}_{year}_elements.jsonl" if year else f"{company_name}_elements.jsonl"
        )
        out_path = extracted_dir / company_name / out_name

        logging.info("extract: parsing %s / %s", company_name, pdf_path.name)
        records = extract_elements(pdf_path)

        if not records:
            raise ValueError(
                f"No elements were produced from '{pdf_path}'. "
                "Re-check the PDF path and PDF_PARTITION_STRATEGY before proceeding."
            )

        lengths = [len(r["text"]) for r in records]
        print(f"\n{company_name}/{pdf_path.name}")
        print(f"  Text elements kept : {len(records)}")
        print(f"  min length   : {min(lengths)}")
        print(f"  median length: {int(statistics.median(lengths))}")
        print(f"  max length   : {max(lengths)}")

        save_jsonl_atomic(records, out_path)
        logging.info("extract: saved %d elements to %s", len(records), out_path)
        written.append(out_path)

    return written


# ---------------------------------------------------------------------------
# Script entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    project_root = Path(__file__).resolve().parent.parent
    run_extraction(
        raw_dir=project_root / "data" / "raw",
        extracted_dir=project_root / "data" / "extracted",
    )
