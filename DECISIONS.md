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

We use `Qwen3-Embedding-8B` via NEBIUS for embedding. This model is available on the NEBIUS platform and produces embeddings suitable for retrieval tasks. We considered using a local model such as `multi-qa-MiniLM-L6-cos-v1` (the course default) but chose to keep the full pipeline on NEBIUS for consistency and to avoid mismatches between local and remote embedding spaces.

**To validate:** We will compare Recall@5 between the NEBIUS embedding model and a local MiniLM baseline on a subset of questions. If the difference is negligible, we may revert to MiniLM to save budget.

### Generation: Qwen3-30B-A3B and Qwen3-235B-A22B

Both models are available on NEBIUS and accessed via the OpenAI-compatible client. They use a Mixture-of-Experts (MoE) architecture where only a subset of parameters are active per token (3B active for the 30B model, 22B active for the 235B model). This means they are more cost-efficient per token than their total parameter count suggests.

We chose two sizes to test whether model capability affects the single-shot vs multi-step comparison. The brief asks whether decomposition helps, and the answer may depend on whether the model is already capable enough to handle complex questions in a single pass.

### Reranking: local cross-encoder

NEBIUS does not offer reranker models. We run `cross-encoder/ms-marco-MiniLM-L-6-v2` locally on Nuvolos. This is the model taught in 🖥️ W10 Lecture.

One team member found during PS2 that adding this cross-encoder on top of BM25 hybrid retrieval actually hurt ranking quality. We suspect this is because the model (22M parameters, trained on web search passages) is too small to improve on BM25 scores for domain-specific climate text. We plan to test this and report the result. If reranking does not help, we drop it and save the pipeline complexity.

## Retrieval strategy

### BM25 hybrid vs pure semantic

We implement BM25 hybrid as our primary retrieval method: semantic similarity scores from the embedding model combined with BM25 keyword scores, weighted and merged into a single ranked list.

**Alternative considered:** Pure semantic retrieval (embedding-only). We keep this as a fallback and will report Recall@5 for both configurations.

**Alternative considered:** BM25 hybrid followed by cross-encoder reranking. As noted above, initial testing suggested the small cross-encoder hurts results on climate text. We test this formally and report the finding.

### Chunking strategy

We use fixed-size (char-limit) chunking as our baseline. This was the simplest strategy and won the W10 NB00 benchmark when combined with reranking.

**Alternative considered:** Heading-delimited chunking (Strategy B from the course). We may test this if fixed-size retrieval performs poorly on our Electrical Utilities PDFs, but we start simple.

## Intermediate storage

The brief requires intermediate results to be persisted in a database, not passed in memory. We use SQLite.

The schema stores:

- **runs:** each pipeline execution (timestamp, model, pipeline type, question ID)
- **sub_results:** for the multi-step pipeline, each sub-question's retrieval context and generated answer
- **final_answers:** the assembled answer for each question under each condition
- **token_usage:** prompt tokens, completion tokens, and wall-clock time per API call

SQLite was chosen over PostgreSQL because it requires no server setup and the data volumes are small (six questions, four conditions, a few dozen sub-questions). The brief suggests SQLite is sufficient.

**Schema will be documented in `CONTRIBUTING.md` once finalised.**

## Evaluation approach

We score each condition on four dimensions from the brief:

- **Answer correctness:** Each answer scored against ground truth as correct, partially correct, or incorrect. Two team members score independently and reconcile disagreements.
- **Answer faithfulness:** Each claim in the generated answer traced back to a retrieved source chunk. We check whether the answer is grounded in retrieved content or whether the model hallucinated.
- **Inspectability:** For multi-step conditions, we identify which sub-step produced an incorrect intermediate result. We include at least one worked example where this diagnosis mattered.
- **Latency and token cost:** Total wall-clock time and token consumption per question for each condition. Multi-step will cost more by definition, so we report the ratio honestly.

We build the reference answer set before running any pipeline condition. The ground-truth file is frozen and committed to the repository.

