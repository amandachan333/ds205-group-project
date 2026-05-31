# Decisions

This document records the architectural choices we made, the alternatives we considered, and the reasoning behind each decision.

## Sector and company selection

We chose the **Electrical Utilities** sector with three companies: DEWA, CenterPoint Energy, and Tenaga Nasional (TNB).

Electrical Utilities uses Scope 1 emissions divided by MWh of electricity produced as its intensity metric. This is the most straightforward of the three available sectors because Scope 1 data is almost always disclosed clearly in sustainability reports. 

The three companies we selected have multiple years of sustainability reports available on the TPI SharePoint folder, which supports the trajectory and change-over-time question types the brief suggests.

## Question design

We wrote six ground-truth questions before building either pipeline. The questions were designed to cover the three suggested types:

- **Comparative** (Q1, Q2): require retrieving data from different companies and comparing them against each other or against benchmarks.
- **Trajectory** (Q3, Q4): require retrieving intensity figures across multiple report years for the same company and computing changes.
- **Change-over-time** (Q5, Q6): require identifying how stated targets evolved between earlier and later reports.

Ground-truth answers were derived manually from the source PDFs, with specific page numbers, section titles, and chunk IDs recorded for traceability. Two team members cross-checked each answer independently before freezing the set.

## Experimental design

The core experiment is a 2x2 comparison:

| | Single-shot | Multi-step |
|---|---|---|
| **Qwen3-30B** | Condition A | Condition B |
| **Qwen3-235B** | Condition C | Condition D |

All four conditions use the same question set, ground-truth answers, vector store, retrieval configuration, and prompt templates. The only variables are pipeline type (single-shot vs multi-step) and model size.

We run the 30B conditions first because they are cheaper. This surfaces pipeline bugs before we spend tokens on the 235B model.

The comparison aims to answer three questions for Sylvan and the TPI Centre:

1. Does multi-step decomposition improve answer correctness compared to single-shot at the same model size?
2. Does a larger model improve correctness compared to a smaller one with the same pipeline type?
3. Does multi-step on the smaller model outperform single-shot on the larger model? If so, decomposition may be a cost-effective alternative to scaling model size.

## Decomposition strategy

We adopt Least-to-Most (LtM) prompting for the multi-step pipeline. LtM decomposes each question upfront into ordered sub-questions, then answers them sequentially with earlier answers carried forward as context. We chose it for its predictable token cost (N+1 calls per question), clean mapping onto our SQLite schema, and natural fit with trajectory and change-over-time questions where earlier sub-steps enable later ones.

Alternatives reviewed: Chain-of-Thought, Self-Ask, ReAct. ReAct is retained as a stretch goal. Full analysis with diagrams and a comparison table is in `decomposition_research.md`.

## Model selection

### Embedding: Qwen3-Embedding-8B

We use `Qwen3-Embedding-8B` via NEBIUS for embedding. This model is available on the NEBIUS platform and produces embeddings suitable for retrieval tasks. We considered using a local model such as `multi-qa-MiniLM-L6-cos-v1` (the course default) but chose to keep the full pipeline on NEBIUS for consistency and to avoid mismatches between local and remote embedding spaces. Embeddings are stored as float32 vectors in a local SQLite database rather than a hosted vector database, keeping all data on the local filesystem and avoiding a separate service dependency.

### Generation: Qwen3-30B-A3B and Qwen3-235B-A22B

Both models are available on NEBIUS and accessed via the OpenAI-compatible client. They use a Mixture-of-Experts (MoE) architecture where only a subset of parameters are active per token (3B active for the 30B model, 22B active for the 235B model). This means they are more cost-efficient per token than their total parameter count suggests.

We chose two sizes to test whether model capability affects the single-shot vs multi-step comparison. The brief asks whether decomposition helps, and the answer may depend on whether the model is already capable enough to handle complex questions in a single pass.

### Reranking: removed from scope

Reranking was planned but removed from scope before implementation. Running a local cross-encoder on Nuvolos would have broken the clean NEBIUS-only compute model that the pipeline otherwise maintains and introduced a local dependency the rest of the pipeline does not have. The hybrid BM25+dense retrieval with value-aware context selection proved sufficient for the benchmark question set, making the reranker unnecessary.

## PDF extraction strategy

The source documents contain dense multi-column tables — emissions intensity trajectories, target timelines, and performance summaries — that are the primary data of interest for TPI Carbon Performance questions. Extracting these tables accurately is the most consequential data preparation decision in the pipeline.

`unstructured` hi_res with the yolox layout model detects text elements, section headings, and table boundaries reliably, but extracts Table elements as flattened text strings, losing column alignment entirely. A misread column value — for example, treating a 2022 intensity figure as a 2021 figure — corrupts the generated answer directly. The alternative of treating the full document as a text stream would be worse still.

We route table pages through Gemini Vision (`gemini-2.5-flash`), which returns structured markdown output for table content and preserves column alignment in a format generation models can read directly. `gemini-2.5-flash` was chosen for cost-effectiveness; we did not benchmark other Gemini model versions. 200 DPI rasterisation was sufficient for Gemini to read table text reliably; we did not evaluate higher values. A 30-page batch limit was set because larger batches caused consistent extraction failures on long documents with dense tables — the exact failure mode was not isolated, but the cap resolved it consistently.

## Retrieval strategy

### BM25 hybrid vs pure semantic

We implement BM25 hybrid as our primary retrieval method: semantic similarity scores from the embedding model combined with BM25 keyword scores, weighted and merged into a single ranked list.

### Chunking strategy

We use fixed-size (char-limit) chunking as our baseline. 

## Intermediate storage

The brief requires intermediate results to be persisted in a database, not passed in memory. We use SQLite.

The schema is documented in CONTRIBUTING.md.

SQLite was chosen over PostgreSQL because it requires no server setup and the data volumes are small (six questions, four conditions, a few dozen sub-questions). The brief suggests SQLite is sufficient.

## Evaluation approach

We chose manual human scoring over automated LLM-as-judge evaluation. The benchmark explicitly evaluates the quality of LLM-generated answers; using an LLM as the judge would introduce the same failure modes being measured. A model that hallucinates plausible but incorrect figures could receive a high score from an automated judge exhibiting the same pattern. Human scoring eliminates this circularity. Two team members scored each run independently and reconciled disagreements.

We score each condition on four dimensions from the brief:

- **Answer correctness:** While the project brief specified scoring each answer against the ground truth, we found through the whole process that using the three proposed possibilities of "correct", "partial" and "incorrect" was not granular enough to have good comparison between each method. Instead, we chose to evaluate correctness as a fraction of the ground-truth key claims each answer matched (k/n). Because the set of key claims is fixed per question, the denominator is constant across the four cells we compare for that question (single-shot vs multi-step × 30B vs 235B), so the fractions are directly comparable within a question. Fractions are not averaged across questions because the number of key claims differs per question; cross-question summaries use a verdict tag (correct/wrong/abstained) derived from each fraction instead.
- **Answer faithfulness:** Each claim in the generated answer traced back to a retrieved source chunk. We check whether the answer is grounded in retrieved content or whether the model hallucinated.
- **Inspectability:** For multi-step conditions, we identify which sub-step produced an incorrect intermediate result. We include at least one worked example where this diagnosis mattered.
- **Latency and token cost:** Total wall-clock time and token consumption per question for each condition. Multi-step will cost more by definition, so we report the ratio honestly.

We build the reference answer set before running any pipeline condition. The ground-truth file is frozen and committed to the repository.

