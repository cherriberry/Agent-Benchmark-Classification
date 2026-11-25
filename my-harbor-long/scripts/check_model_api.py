#!/usr/bin/env python3
"""One small streaming Chat Completions request; run inside the gptme image."""
from __future__ import annotations

import json
import os
import sys

from openai import OpenAI
from api_diagnostics import classify_error


def request_options(model: str) -> dict:
    # Mirror the sampling defaults of the pinned harness. The runtime patch
    # applies this same omission to GPT-6-Luna's full agent requests.
    from gptme.llm.llm_openai import _get_temperature, _get_top_p
    from gptme.llm.models import ModelMeta
    options = {}
    if model.rsplit("/", 1)[-1] != "gpt-6-luna":
        metadata = ModelMeta(provider="local", model=model, context=128000)
        options["temperature"] = _get_temperature("local", metadata)
        top_p = _get_top_p("local", metadata)
        if top_p is not None:
            options["top_p"] = top_p
    return options


def main():
    model = os.environ["MATRIX_API_MODEL"]
    key = os.environ["OPENAI_API_KEY"]
    try:
        client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=key,
                        timeout=60, max_retries=0)
        tools = [{"type": "function", "function": {"name": "readiness_check",
                  "description": "Confirm the test API is ready; no side effects.",
                  "parameters": {"type": "object", "properties": {}}}}]
        options = request_options(model)
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Call readiness_check once with no arguments. Do not explain."}],
            tools=tools, stream=True, stream_options={"include_usage": True}, **options)
        tool_seen = content_seen = False
        with response:
            for chunk in response:
                for choice in chunk.choices:
                    tool_seen = tool_seen or bool(choice.delta.tool_calls)
                    content_seen = content_seen or bool(choice.delta.content)
        if not tool_seen and not content_seen:
            raise ValueError("No usable assistant content or tool call in stream")
        print(json.dumps({"status": "ok", "model_id": model, "tool_call_observed": tool_seen,
                          "sampling_parameters": options, "streaming": True}))
        return 0
    except Exception as exc:
        message = str(exc).replace(key, "<redacted>")
        error = classify_error(message, getattr(exc, "status_code", None))
        print(json.dumps({"status": "failed", "model_id": model,
                          "error_type": type(exc).__name__, **error}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
