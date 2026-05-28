[![Review Assignment Due Date](https://classroom.github.com/assets/deadline-readme-button-22041afd0340ce965d47ae6ef1cefeee28c7c493a6346c4f15d667ab976d596c.svg)](https://classroom.github.com/a/Ho0VU-Re)
# tpi-rag-decomposition

Benchmark comparing two RAG strategies over TPI Carbon Performance PDFs, both using Nebius-hosted open-weight LLMs:

- **Decomposition pipeline** (small model): breaks each question into sub-questions, retrieves and generates per sub-question, persists intermediates to SQLite, then assembles a final answer.
- **Single-shot baseline** (large model): retrieves top-K chunks and answers in one prompt.

Tests whether decomposition with a smaller, cheaper model can match or beat a larger model — evaluated on answer correctness, faithfulness, inspectability, latency, and token cost.

## Overview

> **TODO**: Describe the data sources (which TPI PDFs, how they are ingested) and the outputs produced (evaluation results, reports).

## How to Run

> **TODO**: Provide step-by-step instructions for running the pipeline. For example:
>
> 1. Install dependencies (see [CONTRIBUTING.md](CONTRIBUTING.md) for the full dev setup).
> 2. Configure environment variables — copy `.env.example` to `.env` and fill in your `NEBIUS_API_KEY`.
> 3. Run the pipeline:
>    ```bash
>    # TODO: replace with the actual command
>    python pipeline.py
>    ```
> 4. Check the output in the `reports/` directory.

## Single-Shot Baseline

Run one question through the hybrid retriever + generation model:

```bash
PYTHONPATH=. python pipelines/single_shot_rag.py \
	--question "Has DEWA’s carbon emission intensity for electricity improved consistently since 2010?" \
	--model Qwen/Qwen3-30B-A3B
```

The script writes run logs to `logs/rag_runs.jsonl` and uses the existing vector store at `data/vector_store.db`.

## Configuration

> **TODO**: Describe any configuration files, environment variables, or command-line arguments that control pipeline behaviour.

## Output

> **TODO**: Describe the format and location of the pipeline's output (files, database tables, API calls, etc.).
