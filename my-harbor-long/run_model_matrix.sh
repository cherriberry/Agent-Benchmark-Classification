#!/usr/bin/env bash
set -euo pipefail
TASK_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="${BENCH_DIR:-/data3/fuyuhan/HarnessSecurity-Benchmark}"
PYTHON="${MATRIX_PYTHON:-$BENCH_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  echo "Python not found: $PYTHON. Set MATRIX_PYTHON to Python 3.11 or newer." >&2
  exit 2
fi
exec "$PYTHON" "$TASK_DIR/scripts/model_matrix.py" "$@"
