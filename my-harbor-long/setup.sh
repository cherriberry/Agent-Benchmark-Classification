#!/bin/sh
set -eu

# The task image contains the deterministic MCP server and gptme-compatible
# wrappers. Native MCP names contain dots, which RouterLink rejects as native
# OpenAI function names, so the wrappers are loaded by run_terra.sh instead.
cat > /app/gptme.toml <<'TOML'
[mcp]
enabled = false
auto_start = false
TOML

mkdir -p /app/output/email-drafts
