"""The scripted model must request real tools and retain isolated mounted directories."""
import json
import urllib.request

from experiments.shared_runtime.image_probe import reset_owned
from experiments.shared_runtime.image_config import initialize
from experiments.shared_runtime.model_peer import start_model


def test_scripted_provider_streams_real_tool_calls_with_distinct_credentials():
    server = start_model()
    try:
        for name in ('alpha', 'bravo'):
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                data=json.dumps(dict(model='fixture-' + name, stream=True,
                    messages=[dict(role='user', content='SANDBOX_' + name)])).encode(),
                headers={'Authorization': 'Bearer fixture-' + name, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=3) as response:
                data = response.read().decode()
            chunk = json.loads(data.split('\n')[0].removeprefix('data: '))
            tool = chunk['choices'][0]['delta']['tool_calls'][0]
            assert tool['function']['name'] == 'terminal'
            assert '/agent' in json.loads(tool['function']['arguments'])['command']
        assert all(r['ownCredential'] for r in server.receipts)
    finally:
        server.shutdown()
        server.server_close()


def test_disposable_state_reset_preserves_all_mount_root_inodes(tmp_path):
    root, workspaces = tmp_path / 'home', tmp_path / 'workspaces'
    reset_owned(root, workspaces)
    directories = [root, *[root / 'profiles' / n for n in ('alpha', 'bravo')],
                   *[workspaces / n for n in ('alpha', 'bravo')]]
    inodes = {d: d.stat().st_ino for d in directories}
    for d in directories:
        (d / 'obsolete').write_text('fixture')
    reset_owned(root, workspaces)
    assert {d: d.stat().st_ino for d in directories} == inodes
    assert all(not (d / 'obsolete').exists() for d in directories)


def test_generated_profiles_resolve_the_actual_sandbox_image(tmp_path, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from tools.terminal_scope import terminal_env
    root = tmp_path / '.hermes'
    image = 'fixture/runtime:exact-source-version'
    monkeypatch.setenv('HERMES_HOME', str(root))
    initialize(root, 'http://127.0.0.1:1/v1', {'alpha': 'sandbox-a', 'bravo': 'sandbox-b'}, image)
    for name in ('alpha', 'bravo'):
        with _profile_runtime_scope(root / 'profiles' / name):
            assert terminal_env('TERMINAL_DOCKER_IMAGE') == image


def test_paid_profiles_native_resolver_keeps_real_route_and_bounded_output(tmp_path, monkeypatch):
    from experiments.shared_runtime.paid_probe import configure
    from gateway.run import _profile_runtime_scope, _resolve_runtime_agent_kwargs
    from hermes_cli.config import load_config_readonly
    root = tmp_path / '.hermes'
    monkeypatch.setenv('HERMES_HOME', str(root))
    configure(root, 'sk-or-only-a-test-fixture', 'fixture/paid-model')
    for name in ('alpha', 'bravo'):
        with _profile_runtime_scope(root / 'profiles' / name):
            runtime = _resolve_runtime_agent_kwargs()
            assert runtime['base_url'] == 'https://openrouter.ai/api/v1'
            assert runtime['api_key'] == 'sk-or-only-a-test-fixture'
            assert runtime['request_overrides']['extra_body']['max_tokens'] == 128
            config = load_config_readonly()
            assert config['agent']['api_max_retries'] == 1
            assert config['platform_toolsets']['telegram'] == []


def test_local_wire_reports_native_adapter_connection_state(monkeypatch):
    import asyncio
    from experiments.shared_runtime.transport import install
    from gateway.config import Platform, PlatformConfig
    from gateway.run import GatewayRunner
    monkeypatch.setattr(GatewayRunner, '_instantiate_adapter', GatewayRunner._instantiate_adapter)
    install()
    wire = GatewayRunner._instantiate_adapter(None, Platform.TELEGRAM, PlatformConfig())

    async def exercise():
        assert not wire.is_connected
        assert await wire.connect()
        assert wire.is_connected
        await wire.disconnect()
        assert not wire.is_connected

    asyncio.run(exercise())


def test_separate_workers_load_their_own_native_session_directory(tmp_path, monkeypatch):
    from pathlib import Path
    from gateway.run import load_gateway_config_for_runner
    from gateway.run_bootstrap import _launch_home_may_multiplex
    root = tmp_path / '.hermes'
    home = root / 'profiles' / 'alpha'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    initialize(root, 'http://127.0.0.1:1/v1',
               {'alpha': 'sandbox-a', 'bravo': 'sandbox-b'}, 'fixture/runtime', standalone=True)
    config = load_gateway_config_for_runner()
    assert _launch_home_may_multiplex(config)
    assert not config.multiplex_profiles
    assert config.sessions_dir == home / 'sessions'
