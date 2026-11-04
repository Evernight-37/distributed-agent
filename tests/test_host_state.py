import tempfile
import unittest
from pathlib import Path

from agent_harness.app_bridge import pick_host_state


class HostStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.current = self.root / "app" / "data" / "team-service"
        self.saved = self.root / "local" / "data" / "team-service"

    def pick(self, database=(), accounts=(), legacy=()):
        return pick_host_state(
            self.current,
            self.saved,
            has_database=lambda state: Path(state) in database,
            knows_account=lambda state: Path(state) in accounts,
            is_legacy=lambda state: Path(state) in legacy,
        )

    def test_without_a_saved_path_the_current_root_wins(self):
        self.assertEqual(pick_host_state(self.current, None, lambda s: False, lambda s: False), self.current)

    def test_a_saved_path_that_holds_this_account_is_kept(self):
        self.assertEqual(self.pick(database=(self.saved,), accounts=(self.saved,)), self.saved)

    def test_the_current_database_wins_over_a_stale_saved_path(self):
        # This is the upgrade case: data moved, the profile still points at the
        # old directory, and starting the service there would create an empty
        # database with no devices for the account.
        self.assertEqual(self.pick(database=(self.current, self.saved), accounts=(self.current,)), self.current)

    def test_a_saved_path_inside_the_migrated_directory_is_treated_as_stale(self):
        self.assertEqual(self.pick(database=(self.current, self.saved), legacy=(self.saved,)), self.current)

    def test_real_data_in_a_migrated_directory_still_wins(self):
        self.assertEqual(self.pick(database=(self.current,), accounts=(self.saved,), legacy=(self.saved,)), self.saved)

    def test_a_location_the_user_chose_is_honoured_even_without_data(self):
        self.assertEqual(self.pick(), self.saved)

    def test_an_empty_saved_path_loses_to_a_populated_current_one(self):
        self.assertEqual(self.pick(database=(self.current,)), self.current)

    def test_an_empty_current_root_loses_to_a_populated_saved_one(self):
        self.assertEqual(self.pick(database=(self.saved,)), self.saved)

    def test_a_saved_path_equal_to_the_current_root_is_not_reconsidered(self):
        self.assertEqual(pick_host_state(self.current, self.current, lambda s: False, lambda s: True), self.current)


if __name__ == "__main__":
    unittest.main()
