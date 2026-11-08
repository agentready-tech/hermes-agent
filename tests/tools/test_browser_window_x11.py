"""Window metadata/input decisions with a synthetic X server API; real Chrome belongs to Linux CI."""
import contextlib
import ctypes as C

import pytest

from tools.bot_desktop import browser_window_x11 as window


class FakeX:
    root=1
    def __init__(self):
        self.active=20;self.timestamp=None;self.pid=123;self.focus=True;self.hit=True;self.trace=[]
        self.geometry={'width':390,'height':700}
    @contextlib.contextmanager
    def grabbed(self):
        self.trace.append('grab')
        try:yield
        finally:self.trace.append('ungrab')
    def property(self,which,name):
        if name=='_NET_ACTIVE_WINDOW':return [self.active] if self.timestamp is None else [self.active,self.timestamp]
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


@pytest.mark.parametrize("timestamp",[None,0,0xf1234567])
def test_same_verified_browser_popup_needs_no_fake_transient_and_foreign_wm_pid_is_insufficient(target,timestamp):
    x=FakeX();x.timestamp=timestamp;x.active=21
    assert window.discover('/own/profile',target,x11=x)['window']==21
    x.pid=456
    with pytest.raises(window.WindowUnavailable,match='target_unavailable'):
        window.discover('/own/profile',target,x11=x)


@pytest.mark.parametrize("timestamp",[None,0,0xf1234567])
def test_focus_and_pointer_hit_are_checked_inside_server_grab_before_input(target,timestamp):
    x=FakeX();x.timestamp=timestamp;x.focus=False
    key=b'\x04\x01\0\0'+ord('a').to_bytes(4,'big')
    with pytest.raises(window.WindowUnavailable,match='focus_changed'):window.inject(x,'/own/profile',target,key)
    assert x.trace==['grab','ungrab']
    x=FakeX();x.timestamp=timestamp;x.hit=False
    with pytest.raises(window.WindowUnavailable,match='pointer_occluded'):
        window.inject(x,'/own/profile',target,b'\x05\x01\x00\x04\x00\x04')
    assert x.trace==['grab','hit','ungrab']
    x=FakeX();x.timestamp=timestamp;window.inject(x,'/own/profile',target,key)
    assert x.trace==['grab','key','ungrab']


@pytest.mark.parametrize("timestamp",[None,0,0xf1234567])
def test_own_popup_and_resize_have_explicit_view_change_codes_without_forwarding_input(target,timestamp):
    x=FakeX();x.timestamp=timestamp;x.active=21
    with pytest.raises(window.WindowUnavailable,match='target_changed'):window.check_view(x,'/own/profile',target)
    x.active=20;x.geometry={'width':500,'height':700}
    with pytest.raises(window.WindowUnavailable,match='geometry_changed'):window.check_view(x,'/own/profile',target)
    assert not {'button','key'}&set(x.trace)


def test_changed_browser_generation_rejects_before_input(target):
    x=FakeX();other={**target,'generation':'b'*64}
    with pytest.raises(window.WindowUnavailable,match='owner_changed'):
        window.inject(x,'/own/profile',other,b'\x05\x01\0\0\0\0')
    assert x.trace==['grab','ungrab']



def test_xfwm4_timestamp_is_not_an_alternate_active_window(target):
    x=FakeX();x.active=20;x.timestamp=21
    assert window.discover('/own/profile',x11=x)['window']==20
    x.active=99;x.timestamp=20
    with pytest.raises(window.WindowUnavailable,match='target_unavailable'):
        window.discover('/own/profile',x11=x)


@pytest.mark.parametrize('descriptor',[[],[0],[1],[-1],[0x100000000],[True],['20'],
    [20,-1],[20,0x100000000],[20,True],[20,'timestamp'],[20,0,21],(20,0)])
def test_active_descriptor_rejects_invalid_cardinal_shape_before_input(target,descriptor):
    x=FakeX();original=x.property
    x.property=lambda which,name:descriptor if name=='_NET_ACTIVE_WINDOW' else original(which,name)
    with pytest.raises(window.WindowUnavailable,match='target_unavailable'):
        window.discover('/own/profile',x11=x)
    with pytest.raises(window.WindowUnavailable,match='focus_changed'):
        window.inject(x,'/own/profile',target,b'\x04\x01\0\0'+ord('a').to_bytes(4,'big'))
    assert not {'key','button'}&set(x.trace)


def test_xfwm4_descriptor_does_not_authorize_same_uid_foreign_xres_client(target):
    x=FakeX();x.timestamp=0xf1234567;x.pid=456
    # The spoofable _NET_WM_PID still says123; exact XRes PID must remain mandatory.
    with pytest.raises(window.WindowUnavailable,match='target_unavailable'):
        window.discover('/own/profile',x11=x)
    with pytest.raises(window.WindowUnavailable,match='owner_changed'):
        window.inject(x,'/own/profile',target,b'\x04\x01\0\0'+ord('a').to_bytes(4,'big'))
    assert not {'key','button'}&set(x.trace)



def test_xlib_format32_decoder_preserves_xfwm4_window_and_timestamp():
    # Xlib expands each format32 item to unsigned long on LP64; two items
    # are the WM's real payload, not padding or a second active window.
    x=window.X11.__new__(window.X11);x.display=None;x.root=1
    payload=(C.c_ulong*2)(20,0xf1234567)
    x.atom=lambda *args:33
    x.free=lambda value:None
    def get_property(display,which,atom,offset,length,delete,requested,actual,fmt,count,remaining,data):
        C.cast(actual,C.POINTER(C.c_ulong))[0]=33
        C.cast(fmt,C.POINTER(C.c_int))[0]=32
        C.cast(count,C.POINTER(C.c_ulong))[0]=2
        C.cast(remaining,C.POINTER(C.c_ulong))[0]=0
        C.cast(data,C.POINTER(C.c_void_p))[0]=C.cast(payload,C.c_void_p)
        return 0
    x.get_property=get_property
    assert x.property(x.root,'_NET_ACTIVE_WINDOW')==[20,0xf1234567]
    assert window._active_window(x)==20
