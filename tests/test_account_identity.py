import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_harness.account_identity import (
    generate_identity,
    sign_envelope,
    verify_envelope,
    verify_owner_command,
    approve_certificate,
    approve_delegate,
    encrypt_identity,
    decrypt_identity,
    encrypt_profile,
)
from agent_harness.account_backend import AccountBackend, device_node
from agent_harness.collaboration_store import Store, digest
from agent_harness.broker import PullBackend


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.api = AccountBackend(self.store, PullBackend(self.store))
        self.root = generate_identity()
        self.store.claim_local_host(self.root["account_id"])

    def tearDown(self):
        self.temp.cleanup()

    def call(self, identity, operation, payload, action="rpc"):
        nonce = self.api.api({"action": "challenge", "public_key": identity["public_key"]})["nonce"]
        env = sign_envelope(
            identity,
            {
                "domain": "harness-account-request-v1",
                "nonce": nonce,
                "operation": operation,
                "payload": payload,
            },
        )
        return self.api.api({"action": action, "envelope": env})

    def register(self, identity=None):
        return self.call(
            identity or self.root,
            "register",
            {"name": "Test", "device_name": "Laptop"},
            "register",
        )

    def activate_delegate(self, owner, scopes=None):
        delegate = generate_identity()
        scopes = scopes if scopes is not None else ["collaboration.message", "collaboration.read", "collaboration.task"]
        delegate.update(
            account_id=owner["account_id"],
            chain=approve_delegate(owner, delegate["public_key"], scopes),
        )
        activated = self.call(delegate, "activate-device", {"device_name": "Codex MCP"}, "activate-device")
        self.assertEqual(activated["account_id"], owner["account_id"])
        return delegate

    def test_encryption(self):
        # Keep this generic contract independent of the host's real Keychain,
        # DPAPI, or Secret Service. OS envelopes have dedicated unit tests.
        with mock.patch("agent_harness.os_secrets.protect", return_value=None):
            blob = encrypt_identity(self.root, "")
        self.assertNotIn(self.root["private_key"], str(blob))
        self.assertEqual(decrypt_identity(blob, ""), self.root)
        with self.assertRaises(PermissionError):
            decrypt_identity(blob, "wrong")
        bad = copy.deepcopy(blob)
        target = bad["protected"] if bad.get("version") == 2 else bad
        field = "payload" if bad.get("version") == 2 else "ciphertext"
        target[field] = target[field][:-5] + "AAAAA"
        with self.assertRaises(PermissionError):
            decrypt_identity(bad, "")

    def test_task_trace_is_scoped_in_signed_status_and_project_messages(self):
        from tests.helpers import provision_account

        self.register()
        owner = self.root["account_id"]
        delegate = self.activate_delegate(self.root)
        project = self.call(self.root, "create-project", {"name": "Work"})["id"]
        unrelated = self.call(self.root, "create-project", {"name": "Other"})["id"]
        peer = provision_account(self.store, "peer", invite=self.store.invite(owner, project)["invite"])["id"]
        node = device_node(self.root["public_key"])
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES ('agent-1',?,'Worker',?,?,'workspace','test','medium',60)",
                (project, owner, node),
            )
        private_message = self.store.message(owner, project, "private source", recipient="agent-1")
        foreign_message = self.store.message(owner, unrelated, "another project")
        contract = {
            "project": project,
            "agent": "agent-1",
            "title": "Investigate",
            "instructions": "Inspect",
            "acceptance": "Report",
            "validation": "Review",
            "audience": "private",
        }
        with self.assertRaises(PermissionError):
            self.call(delegate, "task", {**contract, "source_message_id": foreign_message["id"]})
        task = self.call(delegate, "task", {**contract, "source_message_id": private_message["id"]})["id"]
        with self.assertRaisesRegex(ValueError, "未知任务"):
            self.call(delegate, "task-status", {"task": task, "project": unrelated})
        trace = self.call(delegate, "task-status", {"task": task, "project": project})["result"]["trace"]
        self.assertEqual(trace["source_message_id"], private_message["id"])
        self.assertEqual(trace["agent_name"], "Worker")
        self.assertEqual((trace["node"], trace["node_name"]), (node, "Laptop"))
        self.assertEqual(trace["delivery_conversation_id"], private_message["conversation_id"])

        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='needs_review',processed=1 WHERE id=?", (task,))
            p = self.store.require_member(db, project, owner)
            agent = self.store.require_agent_owner(db, "agent-1", owner)
            child = self.store._task(db, p, agent, {**contract, "audience": "group"}, owner, parent_task_id=task)["id"]
            self.store.finalize_draft(db, child, "public report", None)
        owner_view = self.store.project_view(owner, project)
        linked = next(m for m in owner_view["messages"] if m["body"] == "public report")
        self.assertEqual((linked["task_id"], linked["reply_to_message_id"]), (child, private_message["id"]))
        peer_view = self.store.project_view(peer, project)
        child_trace = next(t["trace"] for t in peer_view["tasks"] if t["id"] == child)
        self.assertEqual(child_trace["delivery_conversation_id"], self.store.group_conversation(project))
        self.assertIsNone(child_trace["source_message_id"])
        self.assertIsNone(child_trace["parent_task_id"])
        self.assertNotIn("reply_to_message_id", next(m for m in peer_view["messages"] if m["body"] == "public report"))

    def test_legacy_v1_identity_remains_compatible(self):
        legacy = encrypt_profile(self.root, "legacy-password")
        self.assertEqual(legacy["version"], 1)
        self.assertEqual(decrypt_identity(legacy, "legacy-password"), self.root)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_windows_identity_is_bound_to_dpapi_envelope(self):
        first = encrypt_identity(self.root, "password")
        second = encrypt_identity(self.root, "password")
        self.assertEqual(first["version"], 2)
        self.assertEqual(first["protected"]["backend"], "windows-dpapi")
        self.assertNotEqual(first["protected"]["payload"], second["protected"]["payload"])
        self.assertEqual(decrypt_identity(first, "password"), self.root)

    def test_ephemeral_account_requests_are_cleaned_and_bounded(self):
        child = generate_identity()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO account_challenges VALUES('expired',? ,?)",
                (child["public_key"], time.time() - 1),
            )
            db.execute(
                "INSERT INTO account_requests(id,code_hash,public_key,name,expires) VALUES('old','old',?,'old',?)",
                (child["public_key"], time.time() - 1),
            )
        self.api.api({"action": "challenge", "public_key": child["public_key"]})
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM account_challenges WHERE nonce='expired'").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM account_requests WHERE id='old'").fetchone())
        for _ in range(5):
            self.api.api(
                {
                    "action": "request-device",
                    "public_key": child["public_key"],
                    "name": "Desktop",
                }
            )
        with self.assertRaisesRegex(RuntimeError, "过多待处理"):
            self.api.api(
                {
                    "action": "request-device",
                    "public_key": child["public_key"],
                    "name": "Desktop",
                }
            )

    def test_signature_and_chain(self):
        child = generate_identity()
        child["chain"] = approve_certificate(self.root, child["public_key"])
        child["account_id"] = self.root["account_id"]
        body = {
            "domain": "harness-owner-command-v1",
            "account_id": self.root["account_id"],
            "id": "c1",
            "target": "node",
        }
        env = sign_envelope(child, body)
        self.assertEqual(verify_owner_command(env, self.root["account_id"]), body)
        other = generate_identity()
        with self.assertRaises(PermissionError):
            verify_owner_command(env, other["account_id"])
        bad = copy.deepcopy(env)
        bad["chain"][0]["body"]["child"] = other["public_key"]
        with self.assertRaises(PermissionError):
            verify_envelope(bad)
        bad = copy.deepcopy(env)
        bad["body"]["target"] = "different"
        with self.assertRaises(PermissionError):
            verify_envelope(bad)

    def test_replay_is_rejected_and_service_account_record_is_created(self):
        nonce = self.api.api({"action": "challenge", "public_key": self.root["public_key"]})["nonce"]
        env = sign_envelope(
            self.root,
            {
                "domain": "harness-account-request-v1",
                "nonce": nonce,
                "operation": "register",
                "payload": {"name": "Test"},
            },
        )
        self.api.api({"action": "register", "envelope": env})
        with self.assertRaises(PermissionError):
            self.api.api({"action": "register", "envelope": env})
        result = self.call(self.root, "dashboard", {})
        self.assertEqual(result["employees"], [])
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (self.root["account_id"],)).fetchone()
            self.assertEqual(set(row.keys()), {"id", "username"})

    def test_existing_root_identity_accepts_invite_on_another_service(self):
        with tempfile.TemporaryDirectory() as other_dir:
            other_store = Store(Path(other_dir))
            other = AccountBackend(other_store, PullBackend(other_store))
            host = generate_identity()
            other_store.claim_local_host(host["account_id"])

            def invoke(identity, operation, payload, action):
                nonce = other.api({"action": "challenge", "public_key": identity["public_key"]})["nonce"]
                envelope = sign_envelope(
                    identity,
                    {
                        "domain": "harness-account-request-v1",
                        "nonce": nonce,
                        "operation": operation,
                        "payload": payload,
                    },
                )
                return other.api({"action": action, "envelope": envelope})

            invoke(host, "register", {"name": "Host"}, "register")
            project = invoke(host, "create-project", {"name": "Shared"}, "rpc")["id"]
            invite = invoke(host, "invite", {"project": project}, "rpc")["invite"]
            payload = {
                "invite": invite,
                "name": "My existing identity",
                "device_name": "My laptop",
            }
            result = invoke(self.root, "accept-project-invite", payload, "accept-project-invite")
            self.assertEqual(
                (result["account_id"], result["project"]),
                (self.root["account_id"], project),
            )
            view = invoke(self.root, "project", {"id": project}, "rpc")
            self.assertEqual(view["project"]["id"], project)
            with self.assertRaises(PermissionError):
                invoke(self.root, "accept-project-invite", payload, "accept-project-invite")
            self.assertEqual(
                invoke(self.root, "dashboard", {}, "rpc")["user"]["id"],
                self.root["account_id"],
            )

    def test_child_identity_can_join_another_service_with_same_account_id(self):
        with tempfile.TemporaryDirectory() as other_dir:
            other_store = Store(Path(other_dir))
            other = AccountBackend(other_store, PullBackend(other_store))
            host = generate_identity()
            other_store.claim_local_host(host["account_id"])

            def invoke(identity, operation, payload, action):
                nonce = other.api({"action": "challenge", "public_key": identity["public_key"]})["nonce"]
                return other.api(
                    {
                        "action": action,
                        "envelope": sign_envelope(
                            identity,
                            {
                                "domain": "harness-account-request-v1",
                                "nonce": nonce,
                                "operation": operation,
                                "payload": payload,
                            },
                        ),
                    }
                )

            invoke(host, "register", {"name": "Host"}, "register")
            project = invoke(host, "create-project", {"name": "Shared"}, "rpc")["id"]
            invite = invoke(host, "invite", {"project": project}, "rpc")["invite"]
            child = generate_identity()
            child["chain"] = approve_certificate(self.root, child["public_key"])
            child["account_id"] = self.root["account_id"]
            result = invoke(
                child,
                "accept-project-invite",
                {
                    "invite": invite,
                    "name": "Existing",
                    "device_name": "Second computer",
                },
                "accept-project-invite",
            )
            self.assertEqual(result["account_id"], self.root["account_id"])
            self.assertEqual(
                invoke(child, "dashboard", {}, "rpc")["user"]["id"],
                self.root["account_id"],
            )
            with other_store.connect() as db:
                rows = db.execute(
                    "SELECT public_key FROM account_devices WHERE account_id=?",
                    (self.root["account_id"],),
                ).fetchall()
            self.assertEqual(
                {row["public_key"] for row in rows},
                {self.root["public_key"], child["public_key"]},
            )

    def test_device_approval_and_revocation(self):
        self.register()
        child = generate_identity()
        request = self.api.api(
            {
                "action": "request-device",
                "public_key": child["public_key"],
                "name": "Desktop",
            }
        )
        self.assertEqual(self.api.api({"action": "claim-device", **request})["state"], "pending")
        details = self.call(self.root, "inspect-device", {"code": request["code"]})
        self.assertEqual(details["public_key"], child["public_key"])
        chain = approve_certificate(self.root, child["public_key"])
        self.call(
            self.root,
            "approve-device",
            {"request_id": request["request_id"], "chain": chain},
        )
        claim = self.api.api({"action": "claim-device", **request})
        child.update(account_id=claim["account_id"], chain=claim["chain"])
        self.assertEqual(len(self.call(child, "dashboard", {})["devices"]), 2)
        self.call(self.root, "revoke-device", {"public_key": child["public_key"]})
        with self.assertRaises(PermissionError):
            self.call(child, "dashboard", {})
        with self.assertRaises(PermissionError):
            self.call(child, "activate-device", {"device_name": "Desktop"}, "activate-device")

    def test_a_device_display_name_can_be_changed(self):
        self.register()
        child = generate_identity()
        request = self.api.api({"action": "request-device", "public_key": child["public_key"], "name": "Desktop"})
        self.call(
            self.root,
            "approve-device",
            {"request_id": request["request_id"], "chain": approve_certificate(self.root, child["public_key"])},
        )
        claim = self.api.api({"action": "claim-device", **request})
        child.update(account_id=claim["account_id"], chain=claim["chain"])
        self.call(self.root, "rename-device", {"id": claim["node"], "name": "工作台"})
        names = {device["name"] for device in self.call(self.root, "dashboard", {})["devices"]}
        self.assertIn("工作台", names)
        with self.assertRaises(PermissionError):
            self.call(self.root, "rename-device", {"id": "device-unknown", "name": "x"})

    def test_a_revoked_device_can_be_associated_again(self):
        """Revoking keeps the row for audit; re-associating must reactivate it."""
        self.register()
        child = generate_identity()
        first = self.api.api({"action": "request-device", "public_key": child["public_key"], "name": "Desktop"})
        self.api.api({"action": "claim-device", **first})
        self.call(
            self.root,
            "approve-device",
            {"request_id": first["request_id"], "chain": approve_certificate(self.root, child["public_key"])},
        )
        claim = self.api.api({"action": "claim-device", **first})
        child.update(account_id=claim["account_id"], chain=claim["chain"])
        self.call(self.root, "revoke-device", {"public_key": child["public_key"]})

        second = self.api.api({"action": "request-device", "public_key": child["public_key"], "name": "Desktop again"})
        self.api.api({"action": "claim-device", **second})
        self.call(
            self.root,
            "approve-device",
            {"request_id": second["request_id"], "chain": approve_certificate(self.root, child["public_key"])},
        )
        again = self.api.api({"action": "claim-device", **second})
        self.assertEqual(again["state"], "approved")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT name, revoked FROM account_devices WHERE public_key=?",
                (child["public_key"],),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["revoked"], 0)
        self.assertEqual(rows[0]["name"], "Desktop again")

    def test_a_used_code_says_it_was_used(self):
        self.register()
        child = generate_identity()
        request = self.api.api({"action": "request-device", "public_key": child["public_key"], "name": "Desktop"})
        self.call(
            self.root,
            "approve-device",
            {"request_id": request["request_id"], "chain": approve_certificate(self.root, child["public_key"])},
        )
        with self.assertRaisesRegex(PermissionError, "使用过"):
            self.call(self.root, "inspect-device", {"code": request["code"]})

    def test_an_expired_code_says_it_expired(self):
        self.register()
        child = generate_identity()
        request = self.api.api({"action": "request-device", "public_key": child["public_key"], "name": "Desktop"})
        with self.store.connect() as db:
            db.execute(
                "UPDATE account_requests SET expires=? WHERE id=?",
                (time.time() - 1, request["request_id"]),
            )
        with self.assertRaisesRegex(PermissionError, "过期"):
            self.call(self.root, "inspect-device", {"code": request["code"]})

    def test_scoped_codex_delegate_can_collaborate_but_not_administer(self):
        self.register()
        project = self.call(self.root, "create-project", {"name": "MCP project"})["id"]
        delegate = self.activate_delegate(self.root)
        self.assertEqual(self.call(delegate, "dashboard", {})["projects"][0]["id"], project)
        sent = self.call(
            delegate,
            "message",
            {"project": project, "body": "Sent from Codex delegate"},
        )
        self.assertIsInstance(sent["id"], int)
        view = self.call(delegate, "project", {"project": project})
        self.assertEqual(view["messages"][-1]["sender_client"], "codex")
        self.assertFalse(
            any(
                device["public_key"] == delegate["public_key"]
                for device in self.call(self.root, "dashboard", {})["devices"]
            )
        )
        with self.assertRaises(PermissionError):
            self.call(delegate, "invite", {"project": project})
        with self.assertRaises(PermissionError):
            self.call(delegate, "owner-command", {"envelope": {}})
        with self.assertRaises(PermissionError):
            self.call(delegate, "revoke-device", {"public_key": self.root["public_key"]})
        with self.assertRaises(PermissionError):
            self.call(delegate, "memory-note-add", {"project": project, "body": "forged"})
        with self.store.connect() as db:
            node = device_node(self.root["public_key"])
            db.execute("INSERT INTO pull_devices(node,token_hash) VALUES(?,?)", (node, "test-project-device"))
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES ('agent-permissions',?,'Peer',?,?,'workspace','deepseek-flash','medium',60)",
                (project, self.root["account_id"], node),
            )
        source_id = self.store.task(
            self.root["account_id"],
            {
                "project": project,
                "agent": "agent-permissions",
                "title": "Check log",
                "instructions": "Inspect",
                "acceptance": "Explain",
                "validation": "Review",
            },
        )["id"]
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET state='needs_review',processed=1 WHERE id=?", (source_id,))
            source = dict(db.execute("SELECT * FROM tasks WHERE id=?", (source_id,)).fetchone())
            agent = dict(db.execute("SELECT * FROM agents WHERE id='agent-permissions'").fetchone())
            self.store.post_permission_request(db, project, agent, source, {"reason": "Test"})
        owner_view = self.call(self.root, "project", {"project": project})
        request = next(m["permission_request"] for m in owner_view["messages"] if m.get("permission_request"))
        self.assertEqual(request["agent_id"], "agent-permissions")
        permission = {"project": project, "request_id": request["id"], "approve": False}
        decision = {"project": project, "kind": "permission", "id": request["id"], "approve": True, "client": "dsh"}
        self.assertEqual(owner_view["mcp_approval_kinds"], [])
        pending = self.call(delegate, "mcp-approvals", {"project": project})["requests"]
        self.assertEqual(
            [(r["kind"], r["id"], r["can_decide"]) for r in pending], [("permission", request["id"], False)]
        )
        with self.assertRaises(PermissionError):
            self.call(delegate, "permission-decide", permission)
        with self.assertRaises(PermissionError):
            self.call(delegate, "message", {"project": project, "body": "/allow Peer"})
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", decision)
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-policy", {"project": project, "kinds": ["permission"]})
        self.assertEqual(self.store.grants("agent-permissions"), [])
        self.assertEqual(
            self.call(self.root, "mcp-approval-policy", {"project": project, "kinds": ["permission"]}),
            {"kinds": ["permission"]},
        )
        self.assertEqual(self.call(self.root, "project", {"project": project})["mcp_approval_kinds"], ["permission"])
        self.assertTrue(self.call(delegate, "mcp-approvals", {})["requests"][0]["can_decide"])
        observer = self.activate_delegate(self.root, ["collaboration.read"])
        self.assertFalse(self.call(observer, "mcp-approvals", {})["requests"][0]["can_decide"])
        with self.assertRaises(PermissionError):
            self.call(observer, "mcp-approval-decide", decision)
        approved = self.call(delegate, "mcp-approval-decide", decision)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(self.call(delegate, "mcp-approval-decide", decision), approved)
        self.assertEqual(self.call(delegate, "mcp-approvals", {})["requests"], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE project_id=?", (project,)).fetchone()[0], 2)
            audits = db.execute("SELECT actor,details FROM audit WHERE action='mcp_approval_decide'").fetchall()
            self.assertTrue(audits)
            self.assertTrue(all(row["actor"] == self.root["account_id"] for row in audits))
            self.assertTrue(
                all(json.loads(row["details"])["delegate"] == digest(delegate["public_key"]) for row in audits)
            )
            self.assertEqual(json.loads(audits[0]["details"])["client"], "dsh")
        self.call(self.root, "mcp-approval-policy", {"project": project, "kinds": []})
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", decision)
        delegate_view = self.call(delegate, "project", {"project": project})
        self.assertFalse(delegate_view["members"][0]["can_manage_project_agent"])
        self.assertFalse(any("permission_request" in m for m in delegate_view["messages"]))
        with self.assertRaises(PermissionError):
            self.call(
                delegate,
                "transfer-list",
                {},
            )
        with self.assertRaises(PermissionError):
            self.call(delegate, "set-admin", {"project": project})
        with self.assertRaises(PermissionError):
            self.call(delegate, "link-service", {"server": "http://127.0.0.1:1"})
        self.call(self.root, "revoke-delegate", {"public_key": delegate["public_key"]})
        with self.assertRaises(PermissionError):
            self.call(delegate, "dashboard", {})

    def test_delegated_attachment_approval_is_bound_to_owner_project_and_manifest(self):
        self.register()
        owner = self.root["account_id"]
        project = self.call(self.root, "create-project", {"name": "MCP attachment"})["id"]
        unrelated = self.call(self.root, "create-project", {"name": "Other"})["id"]
        delegate = self.activate_delegate(self.root)
        other = generate_identity()
        self.register(other)
        invitation = self.call(self.root, "invite", {"project": project})["invite"]
        self.store.join(other["account_id"], invitation)
        other_delegate = self.activate_delegate(other)
        self.call(other, "mcp-approval-policy", {"project": project, "kinds": ["attachment"]})
        node = device_node(self.root["public_key"])
        manifest = [{"name": "report.txt", "relative_path": "private/report.txt", "size": 4, "sha256": "a" * 64}]
        manifest_digest = "b" * 64
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES ('attachments',?,'Sender',?,?,'workspace','deepseek-flash','medium',60)",
                (project, owner, node),
            )
            db.execute(
                "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,created_at) "
                "VALUES ('job',?,'attachments','task','{}','running','lease',?)",
                (node, time.time()),
            )
            db.execute(
                "INSERT INTO tasks(id,project_id,agent_id,title,contract,state,job,created_at) "
                "VALUES ('task',?,'attachments','Private task',?,'running','job',?)",
                (project, json.dumps({"private_turn": True, "reply_to": other["account_id"]}), time.time()),
            )
            db.execute(
                "INSERT INTO attachment_approvals VALUES ('attachment','job',?,?,'attachments',?,?,?,'pending',?,?)",
                (owner, node, project, json.dumps(manifest), manifest_digest, time.time(), time.time()),
            )
        visible = self.call(delegate, "mcp-approvals", {"project": project})["requests"][0]
        self.assertEqual(
            (visible["kind"], visible["digest"], visible["destination"]), ("attachment", manifest_digest, "私聊")
        )
        self.assertNotIn("relative_path", visible["manifest"][0])
        self.assertFalse(visible["can_decide"])
        self.call(self.root, "mcp-approval-policy", {"project": project, "kinds": ["attachment"]})
        self.call(self.root, "mcp-approval-policy", {"project": unrelated, "kinds": ["attachment"]})
        self.assertEqual(self.call(other_delegate, "mcp-approvals", {"project": project})["requests"], [])
        decision = {
            "project": project,
            "kind": "attachment",
            "id": "attachment",
            "approve": True,
            "digest": manifest_digest,
        }
        with self.assertRaises(PermissionError):
            self.call(other_delegate, "mcp-approval-decide", decision)
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", {**decision, "project": unrelated})
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", {**decision, "digest": "c" * 64})
        self.assertTrue(self.call(delegate, "mcp-approvals", {})["requests"][0]["can_decide"])
        approved = self.call(delegate, "mcp-approval-decide", decision)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(self.call(delegate, "mcp-approval-decide", decision), approved)
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", {**decision, "approve": False})
        self.assertEqual(self.call(delegate, "mcp-approvals", {})["requests"], [])

    def test_delegated_execution_approval_requires_its_own_policy_and_receiver_digest(self):
        self.register()
        owner = self.root["account_id"]
        project = self.call(self.root, "create-project", {"name": "MCP execution"})["id"]
        delegate = self.activate_delegate(self.root)
        node = device_node(self.root["public_key"])
        request_id = "execution-check"
        snapshot = "d" * 64
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES ('executor',?,'Executor',?,?,'workspace','deepseek-flash','medium',60)",
                (project, owner, node),
            )
            db.execute("INSERT INTO pull_bindings VALUES ('executor','[]','[\"check\"]')")
            db.execute(
                "INSERT INTO executions(id,project_id,agent_id,template,state,created_at) "
                "VALUES (?,?,'executor','check','running',?)",
                (request_id, project, time.time()),
            )
            db.execute(
                "INSERT INTO pull_jobs(id,node,agent_id,kind,request,state,lease_id,result,created_at) "
                "VALUES (?,?,'executor','execution',?,'running','lease',?,?)",
                (
                    request_id,
                    node,
                    json.dumps({"template": "check"}),
                    json.dumps({"local_status": "waiting_local_approval"}),
                    time.time(),
                ),
            )
        self.call(self.root, "mcp-approval-policy", {"project": project, "kinds": ["permission"]})
        payload = {"project": project, "kind": "execution", "id": request_id, "approve": True, "digest": snapshot}
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", payload)
        self.call(self.root, "mcp-approval-policy", {"project": project, "kinds": ["execution"]})
        [waiting] = self.call(delegate, "mcp-approvals", {"project": project})["requests"]
        self.assertEqual((waiting["kind"], waiting["template"], waiting["id"]), ("execution", "check", request_id))
        self.assertFalse(waiting["can_decide"])
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", payload)
        with self.store.connect() as db:
            db.execute(
                "UPDATE pull_jobs SET result=? WHERE id=?",
                (
                    json.dumps({"local_status": "waiting_local_approval", "execution_approval_digest": snapshot}),
                    request_id,
                ),
            )
        [ready] = self.call(delegate, "mcp-approvals", {"project": project})["requests"]
        self.assertTrue(ready["can_decide"])
        self.assertEqual(ready["digest"], snapshot)
        denied = self.call(delegate, "mcp-approval-decide", {**payload, "approve": False})
        self.assertEqual(denied["state"], "denied")
        with self.assertRaises(PermissionError):
            self.call(delegate, "mcp-approval-decide", payload)
        self.assertEqual(self.call(delegate, "mcp-approvals", {"project": project})["requests"], [])

    def test_delegate_certificate_is_leaf_only_and_tamper_evident(self):
        delegate = generate_identity()
        delegate.update(
            account_id=self.root["account_id"],
            chain=approve_delegate(self.root, delegate["public_key"], ["collaboration.read"]),
        )
        verified = verify_envelope(sign_envelope(delegate, {"probe": True}))
        self.assertEqual(verified["delegate_scopes"], ("collaboration.read",))
        child = generate_identity()
        child.update(
            account_id=self.root["account_id"],
            chain=delegate["chain"] + approve_certificate(delegate, child["public_key"])[-1:],
        )
        with self.assertRaises(PermissionError):
            verify_envelope(sign_envelope(child, {"probe": True}))
        bad = copy.deepcopy(sign_envelope(delegate, {"probe": True}))
        bad["chain"][-1]["body"]["scopes"] = ["collaboration.task"]
        with self.assertRaises(PermissionError):
            verify_envelope(bad)

    def approve_child(self, parent):
        child = generate_identity()
        request = self.api.api(
            {
                "action": "request-device",
                "public_key": child["public_key"],
                "name": "Child",
            }
        )
        self.call(
            parent,
            "approve-device",
            {
                "request_id": request["request_id"],
                "chain": approve_certificate(parent, child["public_key"]),
            },
        )
        claim = self.api.api({"action": "claim-device", **request})
        child.update(account_id=claim["account_id"], chain=claim["chain"])
        return child

    def test_root_revoke_and_descendant_cascade(self):
        from agent_harness.account_backend import device_node
        from agent_harness.collaboration_store import digest

        self.register()
        child = self.approve_child(self.root)
        grandchild = self.approve_child(child)
        self.assertTrue(
            next(
                d
                for d in self.call(self.root, "dashboard", {})["devices"]
                if d["public_key"] == self.root["public_key"]
            )["is_root"]
        )
        with self.assertRaises(ValueError):
            self.call(child, "revoke-device", {"id": device_node(self.root["public_key"])})
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO pull_devices VALUES(?,?,?)",
                (
                    device_node(grandchild["public_key"]),
                    digest("synthetic-token-only"),
                    0,
                ),
            )
        command = sign_envelope(
            self.root,
            {
                "domain": "harness-owner-command-v1",
                "account_id": self.root["account_id"],
                "id": "cascade-command",
                "target": device_node(grandchild["public_key"]),
                "action": "browse",
                "data": {},
            },
        )
        self.call(self.root, "owner-command", {"envelope": command})
        self.call(self.root, "revoke-device", {"id": device_node(child["public_key"])})
        with self.assertRaises(PermissionError):
            self.call(grandchild, "dashboard", {})
        with self.store.connect() as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM pull_devices WHERE node=?",
                    (device_node(grandchild["public_key"]),),
                ).fetchone()
            )
            self.assertEqual(
                db.execute("SELECT state FROM owner_commands WHERE id='cascade-command'").fetchone()["state"],
                "failed",
            )
        self.assertEqual(len(self.call(self.root, "dashboard", {})["devices"]), 1)

    def test_withdraw_filters_without_deleting_history(self):
        import json
        from agent_harness.account_backend import device_node
        from agent_harness.owner_worker import execution_project

        self.register()
        project = self.call(self.root, "create-project", {"name": "Project"})["id"]
        user = self.root["account_id"]
        node = device_node(self.root["public_key"])
        backend = execution_project("employee", project)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO employees VALUES(?,?,?,?,?,?,?)",
                ("employee", "Employee", user, node, "{}", "idle", 0),
            )
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "agent",
                    project,
                    "Employee",
                    user,
                    node,
                    backend,
                    "model",
                    "medium",
                    120,
                ),
            )
            body = {
                "action": "withdraw",
                "data": {"id": "employee", "project_id": project},
            }
            db.execute(
                "INSERT INTO owner_commands VALUES(?,?,?,?,?,?,?)",
                (
                    "withdraw",
                    user,
                    node,
                    json.dumps({"body": body}),
                    "applied",
                    "{}",
                    1,
                ),
            )
        self.assertEqual(self.call(self.root, "project", {"id": project})["members"], [])
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute("SELECT * FROM agents WHERE id='agent'").fetchone())

    def test_cross_account_approval(self):
        self.register()
        other = generate_identity()
        self.register(other)
        child = generate_identity()
        request = self.api.api(
            {
                "action": "request-device",
                "public_key": child["public_key"],
                "name": "Desktop",
            }
        )
        with self.assertRaises(PermissionError):
            self.call(
                other,
                "approve-device",
                {
                    "request_id": request["request_id"],
                    "chain": approve_certificate(self.root, child["public_key"]),
                },
            )


if __name__ == "__main__":
    unittest.main()
