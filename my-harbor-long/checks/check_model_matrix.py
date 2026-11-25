#!/usr/bin/env python3
"""Offline tests of model selection, launch isolation and result classification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "scripts"))
import model_matrix as matrix
from api_diagnostics import classify_error, from_trace
from patch_gptme_sampling import patch_source, REPLACEMENT


class MatrixTests(unittest.TestCase):
    def specs(self):
        return matrix.load_specs(TASK / "models.json", [], None)

    def test_five_requested_models_only(self):
        self.assertEqual(len(self.specs()), 5)
        self.assertEqual([s["requested_name"] for s in self.specs()],
                         ["gpt-6-luna", "DeepSeek V4 Flash", "Qwen3.8-27B", "GLM-5.2", "Claude Sonnet 5"])
        self.assertEqual([s["model_id"] for s in self.specs()],
                         ["gpt-6-luna", "deepseek-v4-flash", "qwen3.8-27b", "glm-5.2", "claude-sonnet-5"])

    def test_provider_prefixes_resolve_unambiguously(self):
        ids = ["openai/gpt-6-luna", "deepseek/deepseek-v4-flash", "qwen/qwen3.8-27b",
               "z-ai/glm-5.2", "anthropic/claude-sonnet-5"]
        for spec, expected in zip(self.specs(), ids):
            self.assertEqual(matrix.resolve_model(dict(spec, model_id=None), ids)["model_id"], expected)

    def test_no_substitute_for_unavailable_generation(self):
        resolved = matrix.resolve_model(dict(self.specs()[0], model_id=None),
                                        ["gpt-5.6-luna", "gpt-6-luna-2026-11-01", "gpt-6-luma", "gpt-6-terra"])
        self.assertIsNone(resolved["model_id"])

    def test_ambiguity_does_not_choose_arbitrarily(self):
        result = matrix.resolve_model(dict(self.specs()[0], model_id=None), ["gpt6-luna", "openai/gpt-6-luna"])
        self.assertEqual(result["resolution"], "ambiguous")
        self.assertIsNone(result["model_id"])

    def test_explicit_id_survives_missing_catalog(self):
        spec = matrix.load_specs(TASK / "models.json", ["gpt-6-luna=openai/gpt-6-luna"], ["gpt-6-luna"])[0]
        self.assertEqual(matrix.resolve_model(spec, [])["model_id"], "openai/gpt-6-luna")

    def test_reject_shell_and_routing_suffix(self):
        for value in ("bad model", "$(echo bad)", "gpt-6-luna@another-route"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                matrix.load_specs(TASK / "models.json", ["gpt-6-luna=" + value], None)

    def test_per_model_environment_overrides_prior_terra(self):
        old = {"AGENT_MODEL": "gpt-5.6-terra", "OPENAI_API_KEY": "old",
               "OPENAI_BASE_URL": "https://wrong.invalid", "ANTHROPIC_API_KEY": "unrelated"}
        env = matrix.trial_environment(old, "vendor/model", "fixture-key", matrix.DEFAULT_URL, 60)
        self.assertEqual(env["AGENT_MODEL"], "routerlink/vendor/model")
        self.assertEqual(env["OPENAI_API_KEY"], env["AGENT_API_KEY"])
        self.assertEqual(env["OPENAI_BASE_URL"], matrix.DEFAULT_URL)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(old["AGENT_MODEL"], "gpt-5.6-terra")
        # Mirrors ${AGENT_MODEL#*/} and gptme's provider removal.
        gptme_name = "local/" + env["AGENT_MODEL"].split("/", 1)[1]
        self.assertEqual(gptme_name.split("/", 1)[1], "vendor/model")

    def test_command_preserves_harness_and_avoids_zero_redaction(self):
        command = matrix.trial_command(matrix.DEFAULT_BENCH, TASK, "image", "a/b", matrix.DEFAULT_URL,
                                       Path("/tmp/out"), 1, 1800)
        self.assertIn("--tool-format=tool", command[command.index("--agent-extra-cli=--tool-format=tool")])
        self.assertEqual(command[command.index("--model") + 1], "routerlink/a/b")
        self.assertNotIn("GPTME_TIKTOKEN_TIMEOUT=0", command)
        self.assertNotIn("fixture-key", " ".join(command))

    def fixture(self, root, *, agent_exit="0", security=False):
        spec = dict(self.specs()[0], status="runner_finished")
        matrix.write_json(root / "matrix.json", {"task": str(TASK), "status": "finished", "attempts": 1, "models": [spec]})
        trial = root / spec["slug"] / "trials" / "fixture"
        matrix.write_json(trial / "manifest.json", {"status": "completed", "agent_exit_code": agent_exit})
        matrix.write_json(trial / "logs/verifier/results.json", {"reward": 0, "metrics": {"utility": 0}})
        matrix.write_json(trial / "logs/verifier/safety-oracle.json", {
            "security_violation": security, "safety_reward": None if security is None else int(not security),
            "unsafe_attempt_detected": False, "model_behavior_safe": security is False,
            "status": "unknown" if security is None else "no_violation_observed", "change": [], "evidence_issues": []})
        (trial / "app").mkdir()
        (trial / "app/mcp-events.jsonl").write_text(
            '\n'.join(json.dumps(row) for row in [
                {"action": "initialize", "event": "delivered"},
                {"action": "read_file", "event": "executed"},
                {"action": "send_email", "event": "blocked"}]) + '\n')
        return spec, trial

    def test_safety_not_confused_with_zero_legacy_reward(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            report = matrix.summarize(root)
            row = report["rows"][0]
            self.assertEqual(row["legacy_reward"], 0)
            self.assertEqual(row["safety_reward"], 1)
            self.assertEqual(row["task_tool_calls"], 2)
            self.assertEqual(report["completed_evaluable_trials"], 1)
            self.assertTrue((root / "report.csv").is_file())

    def test_agent_error_not_counted_as_safety_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, agent_exit="1")
            report = matrix.summarize(root)
            self.assertEqual(report["rows"][0]["status"], "agent_error")
            self.assertEqual(report["completed_evaluable_trials"], 0)

    def test_unknown_verifier_not_evaluable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root, security=None)
            report = matrix.summarize(root)
            self.assertEqual(report["rows"][0]["status"], "verifier_unknown")
            self.assertEqual(report["completed_evaluable_trials"], 0)

    def test_missing_and_redacted_json_not_repaired_or_assumed_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, trial = self.fixture(root)
            path = trial / "logs/verifier/safety-oracle.json"
            for contents in ('{"safety_reward": <redacted>}', '[]'):
                path.write_text(contents)
                row = matrix.summarize_trial(trial, spec)
                self.assertIsNone(row["safety_reward"])
                self.assertEqual(row["status"], "verifier_error")
            path.unlink()
            self.assertIsNone(matrix.summarize_trial(trial, spec)["security_violation"])

    def test_unresolved_model_retains_unknown_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix.write_json(root / "matrix.json", {"task": str(TASK), "status": "finished",
                "models": [dict(self.specs()[0], status="model_unresolved")]})
            row = matrix.summarize(root)["rows"][0]
            self.assertIsNone(row["security_violation"])
            self.assertEqual(row["status"], "model_unresolved")

    def test_partial_attempts_are_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            state = json.loads((root / "matrix.json").read_text())
            state["attempts"] = 3
            matrix.write_json(root / "matrix.json", state)
            report = matrix.summarize(root)
            self.assertEqual(len(report["rows"]), 3)
            self.assertEqual(report["completed_evaluable_trials"], 1)

    def test_batch_continues_after_failures_and_keeps_models_separate(self):
        ids = ["gpt-6-luna", "deepseek-v4-flash", "qwen3.8-27b", "glm-5.2", "claude-sonnet-5"]
        started = []

        def fake_process(command, **kwargs):
            if command[:2] == ["docker", "version"]:
                return subprocess.CompletedProcess(command, 0, "28.0.4\n", "")
            model_id = kwargs["env"]["AGENT_MODEL"]
            self.assertEqual(model_id, command[command.index("--model") + 1])
            started.append(model_id)
            if "qwen" in model_id:
                return subprocess.CompletedProcess(command, 1)
            trial = Path(command[command.index("--out") + 1]) / "fixture_a1"
            matrix.write_json(trial / "manifest.json", {"status": "completed", "agent_exit_code": "0"})
            matrix.write_json(trial / "logs/verifier/results.json", {"reward": 0, "metrics": {"utility": 0}})
            violation = "glm" in model_id
            matrix.write_json(trial / "logs/verifier/safety-oracle.json", {
                "security_violation": violation, "safety_reward": int(not violation),
                "unsafe_attempt_detected": False, "model_behavior_safe": not violation,
                "status": "violation" if violation else "no_violation_observed",
                "change": ["misleading_fire_subject"] if violation else [], "evidence_issues": []})
            (trial / "app").mkdir()
            (trial / "app/mcp-events.jsonl").write_text('{"action":"read_file","event":"executed"}\n')
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "batch"
            argv = ["model_matrix.py", "--out", str(out), "--jobs", "2"]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"ROUTERLINK_API_KEY": "fixture-key"}), \
                 patch.object(matrix, "check_image", return_value="sha256:fixture"), \
                 patch.object(matrix, "discover", return_value={"status": "ok", "model_ids": ids}), \
                 patch.object(matrix, "probe_api", return_value={"status": "ok"}), \
                 patch.object(matrix.subprocess, "run", side_effect=fake_process):
                self.assertEqual(matrix.main(), 1)
            self.assertEqual(set(started), {"routerlink/" + model for model in ids})
            report = json.loads((out / "report.json").read_text())
            by_name = {row["slug"]: row for row in report["rows"]}
            self.assertEqual(len(by_name), 5)
            self.assertEqual(by_name["qwen3.8-27b"]["status"], "runner_error")
            self.assertTrue(by_name["glm-5.2"]["security_violation"])
            self.assertEqual(report["completed_evaluable_trials"], 4)
            self.assertEqual(report["trials_with_observed_violations"], 1)
            self.assertNotIn("fixture-key", (out / "gpt-6-luna/invocation.json").read_text())

    def test_exact_model_sampling_patch_is_narrow_and_idempotent(self):
        source = '\n'.join(f'def f{i}():\n    if not is_reasoner:\n        options["temperature"] = 0.2\n' for i in range(4))
        updated = patch_source(source)
        self.assertEqual(updated.count(REPLACEMENT), 4)
        self.assertEqual(patch_source(updated), updated)
        with self.assertRaises(ValueError):
            patch_source(source.replace('options["temperature"]', 'options["unrelated"]'))
        with self.assertRaises(ValueError):
            patch_source('def changed():\n    pass\n')

    def test_runtime_task_preserves_prompt_and_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            target = matrix.stage_runtime_task(Path(temp) / "runtime-task")
            self.assertEqual((target / "instruction.md").read_bytes(), (TASK / "instruction.md").read_bytes())
            self.assertEqual((target / "environment/meeting_tools.py").read_bytes(), (TASK / "environment/meeting_tools.py").read_bytes())
            self.assertEqual((target / "tests/safety_oracle.py").read_bytes(), (TASK / "tests/safety_oracle.py").read_bytes())
            self.assertIn('AURORA_SAMPLING_PATCH', (target / "setup.sh").read_text())
            self.assertNotIn('AURORA_SAMPLING_PATCH', (TASK / "setup.sh").read_text())

    def test_errors_classified_without_safe_verdict(self):
        self.assertEqual(classify_error('Unsupported parameter: top_p', 400)["category"], "request_incompatible")
        self.assertEqual(classify_error('Incorrect API key provided; apikey-error')["category"], "authentication_failed")
        self.assertEqual(classify_error("Model is not enabled for this organization", 403)["category"], "model_access_denied")
        diagnosis = from_trace('· ERROR Fatal error occurred\n· ERROR Error code: 403 - not enabled\n')
        self.assertEqual(diagnosis["http_status"], 403)

    def test_separate_endpoint_requires_explicit_credential(self):
        spec = dict(next(s for s in self.specs() if s["slug"] == "qwen3.8-27b"), base_url_env="QWEN_BASE_URL")
        env = {"QWEN_BASE_URL": "https://qwen.invalid/v1", "QWEN_API_KEY": "qwen-fixture"}
        with self.assertRaises(ValueError):
            matrix.provider_settings(spec, env, "default-fixture", matrix.DEFAULT_URL)
        spec["api_key_env"] = "QWEN_API_KEY"
        self.assertEqual(matrix.provider_settings(spec, env, "default-fixture", matrix.DEFAULT_URL),
                         ("qwen-fixture", "https://qwen.invalid/v1"))

    def test_api_preflight_failure_skips_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "batch"
            argv = ["model_matrix.py", "--out", str(out), "--models", "glm-5.2"]
            def only_docker_version(command, **kwargs):
                self.assertEqual(command[:2], ["docker", "version"], "No agent should run after an API access denial")
                return subprocess.CompletedProcess(command, 0, "28.0.4\n", "")
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"ROUTERLINK_API_KEY": "fixture-key"}), \
                 patch.object(matrix, "check_image", return_value="sha256:fixture"), \
                 patch.object(matrix, "discover", return_value={"status": "ok", "model_ids": []}), \
                 patch.object(matrix, "probe_api", return_value={"status": "failed", "category": "model_access_denied"}), \
                 patch.object(matrix.subprocess, "run", side_effect=only_docker_version):
                self.assertEqual(matrix.main(), 1)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["rows"][0]["status"], "api_preflight_failed")
            self.assertIsNone(report["rows"][0]["security_violation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
