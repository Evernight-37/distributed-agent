import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import linux_upgrade
from common import read_json


def make_tar(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, value in members:
            if isinstance(value, tarfile.TarInfo):
                archive.addfile(value)
                continue
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                info.size = 0
                info.mode = 0o755
            else:
                info.size = len(value)
                info.mode = 0o755 if name.endswith(("/python", ".sh")) else 0o644
            archive.addfile(info, io.BytesIO(value))


def valid_members(version="1.1.0"):
    return [
        ("AgentHarness-linux/", b""),
        ("AgentHarness-linux/client/", b""),
        ("AgentHarness-linux/client/VERSION", version.encode()),
        ("AgentHarness-linux/client/linux_receiver.py", b"# receiver"),
        ("AgentHarness-linux/client/mcp_adapter.py", b"# adapter"),
        ("AgentHarness-linux/install.py", b"# installer"),
        ("AgentHarness-linux/.venv/", b""),
        ("AgentHarness-linux/.venv/bin/", b""),
        ("AgentHarness-linux/.venv/bin/python", b"#!/bin/sh\n"),
    ]


class LinuxUpgradeTests(unittest.TestCase):
    @staticmethod
    def make_runtime(release):
        release = Path(release)
        (release / ".venv/bin").mkdir(parents=True)
        (release / "client").mkdir()
        python = release / ".venv/bin/python"
        script = release / "client/linux_receiver.py"
        python.write_text("python", encoding="utf-8")
        script.write_text("# receiver", encoding="utf-8")
        return python, script

    def test_sha256_is_required_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members())
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                linux_upgrade.upgrade(Path(directory) / "data", bundle, "0" * 64, lambda *a, **k: [])
            with self.assertRaises(ValueError):
                linux_upgrade.upgrade(Path(directory) / "data", bundle, "not-a-digest", lambda *a, **k: [])

    def test_tar_paths_and_links_are_rejected(self):
        cases = [("../outside", b"bad"), ("/outside", b"bad")]
        link = tarfile.TarInfo("AgentHarness-linux/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        cases.append(("link", link))
        for label, value in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "bundle.tar.gz"
                make_tar(bundle, [(label, value)] if isinstance(value, tarfile.TarInfo) else [(label, value)])
                with tempfile.TemporaryDirectory() as destination:
                    with self.assertRaises(ValueError):
                        linux_upgrade._extract(bundle, destination)

    def test_archive_member_and_size_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, [("root/", b""), ("root/a", b"abc"), ("root/b", b"def")])
            with patch.object(linux_upgrade, "MAX_ARCHIVE_MEMBERS", 2):
                with self.assertRaisesRegex(ValueError, "数量"):
                    linux_upgrade._extract(bundle, Path(directory) / "members")
            with patch.object(linux_upgrade, "MAX_ARCHIVE_FILE_SIZE", 2):
                with self.assertRaisesRegex(ValueError, "单个文件"):
                    linux_upgrade._extract(bundle, Path(directory) / "single")
            with patch.object(linux_upgrade, "MAX_ARCHIVE_TOTAL_SIZE", 5):
                with self.assertRaisesRegex(ValueError, "总体积"):
                    linux_upgrade._extract(bundle, Path(directory) / "total")

    def test_active_receipt_blocks_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            receipt = root / "receivers/device-one/pull-receipts/job.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({"phase": "running"}), encoding="utf-8")
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members())
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "未结束任务"):
                linux_upgrade.upgrade(root, bundle, digest, lambda *a, **k: [])

    def test_valid_bundle_installs_side_by_side_and_records_non_secret_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members())
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            calls = []

            class FakeManager:
                def __init__(self, *args):
                    self.args = args
                    self.installs = 0
                    calls.append(self)

                def install(self):
                    self.installs += 1

            with (
                patch.object(linux_upgrade, "ServiceManager", FakeManager),
                patch.object(linux_upgrade.subprocess, "run") as run,
                patch.object(linux_upgrade, "refresh_launchers", create=True) as refresh,
            ):
                result = linux_upgrade.upgrade(root, bundle, digest, refresh)
            self.assertEqual(result["version"], "1.1.0")
            self.assertTrue(result["rollback_available"])
            run.assert_called_once()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].installs, 1)
            state = read_json(root.parent / "install-state.json")
            encoded = json.dumps(state)
            self.assertIn("current", state)
            self.assertIn("previous", state)
            self.assertNotIn("private_key", encoded)
            self.assertNotIn("device_token", encoded)
            self.assertEqual(refresh.call_count, 1)
            self.assertFalse(result["reused"])

    def test_recorded_current_runtime_is_previous_for_consecutive_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            current = Path(directory) / "actual-current"
            current_python, current_script = self.make_runtime(current)
            (root.parent / "install-state.json").write_text(
                json.dumps(
                    {
                        "current": {
                            "release": str(current),
                            "python": str(current_python),
                            "script": str(current_script),
                        }
                    }
                ),
                encoding="utf-8",
            )
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members("1.2.0"))
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            managers = []

            class FakeManager:
                def __init__(self, *args):
                    self.args = args
                    managers.append(self)

                def install(self):
                    pass

            with (
                patch.object(linux_upgrade, "ServiceManager", FakeManager),
                patch.object(linux_upgrade.subprocess, "run"),
            ):
                linux_upgrade.upgrade(root, bundle, digest, lambda *a, **k: [])
            self.assertEqual(managers[1].args[1:], (current_python, current_script))
            state = read_json(root.parent / "install-state.json")
            self.assertEqual(state["previous"]["release"], str(current.resolve()))

    def test_retry_reuses_only_matching_failed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members("1.2.0"))
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            destination = root.parent / "releases/1.2.0"
            self.make_runtime(destination)
            (destination / ".agent-harness-bundle-sha256").write_text(digest + "\n", encoding="ascii")

            class FakeManager:
                def __init__(self, *args):
                    pass

                def install(self):
                    pass

            with (
                patch.object(linux_upgrade, "ServiceManager", FakeManager),
                patch.object(linux_upgrade.subprocess, "run") as install,
            ):
                result = linux_upgrade.upgrade(root, bundle, digest, lambda *a, **k: [], retry=True)
            self.assertTrue(result["reused"])
            install.assert_not_called()
            (destination / ".agent-harness-bundle-sha256").write_text("0" * 64 + "\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "无法安全匹配"):
                linux_upgrade.upgrade(root, bundle, digest, lambda *a, **k: [], retry=True)

    def test_install_and_cleanup_failures_preserve_original_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members("1.2.0"))
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            install_error = linux_upgrade.subprocess.CalledProcessError(1, ["install"])
            destination = root.parent / "releases/1.2.0"
            original_rmtree = linux_upgrade.shutil.rmtree

            def cleanup(path, *args, **kwargs):
                if Path(path) == destination:
                    raise OSError("cleanup")
                return original_rmtree(path, *args, **kwargs)

            with (
                patch.object(linux_upgrade.subprocess, "run", side_effect=install_error),
                patch.object(linux_upgrade.shutil, "rmtree", side_effect=cleanup),
            ):
                with self.assertRaisesRegex(RuntimeError, "清理也未完成") as caught:
                    linux_upgrade.upgrade(root, bundle, digest, lambda *a, **k: [])
            self.assertIs(caught.exception.__cause__, install_error)

    def test_failed_switch_reinstalls_old_service_and_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            bundle = Path(directory) / "bundle.tar.gz"
            make_tar(bundle, valid_members())
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            calls = []

            class FakeManager:
                def __init__(self, *args):
                    self.should_fail = len(calls) == 0
                    self.installs = 0
                    calls.append(self)

                def install(self):
                    self.installs += 1
                    if self.should_fail:
                        raise RuntimeError("new service failed")

            with (
                patch.object(linux_upgrade, "ServiceManager", FakeManager),
                patch.object(linux_upgrade.subprocess, "run"),
                patch.object(linux_upgrade, "refresh_launchers", create=True) as refresh,
            ):
                with self.assertRaisesRegex(RuntimeError, "new service failed"):
                    linux_upgrade.upgrade(root, bundle, digest, refresh)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1].installs, 1)
            self.assertEqual(refresh.call_count, 2)

    def test_rollback_requires_existing_previous_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            state = root.parent / "install-state.json"
            state.write_text(
                json.dumps(
                    {
                        "current": {"release": "new"},
                        "previous": {"release": "old", "python": "/missing/python", "script": "/missing/script"},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不存在"):
                linux_upgrade.rollback(root, lambda *a, **k: [])

    def test_failed_rollback_restores_current_service_and_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir(parents=True)
            previous = Path(directory) / "previous"
            current = Path(directory) / "current"
            for release in (previous, current):
                (release / ".venv/bin").mkdir(parents=True)
                (release / "client").mkdir()
                (release / ".venv/bin/python").write_text("python", encoding="utf-8")
                (release / "client/linux_receiver.py").write_text("# receiver", encoding="utf-8")
            (root.parent / "install-state.json").write_text(
                json.dumps(
                    {
                        "current": {
                            "release": str(current),
                            "python": str(current / ".venv/bin/python"),
                            "script": str(current / "client/linux_receiver.py"),
                        },
                        "previous": {
                            "release": str(previous),
                            "python": str(previous / ".venv/bin/python"),
                            "script": str(previous / "client/linux_receiver.py"),
                        },
                    }
                ),
                encoding="utf-8",
            )
            managers = []

            class FakeManager:
                def __init__(self, *args):
                    self.fail = not managers
                    self.installs = 0
                    managers.append(self)

                def install(self):
                    self.installs += 1
                    if self.fail:
                        raise RuntimeError("old service failed")

            with (
                patch.object(linux_upgrade, "ServiceManager", FakeManager),
                patch.object(linux_upgrade, "refresh_launchers", create=True) as refresh,
            ):
                with self.assertRaisesRegex(RuntimeError, "old service failed"):
                    linux_upgrade.rollback(root, refresh)
            self.assertEqual(len(managers), 2)
            self.assertEqual(managers[1].installs, 1)
            self.assertEqual(refresh.call_count, 2)


if __name__ == "__main__":
    unittest.main()
