"""Install the sandbox's watchdog and transfer its receipts to native Hermes.

Only the configuration/event bridge runs here. Memory sampling and every
Chrome signal run inside the selected sandbox as its desktop user.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from hermes_constants import get_hermes_home

INSTALLER = '/usr/local/lib/agentsee/install_browser_watchdog.py'


def configuration(home=None):
    from hermes_runtime.standalone.config import read_config
    value = (read_config(home or get_hermes_home()).get('browser') or {}).get('memory_guard') or {}
    if type(value.get('enabled', False)) is not bool:
        raise ValueError('browser.memory_guard.enabled must be true or false')
    threshold = value.get('threshold_mib', 2048)
    if type(threshold) is not int or threshold < 256:
        raise ValueError('browser.memory_guard.threshold_mib must be an integer >=256')
    return {'enabled': value.get('enabled', False), 'threshold_bytes': threshold * 1024 * 1024}


def marker():
    return get_hermes_home() / 'cache' / 'browser-watchdog.json'


def owner_identity(env):
    # Use the durable managed sandbox binding across Kubernetes and Firecracker.
    target = getattr(env, 'kubernetes_identity', {})
    stable = {key: target.get(key) for key in ('namespace', 'pod', 'container')}
    home = get_hermes_home().resolve()
    profile = home.name
    if home.parent == Path('/root/.hermes/profiles'):
        if profile == 'mike':
            namespace = 'agentsee-mike'
        elif re.fullmatch(r'env_[a-f0-9]{32}', profile):
            namespace = 'agentsee-a-' + profile[4:]
        else:
            namespace = None
        binding = {'namespace': namespace, 'pod': 'sandbox-0', 'container': 'sandbox'}
        if namespace and getattr(env, 'firecracker_identity', None):
            stable = binding
        if namespace and stable == binding:
            home = Path('/root/.hermes')
    return hashlib.sha256(json.dumps([str(home), stable], sort_keys=True).encode()).hexdigest()


def install(env):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    from utils import atomic_write_text

    config = configuration()
    if not config['enabled'] and not marker().exists():
        return None
    chrome = sandbox_host.chromium_executable(env)
    if not chrome:
        raise RuntimeError('Sandbox browser watchdog requires its own Chrome executable')
    state = sandbox_host.desktop_home(env).rstrip('/') + '/.hermes/browser-watchdog'
    config.update(owner=owner_identity(env),
                  chrome_executable=chrome, chromium_sandbox_enabled=not sandbox_host.chromium_sandbox_bypass(env))
    data = json.dumps({'state': state, 'config': config}).encode()
    result = streams.run_in(env, ['python3', INSTALLER], stdin=data, user=sandbox_host._user_for(env), timeout=20)
    if result.returncode:
        raise RuntimeError('Sandbox image lacks a working browser watchdog; browser execution refused')
    installed = json.loads(result.stdout)
    if installed:
        marker().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(marker(), json.dumps({'installed': True}), mode=0o600)
    return installed


def command(env, installed, action, *arguments):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    result = streams.run_in(env, ['python3', installed['script'], action, '--state', installed['state'], *arguments],
                            user=sandbox_host._user_for(env), timeout=15)
    if result.returncode:
        raise RuntimeError('BROWSER_MEMORY_PRESSURE: sandbox Chrome stopped or lacks memory headroom. '
                           'Do not retry submit/send/payment actions without checking their outcome.')
    return result.stdout


def executable(env, fallback):
    installed = install(env)
    return installed['executable'] if installed else fallback


def before_tool(env):
    installed = install(env)
    if installed:
        command(env, installed, 'permit')


def receive(env, admit):
    installed = install(env)
    if not installed:
        return 0
    events = json.loads(command(env, installed, 'events'))
    for event in events:
        receipt = admit(event)
        if not isinstance(receipt, dict) or not (receipt.get('accepted') is True or receipt.get('message_id')):
            raise RuntimeError('Sandbox browser incident has no durable main admission receipt')
        command(env, installed, 'ack', '--event-id', event['id'])
    return len(events)


def tool_preflight_error():
    if not configuration()['enabled']:
        return None
    from tools.environments.network_transport import sandbox_environment
    try:
        environment = sandbox_environment()
        if environment is None:
            raise RuntimeError('Browser watchdog requires the configured sandbox')
        before_tool(environment)
    except (RuntimeError, OSError):
        return ('BROWSER_MEMORY_PRESSURE: sandbox Chrome cannot run safely. '
                'Wait for memory headroom; check interrupted submit/send/payment outcomes before retrying.')
    return None
