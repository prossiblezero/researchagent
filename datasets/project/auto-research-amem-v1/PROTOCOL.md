# A-MEM / LoCoMo development protocol v1

This is a project research campaign using an official algorithm implementation and an official-data subset. It is not the published A-MEM score or a complete LoCoMo leaderboard evaluation.

## Frozen inputs and baseline

- `data/locomo/tasks.json`: all 40 development questions, categories 1–4, seed `[13]`.
- `data/locomo/corpus.json`: complete conversations conv-26 and conv-30, 788 turns. Preserve chronological order, speakers, timestamps and image captions. The upstream `load_dataset.parse_conversation` implements caption handling.
- `upstream/`: official WujiangXu/AgenticMemory revision `0c8039f28fdcc08189a23c07a3437d9d2482f9c2`, including the robust memory and QA implementation, parsers and MIT license. This directory must be protected in every delegation.
- `paper/page-01.txt` through `paper/page-28.txt`: text layer of arXiv 2502.12110; the complete PDF is also in the research-space library. Text extraction does not establish that figures/formula layout were verified.
- Host-only answer labels are absent from this workspace. Category 5 is excluded because the upstream QA method uses the reference answer as a multiple-choice option. Pass an empty reference argument for categories 1–4.
- Holdout conv-41/conv-42 and their 40 questions are not staged. Do not access/download other benchmark copies. Additional source downloads are disabled for this frozen evaluation; dependency preparation remains available.

Use the official robust classes, MiniLM embeddings, memory analysis, conditional strengthening/neighbor updates, consolidation, query generation and answer prompts. Preserve `memory_layer_robust.py` and the baseline algorithm. Adapter code may replace the LLM transport factory. The QA class in `test_advanced_robust.py` can be extracted unchanged with Python AST to avoid that module's import-time downloads and unrelated metric models; record this adaptation explicitly. Do not substitute keyword-only memory or omit evolution to finish faster.

The local embedding model is `D:/paper/researchagent-models/all-MiniLM-L6-v2/1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Resolve the upstream `all-MiniLM-L6-v2` alias to this same local path, including consolidation. Set `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `TOKENIZERS_PARALLELISM=false` and `LITELLM_LOCAL_MODEL_COST_MAP=True` before imports. The host disables automatic dotenv loading. The prepared `.venv` includes the needed libraries; do not create another environment. The native process tree remains bounded to 4 GiB / 32 processes with networking disabled.

## Model exchange and resumable state

Declare `model_requests={"mode":"stdio","model_id":"sudocode-luna","max_requests":...,"seconds":...}`. All commands in that delegation share this model budget. Print `RESEARCH_MODEL_REQUEST ` followed by JSON `{id,messages}`, flush, then read one JSON line from stdin. IDs are unique within each command; replies contain `{id,model_id,content,usage}`. No credentials are provided. Do not catch host protocol/budget failures and silently claim a completed baseline.

Stdio v1 does not forward per-request temperature/max_tokens. Report actual provider defaults as a deviation from upstream generation settings; use the same model settings for baseline/candidate/ablation. Do not claim an exact numerical replication of the paper.

Complete memory building can take hours. Give commands and model calls enough time, print compact progress, and save completed turn state to JSON plus numpy arrays (`allow_pickle=False`) with conversation, method/code, model and parameter hashes. Only reuse caches produced by this campaign with matching provenance. Preserve conditional evolution and the 100-evolution consolidation behavior. An observation timeout does not imply process failure. Do not load external pickle caches.

## Prediction and host scoring contract

Each command writes a separate result JSON, for example:

```json
{
  "metrics": {"predictions_written": 40},
  "config": {
    "dataset": "locomo", "dataset_version": "SHA256_OF_DATA_TASKS_FILE",
    "split": "development", "seeds": [13],
    "parameters": {"method": "official-robust-adapter", "retrieve_k": 10}
  },
  "diagnostics": {"predictions_path": "results/baseline-predictions.jsonl"}
}
```

The predictions file has one row per task and seed:

```json
{"task_id":"conv-26/qa-052","seed":13,"answer":"MODEL_OUTPUT","evidence_ids":["D1:1"]}
```

`evidence_ids` is the ordered, deduplicated dialogue IDs of the actual initial retrieval, not gold labels or invented citations. Instrument the existing retriever to observe its selected indices without changing the query, scores or official answer context. Record neighborhood-expanded context IDs separately when useful. Host checks IDs belong to the question's conversation and scores initial retrieval at k=5/10.

The host computes `answer_f1` using upstream `utils.py` set-token F1 (lowercase, replace `. , ! ?` with spaces), `exact_match` using lowercase stripped text, and evidence recall as the fraction of gold dialogue IDs recovered. All 40 questions count for answer quality; 39 have fully mapped evidence and count for evidence recall. Empty answers score zero. Incomplete or duplicate predictions invalidate the measurement; failed predictions remain archived. Script-supplied scores cannot satisfy research goals. Runtime and token costs come from host receipts, not script self-reporting. No BERTScore/METEOR score is claimed when those scorers were not run.

## Research sequence

Read original evidence and inspect the pinned implementation; save the baseline plan; delegate the adapter to Codex; run the complete baseline. Then use actual failure examples and host measurements to formulate a falsifiable method change, save a new plan, implement it and its ablation, and compare the same 40 questions. Keep baseline, data and scoring frozen. Record results even when the candidate degrades. Freeze the selected implementation before requesting a separate holdout run; development gains alone do not prove generalization or novelty.
