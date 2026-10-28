import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_harness import owner_backend as backend
from agent_harness import owner_worker as worker
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
                    "test-model",
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

    def test_receive_directory_rejects_unsafe_or_missing_paths(self):
        missing = self.root / "missing"
        regular_file = self.root / "file.txt"
        regular_file.write_text("not a directory")
        for index, candidate in enumerate(("relative/path", Path(self.root.anchor), missing, regular_file)):
            with self.subTest(candidate=candidate):
                response = self.configure_receive_directory(
                    candidate,
                    cid=f"invalid-receive-{index}",
                )
                self.assertEqual(response["state"], "failed")
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
