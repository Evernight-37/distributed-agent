import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_harness import owner_backend as backend
from agent_harness import owner_worker as worker
from agent_harness.broker import PullBackend
from agent_harness.collaboration_store import Store
from agent_harness.common import read_json, write_json


# These tests isolate catalog/control behavior. Signature cryptography is exercised
# by account_identity's own suite; no production verification is replaced.
def verify(envelope, expected_account):
    if envelope.get("signed_by") != expected_account:
        raise PermissionError("bad signature")


class FakeConnector:
    def __init__(self, root):
        self.root = Path(root)
        self.config = {"owner_account_id": "u1", "node": "n1"}
        self.saved = []
        self.calls = []

    def bindings(self):
        return self.saved[:]

    def save_binding(self, b):
        self.saved = [x for x in self.saved if x["backend_project"] != b["backend_project"]] + [dict(b)]
        return b

    def executor(self, r):
        self.calls.append(r)
        if r["action"] == "register":
            write_json(
                self.root / "projects" / r["project"]["name"] / "project.json",
                r["project"],
            )
        elif r["action"] == "configure":
            path = self.root / "projects" / r["project"] / "project.json"
            p = read_json(path)
            p.update({k: r[k] for k in ("model", "effort", "timeout", "strict_worktree")})
            if "model_config_version" in r:
                p["model_config_version"] = r["model_config_version"]
            write_json(path, p)
        return {}


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.backend_verifier = patch.object(backend, "verify_owner_command", verify)
        self.worker_verifier = patch.object(worker, "verify_owner_command", verify)
        self.backend_verifier.start()
        self.worker_verifier.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root / "store")
        self.backend = backend.OwnerBackend(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO users(id,username) VALUES('u1','one')")
            db.execute("INSERT INTO users(id,username) VALUES('u2','two')")
            db.execute("INSERT INTO nodes VALUES('n1','u1')")
            db.execute("INSERT INTO nodes VALUES('n2','u2')")
            db.execute("INSERT INTO projects(id,name,owner_id) VALUES('p1','One','u1')")
            db.execute("INSERT INTO project_users VALUES('p1','u1')")
        self.connector = FakeConnector(self.root / "device")
        self.control = worker.OwnerControl(self.connector)
        self.counter = 0
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()
        self.worker_verifier.stop()
        self.backend_verifier.stop()

    def envelope(self, action, data, target="n1", owner="u1", cid=None):
        self.counter += 1
        return {
            "signed_by": owner,
            "body": {
                "domain": "harness-owner-command-v1",
                "account_id": owner,
                "target": target,
                "id": cid or str(self.counter),
                "action": action,
                "data": data,
            },
        }

    def create(self):
        return self.control.apply(self.envelope("create", {"id": "e1", "name": "Employee"}))

    def configure(self):
        return self.control.apply(
            self.envelope(
                "configure",
                {
                    "id": "e1",
                    "settings": {
                        "model": "gpt-5.6-sol",
                        "effort": "high",
                        "enabled": True,
                        "strict_worktree": True,
                    },
                },
            )
        )

    def configure_workspace(self, project="p1", path=None):
        return self.control.apply(
            self.envelope(
                "workspace-configure",
                {"project_id": project, "path": str(path or self.workspace)},
            )
        )

    def configure_default_workspace(self, project="p1", cid=None):
        return self.control.apply(
            self.envelope(
                "workspace-default",
                {"project_id": project},
                cid=cid,
            )
        )

    def configure_receive_directory(self, path, cid=None):
        return self.control.apply(
            self.envelope(
                "transfer-receive-directory",
                {"path": str(path)},
                cid=cid,
            )
        )

    def apply_owner_command(self, action, data):
        envelope = self.envelope(action, data)
        device = {"node": "n1", "owner_id": "u1"}
        self.backend.enqueue("u1", envelope)
        with self.store.connect() as db:
            self.assertIn(envelope, self.backend.poll(db, device))
        response = self.control.apply(envelope)
        with self.store.connect() as db:
            self.backend.report(db, device, envelope["body"]["id"], **response)
        return response

    def test_model_capabilities_command_uses_same_device_and_reuses_recent_result(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO pull_devices(node,token_hash,last_seen) VALUES('n1','token',?)", (time.time(),))
        results = [{"name": "demo", "executor": "codex", "efforts": ["high"], "default_effort": "high"}]
        with patch("agent_harness.model_capabilities.snapshot", return_value=results):
            applied = self.apply_owner_command("model-capabilities", {})
        self.assertEqual(applied["result"]["models"], results)
        cached = self.backend.enqueue("u1", self.envelope("model-capabilities", {}))
        self.assertEqual(cached["state"], "applied")
        self.assertEqual(cached["result"], applied["result"])
        with self.assertRaises(PermissionError):
            self.backend.enqueue("u2", self.envelope("model-capabilities", {}, owner="u2"))

    def advertise_project_agents(self):
        with self.store.connect() as db:
            PullBackend(self.store)._advertise(db, {"node": "n1", "owner_id": "u1"}, self.connector.bindings())
            return {row["project_id"]: dict(row) for row in db.execute("SELECT * FROM agents WHERE node='n1'")}

    def test_project_agent_lifecycle_preserves_binding_and_needs_no_employee(self):
        self.configure_workspace()
        settings = {"model": "gpt-5.6-sol", "effort": "low", "enabled": True}
        created = self.apply_owner_command(
            "project-agent-create", {"id": "within-project", "project_id": "p1", "name": "Agent", "settings": settings}
        )
        self.assertEqual(created["state"], "applied", created)
        self.assertEqual(self.backend.snapshot("u1")["employees"], [])
        project = self.advertise_project_agents()["p1"]
        target = {k: project[k] for k in ("project_id", "backend_project")}
        target["agent_id"] = project["id"]
        directory = self.connector.root / "projects" / target["backend_project"]
        write_json(directory / "session.json", {"thread_id": "original-session"})
        configured = self.apply_owner_command(
            "project-agent-configure",
            dict(target, name="Agent", settings=dict(settings, model="deepseek-flash", strict_worktree=True)),
        )
        self.assertEqual(configured["state"], "applied", configured)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT model FROM agents WHERE id=?", (target["agent_id"],)).fetchone()[0],
                "deepseek-flash",
            )
        current = self.advertise_project_agents()["p1"]
        self.assertEqual((current["id"], current["model"]), (target["agent_id"], "deepseek-flash"))
        write_json(directory / "active.json", {"job": "running"})
        attempt = self.apply_owner_command("project-agent-delete", target)
        self.assertEqual(attempt["state"], "failed")
        self.assertTrue(self.connector.bindings()[0]["enabled"])
        write_json(directory / "active.json", None)
        deleted = self.apply_owner_command("project-agent-delete", target)
        self.assertEqual(deleted["state"], "applied", deleted)
        current = self.advertise_project_agents()["p1"]
        self.assertEqual(current["id"], target["agent_id"])
        self.assertFalse(current["enabled"])
        self.assertEqual(read_json(directory / "session.json"), {"thread_id": "original-session"})

    def test_agent_workspaces_are_private_and_survive_shared_workspace_changes(self):
        from agent_harness import sealing

        pair = sealing.generate_keypair()
        self.connector.config["encrypt_key"] = pair

        def create(identity, mode, path=None):
            data = dict(
                id=identity,
                project_id="p1",
                name="Agent",
                settings={"model": "gpt-5.6-sol", "effort": "low"},
                workspace_mode=mode,
            )
            if path is not None:
                data["sealed_workspace"] = sealing.seal(
                    pair["public_key"], {"path": str(path)}, {"target": "n1", "id": identity, "project": "p1"}
                )
            return self.apply_owner_command("project-agent-create", data)

        roots = []
        for identity, mode, path in (
            ("a", "dedicated", None),
            ("b", "dedicated", None),
            ("c", "custom", self.workspace),
        ):
            response = create(identity, mode, path)
            self.assertEqual(response["state"], "applied", response)
            record = self.connector.root / "projects" / response["result"]["backend_project"] / "project.json"
            roots.append((record, Path(read_json(record)["remote_path"])))
        self.assertEqual(roots[-1][1], self.workspace)
        self.assertEqual(len({path for _, path in roots}), 3)
        self.assertTrue(all(path.is_dir() for _, path in roots))
        self.assertEqual(create("duplicate", "custom", self.workspace)["state"], "failed")
        self.assertEqual(create("missing", "custom", self.workspace / "absent")["state"], "failed")
        self.assertEqual(create("relative", "custom", "relative/path")["state"], "failed")
        self.assertFalse(self.control._workspaces())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_workspaces").fetchone()[0], 0)
            for row in db.execute("SELECT envelope,result FROM owner_commands"):
                self.assertNotIn(str(self.workspace), str(tuple(row)))
        self.assertEqual(self.configure_default_workspace()["state"], "applied")
        self.assertEqual(self.configure_workspace(path=self.root)["state"], "applied")
        for record, path in roots:
            self.assertEqual(Path(read_json(record)["remote_path"]), path)

    def test_native_project_agent_effort_is_validated_on_the_receiver(self):
        self.configure_workspace()
        settings = {"model": "deepseek-flash", "effort": "xhigh", "enabled": True, "model_config_version": 2}
        data = {"id": "native-agent", "project_id": "p1", "name": "Agent", "settings": settings}
        with patch("agent_harness.model_capabilities.for_model", return_value={"efforts": ["off", "high", "max"]}):
            rejected = self.apply_owner_command("project-agent-create", data)
            self.assertEqual(rejected["state"], "failed")
            self.assertEqual(self.connector.calls, [])
            settings["effort"] = "max"
            applied = self.apply_owner_command("project-agent-create", data)
        self.assertEqual(applied["state"], "applied", applied)
        self.assertEqual(applied["result"]["settings"]["effort"], "max")
        backend_project = applied["result"]["backend_project"]
        project_file = self.connector.root / "projects" / backend_project / "project.json"
        self.assertEqual(read_json(project_file)["model_config_version"], 2)

        project = self.advertise_project_agents()["p1"]
        target = {"agent_id": project["id"], "project_id": "p1", "backend_project": backend_project}
        settings["effort"] = "default"
        with patch("agent_harness.model_capabilities.for_model") as capability:
            changed = self.apply_owner_command("project-agent-configure", dict(target, name="Agent", settings=settings))
            capability.assert_not_called()
        self.assertEqual(changed["state"], "applied", changed)
        self.assertEqual(read_json(project_file)["effort"], "default")

    def test_editing_a_legacy_assignment_detaches_it_from_global_configure(self):
        self.assertEqual(self.apply_owner_command("create", {"id": "e1", "name": "Employee"})["state"], "applied")
        self.configure()
        self.configure_workspace()
        with self.store.connect() as db:
            db.execute("INSERT INTO projects(id,name,owner_id) VALUES('p2','Two','u1')")
            db.execute("INSERT INTO project_users VALUES('p2','u1')")
        self.configure_workspace("p2")
        for project_id in ("p1", "p2"):
            self.assertEqual(
                self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": project_id}))["state"],
                "applied",
            )
        first = self.advertise_project_agents()["p1"]
        response = self.apply_owner_command(
            "project-agent-configure",
            {
                "agent_id": first["id"],
                "project_id": "p1",
                "backend_project": first["backend_project"],
                "name": "Project-specific",
                "settings": {"model": "deepseek-flash", "effort": "low", "enabled": True},
            },
        )
        self.assertEqual(response["state"], "applied", response)
        self.assertEqual(self.configure()["state"], "applied")
        second_dispatch = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        self.assertEqual(second_dispatch["state"], "failed")
        projects = self.advertise_project_agents()
        self.assertEqual(projects["p1"]["id"], first["id"])
        self.assertEqual(projects["p1"]["model"], "deepseek-flash")
        self.assertEqual(projects["p2"]["model"], "gpt-5.6-sol")
        self.assertEqual(self.apply_owner_command("delete", {"id": "e1"})["state"], "applied")
        projects = self.advertise_project_agents()
        self.assertTrue(projects["p1"]["enabled"])
        self.assertFalse(projects["p2"]["enabled"])

    def test_project_agent_commands_validate_project_node_and_unsealed_identity(self):
        data = {
            "id": "within-project",
            "project_id": "missing",
            "name": "Agent",
            "settings": {"model": "gpt-5.6-sol", "effort": "low", "enabled": True},
        }
        with self.assertRaises(PermissionError):
            self.backend.enqueue("u1", self.envelope("project-agent-create", data))
        with self.assertRaises(PermissionError):
            self.backend.enqueue("u1", self.envelope("project-agent-create", dict(data, project_id="p1"), target="n2"))
        sealed = self.envelope("project-agent-create", dict(data, project_id="p1"))
        sealed["body"]["sealed"] = {}
        with self.assertRaises(ValueError):
            self.backend.enqueue("u1", sealed)
        self.assertEqual(self.control.apply(sealed)["state"], "failed")

    def test_project_remove_records_applied_tombstone_for_a_revoked_device(self):
        node = backend.device_node("orphaned-project-key")
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes VALUES(?,?)", (node, "u1"))
            db.execute(
                "INSERT INTO account_devices VALUES(?,?,?,?,1)",
                ("orphaned-project-key", "u1", "Old machine", "[]"),
            )
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES ('orphan-agent','p1','Old Agent','u1',?,'old-agent','deepseek-flash','low',60)",
                (node,),
            )
        delete = self.envelope(
            "project-agent-delete",
            {"agent_id": "orphan-agent", "backend_project": "old-agent", "project_id": "p1"},
            target=node,
        )
        result = self.backend.enqueue("u1", delete)
        self.assertEqual(result["state"], "applied")
        self.assertEqual(
            self.backend.command("u1", result["id"])["result"],
            {"project_id": "p1", "backend_project": "old-agent", "enabled": False},
        )
        self.assertTrue(self.backend.enqueue("u1", delete)["duplicate"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT enabled FROM agents WHERE id='orphan-agent'").fetchone()[0], 0)

    def test_strict_worktree_setting_propagates_to_binding_and_registration(self):
        self.create()
        result = self.configure()
        self.assertEqual(self.configure_workspace()["state"], "applied")
        self.assertTrue(result["result"]["settings"]["strict_worktree"])
        dispatched = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        self.assertTrue(dispatched["result"]["enabled"])
        self.assertTrue(self.connector.saved[0]["strict_worktree"])
        registered = read_json(
            self.root / "device" / "projects" / self.connector.saved[0]["backend_project"] / "project.json"
        )
        self.assertTrue(registered["strict_worktree"])

    def test_strict_worktree_setting_updates_an_existing_registration(self):
        self.create()
        self.configure()
        self.configure_workspace()
        self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        update = self.envelope(
            "configure",
            {
                "id": "e1",
                "settings": {
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "enabled": True,
                    "strict_worktree": False,
                },
            },
        )
        self.assertEqual(self.control.apply(update)["state"], "applied")
        project = read_json(
            self.root / "device" / "projects" / self.connector.saved[0]["backend_project"] / "project.json"
        )
        self.assertFalse(project["strict_worktree"])

    def test_a_sealed_payload_never_reaches_the_hub_in_the_clear(self):
        from agent_harness import sealing

        pair = sealing.generate_keypair()
        self.connector.config["encrypt_key"] = pair
        self.create()
        self.configure()
        self.configure_workspace()
        self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        # A second configure carries a different model, sealed. Only the routing
        # field stays readable, so the Hub can authorize but not read the payload.
        command_id = "sealed-1"
        env = self.envelope("configure", {"id": "e1"}, cid=command_id)
        env["body"]["sealed"] = sealing.seal(
            pair["public_key"],
            {
                "id": "e1",
                "settings": {
                    "model": "deepseek-flash",
                    "effort": "high",
                    "enabled": True,
                    "strict_worktree": False,
                },
                "sentinel": "不能出现在服务端",
            },
            {"target": "n1", "id": command_id},
        )
        self.assertNotIn("不能出现在服务端", str(env))
        self.assertNotIn("deepseek-flash", str(env))
        self.assertEqual(self.control.apply(env)["state"], "applied")
        project = read_json(
            self.root / "device" / "projects" / self.connector.saved[0]["backend_project"] / "project.json"
        )
        self.assertEqual(project["model"], "deepseek-flash")

    def test_ownership_and_ack(self):
        env = self.envelope("create", {"id": "e1", "name": "Employee"})
        self.backend.enqueue("u1", env)
        self.assertEqual(self.backend.snapshot("u1")["employees"][0]["settings"], {})
        self.assertEqual(self.backend.snapshot("u2")["employees"], [])
        with self.assertRaises(PermissionError):
            self.backend.enqueue("u1", self.envelope("browse", {}, target="n2"))
        with self.store.connect() as db:
            self.assertEqual(len(self.backend.poll(db, {"node": "n1", "owner_id": "u1"})), 1)
            with self.assertRaises(PermissionError):
                self.backend.report(
                    db,
                    {"node": "n2", "owner_id": "u2"},
                    env["body"]["id"],
                    "applied",
                    {},
                )
            result = {"settings": {"enabled": False}, "name": "Employee"}
            self.backend.report(
                db,
                {"node": "n1", "owner_id": "u1"},
                env["body"]["id"],
                "applied",
                result,
            )
            self.assertTrue(
                self.backend.report(
                    db,
                    {"node": "n1", "owner_id": "u1"},
                    env["body"]["id"],
                    "applied",
                    result,
                )["duplicate"]
            )
            with self.assertRaises(ValueError):
                self.backend.report(
                    db,
                    {"node": "n1", "owner_id": "u1"},
                    env["body"]["id"],
                    "failed",
                    {},
                )
        self.assertEqual(self.backend.snapshot("u1")["employees"][0]["revision"], 1)

    def test_membership_and_no_execution_settings(self):
        self.backend.enqueue("u1", self.envelope("create", {"id": "e1", "name": "Employee"}))
        with self.assertRaises(PermissionError):
            self.backend.enqueue("u1", self.envelope("dispatch", {"id": "e1", "project_id": "unknown"}))
        self.create()
        response = self.control.apply(self.envelope("configure", {"id": "e1", "settings": {"shell": "do anything"}}))
        self.assertEqual(response["state"], "failed")
        self.assertEqual(self.connector.calls, [])

    def test_idempotency(self):
        env = self.envelope("create", {"id": "e1", "name": "Employee"}, cid="repeat")
        self.backend.enqueue("u1", env)
        self.assertTrue(self.backend.enqueue("u1", env)["duplicate"])
        changed = self.envelope("create", {"id": "e1", "name": "Different"}, cid="repeat")
        with self.assertRaises(ValueError):
            self.backend.enqueue("u1", changed)
        response = self.control.apply(env)
        self.assertEqual(response, self.control.apply(env))
        with self.assertRaises(ValueError):
            self.control.apply(changed)

    def test_signature_target(self):
        with self.assertRaises(PermissionError):
            self.control.apply(self.envelope("browse", {}, target="n2"))
        with self.assertRaises(PermissionError):
            self.control.apply(self.envelope("browse", {}, owner="u2"))

    def test_sessions_and_withdraw(self):
        self.create()
        self.assertEqual(self.configure()["state"], "applied")
        self.configure_workspace("p1")
        second_workspace = self.root / "workspace-two"
        second_workspace.mkdir()
        self.configure_workspace("p2", second_workspace)
        first = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        second = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p2"}))
        self.assertEqual(first["state"], "applied")
        self.assertEqual(second["state"], "applied")
        self.assertNotEqual(first["result"]["backend_project"], second["result"]["backend_project"])
        backend_name = first["result"]["backend_project"]
        write_json(
            self.connector.root / "projects" / backend_name / "active.json",
            {"job": "running"},
        )
        self.assertEqual(self.configure()["state"], "failed")
        withdrawn = self.control.apply(self.envelope("withdraw", {"id": "e1", "project_id": "p1"}))
        self.assertEqual(withdrawn["state"], "applied")
        self.assertTrue(read_json(self.connector.root / "projects" / backend_name / "active.json"))

    def test_delete_requires_owner_confirmation_and_preserves_work_files(self):
        create = self.envelope("create", {"id": "e1", "name": "Employee"})
        self.backend.enqueue("u1", create)
        self.create()
        self.configure()
        self.configure_workspace()
        dispatched = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "delete-test-agent",
                    "p1",
                    "Employee",
                    "u1",
                    "n1",
                    dispatched["result"]["backend_project"],
                    "gpt-5.6-sol",
                    "low",
                    30,
                ),
            )
            db.execute("UPDATE projects SET admin_agent_id='delete-test-agent' WHERE id='p1'")
        work = self.workspace / "keep.txt"
        work.write_text("research")
        delete = self.envelope("delete", {"id": "e1"})
        with self.assertRaises(PermissionError):
            self.backend.enqueue("u2", self.envelope("delete", {"id": "e1"}, target="n2", owner="u2"))
        self.backend.enqueue("u1", delete)
        self.assertEqual(len(self.backend.snapshot("u1")["employees"]), 1)
        with self.store.connect() as db:
            self.backend.poll(db, {"node": "n1", "owner_id": "u1"})
            response = self.control.apply(delete)
            self.assertEqual(response["state"], "applied")
            self.backend.report(db, {"node": "n1", "owner_id": "u1"}, delete["body"]["id"], **response)
        self.assertEqual(self.backend.snapshot("u1")["employees"], [])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT enabled FROM agents WHERE id='delete-test-agent'").fetchone()[0],
                0,
            )
            self.assertIsNone(db.execute("SELECT admin_agent_id FROM projects WHERE id='p1'").fetchone()[0])
        self.assertEqual(work.read_text(), "research")
        self.assertTrue(all(not b["enabled"] and not b["assigned"] for b in self.connector.bindings()))
        self.assertEqual(self.control.apply(delete), response)
        self.assertEqual(self.configure()["state"], "failed")

    def test_delete_rejects_active_work_without_disabling_bindings(self):
        self.create()
        self.configure()
        self.configure_workspace()
        response = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        backend = response["result"]["backend_project"]
        active = self.connector.root / "projects" / backend / "active.json"
        write_json(active, {"job": "running"})
        deleted = self.control.apply(self.envelope("delete", {"id": "e1"}))
        self.assertEqual(deleted["state"], "failed")
        self.assertTrue(self.connector.bindings()[0]["enabled"])
        self.assertTrue(active.is_file())

    def test_browse_and_unknown_receipt(self):
        (self.workspace / "folder").mkdir()
        (self.workspace / "secret.txt").write_text("not disclosed")
        response = self.control.apply(self.envelope("browse", {"path": str(self.workspace)}))
        self.assertEqual(
            response["result"]["children"],
            [{"name": "folder", "path": str(self.workspace / "folder")}],
        )
        env = self.envelope("browse", {"path": str(self.workspace)})
        receipt = self.connector.root / "owner-receipts" / (worker.key(env["body"]["id"]) + ".json")
        write_json(
            receipt,
            {
                "fingerprint": worker.key(worker.canonical(env["body"])),
                "phase": "started",
            },
        )
        self.assertEqual(self.control.apply(env)["state"], "failed")

    def test_workspace_belongs_to_shared_project_not_employee(self):
        self.create()
        self.configure()
        second = self.control.apply(self.envelope("create", {"id": "e2", "name": "Second"}))
        self.assertEqual(second["state"], "applied")
        self.control.apply(
            self.envelope(
                "configure",
                {
                    "id": "e2",
                    "settings": {
                        "model": "gpt-5.6-sol",
                        "effort": "medium",
                        "enabled": True,
                    },
                },
            )
        )
        self.configure_workspace()
        first = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        other = self.control.apply(self.envelope("dispatch", {"id": "e2", "project_id": "p1"}))
        self.assertNotEqual(first["result"]["backend_project"], other["result"]["backend_project"])
        for binding in self.connector.bindings():
            self.assertNotIn("remote_path", binding)
            project = read_json(self.connector.root / "projects" / binding["backend_project"] / "project.json")
            self.assertEqual(project["remote_path"], str(self.workspace.resolve()))

    def test_remote_default_workspace_is_contained_idempotent_and_reported_ready(self):
        command = self.envelope("workspace-default", {"project_id": "p1"}, cid="default-workspace")
        queued = self.backend.enqueue("u1", command)
        self.assertEqual(queued["state"], "queued")
        with self.assertRaises(PermissionError):
            self.backend.enqueue(
                "u1",
                self.envelope(
                    "workspace-default",
                    {"project_id": "missing-project"},
                    cid="wrong-project",
                ),
            )
        with self.assertRaises(PermissionError):
            self.backend.enqueue(
                "u1",
                self.envelope(
                    "workspace-default",
                    {"project_id": "p1"},
                    target="n2",
                    cid="wrong-device",
                ),
            )
        response = self.control.apply(command)
        self.assertEqual(response["state"], "applied")
        self.assertNotIn("remote_path", response["result"])
        registry = read_json(self.connector.root / "project-workspaces.json")
        workspace = Path(registry["workspaces"]["p1"]["remote_path"]).resolve()
        self.assertTrue(workspace.is_relative_to((self.connector.root / "workspaces").resolve()))
        self.assertTrue(workspace.is_dir())
        self.assertEqual(response, self.control.apply(command))
        other = self.configure_default_workspace(cid="another-default-command")
        self.assertTrue(other["result"]["existing"])
        self.assertEqual(
            Path(read_json(self.connector.root / "project-workspaces.json")["workspaces"]["p1"]["remote_path"]),
            workspace,
        )
        with self.store.connect() as db:
            self.backend.poll(db, {"node": "n1", "owner_id": "u1"})
            self.backend.report(db, {"node": "n1", "owner_id": "u1"}, command["body"]["id"], **response)
            stored = db.execute("SELECT status FROM project_workspaces WHERE project_id='p1' AND node='n1'").fetchone()
        self.assertEqual(stored["status"], "ready")

    def test_remote_default_workspace_waits_for_active_project(self):
        self.create()
        self.configure()
        self.configure_workspace()
        dispatched = self.control.apply(self.envelope("dispatch", {"id": "e1", "project_id": "p1"}))
        registry = self.connector.root / "project-workspaces.json"
        write_json(registry, {"version": 1, "workspaces": {}})
        write_json(
            self.connector.root / "projects" / dispatched["result"]["backend_project"] / "active.json",
            {"job": "running"},
        )
        response = self.configure_default_workspace(cid="blocked-default")
        self.assertEqual(response["state"], "failed")
        self.assertFalse(read_json(registry)["workspaces"])

    def test_receive_directory_is_local_idempotent_and_not_reported(self):
        receive = self.root / "received"
        receive.mkdir()
        command = self.envelope(
            "transfer-receive-directory",
            {"path": str(receive)},
            cid="receive-directory",
        )
        queued = self.backend.enqueue("u1", command)
        self.assertEqual(queued["state"], "queued")
        with self.assertRaises(PermissionError):
            self.backend.enqueue(
                "u1",
                self.envelope(
                    "transfer-receive-directory",
                    {"path": str(receive)},
                    target="n2",
                    cid="wrong-receive-device",
                ),
            )
        response = self.control.apply(command)
        self.assertEqual(response, {"state": "applied", "result": {"configured": True}})
        self.assertEqual(response, self.control.apply(command))
        settings = read_json(self.connector.root / "file-transfer-settings.json")
        self.assertEqual(
            settings,
            {"version": 1, "receive_folder": str(receive.resolve())},
        )
        with self.store.connect() as db:
            self.backend.poll(db, {"node": "n1", "owner_id": "u1"})
            self.backend.report(db, {"node": "n1", "owner_id": "u1"}, command["body"]["id"], **response)
            row = db.execute("SELECT result,envelope FROM owner_commands WHERE id='receive-directory'").fetchone()
            stored = row["result"]
            workspace_count = db.execute("SELECT COUNT(*) FROM project_workspaces").fetchone()[0]
        self.assertNotIn(str(receive), stored)
        self.assertNotIn(str(receive), row["envelope"])
        self.assertIn("[redacted]", row["envelope"])
        self.assertEqual(workspace_count, 0)

    def test_an_orphaned_employee_can_be_deleted(self):
        """A machine that was re-associated keeps its old node but loses its device."""
        node = backend.device_node("orphaned-public-key")
        with self.store.connect() as db:
            db.execute("INSERT INTO nodes VALUES(?,?)", (node, "u1"))
            db.execute(
                "INSERT INTO account_devices VALUES(?,?,?,?,1)",
                ("orphaned-public-key", "u1", "Old machine", "[]"),
            )
        self.backend.enqueue(
            "u1",
            self.envelope("create", {"id": "e-orphan", "name": "斯科特"}, target=node),
        )
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO agents(id,project_id,name,owner_id,node,backend_project,model,effort,timeout) "
                "VALUES ('agent-orphan','p1','斯科特','u1',?,?,'deepseek-flash','medium',60)",
                (node, worker.execution_project("e-orphan", "p1")),
            )
        self.store.set_grant("agent-orphan", "full_access", True, actor="u1")
        result = self.backend.enqueue(
            "u1",
            self.envelope("delete", {"id": "e-orphan"}, target=node),
        )
        self.assertTrue(result["applied"])
        self.assertEqual(self.store.grants("agent-orphan"), [])
        self.assertEqual(self.backend.snapshot("u1")["employees"], [])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM owner_commands WHERE node=?",
                    (node,),
                ).fetchone()[0],
                0,
            )
        self.assertTrue(self.store.events_wait("u1", 0, 0)["changed"])

    def test_receive_directory_rejects_unsafe_paths_and_creates_missing_ones(self):
        missing = self.root / "missing"
        regular_file = self.root / "file.txt"
        regular_file.write_text("not a directory")
        for index, candidate in enumerate(("relative/path", Path(self.root.anchor), regular_file)):
            with self.subTest(candidate=candidate):
                response = self.configure_receive_directory(
                    candidate,
                    cid=f"invalid-receive-{index}",
                )
                self.assertEqual(response["state"], "failed")
        # A directory the owner names does not have to exist yet: the receiver
        # creates it instead of refusing the setting.
        response = self.configure_receive_directory(missing, cid="created-receive")
        self.assertEqual(response["state"], "applied")
        self.assertTrue(missing.is_dir())
        target = self.root / "real-receive"
        target.mkdir()
        symlink = self.root / "receive-link"
        try:
            symlink.symlink_to(target, target_is_directory=True)
        except OSError:
            return
        response = self.configure_receive_directory(symlink, cid="symlink-receive")
        self.assertEqual(response["state"], "failed")

    def test_legacy_bindings_migrate_only_when_paths_agree(self):
        first = self.root / "old-one"
        second = self.root / "old-two"
        first.mkdir()
        second.mkdir()
        for backend_name, remote in (("legacy-a", first), ("legacy-b", first)):
            write_json(
                self.connector.root / "projects" / backend_name / "project.json",
                {
                    "name": backend_name,
                    "remote_path": str(remote),
                    "model": "gpt-5.6-sol",
                    "effort": "medium",
                },
            )
            self.connector.saved.append(
                {"backend_project": backend_name, "project_id": "legacy-project", "employee_id": backend_name}
            )
        self.assertEqual(self.control._workspace("legacy-project"), str(first.resolve()))
        registry = read_json(self.connector.root / "project-workspaces.json")
        self.assertEqual(registry["workspaces"]["legacy-project"]["status"], "ready")

        write_json(
            self.connector.root / "projects" / "legacy-c" / "project.json",
            {
                "name": "legacy-c",
                "remote_path": str(second),
                "model": "gpt-5.6-sol",
                "effort": "medium",
            },
        )
        self.connector.saved.extend(
            [
                {"backend_project": "legacy-a", "project_id": "conflict", "employee_id": "x"},
                {"backend_project": "legacy-c", "project_id": "conflict", "employee_id": "y"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "不同目录"):
            self.control._workspace("conflict")
        conflict = read_json(self.connector.root / "project-workspaces.json")["workspaces"]["conflict"]
        self.assertEqual(conflict["status"], "needs_choice")
        self.assertEqual(set(conflict["candidates"]), {str(first.resolve()), str(second.resolve())})


if __name__ == "__main__":
    unittest.main()
