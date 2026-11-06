"""Two bounded real-provider turns through the actual shared native gateway."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3

from experiments.shared_runtime.image_client import Clients, Workers
from experiments.shared_runtime.image_config import initialize
from experiments.shared_runtime.model_peer import NAMES


def configure(root, key, model):
    initialize(root, 'https://openrouter.ai/api/v1', dict.fromkeys(NAMES, 'unused'), 'unused')
    for home in [root, *[root / 'profiles' / n for n in NAMES]]:
        config = json.loads((home / 'config.yaml').read_text())
        config['model'].update(provider='prototype-openrouter', default=model)
        config['providers'] = {'prototype-openrouter': {
            'base_url': 'https://openrouter.ai/api/v1', 'key_env': 'OPENAI_API_KEY',
            'api_mode': 'chat_completions', 'request_timeout_seconds': 45,
            'extra_body': {'max_tokens': 128, 'reasoning': {'enabled': False}}}}
        config['agent'] = {'max_turns': 2, 'api_max_retries': 1, 'run_budget_seconds': 60}
        config['platform_toolsets'] = {'telegram': [], 'cli': []}
        config['compression'] = {'enabled': False}
        (home / 'config.yaml').write_text(json.dumps(config))
        (home / '.env').write_text('OPENAI_API_KEY=' + key + '\nTELEGRAM_ALLOWED_USERS=9001\n')


def usage(home):
    with sqlite3.connect('file:' + str(home / 'state.db') + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT SUM(api_call_count),SUM(input_tokens),SUM(output_tokens) '
                         'FROM session_model_usage').fetchone()
    result = dict(zip(('apiCalls', 'inputTokens', 'outputTokens'), (int(v or 0) for v in row)))
    if not 1 <= result['apiCalls'] <= 2 or result['outputTokens'] <= 0:
        raise RuntimeError('PAID_USAGE_NOT_BOUNDED')
    return result


async def exercise(workers):
    async with Clients(workers) as clients:
        views = await asyncio.gather(*(clients.turn(n, 'Reply with exactly PAID_' + n + ' and nothing else.')
                                       for n in NAMES))
        proof = {}
        for name, view in zip(NAMES, views):
            answers = [m.get('content', '') for m in view['messages'] if m.get('role') == 'assistant']
            other = 'bravo' if name == 'alpha' else 'alpha'
            if not any('PAID_' + name in str(a) for a in answers) or 'PAID_' + other in json.dumps(view['messages']):
                raise RuntimeError('PAID_REPLY_OWNERSHIP:' + name)
            proof[name] = {'ownReply': True, 'foreignHistoryAbsent': True,
                           **usage(workers.root / 'profiles' / name)}
        return proof


def main():
    os.umask(0o077)
    output = Path('/probe')
    plan = json.loads((output / 'launch.json').read_text())
    key = (output / 'provider-key.private').read_text().strip()
    configure(output / 'home', key, plan['model'])
    workers = Workers(output / 'home', 'shared', output)
    try:
        workers.start()
        proof = asyncio.run(exercise(workers))
        result = {'phase': 'complete', 'sourceRevision': plan['sourceRevision'], 'image': plan['image'],
                  'model': plan['model'], 'runtimeProcessCount': len(workers.pids), 'profiles': proof,
                  'actualNativeGateway': True, 'realProvider': 'openrouter', 'productionTouched': False}
        (output / 'paid-proof.json').write_text(json.dumps(result, indent=2))
    except Exception as exc:
        # Provider errors may contain response bodies. Keep details only in the private log.
        (output / 'paid-status.json').write_text(json.dumps({'phase': 'failed', 'error': type(exc).__name__}))
        raise
    finally:
        workers.stop()


if __name__ == '__main__':
    main()
