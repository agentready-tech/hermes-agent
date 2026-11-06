"""Hermetic real-gateway transport probe; only the external model is scripted."""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        model = request.get('model', '')
        expected = 'Bearer fixture-' + model.removeprefix('fixture-')
        self.server.receipts.append({'model': model, 'ownCredential': self.headers.get('Authorization') == expected})
        if self.headers.get('Authorization') != expected:
            self.send_error(401); return
        # Both real parent runtimes must reach inference while neither turn has
        # completed. A session-id-only lease fails this deterministic barrier.
        if self.server.parallel and model in ('fixture-alpha', 'fixture-bravo'):
            self.server.barrier.wait(timeout=12)
        text = 'ACK_' + model
        message = {'role': 'assistant', 'content': text}
        payload = {'id': 'fixture-' + uuid.uuid4().hex, 'object': 'chat.completion', 'model': model,
                   'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 20, 'completion_tokens': 4, 'total_tokens': 24}}
        if request.get('stream'):
            event = {**payload, 'object': 'chat.completion.chunk', 'choices': [
                {'index': 0, 'delta': message, 'finish_reason': 'stop'}]}
            data = ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            data = json.dumps(payload).encode(); kind = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)


def initialize(root, url):
    root.mkdir(parents=True, mode=0o700)
    for name, home in [('control', root), *[(n, root / 'profiles' / n) for n in ('alpha', 'bravo')]]:
        home.mkdir(parents=True, exist_ok=True)
        (home / 'workspace').mkdir(exist_ok=True)
        config = {'_config_version': 12, 'gateway': {'multiplex_profiles': name == 'control'},
                  'model': {'provider': 'custom', 'default': 'fixture-' + name, 'base_url': url,
                            'key_env': 'OPENAI_API_KEY', 'context_length': 131072},
                  'auxiliary': {'title_generation': {'enabled': False}},
                  'platform_toolsets': {'cli': [], 'telegram': []}, 'memory': {'memory_enabled': False}}
        (home / 'config.yaml').write_text(json.dumps(config))
        (home / '.env').write_text('OPENAI_API_KEY=fixture-' + name + '\nTELEGRAM_ALLOWED_USERS=9001\n')


def control(home, verb, params=None):
    pointer = home / 'gateway.sock.path'
    path = pointer.read_text().strip() if pointer.exists() else str(home / 'gateway.sock')
    request = json.dumps({'protocol': 1, 'id': 1, 'verb': verb, 'params': params or {}}).encode() + b'\n'
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(3); peer.connect(path); peer.sendall(request)
        with peer.makefile('rb') as stream:
            response = json.loads(stream.readline())
    if not response.get('ok'):
        raise RuntimeError('CONTROL_REJECTED:' + str(response.get('error')))
    return response['result']


async def rpc(ws, method, **params):
    identity = uuid.uuid4().hex
    await ws.send(json.dumps({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params}))
    async with asyncio.timeout(45):
        while True:
            reply = json.loads(await ws.recv())
            if reply.get('id') == identity:
                if 'error' in reply:
                    raise RuntimeError(method + ':' + json.dumps(reply['error']))
                return reply['result']


async def exercise(home, descriptor):
    from websockets.asyncio.client import connect
    results = {}
    async def profile(name):
        grant = control(home, 'session-ticket', {'profile_id': str((home / 'profiles' / name).resolve()),
            'instance_id': descriptor['instance_id'], 'purpose': 'interactive'})
        async with connect(descriptor['api_origin'].replace('http:', 'ws:') + '/api/ws',
                subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + grant['ticket']]) as ws:
            session = await rpc(ws, 'session.resume', session_id='agentready_home', profile=name)
            sid = session['session_id']
            await rpc(ws, 'prompt.submit', session_id=sid, profile=name, input_id='warm-' + name, text='WARM_' + name)
            deadline = time.monotonic() + 45
            while True:
                view = await rpc(ws, 'session.resume', session_id=sid, profile=name)
                encoded = json.dumps(view.get('messages', []))
                if ('ACK_fixture-' + name in encoded and not view['running']
                        and all(row['status'] == 'terminal' for row in view['pending'])):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('TENANT_REPLY_MISSING:' + name)
                await asyncio.sleep(.1)
            other = 'bravo' if name == 'alpha' else 'alpha'
            if 'ACK_fixture-' + other in encoded or 'WARM_' + other in encoded:
                raise RuntimeError('CROSS_PROFILE_HISTORY')
            results[name] = {'logicalSessionId': view.get('stored_session_id'), 'model': view.get('info', {}).get('model'),
                             'ownReply': True, 'foreignHistoryAbsent': True}
    await asyncio.gather(*(profile(name) for name in ('alpha', 'bravo')))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(); os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    home = args.output / '.hermes'
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model); model.receipts = []
    model.parallel = True; model.barrier = threading.Barrier(2)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    initialize(home, f'http://127.0.0.1:{model.server_port}/v1')
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ') if k in os.environ}
    env.update(HOME=str(args.output / 'user'), HERMES_HOME=str(home), PYTHONUNBUFFERED='1')
    Path(env['HOME']).mkdir()
    root = Path(__file__).resolve().parents[2]
    env['PYTHONPATH'] = str(root)
    with (args.output / 'gateway.private.log').open('w') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('worker.py')), '--home', str(home),
            '--profiles', 'alpha', 'bravo'], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 120
            descriptor = {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError, RuntimeError):
                    pass
                time.sleep(.1)
            if descriptor.get('state') != 'ready':
                raise RuntimeError('GATEWAY_BOOT_FAILED')
            result = asyncio.run(exercise(home, descriptor))
            (args.output / 'proof.json').write_text(json.dumps({'phase': 'complete', 'runtimes': result,
                'hermesProcesses': 1, 'modelReceipts': model.receipts, 'realModelCalls': False}, indent=2))
            print(json.dumps(result))
        finally:
            (args.output / 'model-receipts.json').write_text(json.dumps(model.receipts, indent=2))
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=10)
            model.shutdown(); model.server_close()


if __name__ == '__main__':
    main()
