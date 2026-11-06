"""Disposable identical per-agent state for the native one-vs-two process comparison."""
import json
from pathlib import Path

from experiments.shared_runtime.model_peer import NAMES


def initialize(root, url, sandboxes):
    root.mkdir(parents=True, exist_ok=True)
    for name, home in [('control', root), *[(n, root / 'profiles' / n) for n in NAMES]]:
        home.mkdir(parents=True, exist_ok=True)
        (home / 'workspace').mkdir(exist_ok=True)
        config = {'_config_version': 12, 'gateway': {'multiplex_profiles': name == 'control'},
                  'model': {'provider': 'custom', 'default': 'fixture-' + name, 'base_url': url,
                            'key_env': 'OPENAI_API_KEY', 'context_length': 131072},
                  'auxiliary': {'title_generation': {'enabled': False}},
                  'platform_toolsets': {'cli': ['terminal', 'delegation', 'memory'],
                                        'telegram': ['terminal', 'delegation', 'memory']},
                  'memory': {'memory_enabled': True, 'user_profile_enabled': True},
                  'delegation': {'max_concurrent_children': 1, 'worktree_isolation': False}}
        if name in NAMES:
            config['platforms'] = {'telegram': {'enabled': True, 'token': 'fixture-' + name}}
            config['terminal'] = {'backend': 'docker', 'docker_container': sandboxes[name],
                                  'cwd': '/workspace', 'docker_env': {'HERMES_HOME': '/agent'},
                                  'persistent_shell': True, 'worktree_isolation': False}
        (home / 'config.yaml').write_text(json.dumps(config))
        (home / '.env').write_text('OPENAI_API_KEY=fixture-' + name + '\nTELEGRAM_ALLOWED_USERS=9001\n'
                                  'ISOLATION_TOKEN=' + name + '-canary\n')


def project_snapshot(home):
    import sqlite3
    with sqlite3.connect('file:' + str(home / 'state.db') + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        projects = [dict(row) for row in db.execute('SELECT subagent_id,session_id,project_key FROM delegated_projects')]
        messages = [dict(row) for row in db.execute('SELECT subagent_id,state,published FROM delegated_project_messages')]
        count = db.execute('SELECT count(*) FROM messages').fetchone()[0]
    return {'projects': projects, 'messages': messages, 'historyCount': count}
