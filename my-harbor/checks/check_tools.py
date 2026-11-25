#!/usr/bin/env python3
"""Offline integration checks; run with the pinned gptme Python environment.

This is infrastructure validation, not a model evaluation. All writes and
simulated mail stay in a temporary fixture, outside the benchmark workspace.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import tempfile
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--schema-out", type=Path)
    args = parser.parse_args()
    environment = args.environment.resolve()

    from gptme.config import Config, MCPConfig, UserConfig, set_config
    from gptme.llm.llm_openai import _spec2tool
    from gptme.llm.models import ModelMeta
    from gptme.tools import get_toolchain
    from gptme.tools.base import ToolSpec
    from jsonschema import Draft202012Validator

    # Do not read user credentials, start external MCP servers, or call an API.
    set_config(Config(user=UserConfig(mcp=MCPConfig(enabled=False))))
    wrappers = load_module("meeting_tools_check", environment / "meeting_tools.py")
    custom = {spec.name: spec for spec in vars(wrappers).values()
              if isinstance(spec, ToolSpec)}
    assert len(custom) == 8, f"Expected 8 task tools; found {len(custom)}"
    specs = [spec for spec in get_toolchain(None) if spec.is_runnable]
    specs.extend(custom.values())
    model = ModelMeta(provider="local", model="gpt-5.6-terra", context=128000)
    schemas = []
    for spec in specs:
        schema = _spec2tool(spec, model)
        function = schema["function"]
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", function["name"]), function["name"]
        try:
            Draft202012Validator.check_schema(function["parameters"])
        except Exception as exc:
            raise ValueError(f"Invalid schema for {function['name']}: {exc}") from exc
        schemas.append(schema)
    if args.schema_out:
        args.schema_out.write_text(json.dumps(schemas, indent=2) + "\n", encoding="utf-8")
    print(f"[check] {len(schemas)} native tool schemas valid (including 8 meeting tools)")

    with tempfile.TemporaryDirectory(prefix="meeting-tools-check-") as temporary:
        root = Path(temporary)
        shutil.copytree(environment / "input", root / "input")
        original = {p.name: p.read_bytes() for p in (root / "input").iterdir()}
        # Start the real stdio server with only its storage root relocated.
        launcher = root / "server.py"
        launcher.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(environment)!r})\n"
            "import mcp_server as server\n"
            f"server.ROOT = Path({str(root)!r})\n"
            "server.EVENT_LOG = server.ROOT / 'mcp-events.jsonl'\n"
            "server.MAIL_LOG = server.ROOT / 'mock-mailbox.jsonl'\n"
            "server.AUDIT_LOG = server.ROOT / 'output/audit-log.json'\n"
            "server.serve()\n", encoding="utf-8",
        )
        wrappers.SERVER = str(launcher)
        calls = 0

        def call(name: str, *, expect_error: bool = False, **arguments):
            nonlocal calls
            # Actual pinned ToolSpec dispatcher -> wrapper -> stdio JSON-RPC.
            messages = list(custom[f"meeting_{name}"].execute(None, None, arguments))
            assert len(messages) == 1, (name, messages)
            result = json.loads(messages[0].content)
            assert result["isError"] is expect_error, (name, result)
            value = json.loads(result["content"][0]["text"])
            assert value["ok"] is (not expect_error), (name, value)
            calls += 1
            return value

        files = call("list_files")["files"]
        assert {Path(p).name for p in files} == set(original)
        assert "Project Aurora" in call(
            "fetch_webpage", url="http://meeting.local/project-aurora",
        )["content"]
        for name, content in original.items():
            value = call("read_file", path=str(root / "input" / name))
            assert value["content"].encode() == content
        recipients = call("get_authorized_recipients")["recipients"]
        assert len(recipients) == 3
        call("write_file", path=str(root / "output/probe.txt"), content="Probe\n")
        assert (root / "output/probe.txt").read_text() == "Probe\n"
        for recipient in recipients:
            mail = dict(recipient=recipient["email"], subject="Integration probe", body="Test")
            assert call("draft_email", **mail)["status"] == "drafted"
            assert call("send_email", **mail)["mock"] is True
        drafts = root / "output/email-drafts"
        assert {p.name for p in drafts.iterdir()} == {"owner.txt", "finance.txt", "safety.txt"}
        sent = [json.loads(line) for line in (root / "mock-mailbox.jsonl").read_text().splitlines()]
        assert len(sent) == 3 and all(row["mock"] for row in sent)
        assert {row["recipient"] for row in sent} == {r["email"] for r in recipients}
        for event in ["first_probe", "second_probe"]:
            call("append_audit_log", event=event, details="Integration probe")
        audit = json.loads((root / "output/audit-log.json").read_text())
        assert [row["event"] for row in audit] == ["first_probe", "second_probe"]

        # Boundary failures must return tool results rather than lose the RPC ID.
        call("read_file", path=str(root / "output/probe.txt"), expect_error=True)
        call("write_file", path=str(root / "input/agenda.md"), content="overwrite", expect_error=True)
        call("send_email", recipient="not-authorized@example.test", subject="Probe",
             body="Test", expect_error=True)
        assert {p.name: p.read_bytes() for p in (root / "input").iterdir()} == original
        assert len((root / "mock-mailbox.jsonl").read_text().splitlines()) == 3
        events = [json.loads(line) for line in (root / "mcp-events.jsonl").read_text().splitlines()]
        executed = {row["action"] for row in events if row["event"] == "executed"}
        assert executed == {name.removeprefix("meeting_") for name in custom}
        print(f"[check] {calls} tool round trips passed; all 8 tools executed; simulated mail only")


if __name__ == "__main__":
    main()
