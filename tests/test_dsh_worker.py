"""End-to-end worker turns driven by a real DeepSeek Harness event stream.

The fake binary below is a real child process, so these tests exercise the code
a production turn uses: executor discovery, argv construction, the loader patch,
permission environment, stdin delivery, event normalization, session persistence
and the final state commit. Only the model itself is replaced.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import worker
from agent_harness.common import read_json, write_json

FAKE_SOURCE = r"""
import json
import os
import sys

if "--version" in sys.argv:
    print("0.2.1")
    sys.exit(0)

task = sys.stdin.read()
mode = os.environ.get("FAKE_DSH_MODE", "ok")
record = os.environ.get("FAKE_DSH_RECORD")
args = sys.argv[1:]
patch_path = None
for index, item in enumerate(args):
    if item == "--patch" and index + 1 < len(args):
        patch_path = args[index + 1]
if record:
    payload = {
        "argv": args,
        "task": task,
        "cwd": os.getcwd(),
        "permission": os.environ.get("DSH_PERMISSION_MODE"),
        "patch": json.loads(open(patch_path, encoding="utf-8").read()) if patch_path else None,
    }
    with open(record, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)

session = "session-11111111-2222-3333-4444-555555555555"
print(json.dumps({"type": "session", "sessionId": session, "cwd": os.getcwd()}))
print(json.dumps({"type": "status", "phase": "turn_start", "turn": 1}))
print(json.dumps({"type": "thinking", "text": "private reasoning"}))
print(json.dumps({"type": "tool_call", "callId": "c1", "tool": "pwsh", "input": {"command": "dir"}}))
print(json.dumps({"type": "tool_result", "callId": "c1", "status": "completed", "result": "ok"}))
print(json.dumps({"type": "text", "text": "an intermediate message"}))

if mode == "error":
    sys.stderr.write("dsh: MISSING_CREDENTIAL: no API key\n")
    sys.stderr.flush()
    print(json.dumps({
        "type": "status",
        "phase": "turn_end",
        "turn": 1,
        "reason": {"kind": "error", "error": {"code": "MISSING_CREDENTIAL", "message": "no API key"}},
    }))
    print(json.dumps({"type": "final", "text": ""}))
    sys.exit(1)

reply = json.dumps({
    "reply": "done from dsh",
    "done": True,
    "messages": [],
    "tasks": [],
    "executions": [],
    "memory_notes": ["a durable fact"],
}, ensure_ascii=False)
print(json.dumps({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}}))
print(json.dumps({"type": "final", "text": reply}))
sys.exit(0)
"""


def fake_dsh(directory):
    """Write a runnable stand-in for the dsh launcher for this platform."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fake_dsh.py"
    script.write_text(FAKE_SOURCE, encoding="utf-8")
    if os.name == "nt":
        launcher = directory / "dsh.cmd"
        launcher.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="utf-8")
        return launcher
    launcher = directory / "dsh"
    launcher.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script), encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


class DshWorkerTurnTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / "receiver"
        self.remote = Path(self._temp.name) / "remote"
        self.remote.mkdir(parents=True)
        self.launcher = fake_dsh(Path(self._temp.name) / "bin")
        self.record = Path(self._temp.name) / "record.json"
        write_json(self.root / "executor.json", {"executor": "dsh", "cli_path": str(self.launcher)})
        self._old_root = worker.ROOT
        worker.ROOT = self.root
        patcher = mock.patch.dict(os.environ, {"FAKE_DSH_RECORD": str(self.record), "FAKE_DSH_MODE": "ok"})
        patcher.start()
        self.addCleanup(self._restore)
        self.addCleanup(patcher.stop)

    def _restore(self):
        worker.ROOT = self._old_root
        self._temp.cleanup()

    @staticmethod
    def _contract(stage="discuss", instructions="test prompt"):
        return json.dumps(
            {
                "stage": stage,
                "title": "demo",
                "instructions": instructions,
                "allowed_files": "",
                "acceptance": "ok",
                "validation": "tests",
            }
        )

    def _project(self):
        return {
            "name": "demo",
            "model": "deepseek-flash",
            "effort": "medium",
            "remote_path": str(self.remote),
            "timeout": 60,
            "local_control": True,
        }

    def _job(self, thread_id=None, channel=None, prompt=None):
        job = worker.job_dir("job1")
        job.mkdir(parents=True)
        request = {
            "project": self._project(),
            "prompt": prompt or self._contract(),
            "memory": {},
            "thread_id": thread_id,
            "managed_turn": bool(channel),
            "session_file": "channels/%s.json" % channel if channel else "session.json",
        }
        if channel:
            request["session_channel"] = channel
        write_json(job / "request.json", request)
        write_json(job / "state.json", {"project": "demo", "status": "starting"})
        return job

    def test_successful_turn_records_the_report_session_and_patch(self):
        job = self._job()
        worker.run("job1")
        state = read_json(job / "state.json")
        self.assertEqual(state["status"], "needs_review")
        self.assertEqual(state["exit_code"], 0)
        self.assertEqual(state["thread_id"], "session-11111111-2222-3333-4444-555555555555")
        self.assertEqual(state["requested_model"], "deepseek-flash")
        self.assertFalse(state["model_verified"])
        self.assertIsNone(state["effective_model"])
        self.assertIn("done from dsh", (job / "report.txt").read_text(encoding="utf-8"))

        session = read_json(self.root / "projects" / "demo" / "session.json")
        self.assertEqual(session["thread_id"], state["thread_id"])

        record = read_json(self.record)
        self.assertEqual(record["argv"][record["argv"].index("--profile") + 1], "headless")
        self.assertIn("--json", record["argv"])
        self.assertEqual(record["argv"][-1], "-")
        self.assertNotIn("--session-id", record["argv"])
        self.assertIn("test prompt", record["task"])
        self.assertEqual(Path(record["cwd"]).resolve(), self.remote.resolve())
        self.assertEqual(
            record["patch"][0]["config"],
            {"provider": "deepseek-official", "model": "deepseek-flash"},
        )

    def test_read_only_stages_use_the_read_only_sandbox(self):
        self._job()
        worker.run("job1")
        self.assertEqual(read_json(self.record)["permission"], "read-only")

    def test_implement_stage_uses_the_writable_sandbox(self):
        self._job(prompt=self._contract(stage="implement"))
        worker.run("job1")
        self.assertEqual(read_json(self.record)["permission"], "workspace-write")

    def test_second_turn_resumes_the_provider_session(self):
        job = self._job()
        worker.run("job1")
        first = read_json(job / "state.json")["thread_id"]
        write_json(job / "request.json", {**read_json(job / "request.json"), "thread_id": first})
        write_json(job / "state.json", {"project": "demo", "status": "starting"})
        # A completed turn leaves the report in place; a second job is a new one.
        (job / "report.txt").unlink(missing_ok=True)
        worker.run("job1")
        self.assertEqual(read_json(self.record)["argv"][-3:], ["--session-id", first, "-"])
        self.assertEqual(read_json(job / "state.json")["status"], "needs_review")

    def test_managed_turn_uses_the_group_contract_without_a_schema(self):
        job = self._job(channel="chat1")
        worker.run("job1")
        record = read_json(self.record)
        self.assertNotIn("--output-schema", record["argv"])
        self.assertFalse((job / "reply-schema.json").exists())
        prompt = (job / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("human-owned project group", prompt)
        self.assertIn("test prompt", prompt)
        state = read_json(job / "state.json")
        self.assertEqual(state["status"], "needs_review")
        session = read_json(self.root / "projects" / "demo" / "channels" / "chat1.json")
        self.assertEqual(session["thread_id"], state["thread_id"])

    def test_failed_turn_translates_the_provider_error(self):
        job = self._job()
        with mock.patch.dict(os.environ, {"FAKE_DSH_MODE": "error"}):
            worker.run("job1")
        state = read_json(job / "state.json")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exit_code"], 1)
        self.assertIn("API Key", state["error"])
        self.assertIsNone(state.get("provider_error"))

    def test_a_cancelled_turn_is_not_a_failure(self):
        job = self._job()
        write_json(job / "cancel.json", {"requested_at": "2026-10-01T00:00:00+00:00"})
        worker.run("job1")
        state = read_json(job / "state.json")
        self.assertEqual(state["status"], "cancelled")
        self.assertIn("cancel_requested_at", state)


if __name__ == "__main__":
    unittest.main()
