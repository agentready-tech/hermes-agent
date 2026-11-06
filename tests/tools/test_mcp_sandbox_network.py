"""Real SDK clients keep hostnames/TLS policy but connect through the native sandbox."""

import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib
import threading
from types import SimpleNamespace

import pytest

from tools.environments import network_transport as transport
from tools.thread_context import propagate_context_to_thread


@pytest.fixture
def server():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            seen.append((self.headers.get("Host"), self.headers.get("Authorization")))
            data = b'data: {"jsonrpc":"2.0","result":{}}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=propagate_context_to_thread(server.serve_forever), daemon=True)
        worker.start()
        try:
            yield server.server_port, seen
        finally:
            server.shutdown()
            worker.join(timeout=3)


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
@pytest.mark.parametrize("async_mode", [False, True])
def test_sdk_http_and_streams_use_existing_target_without_rewriting_origin(monkeypatch, server, module_name, async_mode):
    httpx = importlib.import_module(module_name)
    port, seen = server
    environment = object()
    forwarded = []
    monkeypatch.setattr(transport, "sandbox_environment", lambda: environment)
    def forward(selected, remote_port, **kwargs):
        assert selected is environment
        forwarded.append((kwargs["remote_host"], remote_port))
        return port
    monkeypatch.setattr(transport.streams, "forward_port", forward)
    url = "http://unresolvable.example.invalid:8765/mcp"
    if async_mode:
        async def run():
            async with httpx.AsyncClient(transport=transport.http_transport(httpx), trust_env=False) as client:
                async with client.stream("GET", url, headers={"Authorization": "Bearer synthetic"}) as response:
                    return b"".join([part async for part in response.aiter_bytes()])
        body = asyncio.run(run())
    else:
        with httpx.Client(transport=transport.http_transport(httpx, async_mode=False), trust_env=False) as client:
            with client.stream("GET", url, headers={"Authorization": "Bearer synthetic"}) as response:
                body = b"".join(response.iter_bytes())
    assert body.startswith(b"data:")
    assert forwarded == [("unresolvable.example.invalid", 8765)]
    assert seen == [("unresolvable.example.invalid:8765", "Bearer synthetic")]


def test_nonlocal_failure_cannot_fall_back_to_host_and_local_stays_local(monkeypatch):
    monkeypatch.setattr("tools.terminal_tool._get_env_config", lambda: {"env_type": "kubernetes"})
    monkeypatch.setattr("tools.terminal_tool.ensure_task_env", lambda *_: None)
    with pytest.raises(transport.SandboxTransportUnavailable):
        transport.sandbox_environment()
    monkeypatch.setattr("tools.terminal_tool._get_env_config", lambda: {"env_type": "local"})
    assert transport.sandbox_environment() is None


def test_profile_and_actual_target_are_part_of_connection_identity(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    identities = []
    for name in ("first", "second"):
        token = set_hermes_home_override(tmp_path / name)
        try:
            identities.append(transport.sandbox_identity(SimpleNamespace(namespace=name, pod="sandbox-0", container="sandbox",
                kubernetes_identity={"uid": name + "-existing-pod"})))
        finally:
            reset_hermes_home_override(token)
    assert identities[0] != identities[1]
    assert identities[0]["target"]["kubernetes_identity"] == {"uid": "first-existing-pod"}


def test_custom_api_actual_request_uses_same_sandbox_http_transport(monkeypatch, server):
    from tools.connectors.http_api import execute
    port, seen = server
    environment = object()
    forwarded = []
    monkeypatch.setattr(transport, "sandbox_environment", lambda: environment)
    def forward(selected, remote_port, **kwargs):
        assert selected is environment
        forwarded.append((kwargs["remote_host"], remote_port))
        return port
    monkeypatch.setattr(transport.streams, "forward_port", forward)
    spec = {"base_url": "http://unresolvable.example.invalid:8765", "operations": [
        {"name": "lookup", "method": "GET", "path": "/items", "input_schema": {"type": "object"}}]}
    result = execute(spec, "lookup", {}, {"Authorization": "Bearer synthetic"})
    assert result["status"] == 200 and forwarded == [("unresolvable.example.invalid", 8765)]
    assert seen == [("unresolvable.example.invalid:8765", "Bearer synthetic")]


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_tls_keeps_original_sni_and_certificate_validation_through_sandbox_relay(monkeypatch, tmp_path, module_name):
    from datetime import UTC, datetime, timedelta
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    httpx = importlib.import_module(module_name)
    host = "unresolvable.example.invalid"
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    names = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.set_servername_callback(lambda _socket, name, _context: names.append(name))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=propagate_context_to_thread(server.serve_forever), daemon=True)
        worker.start()
        monkeypatch.setattr(transport, "sandbox_environment", lambda: object())
        def forward(_environment, port, **kwargs):
            assert kwargs["remote_host"] == host and port == 443
            return server.server_port
        monkeypatch.setattr(transport.streams, "forward_port", forward)
        try:
            verify = ssl.create_default_context(cafile=str(cert_path))
            with httpx.Client(transport=transport.http_transport(httpx, async_mode=False, verify=verify), trust_env=False) as client:
                assert client.get("https://" + host + "/mcp").text == "ok"
            async def run():
                async with httpx.AsyncClient(transport=transport.http_transport(httpx, verify=verify), trust_env=False) as client:
                    return (await client.get("https://" + host + "/mcp")).text
            assert asyncio.run(run()) == "ok"
        finally:
            server.shutdown(); worker.join(timeout=3)
    assert names == [host, host]
