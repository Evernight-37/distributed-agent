"""The single-instance guard: one window per data directory."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_harness.instance import Instance


class InstanceTests(unittest.TestCase):
    def test_a_second_process_raises_the_first_and_steps_aside(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shown = []
            first = Instance(root)
            self.assertTrue(first.claim(on_show=lambda: shown.append(True)))

            second = Instance(root)
            self.assertFalse(second.claim(on_show=lambda: None))
            self.assertEqual(shown, [True])

            first.release()

    def test_release_removes_only_its_own_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = Instance(root)
            first.claim(on_show=lambda: None)
            first.release()
            self.assertFalse((root / "instance.json").exists())

    def test_a_stale_record_does_not_block_a_new_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "instance.json").write_text(json.dumps({"port": 9, "token": "gone"}), "utf-8")
            third = Instance(root)
            self.assertTrue(third.claim(on_show=lambda: None))
            third.release()

    def test_a_record_without_the_right_shape_is_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "instance.json").write_text("{}", "utf-8")
            fourth = Instance(root)
            self.assertTrue(fourth.claim(on_show=lambda: None))
            fourth.release()


if __name__ == "__main__":
    unittest.main()
