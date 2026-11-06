"""Script only external inference; all agents, tools, children and stores stay native."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shlex
import threading
import uuid

NAMES = ('alpha', 'bravo')


def sandbox_command(name, phase):
    other = 'bravo' if name == 'alpha' else 'alpha'
    code = ("from pathlib import Path; import json,os; "
            "h=Path('/agent'); text=(h/'.env').read_text(); "
            f"assert 'ISOLATION_TOKEN={name}-canary' in text; "
            f"assert '{other}-canary' not in text; "
            f"assert os.environ.get('OPENAI_API_KEY','fixture-{name}') == 'fixture-{name}'; "
            "assert not Path('/var/run/docker.sock').exists(); "
            "assert not Path('/probe').exists(); "
            f"Path('/workspace/{phase}.json').write_text(json.dumps(dict(owner='{name}',ownSecret=True,foreignHomeAbsent=True)))")
    return '/opt/hermes/.venv/bin/python -c ' + shlex.quote(code)


def call(name, args):
    return {'id': 'call_' + uuid.uuid4().hex, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


def scripted_reply(server, request, name):
    messages = request['messages']
    index = max(i for i, m in enumerate(messages) if m['role'] == 'user')
    text = str(messages[index].get('content', ''))
    tools = [m for m in messages[index + 1:] if m['role'] == 'tool']
    if '[ASYNC DELEGATION COMPLETE' in text or '[INTERNAL NOTIFICATION' in text:
        return {'role': 'assistant', 'content': 'COMPLETION_' + name}
    if 'CHILD_' + name in text:
        if not tools:
            return {'role': 'assistant', 'tool_calls': [call('terminal', {
                'command': sandbox_command(name, 'child'), 'timeout': 20})]}
        server.child_entered[name].set()
        if not server.release[name].wait(90):
            raise TimeoutError('CHILD_RELEASE_DEADLINE:' + name)
        return {'role': 'assistant', 'content': 'CHILD_DONE_' + name}
    if 'SANDBOX_' + name in text and not tools:
        return {'role': 'assistant', 'tool_calls': [call('terminal', {
            'command': sandbox_command(name, 'parent'), 'timeout': 20})]}
    if 'MEMORY_' + name in text and not tools:
        return {'role': 'assistant', 'tool_calls': [call('memory', {
            'action': 'add', 'target': 'memory', 'content': 'Private favorite color for ' + name + ' is amber.'})]}
    if 'SPAWN_' + name in text and not tools:
        return {'role': 'assistant', 'tool_calls': [call('delegate_task', {'tasks': [{
            'goal': 'CHILD_' + name + ': run your terminal check, then return CHILD_DONE_' + name,
            'project': 'same-project', 'role': 'leaf'}]})]}
    if 'ROSTER_' + name in text and not tools:
        return {'role': 'assistant', 'tool_calls': [call('delegate_task', {'action': 'list'})]}
    if 'ROSTER_' + name in text:
        payload = json.loads(tools[-1]['content'])
        projects = payload.get('projects', [])
        server.roster[name] = {'onlyOwnChild': len(projects) == 1 and
            projects[0]['subagent_id'] == server.expected_ids[name]}
    if 'FOREIGN_' + name in text and not tools:
        operation, identity = text.split('FOREIGN_' + name + ':', 1)[1].split()[:2]
        return {'role': 'assistant', 'tool_calls': [call('delegate_task', {
            'action': operation, 'subagent_id': identity})]}
    if 'FOREIGN_' + name in text:
        server.foreign_refused.append({'owner': name, 'rejected': 'error' in tools[-1]['content'].lower()})
    if 'SEND_' + name in text and not tools:
        identity = text.split('SEND_' + name + ':', 1)[1].split()[0]
        return {'role': 'assistant', 'tool_calls': [call('delegate_task', {
            'action': 'send', 'subagent_id': identity, 'message': 'CHILD_' + name + ': persisted followup'})]}
    return {'role': 'assistant', 'content': 'ACK_' + name + '_' + ('TOOL' if tools else 'TURN')}


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        name = request.get('model', '').removeprefix('fixture-')
        valid = name in NAMES and self.headers.get('Authorization') == 'Bearer fixture-' + name
        self.server.receipts.append({'owner': name, 'ownCredential': valid})
        if not valid:
            self.send_error(401)
            return
        try:
            message = scripted_reply(self.server, request, name)
        except TimeoutError:
            self.send_error(503)
            return
        finish = 'tool_calls' if message.get('tool_calls') else 'stop'
        base = {'id': 'fixture-' + uuid.uuid4().hex, 'model': request['model'],
                'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70}}
        if request.get('stream'):
            delta = dict(message)
            if delta.get('tool_calls'):
                delta['tool_calls'] = [dict(t, index=i) for i, t in enumerate(delta['tool_calls'])]
            base.update(object='chat.completion.chunk', choices=[{'index': 0, 'delta': delta, 'finish_reason': finish}])
            body = ('data: ' + json.dumps(base) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            base.update(object='chat.completion', choices=[{'index': 0, 'message': message, 'finish_reason': finish}])
            body = json.dumps(base).encode()
            kind = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_model():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    server.receipts = []
    server.child_entered = {n: threading.Event() for n in NAMES}
    server.release = {n: threading.Event() for n in NAMES}
    server.expected_ids, server.roster, server.foreign_refused = {}, {}, []
    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(server.serve_forever, name='fixture-inference').start()
    return server
