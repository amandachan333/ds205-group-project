# pipelines/

Scripts in this directory fall into three groups with different
lifecycles. Run them in the order described below.

## Ingestion (run once, idempotent)

PDFs → text elements → chunks → embeddings. Each stage skips work
that's already been done unless invoked with `--force`.

1. `extract.py` — PDF → `data/extracted/<company>/*.jsonl`
   (unstructured + Gemini Vision for table pages)
2. `chunk.py` — extracted JSONL → `data/chunked/<company>/*.jsonl`
   (sentence-window chunking, table-aware)
3. `embed.py` — chunks → `data/vector_store.db`
   (NEBIUS Qwen3-Embedding-8B; logs spend to `logs/token_spend.jsonl`)

## RAG runtime (run per question)

Shared retrieval infrastructure consumed by two pipeline variants.

- `retrieval.py` — hybrid BM25 + dense retrieval with RRF fusion.
  Imported by both pipelines; not run directly.
- `single_shot_rag.py` — one retrieval + one generation call.
- `multi_step_rag.py` — Least-to-Most decomposition: decompose →
  sub-question loop with intermediate persistence → assembly.

Both write to `db/benchmark.db`.

## Orchestration and offline evaluation

- `run_all.py` — runs either pipeline over all six benchmark
  questions in `evaluation/ground_truth.md`.
- `dump_runs.py` — exports `db/benchmark.db` results to
  `logs/runs.jsonl` for analysis.
- `evaluate_retrieval.py` — retrieval-quality eval (Recall@k, MRR)
  against ground truth; no generation calls, no API spend.

## See also

- `evaluation/` — human scoring aids, notebook for analysis
- `db/schema.sql` — table definitions used by these scripts
- `DECISIONS.md` — why this architecture, what alternatives we considered