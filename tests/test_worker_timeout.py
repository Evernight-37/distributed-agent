import unittest

import worker


class WorkerTimeoutTests(unittest.TestCase):
    def test_managed_and_ordinary_turns_honor_configured_timeout(self):
        for managed in (False, True):
            config = {"project": {"timeout": 600}, "managed_turn": managed}
            self.assertEqual(worker.execution_timeout(config), 600)

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            worker.execution_timeout({"project": {"timeout": 0}})


if __name__ == "__main__":
    unittest.main()
