"""Panel screen and provider metadata reuse a live gateway, without standalone UI startup."""
import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_gateway_api_serves_panel_auth_screen_and_subscription_metadata(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.run_bootstrap import _start_gateway_start_control_socket
    from gateway.run_runtime import initialize_gateway_runtime, publish_gateway_runtime_ready, start_gateway_runtime_api
    from gateway.runtime_ownership import process_ownership
    from hermes_cli import web_server
    from hermes_cli.web_routers import display, oauth
    from hermes_constants import get_hermes_home
    from hermes_runtime.standalone.panel_gateway_auth import GatewayPanelAuth
    from tools.bot_desktop import runtime
    from websockets.asyncio.client import connect

    home = get_hermes_home()
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'standalone': True}, 'terminal': {'backend': 'local'},
        'autonomous': {'dashboard': {'enabled': False}},
        'model': {'provider': 'custom', 'default': 'fixture-no-network', 'base_url': 'http://127.0.0.1:1/v1'}}))

    @asynccontextmanager
    async def forbidden_dashboard(app):
        pytest.fail('standalone dashboard lifespan was started')
        yield

    async def screen_fixture(ws, info):
        assert info['hermes_home'] == str(home)
        await ws.send_bytes(b'RFB fixture')
        await ws.close()

    monkeypatch.setattr('hermes_cli.web_server_app.standalone_lifespan', forbidden_dashboard)
    monkeypatch.setattr(oauth, '_build_oauth_catalog', list)
    monkeypatch.setattr(runtime, 'rfb_socket_path', lambda: home / 'fixture.sock')
    monkeypatch.setattr(runtime, 'status', lambda: runtime.DesktopStatus(
        profile='default', supported=True, installed=True, missing=[], running=True,
        pid=None, display=None, socket=None, geometry='1024x768', install_command=None, browser=None))
    monkeypatch.setattr(display, '_bridge', screen_fixture)
    process_ownership.reserve([home])
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=False))
    try:
        await initialize_gateway_runtime(runner)
        runner.session_control_server = await _start_gateway_start_control_socket(runner)
        assert runner.session_control_server is not None
        await start_gateway_runtime_api(runner)
        assert await runner.start()
        publish_gateway_runtime_ready(runner)
        auth = GatewayPanelAuth(home)
        assert await asyncio.to_thread(auth.authenticated_request, '/api/providers/oauth') == {'providers': []}
        target, options = await asyncio.to_thread(auth.rpc_connection)
        assert options['subprotocols'][0] == 'hermes-gateway-v1'
        async with connect(target, proxy=None, **options) as ws:
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'display.observe', 'params': {}}))
            async with asyncio.timeout(20):
                while True:
                    result = json.loads(await ws.recv())
                    if result.get('id') == 1:
                        break
            assert 'error' not in result
            screen_url = await asyncio.to_thread(auth.display_url, result['result']['ticket'])
            async with connect(screen_url, proxy=None) as screen:
                assert await screen.recv() == b'RFB fixture'
        assert web_server.app.state.gateway_runner is runner
    finally:
        await runner.stop()
        if getattr(runner, 'session_control_server', None):
            await runner.session_control_server.stop()
        process_ownership.close()


def test_missing_gateway_never_falls_back_to_dashboard_or_starts_owner(tmp_path, monkeypatch):
    from hermes_cli.gateway_runtime import GatewayDiscovery
    from hermes_runtime.standalone.panel_gateway_auth import GatewayPanelAuth
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint',
                        lambda home: GatewayDiscovery('missing'))
    auth = GatewayPanelAuth(tmp_path)
    with pytest.raises(ValueError, match='not ready'):
        auth.rpc_connection()
    with pytest.raises(ValueError, match='not ready'):
        auth.authenticated_request('/api/providers/oauth')
