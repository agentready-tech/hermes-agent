"""Private own-browser window custody for the human settings channel; never a model tool."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import stat
import subprocess
import time

from hermes_constants import get_hermes_home
from tools.bot_desktop import lease, runtime

CAPABILITY='google-browser-window-v1'


class WindowUnavailable(RuntimeError):
    pass


def _path(name):
    return get_hermes_home()/'bot-desktop'/name


def _identity(viewer):
    return hashlib.sha256(viewer.encode()).hexdigest()


def _read(path):
    uid = getattr(os, 'getuid', None)
    if not callable(uid):
        raise WindowUnavailable('browser_window_unsupported_platform')
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as stream:
            info=os.fstat(stream.fileno());raw=stream.read(16385)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=uid()
                or info.st_mode&0o077 or len(raw)>16384):raise ValueError
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError
        return value
    except (OSError,ValueError,TypeError):
        raise WindowUnavailable('browser_window_receipt_unavailable') from None


def _save(path, value):
    from utils import atomic_write_text
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_write_text(path,json.dumps(value),mode=0o600)


def validate_viewport(viewport):
    if viewport is None:return None
    if (not isinstance(viewport,dict) or set(viewport)!={'width','height'}
            or any(type(value) is not int or not 240<=value<=4096 for value in viewport.values())):
        raise WindowUnavailable('browser_window_invalid_viewport')
    return dict(viewport)


def prepare(viewer, viewport=None):
    viewport=validate_viewport(viewport);lease.get();result=[]
    def prepared(current):
        if current.holder==lease.HUMAN:raise WindowUnavailable('browser_login_control_changed')
        _save(_path('browser-window-intent.json'),{'viewer_hash':_identity(viewer),'viewport':viewport})
        result.append({'surface':'browser-intent','viewer_id':viewer,'lease':lease.public_view(current)})
        return False
    lease._transition(None,prepared)
    return result[0]


def intent(viewer):
    value=_read(_path('browser-window-intent.json'))
    if set(value)!={'viewer_hash','viewport'} or value['viewer_hash']!=_identity(viewer):
        raise WindowUnavailable('browser_window_intent_unavailable')
    return validate_viewport(value['viewport'])


def _sandbox():
    from tools.bot_desktop import sandbox_host
    env=runtime._owned_sandbox_env()
    if env is None or sandbox_host._user_for(env)!=sandbox_host.DESKTOP_USER:
        raise WindowUnavailable('browser_window_environment_unavailable')
    return env


def available():
    if not callable(getattr(os, 'getuid', None)):
        return False
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    try:
        env=_sandbox()
        result=streams.run_in(env,_python("import ctypes,os;assert os.access('/usr/bin/x11vnc',os.X_OK);[ctypes.CDLL(n) for n in ('libX11.so.6','libXRes.so.1','libXtst.so.6','libXcomposite.so.1')]"),
            user=sandbox_host.DESKTOP_USER,timeout=10)
        return result.returncode==0 and bool(_helper_dir(env))
    except (OSError,ValueError,RuntimeError):return False


_HELPER_ROOT='/usr/local/lib/agentsee/browser-window'
_PYTHON='/usr/bin/python3'


def _python(program, environment=None):
    environment=environment or {"PATH":"/usr/bin:/bin","HOME":"/home/pn","LANG":"C.UTF-8"}
    allowed={"PATH","HOME","USER","LOGNAME","LANG","LC_ALL","DISPLAY","XAUTHORITY","DBUS_SESSION_BUS_ADDRESS","XDG_RUNTIME_DIR"}
    if set(environment)-allowed:raise WindowUnavailable('browser_window_environment_unavailable')
    return ['/usr/bin/env','-i',*[f'{key}={value}' for key,value in sorted(environment.items())],_PYTHON,'-I','-B','-c',program]


def isolated_command(module, environment=None):
    if module not in {'browser_launch','browser_window_x11','browser_window_stream'}:
        raise WindowUnavailable('browser_window_helper_unavailable')
    bootstrap=f"import sys;sys.path.insert(0,{_HELPER_ROOT!r});import {module};{module}.main()"
    return _python(bootstrap,environment)


def desktop_environment(env):
    from tools.bot_desktop import sandbox_host
    published=runtime.published_env()
    display=published.get('DISPLAY','');auth=published.get('XAUTHORITY','')
    if (not re.fullmatch(r':\d{1,4}(?:\.\d)?',display) or not auth.startswith('/')
            or '..' in Path(auth).parts or '\x00' in auth):
        raise WindowUnavailable('browser_window_environment_unavailable')
    result={'PATH':'/usr/bin:/bin','HOME':sandbox_host.desktop_home(env),'USER':'pn','LOGNAME':'pn',
            'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','DISPLAY':display,'XAUTHORITY':auth}
    for key in ('DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR'):
        value=published.get(key)
        if value and isinstance(value,str) and len(value)<=4096 and '\x00' not in value:result[key]=value
    return result

_CHECK_HELPERS = """import ctypes,hashlib,json,os,re,stat,sys
from pathlib import Path
root=Path('/usr/local/lib/agentsee/browser-window')
expected=json.load(sys.stdin)
def owned(path,directory=False):
 s=path.lstat()
 if s.st_uid!=0 or s.st_mode&0o022 or path.is_symlink() or (not directory and (not stat.S_ISREG(s.st_mode) or s.st_nlink!=1)) or (directory and not stat.S_ISDIR(s.st_mode)):raise ValueError()
for path in [*reversed(root.parents),root]:owned(path,True)
p=root/'manifest.json';owned(p)
raw=p.read_bytes()
if len(raw)>16384:raise ValueError()
m=json.loads(raw)
if set(m)!={'schema','abi','sourceRepository','sourceTag','sourceRevision','files'} or type(m['schema']) is not int or m['schema']!=1 or m['abi']!='google-browser-window-v1' or m['files']!=expected:raise ValueError()
if any(not isinstance(m[k],str) or not 1<=len(m[k])<=256 for k in ('sourceRepository','sourceTag','sourceRevision')):raise ValueError()
if not re.fullmatch('[a-f0-9]{40}',m['sourceRevision']):raise ValueError()
if {p.name for p in root.iterdir()}!=set(expected)|{'manifest.json'}:raise ValueError()
owned(Path('/usr/bin/x11vnc'))
for name,sha in expected.items():
 p=root/name;owned(p)
 if hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError()
for name in ('libX11.so.6','libXRes.so.1','libXtst.so.6','libXcomposite.so.1'):ctypes.CDLL(name)
print('verified')
"""


def _helper_dir(env):
    from tools.bot_desktop import browser_instance, browser_launch, browser_window_x11, browser_window_stream, rfb_filter, sandbox_host
    from tools.environments import streams
    sources={Path(module.__file__).name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
             for module in (browser_instance,browser_launch,browser_window_x11,browser_window_stream,rfb_filter)}
    result=streams.run_in(env,_python(_CHECK_HELPERS),stdin=json.dumps(sources).encode(),
        user=sandbox_host.DESKTOP_USER,timeout=10)
    if result.returncode or result.stdout.strip()!=b'verified':raise WindowUnavailable('browser_window_helper_unavailable')
    return _HELPER_ROOT


def open_google():
    from tools.bot_desktop import sandbox_host
    env=_sandbox()
    return sandbox_host.open_google(env,launch_helper=_helper_dir(env)+'/browser_launch.py')


def _request(operation, target=None, viewport=None):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    env=_sandbox();request={'operation':operation,'profile':sandbox_host.browser_profile_dir(env)}
    if target is not None:request['target']=target
    if viewport is not None:request['viewport']=viewport
    _helper_dir(env)
    try:
        result=streams.run_in(env,isolated_command('browser_window_x11',desktop_environment(env)),stdin=json.dumps(request).encode(),
            child_env={},user=sandbox_host.DESKTOP_USER,timeout=15)
    except (OSError,subprocess.SubprocessError):raise WindowUnavailable('browser_window_unavailable') from None
    try:value=json.loads(result.stdout)
    except (ValueError,TypeError):raise WindowUnavailable('browser_window_unavailable') from None
    if result.returncode or not isinstance(value,dict) or value.get('ok') is not True:
        raise WindowUnavailable('browser_window_unavailable')
    return value['target']


def _held(viewer, epoch, action):
    result=[]
    def guarded(current):
        deadline=lease._deadline(current)
        if (current.holder!=lease.HUMAN or current.viewer_id!=viewer or current.epoch!=epoch
                or deadline is None or time.time()>=deadline):
            raise WindowUnavailable('browser_login_control_changed')
        result.append(action())
        return False
    lease._transition(None,guarded)
    return result[0]


def qualify(viewer, epoch):
    viewport=intent(viewer)
    def qualified():
        target=_request('discover');original=dict(target['geometry']);initial=dict(target)
        if viewport:target=_request('resize',target,viewport)
        value={'viewer_hash':_identity(viewer),'lease_epoch':epoch,'target':target,
               'restore_target':target if viewport else None,'original_geometry':original,
               'nonce':secrets.token_hex(24)}
        _save(_path('browser-window.json'),value)
        return initial
    _held(viewer,epoch,qualified)


def _qualified(viewer, epoch, nonce=None):
    value=_read(_path('browser-window.json'))
    if (set(value)!={'viewer_hash','lease_epoch','target','restore_target','original_geometry','nonce'}
            or value['viewer_hash']!=_identity(viewer) or value['lease_epoch']!=epoch
            or (nonce is not None and value['nonce']!=nonce)):
        raise WindowUnavailable('browser_window_intent_unavailable')
    return value


def ticket(viewer, epoch, *, resume=False):
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    def qualified():
        value=_qualified(viewer,epoch)
        if resume:
            value['target']=_request('discover',value['target']);value['nonce']=secrets.token_hex(24)
            _save(_path('browser-window.json'),value)
        else:_request('verify',value['target'])
        token=mint_ticket(user_id='display:'+viewer,provider='bot-desktop',extra={
            'hermes_home':str(get_hermes_home()),'viewer_id':viewer,'surface':'browser-window',
            'lease_epoch':epoch,'window_nonce':value['nonce']})
        return {'surface':'browser-window','viewer_id':viewer,'ticket':token,'path':'/api/display/ws',
                'geometry':value['target']['geometry']}
    result=_held(viewer,epoch,qualified)
    return {**result,'lease':lease.public_view(lease.get())}


def resume(viewer):
    current=lease.get()
    return ticket(viewer,current.epoch,resume=True)


def restore(viewer):
    current=lease.get()
    if current.holder!=lease.HUMAN or current.viewer_id!=viewer:return False
    try:
        def resize_back():
            value=_qualified(viewer,current.epoch)
            if value['restore_target'] is None:return False
            _request('resize',value['restore_target'],value['original_geometry'])
            value['restore_target']=None;_save(_path('browser-window.json'),value)
            return True
        return _held(viewer,current.epoch,resize_back)
    except WindowUnavailable:return False  # Lost ownership/changed geometry never grants permission to repair it.


def finish(viewer, epoch, nonce, *, commit):
    """Lease transition and receipt nonce are one decision, fenced to the current stream generation."""
    def release_if_current(current):
        if current.holder!=lease.HUMAN or current.viewer_id!=viewer or current.epoch!=epoch:return False
        try:value=_qualified(viewer,epoch,nonce)
        except WindowUnavailable:return False
        if not commit(lambda:True):return False
        if value['restore_target'] is not None and (deadline:=lease._deadline(current)) is not None and time.time()<deadline:
            try:_request('resize',value['restore_target'],value['original_geometry'])
            except WindowUnavailable:pass  # Changed/dead windows are left alone; control must still return.
        def release():
            current.holder,current.viewer_id,current.since,current.reason=lease.AGENT,None,time.time(),''
            current.expires_at=None
            return True
        return commit(release)
    return lease._transition(None,release_if_current)


def open_stream(viewer, epoch, nonce):
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    value=_held(viewer,epoch,lambda:_qualified(viewer,epoch,nonce))
    env=_sandbox();request={'profile':sandbox_host.browser_profile_dir(env),'target':value['target']}
    _helper_dir(env)
    relay=streams.open_stream(env,isolated_command('browser_window_stream',desktop_environment(env)),
        child_env={},user=sandbox_host.DESKTOP_USER)
    try:
        relay.stdin.write((json.dumps(request)+'\n').encode());relay.stdin.flush()
    except BaseException:
        relay.kill();raise
    return relay,value['target']['geometry']
