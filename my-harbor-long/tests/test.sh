#!/bin/sh
set -eu
# HSB runs here against the full live /app; Harbor collects all declared sources.
exec python3 /tests/run_verifiers.py --complete-snapshot
