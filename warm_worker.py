"""Per-project local Codex app-server manager with a bounded idle lifetime."""

import json
import os
import re
import time
from pathlib import Path

from appserver_client import AppServer, PublicReplyDecoder
from common import lock, read_json, write_json
from worker import (GROUP_RULES, ROOT, cli_path, codex_login_status, effective_model, execution_timeout,
                    executor_environment,
                    job_dir, now, process_stamp, project_dir)

DEFAULT_IDLE_SECONDS = 300


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
    command = [cli_path(), 'app-server', '--stdio',
               '--disable', 'apps', '--disable', 'plugins', '--disable', 'remote_plugin',
               '--disable', 'skill_mcp_dependency_install']
    if stage in {'discuss', 'review'}:
        command += ['--disable', 'shell_tool', '--disable', 'unified_exec']
    return command


def mcp_names(server):
    names = []
    cursor = None
    while True:
        params = {'limit': 100}
        if cursor:
            params['cursor'] = cursor
        result = server.call('mcpServerStatus/list', params, 20)
        names.extend(item['name'] for item in result.get('data', []))
        cursor = result.get('nextCursor')
        if not cursor:
            return names


class WarmManager:
    def __init__(self, project):
        self.project = project
        self.directory = project_dir(project)
        self.server = None
        self.stage = None
        self.stderr = None

    def ensure_server(self, stage, cwd):
        if self.server and self.stage == stage and self.server.process.poll() is None:
            return self.server
        self.close_server()
        self.stderr = (self.directory / 'app-server-stderr.log').open('ab')
        command = server_command(stage)
        # App-server lacks exec's --ignore-user-config. Discover configured MCP
        # names without a model turn, then start the actual worker with every
        # inherited server disabled. Never read or serialize MCP credentials.
        probe = AppServer(command, cwd, local_env(), self.stderr)
        try:
            names = mcp_names(probe)
        finally:
            probe.close()
        for name in names:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                raise RuntimeError('Cannot safely disable a configured MCP server name')
            command += ['-c', 'mcp_servers.' + name + '={command="",enabled=false}']
        self.server = AppServer(command, cwd, local_env(), self.stderr)
        configured = (self.server.call('config/read', {'includeLayers': False}, 20)
                      .get('config') or {}).get('mcp_servers') or {}
        if any(not isinstance(item, dict) or item.get('enabled') is not False for item in configured.values()):
            self.close_server()
            raise RuntimeError('Could not disable inherited MCP servers for the group worker')
        self.stage = stage
        return self.server

    def close_server(self):
        if self.server:
            self.server.close()
            self.server = None
        if self.stderr:
            self.stderr.close()
            self.stderr = None

    def update_state(self, path, **values):
        state = read_json(path / 'state.json', {})
        state.update(values)
        write_json(path / 'state.json', state)
        return state

    def run_job(self, job):
        path = job_dir(job)
        config = read_json(path / 'request.json')
        if not config or config.get('project', {}).get('name') != self.project or not config.get('managed_turn'):
            raise ValueError('Invalid warm worker assignment')
        project = config['project']
        contract = json.loads(config['prompt'])
        stage = contract.get('stage', 'discuss')
        cwd = project['remote_path']
        login=codex_login_status(cli_path(),ROOT)
        if not login['logged_in']:
            raise RuntimeError(login['login_error'])
        server = self.ensure_server(stage, cwd)
        existing = config.get('thread_id')
        thread_settings = {'model': project['model'], 'modelProvider': 'openai', 'cwd': cwd,
                           'approvalPolicy': 'never',
                           'sandbox': 'workspace-write' if stage == 'implement' else 'read-only'}
        if existing:
            thread_settings['threadId'] = existing
        thread_result = server.call('thread/resume' if existing else 'thread/start', thread_settings, 40)
        thread_id = thread_result['thread']['id']
        session_file = self.directory / config.get('session_file', 'session.json')
        with lock(ROOT / 'registry.lock'):
            write_json(session_file, {'thread_id': thread_id, 'last_job': job})
        prompt = GROUP_RULES + '\nPROJECT MEMORY:\n' + json.dumps(config['memory'], ensure_ascii=False) + '\nTASK CONTRACT:\n' + config['prompt']
        (path / 'prompt.txt').write_text(prompt, encoding='utf-8')
        sandbox = {'type': 'workspaceWrite', 'writableRoots': [cwd], 'networkAccess': False} if stage == 'implement' else {'type': 'readOnly'}
        started = now()
        self.update_state(path, status='running', started_at=started, thread_id=thread_id,
                          requested_model=project['model'], reasoning_effort=project['effort'],
                          app_server_pid=server.process.pid)
        start = server.call('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}],
                                           'cwd': cwd, 'model': project['model'], 'effort': project['effort'],
                                           'approvalPolicy': 'never', 'sandboxPolicy': sandbox,
                                           'outputSchema': response_schema()}, 40)
        turn_id = start['turn']['id']
        deadline = time.monotonic() + execution_timeout(config)
        decoder = PublicReplyDecoder()
        report = None
        cancelled = timed_out = interrupted = False
        while True:
            if (path / 'cancel.json').exists():
                cancelled = True
            if time.monotonic() >= deadline:
                timed_out = True
            if (cancelled or timed_out) and not interrupted:
                server.call('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id}, 10)
                interrupted = True
            try:
                event = server.event(.2)
            except TimeoutError:
                continue
            if 'id' in event:
                continue
            method = event.get('method')
            params = event.get('params') or {}
            if params.get('turnId') not in {None, turn_id}:
                continue
            if method == 'item/agentMessage/delta':
                draft = decoder.feed(params.get('delta', ''))
                previous = read_json(path / 'state.json', {}).get('reply_draft')
                if draft and draft != previous:
                    self.update_state(path, reply_draft=draft)
            elif method == 'item/completed':
                item = params.get('item') or {}
                if item.get('type') == 'agentMessage':
                    candidate = item.get('text', '')
                    try:
                        parsed = json.loads(candidate)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(parsed, dict) and isinstance(parsed.get('reply'), str):
                        report = candidate
                        (path / 'report.txt').write_text(report, encoding='utf-8')
                        self.update_state(path, reply_draft=parsed['reply'][:16000])
            elif method == 'turn/completed':
                turn = params.get('turn') or {}
                if turn.get('id') != turn_id:
                    continue
                status = 'cancelled' if cancelled else 'timed_out' if timed_out else 'needs_review' if turn.get('status') == 'completed' and report else 'failed'
                effective = effective_model(thread_id, started)
                if effective and effective != project['model']:
                    status = 'failed'
                self.update_state(path, status=status, finished_at=now(),
                                  error=('Effective model differs from requested model' if effective and effective != project['model']
                                         else (turn.get('error') or {}).get('message') if status == 'failed' else None),
                                  effective_model=effective, model_verified=bool(effective))
                return

    def loop(self):
        executor = read_json(ROOT / 'executor.json', {})
        idle = executor.get('warm_idle_seconds', DEFAULT_IDLE_SECONDS)
        if isinstance(idle, bool) or not isinstance(idle, int) or not 30 <= idle <= 3600:
            idle = DEFAULT_IDLE_SECONDS
        last_active = time.monotonic()
        try:
            while True:
                with lock(ROOT / 'registry.lock'):
                    active = read_json(self.directory / 'active.json')
                    if not active and time.monotonic() - last_active >= idle:
                        warm = read_json(self.directory / 'warm.json')
                        if warm and warm.get('pid') == os.getpid():
                            write_json(self.directory / 'warm.json', None)
                        return
                if active and active.get('pid') == os.getpid():
                    job = active['job']
                    state = read_json(job_dir(job) / 'state.json', {})
                    if state.get('status') == 'starting':
                        try:
                            self.run_job(job)
                        except Exception as exc:
                            self.update_state(job_dir(job), status='failed', error=str(exc)[:2000], finished_at=now())
                            self.close_server()
                        finally:
                            with lock(ROOT / 'registry.lock'):
                                current = read_json(self.directory / 'active.json')
                                if current and current.get('job') == job:
                                    write_json(self.directory / 'active.json', None)
                            last_active = time.monotonic()
                time.sleep(.2)
        finally:
            self.close_server()


if __name__ == '__main__':
    import sys
    from common import identifier
    WarmManager(identifier(sys.argv[1])).loop()
