"""Per-project local Codex app-server manager with an explicit receiver root."""

# Keep startup imports stdlib-only.  The CLI binds the receiver root and puts it
# first on sys.path before importing any receiver-local module.
import argparse
import importlib
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

DEFAULT_IDLE_SECONDS = 300
DEFAULT_INTERRUPT_GRACE_SECONDS = 5
ROOT = None


def configure_runtime(root):
    global ROOT, AppServer, PublicReplyDecoder, lock, read_json, write_json
    global worker, GROUP_RULES, cli_path, codex_login_status, effective_model
    global execution_timeout, executor_environment, job_dir, now, process_stamp, project_dir
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Explicit receiver root is invalid')
    # Import code from this immutable service bundle; root contains mutable
    # receiver state and may belong to an older active runtime generation.
    code_root = Path(__file__).resolve().parent
    value = str(code_root)
    sys.path[:] = [value] + [item for item in sys.path if os.path.normcase(os.path.abspath(item or '.')) != os.path.normcase(value)]
    # A fresh CLI process has no local modules yet. Tests may rebind a loaded
    # module; worker.set_root still makes all state paths explicit.
    worker = importlib.import_module('worker')
    worker.set_root(root)
    appserver = importlib.import_module('appserver_client')
    common = importlib.import_module('common')
    AppServer, PublicReplyDecoder = appserver.AppServer, appserver.PublicReplyDecoder
    lock, read_json, write_json = common.lock, common.read_json, common.write_json
    GROUP_RULES = worker.GROUP_RULES
    cli_path, codex_login_status = worker.cli_path, worker.codex_login_status
    effective_model, execution_timeout = worker.effective_model, worker.execution_timeout
    executor_environment, job_dir, now = worker.executor_environment, worker.job_dir, worker.now
    process_stamp, project_dir = worker.process_stamp, worker.project_dir
    ROOT = root
    return root


def response_schema():
    def obj(properties):
        return {'type': 'object', 'additionalProperties': False,
                'required': list(properties), 'properties': properties}
    def array(properties):
        return {'type': 'array', 'items': obj(properties)}
    return obj({'reply': {'type': 'string'}, 'done': {'type': 'boolean'},
                'messages': array({'recipient': {'type': ['string', 'null']}, 'body': {'type': 'string'}}),
                'tasks': array({key: {'type': 'string'} for key in
                                ('agent', 'title', 'instructions', 'stage', 'allowed_files', 'acceptance', 'validation')}),
                'executions': array({'template': {'type': 'string'}}),
                'memory_notes': {'type': 'array', 'items': {'type': 'string'}}})


def local_env():
    return executor_environment(ROOT)


def server_command(stage):
    command = [cli_path(ROOT), 'app-server', '--stdio',
               '--disable', 'apps', '--disable', 'plugins', '--disable', 'remote_plugin',
               '--disable', 'skill_mcp_dependency_install']
    if stage in {'discuss', 'review'}:
        command += ['--disable', 'shell_tool', '--disable', 'unified_exec']
    return command


def mcp_names(server):
    names, cursor = [], None
    while True:
        params = {'limit': 100}
        if cursor: params['cursor'] = cursor
        result = server.call('mcpServerStatus/list', params, 20)
        names.extend(item['name'] for item in result.get('data', []))
        cursor = result.get('nextCursor')
        if not cursor: return names


class WarmManager:
    def __init__(self, project, launch_token=None, generation=None):
        self.project, self.launch_token, self.generation = project, launch_token, generation
        self.directory = project_dir(project)
        self.server = self.stage = self.stderr = None
        # A thread stays attached to one app-server for that process lifetime.
        # Calling thread/resume again on the same server asks Codex to create a
        # second writer for a thread it already owns and is rejected.
        self.attached_thread_ids = set()
        self.stop_event = threading.Event()

    def publish_ready(self):
        try:
            launch = read_json(self.directory / 'launch.json', {}) or {}
            expected = {'token': self.launch_token, 'generation': self.generation,
                        'project': self.project, 'root': str(ROOT)}
            for key, value in expected.items():
                if launch.get(key) != value:
                    raise RuntimeError('Launch record rejected: %s does not match' % key)
            if process_stamp(launch.get('parent_pid')) != launch.get('parent_stamp'):
                raise RuntimeError('Launch record rejected: parent receiver is not alive')
            stamp = process_stamp(os.getpid())
            if stamp is None:
                raise RuntimeError('Launch record rejected: child process identity unavailable')
            ready = {'pid': os.getpid(), 'stamp': stamp, 'token': self.launch_token,
                     'generation': self.generation, 'root': str(ROOT)}
            write_json(self.directory / 'ready.json', ready)
            return ready
        except BaseException as exc:
            write_json(self.directory / 'startup-error.json', {
                'token': self.launch_token, 'generation': self.generation,
                'error': (type(exc).__name__ + ': ' + str(exc))[:2000], 'at': time.time()})
            raise

    def reject_registration(self, reason):
        write_json(self.directory / 'startup-error.json', {
            'token': self.launch_token, 'generation': self.generation,
            'error': str(reason)[:2000], 'at': time.time()})
        raise RuntimeError(reason)

    def ensure_server(self, stage, cwd):
        if self.server and self.stage == stage and self.server.process.poll() is None: return self.server
        self.close_server(); self.stderr = (self.directory / 'app-server-stderr.log').open('ab')
        command = server_command(stage); probe = AppServer(command, cwd, local_env(), self.stderr)
        try: names = mcp_names(probe)
        finally: probe.close()
        for name in names:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name): raise RuntimeError('Cannot safely disable a configured MCP server name')
            command += ['-c', 'mcp_servers.' + name + '={command="",enabled=false}']
        self.server = AppServer(command, cwd, local_env(), self.stderr)
        configured = (self.server.call('config/read', {'includeLayers': False}, 20).get('config') or {}).get('mcp_servers') or {}
        if any(not isinstance(item, dict) or item.get('enabled') is not False for item in configured.values()):
            self.close_server(); raise RuntimeError('Could not disable inherited MCP servers for the group worker')
        self.stage = stage; return self.server

    def close_server(self):
        if self.server: self.server.close(); self.server = None
        if self.stderr: self.stderr.close(); self.stderr = None
        self.attached_thread_ids.clear()

    def attach_thread(self, server, settings, existing):
        """Attach one session to this app-server, recovering stale writers safely."""
        if existing and existing in self.attached_thread_ids:
            return existing, None
        if existing:
            resume = dict(settings, threadId=existing)
            try:
                result = server.call('thread/resume', resume, 40)
                thread_id = result['thread']['id']
                self.attached_thread_ids.add(thread_id)
                return thread_id, None
            except RuntimeError as exc:
                # A previous app-server (including another Codex client) can
                # retain the thread-store writer after this worker has gone.
                # It is unsafe to kill an unrelated process.  Start a fresh
                # session instead; every turn already carries the bounded chat
                # context and project memory needed to continue the project.
                if 'already has an active writer' not in str(exc):
                    raise
                result = server.call('thread/start', settings, 40)
                thread_id = result['thread']['id']
                self.attached_thread_ids.add(thread_id)
                return thread_id, existing
        result = server.call('thread/start', settings, 40)
        thread_id = result['thread']['id']
        self.attached_thread_ids.add(thread_id)
        return thread_id, None

    def update_state(self, path, **values):
        state = read_json(path / 'state.json', {}); state.update(values); write_json(path / 'state.json', state); return state

    def run_job(self, job):
        path = job_dir(job); config = read_json(path / 'request.json')
        if not config or config.get('project', {}).get('name') != self.project or not config.get('managed_turn'):
            raise ValueError('Invalid warm worker assignment')
        project = config['project']; contract = json.loads(config['prompt']); stage = contract.get('stage', 'discuss'); cwd = project['remote_path']
        login = codex_login_status(cli_path(ROOT), ROOT)
        if not login['logged_in']: raise RuntimeError(login['login_error'])
        server = self.ensure_server(stage, cwd); existing = config.get('thread_id')
        settings = {'model': project['model'], 'modelProvider': 'openai', 'cwd': cwd,
                    'approvalPolicy': 'never', 'sandbox': 'workspace-write' if stage == 'implement' else 'read-only'}
        thread_id, replaced_thread = self.attach_thread(server, settings, existing)
        with lock(ROOT / 'registry.lock'):
            write_json(self.directory / config.get('session_file', 'session.json'), {'thread_id': thread_id, 'last_job': job})
        prompt = GROUP_RULES + '\nPROJECT MEMORY:\n' + json.dumps(config['memory'], ensure_ascii=False) + '\nTASK CONTRACT:\n' + config['prompt']
        (path / 'prompt.txt').write_text(prompt, encoding='utf-8')
        sandbox = {'type': 'workspaceWrite', 'writableRoots': [cwd], 'networkAccess': False} if stage == 'implement' else {'type': 'readOnly'}
        started = now(); self.update_state(path, status='running', started_at=started, thread_id=thread_id,
            requested_model=project['model'], reasoning_effort=project['effort'], app_server_pid=server.process.pid,
            session_replaced_from=replaced_thread,
            session_replaced_reason=('active_writer_conflict' if replaced_thread else None))
        turn_id = server.call('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}], 'cwd': cwd,
            'model': project['model'], 'effort': project['effort'], 'approvalPolicy': 'never', 'sandboxPolicy': sandbox,
            'outputSchema': response_schema()}, 40)['turn']['id']
        deadline = time.monotonic() + execution_timeout(config); decoder = PublicReplyDecoder(); report = None
        cancelled = timed_out = interrupted = False
        interrupt_deadline = None
        executor = read_json(ROOT / 'executor.json', {}) or {}
        grace = executor.get('interrupt_grace_seconds', DEFAULT_INTERRUPT_GRACE_SECONDS)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 1 <= grace <= 30:
            grace = DEFAULT_INTERRUPT_GRACE_SECONDS
        while True:
            if (path / 'cancel.json').exists(): cancelled = True
            if time.monotonic() >= deadline: timed_out = True
            if (cancelled or timed_out) and not interrupted:
                interrupted = True
                interrupt_deadline = time.monotonic() + grace
                try:
                    server.call('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id}, min(grace, 10))
                except (RuntimeError, TimeoutError):
                    pass
            if interrupted and time.monotonic() >= interrupt_deadline:
                self.update_state(path, status='cancelled' if cancelled else 'timed_out',
                                  finished_at=now(), error=None)
                self.close_server()
                return
            try: event = server.event(.2)
            except TimeoutError: continue
            except RuntimeError:
                if not interrupted:
                    raise
                self.update_state(path, status='cancelled' if cancelled else 'timed_out',
                                  finished_at=now(), error=None)
                self.close_server()
                return
            if 'id' in event: continue
            method, params = event.get('method'), event.get('params') or {}
            if params.get('turnId') not in {None, turn_id}: continue
            if method == 'item/agentMessage/delta':
                draft = decoder.feed(params.get('delta', '')); previous = read_json(path / 'state.json', {}).get('reply_draft')
                if draft and draft != previous: self.update_state(path, reply_draft=draft)
            elif method == 'item/completed':
                item = params.get('item') or {}
                if item.get('type') == 'agentMessage':
                    try: parsed = json.loads(item.get('text', ''))
                    except (TypeError, ValueError): continue
                    if isinstance(parsed, dict) and isinstance(parsed.get('reply'), str):
                        report = item['text']; (path / 'report.txt').write_text(report, encoding='utf-8'); self.update_state(path, reply_draft=parsed['reply'][:16000])
            elif method == 'turn/completed' and (params.get('turn') or {}).get('id') == turn_id:
                turn = params['turn']; status = 'cancelled' if cancelled else 'timed_out' if timed_out else 'needs_review' if turn.get('status') == 'completed' and report else 'failed'
                effective = effective_model(thread_id, started)
                if effective and effective != project['model']: status = 'failed'
                self.update_state(path, status=status, finished_at=now(),
                    error=('Effective model differs from requested model' if effective and effective != project['model'] else (turn.get('error') or {}).get('message') if status == 'failed' else None),
                    effective_model=effective, model_verified=bool(effective))
                if interrupted: self.close_server()
                return

    def loop(self):
        executor = read_json(ROOT / 'executor.json', {}); idle = executor.get('warm_idle_seconds', DEFAULT_IDLE_SECONDS)
        if isinstance(idle, bool) or not isinstance(idle, int) or not 30 <= idle <= 3600: idle = DEFAULT_IDLE_SECONDS
        last_active = time.monotonic(); self.publish_ready(); registered = False
        exit_record = {'generation': self.generation, 'pid': os.getpid(),
                       'stamp': process_stamp(os.getpid()), 'reason': 'normal'}
        try:
            while True:
                with lock(ROOT / 'registry.lock'):
                    active = read_json(self.directory / 'active.json'); warm = read_json(self.directory / 'warm.json')
                    owns = bool(warm and warm.get('pid') == os.getpid() and warm.get('stamp') == process_stamp(os.getpid())
                                and warm.get('token') == self.launch_token and warm.get('generation') == self.generation)
                    if owns: registered = True
                    elif registered:
                        self.reject_registration('Warm registration was revoked')
                    else:
                        launch = read_json(self.directory / 'launch.json', {}) or {}
                        if launch.get('token') != self.launch_token or launch.get('generation') != self.generation:
                            self.reject_registration('Warm registration was rejected before ACK')
                    if owns and not active and time.monotonic() - last_active >= idle:
                        write_json(self.directory / 'warm.json', None); return
                if not owns:
                    self.stop_event.wait(.05); continue
                if active:
                    job = active['job']; state = read_json(job_dir(job) / 'state.json', {})
                    if state.get('status') == 'starting':
                        try: self.run_job(job)
                        except Exception as exc:
                            self.update_state(job_dir(job), status='failed', error=str(exc)[:2000], finished_at=now()); self.close_server()
                        finally:
                            with lock(ROOT / 'registry.lock'):
                                current = read_json(self.directory / 'active.json')
                                if current and current.get('job') == job: write_json(self.directory / 'active.json', None)
                            last_active = time.monotonic()
                self.stop_event.wait(.2)
        except BaseException as exc:
            exit_record.update(reason='exception', error=(type(exc).__name__ + ': ' + str(exc))[:2000])
            with (self.directory / 'warm-supervisor.log').open('a', encoding='utf-8') as output:
                output.write(time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + ''.join(traceback.format_exception_only(type(exc), exc))[-2000:])
            raise
        finally:
            exit_record['finished_at'] = now()
            write_json(self.directory / 'worker-self-exit.json', exit_record)
            self.close_server()


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument('--root', required=True); parser.add_argument('--project', required=True)
    parser.add_argument('--token', required=True); parser.add_argument('--generation', required=True); args = parser.parse_args(argv)
    configure_runtime(args.root)
    from common import identifier
    WarmManager(identifier(args.project), args.token, args.generation).loop()


if __name__ == '__main__':
    main()
else:
    # Library/tests bind the source checkout; CLI execution never reaches this
    # branch before parsing its explicit receiver root.
    configure_runtime(Path(__file__).resolve().parent)
