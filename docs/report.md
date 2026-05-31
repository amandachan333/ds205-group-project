# Project C - Benchmark Report: Single-shot vs Multi-step RAG for Carbon Performance

> **How to use this file.** This is the argument layer. Each Results subsection cites a figure or number the analysis notebook (`analysis.ipynb`) already produces - point to it, don't recompute. Sections marked **[FILL]** are still to be written; **[TO SUPPLY]** marks a specific datum that isn't yet in any source file. The evaluation framework (Section 2.4) is being written separately.

---

## 1. Summary

**[FILL - write last.]** One short paragraph: the headline finding and the one-sentence recommendation to Sylvan, before any evidence. Everything below justifies it.

Spine to use (from the 2x2 design): does multi-step decomposition improve answer quality and/or trustworthiness over single-shot, and is the extra cost justified for TPI - including whether multi-step on the 30B beats single-shot on the 235B (decomposition as a substitute for model scale).

---

## 2. Methodology

### 2.1 Question set & classification

**Sector and companies.** Electrical Utilities sector, intensity metric = Scope 1 / MWh of electricity generated; chosen because Scope 1 is the most consistently and clearly disclosed of the available sectors. Three companies - DEWA, CenterPoint Energy, Tenaga Nasional (TNB) - each with multiple years of sustainability reports, supporting the trajectory and change-over-time question types.

**Ground truth.** Six questions were written *before* building either pipeline. Ground-truth answers were derived manually from the source PDFs, with page numbers, section titles, and chunk IDs recorded for traceability. Two team members cross-checked each answer independently before the set was frozen and committed.

**Classification (primary scheme).** Rather than the brief's three suggested types as a rigid 2+2+2, the set is classified on the two axes that actually predict performance:

1. **Retrieval scope** - single-company vs cross-company.
2. **Answer operation** - retrieve-and-state vs synthesis (trajectory / comparative) vs derived computation.

The brief's three types (Trajectory / Comparative / Change-over-time) are retained only as a secondary coverage note, applied honestly to allow hybrids.

| Q | Scope | Core operation | Brief-type (coverage) | Primary class |
|---|---|---|---|---|
| Q1 | 2 companies | Compute each company's change over time, then compare | Comparative | Comparative + trajectory (synthesis) |
| Q2 | 3 companies | Derive required annual rate per company, then rank | Comparative | Derived computation |
| Q3 | 1 company | Enumerate stated targets | (labelled Trajectory) | Retrieve-and-state |
| Q4 | 1 company | Assess multi-year intensity series + net change | Trajectory | Trajectory (synthesis) |
| Q5 | 3 companies | Compare stated targets across report vintages | Change-over-time | Change-over-time (synthesis) |
| Q6 | 3 companies | Derive actual vs required rate, judge credibility | (labelled Change-over-time) | Derived computation |

Note: Q3 lists targets with no intensity time-series; Q6 is a trajectory-credibility comparison, not a change in target wording. Both sit differently from their brief-type label, which is why the two-axis scheme is primary.

**Why this scheme carries the argument.** It maps onto the results where the brief's types do not: the derived-computation questions (Q2, Q6) score 0 for every configuration (neither pipeline did the required-rate arithmetic); the single retrieve-and-state question (Q3) is the only one all 24 runs got right (an easy control); and the synthesis questions (Q1, Q4, Q5) are exactly where multi-step weakly dominates.

### 2.2 The two pipelines

**[FILL - brief description only; full architecture lives in README/DECISIONS.]** Cover, in a few sentences each:

- **Shared stack** (held constant across all four conditions): BM25-hybrid retrieval (semantic + keyword merged), fixed-size character chunking, `Qwen3-Embedding-8B` embeddings via NEBIUS, local `ms-marco-MiniLM-L-6-v2` cross-encoder rerank, ChromaDB vector store, SQLite for intermediate persistence.
- **Single-shot baseline:** retrieve top-K -> one prompt -> one answer.
- **Multi-step:** Least-to-Most decomposition -> per-sub-question retrieval + generation -> intermediate results persisted to SQLite -> assembly call reads from the DB -> final answer.
- **2x2 design:** pipeline type x model size (Qwen3-30B-A3B, Qwen3-235B-A22B). State the three comparison questions the design answers (decomposition effect, model-size effect, and whether multi-step@30B beats single-shot@235B).

### 2.3 Decomposition design & justification

**[FILL - the brief's "core engineering challenge"; must be argued, not just described.]** Source: `decomposition_research.md`.

- **Choice:** Least-to-Most (LtM). Justify on: clean two-phase mapping onto the SQLite schema (one decomposition record, one per sub-step, one assembly record), predictable N+1 call count (budget-safe vs Self-Ask/ReAct's variable calls), natural fit with trajectory/change-over-time ordering, and the fixed question set neutralising LtM's main weakness (sub-questions planned without seeing documents can be reviewed before spending tokens).
- **Alternatives reviewed:** CoT, Self-Ask, ReAct - summarise the comparison table and why each was set aside; ReAct retained as a stretch goal.
- **Granularity trade-off (the brief's specific demand - do not skip):** address that decomposition must not be *so granular that assembly reintroduces the failure mode*. Use the live evidence: Run 39 split Q4 into **31 sub-questions** (184k tokens, the most expensive run). Discuss how sub-question count was chosen and the cost/inspectability trade-off it implies.

### 2.4 Evaluation framework

We score every run on the two axes the brief asks for - correctness and faithfulness - plus a verdict tag for cross-question aggregation, and we log latency, tokens, and cost per run. The scoring rules below are what make the Results in Section 3 defensible, so we state them up front.

**Correctness (k/n key claims).** The brief proposed a three-level label (correct / partial / incorrect). We found this too coarse to separate the four configurations cleanly, so we score correctness as the fraction of the ground-truth answer's key claims that a run matched. The set of key claims - including the headline conclusion - is fixed per question, so the denominator is constant across the four cells compared for that question (single-shot vs multi-step x 30B vs 235B) and the fractions are directly comparable within a question. A claim the run addressed but got wrong (e.g. a wrong figure) and a claim it omitted both count as not matched. The per-question denominators are: Q1 = 5, Q2 = 4, Q3 = 3, Q4 = 4, Q5 = 4, Q6 = 4.

Because the denominators differ across questions, correctness is never averaged across questions. Within-question fractions are compared directly; cross-question aggregation uses the verdict tag instead.

**Faithfulness (k/n claims grounded), with a three-way classification.** Faithfulness traces each claim in the final answer back to a retrieved chunk. We classify each claim as one of three things, not two:

- **Supported** - the claim is grounded in retrieved text.
- **Unsupported** - the claim is asserted without grounding in any retrieved chunk (the model's own error).
- **Corpus-error** - the claim faithfully reflects a retrieved chunk, but the chunk itself is wrong because of a PDF extraction artifact (a garbled table, a mislabelled year).

Corpus-error claims count as **supported** for the faithfulness score, because the model was loyal to the text it was given; the failure was upstream in extraction, not in the model. This distinction is methodologically central: collapsing corpus-error into "unsupported" would misattribute data-quality failures to model hallucination and distort the faithfulness numbers. Keeping the three-way split lets us say precisely where each numeric error came from.

**Arithmetic derivations are excluded from the faithfulness denominator.** A figure the model *computed* (e.g. a percentage reduction derived from two retrieved endpoints) is not a retrieved claim, so it cannot be traced to a chunk and is not scored for faithfulness. Only directly retrieved claims are faithfulness-scoreable. Such derivations are still assessed under correctness.

**Refusals are tagged as abstentions, not scored as wrong.** When a run declines to answer ("insufficient information"), we tag it as an abstention rather than penalising it as an incorrect answer. An honest refusal and a confidently wrong answer are very different behaviours, and the verdict tag (correct / wrong / abstained) is what captures that difference - it is where the single-shot vs multi-step distinction shows up most clearly. Abstentions typically make few claims, so they tend to score high on faithfulness by construction; this is read alongside the verdict tag, not in isolation.

**Provenance.** Correctness and faithfulness were scored against the frozen ground-truth set by a single evaluator (see Limitations). Latency, prompt/completion tokens, and cost are logged automatically per API call and summed per run.

---

## 3. Results

> Each subsection points to the corresponding `analysis.ipynb` section and its saved figure. Compare within a question; never average correctness across questions (denominators differ).

### 3.1 Correctness

Across the 24 runs, multi-step weakly dominates single-shot on correctness: it is better on Q1, Q4, and Q5, tied on the rest, and never worse. The best within-question scores by pipeline are Q1 (multi 0.60 vs single 0.40), Q4 (0.50 vs 0.25), and Q5 (0.25 vs 0.00); Q3 is tied at 1.00, while Q2 and Q6 are 0.00 for both. The advantage is real but modest - it appears on the synthesis questions and disappears wherever a question is either trivially easy (Q3) or defeated by the same obstacle for both pipelines (Q2, Q6).

![Per-question correctness by configuration](images/correctness_by_question.png)

*Per-question correctness, one panel per question; bars are the matched-claim fraction k/n, denominator in each panel title. Compare bars within a panel only. The near-identical 30B/235B pairs show that model size barely moves correctness; the blue (multi-step) bars meet or exceed the red (single-shot) bars in every panel.*

Aggregated by verdict tag (the cross-question view, since correctness fractions are not averaged), multi-step records 6 correct, 4 wrong, and 2 abstentions; single-shot records 4 correct, 3 wrong, and 5 abstentions. Model size barely shifts the mix: the 235B split is 5/3/4 and the 30B split is 5/4/3.

![Verdict composition by pipeline and by model](images/verdict_composition.png)

*Verdict composition. The decisive difference is abstention: single-shot abstains on 5 of 12 runs against multi-step's 2 of 12. Splitting by model size (right) confirms scale barely changes the picture.*

The verdict view exposes what the fractions alone hide: decomposition's main effect is to convert abstentions into committed answers. That yields both more correct answers and more wrong ones - the trustworthiness implication is taken up in Section 4.

### 3.2 Faithfulness

Faithfulness is uniformly high and similar across pipelines. Pooled across all runs it is 94.4%; by pipeline, single-shot is marginally higher at 95.5% and multi-step is 93.8%. Multi-step makes *more* claims overall at essentially the same faithfulness rate, so it is more substantive without being less grounded.

![Pooled faithfulness by pipeline](images/faithfulness.png)

*Pooled faithfulness (supported claims / total claims, with corpus-error counted as supported). Both pipelines sit in the mid-90s; the gap between them is small.*

The key reading is that the pipeline difference is **not** fabrication. The handful of sub-100% runs lose points by asserting from absence (stating a target was not set when no chunk confirms it either way), not by inventing figures. Where correctness collapses while faithfulness stays high - most visibly on the corruption-heavy Q5 - the cause is the retrieved text being wrong, not the model being unfaithful to it. This is exactly what the three-way faithfulness scheme (Section 2.4) was designed to surface.

### 3.3 Inspectability and the failure mechanism

Because multi-step persists every sub-step, a wrong final answer can be traced to the sub-step that caused it - which is the brief's inspectability requirement. The failure mechanisms of the two pipelines turn out to be different and measurable.

![Retrieval coverage vs outcome](images/retrieval_coverage.png)

*Companies retrieved (of three) against outcome. Single-shot's abstentions all occur at exactly one company retrieved - it abstains because it under-retrieves. Multi-step's failures occur at full three-company coverage - so its failures are downstream reasoning, not retrieval.*

Single-shot's abstentions every time coincide with only one company's documents reaching the prompt: it gives up because retrieval starved it of evidence. Multi-step's failures, by contrast, occur when all three companies were retrieved - the evidence was present and the breakdown was in reasoning or assembly. The two pipelines fail in different components, and the persisted intermediates let us see which. (Honest caveat: this cleanly explains single-shot's *abstentions*; its *wrong* answers are mixed - on Q5 it retrieved all three companies and was still wrong.)

Two worked cases make the diagnosis concrete:

- **Run 57 (single-shot, Q4).** The model concluded DEWA's intensity "improved consistently" since 2010 - but its own retrieved chunk shows the 2021 reversal. The contradiction is with evidence the model held, so this is a reasoning failure, not a retrieval or corpus failure.
- **Run 53 (multi-step, Q6).** The model called TNB's net-zero commitment *credible* by reading TNB's stated 5% annual *target* as an achieved trajectory. The intermediate sub-answers show exactly where the target-vs-actual confusion entered, which is the inspectability advantage in action; the ground truth finds none of the three companies credible.

### 3.4 Latency, token cost, and spend summary

Decomposition is markedly more expensive on every axis. Averaged per run, multi-step uses 59,151 tokens against single-shot's 3,171 (18.7x), takes 358s against 29s (12.5x), and costs $0.0090 against $0.0006 (14.1x).

![Latency, tokens, and cost by pipeline](images/cost_comparison.png)

*Mean latency, tokens, and cost per run by pipeline, with the multi/single multiplier in each panel title. The trustworthiness gains of Section 3.1 come at roughly an order-of-magnitude cost increase.*

Per-run cost is not uniform within multi-step: it scales with the number of sub-questions. The most expensive run was Run 39 (Q4, 30B), which decomposed the question into 31 sub-steps and consumed 183,954 tokens in a single run - a direct illustration of the granularity trade-off discussed in Section 2.3.

**Spend summary.** The 24 benchmark runs logged here consumed **747,869 tokens for $0.1160** in total (multi-step accounts for $0.108 of that, single-shot $0.008). This is the cost of the final benchmark only.

**[TO SUPPLY - reconcile against the $100 NEBIUS budget.]** The figure above is the logged benchmark spend; it does not include development and validation runs on NEBIUS. Insert the total NEBIUS drawdown against the $100 budget here - that number is not in any current source file.

---

## 4. Discussion

**Trustworthiness vs accuracy.** The headline effect of decomposition is not a large accuracy gain but a change in *behaviour*: it converts abstentions into committed answers. Single-shot abstained on 5 of 12 runs, multi-step on only 2. Those recovered answers split into both more correct and more wrong outcomes, which means decomposition's value depends on what the user wants. If the cost of a confident wrong answer is high - as it is for an assessor like TPI relying on the output - then single-shot's tendency to abstain when it has retrieved too little is a feature, not just a weakness. Multi-step's advantage is that when it does commit, the persisted sub-steps make the commitment auditable (Section 3.3), so a wrong answer can at least be caught. The trade is between an opaque pipeline that fails by going quiet and a transparent one that fails out loud but can be inspected.

**Extraction quality, not hallucination, caps numeric correctness.** The faithfulness numbers (Section 3.2) and the corruption notes in scoring together point to one conclusion: where answers are numerically wrong, the usual cause is corrupted retrieved text, not model invention. Five runs across Q1 and Q5 carry explicit corpus-corruption notes - a 345% figure where 35% was meant, TNB intensity garbled by roughly 50%, corrupted 2019 endpoints. The effect is visible in the scores: Q5, the most corruption-flagged question, collapses to 0.12 mean correctness across all four configurations while runs stay faithful to the (corrupted) text. Better extraction would most directly raise correctness on exactly these cases, and it would help both pipelines equally - it is orthogonal to the decomposition question.

**The honest boundary.** Not every failure is an extraction problem. Questions Q2 and Q6 score zero across all four configurations and carry no corruption flags. Both are derived-computation questions requiring the calculation of a required annual reduction rate and reasoning over the result, and no run, under either pipeline or either model size, performed that arithmetic. This is a genuine end-to-end limitation of the current pipelines on multi-step quantitative reasoning rather than an artifact of scoring or data quality, and decomposition did not address it. The implications for pipeline design are taken up in Section 7.

---

## 5. Recommendation to Sylvan

**Does decomposition improve answer quality?** Yes, but modestly and only on the right question types. On the synthesis questions (Q1, Q4, Q5) multi-step beats single-shot and is never worse anywhere. On the trivially easy question (Q3) both are perfect, and on the derived-computation questions (Q2, Q6) both fail completely. So decomposition helps where a question requires assembling evidence across companies or years, and does nothing where the bottleneck is either absent or is hard quantitative reasoning that neither pipeline can do.

**Does it improve trustworthiness even where accuracy is similar?** Yes, and this is the stronger case for it. Multi-step abstains far less and, more importantly, its persisted intermediate results make every answer auditable - a wrong conclusion can be traced to the sub-step that produced it (Section 3.3). For an assessor who needs to trust or check the output, that inspectability is worth more than the accuracy delta. The caveat is that decomposition also produces more confident wrong answers, so the auditability is not optional - it is what makes the extra wrong answers tolerable.

**Is the cost justified for TPI's use case?** It depends on volume and stakes. Multi-step costs roughly 14x more, runs 12-13x slower, and uses ~19x the tokens. For high-stakes, low-volume analytical questions where an answer must be defensible and checkable, the cost is justified by the inspectability. For high-volume or latency-sensitive use, single-shot's behaviour - abstaining rather than guessing when it under-retrieves - may be the safer default, with multi-step reserved for questions flagged as complex.

**On decomposition vs model scale.** The design also asked whether multi-step on the smaller model can substitute for scaling to the larger one. The evidence says model size barely matters: the 30B and 235B verdict splits are nearly identical (5/4/3 vs 5/3/4), and within-question correctness pairs are close. Pipeline choice moved results more than model size did. So the lever that matters here is the pipeline, not the model - multi-step@30B is a more sensible investment than single-shot@235B for these question types.

**Bottom line.** Adopt multi-step decomposition selectively - for complex, high-stakes Carbon Performance questions where auditability matters - and keep single-shot for simple lookups. But the single largest available gain for both pipelines is upstream: fixing PDF table extraction would lift numeric correctness more than any pipeline or model change shown here.

---

## 6. Limitations

- **Small sample.** Six questions x two pipelines x two models = 24 runs. Every number in this report is descriptive; no statistical significance is claimed or warranted, and the "weak dominance" of multi-step should be read as a direction, not a tested effect.
- **Single-rater scoring.** Correctness and faithfulness were scored against the frozen ground truth by one evaluator. There is no inter-rater reliability check, so scoring judgements - especially the supported/unsupported/corpus-error calls - carry one person's interpretation.
- **Correctness is not averaged across questions.** Denominators differ per question (3-5 key claims), so within-question fractions are the only valid comparison; cross-question aggregation uses the verdict tag instead. Any reading that averages the fractions would be invalid.
- **Corpus corruption caps numeric correctness for every configuration.** Several questions are bounded above by PDF extraction quality rather than by pipeline or model capability, so the correctness ceiling is partly an artifact of the corpus, not of the methods under test.
- **Unbalanced question set.** Under honest classification the set has one pure-trajectory question, one pure-change-over-time question, and no pure-comparative-without-time question. The comparison is therefore strongest as evidence about synthesis questions and weakest about derived computation, where both pipelines simply failed - so the conclusions generalise most safely to the synthesis case.

---

## 7. Future work

The benchmark isolates two failure modes that are distinct in cause and therefore call for independent remedies. The first is corpus corruption, in which the model reasons faithfully over retrieved text that is itself wrong because of PDF extraction artifacts. The second is the uniform failure of all four configurations on the derived-computation questions (Q2 and Q6), which carry no corruption flags and reflect a genuine limitation of the pipelines on multi-step quantitative reasoning rather than a deficiency of the data or of the scoring scheme. The two are separable: better extraction cannot fix the arithmetic failures, and a better reasoning step cannot fix corrupted inputs. Three lines of work follow.

### 7.1 A deterministic computation step for derived-computation questions

The shared cause of the Q2 and Q6 failures is that the required arithmetic - computing an annual reduction rate from retrieved endpoints and target years, then comparing rates - was delegated to the language model, which did not perform it reliably under either pipeline or model size. The proposed next step is to separate retrieval from calculation by introducing a deterministic computation stage into the multi-step pipeline. Once the relevant sub-questions have resolved the numeric inputs (base-year intensity, most recent intensity, base year, and target year), the required and actual annual reduction rates would be computed in code, in the manner of program-aided or tool-augmented generation, rather than generated as free text. This change is well aligned with the existing evaluation design: computed figures are already excluded from the faithfulness denominator (Section 2.4) on the grounds that they are not retrieved claims, and moving them to a deterministic step makes that exclusion a principled architectural boundary rather than a scoring convenience.

### 7.2 Improving table extraction to reduce corpus error

The corpus-error cases documented in Section 4 trace to a single fault: our extraction used Gemini only on pages already identified as tables, and much of the corruption arises when a table is not recognised as a table in the first place, so it bypasses Gemini and is processed as ordinary text, where its numeric content is mangled. Groups working on Projects A and B of the final project may have developed better approaches to this extraction problem. Because the fault caps numeric correctness for both pipelines equally, addressing it is orthogonal to the decomposition question and would raise the correctness ceiling for the benchmark as a whole.

### 7.3 ReAct as an additional comparison arm

The decomposition study (Section 2.3) adopted Least-to-Most prompting and deferred ReAct as a stretch goal. Implementing ReAct as a third pipeline would test whether interleaved reasoning-retrieval cycles outperform Least-to-Most's upfront decomposition on the synthesis questions, where decomposition currently shows its only advantage. This would strengthen the generality of the pipeline comparison beyond the two configurations evaluated here.

These proposals are forward-looking and are not substantiated by the present results; they are stated as the priority next steps that the current findings most directly motivate.

---

## Appendices

**[FILL / optional]** Per-run scoring table (the 24-row table from the scoring worksheet), schema reference (point to CONTRIBUTING.md), full decomposition comparison (point to `decomposition_research.md`).
