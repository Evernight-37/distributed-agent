import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import warm_worker
import worker
from common import write_json


class WarmWorkerAuthTests(unittest.TestCase):
    def test_managed_turn_fails_before_app_server_when_cli_is_logged_out(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(worker, "ROOT", Path(folder)),
            patch.object(warm_worker, "ROOT", Path(folder)),
        ):
            project = Path(folder) / "projects" / "project"
            job = Path(folder) / "jobs" / "job"
            project.mkdir(parents=True)
            job.mkdir(parents=True)
            write_json(
                job / "request.json",
                {
                    "project": {
                        "name": "project",
                        "remote_path": folder,
                        "model": "model",
                        "effort": "low",
                        "timeout": 120,
                    },
                    "prompt": json.dumps({"stage": "discuss"}),
                    "managed_turn": True,
                    "thread_id": None,
                },
            )
            manager = warm_worker.WarmManager("project")
            with (
                patch.object(warm_worker, "cli_path", return_value="codex.exe"),
                patch.object(
                    warm_worker,
                    "codex_login_status",
                    return_value={"logged_in": False, "login_error": "Codex CLI 尚未登录"},
                ),
                patch.object(manager, "ensure_server") as start,
            ):
                with self.assertRaisesRegex(RuntimeError, "尚未登录"):
                    manager.run_job("job")
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
