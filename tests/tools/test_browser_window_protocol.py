"""Actual existing bridges: typed own-window changes keep a bounded view-resume grant."""
import asyncio
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from tools.bot_desktop import lease
from hermes_cli.web_routers import display
from hermes_runtime.standalone import panel


@pytest.fixture
def own(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path));lease._reset_for_tests();display._viewer_streams.clear()
    yield tmp_path
    display._viewer_streams.clear();lease._reset_for_tests()


@pytest.mark.parametrize('exit_code,reason',[(45,'browser-window-geometry-changed'),(46,'browser-window-target-changed')])
def test_real_rfb_bridge_exports_only_typed_view_change_and_keeps_current_human_epoch(own,monkeypatch,exit_code,reason):
    async def scenario():
        held=lease.acquire('synthetic-viewer');codes=[]
        reader=asyncio.StreamReader();reader.feed_eof()
        class Writer:
            def close(self):pass
            async def wait_closed(self):pass
        class Relay:
            def wait(self,timeout):return exit_code
            def kill(self):pass
        class Ws:
            async def receive(self):await asyncio.Event().wait()
            async def send_bytes(self,value):pass
            async def close(self,code=1000,reason=''):codes.append((code,reason))
        async def opened(info):return reader,Writer(),Relay()
        monkeypatch.setattr(display,'_open_browser_rfb',opened)
        expire=Mock();monkeypatch.setattr(display,'_expire_disconnected_viewer',expire)
        await display._bridge(Ws(),{'hermes_home':str(own),'viewer_id':'synthetic-viewer',
            'surface':'browser-window','lease_epoch':held.epoch,'window_nonce':'a'*48})
        assert (4001,reason) in codes
        assert lease.get().holder==lease.HUMAN and lease.get().epoch==held.epoch
        expire.assert_called_once_with(str(own),'synthetic-viewer',held.epoch)
    asyncio.run(scenario())


@pytest.mark.parametrize('inbound',[False,True])
def test_reverse_native_relay_preserves_fixed_view_cue_only_from_authorized_native_direction(monkeypatch,inbound):
    async def scenario():
        reason='browser-window-target-changed';closed=[]
        class Socket:
            def __init__(self,source):self.source=source
            def __aiter__(self):return self
            async def __anext__(self):
                if self.source:raise ConnectionClosedError(Close(4001,reason),None)
                await asyncio.Event().wait()
            async def close(self,code=1000,reason=''):closed.append((self.source,code,reason))
            async def send(self,value):pass
        native=Socket(not inbound);remote=Socket(inbound)
        class Context(AbstractAsyncContextManager):
            def __init__(self,socket):self.socket=socket
            async def __aenter__(self):return self.socket
            async def __aexit__(self,*args):return False
        def connect(url,**kwargs):return Context(native if url=='native-target' else remote)
        monkeypatch.setattr('websockets.asyncio.client.connect',connect)
        auth=SimpleNamespace(gateway_owned=True,endpoint=lambda:None,display_url=lambda ticket:'native-target')
        await panel.bridge({'kind':'rfb','id':'synthetic-channel','ticket':'synthetic-ticket','googleLogin':True,
            'canControl':True,'canConfigure':True},{'url':'https://panel.invalid','environment_id':'synthetic-env'},
            1234,'synthetic-credential',auth,google_auth=auth)
        assert (False,4001,'' if inbound else reason) in closed
    asyncio.run(scenario())
