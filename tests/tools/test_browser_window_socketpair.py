"""Real own-child socketpair framing; no X server, Chromium, account or network."""
import json
import os
import sys

from tools.bot_desktop import browser_window_stream as stream


def test_inetd_child_receives_one_bidirectional_socket_not_separate_exec_pipes(tmp_path):
    script=tmp_path/'synthetic-rfb'
    script.write_text('#!'+sys.executable+'\nimport os\nos.write(0,b"SOCKET")\nvalue=os.read(0,2)\nos.write(0,value.upper())\n')
    script.chmod(0o700)
    channel,child=stream.capture_child({'window':22},executable=str(script))
    try:
        channel.settimeout(2)
        assert channel.recv(6)==b'SOCKET'
        channel.sendall(b'ok');assert channel.recv(2)==b'OK'
        assert child.wait(timeout=2)==0
    finally:
        channel.close();stream.stop_child(child)


def test_request_reader_leaves_following_rfb_bytes_unconsumed():
    reader,writer=os.pipe()
    try:
        value={'profile':'/own/profile','target':{'window':22}}
        os.write(writer,(json.dumps(value)+'\n').encode()+b'RFB')
        assert stream.read_request(reader)==value
        assert os.read(reader,3)==b'RFB'
    finally:os.close(reader);os.close(writer)
