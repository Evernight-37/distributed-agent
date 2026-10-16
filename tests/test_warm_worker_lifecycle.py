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
    def test_attached_thread_is_reused_without_second_resume(self):
        class Server:
            def __init__(self): self.calls = []
            def call(self, method, params, timeout):
                self.calls.append((method, params, timeout))
                return {'thread': {'id': params.get('threadId', 'new-thread')}}
        manager = warm_worker.WarmManager('project')
        server = Server()
        settings = {'model': 'test-model', 'cwd': 'C:\\project'}
        first, replaced = manager.attach_thread(server, settings, 'existing-thread')
        second, replaced_again = manager.attach_thread(server, settings, 'existing-thread')
        self.assertEqual((first, second), ('existing-thread', 'existing-thread'))
        self.assertIsNone(replaced)
        self.assertIsNone(replaced_again)
        self.assertEqual([item[0] for item in server.calls], ['thread/resume'])

    def test_switching_channels_keeps_all_threads_attached(self):
        class Server:
            def __init__(self): self.calls = []
            def call(self, method, params, timeout):
                self.calls.append((method, params, timeout))
                return {'thread': {'id': params['threadId']}}
        manager = warm_worker.WarmManager('project')
        server = Server(); settings = {'model': 'test-model', 'cwd': 'C:\\project'}
        manager.attach_thread(server, settings, 'group-thread')
        manager.attach_thread(server, settings, 'private-thread')
        manager.attach_thread(server, settings, 'group-thread')
        self.assertEqual([item[1]['threadId'] for item in server.calls],
                         ['group-thread', 'private-thread'])

    def test_active_writer_conflict_starts_fresh_session(self):
        class Server:
            def __init__(self): self.calls = []
            def call(self, method, params, timeout):
                self.calls.append((method, dict(params), timeout))
                if method == 'thread/resume':
                    raise RuntimeError('Codex app-server rejected thread/resume: thread old already has an active writer')
                return {'thread': {'id': 'replacement-thread'}}
        manager = warm_worker.WarmManager('project')
        server = Server()
        settings = {'model': 'test-model', 'cwd': 'C:\\project'}
        thread_id, replaced = manager.attach_thread(server, settings, 'old-thread')
        self.assertEqual(thread_id, 'replacement-thread')
        self.assertEqual(replaced, 'old-thread')
        self.assertEqual([item[0] for item in server.calls], ['thread/resume', 'thread/start'])
        self.assertNotIn('threadId', server.calls[1][1])

    def test_receiver_retires_its_idle_worker_without_touching_active(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder); worker.set_root(root)
            idle = root / 'projects' / 'idle'; idle.mkdir(parents=True)
            active = root / 'projects' / 'active'; active.mkdir(parents=True)
            write_json(idle / 'active.json', None)
            write_json(active / 'active.json', {'job': 'job-active'})
            class Child:
                pid = 101
                def poll(self): return None
                def wait(self, timeout): raise subprocess.TimeoutExpired('child', timeout)
            runtime = worker.ManagedRuntime(root)
            runtime._children = {'idle-generation': Child(), 'active-generation': Child()}
            write_json(idle / 'warm.json', {'generation': 'idle-generation', 'pid': 101})
            write_json(active / 'warm.json', {'generation': 'active-generation', 'pid': 101})
            self.assertEqual(runtime.retire_idle_workers(), 1)
            self.assertIsNone(read_json(idle / 'warm.json'))
            self.assertEqual(read_json(active / 'warm.json')['generation'], 'active-generation')

    def setUp(self):
        self.original_root = worker.ROOT

    def tearDown(self):
        worker.set_root(self.original_root)

    def runtime_root(self, folder):
        root = Path(folder)
        for name in ('common.py', 'worker.py', 'warm_worker.py', 'appserver_client.py', 'jobrunner.py'):
            shutil.copyfile(Path(__file__).parents[1] / 'src' / 'agent_harness' / name, root / name)
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

    def test_second_turn_reuses_idle_warm_worker_and_commits_new_owner(self):
        """JSON null in active.json is the normal state between warm turns."""
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder)
            worker.set_root(root)
            directory = root / 'projects' / 'project'
            project = {'name': 'project', 'remote_path': str(root), 'model': 'test-model',
                       'effort': 'low', 'timeout': 30, 'local_control': True}
            write_json(directory / 'project.json', project)
            write_json(directory / 'memory.json', {'revision': 0, 'entries': []})
            write_json(directory / 'channels' / 'group.json', {'thread_id': 'existing-thread'})
            write_json(directory / 'active.json', None)

            generation = uuid.uuid4().hex
            pid = os.getpid()
            stamp = worker.process_stamp(pid)
            write_json(directory / 'warm.json', {
                'pid': pid, 'stamp': stamp, 'token': uuid.uuid4().hex,
                'generation': generation})

            class ExistingWarmRuntime:
                def owns(self, candidate_generation, candidate_pid):
                    return candidate_generation == generation and candidate_pid == pid

            result = worker.rpc({
                'action': 'start', 'project': 'project', 'job': 'second-turn',
                'prompt': json.dumps({'stage': 'discuss'}), 'session_channel': 'group'},
                ExistingWarmRuntime())

            active = read_json(directory / 'active.json')
            self.assertEqual(result['status'], 'starting')
            self.assertEqual(active, {
                'job': 'second-turn', 'pid': pid, 'stamp': stamp,
                'generation': generation})
            fetched = worker.rpc({'action': 'fetch', 'job': 'second-turn'})
            self.assertEqual(fetched['status'], 'starting')
            self.assertNotEqual(fetched['status'], 'interrupted')

    def test_external_script_imports_worker_from_receiver_root(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as external:
            root = self.runtime_root(state); token = uuid.uuid4().hex; generation = uuid.uuid4().hex
            self.launch_record(root, token, generation)
            script = Path(external) / 'warm_worker.py'
            for name in ('common.py', 'worker.py', 'warm_worker.py', 'appserver_client.py', 'jobrunner.py'):
                shutil.copyfile(Path(__file__).parents[1] / 'src' / 'agent_harness' / name, Path(external) / name)
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

    def test_guardian_prefers_same_generation_worker_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); project = root / 'projects' / 'project'; job = root / 'jobs' / 'job'
            project.mkdir(parents=True); job.mkdir(parents=True); worker.set_root(root)
            generation = 'generation'
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'running'})
            write_json(project / 'active.json', {'job': 'job', 'pid': 123, 'stamp': 'x', 'generation': generation})
            write_json(project / 'worker-self-exit.json', {
                'generation': generation, 'pid': 123, 'reason': 'exception', 'error': 'specific worker failure'})
            class Child:
                pid = 123
                def wait(self): return 9
            runtime = worker.ManagedRuntime(root)
            runtime._watch('project', generation, Child(), open(os.devnull, 'wb'))
            state = read_json(job / 'state.json')
            self.assertEqual(state['error'], 'specific worker failure')
            self.assertEqual(read_json(project / 'worker-exit.json')['error'], 'specific worker failure')

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
            # Repeated UI fetches at the same instant are one observation.
            second = worker.reconcile(first)
            self.assertEqual(second['unknown_observations'], 1)
            second['unknown_observed_at'] -= worker.PROCESS_MISSING_CONFIRM_SECONDS + .1
            self.assertIsNone(worker.reconcile(second))
            self.assertEqual(read_json(job / 'state.json')['status'], 'interrupted')

    def test_managed_fetch_returns_bounded_relevant_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); worker.set_root(root)
            project = root / 'projects' / 'project'; job = root / 'jobs' / 'job'
            project.mkdir(parents=True); job.mkdir(parents=True)
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'failed'})
            write_json(job / 'request.json', {'managed_turn': True})
            (project / 'app-server-stderr.log').write_text('a' * 40000, encoding='utf-8')
            (project / 'warm-supervisor.log').write_text('b' * 40000, encoding='utf-8')
            result = worker.rpc({'action': 'fetch', 'job': 'job'})
            self.assertEqual(len(result['app_server_stderr']), 32768)
            self.assertEqual(len(result['warm_supervisor_log']), 32768)

    def test_nonterminal_job_without_active_owner_converges_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); worker.set_root(root)
            project = root / 'projects' / 'project'; job = root / 'jobs' / 'orphan'
            project.mkdir(parents=True); job.mkdir(parents=True)
            write_json(job / 'state.json', {
                'job': 'orphan', 'project': 'project', 'status': 'starting'})
            write_json(project / 'active.json', None)

            result = worker.rpc({'action': 'fetch', 'job': 'orphan'})

            self.assertEqual(result['status'], 'interrupted')
            self.assertIn('No active worker owns', result['error'])

    def test_timeout_without_turn_completed_has_bounded_terminal_state_and_closes_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder); worker.set_root(root); warm_worker.configure_runtime(root)
            project_dir = root / 'projects' / 'project'; job = root / 'jobs' / 'job'; job.mkdir(parents=True)
            project = {'name': 'project', 'remote_path': str(root), 'model': 'test-model',
                       'effort': 'low', 'timeout': .05, 'local_control': True}
            write_json(job / 'request.json', {'project': project, 'prompt': json.dumps({'stage': 'discuss'}),
                                               'managed_turn': True, 'memory': {}, 'thread_id': None})
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'starting'})
            write_json(root / 'executor.json', {'interrupt_grace_seconds': 1})

            class Process: pid = 456
            class Server:
                process = Process()
                def __init__(self): self.interrupted = False; self.closed = False
                def call(self, method, params, timeout=30):
                    if method == 'thread/start': return {'thread': {'id': 'thread'}}
                    if method == 'turn/start': return {'turn': {'id': 'turn'}}
                    if method == 'turn/interrupt': self.interrupted = True; return {}
                    raise AssertionError(method)
                def event(self, timeout): raise TimeoutError()
                def close(self): self.closed = True
            server = Server(); manager = warm_worker.WarmManager('project', 'token', 'generation'); manager.server = server
            with patch.object(manager, 'ensure_server', return_value=server), \
                 patch.object(warm_worker, 'cli_path', return_value='codex'), \
                 patch.object(warm_worker, 'codex_login_status', return_value={'logged_in': True}), \
                 patch.object(warm_worker, 'effective_model', return_value='test-model'):
                started = time.monotonic(); manager.run_job('job')
            state = read_json(job / 'state.json')
            self.assertEqual(state['status'], 'timed_out')
            self.assertTrue(server.interrupted); self.assertTrue(server.closed)
            self.assertLess(time.monotonic() - started, 2.5)

    def test_disconnect_after_interrupt_preserves_timeout_terminal_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.runtime_root(folder); worker.set_root(root); warm_worker.configure_runtime(root)
            job = root / 'jobs' / 'job'; job.mkdir(parents=True)
            project = {'name': 'project', 'remote_path': str(root), 'model': 'test-model',
                       'effort': 'low', 'timeout': .01, 'local_control': True}
            write_json(job / 'request.json', {'project': project, 'prompt': json.dumps({'stage': 'discuss'}),
                                               'managed_turn': True, 'memory': {}, 'thread_id': None})
            write_json(job / 'state.json', {'job': 'job', 'project': 'project', 'status': 'starting'})
            class Process: pid = 456
            class Server:
                process = Process()
                closed = False
                events = 0
                def call(self, method, params, timeout=30):
                    if method == 'thread/start': return {'thread': {'id': 'thread'}}
                    if method == 'turn/start': return {'turn': {'id': 'turn'}}
                    if method == 'turn/interrupt': return {}
                def event(self, timeout):
                    self.events += 1
                    if self.events == 1:
                        time.sleep(.02); raise TimeoutError()
                    raise RuntimeError('disconnected')
                def close(self): self.closed = True
            server = Server(); manager = warm_worker.WarmManager('project'); manager.server = server
            with patch.object(manager, 'ensure_server', return_value=server), \
                 patch.object(warm_worker, 'cli_path', return_value='codex'), \
                 patch.object(warm_worker, 'codex_login_status', return_value={'logged_in': True}), \
                 patch.object(warm_worker, 'effective_model', return_value='test-model'):
                manager.run_job('job')
            self.assertEqual(read_json(job / 'state.json')['status'], 'timed_out')
            self.assertTrue(server.closed)

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
