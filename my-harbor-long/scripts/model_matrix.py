#!/usr/bin/env python3
"""Run the unchanged Aurora task with configured models through HSB's pinned gptme.

Host dependencies: Python 3.11+, Docker, the existing HSB checkout and task image.
Discovery and small API probes use the OpenAI SDK already in that image.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

from api_diagnostics import from_trace

TASK = Path(__file__).resolve().parents[1]
DEFAULT_BENCH = Path("/data3/fuyuhan/HarnessSecurity-Benchmark")
DEFAULT_IMAGE = "meeting-long-gptme-terra:task"
DEFAULT_URL = "https://router-link.world3.ai/api/v1"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value, None
    except (OSError, ValueError) as exc:
        return {}, f"{path.name}: {type(exc).__name__}"


def normalized(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def resolve_model(spec, available):
    """Never fall back to another model generation, dated variant or size."""
    if spec.get("model_id"):
        return dict(spec, resolution="explicit", candidates=[])
    target = normalized(spec["requested_name"])
    candidates = sorted({model for model in available
                         if normalized(model) == target or normalized(model.rsplit("/", 1)[-1]) == target})
    return dict(spec, model_id=candidates[0] if len(candidates) == 1 else None,
                resolution="resolved" if len(candidates) == 1 else ("ambiguous" if candidates else "not_found"),
                candidates=candidates)


def load_specs(path, overrides, selected, key_overrides=(), url_overrides=()):
    specs = json.loads(path.read_text(encoding="utf-8"))["models"]
    if not specs or len({s["slug"] for s in specs}) != len(specs):
        raise ValueError("Model slugs must be nonempty and unique")
    for spec in specs:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", spec["slug"]):
            raise ValueError("Invalid model slug")
        if not isinstance(spec.get("requested_name"), str) or not spec["requested_name"].strip():
            raise ValueError("Each model needs a requested_name")
    by_slug = {spec["slug"]: spec for spec in specs}
    for override in overrides:
        slug, separator, model_id = override.partition("=")
        if not separator or slug not in by_slug or not model_id.strip():
            raise ValueError("--model-id must be known-slug=exact-router-model-id")
        by_slug[slug]["model_id"] = model_id
    for field, values in (("api_key_env", key_overrides), ("base_url_env", url_overrides)):
        for override in values:
            slug, separator, name = override.partition("=")
            if not separator or slug not in by_slug or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                raise ValueError(f"{field} must be known-slug=ENVIRONMENT_VARIABLE_NAME (never a key value)")
            by_slug[slug][field] = name
    for spec in specs:
        for field in ("api_key_env", "base_url_env"):
            if spec.get(field) is not None and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", spec[field]):
                raise ValueError(f"Invalid environment variable name in {field}")
        model_id = spec.get("model_id")
        if model_id is not None and (not isinstance(model_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*", model_id)):
            raise ValueError("Invalid model ID (whitespace, shell characters and @ routing suffixes are unsupported)")
    if selected:
        if set(selected) - by_slug.keys():
            raise ValueError("Unknown --models slug")
        specs = [spec for spec in specs if spec["slug"] in selected]
    return specs


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError("Use an HTTP(S) base URL without credentials or a query string")
    return url


def provider_settings(spec, env, default_key, default_url):
    key_name, url_name = spec.get("api_key_env"), spec.get("base_url_env")
    if key_name and not env.get(key_name):
        raise ValueError(f"Export the configured credential variable {key_name}")
    if url_name and not env.get(url_name):
        raise ValueError(f"Export the configured endpoint variable {url_name}")
    url = validate_url(env[url_name] if url_name else default_url)
    if url.rstrip("/") != default_url.rstrip("/") and not key_name:
        raise ValueError("A separate model endpoint requires an explicit --model-key-env to avoid sending the default key to another service")
    key = env[key_name] if key_name else default_key
    if not key:
        raise ValueError("No API credential configured for this model")
    return key, url


def stage_runtime_task(destination):
    """Copy only task inputs; patch the transport in each disposable container."""
    destination.mkdir(parents=True)
    for name in ("instruction.md", "task.toml"):
        shutil.copy2(TASK / name, destination / name)
    for name in ("environment", "tests"):
        shutil.copytree(TASK / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    patch = (TASK / "scripts/patch_gptme_sampling.py").read_text(encoding="utf-8")
    setup = (TASK / "setup.sh").read_text(encoding="utf-8")
    setup += "\n# Runtime-only compatibility for GPT-6-Luna; original task remains intact.\n"
    setup += "/opt/agent/bin/python - <<'AURORA_SAMPLING_PATCH'\n" + patch + "\nAURORA_SAMPLING_PATCH\n"
    (destination / "setup.sh").write_text(setup, encoding="utf-8")
    return destination


def probe_api(image, model_id, key, url):
    env = os.environ.copy()
    env.update(OPENAI_API_KEY=key, OPENAI_BASE_URL=url, MATRIX_API_MODEL=model_id)
    container = "aurora-api-probe-" + uuid.uuid4().hex[:16]
    command = ["docker", "run", "--rm", "--name", container,
               "-e", "OPENAI_API_KEY", "-e", "OPENAI_BASE_URL", "-e", "MATRIX_API_MODEL",
               "--mount", f"type=bind,src={TASK / 'scripts'},dst=/matrix-scripts,readonly",
               "--entrypoint", "/opt/agent/bin/python", image, "/matrix-scripts/check_model_api.py"]
    try:
        process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or result.get("status") not in ("ok", "failed"):
            raise ValueError("Invalid API preflight result")
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "category": "preflight_execution_error", "error_type": type(exc).__name__,
                "suggested_action": "检查 Docker/网络，或用 --skip-api-preflight 直接运行并保留 agent.log。"}
    finally:
        try:
            subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            print(f"[warning] Check cleanup of temporary API probe container {container}", file=sys.stderr)


def trial_environment(base, model_id, key, url, max_steps):
    env = base.copy()
    # HSB's gptme launch command strips one leading component. Add a transport
    # prefix, so vendor-prefixed API IDs survive intact: routerlink/a/b -> local/a/b.
    env.update(AGENT_MODEL="routerlink/" + model_id, AGENT_API_KEY=key,
               OPENAI_API_KEY=key, AGENT_BASE_URL=url, OPENAI_BASE_URL=url,
               GPTME_MAX_STEPS=str(max_steps))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "LLM_PROXY", "GPTME_MODEL"):
        env.pop(name, None)
    return env


def trial_command(bench, task, image, model_id, url, out, attempts, timeout):
    return [str(bench / ".venv/bin/python"), str(bench / "scripts/_shared/run_agent.py"),
            "--harness", "gptme", "--task", str(task), "--image", image,
            "--model", "routerlink/" + model_id, "--base-url", url,
            "--agent-timeout", str(timeout), "--attempts", str(attempts),
            "--agent-extra-cli=--no-workspace", "--agent-extra-cli=--tools=+./meeting_tools.py",
            "--agent-extra-cli=--tool-format=tool", "--env", "GPTME_DISABLE_PATH_INCLUDE=1",
            "--out", str(out), "--flat-output"]


def fingerprint(task, bench):
    paths = [task / name for name in ("instruction.md", "task.toml", "setup.sh")]
    paths.extend(p for folder in ("environment", "tests") for p in (task / folder).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts)
    paths.extend(bench / "scripts/_shared" / name for name in ("run_agent.py", "harness/gptme/harness.json"))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def discover(image, env):
    command = ["docker", "run", "--rm", "-e", "OPENAI_API_KEY", "-e", "OPENAI_BASE_URL",
               "--mount", f"type=bind,src={TASK / 'scripts/routerlink_models.py'},dst=/matrix-models.py,readonly",
               "--entrypoint", "/opt/agent/bin/python", image, "/matrix-models.py"]
    try:
        process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=75)
        result = json.loads(process.stdout)
        if not isinstance(result, dict):
            raise ValueError("Invalid discovery response")
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error_type": type(exc).__name__, "model_ids": []}


def check_image(bench, image, rebuild, out):
    inspection = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                capture_output=True, text=True)
    if rebuild or inspection.returncode:
        print("[build] Preparing the shared gptme task image; see build.log", flush=True)
        command = [str(bench / ".venv/bin/python"), str(bench / "scripts/_shared/docker_builder.py"),
                   "build", "gptme", "--task", str(TASK), "--image", image]
        with (out / "build.log").open("w") as log:
            process = subprocess.run(command, cwd=bench, stdout=log, stderr=subprocess.STDOUT)
        if process.returncode:
            raise RuntimeError("Task image build failed; see build.log")
        inspection = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                    capture_output=True, text=True, check=True)
    # An old image must not silently substitute different task fixtures or tools.
    verify_source = """from pathlib import Path
expected=Path('/task/environment')
for p in expected.rglob('*'):
    if p.is_file() and p.name!='Dockerfile' and '__pycache__' not in p.parts:
        actual=Path('/app')/p.relative_to(expected)
        if not actual.is_file() or actual.read_bytes()!=p.read_bytes():
            raise SystemExit('Task image mismatch: '+str(p.relative_to(expected))+'; rerun with --rebuild')
print('Task image sources match current fixtures and wrappers')
"""
    base = ["docker", "run", "--rm", "--network", "none", "--mount",
            f"type=bind,src={TASK},dst=/task,readonly", "--entrypoint", "/opt/agent/bin/python", image]
    with (out / "tool-preflight.log").open("w") as log:
        for arguments in (["-c", verify_source], ["/task/checks/check_tools.py", "--environment", "/app"]):
            result = subprocess.run(base + arguments, stdout=log, stderr=subprocess.STDOUT, timeout=120)
            if result.returncode:
                raise RuntimeError("Offline task/tool preflight failed; see tool-preflight.log (try --rebuild for image mismatch)")
    return inspection.stdout.strip()


def summarize_trial(trial, spec):
    manifest, manifest_error = read_json(trial / "manifest.json")
    security, security_error = read_json(trial / "logs/verifier/safety-oracle.json")
    legacy, legacy_error = read_json(trial / "logs/verifier/results.json")
    issues = [error for error in (manifest_error, security_error, legacy_error) if error]
    events, event_errors = [], []
    try:
        for number, line in enumerate((trial / "app/mcp-events.jsonl").read_text().splitlines(), 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Expected event object")
                events.append(event)
            except ValueError:
                event_errors.append(f"mcp-events.jsonl: invalid line {number}")
    except OSError:
        event_errors.append("mcp-events.jsonl: missing")
    issues.extend(event_errors)
    exit_code = manifest.get("agent_exit_code")
    if manifest_error or manifest.get("status") != "completed":
        status = "infrastructure_error"
    elif str(exit_code) != "0":
        status = "agent_error"
    elif security_error or legacy_error:
        status = "verifier_error"
    elif security.get("security_violation") is None:
        status = "verifier_unknown"
    else:
        status = "completed"
    try:
        error_diagnosis = from_trace((trial / "logs/agent/agent.log").read_text(encoding="utf-8"))
    except OSError:
        error_diagnosis = None
    return {
        "requested_name": spec["requested_name"], "model_id": spec.get("model_id"),
        "slug": spec["slug"], "trial": str(trial), "status": status, "agent_exit_code": exit_code,
        "legacy_reward": legacy.get("reward"), "utility": legacy.get("metrics", {}).get("utility"),
        "security_violation": security.get("security_violation"),
        "safety_reward": security.get("safety_reward"),
        "unsafe_attempt_detected": security.get("unsafe_attempt_detected"),
        "model_behavior_safe": security.get("model_behavior_safe"),
        "violations": security.get("change", []), "security_status": security.get("status", "unknown"),
        "task_tool_calls": None if event_errors else sum(e.get("event") in ("executed", "blocked") for e in events),
        "trace": str(trial / "logs/agent/agent.log"),
        "safety_details": str(trial / "logs/verifier/safety-oracle.json"),
        "error_diagnosis": error_diagnosis,
        "evidence_issues": issues + security.get("evidence_issues", []),
    }


def summarize(out):
    state, error = read_json(out / "matrix.json")
    if error:
        raise ValueError("Cannot read matrix.json in the requested batch directory")
    rows = []
    for spec in state["models"]:
        manifests = sorted((out / spec["slug"] / "trials").glob("*/manifest.json"))
        for manifest in manifests:
            rows.append(summarize_trial(manifest.parent, spec))
        for missing_attempt in range(len(manifests), state.get("attempts", 1)):
            rows.append({"requested_name": spec["requested_name"], "model_id": spec.get("model_id"),
                         "slug": spec["slug"], "status": spec.get("status", "not_run"),
                         "missing_attempt": missing_attempt + 1,
                         "security_violation": None, "safety_reward": None, "unsafe_attempt_detected": None,
                         "model_behavior_safe": None, "legacy_reward": None, "utility": None,
                         "task_tool_calls": None, "violations": [], "trial": None,
                         "reason": spec.get("reason"), "runner_log": str(out / spec["slug"] / "runner.log")})
    evaluated = [r for r in rows if r["status"] == "completed" and r["security_violation"] is not None]
    report = {"task": state["task"], "harness": "gptme", "batch_status": state["status"],
              "completed_evaluable_trials": len(evaluated),
              "trials_with_observed_violations": sum(r["security_violation"] is True for r in evaluated),
              "rows": rows,
              "interpretation": "One attempt per model is a smoke test, not a safety rate estimate. Failures and unknowns are not safe passes. Tool-blocked attempts differ from delivered effects."}
    write_json(out / "report.json", report)
    columns = ["requested_name", "model_id", "status", "utility", "legacy_reward", "security_violation",
               "safety_reward", "unsafe_attempt_detected", "model_behavior_safe", "task_tool_calls", "trial"]
    with (out / "report.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Aurora 多模型评测", "", "同一案例、同一 gptme harness；邮箱操作均为本地模拟。", "",
             "| 模型 | 运行状态 | utility | 原 reward | 安全分 | 越权尝试 | 任务工具调用 |",
             "|---|---|---:|---:|---:|---|---:|"]
    def cell(value):
        return "未知" if value is None else str(value).replace("|", "\\|")
    for row in rows:
        lines.append("| " + " | ".join(cell(row.get(k)) for k in (
            "requested_name", "status", "utility", "legacy_reward", "safety_reward", "unsafe_attempt_detected", "task_tool_calls")) + " |")
    lines += ["", "每模型一次只是冒烟测试，不能据此估算安全失败率。运行错误与证据不足不计作安全通过。",
              "安全分衡量违规是否实际发生；被工具阻止的越权尝试需单独查看。", ""]
    for row in rows:
        lines += [f"## {row['requested_name']}", "", f"API model ID: `{row.get('model_id')}`", ""]
        if row.get("trace"):
            lines += [f"- Trace: [{Path(row['trace']).name}]({row['trace']})",
                      f"- 安全证据: [safety-oracle.json]({row['safety_details']})",
                      f"- 已观察到的违规: {', '.join(row['violations']) or '无已确认项（结合运行状态判断）'}", ""]
            if row.get("error_diagnosis"):
                diagnosis = row["error_diagnosis"]
                lines += [f"调用错误：`{diagnosis['category']}` — {diagnosis['message']}", "",
                          diagnosis["suggested_action"], ""]
        elif row.get("reason"):
            lines += [str(row["reason"]), ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=TASK / "models.json")
    parser.add_argument("--models", nargs="+", help="Select model slugs from models.json")
    parser.add_argument("--model-id", action="append", default=[], help="slug=exact-provider-ID; repeatable")
    parser.add_argument("--model-key-env", action="append", default=[], help="slug=API_KEY_ENV_NAME; value stays in environment")
    parser.add_argument("--model-base-url-env", action="append", default=[], help="slug=BASE_URL_ENV_NAME; pair with --model-key-env")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--agent-timeout", type=int, default=1800)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--image", default=os.environ.get("IMAGE", DEFAULT_IMAGE))
    parser.add_argument("--bench", type=Path, default=Path(os.environ.get("BENCH_DIR", str(DEFAULT_BENCH))))
    parser.add_argument("--out", type=Path, help="New batch directory; never reuse an existing directory")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--list-models", action="store_true", help="Only discover model IDs; no model inference")
    parser.add_argument("--check-api", action="store_true", help="Only run a small streaming tool-call probe for each model; no benchmark task")
    parser.add_argument("--skip-api-preflight", action="store_true", help="Skip the small per-model API request before the benchmark")
    parser.add_argument("--plan", action="store_true", help="Print offline configuration; no Docker/API calls")
    parser.add_argument("--summarize", type=Path, help="Rebuild reports from a batch, with no model/API calls")
    args = parser.parse_args()
    if args.summarize:
        report = summarize(args.summarize.resolve())
        print(f"Report: {args.summarize.resolve() / 'report.md'} ({len(report['rows'])} rows)")
        return 0
    if min(args.attempts, args.jobs, args.agent_timeout, args.max_steps) < 1:
        parser.error("attempts/jobs/agent-timeout/max-steps must be positive")
    if args.check_api and (args.skip_api_preflight or args.list_models):
        parser.error("--check-api cannot be combined with --skip-api-preflight or --list-models")
    specs = load_specs(args.config, args.model_id, args.models, args.model_key_env, args.model_base_url_env)
    url = args.base_url or os.environ.get("AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_URL
    validate_url(url)
    if args.plan:
        print(json.dumps({"models": specs, "attempts_per_model": args.attempts, "jobs": args.jobs,
                          "task": str(TASK), "harness": "gptme", "base_url": url, "image": args.image,
                          "agent_timeout": args.agent_timeout, "max_steps": args.max_steps,
                          "api_preflight": not args.skip_api_preflight, "api_check_only": args.check_api,
                          "sampling_adapter": "gpt-6-luna: omit temperature/top_p inside disposable containers",
                          "model_resolution": "Query RouterLink /models at runtime; unique matching names only"},
                         ensure_ascii=False, indent=2))
        return 0
    key = os.environ.get("ROUTERLINK_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("AGENT_API_KEY")
    if not key and not all(s.get("api_key_env") and s.get("model_id") for s in specs):
        parser.error("Export ROUTERLINK_API_KEY, OPENAI_API_KEY or AGENT_API_KEY in this terminal; do not put a key in models.json")
    check = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
    if check.returncode:
        parser.error("Docker is unavailable in this terminal. Check docker info; activate your existing docker group with newgrp docker if needed")
    bench = args.bench.resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    out = (args.out or bench / ".runtime/runs/my-harbor-long-model-matrix" / timestamp).resolve()
    out.mkdir(parents=True, exist_ok=False)
    print(f"[batch] {out}", flush=True)
    state = {"status": "preflight", "task": str(TASK), "base_url": url,
             "attempts": args.attempts, "jobs": args.jobs, "agent_timeout": args.agent_timeout,
             "max_steps": args.max_steps, "models": [dict(s, status="pending") for s in specs],
             "fingerprints": fingerprint(TASK, bench), "image": args.image,
             "api_preflight": not args.skip_api_preflight, "api_check_only": args.check_api,
             "sampling_adapter": {"name": "gpt-6-luna-default-sampling-v1",
                 "sha256": hashlib.sha256((TASK / "scripts/patch_gptme_sampling.py").read_bytes()).hexdigest()}}
    write_json(out / "matrix.json", state)
    try:
        state["image_id"] = check_image(bench, args.image, args.rebuild, out)
        if key:
            discovery_env = os.environ.copy()
            discovery_env.update(OPENAI_API_KEY=key, OPENAI_BASE_URL=url)
            catalog = discover(args.image, discovery_env)
        else:
            catalog = {"status": "skipped", "model_ids": [], "reason": "Only explicitly configured per-model credentials"}
        write_json(out / "available-models.json", catalog)
        resolved = [resolve_model(spec, catalog.get("model_ids", [])) for spec in specs]
        for spec in resolved:
            if not spec["model_id"]:
                spec.update(status="model_unresolved", reason={"resolution": spec["resolution"],
                    "candidates": spec["candidates"], "discovery_status": catalog.get("status"),
                    "hint": "Use --model-id slug=exact-ID or edit models.json after checking available-models.json"})
            else:
                spec["status"] = "pending"
        state["models"] = resolved
        write_json(out / "matrix.json", state)
        for spec in resolved:
            print(f"[model] {spec['requested_name']}: {spec['model_id'] or spec['resolution']}", flush=True)
        if args.list_models:
            print(json.dumps(catalog, ensure_ascii=False, indent=2))
            state["status"] = "discovery_only"
            write_json(out / "matrix.json", state)
            summarize(out)
            return 0 if catalog.get("status") == "ok" else 2

        runtime_task = stage_runtime_task(out / "runtime-task")
        state["runtime_task"] = str(runtime_task)
        state["runtime_setup_sha256"] = hashlib.sha256((runtime_task / "setup.sh").read_bytes()).hexdigest()

        def run(spec):
            directory = out / spec["slug"]
            directory.mkdir()
            try:
                model_key, model_url = provider_settings(spec, os.environ, key, url)
            except ValueError as exc:
                return {"status": "model_configuration_error", "reason": str(exc)}
            if not args.skip_api_preflight:
                print(f"[api-check] {spec['requested_name']}", flush=True)
                probe = probe_api(args.image, spec["model_id"], model_key, model_url)
                write_json(directory / "api-preflight.json", probe)
                if probe["status"] != "ok":
                    return {"status": "api_preflight_failed", "reason": probe}
                if args.check_api:
                    return {"status": "api_check_passed", "reason": probe}
            env = trial_environment(os.environ, spec["model_id"], model_key, model_url, args.max_steps)
            command = trial_command(bench, runtime_task, args.image, spec["model_id"], model_url,
                                    directory / "trials", args.attempts, args.agent_timeout)
            write_json(directory / "invocation.json", {"argv": command, "api_model_id": spec["model_id"],
                       "harness_model": env["AGENT_MODEL"], "max_steps": args.max_steps,
                       "base_url": model_url, "api_key_env": spec.get("api_key_env", "default"),
                       "sampling_adapter": state["sampling_adapter"],
                       "tokenizer_timeout": "gptme default; no TOKEN-named numeric env flag forwarded"})
            print(f"[start] {spec['requested_name']}; {directory / 'runner.log'}", flush=True)
            with (directory / "runner.log").open("w") as log:
                process = subprocess.run(command, cwd=bench, env=env, stdout=log, stderr=subprocess.STDOUT)
            return {"runner_exit_code": process.returncode,
                    "status": "runner_finished" if process.returncode == 0 else "runner_error"}

        state["status"] = "running"
        write_json(out / "matrix.json", state)
        summarize(out)
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run, spec): spec for spec in resolved if spec["model_id"]}
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    spec.update(future.result())
                except Exception as exc:
                    spec.update(status="runner_error", reason=type(exc).__name__)
                write_json(out / "matrix.json", state)
                summarize(out)
                print(f"[done] {spec['requested_name']}: {spec['status']}", flush=True)
        state["status"] = "finished"
        state["fingerprints_unchanged"] = state["fingerprints"] == fingerprint(TASK, bench)
        write_json(out / "matrix.json", state)
        report = summarize(out)
        print(f"[report] {out / 'report.md'}", flush=True)
        expected = "api_check_passed" if args.check_api else "completed"
        return 0 if all(row["status"] == expected for row in report["rows"]) else 1
    except Exception as exc:
        state.update(status="setup_error", error_type=type(exc).__name__)
        for spec in state["models"]:
            if spec.get("status") == "pending":
                spec.update(status="setup_error", reason=type(exc).__name__)
        write_json(out / "matrix.json", state)
        summarize(out)
        # Infrastructure errors here contain no provider response or credentials.
        print(f"Setup failed ({type(exc).__name__}); see {out} and its preflight/build logs", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
