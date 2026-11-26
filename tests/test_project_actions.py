import unittest

from agent_harness.project_actions import (
    action_schema,
    project_tool_action,
    resolve_tool_action,
    tool_specs,
    validate_action,
)


class ProjectActionTests(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "title": "Fix login",
            "instructions": "Correct retry",
            "deliverable": "Patch",
            "acceptance": "Retry succeeds",
            "validation": "Run the login test",
            "agent": "scott",
            "stage": "implement",
            "allowed_files": "login.py",
            "reference_message_ids": [1],
        }
        self.admin = {
            "role": "admin",
            "agents": ["scott"],
            "drafts": [{"id": "draft-a", "revision": 2}],
            "works": ["work-a"],
        }

    def test_assigned_scope_limits_which_actions_are_possible(self):
        create = {"kind": "create_draft", "spec": self.spec}
        result = {"kind": "deliver", "summary": "Done", "links": []}
        self.assertEqual(validate_action(create, self.admin), create)
        self.assertEqual(validate_action(result, {"role": "executor"}), result)
        self.assertEqual(action_schema({"role": "chat"}), {"type": "null"})
        for action, policy in ((create, {"role": "executor"}), (result, self.admin), (result, {"role": "chat"})):
            with self.assertRaises(ValueError):
                validate_action(action, policy)

    def test_existing_draft_and_work_references_require_authorized_versions(self):
        revise = {"kind": "revise_draft", "draft_id": "draft-a", "base_revision": 2, "spec": self.spec}
        self.assertEqual(validate_action(revise, self.admin), revise)
        for action in (
            {**revise, "base_revision": 3},
            {**revise, "base_revision": True},
            {**revise, "draft_id": "somebody-elses-draft"},
            {"kind": "follow_up", "work_id": "work-b", "instruction": None},
            {"kind": "create_draft", "spec": {**self.spec, "status": "accepted"}},
            {"kind": "create_draft", "spec": {**self.spec, "agent": "another-agent"}},
        ):
            with self.assertRaises(ValueError):
                validate_action(action, self.admin)
        with self.assertRaises(ValueError):
            validate_action({"kind": "create_draft", "spec": self.spec}, {**self.admin, "bound_draft": True})

    def test_bound_native_revision_uses_the_dispatch_target_and_version(self):
        policy = {**self.admin, "bound_draft": True}
        [tool] = tool_specs(policy)
        self.assertEqual(tool["name"], "project_draft_revise")
        self.assertEqual(tool["inputSchema"]["required"], ["changes"])
        change = project_tool_action(tool["name"], {"changes": {"deliverable": "New patch"}})
        self.assertEqual(resolve_tool_action(change, policy), {**change, "draft_id": "draft-a", "base_revision": 2})
        for arguments, field in (
            ({"draft_id": "draft-a"}, "draft_id"),
            ({"base_revision": 2}, "base_revision"),
            ({"changes": {}}, "changes"),
            ({"changes": {"agent": "unknown"}}, "changes.agent"),
            ({"changes": {"title": False}}, "changes.title"),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, field):
                resolve_tool_action({**change, **arguments}, policy)

    def test_native_tools_are_limited_to_authorized_actions_and_targets(self):
        self.assertEqual(tool_specs({"role": "chat"}), [])
        self.assertEqual([tool["name"] for tool in tool_specs({"role": "executor"})], ["project_task_deliver"])
        self.assertEqual(tool_specs({**self.admin, "bound_draft": True, "drafts": []}), [])
        revision = project_tool_action("project_draft_revise", {"draft_id": "draft-a", "changes": {"title": "Now"}})
        self.assertEqual(resolve_tool_action(revision, self.admin), {**revision, "base_revision": 2})
        with self.assertRaisesRegex(ValueError, "draft_id"):
            resolve_tool_action({**revision, "draft_id": "unknown"}, self.admin)
        with self.assertRaisesRegex(ValueError, "缺少字段"):
            resolve_tool_action(project_tool_action("project_draft_create", {"spec": {}}), self.admin)
        with self.assertRaisesRegex(ValueError, "不允许"):
            resolve_tool_action(project_tool_action("project_task_deliver", {"summary": "ok", "links": []}), self.admin)
        with self.assertRaisesRegex(ValueError, "kind"):
            project_tool_action("project_draft_create", {"kind": "deliver"})
