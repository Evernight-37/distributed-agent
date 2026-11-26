"""End-to-end worker turns driven by a real DeepSeek Harness event stream.

The fake binary below is a real child process, so these tests exercise the code
a production turn uses: executor discovery, argv construction, the loader patch,
permission environment, stdin delivery, event normalization, session persistence
and the final state commit. Only the model itself is replaced.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_harness import worker
from agent_harness.common import read_json, write_json
from agent_harness.project_actions import action_schema

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

reply_object = {
    "reply": "done from dsh",
    "done": True,
    "messages": [],
    "tasks": [],
    "executions": [],
    "memory_notes": ["a durable fact"],
}
if mode == "attachment":
    reply_object["attachments"] = [{"path": "report.txt"}]
reply = json.dumps(reply_object, ensure_ascii=False)
for step, usage in enumerate([
    {"inputTokens": 5, "cacheReadTokens": 15, "cacheWriteTokens": 0, "outputTokens": 2, "totalTokens": 22},
    {"inputTokens": 2, "cacheReadTokens": 30, "cacheWriteTokens": 2, "outputTokens": 3, "totalTokens": 37},
], 1):
    print(json.dumps({"type": "status", "phase": "step_start", "turn": 1, "step": step}))
    print(json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": step, "usage": usage}))
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


class FencedReportTests(unittest.TestCase):
    def test_a_fenced_json_reply_is_unwrapped(self):
        from agent_harness.worker import unfenced_report

        self.assertEqual(unfenced_report('```json\n{"reply": "hi"}\n```'), '{"reply": "hi"}')
        self.assertEqual(unfenced_report('{"reply": "hi"}'), '{"reply": "hi"}')
        # A fence that is not a JSON object is left alone.
        self.assertEqual(unfenced_report("```\nhello\n```"), "```\nhello\n```")


class ProjectActionTransportTests(unittest.TestCase):
    def test_unified_delivery_rules_end_the_turn_without_resubmitting_files(self):
        rules = worker.reply_rules({"role": "executor"}, False, project_tools=True, file_delivery=True)
        self.assertIn("Use project_task_deliver for every delivery", rules)
        self.assertIn("pending_attachment_delivery", rules)
        self.assertIn("project_action null and empty attachments", rules)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the generated DSH tool")
    def test_native_delivery_tool_passes_optional_attachment_paths_to_the_mailbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tool.mjs").write_text(
                worker.foolship_reply_js({"role": "executor"}, project_tools=True, file_delivery=True),
                encoding="utf-8",
            )
            script = root / "check.mjs"
            script.write_text(
                'import {apply} from "./tool.mjs";\n'
                'import assert from "node:assert/strict";\n'
                'import {readdir,readFile,writeFile} from "node:fs/promises";\n'
                'const tools = {}; apply({tools:{register:t=>tools[t.name]=t}},{path:"reply.json"});\n'
                "const submit = tools.project_task_deliver;\n"
                "assert.ok(submit.parameters.attachments);\n"
                'const bad = await submit.execute({summary:"result",links:[],attachments:["same.txt","same.txt"]});\n'
                'assert.equal(bad.error_code,"invalid_arguments");\n'
                'const params = {summary:"result",links:[],attachments:["output/result.txt"]};\n'
                "const pending = submit.execute(params);\n"
                "let request;\n"
                "for(let i=0;i<40 && !request;i++) {\n"
                '  request = (await readdir(".")).find(x=>/^pt-request-[a-f0-9]{32}\\.json$/.test(x));\n'
                "  if(!request) await new Promise(resolve=>setTimeout(resolve, 20));\n"
                "}\n"
                'assert.deepEqual(JSON.parse(await readFile(request,"utf8")),{action:{kind:"deliver",...params}});\n'
                'await writeFile(request.replace("pt-request-","pt-result-"),JSON.stringify({ok:true,status:"pending_attachment_delivery"}));\n'
                'assert.deepEqual(await pending,{ok:true,status:"pending_attachment_delivery"});\n',
                encoding="utf-8",
            )
            run = subprocess.run([shutil.which("node"), str(script)], cwd=root, timeout=15, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))

    def test_native_text_delivery_rejects_final_reply_without_files(self):
        answer = {"reply": "", "project_action": {"kind": "deliver", "summary": "done", "links": []}}
        self.assertIn("project_task_deliver", worker.project_delivery_error(json.dumps(answer)))
        self.assertIsNone(
            worker.project_delivery_error(json.dumps({**answer, "attachments": [{"path": "result.txt"}]}))
        )

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the generated DSH tool")
    def test_native_dsh_rejects_final_text_delivery_and_accepts_file_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tool.mjs").write_text(
                worker.foolship_reply_js({"role": "executor"}, project_tools=True), encoding="utf-8"
            )
            script = root / "check.mjs"
            script.write_text(
                'import {apply} from "./tool.mjs";\n'
                'import assert from "node:assert/strict";\n'
                'const tools = {}; apply({tools:{register:t=>tools[t.name]=t}},{path:"reply.json"});\n'
                'const result = {reply:"",project_action:{kind:"deliver",summary:"done",links:[]}};\n'
                "await assert.rejects(tools.foolship_reply.execute(result), /project_task_deliver/);\n"
                'assert.deepEqual(await tools.foolship_reply.execute({...result,attachments:[{path:"result.txt"}]}),{ok:true});\n',
                encoding="utf-8",
            )
            run = subprocess.run([shutil.which("node"), str(script)], cwd=root, timeout=15, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the generated DSH tool")
    def test_native_draft_tool_uses_job_mailbox_without_accepting_a_version(self):
        policy = {
            "role": "admin",
            "bound_draft": True,
            "agents": ["admin"],
            "drafts": [{"id": "draft-bound", "revision": 7}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tool.mjs").write_text(worker.foolship_reply_js(policy, project_tools=True), encoding="utf-8")
            script = root / "check.mjs"
            script.write_text(
                """
import {apply} from "./tool.mjs";
import assert from "node:assert/strict";
import {readdir, readFile, writeFile} from "node:fs/promises";
import {setTimeout} from "node:timers/promises";
const tools = {};
apply({tools: {register: tool => tools[tool.name] = tool}}, {path:"reply.json"});
assert.deepEqual(Object.keys(tools).sort(), ["foolship_reply", "project_draft_revise"]);
const revise = tools.project_draft_revise;
const changes = {deliverable:"new result"};
assert.deepEqual(Object.keys(revise.parameters), ["changes"]);
const invalid = await revise.execute({changes, base_revision:7});
assert.equal(invalid.error_code, "invalid_arguments");
assert.match(invalid.message, /base_revision/);
assert.equal((await readdir(".")).some(x => x.startsWith("pt-request-")), false);
const pending = revise.execute({changes});
let requestName;
while (!requestName) {
  requestName = (await readdir(".")).find(x => /^pt-request-[a-f0-9]{32}\\.json$/.test(x));
  if (!requestName) await setTimeout(10);
}
assert.deepEqual(JSON.parse(await readFile(requestName, "utf8")), {action:{kind:"revise_draft", changes}});
const receipt = {ok:true,id:"draft-bound",revision:8,status:"draft"};
await writeFile(requestName.replace("pt-request-", "pt-result-"), JSON.stringify(receipt));
assert.deepEqual(await pending, receipt);
assert.equal((await readdir(".")).some(x => x.startsWith("pt-")), false);
await assert.rejects(tools.foolship_reply.execute({reply:"",project_action:{kind:"revise_draft"}}));
assert.deepEqual(await tools.foolship_reply.execute({reply:"",project_action:null}), {ok:true});
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [shutil.which("node"), str(script)],
                cwd=root,
                timeout=15,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_policy_schema_replaces_legacy_actions_and_forces_empty_tasks(self):
        policy = {"role": "executor"}
        schema = worker.reply_schema(policy)
        fields = schema["properties"]
        self.assertEqual(fields["project_action"], action_schema(policy))
        self.assertEqual(fields["tasks"]["maxItems"], 0)
        self.assertFalse({"work_proposal", "work_follow_up", "work_delivery"} & fields.keys())

    def test_dsh_tool_embeds_and_enforces_the_trusted_action_union(self):
        source = worker.foolship_reply_js({"role": "executor"})
        self.assertIn('"deliver"', source)
        self.assertIn('"wait"', source)
        self.assertIn("matchesAction(args.project_action", source)
        self.assertIn("legacy project action is not allowed", source)

    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the generated DSH tool")
    def test_dsh_tool_accepts_one_valid_action_and_rejects_another_roles_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tool.mjs").write_text(worker.foolship_reply_js({"role": "executor"}), encoding="utf-8")
            script = root / "check.mjs"
            script.write_text(
                'import {apply} from "./tool.mjs";\n'
                'import assert from "node:assert/strict";\n'
                'let tool; apply({tools:{register: t => tool = t}}, {path:"reply.json"});\n'
                'await assert.rejects(tool.execute({project_action:{kind:"follow_up",work_id:"x",instruction:null}}));\n'
                'await assert.rejects(tool.execute({project_action:{kind:"deliver",summary:"ok",links:[],message_id:1}}));\n'
                'const reply = {reply:"",project_action:{kind:"deliver",summary:"ok",links:[]}};\n'
                "assert.deepEqual(await tool.execute(reply), {ok:true});\n"
                "await assert.rejects(tool.execute(reply));\n",
                encoding="utf-8",
            )
            subprocess.run([shutil.which("node"), str(script)], cwd=root, check=True, timeout=15, capture_output=True)
            self.assertEqual(
                read_json(root / "reply.json")["project_action"], {"kind": "deliver", "summary": "ok", "links": []}
            )


class DshWorkerTurnTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name) / "receiver"
        self.remote = Path(self._temp.name) / "remote"
        self.remote.mkdir(parents=True)
        self.launcher = fake_dsh(Path(self._temp.name) / "bin")
        self.record = Path(self._temp.name) / "record.json"
        write_json(self.root / "executor.json", {"executor": "dsh", "cli_paths": {"dsh": str(self.launcher)}})
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

    def _job(self, thread_id=None, channel=None, prompt=None, job_id="job1"):
        job = worker.job_dir(job_id)
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
        self.assertIsNone(state["effective_reasoning_effort"])
        self.assertEqual(
            state["usage"], {"input_tokens": 54, "output_tokens": 5, "cached_input_tokens": 45, "cache_write_tokens": 2}
        )
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

    def test_rules_are_sent_once_and_only_the_contract_afterwards(self):
        """A resumed thread already holds the rules; repeating them wastes tokens."""
        self._job(channel="group")
        worker.run("job1")
        first = read_json(self.record)["task"]
        self.assertIn("You are an agent in a human-owned project group", first)

        import shutil

        shutil.rmtree(worker.job_dir("job1"), ignore_errors=True)
        self._job(thread_id="session-11111111-2222-3333-4444-555555555555", channel="group")
        worker.run("job1")
        resumed = read_json(self.record)["task"]
        self.assertNotIn("You are an agent in a human-owned project group", resumed)
        self.assertTrue(resumed.startswith("Reply with one JSON object"), resumed[:80])
        self.assertIn("TASK CONTRACT:", resumed)

    def test_the_reply_tool_result_wins_over_prose(self):
        """A structured answer written by the tool must not be re-parsed from text."""
        job = self._job(channel="group")
        worker.run("job1")
        self.assertTrue((job / "foolship_reply.js").is_file(), "the worker stages the tool")

        import shutil

        shutil.rmtree(job, ignore_errors=True)
        job = self._job(channel="group")
        (job / "reply.json").write_text(json.dumps({"reply": "from the tool"}), encoding="utf-8")
        worker.run("job1")
        self.assertIn("from the tool", (job / "report.txt").read_text(encoding="utf-8"))

    def test_conversation_attachments_land_inside_the_project(self):
        job = self._job(channel="group")
        request = read_json(job / "request.json")
        contract = json.loads(request["prompt"])
        contract["attachments"] = [{"id": "file-1", "name": "shot.png", "mime": "image/png", "size": 2}]
        request["prompt"] = json.dumps(contract)
        write_json(job / "request.json", request)
        from agent_harness import pull_worker

        answer = {"files": [{"id": "file-1", "name": "shot.png", "mime": "image/png", "base64": "aGk="}]}
        with mock.patch.object(pull_worker, "api", return_value=answer):
            worker.run("job1")
        written = self.remote / ".foolship" / "inbound" / "shot.png"
        self.assertEqual(written.read_bytes(), b"hi")
        task = read_json(self.record)["task"]
        self.assertIn(".foolship/inbound/shot.png", task)

    def test_every_stage_runs_with_write_access_to_its_workspace(self):
        self._job()
        worker.run("job1")
        self.assertEqual(read_json(self.record)["permission"], "workspace-write")

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

    def test_native_effort_default_resumes_only_its_own_session(self):
        project = worker.project_dir("demo")
        write_json(project / "project.json", {**self._project(), "effort": "default", "model_config_version": 2})
        write_json(project / "session.json", {"thread_id": "legacy-session"})
        write_json(project / "channels" / "group-dsh.json", {"thread_id": "explicit-session"})
        with (
            mock.patch.object(worker, "launch_supervisor", return_value=123),
            mock.patch.object(worker, "process_stamp", return_value=1),
        ):
            worker.rpc({"action": "start", "project": "demo", "job": "default-first", "prompt": self._contract()})
            first = read_json(worker.job_dir("default-first") / "request.json")
            self.assertEqual(first["session_file"], "session-dsh-default.json")
            self.assertIsNone(first["thread_id"])
            write_json(project / "active.json", None)

            write_json(project / "channels" / "group-dsh-default.json", {"thread_id": "default-session"})
            worker.rpc(
                {
                    "action": "start",
                    "project": "demo",
                    "job": "default-next",
                    "prompt": self._contract(),
                    "session_channel": "group",
                }
            )
            next_turn = read_json(worker.job_dir("default-next") / "request.json")
            self.assertEqual(next_turn["session_file"], "channels/group-dsh-default.json")
            self.assertEqual(next_turn["thread_id"], "default-session")

    def test_dsh_resume_applies_grant_and_revocation_to_the_same_session(self):
        thread_id = None
        turns = (
            ("baseline", [], "workspace-write"),
            ("approved", ["full_access"], "danger-full-access"),
            ("revoked", [], "workspace-write"),
        )
        for job_id, grants, mode in turns:
            with self.subTest(job=job_id):
                objective = json.dumps({"grants": grants, "instructions": "Check permissions"})
                contract = {"stage": "discuss", "objective": objective}
                job = self._job(thread_id=thread_id, channel="group", prompt=json.dumps(contract), job_id=job_id)
                worker.run(job_id)

                record = read_json(self.record)
                argv = record["argv"]
                self.assertEqual(argv[argv.index("--permission-mode") + 1], mode)
                self.assertEqual(argv[argv.index("--approval-policy") + 1], "never")
                self.assertEqual(record["permission"], mode)
                sandbox = next(row["config"] for row in record["patch"] if row["id"] == "sandbox-policy")
                self.assertEqual(sandbox["mode"], mode)
                if thread_id:
                    self.assertEqual(argv[argv.index("--session-id") + 1], thread_id)
                else:
                    self.assertNotIn("--session-id", argv)
                state = read_json(job / "state.json")
                self.assertEqual(state["status"], "needs_review")
                thread_id = state["thread_id"] if thread_id is None else thread_id
                self.assertEqual(state["thread_id"], thread_id)

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

    def test_managed_dsh_turn_keeps_attachment_request_for_receiver(self):
        job = self._job(channel="chat1")
        with mock.patch.dict(os.environ, {"FAKE_DSH_MODE": "attachment"}):
            worker.run("job1")
        self.assertEqual(read_json(job / "state.json")["status"], "needs_review")
        self.assertEqual(
            json.loads((job / "report.txt").read_text(encoding="utf-8"))["attachments"], [{"path": "report.txt"}]
        )
        self.assertIn('"attachments": [{path}]', (job / "prompt.txt").read_text(encoding="utf-8"))

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
