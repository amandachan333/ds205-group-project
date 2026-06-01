"""
multi_step_rag.py
-----------------
Multi-step RAG pipeline using Least-to-Most (LtM) prompting.

For each complex question this pipeline runs three phases, persisting after
every LLM call so the run can be resumed from any crash:

  1. Decomposition: ask the model to break the question into ordered
     sub-questions. Stored in the `decompositions` table (one row per run).
  2. Sequential sub-question answering: each sub-question gets its own
     retrieval pass and generation call. Earlier sub-answers are carried
     forward as `Established findings` so later sub-questions can build on
     them. Each step is stored in the `steps` table with `step_index = 1..N`.
  3. Assembly: synthesise the final answer from the completed sub-answers.
     Stored in the `final_answers` table.

Retrieval and prompt formatting come from `pipelines/retrieval.py` and are
shared verbatim with the single-shot pipeline so the benchmark comparison is
fair - the only thing that differs between the two pipelines is the question
phrasing (one shot vs decomposed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval import (
    BM25_WEIGHT,
    RRF_K,
    check_budget,
    format_context,
    get_corpus_inventory,
    hybrid_retrieve,
    load_index,
    log_generation_spend,
    make_client,
    select_context_chunks,
    summarise_chunks_for_step,
)

from config import DB_PATH, LOG_DIR
from db import database as database
from utils import bootstrap_runtime_env, ensure_stage_dirs

bootstrap_runtime_env()
ensure_stage_dirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "multi_step_rag.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get("RAG_GENERATION_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")
DEFAULT_TOP_N = 40
DEFAULT_CONTEXT_CHUNKS = 10
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS_DECOMP = 1024
DEFAULT_MAX_OUTPUT_TOKENS_SUB = 1024
DEFAULT_MAX_OUTPUT_TOKENS_ASSEMBLY = 2048
DEFAULT_MAX_CHARS_PER_CHUNK = 1400

PIPELINE_TYPE = "multi_step"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

DECOMP_SYSTEM_PROMPT = (
    "You are an expert analyst specialising in corporate carbon performance and emissions reporting. "
    "Your task is to break down a complex question into a sequence of simpler sub-questions that can each be answered by searching a company's sustainability or annual report."
)

DECOMP_USER_PROMPT = """Rules for decomposition:
- Order sub-questions from simplest to most dependent. Data extraction steps (retrieving a single figure for a single year) must come before reasoning steps (assessing trends, computing changes, making comparisons).
- Each sub-question must be independently answerable from a single retrieved passage - do not write sub-questions that themselves require multi-step reasoning.
- Be specific. Include the company name, year, and metric in each sub-question so it can be used directly as a retrieval query.
- If the original question refers to "most recent year", "latest reported year", "current", or similar relational time references, use the corpus context provided below to resolve these into concrete years, and write sub-questions that name those years explicitly. Do not write a sub-question whose only purpose is to determine which year is the most recent - that information is given to you.
- The final sub-question should be the synthesis or reasoning step that combines the earlier answers into the overall answer.
- Return ONLY a numbered list of sub-questions. No preamble, no explanation, no text before or after the list.

---

Example 1 - Trajectory question:

Question: Has Company A's Scope 1 emissions intensity decreased consistently between 2020 and 2022, and by how much in total?

Sub-questions:
1. What was Company A's Scope 1 emissions intensity in 2020?
2. What was Company A's Scope 1 emissions intensity in 2021?
3. What was Company A's Scope 1 emissions intensity in 2022?
4. Did Company A's Scope 1 emissions intensity decrease between 2020 and 2021?
5. Did Company A's Scope 1 emissions intensity decrease between 2021 and 2022?
6. Was the decrease consistent every year between 2020 and 2022, or were there any reversals?
7. What is the total percentage change in Company A's Scope 1 emissions intensity from 2020 to 2022?

---

Example 2 - Change-over-time question:

Question: How has Company B's Scope 3 emissions reporting coverage changed between its 2021 and 2023 sustainability reports, and does the change affect the interpretation of its overall emissions trend?

Sub-questions:
1. Which Scope 3 categories did Company B report in its 2021 sustainability report?
2. Which Scope 3 categories does Company B report in its 2023 sustainability report?
3. Have any Scope 3 categories been added between 2021 and 2023?
4. Have any Scope 3 categories been removed or reclassified between 2021 and 2023?
5. What was Company B's total reported Scope 3 emissions figure in 2021?
6. What is Company B's total reported Scope 3 emissions figure in 2023?
7. Does the change in Scope 3 coverage affect whether the apparent emissions trend represents a genuine reduction or a reporting boundary change?

---

Example 3 - Comparative question:

Question: Comparing Company C and Company D's progress toward their respective science-based Scope 1 and Scope 2 emissions reduction targets, which company has achieved the greater reduction from its base year to its most recently reported year, in both absolute and percentage terms?

Sub-questions:
1. What is Company C's science-based Scope 1 and Scope 2 emissions reduction target, and what is its base year?
2. What were Company C's combined Scope 1 and Scope 2 emissions in its base year?
3. What are Company C's most recently reported combined Scope 1 and Scope 2 emissions?
4. What is Company D's science-based Scope 1 and Scope 2 emissions reduction target, and what is its base year?
5. What were Company D's combined Scope 1 and Scope 2 emissions in its base year?
6. What are Company D's most recently reported combined Scope 1 and Scope 2 emissions?
7. What is Company C's absolute reduction and percentage reduction from base year to most recent year?
8. What is Company D's absolute reduction and percentage reduction from base year to most recent year?
9. Which company has achieved the greater reduction in absolute terms, and which in percentage terms?

---

Example 4 - Comparative question with "most recent year" (assuming the corpus context lists Company E reports through 2024 and Company F reports through 2023):

Question: Comparing Company E's and Company F's emissions intensity from 2019 to their most recently reported year, which company achieved the larger reduction in absolute and percentage terms?

Sub-questions:
1. What was Company E's emissions intensity in 2019?
2. What was Company E's emissions intensity in 2024?
3. What was Company F's emissions intensity in 2019?
4. What was Company F's emissions intensity in 2023?
5. What is Company E's absolute reduction and percentage reduction in emissions intensity from 2019 to 2024?
6. What is Company F's absolute reduction and percentage reduction in emissions intensity from 2019 to 2023?
7. Which company achieved the larger absolute reduction, and which achieved the larger percentage reduction?
8. Do the two measures (absolute and percentage) agree on the ranking?

---

{corpus_context}

Now decompose the following question:

Question: {complex_question}

Sub-questions:
"""


SUBQ_SYSTEM_PROMPT = (
    "You are an expert analyst specialising in corporate carbon performance and emissions reporting. "
    "You are answering one step in a multi-step analysis of a company's carbon performance."
)

SUBQ_USER_PROMPT = """You will be given:
- The original complex question for context
- Facts already established in previous steps
- Retrieved passages relevant to the current sub-question
- The current sub-question to answer

Rules:
- Answer only the current sub-question. Do not attempt to answer the original complex question in full.
- Use the retrieved passages as your primary source. Use previously established facts only to provide context or to perform reasoning that combines prior findings with new evidence.
- If the retrieved passages do not contain enough information to answer the sub-question, say so explicitly. Do not guess.
- If a retrieved passage contains a chart or table with multiple values across years (e.g. a sequence like "0.4178 0.4041 0.4293" labelled "2019-2021"), state explicitly which value you are extracting and which year it corresponds to. The order of values in the source must match the order of years stated in the chart heading, axis label, or surrounding text. If the alignment between values and years is ambiguous, say so rather than guess.
- If a retrieved passage contains multiple distinct values for the same year (for
  example, a chart showing both "electricity-only" and "combined electricity and
  water" emissions intensity, both for 2019), state all candidate values found and
  explicitly identify which one is the carbon-performance comparable answer for the
  sub-question. If you cannot determine which is comparable from the passage alone,
  state that the answer is ambiguous and report both candidates with their labels.
- If a retrieved passage contains a figure labelled with a different year than the
  chunk's source report (for example, a chunk from a "2019 Sustainability Report"
  whose text reads "FY2018 was 0.57 tCO2e/MWh"), report which year the figure
  actually applies to according to the passage's own text, not the year of the
  report it appears in. If the year alignment between the passage and the
  sub-question is unclear, say so explicitly rather than answer.
- For every factual claim drawn from the retrieved passages, cite the source document and page number in parentheses, e.g. (2023 Sustainability Report, p.12).
- If the sub-question requires a calculation, show your working.
- Be concise. One to three sentences is usually sufficient.

---

Original question: {original_question}

Previously established facts:
{established_facts}

Retrieved passages:
{retrieved_chunks}

Current sub-question: {sub_question}
"""


ASSEMBLY_SYSTEM_PROMPT = (
    "You are an expert analyst specialising in corporate carbon performance and emissions reporting. "
    "You are writing a final answer for a stakeholder at the TPI Centre based on findings from a structured multi-step analysis."
)

ASSEMBLY_USER_PROMPT = """You will be given:
- The original complex question
- A numbered list of sub-questions and their verified answers, established through document retrieval

Rules:
- Base your answer entirely on the established findings provided. Do not introduce new claims or figures not present in the findings.
- Address every part of the original question directly.
- Where findings include citations, carry them through to the final answer so claims remain traceable.
- If any sub-step finding was incomplete or uncertain, reflect that uncertainty honestly in the final answer rather than smoothing over it.
- If a sub-step reported multiple candidates with labels, use the most carbon-performance-relevant one (typically electricity-only for utilities) and note the alternative.
- Write in clear, professional prose suitable for a sustainability analyst. Avoid bullet points unless the question explicitly asks for a list.

---

Original question: {original_question}

Established findings:
{findings}
"""


# Stable question id helper
def stable_qid(question_text: str) -> str:
    """Deterministic question id derived from normalized text."""
    norm = " ".join(question_text.split()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# LLM call wrapper
# ---------------------------------------------------------------------------


def _chat(
    client,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_output_tokens: int,
) -> tuple[str, int, int]:
    """Single chat completion call. Returns (text, prompt_tokens, completion_tokens)."""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_output_tokens,
    )
    text = (response.choices[0].message.content or "").strip()
    usage = response.usage
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return text, prompt_tokens, completion_tokens


# ---------------------------------------------------------------------------
# Decomposition parsing
# ---------------------------------------------------------------------------

_NUMBERED_LINE = re.compile(r"^\s*\d+[\.\)]\s+(.+?)\s*$")


def parse_sub_questions(raw: str) -> list[str]:
    """
    Extract sub-questions from a numbered-list response.

    Strips numbering, ignores any preamble/postamble, and drops blank lines.
    Returns an empty list if no numbered items are found.
    """
    sub_qs: list[str] = []
    for line in raw.splitlines():
        match = _NUMBERED_LINE.match(line)
        if match:
            text = match.group(1).strip()
            if text:
                sub_qs.append(text)
    return sub_qs


# ---------------------------------------------------------------------------
# Prompt-block formatters
# ---------------------------------------------------------------------------


def _format_established_facts(completed_steps: list) -> str:
    """Render completed sub-question answers as the 'previously established facts' block."""
    if not completed_steps:
        return "(none yet - this is the first sub-question)"
    lines: list[str] = []
    for step in completed_steps:
        lines.append(f"Step {step['step_index']}: {step['sub_question']}")
        lines.append(f"Answer: {step['answer']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_findings(completed_steps: list) -> str:
    """Render the findings block fed to the assembly prompt."""
    lines: list[str] = []
    for step in completed_steps:
        lines.append(f"{step['step_index']}. {step['sub_question']}")
        lines.append(f"   Answer: {step['answer']}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Phase 1: decomposition
# ---------------------------------------------------------------------------


def run_decomposition(
    *,
    conn,
    client,
    model: str,
    run_id: int,
    question_id: str,
    question: str,
    decomp_id: int,
    corpus_context: str,
    temperature: float,
    max_output_tokens: int,
) -> tuple[list[str], dict[str, int]]:
    """
    Call the decomposition LLM, parse sub-questions, persist to the
    `decompositions` table. Returns (sub_questions, token_usage).

    On parse failure the row is marked status='failed' (sub_questions='[]'),
    the run is marked failed, and an exception is raised so the caller stops.
    """
    log.info("[run %d] Decomposing question ...", run_id)
    user_prompt = DECOMP_USER_PROMPT.format(
        complex_question=question,
        corpus_context=corpus_context,
    )
    raw, p_tokens, c_tokens = _chat(
        client=client,
        model=model,
        system_prompt=DECOMP_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    log.debug("[run %d] Raw decomposition response:\n%s", run_id, raw)

    log_generation_spend(
        question_id,
        model,
        p_tokens,
        c_tokens,
        extra={"phase": "decomposition", "run_id": run_id},
    )

    sub_qs = parse_sub_questions(raw)

    if not sub_qs:
        database.complete_decomposition(
            conn,
            decomp_id,
            sub_questions_json="[]",
            raw_response=raw,
            input_tokens=p_tokens,
            output_tokens=c_tokens,
            status="failed",
        )
        database.fail_run(conn, run_id)
        raise RuntimeError(
            f"Decomposition produced no parseable sub-questions for run {run_id}. "
            f"Raw response stored in decompositions.raw_response."
        )

    database.complete_decomposition(
        conn,
        decomp_id,
        sub_questions_json=json.dumps(sub_qs, ensure_ascii=False),
        raw_response=raw,
        input_tokens=p_tokens,
        output_tokens=c_tokens,
        status="complete",
    )
    log.info("[run %d] Decomposed into %d sub-questions", run_id, len(sub_qs))
    for i, sq in enumerate(sub_qs, 1):
        log.info("  %d. %s", i, sq)

    return sub_qs, {"prompt_tokens": p_tokens, "completion_tokens": c_tokens}


def review_sub_questions(sub_qs: list[str]) -> list[str] | None:
    """
    Interactive review of the decomposition. Returns the (possibly edited) list,
    or None if the user wants to quit.
    """
    while True:
        print("\nProposed decomposition:")
        for i, sq in enumerate(sub_qs, 1):
            print(f"  {i}. {sq}")
        choice = input("\n[a]ccept / [e]dit / [r]egenerate / [q]uit > ").strip().lower()
        if choice in ("a", ""):
            return sub_qs
        if choice == "q":
            return None
        if choice == "r":
            return []  # signal: regenerate (caller handles)
        if choice == "e":
            print("Enter edited sub-questions, one per line. Empty line to finish:")
            edited: list[str] = []
            while True:
                line = input(f"  {len(edited) + 1}. ").strip()
                if not line:
                    break
                edited.append(line)
            if edited:
                sub_qs = edited
                continue
            print("No sub-questions entered, keeping the previous list.")
        else:
            print("Unknown choice; pick a / e / r / q.")


# ---------------------------------------------------------------------------
# Phase 2: sub-question answering
# ---------------------------------------------------------------------------


def answer_sub_question(
    *,
    conn,
    client,
    index,
    model: str,
    run_id: int,
    question_id: str,
    original_question: str,
    step_row,
    completed_steps: list,
    top_n: int,
    context_chunks_n: int,
    bm25_weight: int,
    rrf_k: int,
    max_chars_per_chunk: int,
    temperature: float,
    max_output_tokens: int,
) -> tuple[int, int]:
    """
    Retrieve for one sub-question, call the LLM, persist the result.
    Returns (prompt_tokens, completion_tokens).
    """
    step_id = step_row["step_id"]
    step_index = step_row["step_index"]
    sub_question = step_row["sub_question"]

    log.info("[run %d] Step %d: %s", run_id, step_index, sub_question)

    retrieved = hybrid_retrieve(
        query=sub_question,
        client=client,
        index=index,
        k=max(top_n, context_chunks_n),
        bm25_weight=bm25_weight,
        rrf_k=rrf_k,
    )
    selected = select_context_chunks(sub_question, retrieved, context_chunks_n)
    context = format_context(selected, max_chars_per_chunk)

    established = _format_established_facts(completed_steps)
    user_prompt = SUBQ_USER_PROMPT.format(
        original_question=original_question,
        established_facts=established,
        retrieved_chunks=context,
        sub_question=sub_question,
    )

    answer, p_tokens, c_tokens = _chat(
        client=client,
        model=model,
        system_prompt=SUBQ_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )

    log.debug("[run %d] Step %d answer: %s", run_id, step_index, answer)

    log_generation_spend(
        question_id,
        model,
        p_tokens,
        c_tokens,
        extra={"phase": "sub_question", "run_id": run_id, "step_index": step_index},
    )

    database.complete_step(
        conn,
        step_id=step_id,
        answer=answer,
        retrieved_chunks=summarise_chunks_for_step(selected),
        input_tokens=p_tokens,
        output_tokens=c_tokens,
    )
    return p_tokens, c_tokens


# ---------------------------------------------------------------------------
# Phase 3: assembly
# ---------------------------------------------------------------------------


def assemble_final_answer(
    *,
    conn,
    client,
    model: str,
    run_id: int,
    question_id: str,
    original_question: str,
    completed_steps: list,
    temperature: float,
    max_output_tokens: int,
) -> tuple[str, int, int]:
    """Run the assembly call, persist the final answer. Returns (answer, p_tok, c_tok)."""
    log.info(
        "[run %d] Assembling final answer from %d sub-answers ...", run_id, len(completed_steps)
    )
    findings = _format_findings(completed_steps)
    user_prompt = ASSEMBLY_USER_PROMPT.format(
        original_question=original_question,
        findings=findings,
    )
    answer, p_tokens, c_tokens = _chat(
        client=client,
        model=model,
        system_prompt=ASSEMBLY_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    log_generation_spend(
        question_id,
        model,
        p_tokens,
        c_tokens,
        extra={"phase": "assembly", "run_id": run_id},
    )
    database.insert_final_answer(conn, run_id, answer)
    log.info("[run %d] Final answer:\n%s", run_id, answer)
    return answer, p_tokens, c_tokens


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def run_pipeline(
    *,
    conn,
    client,
    index,
    model: str,
    question: str,
    question_id: str,
    run_id: int,
    decomp_id: int,
    args: argparse.Namespace,
    corpus_context: str,
    sub_qs_existing: list[str] | None = None,
) -> None:
    """
    Drive a single multi-step run from current state to completion.

    `sub_qs_existing` is non-None only when resuming a run whose decomposition
    is already in the database. In that case we skip phase 1 entirely.
    """
    started_at = time.perf_counter()
    # Assembly tokens are the only ones not persisted to a dedicated table row,
    # so we track them locally and add them to the DB-derived total at the end.
    assembly_prompt_tokens = 0
    assembly_completion_tokens = 0

    # Phase 1: decomposition (skipped if already complete on resume)
    if sub_qs_existing is None:
        sub_qs, usage = run_decomposition(
            conn=conn,
            client=client,
            model=model,
            run_id=run_id,
            question_id=question_id,
            question=question,
            decomp_id=decomp_id,
            corpus_context=corpus_context,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens_decomp,
        )

        # Optional human review before spending tokens on the sub-question loop
        if args.review:
            while True:
                reviewed = review_sub_questions(sub_qs)
                if reviewed is None:
                    log.info("[run %d] User quit before sub-question loop. Marking failed.", run_id)
                    database.fail_run(conn, run_id)
                    return
                if reviewed:
                    sub_qs = reviewed
                    # Update the stored decomposition to reflect any edits
                    database.complete_decomposition(
                        conn,
                        decomp_id,
                        sub_questions_json=json.dumps(sub_qs, ensure_ascii=False),
                        raw_response="(edited via --review)",
                        input_tokens=usage["prompt_tokens"],
                        output_tokens=usage["completion_tokens"],
                        status="complete",
                    )
                    break
                # User asked to regenerate -> re-run decomposition
                sub_qs, usage = run_decomposition(
                    conn=conn,
                    client=client,
                    model=model,
                    run_id=run_id,
                    question_id=question_id,
                    question=question,
                    decomp_id=decomp_id,
                    corpus_context=corpus_context,
                    temperature=args.temperature,
                    max_output_tokens=args.max_output_tokens_decomp,
                )

        # Pre-insert all sub-question rows as pending so the full plan is
        # visible in SQLite before any sub-question call runs.
        for i, sq in enumerate(sub_qs, start=1):
            database.insert_step(conn, run_id=run_id, step_index=i, sub_question=sq)
    else:
        sub_qs = sub_qs_existing
        log.info(
            "[run %d] Resuming with %d sub-questions already in DB",
            run_id,
            len(sub_qs),
        )

    if args.dry_run:
        log.info(
            "[run %d] Dry run - decomposition complete, stopping before sub-question loop.", run_id
        )
        check_budget("multi_step")
        return

    # Phase 2: sub-question loop (resumable - process pending in order).
    # Per-step token counts are persisted to the `steps` table inside
    # answer_sub_question(), so nothing to accumulate locally here.
    while True:
        pending = database.get_pending_steps(conn, run_id)
        if not pending:
            break
        next_step = pending[0]
        completed = list(database.get_completed_steps(conn, run_id))
        answer_sub_question(
            conn=conn,
            client=client,
            index=index,
            model=model,
            run_id=run_id,
            question_id=question_id,
            original_question=question,
            step_row=next_step,
            completed_steps=completed,
            top_n=args.top_n,
            context_chunks_n=args.context_chunks,
            bm25_weight=args.bm25_weight,
            rrf_k=args.rrf_k,
            max_chars_per_chunk=args.max_chars_per_chunk,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens_sub,
        )

    # Phase 3: assembly (skipped if final_answer already exists)
    if database.get_final_answer(conn, run_id) is None:
        completed = list(database.get_completed_steps(conn, run_id))
        if not completed:
            log.error("[run %d] No completed sub-steps; cannot assemble. Marking failed.", run_id)
            database.fail_run(conn, run_id)
            return
        _, assembly_prompt_tokens, assembly_completion_tokens = assemble_final_answer(
            conn=conn,
            client=client,
            model=model,
            run_id=run_id,
            question_id=question_id,
            original_question=question,
            completed_steps=completed,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens_assembly,
        )
    else:
        log.info("[run %d] Final answer already present, skipping assembly.", run_id)

    # Aggregate run-level totals. Decomposition + each sub-question step have
    # their token counts in the database, so we sum from there. Assembly is the
    # only phase without its own table column for tokens; we add this-process
    # assembly tokens locally. (If this is a resumed run where assembly was
    # already done in a previous process, those assembly tokens are visible in
    # token_spend.jsonl but not counted in runs.total_tokens. This is a known
    # gap; for our 6-question benchmark, assembly is typically ~1-3k tokens
    # and runs almost always complete in one process.)
    from retrieval import calculate_generation_cost

    decomp_row = database.get_decomposition(conn, run_id)
    completed = list(database.get_completed_steps(conn, run_id))

    grand_prompt = (decomp_row["input_tokens"] or 0) if decomp_row else 0
    grand_completion = (decomp_row["output_tokens"] or 0) if decomp_row else 0
    for step in completed:
        grand_prompt += step["input_tokens"] or 0
        grand_completion += step["output_tokens"] or 0
    grand_prompt += assembly_prompt_tokens
    grand_completion += assembly_completion_tokens

    _, _, total_cost = calculate_generation_cost(model, grand_prompt, grand_completion)

    latency_seconds = time.perf_counter() - started_at
    database.complete_run(
        conn,
        run_id=run_id,
        latency_seconds=latency_seconds,
        total_tokens=int(grand_prompt + grand_completion),
        total_cost_usd=total_cost,
    )
    check_budget("multi_step")
    log.info(
        "[run %d] Multi-step complete | %.2fs | %d prompt + %d completion = %d tokens | $%.4f",
        run_id,
        latency_seconds,
        grand_prompt,
        grand_completion,
        grand_prompt + grand_completion,
        total_cost,
    )


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------


def prepare_resume(conn, resume_run_id: int) -> tuple[int, str, str, int, list[str] | None]:
    """
    Inspect the DB and return (run_id, question_id, question_text, decomp_id, sub_qs)
    ready to feed into run_pipeline.

    sub_qs is None when decomposition still needs to be run (pending row or no row).
    Raises SystemExit if the run cannot be resumed (not found, already complete,
    wrong pipeline type, or decomposition failed previously).
    """
    run = database.get_run(conn, resume_run_id)
    if run is None:
        sys.exit(f"Run {resume_run_id} not found.")
    if run["pipeline_type"] != PIPELINE_TYPE:
        sys.exit(
            f"Run {resume_run_id} is pipeline_type={run['pipeline_type']!r}, not 'multi_step'."
        )
    if run["status"] == "complete":
        sys.exit(f"Run {resume_run_id} is already complete.")
    if run["status"] == "failed":
        sys.exit(f"Run {resume_run_id} is marked failed; create a new run instead.")

    q_row = conn.execute(
        "SELECT question_id, question_text FROM questions WHERE question_id = ?",
        (run["question_id"],),
    ).fetchone()
    if q_row is None:
        sys.exit(f"Question {run['question_id']} for run {resume_run_id} not found.")
    question_id = q_row["question_id"]
    question_text = q_row["question_text"]

    decomp = database.get_decomposition(conn, resume_run_id)
    if decomp is None:
        # No decomposition row yet - create one and start from phase 1.
        decomp_id = database.create_decomposition(conn, resume_run_id)
        return resume_run_id, question_id, question_text, decomp_id, None
    if decomp["status"] == "failed":
        sys.exit(
            f"Run {resume_run_id} previously failed to decompose. "
            f"Inspect decompositions.raw_response and start a new run."
        )
    if decomp["status"] == "pending":
        # Decomposition was created but never completed - redo phase 1.
        return resume_run_id, question_id, question_text, decomp["decomp_id"], None

    # status == 'complete'
    sub_qs = json.loads(decomp["sub_questions"] or "[]")
    if not sub_qs:
        sys.exit(
            f"Run {resume_run_id} has a complete-but-empty decomposition. "
            f"Inspect decompositions row and start a new run."
        )
    return resume_run_id, question_id, question_text, decomp["decomp_id"], sub_qs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a multi-step (Least-to-Most) RAG answer over the existing vector store.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db", type=Path, default=DB_PATH, help="Path to vector_store.db produced by embed.py."
    )
    parser.add_argument(
        "--question",
        type=str,
        default=None,
        help="Question to answer (required unless --resume is given).",
    )
    parser.add_argument(
        "--resume", type=int, default=None, help="Resume an existing multi-step run by its run_id."
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL, help="Nebius generation model name."
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help="Number of chunks to retrieve per sub-question.",
    )
    parser.add_argument(
        "--context-chunks",
        type=int,
        default=DEFAULT_CONTEXT_CHUNKS,
        help="Number of retrieved chunks placed in each sub-question prompt.",
    )
    parser.add_argument(
        "--bm25-weight", type=int, default=BM25_WEIGHT, help="Relative BM25 weight in RRF fusion."
    )
    parser.add_argument("--rrf-k", type=int, default=RRF_K, help="Reciprocal rank fusion constant.")
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help="Sampling temperature for all calls.",
    )
    parser.add_argument(
        "--max-output-tokens-decomp",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS_DECOMP,
        help="Max output tokens for the decomposition call.",
    )
    parser.add_argument(
        "--max-output-tokens-sub",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS_SUB,
        help="Max output tokens for each sub-question call.",
    )
    parser.add_argument(
        "--max-output-tokens-assembly",
        type=int,
        default=DEFAULT_MAX_OUTPUT_TOKENS_ASSEMBLY,
        help="Max output tokens for the assembly call.",
    )
    parser.add_argument(
        "--max-chars-per-chunk",
        type=int,
        default=DEFAULT_MAX_CHARS_PER_CHUNK,
        help="Truncate each chunk to this many characters before prompting.",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Pause after decomposition for human review/edit of sub-questions.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run decomposition only; do not loop sub-questions or assemble.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging (DEBUG level).")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.resume is None and not args.question:
        parser.error("--question is required unless --resume RUN_ID is given.")

    # Load retrieval index up-front; we need it for sub-question retrieval.
    # On --dry-run we still load it for symmetry, but it would only matter
    # if decomposition succeeds and we wanted to proceed.
    index = load_index(args.db)
    client = make_client()

    # Corpus metadata used to resolve "most recent year"-style phrasings in
    # the decomposition prompt. Cheap one-shot query at startup; ignored on
    # resume runs whose decomposition is already complete.
    corpus_context = get_corpus_inventory(args.db)
    log.info("Corpus inventory:\n%s", corpus_context)

    conn = database.get_connection()
    try:
        database.init_db(conn)

        if args.resume is not None:
            run_id, question_id, question_text, decomp_id, sub_qs = prepare_resume(
                conn, args.resume
            )
            log.info("[run %d] Resuming multi-step pipeline.", run_id)
        else:
            question_id = stable_qid(args.question)
            database.insert_question(conn, question_id, args.question)
            run_id = database.create_run(conn, question_id, PIPELINE_TYPE, args.model)
            decomp_id = database.create_decomposition(conn, run_id)
            question_text = args.question
            sub_qs = None

        try:
            run_pipeline(
                conn=conn,
                client=client,
                index=index,
                model=args.model,
                question=question_text,
                question_id=question_id,
                run_id=run_id,
                decomp_id=decomp_id,
                args=args,
                corpus_context=corpus_context,
                sub_qs_existing=sub_qs,
            )
        except Exception:
            log.exception(
                "[run %d] Unhandled error; leaving run in 'running' state for resume.", run_id
            )
            raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
