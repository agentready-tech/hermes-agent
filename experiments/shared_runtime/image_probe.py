"""Actual local Linux gateway experiment; never a production operator."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import platform
import shutil
import statistics
import sys

from experiments.shared_runtime.image_client import Clients, Workers, wait_for
from experiments.shared_runtime.image_config import initialize, project_snapshot
from experiments.shared_runtime.metrics import measure
from experiments.shared_runtime.model_peer import NAMES, start_model


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def status(output, phase, **extra):
    write(output / 'status.json', {'phase': phase, 'productionTouched': False, **extra})


def memory_proof(root):
    result = {}
    for name in NAMES:
        file = root / 'profiles' / name / 'memories' / 'MEMORY.md'
        text = file.read_text()
        other = 'bravo' if name == 'alpha' else 'alpha'
        if name not in text or other in text:
            raise RuntimeError('MEMORY_OWNERSHIP:' + name)
        result[name] = {'ownFact': True, 'foreignFactAbsent': True}
    return result


def sandbox_proof(output, phase):
    for name in NAMES:
        value = json.loads((output / 'workspaces' / name / (phase + '.json')).read_text())
        if value != dict(owner=name, ownSecret=True, foreignHomeAbsent=True):
            raise RuntimeError('SANDBOX_OWNERSHIP:' + name)


def children_complete(root):
    snapshots = {n: project_snapshot(root / 'profiles' / n) for n in NAMES}
    return all(s['messages'] and all(m['state'] == 'completed' and m['published'] for m in s['messages'])
               for s in snapshots.values())


async def exercise(workers, server, output):
    measures, proof = {}, {}
    async with Clients(workers) as clients:
        measures['idle'] = measure(workers.pids)
        await clients.both('WARM')
        await clients.both('MEMORY')
        proof['memory'] = memory_proof(workers.root)
        await clients.both('SANDBOX')
        sandbox_proof(output, 'parent')
        proof['sandbox'] = {'actualNativeTerminalBoth': True, 'ownMountsAndSyntheticSecrets': True}
        measures['warm'] = measure(workers.pids)
        write(workers.output / 'partial-proof.json', {'phase': 'warm', 'measurements': measures, 'proof': proof})
        await clients.both('SPAWN')
        await asyncio.to_thread(wait_for, lambda: workers.check_alive() and all(e.is_set() for e in server.child_entered.values()),
                                timeout=60, what='NATIVE_CHILDREN_RUNNING')
        sandbox_proof(output, 'child')
        await clients.both('FOLLOWUP')
        measures['heldChildren'] = measure(workers.pids)
        proof['parentsNonblockingWhileBothChildrenRunning'] = True
        write(workers.output / 'partial-proof.json', {'phase': 'held_children', 'measurements': measures, 'proof': proof})
        snapshots = {n: project_snapshot(workers.root / 'profiles' / n) for n in NAMES}
        ids = {n: s['projects'][0]['subagent_id'] for n, s in snapshots.items()}
        if ids['alpha'] == ids['bravo']:
            raise RuntimeError('CHILD_ID_COLLISION')
        server.expected_ids = ids
        await clients.both('ROSTER')
        if not all(r['onlyOwnChild'] for r in server.roster.values()) or len(server.roster) != 2:
            raise RuntimeError('FOREIGN_CHILD_ROSTER')
        for operation in ('read', 'stop', 'steer'):
            await asyncio.gather(*(clients.turn(n, 'FOREIGN_' + n + ':' + operation + ' ' +
                ids['bravo' if n == 'alpha' else 'alpha']) for n in NAMES))
        if len(server.foreign_refused) != 6 or not all(r['rejected'] for r in server.foreign_refused):
            raise RuntimeError('FOREIGN_CHILD_CONTROL_ACCEPTED')
        proof['ownOnlyListReadStop'] = True
        server.release['alpha'].set()

        def alpha_only():
            workers.check_alive()
            a = project_snapshot(workers.root / 'profiles' / 'alpha')['messages']
            b = project_snapshot(workers.root / 'profiles' / 'bravo')['messages']
            return a[0]['state'] == 'completed' and a[0]['published'] and b[0]['state'] == 'running'
        await asyncio.to_thread(wait_for, alpha_only, timeout=60, what='OWN_ONLY_ALPHA_COMPLETION')
        await clients.completion('alpha')
        server.release['bravo'].set()
        await asyncio.to_thread(wait_for, lambda: workers.check_alive() and children_complete(workers.root), timeout=60, what='CHILD_DELIVERY')
        await clients.completion('bravo')
        proof['independentPersistentChildrenAndDelivery'] = True
        proof['childIds'] = ids
        for name in NAMES:
            view = await clients.view(name)
            other = 'bravo' if name == 'alpha' else 'alpha'
            text = json.dumps(view['messages'])
            if 'CHILD_DONE_' + other in text or 'SPAWN_' + other in text:
                raise RuntimeError('FOREIGN_HISTORY:' + name)
        measures['completed'] = measure(workers.pids)
        proof['preRestart'] = {n: project_snapshot(workers.root / 'profiles' / n) for n in NAMES}
        write(workers.output / 'partial-proof.json', {'phase': 'completed_before_restart', 'measurements': measures, 'proof': proof})
    return {'measurements': measures, 'proof': proof}


async def restart_proof(workers, before):
    async with Clients(workers) as clients:
        for name in NAMES:
            snapshot = project_snapshot(workers.root / 'profiles' / name)
            if snapshot['projects'] != before['preRestart'][name]['projects']:
                raise RuntimeError('RESTART_CHILD_IDENTITY:' + name)
            if snapshot['historyCount'] < before['preRestart'][name]['historyCount']:
                raise RuntimeError('RESTART_HISTORY_LOSS:' + name)
        await clients.both('RESTORED')
        counts = {n: (await clients.view(n))['message_count'] for n in NAMES}
        await asyncio.gather(*(clients.turn(n, 'SEND_' + n + ':' + before['childIds'][n]) for n in NAMES))
        await asyncio.to_thread(wait_for, lambda: workers.check_alive() and children_complete(workers.root), timeout=60, what='REUSED_CHILD_DELIVERY')
        await asyncio.gather(*(clients.completion(n, after=counts[n]) for n in NAMES))
    return {'fullProcessRestart': True, 'sameHomeSessionId': True, 'persistentChildIdsUnchanged': True,
            'existingChildrenAcceptedNewWork': True, 'memory': memory_proof(workers.root)}


def reset_owned(root, workspaces):
    # Keep mounted directory inodes. Only contents created by this experiment are disposable.
    for directory in [root, *[root / 'profiles' / n for n in NAMES], *[workspaces / n for n in NAMES]]:
        directory.mkdir(parents=True, exist_ok=True)
        for path in directory.iterdir():
            if path == root / 'profiles':
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/probe'))
    args = parser.parse_args()
    output, root = args.output, args.output / 'home'
    plan = json.loads((output / 'launch.json').read_text())
    server, trials = start_model(), []
    workers = None
    try:
        for index, mode in enumerate(('shared', 'separate', 'separate', 'shared')):
            status(output, 'exercising_native_runtimes', trial=index, mode=mode)
            reset_owned(root, output / 'workspaces')
            for event in [*server.release.values(), *server.child_entered.values()]:
                event.clear()
            server.roster.clear()
            server.foreign_refused.clear()
            initialize(root, f'http://127.0.0.1:{server.server_port}/v1', plan['sandboxes'], plan['image'],
                       standalone=mode == 'separate')
            trialdir = output / f'trial-{index}-{mode}'
            trialdir.mkdir()
            workers = Workers(root, mode, trialdir).start()
            result = asyncio.run(exercise(workers, server, output))
            workers.stop()
            workers = Workers(root, mode, trialdir).start()
            result['restart'] = asyncio.run(restart_proof(workers, result['proof']))
            workers.stop()
            write(trialdir / 'proof.json', result)
            trials.append(dict(mode=mode, **result))
        summary = {}
        for phase in trials[0]['measurements']:
            by_mode = {mode: statistics.median(t['measurements'][phase]['medianBytes']['Pss']
                        for t in trials if t['mode'] == mode) for mode in ('shared', 'separate')}
            summary[phase] = dict(pssBytes=by_mode, savedBytes=by_mode['separate'] - by_mode['shared'],
                                 savedPercent=100 * (1 - by_mode['shared'] / by_mode['separate']))
        if not server.receipts or not all(r['ownCredential'] for r in server.receipts):
            raise RuntimeError('PROVIDER_CREDENTIAL_OWNERSHIP')
        write(output / 'scripted-proof.json', {'phase': 'complete', 'architecture': platform.machine(),
            'python': sys.version.split()[0], 'image': plan['image'], 'sourceRevision': plan['sourceRevision'],
            'memory': summary, 'trials': trials, 'modelReceipts': server.receipts,
            'providerFixtureExcludedFromPss': True, 'guiBrowserIncluded': False,
            'paidProviderProbeComplete': False, 'productionTouched': False})
        status(output, 'scripted_benchmark_complete', paidProviderProbeComplete=False)
    except Exception as exc:
        status(output, 'failed', error=type(exc).__name__, detail=str(exc)[:300], completedTrials=len(trials))
        raise
    finally:
        for event in server.release.values():
            event.set()
        if workers:
            workers.stop()
        write(output / 'model-receipts.json', server.receipts)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
