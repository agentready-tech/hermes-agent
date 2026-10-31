"""Node firewall must preserve local Kubernetes bootstrap traffic (real Linux namespace)."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


LOOPBACK_PROBE = r'''
import http.server, socket, subprocess, threading, urllib.request
from deploy.kubernetes.host_guard import apply
subprocess.run(['ip','link','set','lo','up'],check=True,timeout=5)
apply('10.216.0.1','10.216.0.2','10.216.0.1','10.240.0.0/24')
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'local-api-ok')
    def log_message(self,*args):
        pass
class IPv6Server(http.server.HTTPServer):
    address_family=socket.AF_INET6
for family, host, port in [(http.server.HTTPServer,'127.0.0.1',6443),(IPv6Server,'::1',10250)]:
    server=family((host,port),Handler)
    server.timeout=5
    worker=threading.Thread(target=server.handle_request,daemon=True)
    worker.start()
    address=('['+host+']') if ':' in host else host
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('http://'+address+':'+str(port)+'/readyz',timeout=3) as response:
            assert response.status==200 and response.read()==b'local-api-ok'
    finally:
        worker.join(timeout=6); server.server_close()
print('IPv4/IPv6 loopback remains reachable under node guard')
'''


@pytest.mark.platforms("linux")
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="requires a disposable root network namespace")
def test_loopback_api_and_kubelet_survive_node_guard():
    capability = subprocess.run(["unshare", "--net", "true"], capture_output=True, check=False, timeout=5)
    if capability.returncode:
        pytest.skip("CAP_SYS_ADMIN is required to create the disposable network namespace")
    subprocess.run(["unshare", "--net", sys.executable, "-c", LOOPBACK_PROBE],
                   cwd=Path(__file__).resolve().parents[3], check=True, timeout=30, capture_output=True)
