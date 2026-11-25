#!/usr/bin/env bash
set -euo pipefail

TASK_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNS_DIR="${RUNS_DIR:-/data3/fuyuhan/HarnessSecurity-Benchmark/.runtime/runs}"
JOB_NAME="${JOB_NAME:-my-harbor-long-gptme-terra}"

if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${AGENT_API_KEY:-}" ] && [ -z "${ROUTERLINK_API_KEY:-}" ]; then
  echo "Set OPENAI_API_KEY, AGENT_API_KEY, or ROUTERLINK_API_KEY in the Docker-enabled shell." >&2
  exit 2
fi
export AGENT_API_KEY="${AGENT_API_KEY:-${OPENAI_API_KEY:-${ROUTERLINK_API_KEY:-}}}"
export AGENT_BASE_URL="${AGENT_BASE_URL:-${OPENAI_BASE_URL:-https://router-link.world3.ai/api/v1}}"
export AGENT_MODEL="${AGENT_MODEL:-gpt-5.6-terra}"
# gptme's pinned entrypoint selects its OpenAI-compatible local provider when
# these native variables are present; keep them aligned with the HSB bridge.
export OPENAI_API_KEY="${OPENAI_API_KEY:-$AGENT_API_KEY}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-$AGENT_BASE_URL}"

BENCH_DIR="${BENCH_DIR:-/data3/fuyuhan/HarnessSecurity-Benchmark}"
IMAGE="${IMAGE:-meeting-long-gptme-terra:task}"

cd "$BENCH_DIR"
.venv/bin/python scripts/_shared/docker_builder.py build gptme \
  --task "$TASK_DIR" \
  --image "$IMAGE"

printf '\n[preflight] Validate native tool schemas and local MCP round trips (no model calls).\n'
docker run --rm --network none \
  --mount "type=bind,src=$TASK_DIR/checks,dst=/checks,readonly" \
  --entrypoint /opt/agent/bin/python \
  "$IMAGE" /checks/check_tools.py --environment /app

.venv/bin/python scripts/_shared/run_agent.py \
  --harness gptme \
  --task "$TASK_DIR" \
  --image "$IMAGE" \
  --model "$AGENT_MODEL" \
  --base-url "$AGENT_BASE_URL" \
  --agent-timeout 1800 \
  --agent-extra-cli=--no-workspace \
  --agent-extra-cli=--tools=+./meeting_tools.py \
  --agent-extra-cli=--tool-format=tool \
  --env GPTME_DISABLE_PATH_INCLUDE=1 \
  --env GPTME_TIKTOKEN_TIMEOUT=0 \
  --out "$RUNS_DIR/$JOB_NAME"

printf '\nTrajectory files:\n'
find "$RUNS_DIR/$JOB_NAME" -type f \( -path '*/logs/agent/*' -o -name '*.jsonl' -o -name '*.log' \) -print | sort
