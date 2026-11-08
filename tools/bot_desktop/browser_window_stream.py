"""Per-view own-window RFB socketpair; no listener, daemon, profile or browser is created."""
from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys

if __package__:
    from . import browser_window_x11 as window
    from .rfb_filter import RfbClientFilter
else:
    import browser_window_x11 as window
    from rfb_filter import RfbClientFilter


EXIT_CODES = {'browser_window_closed':42,'browser_window_minimized':43,
              'browser_window_focus_changed':44,'browser_window_geometry_changed':45,
              'browser_window_target_changed':46,'browser_window_pointer_occluded':47,
              'browser_window_owner_changed':48,'browser_window_key_unsupported':49}


def read_request(descriptor=0):
    raw=bytearray()
    while len(raw)<=16384:
        piece=os.read(descriptor,1)
        if not piece:raise ValueError('incomplete request')
        if piece==b'\n':break
        raw+=piece
    request=json.loads(raw)
    if len(raw)>16384 or not isinstance(request,dict) or set(request)!={'profile','target'}:
        raise ValueError('invalid request')
    return request


def write_all(descriptor, data):
    view=memoryview(data)
    while view:
        count=os.write(descriptor,view)
        if count<=0:raise BrokenPipeError
        view=view[count:]


def capture_child(target, *, executable=None):
    binary=executable or '/usr/bin/x11vnc'
    if not binary:raise window.WindowUnavailable('browser_window_capture_unavailable')
    parent,child=socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)
    try:
        # stdin is a duplex socket, not an exec pipe: upstream inetd duplicates fd0.
        process=subprocess.Popen([binary,'-norc','-id',str(target['window']),'-inetd','-q',
            '-rfbversion','3.8','-no6','-noremote','-nocmds','-nosel','-viewonly','-xcomposite'],
            stdin=child,stdout=child,stderr=subprocess.DEVNULL,close_fds=True,start_new_session=True)
    except BaseException:
        parent.close();raise
    finally:child.close()
    return parent,process


def stop_child(process):
    if process.poll() is None:
        os.killpg(process.pid,signal.SIGTERM)  # windows-footgun: ok — fixed image-owned Linux helper.
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=2)  # windows-footgun: ok — fixed image-owned Linux helper.


def serve(request, *, input_fd=0, output_fd=1, x11=None, executable=None, on_ready=lambda:None):
    x=x11 or window.X11();channel=process=None
    try:
        with x.grabbed():
            geometry=window.verify(x,request['profile'],request['target']);x.redirect(request['target']['window'])
            if not x.focus_is(request['target']['window']):raise window.WindowUnavailable('browser_window_focus_changed')
        channel,process=capture_child(request['target'],executable=executable)
        write_all(output_fd,(json.dumps({'ok':True,'geometry':geometry})+'\n').encode())
        on_ready()
        decoder=RfbClientFilter(lambda:True,input_handler=lambda msg:window.inject(x,request['profile'],request['target'],msg))
        while process.poll() is None:
            with x.grabbed():window.check_view(x,request['profile'],request['target'])
            ready,_,_=select.select([input_fd,channel],[],[],1)
            if input_fd in ready:
                data=os.read(input_fd,65536)
                if not data:return
                safe=decoder.feed(data)
                if safe:channel.sendall(safe)
            if channel in ready:
                data=channel.recv(65536)
                if not data:return
                write_all(output_fd,data)
    finally:
        if channel:channel.close()
        if process:stop_child(process)
        if x11 is None:x.close()


def main():
    started=[]
    try:
        request=read_request();serve(request,on_ready=lambda:started.append(True))
    except (OSError,ValueError,RuntimeError,KeyError,TypeError) as error:
        code=str(error) if isinstance(error,window.WindowUnavailable) else 'browser_window_unavailable'
        if not started:write_all(1,b'{"ok":false,"code":"browser_window_unavailable"}\n')
        raise SystemExit(EXIT_CODES.get(code,50)) from None


if __name__=='__main__':main()
