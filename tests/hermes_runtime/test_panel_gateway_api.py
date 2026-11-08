"""Panel screen and provider metadata reuse a live gateway, without standalone UI startup."""
import asyncio
import json

import pytest


async def _rpc_reply(socket, request_id):
    async with asyncio.timeout(20):
        while True:
            reply = json.loads(await socket.recv())
            if reply.get('id') == request_id:
                return reply


@pytest.mark.asyncio
async def test_gateway_api_serves_panel_auth_screen_and_subscription_metadata(tmp_path, monkeypatch):
    home = tmp_path / 'own-gateway'
    assert home.resolve().parent == tmp_path.resolve()
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
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

    assert get_hermes_home().resolve() == home.resolve()
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'standalone': True}, 'terminal': {'backend': 'local'},
        'autonomous': {'dashboard': {'enabled': False}},
        'model': {'provider': 'custom', 'default': 'fixture-no-network', 'base_url': 'http://127.0.0.1:1/v1'}}))

    @asynccontextmanager
    async def forbidden_dashboard(app):
        pytest.fail('standalone dashboard lifespan was started')
        yield

    from tools.bot_desktop import browser_window as window, lease
    seen = []
    monkeypatch.setattr(window,'open_google',lambda:seen.append(str(get_hermes_home())) or True)
    monkeypatch.setattr(runtime,'start',lambda:None)
    monkeypatch.setattr(window,'_request',lambda operation,target=None,viewport=None:target or
        {'pid':123,'generation':'a'*64,'window':20,'geometry':{'width':390,'height':700}})
    async def screen_fixture(ws, info):
        assert info['hermes_home'] == str(home) and info['surface']=='browser-window'
        assert 'window' not in info and 'pid' not in info
        from hermes_constants import hermes_home_key
        key = (hermes_home_key(home), info['viewer_id'])
        display._viewer_streams[key] = display._ViewerStreams(active=1)
        try:
            await ws.send_bytes(b'RFB fixture')
            await ws.receive()
        finally:
            display._viewer_streams.pop(key, None)
            lease.release(info['viewer_id'], profile_key=str(home))

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
        from hermes_runtime.standalone.panel_skills import SkillsRead
        from hermes_runtime.skill_inventory import CAPABILITY
        personal = home / 'skills' / 'own-example'
        personal.mkdir(parents=True, exist_ok=True)
        own_text = '---\nname: own-example\ndescription: Own workflow.\n---\nOWN_PROFILE_BODY\n'
        (personal / 'SKILL.md').write_text(own_text)
        foreign = tmp_path / 'skills-foreign-ambient'
        foreign_skill = foreign / 'skills' / 'own-example'
        foreign_skill.mkdir(parents=True)
        (foreign_skill / 'SKILL.md').write_text(own_text.replace('OWN_PROFILE_BODY', 'FOREIGN_PROFILE_BODY'))
        reader = SkillsRead(home, auth)
        with monkeypatch.context() as foreign_context:
            foreign_context.setenv('HERMES_HOME', str(foreign))
            listed = await reader.handle('skills.library.list', {})
            assert listed['capability'] == CAPABILITY
            assert any(row['name'] == 'own-example' for row in listed['skills'])
            detail = await reader.handle('skills.library.read', {'name': 'own-example'})
            assert detail['content'] == own_text
            assert 'FOREIGN_PROFILE_BODY' not in json.dumps(detail)
            assert str(home) not in json.dumps(detail)
        from urllib.error import HTTPError
        for path in ('/api/skills/library/personal?home=other',
                     '/api/skills/library/personal/content?name=own-example&filePath=secret',
                     '/api/skills/library/personal/content?name=own-example&name=other'):
            with pytest.raises(HTTPError) as denied:
                await asyncio.to_thread(auth.authenticated_request, path)
            assert denied.value.code == 400
        with pytest.raises(HTTPError) as unsafe_name:
            await asyncio.to_thread(auth.authenticated_request, '/api/skills/library/personal/content?name=../other')
        assert unsafe_name.value.code == 400
        system_rows = await reader.handle('skills.system.list', {}, can_read_system=True)
        assert all(row['origin'] == 'system' for row in system_rows['skills'])
        system_detail = await reader.handle('skills.system.read', {'name': 'agentsee-google-browser-login'}, can_read_system=True)
        assert system_detail['skill']['origin'] == 'system' and 'Google Browser Login Skill' in system_detail['content']
        target, options = await asyncio.to_thread(auth.rpc_connection)
        assert options['subprotocols'][0] == 'hermes-gateway-v1'
        async with connect(target, proxy=None, **options) as ws:
            await ws.send(json.dumps({'jsonrpc':'2.0','id':1,'method':'display.observe',
                'params':{'surface':'google-login','viewport':{'width':390,'height':700}}}))
            result=await _rpc_reply(ws,1)
            assert 'error' not in result,result
            assert set(result['result'])=={'surface','viewer_id','lease'}
            assert result['result']['surface']=='browser-intent'
            foreign=tmp_path/'foreign-ambient';foreign.mkdir()
            with monkeypatch.context() as ambient:
                ambient.setenv('HERMES_HOME',str(foreign))
                await ws.send(json.dumps({'jsonrpc':'2.0','id':2,'method':'display.browser.google_login',
                    'params':{'viewer_id':result['result']['viewer_id'],'expected_epoch':result['result']['lease']['epoch']}}))
                opened=await _rpc_reply(ws,2)
            assert 'error' not in opened,opened
            assert opened['result']['opened'] and opened['result']['surface']=='browser-window'
            assert opened['result']['lease']['holder']=='human' and seen==[str(home)]
            assert not {'pid','window','generation','display','profile_key'}&opened['result'].keys()
            assert not (foreign/'bot-desktop').exists()
            screen_url=await asyncio.to_thread(auth.display_url,opened['result']['ticket'])
            async with connect(screen_url,proxy=None) as screen:
                assert await screen.recv()==b'RFB fixture'
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
