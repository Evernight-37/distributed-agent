import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import warm_worker
import worker
from common import read_json, write_json


class WarmWorkerLifecycleTests(unittest.TestCase):
    def test_registered_worker_claims_active_job_despite_launch_pid_mismatch(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(worker, 'ROOT', Path(folder)), \
             patch.object(warm_worker, 'ROOT', Path(folder)), \
             patch.object(warm_worker, 'process_stamp', return_value='self-stamp'):
            root = Path(folder)
            project = root / 'projects' / 'project'
            job = root / 'jobs' / 'job'
            project.mkdir(parents=True)
            job.mkdir(parents=True)
            write_json(root / 'executor.json', {'warm_idle_seconds': 300})
            write_json(project / 'warm.json', {'pid': os.getpid(), 'stamp': 'self-stamp'})
            # The launcher's active PID may be stale during WMI startup.  The
            # registered project worker remains the single local authority.
            write_json(project / 'active.json', {'job': 'job', 'pid': 999999, 'stamp': 'stale'})
            write_json(job / 'state.json', {'status': 'starting'})
            manager = warm_worker.WarmManager('project')
            called = []

            def run_job(name):
                called.append(name)
                write_json(job / 'state.json', {'status': 'needs_review'})
                write_json(project / 'warm.json', None)

            with patch.object(manager, 'run_job', side_effect=run_job):
                manager.loop()

            self.assertEqual(called, ['job'])
            self.assertIsNone(read_json(project / 'active.json'))
            self.assertEqual(read_json(job / 'state.json')['status'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
