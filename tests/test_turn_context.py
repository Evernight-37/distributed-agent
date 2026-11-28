import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness import worker
from agent_harness.common import write_json


class TurnContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(worker, "ROOT", Path(directory.name)))
        self.previous = worker.job_dir("previous")
        self.previous.mkdir(parents=True)
        self.state = {"thread_id": "thread", "status": "needs_review"}
        self.context = {
            "identity": {"id": "alice"},
            "project": {"id": "project"},
            "members": ["alice"],
            "project_memory": ["decision"],
            "protocol": "project rules",
            "task": {"stage": "discuss", "acceptance": "reply"},
            "grants": [],
            "project_action_policy": {"role": "admin"},
            "visible_messages": [{"id": i, "body": f"message {i}"} for i in range(1, 5)],
        }
        self.base = {
            "thread_id": "thread",
            "managed_turn": True,
            "project": {"remote_path": directory.name},
            "session_file": "channels/group-codex.json",
            "previous_job": "previous",
        }
        self.save_prior()

    def config(self):
        wrapper = {"stage": "discuss", "acceptance": "reply", "objective": json.dumps(self.context)}
        return dict(self.base, prompt=json.dumps(wrapper))

    def save_prior(self):
        write_json(self.previous / "request.json", self.config())
        write_json(self.previous / "state.json", self.state)

    def render(self, thread="thread", **changes):
        config = dict(self.config(), **changes)
        return worker.managed_prompt(config, thread, {"role": "admin"}, project_tools=True, file_delivery=True)

    def payload(self, text):
        return json.loads(text.split("TASK CONTRACT:\n", 1)[1])

    def test_resumed_input_carries_changes_references_and_current_authority(self):
        self.context["visible_messages"][3]["body"] = "edited"
        self.context["visible_messages"].append({"id": 5, "body": "new", "project_id": "project"})
        self.context["task"].update(source_message_id=2, reference_message_ids=[3], attachments=[{"path": "notes.txt"}])
        self.context.update(grants=["full_access"], project_memory=[])
        prompt = self.render()
        context = self.payload(prompt)
        self.assertEqual([row["id"] for row in context["visible_messages"]], [2, 3, 4, 5])
        self.assertEqual(context["visible_messages"][-1], {"id": 5, "body": "new"})
        self.assertEqual(context["visible_messages"][-2]["body"], "edited")
        self.assertEqual(context["project_memory"], [])
        self.assertEqual(context["task"], self.context["task"])
        self.assertEqual(context["grants"], ["full_access"])
        self.assertEqual(context["project_action_policy"], {"role": "admin"})
        self.assertTrue({"identity", "project", "members", "protocol", "stage", "acceptance"}.isdisjoint(context))
        self.assertNotIn("pending_attachment_delivery", prompt)

    def test_uncertain_continuity_restores_current_snapshot(self):
        for prompt in (
            self.render(None),
            self.render("replacement"),
            self.render(previous_job="missing"),
            self.render(session_file="channels/dm-alice-codex.json"),
            self.render(project={"remote_path": "/other"}),
        ):
            self.assertEqual(self.payload(prompt)["visible_messages"], self.context["visible_messages"])
            self.assertIn("members", self.payload(prompt))
        self.context["identity"] = {"id": "bob"}
        self.assertEqual(self.payload(self.render())["identity"], {"id": "bob"})
        self.state["status"] = "failed"
        self.save_prior()
        self.assertIn("members", self.payload(self.render()))
        (self.previous / "request.json").write_text("{", encoding="utf-8")
        self.state["status"] = "needs_review"
        write_json(self.previous / "state.json", self.state)
        self.assertIn("members", self.payload(self.render()))

    def test_non_chat_payload_survives_verbatim(self):
        self.assertTrue(self.render(prompt="legacy request").endswith("TASK CONTRACT:\nlegacy request"))
