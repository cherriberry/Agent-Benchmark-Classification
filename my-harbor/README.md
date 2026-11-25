# Meeting Minutes and Email Harbor Task

This task uses a Harbor task package and a local stdio MCP server. Personnel names, roles, and email addresses are returned by `get_authorized_recipients`; they are intentionally not stored in `attendees.csv`. Email delivery is simulated and recorded in `/app/mock-mailbox.jsonl`.

## Run with the HarnessSecurity-Bench harness

`run_terra.sh` builds a task-specific image from the pinned HSB `gptme` harness (`076e433cccad3a0f51e6d87464c6e72033933a17`) and runs it with `gpt-5.6-terra`. It loads valid-name gptme wrappers that forward to the task MCP server, uses native tool calling, and disables automatic prompt file expansion. Run it from a shell that can access Docker:

```bash
cd /data/fuyuhan/my-harbor
./run_terra.sh
```

The runner records the gptme trace under `/data3/fuyuhan/HarnessSecurity-Benchmark/.runtime/runs/my-harbor-gptme-terra/`, normally in `*/logs/agent/agent.log`. The same run directory contains the captured `/app` workspace and verifier logs.

Before starting the agent, the launcher runs `checks/check_tools.py` in a disposable,
network-disabled container. It validates the native JSON Schema produced by the
pinned gptme code, including its default runnable tools, and exercises all eight
meeting tools through the real stdio MCP server using temporary files and a
simulated mailbox. This check makes no model requests and does not populate the
evaluation's outputs. It validates infrastructure, not model behavior or acceptance
by the RouterLink API.

The wrappers explicitly use JSON Schema `string` parameters with string descriptions.
The pinned `ToolSpec.from_function()` alone emits Python `str` and null descriptions,
which produce invalid tool schemas. The wrappers retain its execution dispatcher.
Absolute paths, draft storage under `/app/output/email-drafts`, and audit event logging
are also handled by the local server. The task instruction and input fixtures are unchanged.

`GPTME_TIKTOKEN_TIMEOUT=0` avoids repeated vocabulary-download timeouts. Local token
counts may be approximate; provider-reported usage remains the measurement to use.
