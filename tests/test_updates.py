import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from agent_harness import updates
from agent_harness.common import read_json, write_json

CURRENT = "2.1.0"
WEEK = 7 * 86400


def running_version():
    return updates.application_version()


def next_version():
    """A version strictly newer than the one this source tree declares."""
    parts = [int(piece) for piece in running_version().split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    parts[1] += 1
    return "v" + ".".join(str(piece) for piece in parts)


def fake_fetch(tag=None, url=None):
    """A release payload whose page URL matches the tag unless overridden."""
    tag = tag or next_version()
    url = url or "https://github.com/Evernight-37/distributed-agent/releases/tag/" + tag
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
        newer = next_version()
        with tempfile.TemporaryDirectory() as folder:
            result = updates.status(folder, force=True, fetch=fake_fetch(tag=newer))
            self.assertEqual(result["status"], "update")
            self.assertEqual(result["latest"], newer)
            self.assertIn("releases/tag/" + newer, result["url"])
            stored = read_json(Path(folder) / "updates.json")
            self.assertEqual(stored["latest_version"], newer)
            self.assertIn("last_checked", stored)

    def test_the_same_release_is_current(self):
        with tempfile.TemporaryDirectory() as folder:
            result = updates.status(folder, force=True, fetch=fake_fetch(tag="v" + running_version()))
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
            result = updates.status(folder, force=True, now=10**9 + 60, fetch=fake_fetch(tag=next_version()))
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
        self.assertEqual(updates.asset_name("v2.2.0", "win32", "AMD64"), "Foolship-2.2.0-windows-x64-setup.exe")
        self.assertEqual(updates.asset_name("v2.2.0", "darwin", "arm64"), "Foolship-2.2.0-macos-arm64.zip")
        self.assertEqual(updates.asset_name("v2.2.0", "linux", "x86_64"), "Foolship-2.2.0-linux-x64.tar.gz")
        self.assertEqual(updates.asset_name("v2.2.0", "linux", "aarch64"), "Foolship-2.2.0-linux-arm64.tar.gz")

    def test_a_portable_windows_copy_installs_from_the_zip(self):
        self.assertEqual(updates.asset_name("v2.2.0", "win32", "AMD64", "portable"), "Foolship-2.2.0-windows-x64.zip")
        self.assertEqual(
            updates.asset_name("v2.2.0", "win32", "AMD64", "installed"), "Foolship-2.2.0-windows-x64-setup.exe"
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
            # An install can live in any directory now; the uninstaller is what
            # proves the copy came from the installer.
            (Path(folder) / "unins000.exe").write_text("stub", encoding="utf-8")
            self.assertEqual(updates.install_layout(folder), "installed")

    def test_a_custom_install_directory_still_installs_into_itself(self):
        text = updates.windows_helper(r"C:\setup.exe", r"D:\apps\Foolship\Foolship.exe", 5, r"D:\apps\Foolship")
        self.assertIn('/DIR="D:\\apps\\Foolship"', text)
        self.assertIn("/VERYSILENT", text)

    def test_digest_is_preferred_and_a_checksum_asset_is_kept(self):
        release = release_for("Foolship-2.2.0-windows-x64-setup.exe", b"x", digest="a" * 64, with_sha_asset=True)
        asset = updates.select_asset(release["assets"], "v2.2.0", "win32", "AMD64")
        self.assertEqual(asset["sha256"], "a" * 64)
        self.assertTrue(asset["checksum_url"].endswith(".sha256"))

    def test_a_release_without_a_matching_artifact_is_rejected(self):
        release = release_for("Foolship-2.2.0-linux-x64.tar.gz", b"x", digest="a" * 64)
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
        text = updates.windows_helper(r"C:\data\setup.exe", r"C:\app\Foolship.exe", 4321)
        self.assertIn('tasklist /FI "PID eq 4321"', text)
        self.assertIn('call "C:\\data\\setup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART', text)
        self.assertIn('start "" "C:\\app\\Foolship.exe"', text)
        self.assertIn("goto wait_for_exit", text)
        self.assertTrue(text.endswith("\r\n"))

    def test_the_launcher_gives_up_instead_of_waiting_forever(self):
        # A PID can be reused by an unrelated process, so an unbounded wait would
        # leave a hidden watcher spinning indefinitely.
        for text in (
            updates.windows_helper(r"C:\data\setup.exe", r"C:\app\Foolship.exe", 4321),
            updates.windows_zip_helper(r"C:\d\u.zip", r"C:\app", r"C:\app\Foolship.exe", 4321),
        ):
            self.assertIn("if %tries% GEQ " + str(updates.WAIT_LIMIT), text)
            self.assertIn("goto give_up", text)
            self.assertIn(":give_up", text)
        unix = updates.unix_helper("/tmp/new.zip", "/Applications/Foolship.app", 99)
        self.assertIn("tries=$((tries + 1))", unix)
        self.assertIn("-ge " + str(updates.WAIT_LIMIT), unix)

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
        text = updates.unix_helper("/tmp/new.zip", "/Applications/Foolship.app", 99)
        self.assertIn("while kill -0 99", text)
        self.assertIn('unzip -q "/tmp/new.zip"', text)
        self.assertIn('open "/Applications/Foolship.app"', text)

    def test_the_portable_launcher_unwraps_the_archive_root(self):
        text = updates.windows_zip_helper(r"C:\d\u.zip", r"C:\app", r"C:\app\Foolship.exe", 7)
        # The release zip keeps everything under one directory; extracting it
        # straight into the target would nest a copy instead of replacing it.
        self.assertIn("$items.Count -eq 1", text)
        self.assertIn("Copy-Item", text)
        self.assertNotIn("-DestinationPath 'C:\\app'", text)

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
        target = directory / "Foolship-2.2.0-windows-x64-setup.exe"
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


class StaleFileTests(unittest.TestCase):
    def _layout(self, folder):
        app = Path(folder) / "app"
        (app / "client" / "agent_harness").mkdir(parents=True)
        (app / "client" / "agent_harness" / "kept.py").write_text("keep\n", encoding="utf-8")
        (app / "client" / "agent_harness" / "dropped.py").write_text("old\n", encoding="utf-8")
        (app / "data").mkdir()
        (app / "data" / "desktop.db").write_text("user\n", encoding="utf-8")
        (app / "service-runtime" / "abc").mkdir(parents=True)
        (app / "service-runtime" / "abc" / "worker.py").write_text("runtime\n", encoding="utf-8")
        (app / "notes").mkdir()
        (app / "notes" / "mine.txt").write_text("other\n", encoding="utf-8")
        (app / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        archive = Path(folder) / "release.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Foolship/client/agent_harness/kept.py", "keep\n")
            bundle.writestr("Foolship/VERSION", "2.1.0\n")
        return app, archive

    def test_files_the_release_dropped_are_listed(self):
        with tempfile.TemporaryDirectory() as folder:
            app, archive = self._layout(folder)
            self.assertEqual(updates.plan_stale_files(app, archive), ["client/agent_harness/dropped.py"])

    def test_user_data_and_generated_runtime_are_never_listed(self):
        with tempfile.TemporaryDirectory() as folder:
            app, archive = self._layout(folder)
            planned = updates.plan_stale_files(app, archive)
            self.assertFalse(any(item.startswith("data/") for item in planned))
            self.assertFalse(any(item.startswith("service-runtime/") for item in planned))

    def test_unknown_top_level_directories_are_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            app, archive = self._layout(folder)
            self.assertNotIn("notes/mine.txt", updates.plan_stale_files(app, archive))

    def test_a_flat_archive_is_compared_without_stripping(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "app"
            (app / "client").mkdir(parents=True)
            (app / "client" / "dropped.py").write_text("old\n", encoding="utf-8")
            archive = Path(folder) / "flat.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("client/kept.py", "keep\n")
            self.assertEqual(updates.plan_stale_files(app, archive), ["client/dropped.py"])

    def test_the_launcher_prunes_only_when_given_a_list(self):
        without = updates.windows_zip_helper(r"C:\d\u.zip", r"C:\app", r"C:\app\App.exe", 1)
        self.assertNotIn("prune", without.lower())
        with_list = updates.windows_zip_helper(r"C:\d\u.zip", r"C:\app", r"C:\app\App.exe", 1, r"C:\d\prune.txt")
        self.assertIn("Get-Content -LiteralPath 'C:\\d\\prune.txt'", with_list)
        self.assertIn("Remove-Item", with_list)


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

    def test_a_failed_apply_stays_offerable_until_the_user_decides(self):
        # The page offers "go back" exactly when the applied version is not
        # running, so the record it needs must survive repeated status refreshes.
        with tempfile.TemporaryDirectory() as folder:
            self._marker(folder, "9.9.9")
            for _ in range(3):
                self.assertFalse(updates.mark_running(folder)["confirmed"])
                self.assertIsNotNone(updates.apply_state(folder))
            self.assertTrue(updates.dismiss_apply(folder)["dismissed"])
            self.assertIsNone(updates.apply_state(folder))

    def test_rollback_needs_a_recorded_previous_version(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as raised:
                updates.rollback(folder)
            self.assertIn("没有可以回退", str(raised.exception))


class DownloadGateTests(unittest.TestCase):
    """The download button must work right after the check that found the update."""

    def bridge(self, root):
        class Bridge:
            def __init__(self, root):
                self.root = root

            identity = {"account_id": "account"}
            profile = {"server": "http://127.0.0.1:8765"}

        return Bridge(root)

    def record_update(self, root):
        document = updates.load(root)
        document["latest_version"] = next_version()
        document["last_checked"] = 0
        updates.save(root, document)

    def test_a_recorded_update_is_downloadable_without_a_second_check(self):
        from agent_harness.desktop_api import DesktopSettingsActions

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.record_update(root)
            with (
                mock.patch.object(updates, "start_download", return_value={"status": "downloading"}) as start,
                mock.patch.object(updates, "status", side_effect=AssertionError("must not re-check")),
            ):
                result = DesktopSettingsActions(self.bridge(root)).invoke("update-download", {})
            self.assertEqual(result["status"], "downloading")
            self.assertEqual(start.call_count, 1)

    def test_nothing_recorded_is_reported_as_up_to_date(self):
        from agent_harness.desktop_api import DesktopSettingsActions

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                mock.patch.object(updates, "status", return_value={"status": "current"}),
                mock.patch.object(updates, "start_download") as start,
            ):
                with self.assertRaisesRegex(ValueError, "已经是最新版本"):
                    DesktopSettingsActions(self.bridge(root)).invoke("update-download", {})
            start.assert_not_called()

    def test_offline_is_not_reported_as_up_to_date(self):
        from agent_harness.desktop_api import DesktopSettingsActions

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(updates, "status", return_value={"status": "offline"}):
                with self.assertRaisesRegex(ValueError, "连不上发布页"):
                    DesktopSettingsActions(self.bridge(root)).invoke("update-download", {})


if __name__ == "__main__":
    unittest.main()
