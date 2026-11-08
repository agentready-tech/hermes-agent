"""Owning-profile settings boundary; no real browser/auth/provider is used."""
import json
from unittest.mock import Mock

import pytest

from tools.bot_desktop import browser_window as window, browser_login, lease, runtime
from tools.bot_desktop.rfb_filter import RfbClientFilter
from hermes_runtime.standalone.panel import screen_request
from tui_gateway.contracts.display import DisplayObserveWire


@pytest.fixture
def own(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));lease._reset_for_tests()
    yield tmp_path
    lease._reset_for_tests()


def test_intent_is_not_a_desktop_ticket_and_relay_never_strips_unknown_surface(own):
    reply=window.prepare('synthetic-viewer',{'width':390,'height':700})
    assert set(reply)=={'surface','viewer_id','lease'} and reply['surface']=='browser-intent'
    assert DisplayObserveWire.model_validate(reply)
    assert 'synthetic-viewer' not in (own/'bot-desktop/browser-window-intent.json').read_text()
    for surface in (None,'desktop','unknown'):
        params={} if surface is None else {'surface':surface}
        with pytest.raises(ValueError):
            screen_request(json.dumps({'method':'display.observe','params':params}),can_control=True,can_configure=True,browser_only=True)
    request={'method':'display.observe','params':{'surface':'google-login','viewport':{'width':390,'height':700}}}
    assert json.loads(screen_request(json.dumps(request),can_control=True,can_configure=True,browser_only=True))==request


def test_poisoned_published_environment_is_not_a_child_loader_or_interpreter_selector(own,monkeypatch):
    from tools.bot_desktop import sandbox_host
    monkeypatch.setattr(runtime,'published_env',lambda:{'DISPLAY':':20','XAUTHORITY':'/own/Xauthority',
        'LD_PRELOAD':'/synthetic/evil','LD_LIBRARY_PATH':'/synthetic/evil','PYTHONPATH':'/synthetic/evil',
        'BASH_ENV':'/synthetic/evil','PATH':'/synthetic/evil','HOME':'/synthetic/evil'})
    monkeypatch.setattr(sandbox_host,'desktop_home',lambda env:'/home/pn')
    environment=window.desktop_environment(object())
    assert not {'LD_PRELOAD','LD_LIBRARY_PATH','PYTHONPATH','BASH_ENV'}&environment.keys()
    assert environment['PATH']=='/usr/bin:/bin' and environment['HOME']=='/home/pn'
    command=window.isolated_command('browser_window_stream',environment)
    assert command[:2]==['/usr/bin/env','-i']
    assert '/usr/bin/python3' in command and '-I' in command and '-B' in command
    assert '/synthetic/evil' not in '\n'.join(command)


def test_window_input_sink_consumes_input_but_preserves_rfb_requests_and_default_desktop():
    header=b'RFB 003.008\n'+b'\x01\x01'
    key=b'\x04\x01\x00\x00'+ord('A').to_bytes(4,'big')
    update=b'\x03\x00'+b'\x00'*8
    seen=[];filter_=RfbClientFilter(lambda:True,input_handler=seen.append)
    assert filter_.feed(header+key+update)==header+update and seen==[key]
    assert RfbClientFilter(lambda:True).feed(header+key)==header+key
    assert RfbClientFilter(lambda:False,input_handler=seen.append).feed(header+key)==header
    assert seen==[key]


def test_explicit_google_action_can_create_bounded_intent_without_any_rfb(own,monkeypatch):
    window.prepare('synthetic-viewer')
    start=Mock();opened=Mock(return_value=True);qualify=Mock();expire=Mock()
    monkeypatch.setattr(runtime,'start',start);monkeypatch.setattr(window,'open_google',opened)
    monkeypatch.setattr(window,'qualify',qualify);monkeypatch.setattr(browser_login,'_schedule_intent',expire)
    monkeypatch.setattr(browser_login,'_rpc_live',lambda:True)
    def ticket(viewer,epoch,**kw):
        return {'surface':'browser-window','viewer_id':viewer,'ticket':'synthetic-ticket',
            'path':'/api/display/ws','geometry':{'width':390,'height':700},'lease':lease.public_view(lease.get())}
    monkeypatch.setattr(window,'ticket',ticket)
    result=browser_login.open_google('synthetic-viewer',0)
    assert result['surface']=='browser-window' and result['opened']
    held=lease.get();assert held.holder==lease.HUMAN and held.expires_at is not None
    expire.assert_called_once_with('synthetic-viewer',held.epoch)
    assert browser_login.open_google('synthetic-viewer',0)['opened']
    opened.assert_called_once();qualify.assert_called_once();start.assert_called_once()


def test_non_posix_host_does_not_advertise_or_read_private_window_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(window.os, 'getuid', None)
    monkeypatch.setattr(window, '_sandbox', Mock(side_effect=AssertionError('No remote probe on unsupported host')))
    assert window.available() is False
    with pytest.raises(window.WindowUnavailable, match='unsupported_platform'):
        window._read(tmp_path / 'never-read')
