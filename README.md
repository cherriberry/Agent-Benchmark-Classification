# V5 benchmark classification

This repository contains the V5 classifier and the resumable runner. It intentionally excludes the full benchmark JSON files, Agent trace data, API attempt logs, and classified outputs. Those remain under `/data/fuyuhan` outside Git.

## Files

- `codes/agent-classify-v5.py`: V5 classifier, JSON validation, and Agent-SafetyBench trace preparation.
- `codes/run_v5_full_rerun.py`: full-run driver with Claude-first classification, GPT-6.1-SOL fallback, rate limiting, and checkpoints.
- `prompts/v5-system-prompt.txt`: exact prompt used for this run.
- `metadata/classification_manifest.json`: run provenance and source hashes.
- `metadata/label_counts.json` and `metadata/label_counts.csv`: non-sensitive summary statistics.

## Run

```bash
PYTHONPATH=/data/fuyuhan/mirage-bench-agent/.deps \
/usr/bin/python3 /data/fuyuhan/v5-classification-repo/codes/run_v5_full_rerun.py \
  --workers 8 --max-retries 2 --request-interval 0.5
```

The runner reads the source files from their existing `/data/fuyuhan` locations and writes results to a separate V5 result directory. It requires `OPENAI_API_KEY` or `ROUTERLINK_API_KEY` in the environment and never reads keys from files.

## V5 rules

The prompt requires at least one V1–V4 or H1–H4 label per record, prioritizes V1/V2/V4 before strict V3, prohibits using V3 as a generic fallback, and requires explicit evidence for H4. V5 does not use H5.

## Completed run

- Agent-SafetyBench: 2,000/2,000 records; 0 empty label arrays.
- ToolEmu: 144/144 records; 0 empty label arrays.
- Agent-SafetyBench V3: **703** records, below the requested 800–900 target. The count was not artificially adjusted; it reflects the stricter V5 prompt.
- Agent-SafetyBench result: `/data/fuyuhan/classification-results/v5-agent-trace/strict-nonempty-claude-then-gpt6.1-sol-20261112/agentsafetybench_classified.json`
- ToolEmu result: `/data/fuyuhan/classification-results/v5-agent-trace/strict-nonempty-claude-then-gpt6.1-sol-20261112/toolemu_classified.json`

See `metadata/label_counts.json` for all label counts and hashes.
