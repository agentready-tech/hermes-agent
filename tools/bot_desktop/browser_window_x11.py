"""Own-X11 metadata and input; seeded into the existing desktop, never reads page/AX content."""
from __future__ import annotations

import contextlib
import ctypes as C
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time

if __package__:
    from . import browser_instance as instance
else:
    import browser_instance as instance


class WindowUnavailable(RuntimeError):
    pass


class Attributes(C.Structure):
    _fields_ = [('x', C.c_int), ('y', C.c_int), ('width', C.c_int), ('height', C.c_int),
                ('border_width', C.c_int), ('depth', C.c_int), ('visual', C.c_void_p), ('root', C.c_ulong),
                ('window_class', C.c_int), ('bit_gravity', C.c_int), ('win_gravity', C.c_int),
                ('backing_store', C.c_int), ('backing_planes', C.c_ulong), ('backing_pixel', C.c_ulong),
                ('save_under', C.c_int), ('colormap', C.c_ulong), ('map_installed', C.c_int),
                ('map_state', C.c_int), ('all_event_masks', C.c_long), ('your_event_mask', C.c_long),
                ('do_not_propagate_mask', C.c_long), ('override_redirect', C.c_int), ('screen', C.c_void_p)]


class ClientSpec(C.Structure):
    _fields_ = [('client', C.c_ulong), ('mask', C.c_ulong)]


class ClientValue(C.Structure):
    _fields_ = [('spec', ClientSpec), ('length', C.c_long), ('value', C.c_void_p)]


def _bind(lib, name, result, arguments):
    fn = getattr(lib, name); fn.restype = result; fn.argtypes = arguments
    return fn


class X11:
    def __init__(self):
        self.x = C.CDLL('libX11.so.6'); self.res = C.CDLL('libXRes.so.1'); self.test = C.CDLL('libXtst.so.6')
        p, u, i = C.c_void_p, C.c_ulong, C.c_int
        self.open = _bind(self.x, 'XOpenDisplay', p, [C.c_char_p])
        self.close_display = _bind(self.x, 'XCloseDisplay', i, [p])
        self.sync = _bind(self.x, 'XSync', i, [p, i])
        self.root_window = _bind(self.x, 'XDefaultRootWindow', u, [p])
        self.atom = _bind(self.x, 'XInternAtom', u, [p, C.c_char_p, i])
        self.get_property = _bind(self.x, 'XGetWindowProperty', i, [p,u,u,C.c_long,C.c_long,i,u,
            C.POINTER(u),C.POINTER(i),C.POINTER(u),C.POINTER(u),C.POINTER(p)])
        self.free = _bind(self.x, 'XFree', i, [p])
        self.get_attributes = _bind(self.x, 'XGetWindowAttributes', i, [p,u,C.POINTER(Attributes)])
        self.query_tree = _bind(self.x, 'XQueryTree', i, [p,u,C.POINTER(u),C.POINTER(u),C.POINTER(C.POINTER(u)),C.POINTER(C.c_uint)])
        self.get_focus = _bind(self.x, 'XGetInputFocus', i, [p,C.POINTER(u),C.POINTER(i)])
        self.query_pointer = _bind(self.x, 'XQueryPointer', i, [p,u,C.POINTER(u),C.POINTER(u),
            C.POINTER(i),C.POINTER(i),C.POINTER(i),C.POINTER(i),C.POINTER(C.c_uint)])
        self.resize_window = _bind(self.x,'XResizeWindow',i,[p,u,C.c_uint,C.c_uint])
        self.warp = _bind(self.x, 'XWarpPointer', i, [p,u,u,i,i,C.c_uint,C.c_uint,i,i])
        self.grab_server = _bind(self.x, 'XGrabServer', i, [p])
        self.ungrab_server = _bind(self.x, 'XUngrabServer', i, [p])
        self.display_keycodes = _bind(self.x,'XDisplayKeycodes',i,[p,C.POINTER(i),C.POINTER(i)])
        self.get_mapping = _bind(self.x,'XGetKeyboardMapping',C.POINTER(u),[p,C.c_ubyte,i,C.POINTER(i)])
        self.change_mapping = _bind(self.x,'XChangeKeyboardMapping',i,[p,i,i,C.POINTER(u),i])
        self.query_keymap = _bind(self.x,'XQueryKeymap',i,[p,p])
        self.lock_modifiers = _bind(self.x,'XkbLockModifiers',i,[p,u,u,u])
        self.keycode = _bind(self.x, 'XKeysymToKeycode', C.c_ubyte, [p,u])
        self.fake_key = _bind(self.test, 'XTestFakeKeyEvent', i, [p,C.c_uint,i,u])
        self.fake_button = _bind(self.test, 'XTestFakeButtonEvent', i, [p,C.c_uint,i,u])
        self.res_version = _bind(self.res, 'XResQueryVersion', i, [p,C.POINTER(i),C.POINTER(i)])
        self.res_ids = _bind(self.res, 'XResQueryClientIds', i, [p,C.c_long,C.POINTER(ClientSpec),C.POINTER(C.c_long),C.POINTER(C.POINTER(ClientValue))])
        self.res_pid = _bind(self.res, 'XResGetClientPid', i, [C.POINTER(ClientValue)])
        self.res_free = _bind(self.res, 'XResClientIdsDestroy', None, [C.c_long,C.POINTER(ClientValue)])
        self.display = self.open(None)
        if not self.display: raise WindowUnavailable('browser_window_display_unavailable')
        self.failed = False
        self.handler = C.CFUNCTYPE(i,p,p)(lambda *_: self._error())
        self.set_handler = _bind(self.x, 'XSetErrorHandler', p, [p])
        self.previous_handler = self.set_handler(C.cast(self.handler,p))
        self.root = self.root_window(self.display)
        self.composite = C.CDLL('libXcomposite.so.1')
        self.composite_query = _bind(self.composite,'XCompositeQueryExtension',i,[p,C.POINTER(i),C.POINTER(i)])
        self.composite_redirect = _bind(self.composite,'XCompositeRedirectWindow',None,[p,u,i])
        self.composite_pixmap = _bind(self.composite,'XCompositeNameWindowPixmap',u,[p,u])
        self.free_pixmap = _bind(self.x,'XFreePixmap',i,[p,u])
        major, minor = i(), i()
        if not self.res_version(self.display,C.byref(major),C.byref(minor)) or (major.value,minor.value)<(1,2):
            self.close(); raise WindowUnavailable('browser_window_identity_unavailable')
        self.keys = set(); self.buttons = 0; self.added_mappings = {}

    def _error(self):
        self.failed = True
        return 0

    @contextlib.contextmanager
    def grabbed(self):
        self.grab_server(self.display)
        try:
            self.sync(self.display,False)
            yield
            self.sync(self.display,False)
        finally:
            self.ungrab_server(self.display); self.sync(self.display,False)

    def close(self):
        if self.display:
            with self.grabbed():
                for code in getattr(self,'keys',()):self.fake_key(self.display,code,False,0)
                for button in range(8):
                    if getattr(self,'buttons',0)&(1<<button):self.fake_button(self.display,button+1,False,0)
                for code,(original,assigned) in getattr(self,'added_mappings',{}).items():
                    if self.mapping(code,1)[1][0]==assigned:
                        row=(C.c_ulong*len(original))(*original);self.change_mapping(self.display,code,len(original),row,1)
            self.set_handler(self.previous_handler); self.close_display(self.display); self.display=None

    def mapping(self, first=None, count=None):
        low,high=C.c_int(),C.c_int();self.display_keycodes(self.display,C.byref(low),C.byref(high))
        first=low.value if first is None else first;count=high.value-first+1 if count is None else count
        per=C.c_int();raw=self.get_mapping(self.display,first,count,C.byref(per))
        try:
            if not raw or not 1<=per.value<=32:raise WindowUnavailable('browser_window_key_unsupported')
            return first,[list(raw[index*per.value:(index+1)*per.value]) for index in range(count)]
        finally:
            if raw:self.free(raw)

    def key_plan(self, symbol):
        first,rows=self.mapping()
        for index,row in enumerate(rows):
            for level,value in enumerate(row[:2]):
                if value==symbol:return first+index,level
        if not 0<symbol<=0x0110ffff:raise WindowUnavailable('browser_window_key_unsupported')
        for index,row in enumerate(rows):
            if not any(row):
                code=first+index;assigned=[symbol]*len(row);values=(C.c_ulong*len(row))(*assigned)
                self.change_mapping(self.display,code,len(row),values,1);self.sync(self.display,False)
                self.added_mappings[code]=(row,assigned)
                return code,0
        raise WindowUnavailable('browser_window_key_unsupported')

    def modifier_state(self):
        keys=C.create_string_buffer(32);self.query_keymap(self.display,keys)
        shifts=[self.keycode(self.display,value) for value in (0xffe1,0xffe2)]
        held=[code for code in shifts if code and keys.raw[code//8]&(1<<(code%8))]
        root,child=C.c_ulong(),C.c_ulong();rx,ry,wx,wy=C.c_int(),C.c_int(),C.c_int(),C.c_int();mask=C.c_uint()
        self.query_pointer(self.display,self.root,C.byref(root),C.byref(child),C.byref(rx),C.byref(ry),C.byref(wx),C.byref(wy),C.byref(mask))
        return held,bool(mask.value&2)

    def key_event(self, symbol, down):
        code,level=self.key_plan(symbol)
        printable=symbol<0xff00 or 0x01000000<=symbol<=0x0110ffff
        held,caps=self.modifier_state() if printable and down else ([],False)
        added=self.keycode(self.display,0xffe1) if printable and down and level==1 and not held else 0
        removed=held if printable and down and level==0 else []
        try:
            if caps:self.lock_modifiers(self.display,0x100,2,0)
            for shift in removed:self.fake_key(self.display,shift,False,0)
            if added:self.fake_key(self.display,added,True,0)
            self.fake_key(self.display,code,down,0);self.sync(self.display,False)
        finally:
            if added:self.fake_key(self.display,added,False,0)
            for shift in removed:self.fake_key(self.display,shift,True,0)
            if caps:self.lock_modifiers(self.display,0x100,2,2)
        self.keys.add(code) if down else self.keys.discard(code)

    def property(self, window, name):
        actual, count, remaining = C.c_ulong(), C.c_ulong(), C.c_ulong()
        fmt, data = C.c_int(), C.c_void_p()
        self.failed=False
        status=self.get_property(self.display,window,self.atom(self.display,name.encode(),False),0,4096,
            False,0,C.byref(actual),C.byref(fmt),C.byref(count),C.byref(remaining),C.byref(data))
        try:
            if status or self.failed or remaining.value or fmt.value!=32 or count.value>4096: return []
            return list(C.cast(data,C.POINTER(C.c_ulong))[:count.value]) if data else []
        finally:
            if data:self.free(data)

    def attributes(self, window):
        value=Attributes();self.failed=False
        valid=self.get_attributes(self.display,window,C.byref(value));self.sync(self.display,False)
        if not valid or self.failed:raise WindowUnavailable('browser_window_closed')
        if value.map_state!=2:raise WindowUnavailable('browser_window_minimized')
        if not 100<=value.width<=4096 or not 100<=value.height<=4096:
            raise WindowUnavailable('browser_window_geometry_unavailable')
        return {'width':value.width,'height':value.height}

    def redirect(self, window):
        event,error=C.c_int(),C.c_int()
        if not self.composite_query(self.display,C.byref(event),C.byref(error)):
            raise WindowUnavailable('browser_window_capture_unavailable')
        self.failed=False;self.composite_redirect(self.display,window,0);self.sync(self.display,False)
        pixmap=self.composite_pixmap(self.display,window);self.sync(self.display,False)
        if self.failed or not pixmap:raise WindowUnavailable('browser_window_capture_unavailable')
        self.free_pixmap(self.display,pixmap)

    def client_pid(self, window):
        count=C.c_long();values=C.POINTER(ClientValue)();spec=ClientSpec(window,2)
        status=self.res_ids(self.display,1,C.byref(spec),C.byref(count),C.byref(values))
        try:
            if status!=0 or not 0<count.value<=16:raise WindowUnavailable('browser_window_identity_unavailable')
            pids={self.res_pid(C.byref(values[index])) for index in range(count.value)}
            if len(pids)!=1 or min(pids)<1:raise WindowUnavailable('browser_window_identity_unavailable')
            return pids.pop()
        finally:
            if values:self.res_free(count,values)

    def descendant(self, child, ancestor):
        for _ in range(32):
            if child==ancestor:return True
            if child in (0,1,self.root):return False
            root,parent=C.c_ulong(),C.c_ulong();children=C.POINTER(C.c_ulong)();count=C.c_uint()
            valid=self.query_tree(self.display,child,C.byref(root),C.byref(parent),C.byref(children),C.byref(count))
            if children:self.free(children)
            if not valid:return False
            child=parent.value
        return False

    def focus_is(self, window):
        focus,revert=C.c_ulong(),C.c_int();self.get_focus(self.display,C.byref(focus),C.byref(revert))
        return self.descendant(focus.value,window)

    def pointer_is(self, window, x, y):
        self.warp(self.display,0,window,0,0,0,0,x,y);self.sync(self.display,False)
        current=self.root
        for _ in range(32):
            root,child=C.c_ulong(),C.c_ulong();rx,ry,wx,wy=C.c_int(),C.c_int(),C.c_int(),C.c_int();mask=C.c_uint()
            if not self.query_pointer(self.display,current,C.byref(root),C.byref(child),C.byref(rx),C.byref(ry),C.byref(wx),C.byref(wy),C.byref(mask)):return False
            if not child.value:return self.descendant(current,window)
            current=child.value
        return False


def owner(profile, expected=None):
    lock=instance._lock(profile)
    if not lock:raise WindowUnavailable('browser_window_owner_unavailable')
    value=instance._process(lock['pid'])
    if not instance._matches(value,profile):raise WindowUnavailable('browser_window_owner_unavailable')
    identity=hashlib.sha256(json.dumps(instance._generation(value),separators=(',',':')).encode()).hexdigest()
    if expected is not None and (expected['pid']!=value['pid'] or expected['generation']!=identity):
        raise WindowUnavailable('browser_window_owner_changed')
    return {'pid':value['pid'],'generation':identity}


def discover(profile, previous=None, *, x11=None):
    profile=Path(profile)
    if not profile.is_absolute():raise WindowUnavailable('browser_window_owner_unavailable')
    x=x11 or X11()
    try:
        with x.grabbed():
            current=owner(profile,previous)
            active=x.property(x.root,'_NET_ACTIVE_WINDOW')
            windows=x.property(x.root,'_NET_CLIENT_LIST_STACKING') or x.property(x.root,'_NET_CLIENT_LIST')
            candidates=[w for w in windows if active==[w] and x.client_pid(w)==current['pid']]
            if len(candidates)!=1:raise WindowUnavailable('browser_window_target_unavailable')
            window=candidates[0]
            # XRes proves the same exact browser owner for second top-levels too.
            result={**current,'window':window,'geometry':x.attributes(window)}
            if not x.focus_is(window):raise WindowUnavailable('browser_window_focus_changed')
            if owner(profile,current)!=current:raise WindowUnavailable('browser_window_owner_changed')
            return result
    finally:
        if x11 is None:x.close()


def verify(x, profile, target, *, strict_geometry=True):
    if not isinstance(target,dict) or set(target)!={'pid','generation','window','geometry'}:
        raise WindowUnavailable('browser_window_target_unavailable')
    owner(Path(profile),target)
    if type(target['window']) is not int or target['window']<=1 or x.client_pid(target['window'])!=target['pid']:
        raise WindowUnavailable('browser_window_owner_changed')
    geometry=x.attributes(target['window'])
    if strict_geometry and geometry!=target['geometry']:raise WindowUnavailable('browser_window_geometry_changed')
    return geometry


def resize(profile, target, viewport, *, x11=None):
    if (not isinstance(viewport,dict) or set(viewport)!={'width','height'}
            or any(type(value) is not int or not 240<=value<=4096 for value in viewport.values())):
        raise WindowUnavailable('browser_window_invalid_viewport')
    x=x11 or X11()
    try:
        with x.grabbed():
            original=verify(x,profile,target)
            screen=x.attributes(x.root)
            width=min(screen['width'],max(360,viewport['width']))
            height=min(screen['height'],max(320,viewport['height']))
            x.resize_window(x.display,target['window'],width,height)
        # The WM receives ConfigureRequest; let that existing WM apply its minimum bounds.
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            geometry=x.attributes(target['window'])
            if geometry!=original or geometry=={'width':width,'height':height}:break
            time.sleep(0.02)
        with x.grabbed():
            verify(x,profile,target,strict_geometry=False)
            geometry=x.attributes(target['window'])
        return {**target,'geometry':geometry}
    finally:
        if x11 is None:x.close()


def check_view(x, profile, target):
    owner(Path(profile),target)
    active=x.property(x.root,'_NET_ACTIVE_WINDOW')
    if len(active)!=1:raise WindowUnavailable('browser_window_focus_changed')
    if active!=[target['window']] and x.client_pid(active[0])==target['pid']:
        x.attributes(active[0])
        raise WindowUnavailable('browser_window_target_changed')
    verify(x,profile,target)
    if not x.focus_is(target['window']):raise WindowUnavailable('browser_window_focus_changed')


def inject(x, profile, target, message):
    with x.grabbed():
        check_view(x,profile,target)
        geometry=target['geometry']
        kind=message[0]
        if kind in (4,255):
            down=bool(message[1]) if kind==4 else bool(int.from_bytes(message[2:4],'big'))
            x.key_event(int.from_bytes(message[4:8],'big'),down)
        elif kind==5:
            xx,yy=int.from_bytes(message[2:4],'big'),int.from_bytes(message[4:6],'big')
            if xx>=geometry['width'] or yy>=geometry['height'] or not x.pointer_is(target['window'],xx,yy):
                raise WindowUnavailable('browser_window_pointer_occluded')
            for button in range(8):
                bit=1<<button
                if bool(x.buttons&bit)!=bool(message[1]&bit):x.fake_button(x.display,button+1,bool(message[1]&bit),0)
            x.buttons=message[1]
        # Clipboard and client desktop resize are consumed, never applied to the shared display.


def main():
    try:
        request=json.loads(sys.stdin.buffer.read(16385))
        if set(request)-{'operation','profile','target','viewport'} or request.get('operation') not in ('discover','verify','resize'):
            raise WindowUnavailable('browser_window_invalid_request')
        if request['operation']=='discover':result=discover(request['profile'],request.get('target'))
        elif request['operation']=='resize':result=resize(request['profile'],request['target'],request['viewport'])
        else:
            x=X11()
            try:
                with x.grabbed():verify(x,request['profile'],request['target'])
                result=request['target']
            finally:x.close()
        print(json.dumps({'ok':True,'target':result}))
    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as error:
        code=str(error) if isinstance(error,WindowUnavailable) else ''
        code=code if re.fullmatch('browser_window_[a-z_]{1,80}',code) else 'browser_window_unavailable'
        print(json.dumps({'ok':False,'code':code}));raise SystemExit(1)


if __name__=='__main__':main()
