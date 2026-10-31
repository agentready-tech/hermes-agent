"""Private Docker transport restricted to one persistent sandbox configuration.

The agent receives this Unix socket, never the host daemon socket. A host-owned
policy is captured from its existing sandbox. Compose/operator alone creates
containers; this transport only permits access to the selected sandbox.
"""

import asyncio
import http.client
import json
import os
from pathlib import Path
import re
import socket
from urllib.parse import unquote, urlsplit

LIMIT = 16 * 1024 * 1024
VERSION = re.compile(r"^/v[0-9]+\.[0-9]+(?=/)")


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, *, timeout):
        self.socket_path = path
        # The HTTP Host header is independent of the Unix socket address.
        super().__init__("docker", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def daemon_json(path, *, daemon):
    connection = UnixHTTP(daemon, timeout=15)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        data = response.read(LIMIT + 1)
        if response.status != 200 or len(data) > LIMIT:
            raise PermissionError("Docker resource unavailable")
        return json.loads(data)
    finally:
        connection.close()


class Policy:
    def __init__(self, value, *, lookup):
        self.value, self.lookup = value, lookup
        self.template = value["sandbox"]

    def owns(self, container):
        try:
            value = self.lookup("/containers/" + container + "/json")
        except (PermissionError, OSError, ValueError):
            return False
        if value.get("Id") == self.template["Id"]:
            return True
        return (value.get("Name") == self.template["Name"]
                and value.get("Config", {}).get("Image") == self.template["Config"]["Image"]
                and value.get("HostConfig") == self.template["HostConfig"])

    def authorize(self, method, target, body):
        parsed = urlsplit(target)
        path = VERSION.sub("", unquote(parsed.path))
        if parsed.scheme or parsed.netloc or ".." in path or "\\" in path:
            raise PermissionError("Invalid Docker path")
        if method in {"GET", "HEAD"} and path in {"/_ping", "/version", "/info"}:
            return "forward"
        if method == "GET" and path == "/containers/json":
            return "list"
        if method == "GET" and path.startswith("/images/") and path.endswith("/json"):
            image = path[len("/images/"):-len("/json")]
            if image in {self.template["Config"]["Image"], self.template["Image"]}:
                return "forward"
        match = re.fullmatch(r"/containers/([^/]+)(/json|/start|/stop|/kill|/exec)?", path)
        if match and self.owns(match[1]):
            operation = (method, match[2] or "")
            if operation in {("GET", "/json"), ("POST", "/start")}:
                return "forward"
            if operation == ("POST", "/exec"):
                if json.loads(body).get("Privileged"):
                    raise PermissionError("Privileged exec is not permitted")
                return "forward"
        match = re.fullmatch(r"/exec/([a-f0-9]+)/(json|start|resize)", path)
        if match:
            execution = self.lookup("/exec/" + match[1] + "/json")
            if self.owns(execution.get("ContainerID", "")) and (method, match[2]) in {
                    ("GET", "json"), ("POST", "start"), ("POST", "resize")}:
                return "stream" if match[2] == "start" else "forward"
        raise PermissionError("Docker operation is outside this sandbox")


async def read_request(reader):
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    lines = header.decode("latin1").split("\r\n")
    method, target, version = lines[0].split(" ")
    headers = {}
    for line in lines[1:]:
        if line:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key in headers:
                raise PermissionError("Duplicate request header")
            headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise PermissionError("Chunked requests are not permitted")
    length = int(headers.get("content-length", "0"))
    if not 0 <= length <= LIMIT or version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise PermissionError("Invalid request size or protocol")
    body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
    return method, target, headers, body


async def response(writer, status, body):
    payload = json.dumps(body).encode()
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)
    await writer.drain()


async def copy_stream(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


class Server:
    def __init__(self, policy, daemon):
        self.policy, self.daemon = policy, daemon

    async def handle(self, reader, writer):
        upstream = None
        try:
            method, target, headers, body = await read_request(reader)
            action = await asyncio.to_thread(self.policy.authorize, method, target, body)
            if action == "list":
                rows = await asyncio.to_thread(self.policy.lookup, target)
                allowed = []
                for row in rows:
                    if await asyncio.to_thread(self.policy.owns, row["Id"]):
                        allowed.append(row)
                await response(writer, "200 OK", allowed)
                return
            remote, upstream = await asyncio.open_unix_connection(self.daemon)
            headers["connection"] = "Upgrade" if action == "stream" else "close"
            request = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            upstream.write(request.encode("latin1") + body)
            await upstream.drain()
            if action == "stream":
                header = await remote.readuntil(b"\r\n\r\n")
                writer.write(header)
                await writer.drain()
                # Only Docker's accepted protocol upgrade turns this connection
                # into stdin/stdout. A rejected upgrade must never forward a
                # second client request to the unrestricted daemon.
                if header.split(b"\r\n", 1)[0].split(b" ")[1] == b"101":
                    incoming = asyncio.create_task(copy_stream(reader, upstream))
                    try:
                        await copy_stream(remote, writer)
                    finally:
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                else:
                    upstream.close()
            else:
                await copy_stream(remote, writer)
        except (PermissionError, ValueError, KeyError):
            await response(writer, "403 Forbidden", {"message": "Docker operation denied by sandbox policy"})
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            # Never log request bodies or Docker output: either may contain secrets.
            pass
        finally:
            if upstream is not None:
                upstream.close()
            writer.close()


def prepare(policy_path, listen):
    value = json.loads(Path(policy_path).read_text())
    path = Path(listen)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    return value, path


async def serve(policy_path, listen, daemon):
    value, path = await asyncio.to_thread(prepare, policy_path, listen)
    policy = Policy(value, lookup=lambda path: daemon_json(path, daemon=daemon))
    server = await asyncio.start_unix_server(Server(policy, daemon).handle, str(path))
    await asyncio.to_thread(path.chmod, 0o600)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve(os.environ["SANDBOX_POLICY"], os.environ["SANDBOX_SOCKET"],
                      os.environ.get("DOCKER_DAEMON_SOCKET", "/var/run/docker.sock")))
