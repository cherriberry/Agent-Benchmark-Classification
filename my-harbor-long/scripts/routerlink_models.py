#!/usr/bin/env python3
"""Run inside the existing gptme image; use its installed OpenAI SDK."""
import json
import os
import sys

from openai import OpenAI


def main():
    try:
        client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"],
                        api_key=os.environ["OPENAI_API_KEY"], timeout=30, max_retries=0)
        models = client.models.list()
        print(json.dumps({"status": "ok", "model_ids": sorted({model.id for model in models})}))
        return 0
    except Exception as exc:
        # Never print a client object, environment, raw HTTP headers or credentials.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "http_status": getattr(exc, "status_code", None)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
