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


if __name__ == "__main__":
    unittest.main()
