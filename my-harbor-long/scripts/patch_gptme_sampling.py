#!/usr/bin/env python3
"""Narrow request-compatibility patch applied only inside disposable containers.

Pinned gptme treats an unknown local GPT-6-Luna as a generic sampler. RouterLink
rejects top_p for this ID; leave both optional sampling parameters at server
defaults. All other models, messages, tools and the agent loop stay unchanged.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path

ORIGINAL = "if not is_reasoner:"
REPLACEMENT = 'if not is_reasoner and api_model.split("@", 1)[0].rsplit("/", 1)[-1] != "gpt-6-luna":'


def patch_source(source: str) -> str:
    if source.count(REPLACEMENT) == 4 and ORIGINAL not in source:
        return source
    if source.count(ORIGINAL) != 4:
        raise ValueError("Unexpected gptme source layout; refusing to patch an unverified implementation")
    tree = ast.parse(source)
    targets = [node for node in ast.walk(tree) if isinstance(node, ast.If)
               and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
               and isinstance(node.test.operand, ast.Name) and node.test.operand.id == "is_reasoner"]
    if len(targets) != 4 or any('["temperature"]' not in ast.get_source_segment(source, node.body[0]) for node in targets):
        raise ValueError("Sampling guards changed; refusing a broad source replacement")
    updated = source.replace(ORIGINAL, REPLACEMENT)
    compile(updated, "llm_openai.py", "exec")
    return updated


def main():
    spec = importlib.util.find_spec("gptme.llm.llm_openai")
    if spec is None or spec.origin is None:
        raise RuntimeError("Pinned gptme OpenAI transport not found")
    path = Path(spec.origin)
    before = path.read_bytes()
    after = patch_source(before.decode("utf-8")).encode("utf-8")
    if after != before:
        path.write_bytes(after)
    print(json.dumps({"compatibility_patch": "gpt-6-luna-default-sampling-v1",
                      "omitted_parameters": ["temperature", "top_p"], "changed": before != after,
                      "source": str(path), "before_sha256": hashlib.sha256(before).hexdigest(),
                      "after_sha256": hashlib.sha256(after).hexdigest()}))


if __name__ == "__main__":
    main()
