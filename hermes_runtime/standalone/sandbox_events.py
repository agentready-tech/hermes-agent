"""Receipt bridge for the sandbox's watchdog; no host memory sampling/signals."""
import hashlib
from contextlib import closing
import json
import logging
from pathlib import Path
import signal
import sqlite3
import threading

logger = logging.getLogger(__name__)


def require_owner(home):
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters.hermes import managed_home
    if Path(home).resolve() != managed_home().resolve() or Path(home).resolve() != get_hermes_home().resolve():
        raise RuntimeError('Sandbox browser event profile mismatch')


def affected_tasks(home):
    tasks = set()
    for path in (Path(home) / 'cache/browser-tabs').glob('*.json'):
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rows, dict):
            tasks.update(row['task'] for row in rows.values()
                         if isinstance(row, dict) and isinstance(row.get('task'), str))
    return sorted(tasks)[:100]


def affected_children(home, tasks):
    if not tasks:
        return []
    database = (Path(home).resolve() / 'state.db').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(database, uri=True)) as connection:
        marks = ','.join('?' for _ in tasks)
        return [row[0] for row in connection.execute(
            f"SELECT id FROM sessions WHERE id IN ({marks}) AND source IN ('tool','subagent') ORDER BY id", tasks)]


def admit(home, event):
    from hermes_runtime.adapters.state import main_gateway_origin, current_home_session_id
    from hermes_runtime.events import event_prompt
    from hermes_runtime.standalone.events import enqueue
    from tools.delegate_project_recovery import publish_wakeup

    require_owner(home)
    tasks = affected_tasks(home)
    children = affected_children(home, tasks)
    prompt = ('The sandbox browser memory watchdog stopped or requested shutdown of Chrome. '
              'Its desktop profile and files remain on disk. Hermes and the sandbox were not restarted. '
              'Affected task IDs: ' + (', '.join(tasks) if tasks else 'unknown') + '. '
              'Known affected children: ' + (', '.join(children) if children else 'unknown') + '. '
              'Human tabs may also have been interrupted. '
              'A browser action may have an unknown outcome. Check submit/send/payment outcomes before retrying. '
              'Chrome may start only on a new browser/CUA request after memory pressure falls. '
              'Watchdog outcome: ' + event['phase'] + '.')
    source = 'browser_memory_guard'
    metadata = {'affected_tasks': tasks or ['unknown'], 'affected_children': children or ['unknown'],
                'main_session_id': current_home_session_id(), 'memory': event['sample'], 'forced': event['forced']}
    origin = main_gateway_origin()
    if not origin:
        return enqueue(prompt, source, event['id'], metadata)
    key = hashlib.sha256(json.dumps([source, event['id']]).encode()).hexdigest()
    publish_wakeup('main-browser-memory-' + key, current_home_session_id(), origin,
                   event_prompt(prompt, source, event['id'], metadata),
                   goal='Handle the sandbox browser memory event in the existing main conversation')
    return {'accepted': True}


def poll(home):
    from tools.bot_desktop.browser_memory_guard import configuration, marker, receive
    from tools.environments.network_transport import sandbox_environment

    require_owner(home)
    if configuration(home)['enabled'] or marker().exists():
        environment = sandbox_environment()
        if environment is None:
            raise RuntimeError('Sandbox watchdog requires the agent sandbox, never host execution')
        return receive(environment, lambda event: admit(home, event))
    return 0


async def watch(home):
    """Share the existing native screen connector process; no second web backend."""
    import asyncio
    while True:
        try:
            await asyncio.to_thread(poll, home)
        except Exception as error:  # health: allow BLE001 -- exec transport failures can contain credentials
            logger.warning('Sandbox watchdog receipt bridge deferred (%s)', type(error).__name__)
        await asyncio.sleep(10)


def worker(home):
    from hermes_runtime.standalone.config import activate_home

    activate_home(home)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    while not stopped.is_set():
        try:
            poll(home)
        except Exception as error:  # health: allow BLE001 -- exec transport failures can contain credentials
            logger.warning('Sandbox watchdog receipt bridge deferred (%s)', type(error).__name__)
        stopped.wait(10)
