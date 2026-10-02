import os
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import warm_worker
import worker
from common import read_json, write_json


class WarmWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.original_root = worker.ROOT

    def tearDown(self):
        worker.set_root(self.original_root)

    def runtime_root(self, folder):
        root = Path(folder)
        for name in ('common.py', 'worker.py', 'warm_worker.py', 'appserver_client.py', 'jobrunner.py'):
            shutil.copyfile(Path(__file__).parents[1] / name, root / name)
        (root / 'projects' / 'project').mkdir(parents=True)
        write_json(root / 'executor.json', {'warm_idle_seconds': 300})
        return root

    def launch_record(self, root, token, generation):
        write_json(root / 'projects' / 'project' / 'launch.json', {
            'token': token, 'generation': generation, 'project': 'project', 'root': str(root.resolve()),
            'parent_pid': os.getpid(), 'parent_stamp': worker.process_stamp(os.getpid())})

    def stop_child(self, runtime, generation):
        child = runtime.child(generation)
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        deadline = time.time() + 5
        while runtime.child(generation) is not None and time.time() < deadline:
            time.sleep(.02)

    def test_explicit_root_ready_ack_and_child_survives_launcher_return(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder); token = uuid.uuid4().hex; generation = uuid.uuid4().hex
            self.launch_record(root, token, generation)
            runtime = worker.ManagedRuntime(root)
            child = runtime.launch('project', token, generation)
            try:
                ready = worker._await_ready(root / 'projects' / 'project', child, token, generation, 8)
                self.assertEqual(Path(ready['root']), root.resolve())
                self.assertIsNone(child.poll())
                write_json(root / 'projects' / 'project' / 'warm.json', dict(ready, token=token, generation=generation))
                write_json(root / 'projects' / 'project' / 'launch.json', None)
                time.sleep(.15)
                self.assertIsNone(child.poll(), 'ACKed child must outlive the launching call')
            finally:
                self.stop_child(runtime, generation)

    def test_real_rpc_returns_only_after_ack_and_leaves_child_alive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder)
            project = {'name': 'project', 'remote_path': str(root), 'model': 'test-model',
                       'effort': 'low', 'timeout': 30, 'local_control': True}
            write_json(root / 'projects' / 'project' / 'project.json', project)
            write_json(root / 'projects' / 'project' / 'memory.json', {'revision': 0, 'entries': []})
            write_json(root / 'projects' / 'project' / 'channels' / 'group.json', {'thread_id': None})
            write_json(root / 'executor.json', {'warm_idle_seconds': 300,
                                                'cli_path': str(root / 'missing-codex.exe')})
            runtime = worker.ManagedRuntime(root)
            result = runtime.rpc({'action': 'start', 'project': 'project', 'job': 'rpc-job',
                                  'prompt': json.dumps({'stage': 'discuss'}), 'session_channel': 'group'})
            warm = read_json(root / 'projects' / 'project' / 'warm.json')
            try:
                self.assertEqual(result['supervisor_pid'], warm['pid'])
                self.assertEqual(warm['generation'], read_json(root / 'projects' / 'project' / 'active.json')['generation'])
                self.assertIsNone(runtime.child(warm['generation']).poll())
            finally:
                self.stop_child(runtime, warm['generation'])

    def test_external_script_imports_worker_from_receiver_root(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as external:
            root = self.runtime_root(state); token = uuid.uuid4().hex; generation = uuid.uuid4().hex
            self.launch_record(root, token, generation)
            script = Path(external) / 'warm_worker.py'
            shutil.copyfile(Path(__file__).parents[1] / 'warm_worker.py', script)
            output = (root / 'projects' / 'project' / 'external.log').open('wb')
            child = subprocess.Popen([sys.executable, '-X', 'utf8', str(script), '--root', str(root),
                '--project', 'project', '--token', token, '--generation', generation], stdout=output, stderr=output)
            try:
                ready = worker._await_ready(root / 'projects' / 'project', child, token, generation, 8)
                self.assertEqual(Path(ready['root']), root.resolve())
                self.assertIsNone(child.poll())
                write_json(root / 'projects' / 'project' / 'warm.json', dict(ready, token=token, generation=generation))
                write_json(root / 'projects' / 'project' / 'launch.json', None)
            finally:
                if child.poll() is None: child.terminate()
                child.wait(timeout=10); output.close()

    def test_ready_before_exit_is_required_and_startup_reason_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder); token = uuid.uuid4().hex; generation = uuid.uuid4().hex
            self.launch_record(root, token, generation)
            # Deliberately make the ticket inconsistent with the CLI arguments.
            launch = read_json(root / 'projects' / 'project' / 'launch.json'); launch['project'] = 'other'
            write_json(root / 'projects' / 'project' / 'launch.json', launch)
            process = subprocess.Popen([sys.executable, '-X', 'utf8', str(root / 'warm_worker.py'), '--root', str(root),
                '--project', 'project', '--token', token, '--generation', generation],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.assertRaisesRegex(RuntimeError, 'Launch record rejected|before READY'):
                worker._await_ready(root / 'projects' / 'project', process, token, generation, 8)
            process.wait(timeout=10)
            error = read_json(root / 'projects' / 'project' / 'startup-error.json')
            self.assertEqual(error['generation'], generation)
            self.assertLessEqual(len(error['error']), 2000)

    def test_guardian_marks_same_generation_failed_and_clears_active(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); project = root / 'projects' / 'project'; job = root / 'jobs' / 'job'
            project.mkdir(parents=True); job.mkdir(parents=True)
            worker.set_root(root); generation = 'generation'
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'starting'})
            write_json(project / 'active.json', {'job': 'job', 'pid': 123, 'stamp': 'x', 'generation': generation})
            write_json(project / 'warm.json', {'pid': 123, 'stamp': 'x', 'generation': generation})
            class Child:
                pid = 123
                def wait(self): return 7
            output = open(os.devnull, 'wb')
            runtime = worker.ManagedRuntime(root); runtime._watch('project', generation, Child(), output)
            self.assertEqual(read_json(job / 'state.json')['status'], 'failed')
            self.assertEqual(read_json(job / 'state.json')['exit_code'], 7)
            self.assertIsNone(read_json(project / 'active.json'))

    def test_post_ready_persistence_failure_cleans_only_failed_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder)
            project = {'name': 'project', 'remote_path': str(root), 'model': 'test-model',
                       'effort': 'low', 'timeout': 30, 'local_control': True}
            write_json(root / 'projects' / 'project' / 'project.json', project)
            write_json(root / 'projects' / 'project' / 'memory.json', {'revision': 0, 'entries': []})
            write_json(root / 'projects' / 'project' / 'channels' / 'group.json', {'thread_id': None})
            runtime = worker.ManagedRuntime(root)
            original = worker.write_json
            failed = []

            def fail_active_once(path, value):
                if Path(path).name == 'active.json' and isinstance(value, dict) and value.get('generation') and not failed:
                    failed.append(True)
                    raise OSError('synthetic active commit failure')
                return original(path, value)

            with patch.object(worker, 'write_json', side_effect=fail_active_once):
                with self.assertRaisesRegex(OSError, 'synthetic active commit failure'):
                    runtime.rpc({'action': 'start', 'project': 'project', 'job': 'fault-job',
                                 'prompt': json.dumps({'stage': 'discuss'}), 'session_channel': 'group'})

            directory = root / 'projects' / 'project'
            self.assertIsNone(read_json(directory / 'active.json'))
            self.assertIsNone(read_json(directory / 'warm.json'))
            self.assertIsNone(read_json(directory / 'launch.json'))
            self.assertIsNone(read_json(directory / 'ready.json'))
            deadline = time.time() + 5
            while time.time() < deadline:
                with runtime._guard:
                    if not runtime._children:
                        break
                time.sleep(.02)

    def test_reconcile_requires_two_missing_observations_without_guardian_evidence(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'process_stamp', return_value=None):
            root = Path(folder); worker.set_root(root); project = root / 'projects' / 'project'; job = root / 'jobs' / 'job'
            project.mkdir(parents=True); job.mkdir(parents=True)
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'starting'})
            active = {'job': 'job', 'pid': 999, 'stamp': 'x', 'generation': 'old'}
            first = worker.reconcile(active)
            self.assertEqual(first['unknown_observations'], 1)
            self.assertEqual(read_json(job / 'state.json')['status'], 'starting')
            self.assertIsNone(worker.reconcile(first))
            self.assertEqual(read_json(job / 'state.json')['status'], 'interrupted')

    def test_reconcile_uses_worker_self_exit_after_receiver_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); worker.set_root(root); project = root / 'projects' / 'project'; job = root / 'jobs' / 'job'
            project.mkdir(parents=True); job.mkdir(parents=True)
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'running'})
            write_json(project / 'worker-self-exit.json', {
                'generation': 'generation', 'pid': 123, 'reason': 'exception', 'error': 'bounded failure'})
            active = {'job': 'job', 'pid': 123, 'stamp': 'old', 'generation': 'generation'}

            self.assertIsNone(worker.reconcile(active))
            state = read_json(job / 'state.json')
            self.assertEqual(state['status'], 'failed')
            self.assertEqual(state['error'], 'bounded failure')


if __name__ == '__main__': unittest.main()
