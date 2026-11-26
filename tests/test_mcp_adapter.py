import json
import tempfile
import threading
import unittest
import uuid
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_harness.account_identity import approve_delegate, generate_identity
from agent_harness.app_bridge import AppBridge, MCP_DELEGATE_SCOPES
from agent_harness.collaboration_store import Store
from agent_harness.common import write_json
from agent_harness.mcp_adapter import FoolshipMcpBackend
from agent_harness.panel import handler_for


class QuietBridge(AppBridge):
    def _enroll(self, server=None):
        return None


class McpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "service"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.bridge = QuietBridge(self.root / "app")
        result = self.bridge.invoke("register", {"username": "Owner", "password": "local", "server": self.url})
        self.assertTrue(result["ok"], result)
        Store(self.state).claim_local_host(self.bridge.identity["account_id"], self.bridge.profile["node"])
        self.project = self.bridge._rpc("create-project", {"name": "MCP project"}, server=self.url)["id"]
        delegate = generate_identity()
        delegate.update(
            account_id=self.bridge.identity["account_id"],
            chain=approve_delegate(self.bridge.identity, delegate["public_key"], MCP_DELEGATE_SCOPES),
        )
        self.grant = self.root / "grant.json"
        write_json(
            self.grant,
            {
                "version": 1,
                "profile_id": self.bridge.profile["id"],
                "username": "Owner",
                "identity": delegate,
                "scopes": list(MCP_DELEGATE_SCOPES),
                "servers": [self.url],
            },
        )
        self.bridge._register_mcp_delegate(self.url, {"identity": delegate})
        self.backend = FoolshipMcpBackend(self.grant)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_memory_review_requires_a_project_grant_and_honors_archive(self):
        with Store(self.state).connect() as db:
            note = db.execute(
                "INSERT INTO project_memory_notes(project_id,author_id,body,created_at,state) VALUES(?,?,?,?,?)",
                (self.project, "admin", "A proposed project rule", 100, "pending"),
            ).lastrowid
        payload = {"project": self.project, "ids": [note], "action": "keep", "kind": "rule"}
        view = self.backend.call("harness_project_read", {"project": self.project})
        self.assertFalse(view["project_memory"]["can_review"])
        self.assertEqual(view["project_memory"]["candidates"][0]["id"], note)
        with self.assertRaises((RuntimeError, PermissionError)):
            self.backend.call("harness_project_memory_review", payload)
        self.bridge._rpc("mcp-approval-policy", {"project": self.project, "kinds": ["memory"]}, server=self.url)
        response = self.backend.call("harness_project_memory_review", payload)
        self.assertEqual(response, {"ids": [note], "state": "active"})

        self.bridge._rpc("project-lifecycle", {"project": self.project, "action": "archive"}, server=self.url)
        view = self.backend.call("harness_project_read", {"project": self.project})
        self.assertFalse(view["project_memory"]["can_review"])
        self.assertEqual(view["project_memory"]["notes"][0]["id"], note)
        with self.assertRaises((RuntimeError, PermissionError, ValueError)):
            self.backend.call(
                "harness_project_memory_review", {"project": self.project, "ids": [note], "action": "archive"}
            )

    def test_lists_reads_and_sends_without_browser_session(self):
        status = self.backend.call("harness_status", {})
        self.assertEqual(status["account"], "Owner")
        self.assertEqual(status["unavailable_services"], [])
        projects = self.backend.call("harness_projects_list", {})["projects"]
        self.assertEqual(projects[0]["id"], self.project)
        sent = self.backend.call(
            "harness_message_send",
            {"project": self.project, "body": "Hello from normal Codex"},
        )
        view = self.backend.call("harness_project_read", {"project": self.project, "after_message_id": 0})
        self.assertEqual(view["messages"][-1]["id"], sent["id"])
        self.assertEqual(view["messages"][-1]["body"], "Hello from normal Codex")
        self.assertEqual(view["messages"][-1]["sender_client"], "codex")

    def test_desktop_and_signed_mcp_share_project_work_with_durable_followups(self):
        account = self.bridge.identity["account_id"]
        with Store(self.state).connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "agent-work",
                    self.project,
                    "Worker",
                    account,
                    self.bridge.profile["node"],
                    "backend",
                    "model",
                    "low",
                    90,
                ),
            )
        contract = {
            "project": self.project,
            "action": "create",
            "agent": "agent-work",
            "instructions": "Investigate and verify",
            "deliverable": "Reproduction and analysis",
            "acceptance": "Record the result",
            "validation": "Check the reproduction",
            "visibility": "group",
        }
        first = self.bridge.invoke(
            "project-work", {**contract, "title": "Login", "client_request_id": str(uuid.uuid4())}
        )
        self.assertTrue(first["ok"], first)
        first_id = first["result"]["id"]
        creation = {**contract, "title": "Packaging", "client_request_id": str(uuid.uuid4())}
        second = self.backend.call("harness_project_work", creation)
        self.assertNotEqual(first_id, second["id"])
        self.assertEqual(self.backend.call("harness_project_work", creation)["id"], second["id"])
        follow_up = {
            "project": self.project,
            "action": "follow-up",
            "work_id": first_id,
            "body": "Check Wi-Fi changes too",
            "client_request_id": str(uuid.uuid4()),
        }
        self.backend.call("harness_project_work", follow_up)
        self.backend.call("harness_project_work", follow_up)
        spoof = self.backend.call(
            "harness_message_send",
            {
                "project": self.project,
                "body": json.dumps({"project_work_card": {"kind": "work", "id": first_id, "event": "delivered"}}),
            },
        )
        view = self.backend.call("harness_project_read", {"project": self.project})
        items = {item["id"]: item for item in view["work"]["items"]}
        self.assertEqual(set(items), {first_id, second["id"]})
        self.assertEqual(sum(item.get("body") == follow_up["body"] for item in items[first_id]["inputs"]), 1)
        self.assertFalse(any(item.get("body") == follow_up["body"] for item in items[second["id"]]["inputs"]))
        cards = [message for message in view["messages"] if message.get("project_work_card")]
        self.assertEqual(
            [(message["project_work_card"]["id"], message["project_work_card"]["event"]) for message in cards],
            [(first_id, "created"), (second["id"], "created"), (first_id, "follow_up")],
        )
        self.assertTrue(all(message["sender_type"] == "system" for message in cards))
        self.assertNotIn(
            "project_work_card", next(message for message in view["messages"] if message["id"] == spoof["id"])
        )
        other = self.bridge._rpc("create-project", {"name": "Another project"})["id"]
        with self.assertRaises((ValueError, PermissionError, RuntimeError)):
            self.backend.call("harness_project_work", {**follow_up, "project": other})

    def test_assigns_owned_agent_asynchronously_and_reads_status(self):
        account = self.bridge.identity["account_id"]
        node = self.bridge.profile["node"]
        with Store(self.state).connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "agent-owned",
                    self.project,
                    "Owned worker",
                    account,
                    node,
                    "backend",
                    "model",
                    "medium",
                    120,
                ),
            )
        task = self.backend.call(
            "harness_task_assign",
            {
                "project": self.project,
                "agent": "agent-owned",
                "title": "Review",
                "instructions": "Review the change",
                "stage": "review",
                "acceptance": "Report concrete findings",
                "validation": "Inspect the diff",
            },
        )
        self.assertEqual(task["state"], "queued")
        state = self.backend.call("harness_task_get", {"project": self.project, "task": task["id"]})
        self.assertEqual(state["state"], "queued")
        self.assertEqual(__import__("json").loads(state["contract"])["requested_via"], "codex")
        cancellation = self.backend.call("harness_task_cancel", {"project": "MCP project", "task": task["id"]})
        self.assertEqual(cancellation["state"], "cancelled")

    def test_unknown_task_diagnostic_preserves_owner_recovery_and_mcp_status(self):
        account = self.bridge.identity["account_id"]
        node = self.bridge.profile["node"]
        with Store(self.state).connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("agent-owned", self.project, "Worker", account, node, "backend", "model", "medium", 120),
            )
            db.execute(
                "INSERT INTO tasks(id,project_id,agent_id,title,contract,state,created_at,cutoff,job,error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "task-unknown",
                    self.project,
                    "agent-owned",
                    "Unknown",
                    "{}",
                    "unknown",
                    0,
                    0,
                    "pull-job",
                    "Unknown job",
                ),
            )
            db.execute(
                "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("pull-job", node, "agent-owned", "task", "{}", "unknown", "lease", 0),
            )
        state = self.bridge._rpc("task-status", {"task": "task-unknown"})
        self.assertEqual(state["error"], "Unknown job")
        self.assertEqual(state["local_recovery"], {"node": node, "job_id": "pull-job"})
        delegated = self.backend.call("harness_task_get", {"project": self.project, "task": "task-unknown"})
        self.assertEqual((delegated["state"], delegated["error"]), ("unknown", "Unknown job"))
        self.assertNotIn("local_recovery", delegated)

    def test_task_trace_preserves_visible_source_and_enforces_project_routing(self):
        owner = self.bridge.identity["account_id"]
        node = self.bridge.profile["node"]
        with Store(self.state).connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("agent-trace", self.project, "Trace worker", owner, node, "backend", "model", "medium", 120),
            )
        source = self.backend.call("harness_message_send", {"project": "MCP project", "body": "A proposed review"})
        task = self.backend.call(
            "harness_task_assign",
            {
                "project": "MCP project",
                "agent": "agent-trace",
                "title": "Review",
                "instructions": "Check the sample",
                "acceptance": "Report findings",
                "validation": "Inspect the sample",
                "source_message_id": source["id"],
            },
        )
        status = self.backend.call("harness_task_get", {"project": "MCP project", "task": task["id"]})
        self.assertEqual(status["trace"]["source_message_id"], source["id"])
        self.assertEqual(status["trace"]["agent_id"], "agent-trace")
        self.assertEqual(status["trace"]["node"], node)
        self.assertEqual(status["trace"]["delivery_conversation_id"], source["conversation_id"])
        view = self.backend.call("harness_project_read", {"project": "MCP project"})
        self.assertEqual(view["tasks"][0]["trace"], status["trace"])

        other = self.bridge._rpc("create-project", {"name": "Other project"}, server=self.url)["id"]
        self.backend.call("harness_projects_list", {})
        with self.assertRaises((PermissionError, ValueError)):
            self.backend.call("harness_task_get", {"project": other, "task": task["id"]})

    def test_delegate_receives_pending_request_and_uses_explicit_project_approval_policy(self):
        store = Store(self.state)
        owner = self.bridge.identity["account_id"]
        node = self.bridge.profile["node"]
        contract = '{"title":"Review","instructions":"Review","acceptance":"Done","validation":"Check"}'
        with store.connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("agent-owned", self.project, "Worker", owner, node, "backend", "model", "medium", 120),
            )
            db.execute(
                "INSERT INTO tasks(id,project_id,agent_id,title,contract,state,created_at,cutoff,processed) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("task-source", self.project, "agent-owned", "Review", contract, "needs_review", 0, 0, 1),
            )
            source = db.execute("SELECT * FROM tasks WHERE id='task-source'").fetchone()
            agent = db.execute("SELECT * FROM agents WHERE id='agent-owned'").fetchone()
            store.post_permission_request(db, self.project, agent, source, {"reason": "Review a test fixture"})

        requests = self.backend.call("harness_approvals_list", {"project": "MCP project"})["requests"]
        self.assertEqual(len(requests), 1)
        notice = requests[0]
        self.assertEqual(notice["kind"], "permission")
        self.assertEqual(notice["reason"], "Review a test fixture")
        self.assertFalse(notice["can_decide"])
        decision = {"project": "MCP project", "id": notice["id"], "kind": "permission", "approve": True}
        with self.assertRaises(PermissionError):
            self.backend.call("harness_approval_decide", decision)
        with self.assertRaises(PermissionError):
            self.backend._rpc(self.url, "mcp-approval-policy", {"project": self.project, "kinds": ["permission"]})

        configured = self.bridge.invoke("mcp-approval-policy", {"project": self.project, "kinds": ["permission"]})
        self.assertTrue(configured["ok"], configured)
        requests = self.backend.call("harness_approvals_list", {})["requests"]
        self.assertTrue(requests[0]["can_decide"])
        approved = self.backend.call("harness_approval_decide", decision)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(self.backend.call("harness_approval_decide", decision)["task_id"], approved["task_id"])
        self.assertEqual(self.backend.call("harness_approvals_list", {})["requests"], [])
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_approval_wait_checks_new_ids_and_returns_current_snapshot(self):
        old = {"id": "already-seen"}
        new = {"id": "new-request"}
        with (
            patch.object(
                self.backend, "_approval_snapshot", side_effect=[{"requests": [old]}, {"requests": [old, new]}]
            ),
            patch("agent_harness.mcp_adapter.time.sleep") as wait,
        ):
            result = self.backend.call(
                "harness_approvals_list", {"project": self.project, "seen_ids": ["already-seen"], "wait_seconds": 10}
            )
        self.assertEqual(result["requests"], [old, new])
        wait.assert_called_once_with(2)

    def test_project_names_are_sent_to_hub_as_ids(self):
        sent = self.backend.call("harness_message_send", {"project": "MCP project", "body": "Addressed by name"})
        view = self.backend.call("harness_project_read", {"project": "MCP project"})
        self.assertEqual(sent["project"], self.project)
        self.assertEqual(view["project"]["id"], self.project)
        self.assertEqual(view["messages"][-1]["body"], "Addressed by name")

    def test_incomplete_lookup_differs_from_project_not_visible(self):
        self.assertEqual(self.backend._project_route("MCP project"), (self.project, self.url))
        with patch.object(self.backend, "_rpc", side_effect=RuntimeError("offline")):
            self.assertEqual(self.backend._project_route("MCP project"), (self.project, self.url))
            with self.assertRaisesRegex(RuntimeError, "协作服务暂时无法查询"):
                self.backend._project_route("missing")
        with self.assertRaisesRegex(ValueError, "当前账户看不到这个项目"):
            self.backend._project_route("missing")

    def test_private_audience_routes_the_result_to_this_person(self):
        account = self.bridge.identity["account_id"]
        node = self.bridge.profile["node"]
        with Store(self.state).connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "agent-private",
                    self.project,
                    "Private worker",
                    account,
                    node,
                    "backend-private",
                    "model",
                    "medium",
                    120,
                ),
            )
        task = self.backend.call(
            "harness_task_assign",
            {
                "project": self.project,
                "agent": "agent-private",
                "title": "Private review",
                "instructions": "Only for me",
                "stage": "review",
                "acceptance": "Report",
                "validation": "Inspect",
                "audience": "private",
            },
        )
        contract = __import__("json").loads(
            self.backend.call("harness_task_get", {"project": self.project, "task": task["id"]})["contract"]
        )
        self.assertTrue(contract["private_turn"])
        self.assertEqual(contract["reply_to"], account)

    def test_a_dsh_delegate_is_labelled_as_dsh_but_stays_the_same_person(self):
        dsh = FoolshipMcpBackend(self.grant, client="dsh")
        sent = dsh.call(
            "harness_message_send",
            {"project": self.project, "body": "From DSH"},
        )
        view = dsh.call("harness_project_read", {"project": self.project, "after_message_id": 0})
        message = next(item for item in view["messages"] if item["id"] == sent["id"])
        self.assertEqual(message["sender_client"], "dsh")
        self.assertEqual(message["sender_id"], self.bridge.identity["account_id"])
        with Store(self.state).connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "agent-dsh",
                    self.project,
                    "DSH worker",
                    self.bridge.identity["account_id"],
                    self.bridge.profile["node"],
                    "backend-dsh",
                    "model",
                    "medium",
                    120,
                ),
            )
        task = dsh.call(
            "harness_task_assign",
            {
                "project": self.project,
                "agent": "agent-dsh",
                "title": "From DSH",
                "instructions": "x",
                "stage": "discuss",
                "acceptance": "y",
                "validation": "z",
            },
        )
        contract = __import__("json").loads(
            dsh.call("harness_task_get", {"project": self.project, "task": task["id"]})["contract"]
        )
        self.assertEqual(contract["requested_via"], "dsh")

    def test_unknown_project_and_invalid_message_id_fail_closed(self):
        with self.assertRaises(ValueError):
            self.backend.call("harness_project_read", {"project": "missing"})
        with self.assertRaises(ValueError):
            self.backend.call(
                "harness_message_send",
                {
                    "project": self.project,
                    "body": "x",
                    "client_message_id": "not-a-uuid",
                },
            )

    def test_desktop_enable_and_disable_use_named_codex_entry(self):
        configured = {}
        calls = []

        def codex(*arguments, check=True):
            calls.append(arguments)
            action, name = arguments[:2]
            if action == "get":
                value = configured.get(name)
                return SimpleNamespace(
                    returncode=0 if value else 1,
                    stdout=__import__("json").dumps(value) if value else "",
                    stderr="",
                )
            if action == "add":
                launch = list(arguments[3:])
                configured[name] = {
                    "name": name,
                    "transport": {
                        "type": "stdio",
                        "command": launch[0],
                        "args": launch[1:],
                    },
                }
            if action == "remove":
                configured.pop(name, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(self.bridge, "_codex_mcp", side_effect=codex):
            enabled = self.bridge._enable_mcp()
            self.assertTrue(enabled["enabled"])
            self.assertTrue(enabled["configured"])
            self.assertNotIn("grant", enabled)
            self.assertNotIn("configuration", enabled)
            self.assertTrue(self.bridge._mcp_grant_path().is_file())
            mcp_name = self.bridge._mcp_name()
            self.assertTrue(mcp_name.startswith("foolship-"))
            add = next(call for call in calls if call[:2] == ("add", mcp_name))
            # The entry registers a stable launcher; the adapter path lives inside
            # it, so an upgrade never changes the command Codex remembers.
            launcher = Path(str(add[3]))
            self.assertTrue(launcher.is_file())
            self.assertIn("mcp_adapter.py", launcher.read_text(encoding="utf-8"))
            disabled = self.bridge._disable_mcp()
            self.assertFalse(disabled["enabled"])
            self.assertFalse(self.bridge._mcp_grant_path().exists())
        with Store(self.state).connect() as db:
            delegate = db.execute("SELECT revoked FROM account_delegates ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(delegate["revoked"], 1)

    def test_offline_revocation_is_retained_and_retried(self):
        configured = {}
        offline = {"value": False}
        original_rpc = self.bridge._rpc

        def rpc(operation, payload=None, server=None, timeout=15):
            if operation == "revoke-delegate" and offline["value"]:
                raise RuntimeError("offline")
            return original_rpc(operation, payload, server, timeout)

        def codex(*arguments, check=True):
            action, name = arguments[:2]
            if action == "get":
                value = configured.get(name)
                return SimpleNamespace(
                    returncode=0 if value else 1,
                    stdout=__import__("json").dumps(value) if value else "",
                    stderr="",
                )
            if action == "add":
                launch = list(arguments[3:])
                configured[name] = {
                    "name": name,
                    "transport": {
                        "type": "stdio",
                        "command": launch[0],
                        "args": launch[1:],
                    },
                }
            if action == "remove":
                configured.pop(name, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(self.bridge, "_codex_mcp", side_effect=codex),
            patch.object(self.bridge, "_rpc", side_effect=rpc),
        ):
            self.bridge._enable_mcp()
            offline["value"] = True
            result = self.bridge._disable_mcp()
            self.assertEqual(result["pending_revocations"], 1)
            self.assertTrue(self.bridge._mcp_revocation_path().is_file())
            self.assertFalse(self.bridge._mcp_grant_path().exists())
            offline["value"] = False
            self.assertEqual(self.bridge._retry_mcp_revocations(), [])
            self.assertFalse(self.bridge._mcp_revocation_path().exists())

    def test_two_offline_delegate_generations_are_both_retained(self):
        configured = {}
        offline = {"value": False}
        revoked = []
        original_rpc = self.bridge._rpc

        def rpc(operation, payload=None, server=None, timeout=15):
            if operation == "revoke-delegate":
                if offline["value"]:
                    raise RuntimeError("offline")
                revoked.append(payload["public_key"])
            return original_rpc(operation, payload, server, timeout)

        def codex(*arguments, check=True):
            action, name = arguments[:2]
            if action == "get":
                value = configured.get(name)
                return SimpleNamespace(
                    returncode=0 if value else 1,
                    stdout=__import__("json").dumps(value) if value else "",
                    stderr="",
                )
            if action == "add":
                launch = list(arguments[3:])
                configured[name] = {
                    "name": name,
                    "transport": {
                        "type": "stdio",
                        "command": launch[0],
                        "args": launch[1:],
                    },
                }
            if action == "remove":
                configured.pop(name, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(self.bridge, "_codex_mcp", side_effect=codex),
            patch.object(self.bridge, "_rpc", side_effect=rpc),
        ):
            first = self.bridge._enable_mcp()
            self.assertTrue(first["configured"])
            offline["value"] = True
            self.bridge._disable_mcp()
            first_public = __import__("json").loads(self.bridge._mcp_revocation_path().read_text())["entries"][0][
                "public_key"
            ]
            self.bridge._enable_mcp()
            second_public = __import__("json").loads(self.bridge._mcp_grant_path().read_text())["identity"][
                "public_key"
            ]
            self.assertNotEqual(first_public, second_public)
            self.bridge._disable_mcp()
            entries = __import__("json").loads(self.bridge._mcp_revocation_path().read_text())["entries"]
            self.assertEqual(
                {entry["public_key"] for entry in entries},
                {first_public, second_public},
            )
            offline["value"] = False
            self.assertEqual(self.bridge._retry_mcp_revocations(), [])
            self.assertEqual(set(revoked), {first_public, second_public})
            self.assertFalse(self.bridge._mcp_revocation_path().exists())

    def test_profile_specific_name_does_not_replace_foreign_entry(self):
        foreign = {
            "name": self.bridge._mcp_name(),
            "transport": {"type": "stdio", "command": "other.exe", "args": []},
        }

        def codex(*arguments, check=True):
            if arguments[0] == "get":
                return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(foreign), stderr="")
            self.fail("foreign entry must not be modified")

        with patch.object(self.bridge, "_codex_mcp", side_effect=codex):
            with self.assertRaisesRegex(RuntimeError, "同名"):
                self.bridge._enable_mcp()


if __name__ == "__main__":
    unittest.main()
