"""Window metadata/input decisions with a synthetic X server API; real Chrome belongs to Linux CI."""
import contextlib

import pytest

from tools.bot_desktop import browser_window_x11 as window


class FakeX:
    root=1
    def __init__(self):
        self.active=20;self.pid=123;self.focus=True;self.hit=True;self.trace=[]
        self.geometry={'width':390,'height':700}
    @contextlib.contextmanager
    def grabbed(self):
        self.trace.append('grab')
        try:yield
        finally:self.trace.append('ungrab')
    def property(self,which,name):
        if name=='_NET_ACTIVE_WINDOW':return [self.active]
        if name in ('_NET_CLIENT_LIST_STACKING','_NET_CLIENT_LIST'):return [20,21]
        if name=='_NET_WM_PID':return [123]  # Deliberately spoofable: never consulted for authority.
        return []
    def client_pid(self,which):return self.pid
    def attributes(self,which):return dict(self.geometry)
    def focus_is(self,which):return self.focus
    def pointer_is(self,*args):self.trace.append('hit');return self.hit
    def key_event(self,*args):self.trace.append('key')
    def fake_button(self,*args):self.trace.append('button')
    display=None;buttons=0


@pytest.fixture
def target(monkeypatch):
    value={'pid':123,'generation':'a'*64,'window':20,'geometry':{'width':390,'height':700}}
    def owner(profile,expected=None):
        if expected and expected['generation']!=value['generation']:raise window.WindowUnavailable('browser_window_owner_changed')
        return {key:value[key] for key in ('pid','generation')}
    monkeypatch.setattr(window,'owner',owner)
    return value


def test_same_verified_browser_popup_needs_no_fake_transient_and_foreign_wm_pid_is_insufficient(target):
    x=FakeX();x.active=21
    assert window.discover('/own/profile',target,x11=x)['window']==21
    x.pid=456
    with pytest.raises(window.WindowUnavailable,match='target_unavailable'):
        window.discover('/own/profile',target,x11=x)


def test_focus_and_pointer_hit_are_checked_inside_server_grab_before_input(target):
    x=FakeX();x.focus=False
    key=b'\x04\x01\0\0'+ord('a').to_bytes(4,'big')
    with pytest.raises(window.WindowUnavailable,match='focus_changed'):window.inject(x,'/own/profile',target,key)
    assert x.trace==['grab','ungrab']
    x=FakeX();x.hit=False
    with pytest.raises(window.WindowUnavailable,match='pointer_occluded'):
        window.inject(x,'/own/profile',target,b'\x05\x01\x00\x04\x00\x04')
    assert x.trace==['grab','hit','ungrab']
    x=FakeX();window.inject(x,'/own/profile',target,key)
    assert x.trace==['grab','key','ungrab']


def test_own_popup_and_resize_have_explicit_view_change_codes_without_forwarding_input(target):
    x=FakeX();x.active=21
    with pytest.raises(window.WindowUnavailable,match='target_changed'):window.check_view(x,'/own/profile',target)
    x.active=20;x.geometry={'width':500,'height':700}
    with pytest.raises(window.WindowUnavailable,match='geometry_changed'):window.check_view(x,'/own/profile',target)
    assert not {'button','key'}&set(x.trace)


def test_changed_browser_generation_rejects_before_input(target):
    x=FakeX();other={**target,'generation':'b'*64}
    with pytest.raises(window.WindowUnavailable,match='owner_changed'):
        window.inject(x,'/own/profile',other,b'\x05\x01\0\0\0\0')
    assert x.trace==['grab','ungrab']
