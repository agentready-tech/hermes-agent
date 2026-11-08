"""Actual native RPC/private receipt/lease decisions under temporary owning profiles."""
import asyncio
import json
from unittest.mock import Mock

import pytest

from tools.bot_desktop import browser_window as window, browser_login, lease, runtime
from hermes_cli.web_routers import display


@pytest.fixture
def own(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));lease._reset_for_tests();display._viewer_streams.clear()
    yield tmp_path
    display._viewer_streams.clear();lease._reset_for_tests()


def saved_window(viewer,epoch,nonce='a'*48):
    target={'pid':123,'generation':'b'*64,'window':20,'geometry':{'width':390,'height':700}}
    window._save(window._path('browser-window.json'),{'viewer_hash':window._identity(viewer),
        'lease_epoch':epoch,'target':target,'restore_target':target,
        'original_geometry':{'width':900,'height':700},'nonce':nonce})


def test_actual_rpc_browser_intent_never_probes_or_mints_a_desktop_ticket(own,monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(runtime,'rfb_socket_path',Mock(side_effect=AssertionError('not a Desktop observe')))
    def call(params):return server.handle_request({'jsonrpc':'2.0','id':1,'method':'display.observe','params':params})
    result=call({'surface':'google-login','viewport':{'width':390,'height':700}})['result']
    assert result['surface']=='browser-intent' and set(result)=={'surface','viewer_id','lease'}
    for params in ({'surface':'unknown'},{'surface':'google-login','viewport':{'width':True,'height':700}},
                   {'surface':'google-login','profile':'foreign'},{'surface':'browser-window','viewer_id':'invented'}):
        assert 'error' in call(params)


def test_stale_window_nonce_or_newer_pending_stream_cannot_restore_or_release(own,monkeypatch):
    held=lease.acquire('synthetic-viewer');saved_window('synthetic-viewer',held.epoch,nonce='b'*48)
    restore=Mock();monkeypatch.setattr(window,'_request',restore)
    assert window.finish('synthetic-viewer',held.epoch,'a'*48,commit=lambda action:action()).holder==lease.HUMAN
    assert window.finish('synthetic-viewer',held.epoch,'b'*48,commit=lambda action:False).holder==lease.HUMAN
    restore.assert_not_called()
    assert window.finish('synthetic-viewer',held.epoch,'b'*48,commit=lambda action:action()).holder==lease.AGENT
    restore.assert_called_once()


def test_old_failed_open_cannot_release_a_new_open_pending_on_the_same_viewer_epoch(own,monkeypatch):
    async def scenario():
        held=lease.acquire('synthetic-viewer');saved_window('synthetic-viewer',held.epoch)
        older_ready,newer_ready,fail_old,fail_new=[asyncio.Event() for _ in range(4)]
        info={'surface':'browser-window','hermes_home':str(own),'viewer_id':'synthetic-viewer',
              'lease_epoch':held.epoch,'window_nonce':'a'*48}
        calls=[]
        async def open_window(_):
            index=len(calls);calls.append(index)
            (older_ready if index==0 else newer_ready).set()
            await (fail_old if index==0 else fail_new).wait()
            raise OSError('synthetic window refusal')
        class Ws:
            async def close(self,**kwargs):pass
        monkeypatch.setattr(display,'_open_browser_rfb',open_window)
        restore=Mock();monkeypatch.setattr(window,'_request',restore)
        old=asyncio.create_task(display._bridge(Ws(),info));await older_ready.wait()
        new=asyncio.create_task(display._bridge(Ws(),info));await newer_ready.wait()
        fail_old.set();await old
        assert lease.get(str(own)).holder==lease.HUMAN
        restore.assert_not_called()
        fail_new.set();await new
        assert lease.get(str(own)).holder==lease.AGENT
    asyncio.run(scenario())


def test_prepared_human_intent_expires_on_existing_loop_even_without_window_rfb(own,monkeypatch):
    from tui_gateway.transport import bind_transport,reset_transport
    now=[1000.0];monkeypatch.setattr(lease.time,'time',lambda:now[0])
    held=lease.acquire('synthetic-viewer');scheduled=[]
    class Loop:
        def is_closed(self):return False
        def call_soon_threadsafe(self,fn,*args):scheduled.append((fn,args))
    class Transport:
        closed=False;_loop=Loop()
    token=bind_transport(Transport())
    try:browser_login._schedule_intent('synthetic-viewer',held.epoch)
    finally:reset_transport(token)
    assert len(scheduled)==1
    async def elapsed(delay):now[0]+=delay
    async def expire():
        monkeypatch.setattr(display.asyncio,'sleep',elapsed)
        fn,args=scheduled[0];fn(*args)
        await asyncio.gather(*display._expiry_tasks)
    asyncio.run(expire())
    assert json.loads((own/'bot-desktop/lease.json').read_text())['holder']=='agent'
