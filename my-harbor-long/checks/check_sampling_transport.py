#!/usr/bin/env python3
"""Capture actual pinned-gptme HTTP bodies with an in-memory mock transport.

Run only in a disposable, network-disabled task container. No real model calls.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import httpx
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from patch_gptme_sampling import main as apply_patch
from check_model_api import request_options


def main():
    apply_patch()
    import gptme.llm.llm_openai as transport
    import gptme.llm.models as models
    from gptme.llm.models import ModelMeta
    importlib.reload(transport)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: [DONE]\n\n")

    client = OpenAI(api_key="offline-fixture", base_url="https://offline.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    transport.get_client = lambda provider: client
    transport._prepare_messages_for_api = lambda *args: ([{"role": "user", "content": "fixture"}], None)
    for model in ("gpt-6-luna", "vendor/gpt-6-luna", "deepseek-v4-flash", "qwen3.8-27b", "kimi-k2.7-code", "claude-sonnet-5"):
        models.get_model = lambda *args, **kwargs: ModelMeta(provider="local", model=model, context=128000)
        list(transport.stream([], "local/" + model, None))
        request = captured[-1]
        assert request["model"] == model, request
        omitted = model.rsplit("/", 1)[-1] == "gpt-6-luna"
        assert ("top_p" not in request) is omitted, request
        assert ("temperature" not in request) is omitted, request
        assert {key: request[key] for key in ("top_p", "temperature") if key in request} == request_options(model)
    print(json.dumps({"captured_requests": len(captured), "gpt6_sampling_omitted": True,
                      "other_models_unchanged": True, "api_probe_matches_harness_sampling": True,
                      "network": "none", "real_model_calls": 0}))


if __name__ == "__main__":
    main()
