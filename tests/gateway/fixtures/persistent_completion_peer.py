"""Actual daemon: persistent child wakes an idle owner with no connected viewer."""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import threading

from local_recovery_probe import child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages', [])
        text = next((m.get('content', '') for m in reversed(messages) if m['role'] == 'user'), '')
        message = {'role': 'assistant', 'content': 'MAIN_RESUMED'}
        if text == 'CHILD_ROUTING_PROBE':
            self.server.child_calls += 1
            self.server.entered.set()
            assert self.server.release.wait(60)
            message['content'] = 'CHILD_RETURNED_SAVED_RESULT'
        elif text == 'SPAWN_PERSISTENT_ROUTING_PROBE':
            if messages[-1]['role'] != 'tool':
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'saved-child',
                    'type': 'function', 'function': {'name': 'delegate_task', 'arguments': json.dumps({
                        'tasks': [{'goal': 'CHILD_ROUTING_PROBE', 'project': 'routing-proof'}]})}}]}
            else:
                message['content'] = 'MAIN_YIELDED_WHILE_CHILD_RUNNING'
        elif 'CHILD_RETURNED_SAVED_RESULT' in str(text):
            self.server.completion_inputs.append(text)
        finish = 'tool_calls' if message.get('tool_calls') else 'stop'
        payload = json.dumps({'id': 'fixture', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode()
        kind = 'application/json'
        if body.get('stream'):
            if message.get('tool_calls'):
                message['tool_calls'][0]['index'] = 0
            payload = ('data: ' + json.dumps({'id': 'fixture', 'choices': [
                {'index': 0, 'delta': message, 'finish_reason': finish}]}) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def probe(base, lost_ack):
    root = Path(__file__).resolve().parents[3]
    home, user = base / 'state', base / 'user'
    home.mkdir(); user.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.entered, model.release = threading.Event(), threading.Event()
    model.child_calls, model.completion_inputs = 0, []
    threading.Thread(target=model.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{model.server_port}/v1'
    cfg = {'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'fixture', 'base_url': url},
        'delegation': {'worktree_isolation': False}, 'approvals': {'mode': 'off'},
        'auxiliary': {'title_generation': {'enabled': False}}}
    (home / 'config.yaml').write_text(json.dumps(cfg))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')

    def rows():
        with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM session_admissions ORDER BY seq')]

    async def run(desc):
        async with websocket(home, desc) as ws:
            reply = await rpc(ws, 'session.create', request_id='owner', source='gui',
                              cwd=str(home), toolsets=['delegation'])
            assert 'result' in reply, reply
            sid = reply['result']['session_id']
            reply = await rpc(ws, 'prompt.submit', session_id=sid, input_id='spawn',
                              text='SPAWN_PERSISTENT_ROUTING_PROBE')
            assert 'result' in reply, reply
            assert await asyncio.to_thread(model.entered.wait, 20)
            await async_wait_until(lambda: rows()[0]['status'] == 'terminal', timeout=30)
        # Closing every observer cannot prevent the parent wake.
        model.release.set()
        if lost_ack:
            await async_wait_until(lambda: len(rows()) == 2, timeout=30)
            assert rows()[-1]['status'] == 'queued'
            assert not model.completion_inputs
            return sid
        await async_wait_until(lambda: len(rows()) == 2 and rows()[-1]['status'] == 'terminal', timeout=30)
        assert all(r['outcome'] == 'completed' for r in rows())
        assert len(model.completion_inputs) == 1 and model.child_calls == 1
        with closing(sqlite3.connect(home / 'state.db')) as db:
            assert db.execute('SELECT state,delivery_state FROM async_delegations').fetchall() == [('completed', 'delivered')]
        return sid

    try:
        with daemon(root, home, env, barrier=lost_ack, fixture='local_automation_daemon.py') as (_, desc):
            asyncio.run(run(desc))
        if lost_ack:
            # The native delivery lease survives a crash. Expire only this fixture's
            # claim instead of waiting five minutes for the same restart replay.
            with closing(sqlite3.connect(home / 'state.db')) as db:
                assert db.execute('SELECT delivery_claim FROM async_delegations').fetchone()[0]
                db.execute('UPDATE async_delegations SET delivery_claimed_at=0')
                db.commit()
        # Replaying the same durable ledger after restart must not execute the child or main twice.
        with daemon(root, home, env, barrier=False) as (_, desc):
            async def replay():
                await async_wait_until(lambda: len(rows()) == 2 and rows()[-1]['status'] == 'terminal', timeout=30)
                with closing(sqlite3.connect(home / 'state.db')) as db:
                    await async_wait_until(lambda: db.execute('SELECT delivery_state FROM async_delegations').fetchone()[0]
                                           == 'delivered', timeout=30)
                async with websocket(home, desc) as ws:
                    reply = await rpc(ws, 'session.list')
                    assert 'result' in reply, reply
                assert len(rows()) == 2
                assert model.child_calls == len(model.completion_inputs) == 1
            asyncio.run(replay())
        print(json.dumps({'child_calls': model.child_calls, 'main_completion_inputs': len(model.completion_inputs),
                          'admissions': len(rows()), 'restart': True, 'lost_producer_ack': lost_ack,
                          'viewer_closed_before_completion': True}))
    finally:
        model.release.set()
        model.shutdown(); model.server_close()


if __name__ == '__main__':
    probe(Path(sys.argv[1]), sys.argv[2] == 'lost_ack')
