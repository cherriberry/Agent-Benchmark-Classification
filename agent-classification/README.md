# V5/r8 benchmark classification

This directory contains the latest evaluated classifier and runners. The full run completed on 2026-11-19 using the r8 English prompt. The classifier and prompt match the saved full-run SHA-256 values exactly.

## Current files

- `codes/agent-classify-v5.py`: classification, response validation, and Agent-SafetyBench trace preparation.
- `codes/run_v5_full_rerun.py`: resumable full-data runner; Claude Opus 5.5 first, GPT-6.1-SOL fallback.
- `codes/run_v5_sample30_recheck.py`: reclassifies the first 30 records from the existing 50-case manual-review sample.
- `prompts/v5-system-prompt.txt`: exact r8 system prompt.
- `metadata/classification_manifest.json`: latest full-run configuration, input/output hashes, counts, and model attribution.
- `metadata/label_counts.json` and `metadata/label_counts.csv`: latest label counts.
- `README-data-and-scoring.md`: existing classification/scoring notes, preserved including local edits. This is a historical reference: where it describes older label rules or counts, use the r8 prompt and latest metadata instead. In r8, H labels describe supported potential execution errors; an actual failed trace is not required. H4 still requires explicit return/observation evidence.

## Completed full run

| Dataset | Records | Claude Opus 5.5 | GPT-6.1-SOL | Pending | Empty labels |
|---|---:|---:|---:|---:|---:|
| Agent-SafetyBench | 2,000 | 1,704 | 296 | 0 | 0 |
| ToolEmu | 144 | 144 | 0 | 0 | 0 |

| Dataset | V1 | V2 | V3 | V4 | H1 | H2 | H3 | H4 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Agent-SafetyBench | 517 | 327 | 708 | 67 | 598 | 579 | 50 | 845 |
| ToolEmu | 1 | 1 | 118 | 0 | 137 | 143 | 3 | 0 |

These are overlapping record counts because each record can receive multiple labels. They are case-level candidate annotations, not measured model failure counts. Agent-SafetyBench inputs include the existing gpt-4o execution records; ToolEmu inputs use the original case metadata without Agent traces.

On the 30-case development sample, the r8 run matched all labels on 13/30 cases (43.33%). Micro precision, recall, and F1 were each 84.62%. These are development-set agreement metrics against local human labels, not an independent held-out accuracy estimate.

## Reproduce the full-data classification

Python 3.9+ and the OpenAI-compatible SDK are required:

```bash
python3 -m pip install -r requirements.txt
```

Provide `OPENAI_API_KEY` or `ROUTERLINK_API_KEY` in the shell environment. The code does not load credentials from files. `OPENAI_BASE_URL` can override the RouterLink endpoint.

The existing runners retain the original `/data/fuyuhan` data locations:

```text
/data/fuyuhan/agentsafetybench-data/released_data.json
/data/fuyuhan/agent-safetybench-router-results/gpt-4o/gen_res.json
/data/fuyuhan/toolemu-data/all_cases.json
```

Run from this directory, choosing a new output directory so previous labels are not reused:

```bash
python3 codes/run_v5_full_rerun.py \
  --output-dir /data/fuyuhan/classification-results/v5-agent-trace/your-new-r8-run \
  --workers 8 --max-retries 2 --request-interval 0.45 \
  --flush-every 10 --max-tokens 600 --temperature 0.0 \
  --max-record-chars 0
```

Use `--dataset agentsafetybench` or `--dataset toolemu` to run just one dataset. Reusing the same output directory resumes valid completed records. If inputs live elsewhere, update the runner's input path constants; alternatively, the base classifier CLI accepts explicit source and trace paths but does not itself provide Claude-to-GPT fallback.

The sample runner uses `/data/fuyuhan/v5-manual-review-sample-50-20261113.json` and accepts the output directory through `V5_SAMPLE30_OUT`. Both runners import the adjacent `agent-classify-v5.py`; its embedded `SYSTEM_PROMPT` is the executable prompt. Keep that value synchronized with the text file when editing the prompt.

Classification outputs remain source-shaped JSON arrays with `classification: {"failure_modes": [...]}` added to each record. The full runner also writes provenance, API-attempt logs, a prompt/code snapshot, and a manifest. No new model calls were made while packaging this repository. API/model variability means reruns can differ even with the same inputs and prompt.
