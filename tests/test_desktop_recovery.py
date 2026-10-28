import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_harness.account_identity import generate_identity
from agent_harness.account_recovery import create_recovery_package
from agent_harness.desktop_actions import DesktopIdentityActions


class FakeBridge:
    def __init__(self, chosen, identity=None):
        self.identity = identity
        self.profile = None
        self.activated_services = set()
        self.saved_password = None
        self.enrolled = False
        self.window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: str(chosen))

    def _server(self, value):
        return value

    def _post(self, server, request):
        if request["action"] == "challenge":
            return {"nonce": "challenge"}
        self.asserted_envelope = request["envelope"]
        return {"node": "device-recovered", "username": "Recovered user"}

    def _save_profile(self, password):
        self.saved_password = password

    def _enroll(self, server=None):
        self.enrolled = True


class DesktopRecoveryTests(unittest.TestCase):
    def webview(self):
        return mock.patch.dict(
            "sys.modules",
            {"webview": SimpleNamespace(FileDialog=SimpleNamespace(SAVE="save", OPEN="open"))},
        )

    def test_export_writes_only_encrypted_package(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "recovery.json"
            root = generate_identity()
            bridge = FakeBridge(target, root)
            with self.webview():
                result = DesktopIdentityActions(bridge).invoke("recovery-export", {"recovery_password": "offline"})
            self.assertTrue(result["saved"])
            content = target.read_text(encoding="utf-8")
            self.assertNotIn(root["private_key"], content)
            self.assertEqual(json.loads(content)["version"], 1)

    def test_import_activates_and_saves_a_new_child_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recovery.json"
            root = generate_identity()
            source.write_text(
                json.dumps(create_recovery_package(root, "offline")),
                encoding="utf-8",
            )
            bridge = FakeBridge(source)
            with self.webview():
                result = DesktopIdentityActions(bridge).invoke(
                    "recovery-import",
                    {
                        "server": "https://team.example",
                        "recovery_password": "offline",
                        "password": "local",
                    },
                )
            self.assertTrue(result["imported"])
            self.assertEqual(bridge.identity["account_id"], root["account_id"])
            self.assertNotEqual(bridge.identity["public_key"], root["public_key"])
            self.assertEqual(bridge.profile["username"], "Recovered user")
            self.assertEqual(bridge.saved_password, "local")
            self.assertTrue(bridge.enrolled)


if __name__ == "__main__":
    unittest.main()
