import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv


def bootstrap_runtime_env():
    """Load .env into the process environment and apply any runtime defaults.

    Call this before importing torch-backed libraries so that env vars like
    KMP_DUPLICATE_LIB_OK are visible when those libraries initialise.
    """
    load_dotenv()


def resolve_pdf_workflow_config():
    """Read PDF workflow settings from environment variables and return them
    as a plain dict.

    Expected env vars (all optional with sensible defaults):
      PDF_DIR   - directory that contains the PDF(s)  [default: data/raw]
      PDF_GLOB  - filename pattern inside PDF_DIR     [default: *.pdf]
      HF_HOME   - HuggingFace model cache directory
      KMP_DUPLICATE_LIB_OK - Windows OpenMP workaround flag

    Returns a dict with keys:
      pdf_dir, pdf_glob, pdf_candidates, pdf_path, hf_home, kmp

    Raises FileNotFoundError if no PDFs match the pattern.
    """
    pdf_dir = Path(os.environ.get("PDF_DIR", "data/raw"))
    pdf_glob = os.environ.get("PDF_GLOB", "**/*.pdf")
    pdf_candidates = sorted(pdf_dir.glob(pdf_glob))

    if not pdf_candidates:
        raise FileNotFoundError(
            f"No PDFs found in '{pdf_dir}' matching '{pdf_glob}'. "
            "Check PDF_DIR and PDF_GLOB in your .env file."
        )

    return {
        "pdf_dir": pdf_dir,
        "pdf_glob": pdf_glob,
        "pdf_candidates": pdf_candidates,
        "pdf_path": pdf_candidates[0],
        "hf_home": os.environ.get("HF_HOME"),
        "kmp": os.environ.get("KMP_DUPLICATE_LIB_OK"),
    }


def ensure_stage_dirs(*dirs: Path) -> None:
    """Create one or more pipeline stage directories if they don't already exist."""
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)


def derive_year(stem: str) -> int | None:
    """Extract a 4-digit year (20xx) from a filename stem, or return None."""
    match = re.search(r"(20\d{2})", stem)
    return int(match.group(1)) if match else None


def load_jsonl(path: Path) -> list[dict]:
    """Load a JSONL file and return a list of dicts."""
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def save_jsonl_atomic(records: list[dict], path: Path) -> None:
    """Write records to a JSONL file atomically via a .tmp swap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)
