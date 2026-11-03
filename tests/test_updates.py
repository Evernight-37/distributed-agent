import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_harness import updates
from agent_harness.common import read_json, write_json

CURRENT = "2.1.0"
WEEK = 7 * 86400


def fake_fetch(tag="v2.2.0", url="https://github.com/Evernight-37/distributed-agent/releases/tag/v2.2.0"):
    return lambda timeout=8: {"tag": tag, "url": url}


def failing_fetch(timeout=8):
    raise OSError("no network")


class SettingsTests(unittest.TestCase):
    def test_default_schedule_is_weekly(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(updates.load(folder)["schedule"], "weekly")

    def test_schedule_round_trip_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            for schedule in ("daily", "weekly", "monthly", "never"):
                updates.set_schedule(folder, schedule)
                self.assertEqual(updates.load(folder)["schedule"], schedule)
            with self.assertRaises(ValueError):
                updates.set_schedule(folder, "hourly")

    def test_junk_settings_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            write_json(Path(folder) / "updates.json", {"schedule": "sometimes", "last_checked": "yesterday"})
            document = updates.load(folder)
            self.assertEqual(document["schedule"], "weekly")
            self.assertNotIn("last_checked", document)


class ScheduleTests(unittest.TestCase):
    def test_never_is_never_due(self):
        self.assertFalse(updates.due({"schedule": "never", "last_checked": 0}, now=10**9))

    def test_due_after_the_interval_only(self):
        document = {"schedule": "weekly", "last_checked": 1000.0, "last_attempt": 1000.0}
        self.assertFalse(updates.due(document, now=1000 + WEEK - 1))
        self.assertTrue(updates.due(document, now=1000 + WEEK + 1))

    def test_an_offline_attempt_waits_before_retrying(self):
        now = 10**6
        document = {"schedule": "daily", "last_checked": 0, "last_attempt": now - 60}
        self.assertFalse(updates.due(document, now=now))
        self.assertTrue(updates.due(document, now=now - 60 + updates.OFFLINE_RETRY_SECONDS + 1))


class VersionTests(unittest.TestCase):
    def test_parses_prefixed_and_pre_release_tags(self):
        self.assertEqual(updates.version_tuple("v2.1.0"), (2, 1, 0))
        self.assertEqual(updates.version_tuple("2.1.0-rc.1"), (2, 1, 0))
        self.assertIsNone(updates.version_tuple("unknown"))

    def test_newer_comparisons(self):
        self.assertTrue(updates.is_newer("v2.2.0", "2.1.0"))
        self.assertFalse(updates.is_newer("v2.1.0", "2.1.0"))
        self.assertFalse(updates.is_newer("v2.0.9", "2.1.0"))
        self.assertFalse(updates.is_newer("nonsense", "2.1.0"))
        self.assertFalse(updates.is_newer("v2.2.0", "unknown"))


class StatusTests(unittest.TestCase):
    def test_disabled_never_calls_the_network(self):
        with tempfile.TemporaryDirectory() as folder:
            updates.set_schedule(folder, "never")
            called = []
            result = updates.status(
                folder, force=True, fetch=lambda **k: called.append(1) or {"tag": "v9.9.9", "url": ""}
            )
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(called, [])

    def test_idle_skips_a_check_that_is_not_owed(self):
        with tempfile.TemporaryDirectory() as folder:
            document = updates.load(folder)
            document["last_checked"] = 10**9
            updates.save(folder, document)
            result = updates.status(folder, now=10**9 + 60, fetch=failing_fetch)
            self.assertEqual(result["status"], "idle")

    def test_a_newer_release_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            result = updates.status(folder, force=True, fetch=fake_fetch())
            self.assertEqual(result["status"], "update")
            self.assertEqual(result["latest"], "v2.2.0")
            self.assertIn("releases/tag/v2.2.0", result["url"])
            stored = read_json(Path(folder) / "updates.json")
            self.assertEqual(stored["latest_version"], "v2.2.0")
            self.assertIn("last_checked", stored)

    def test_the_same_release_is_current(self):
        with tempfile.TemporaryDirectory() as folder:
            result = updates.status(folder, force=True, fetch=fake_fetch(tag="v2.1.0"))
            self.assertEqual(result["status"], "current")

    def test_offline_records_only_the_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            result = updates.status(folder, force=True, fetch=failing_fetch)
            self.assertEqual(result["status"], "offline")
            stored = read_json(Path(folder) / "updates.json")
            self.assertNotIn("last_checked", stored)
            self.assertIn("last_attempt", stored)

    def test_a_bad_release_page_falls_back_to_the_releases_page(self):
        with tempfile.TemporaryDirectory() as folder:
            result = updates.status(folder, force=True, fetch=fake_fetch(url="http://evil.example/x"))
            self.assertEqual(result["url"], updates.RELEASES_PAGE)

    def test_force_ignores_the_interval_but_not_never(self):
        with tempfile.TemporaryDirectory() as folder:
            updates.set_schedule(folder, "daily")
            document = updates.load(folder)
            document["last_checked"] = 10**9
            updates.save(folder, document)
            result = updates.status(folder, force=True, now=10**9 + 60, fetch=fake_fetch())
            self.assertEqual(result["status"], "update")


class ReleaseUrlTests(unittest.TestCase):
    def test_returns_the_stored_page(self):
        with tempfile.TemporaryDirectory() as folder:
            updates.status(folder, force=True, fetch=fake_fetch())
            self.assertIn("github.com", updates.release_url(folder))

    def test_rejects_a_stored_foreign_host(self):
        with tempfile.TemporaryDirectory() as folder:
            write_json(Path(folder) / "updates.json", {"schedule": "weekly", "url": "https://example.com/x"})
            with self.assertRaises(ValueError):
                updates.release_url(folder)


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self._position = 0

    def read(self, size):
        chunk = self._data[self._position : self._position + size]
        self._position += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(data):
    def opener(request, timeout=None):
        return FakeResponse(data)

    return opener


def release_for(name, data, digest=None, with_sha_asset=False):
    asset = {
        "name": name,
        "browser_download_url": "https://github.com/Evernight-37/distributed-agent/releases/download/v2.2.0/" + name,
        "size": len(data),
    }
    if digest:
        asset["digest"] = "sha256:" + digest
    assets = [asset]
    if with_sha_asset:
        assets.append(
            {
                "name": name + ".sha256",
                "browser_download_url": "https://github.com/Evernight-37/distributed-agent/releases/download/v2.2.0/"
                + name
                + ".sha256",
                "size": 80,
            }
        )
    return {"tag": "v2.2.0", "assets": assets}


class AssetTests(unittest.TestCase):
    def test_asset_names_follow_the_published_layout(self):
        self.assertEqual(updates.asset_name("v2.2.0", "win32", "AMD64"), "AgentHarness-2.2.0-windows-x64-setup.exe")
        self.assertEqual(updates.asset_name("v2.2.0", "darwin", "arm64"), "AgentHarness-2.2.0-macos-arm64.zip")
        self.assertEqual(updates.asset_name("v2.2.0", "linux", "x86_64"), "AgentHarness-2.2.0-linux-x64.tar.gz")
        self.assertEqual(updates.asset_name("v2.2.0", "linux", "aarch64"), "AgentHarness-2.2.0-linux-arm64.tar.gz")

    def test_a_portable_windows_copy_installs_from_the_zip(self):
        self.assertEqual(
            updates.asset_name("v2.2.0", "win32", "AMD64", "portable"), "AgentHarness-2.2.0-windows-x64.zip"
        )
        self.assertEqual(
            updates.asset_name("v2.2.0", "win32", "AMD64", "installed"), "AgentHarness-2.2.0-windows-x64-setup.exe"
        )

    def test_layout_is_read_from_where_the_application_lives(self):
        with tempfile.TemporaryDirectory() as folder:
            if sys.platform == "darwin":
                # macOS copies are always replaced as a whole bundle.
                self.assertEqual(updates.install_layout(folder), "bundle")
                return
            self.assertEqual(updates.install_layout(folder), "portable")
            if os.name != "nt":
                return
            import unittest.mock

            with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(folder) / "Local")}):
                expected = Path(folder) / "Local" / "AgentHarness" / "app"
                expected.mkdir(parents=True)
                self.assertEqual(updates.install_layout(expected), "installed")
                self.assertEqual(updates.install_layout(folder), "portable")

    def test_digest_is_preferred_and_a_checksum_asset_is_kept(self):
        release = release_for("AgentHarness-2.2.0-windows-x64-setup.exe", b"x", digest="a" * 64, with_sha_asset=True)
        asset = updates.select_asset(release["assets"], "v2.2.0", "win32", "AMD64")
        self.assertEqual(asset["sha256"], "a" * 64)
        self.assertTrue(asset["checksum_url"].endswith(".sha256"))

    def test_a_release_without_a_matching_artifact_is_rejected(self):
        release = release_for("AgentHarness-2.2.0-linux-x64.tar.gz", b"x", digest="a" * 64)
        with self.assertRaises(ValueError) as raised:
            updates.select_asset(release["assets"], "v2.2.0", "win32", "AMD64")
        self.assertIn("没有适用于本机", str(raised.exception))


class DownloadTests(unittest.TestCase):
    def _download(self, folder, data, digest, **kwargs):
        name = updates.asset_name("v2.2.0", "win32", "AMD64")
        release = release_for(name, data, digest=digest, **kwargs)
        return updates.download(
            folder,
            tag="v2.2.0",
            fetch=lambda tag: release,
            opener=opener_for(data),
            # The mechanics under test are platform independent; per-platform
            # asset selection is covered by AssetTests.
            system="win32",
            machine="AMD64",
        )

    def test_a_verified_package_is_staged(self):
        import hashlib

        data = b"installer-bytes" * 100
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            record = self._download(folder, data, digest)
            self.assertEqual(record["version"], "2.2.0")
            self.assertEqual(record["sha256"], digest)
            self.assertEqual(record["size"], len(data))
            self.assertEqual(updates.staged(folder)["file"], record["file"])
            self.assertTrue((updates.pending_dir(folder) / record["file"]).is_file())
            self.assertEqual(updates.download_state(folder)["status"], "ready")

    def test_a_checksum_mismatch_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as raised:
                self._download(folder, b"tampered", "0" * 64)
            self.assertIn("校验失败", str(raised.exception))
            self.assertIsNone(updates.staged(folder))
            self.assertEqual(list(updates.pending_dir(folder).glob("*")), [])

    def test_a_release_without_a_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as raised:
                self._download(folder, b"data", None)
            self.assertIn("没有提供这个安装包的校验值", str(raised.exception))
            self.assertIsNone(updates.staged(folder))

    def test_a_checksum_asset_is_used_when_the_digest_is_missing(self):
        import hashlib

        data = b"linux-tarball"
        digest = hashlib.sha256(data).hexdigest().encode()
        name = updates.asset_name("v2.2.0", "linux", "x86_64")
        release = release_for(name, data, digest=None, with_sha_asset=True)

        def opener(request, timeout=None):
            url = getattr(request, "full_url", "")
            if url.endswith(".sha256"):
                return FakeResponse(digest + b"  " + name.encode())
            return FakeResponse(data)

        with tempfile.TemporaryDirectory() as folder:
            record = updates.download(
                folder, tag="v2.2.0", fetch=lambda tag: release, opener=opener, system="linux", machine="x86_64"
            )
            self.assertEqual(record["sha256"], digest.decode())

    def test_discard_removes_the_staged_package(self):
        import hashlib

        data = b"installer-bytes"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            record = self._download(folder, data, digest)
            updates.discard(folder)
            self.assertFalse((updates.pending_dir(folder) / record["file"]).exists())
            self.assertIsNone(updates.staged(folder))

    def test_status_reports_the_staged_package(self):
        import hashlib

        data = b"installer-bytes"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            self._download(folder, data, digest)
            state = updates.status(folder, force=True, fetch=lambda **k: {"tag": "v2.2.0", "url": ""})
            self.assertEqual(state["staged"]["version"], "2.2.0")

    def test_a_staged_package_is_reverified_before_use(self):
        import hashlib

        data = b"installer-bytes"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            record = self._download(folder, data, digest)
            self.assertIsNone(updates.verify_staged(folder))
            (updates.pending_dir(folder) / record["file"]).write_bytes(b"swapped")
            self.assertIn("不一致", updates.verify_staged(folder))


class HelperTests(unittest.TestCase):
    def test_the_windows_launcher_waits_installs_and_relaunches(self):
        text = updates.windows_helper(r"C:\data\setup.exe", r"C:\app\AgentHarness.exe", 4321)
        self.assertIn('tasklist /FI "PID eq 4321"', text)
        self.assertIn('call "C:\\data\\setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART', text)
        self.assertIn('start "" "C:\\app\\AgentHarness.exe"', text)
        self.assertIn("goto wait_for_exit", text)
        self.assertTrue(text.endswith("\r\n"))

    def test_previous_launchers_are_pruned(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "updates" / "pending"
            directory.mkdir(parents=True)
            old = directory / "apply-2.1.0.cmd"
            old.write_text("old", encoding="utf-8")
            keep = updates.write_helper(folder, "2.2.0", "setup.exe", "app", 1)
            self.assertFalse(old.exists())
            self.assertTrue(keep.is_file())

    def test_the_unix_launcher_waits_unpacks_and_reopens(self):
        text = updates.unix_helper("/tmp/new.zip", "/Applications/AgentHarness.app", 99)
        self.assertIn("while kill -0 99", text)
        self.assertIn('unzip -q "/tmp/new.zip"', text)
        self.assertIn('open "/Applications/AgentHarness.app"', text)

    def test_the_launcher_is_written_outside_the_install_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = updates.write_helper(folder, "v2.2.0", "setup.exe", "app", 1234)
            self.assertTrue(path.is_file())
            self.assertIn(str(Path(folder)), str(path))


class FakeChild:
    pid = 4242


class ApplyTests(unittest.TestCase):
    def _stage(self, folder, data=b"installer"):
        import hashlib

        directory = updates.pending_dir(folder)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "AgentHarness-2.2.0-windows-x64-setup.exe"
        target.write_bytes(data)
        write_json(
            directory / "meta.json",
            {
                "version": "2.2.0",
                "file": target.name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
        return target

    def test_apply_refuses_without_a_staged_package(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as raised:
                updates.apply(folder, target="app", spawn=lambda: FakeChild())
            self.assertIn("没有已下载", str(raised.exception))

    def test_apply_refuses_a_tampered_package(self):
        with tempfile.TemporaryDirectory() as folder:
            target = self._stage(folder)
            target.write_bytes(b"swapped")
            with self.assertRaises(ValueError) as raised:
                updates.apply(folder, target="app", spawn=lambda: FakeChild())
            self.assertIn("不一致", str(raised.exception))

    def test_apply_refuses_while_work_is_running_unless_forced(self):
        with tempfile.TemporaryDirectory() as folder:
            self._stage(folder)
            with self.assertRaises(ValueError) as raised:
                updates.apply(folder, target="app", busy=lambda: ["文件传输"], spawn=lambda: FakeChild())
            self.assertIn("会中断", str(raised.exception))
            started = updates.apply(
                folder, target="app", busy=lambda: ["文件传输"], force=True, spawn=lambda: FakeChild()
            )
            self.assertTrue(started["started"])

    def test_apply_writes_a_launcher_and_a_marker_then_spawns_once(self):
        with tempfile.TemporaryDirectory() as folder:
            self._stage(folder)
            calls = []

            def spawn():
                calls.append(1)
                return FakeChild()

            started = updates.apply(folder, target="app", pid=99, spawn=spawn)
            self.assertEqual(started["version"], "2.2.0")
            self.assertEqual(len(calls), 1)
            marker = updates.apply_state(folder)
            self.assertEqual(marker["to_version"], "2.2.0")
            helper = Path(marker["helper"])
            self.assertTrue(helper.is_file())
            text = helper.read_text(encoding="utf-8")
            if os.name == "nt":
                self.assertIn('tasklist /FI "PID eq 99"', text)
            else:
                self.assertIn("while kill -0 99", text)

    def test_apply_records_the_layout_and_writes_the_matching_launcher(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as app:
            self._stage(folder)
            updates.apply(folder, target=app, spawn=lambda: FakeChild())
            marker = updates.apply_state(folder)
            expected_layout = "bundle" if sys.platform == "darwin" else "portable"
            self.assertEqual(marker["layout"], expected_layout)
            text = Path(marker["helper"]).read_text(encoding="utf-8")
            if os.name == "nt":
                self.assertIn("Expand-Archive", text)
                self.assertNotIn("/VERYSILENT", text)
            else:
                self.assertIn("unzip", text)

    def test_a_failed_spawn_leaves_no_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            self._stage(folder)

            def spawn():
                raise OSError("boom")

            with self.assertRaises(OSError):
                updates.apply(folder, target="app", spawn=spawn)
            self.assertIsNone(updates.apply_state(folder))


class MarkerTests(unittest.TestCase):
    def _marker(self, folder, to_version):
        write_json(updates.apply_marker_path(folder), {"from_version": "2.0.0", "to_version": to_version})

    def test_running_the_applied_version_confirms_and_clears_the_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            self._marker(folder, updates.application_version())
            result = updates.mark_running(folder)
            self.assertTrue(result["confirmed"])
            self.assertIsNone(updates.apply_state(folder))

    def test_running_another_version_reports_the_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            self._marker(folder, "9.9.9")
            result = updates.mark_running(folder)
            self.assertFalse(result["confirmed"])
            self.assertEqual(result["expected"], "9.9.9")
            self.assertIsNone(updates.apply_state(folder))

    def test_rollback_needs_a_recorded_previous_version(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as raised:
                updates.rollback(folder)
            self.assertIn("没有可以回退", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
