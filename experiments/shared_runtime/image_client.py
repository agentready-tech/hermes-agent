"""Authenticated native gateway client and bounded functional synchronization."""
import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from experiments.shared_runtime.probe import control, rpc
from experiments.shared_runtime.model_peer import NAMES


def wait_for(predicate, *, timeout=90, what='condition'):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(what)
        threading.Event().wait(.1)


class Workers:
    def __init__(self, root, mode, output):
        self.root, self.mode, self.output = root, mode, output
        self.processes, self.descriptors = [], {}

    def start(self):
        homes = [('shared', self.root)] if self.mode == 'shared' else [(n, self.root / 'profiles' / n) for n in NAMES]
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'DOCKER_HOST') if k in os.environ}
        env.update(HOME='/probe/user', PYTHONPATH='/opt/hermes', PYTHONUNBUFFERED='1')
        Path(env['HOME']).mkdir(exist_ok=True)
        for name, home in homes:
            command = [sys.executable, '/opt/hermes/experiments/shared_runtime/worker.py', '--home', str(home)]
            if name == 'shared':
                command += ['--profiles', *NAMES]
            env['HERMES_HOME'] = str(home)
            with (self.output / (name + '.private.log')).open('w') as log:
                process = subprocess.Popen(command, cwd='/opt/hermes', env=env, stdout=log, stderr=subprocess.STDOUT)
            self.processes.append(process)

            def ready():
                if process.poll() is not None:
                    raise RuntimeError('GATEWAY_EXIT:' + name)
                try:
                    descriptor = control(home, 'identify')
                except (OSError, ValueError, RuntimeError):
                    return False
                if descriptor.get('state') == 'ready':
                    self.descriptors[name] = descriptor
                    return True
                return False
            wait_for(ready, timeout=120, what='GATEWAY_BOOT:' + name)
        return self

    def stop(self):
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self.processes.clear()

    @property
    def pids(self):
        return [p.pid for p in self.processes]


class Clients:
    def __init__(self, workers):
        self.workers, self.stack, self.peers = workers, AsyncExitStack(), {}

    async def __aenter__(self):
        from websockets.asyncio.client import connect
        for name in NAMES:
            key = 'shared' if self.workers.mode == 'shared' else name
            home = self.workers.root if key == 'shared' else self.workers.root / 'profiles' / name
            descriptor = self.workers.descriptors[key]
            grant = control(home, 'session-ticket', {'profile_id': str(self.workers.root / 'profiles' / name),
                            'instance_id': descriptor['instance_id'], 'purpose': 'interactive'})
            self.peers[name] = await self.stack.enter_async_context(connect(
                descriptor['api_origin'].replace('http:', 'ws:') + '/api/ws',
                subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + grant['ticket']], max_size=8 * 1024 * 1024))
            await self.view(name)
        return self

    async def __aexit__(self, *_):
        await self.stack.aclose()

    async def view(self, name):
        return await rpc(self.peers[name], 'session.resume', session_id='agentready_home',
                         **({'profile': name} if self.workers.mode == 'shared' else {}))

    async def submit(self, name, marker):
        return await rpc(self.peers[name], 'prompt.submit', session_id='agentready_home',
                         input_id=marker, text=marker,
                         **({'profile': name} if self.workers.mode == 'shared' else {}))

    async def turn(self, name, marker):
        before = await self.view(name)
        await self.submit(name, marker)
        async with asyncio.timeout(90):
            while True:
                view = await self.view(name)
                if (view['message_count'] > before['message_count'] and not view['running']
                        and all(row['status'] == 'terminal' for row in view['pending'])):
                    return view
                await asyncio.sleep(.1)

    async def both(self, phase):
        return dict(zip(NAMES, await asyncio.gather(*(self.turn(n, phase + '_' + n) for n in NAMES))))
