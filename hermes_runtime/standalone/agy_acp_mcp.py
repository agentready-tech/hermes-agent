"""An authenticated loopback MCP endpoint inside one Antigravity lease."""

from contextlib import contextmanager
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Queue
from secrets import token_urlsafe
from threading import Event, Thread
from uuid import uuid4

from providers.agy_acp_wire import decode, encode
from providers.agy_policy import AgyError

SERVER_NAME = "hermes"
TOOL_TIMEOUT = 600
MCP_REQUEST_LIMIT = 64 * 1024


class Bridge:
    def __init__(self, catalog):
        self.tools = {tool["name"]: {"name": tool["name"], "description": tool["description"],
                                    "inputSchema": tool["parameters"]} for tool in catalog}
        self.token = token_urlsafe(32)
        self.calls = Queue(maxsize=1)
        self.replies = Queue(maxsize=1)
        self.closed = Event()
        self.accepting = Event()
        self.failed = Event()
        self.seen = set()

    def response(self, request, handler):
        method, params, request_id = request["method"], request.get("params", {}), request.get("id")
        if request_id is None:
            handler.respond(202)
            return
        if type(request_id) not in (str, int) or not isinstance(params, dict):
            raise AgyError("protocol")
        if method == "initialize":
            handler.reply(request_id, result={"protocolVersion": params["protocolVersion"],
                                             "capabilities": {"tools": {}},
                                             "serverInfo": {"name": SERVER_NAME, "version": "1"}})
        elif method == "tools/list":
            handler.reply(request_id, result={"tools": list(self.tools.values())})
        elif method == "tools/call":
            self.call(request_id, params, handler)
        elif method == "ping":
            handler.reply(request_id, result={})
        else:
            handler.reply(request_id, error={"code": -32601, "message": "Method not supported"})

    def call(self, request_id, params, handler):
        name, arguments = params.get("name"), params.get("arguments", {})
        if (not self.accepting.is_set() or self.failed.is_set() or not isinstance(name, str) or name not in self.tools
                or not isinstance(arguments, dict) or request_id in self.seen or len(self.seen) >= 1024):
            handler.reply(request_id, error={"code": -32602, "message": "Invalid or repeated Hermes tool request"})
            return
        self.seen.add(request_id)
        self.calls.put_nowait({"type": "tool_call", "id": f"agy-{uuid4().hex}", "name": name, "arguments": arguments})
        progress = (params.get("_meta") or {}).get("progressToken") if isinstance(params.get("_meta"), dict) else None
        try:
            handler.result_stream(request_id, progress)
        except OSError:
            self.failed.set()
            raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.connection.settimeout(5)
        bridge = self.server.bridge
        expected = f"127.0.0.1:{self.server.server_port}"
        if (self.path != "/mcp" or self.headers.get("Host") != expected
                or not compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {bridge.token}".encode())):
            self.respond(403)
            return
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= MCP_REQUEST_LIMIT:
                raise AgyError("protocol")
            request = decode(self.rfile.read(length))
            if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise AgyError("protocol")
            bridge.response(request, self)
        except (AgyError, TypeError, ValueError, KeyError):
            self.respond(400)
        except OSError:
            # A disconnect cannot replay the tool request.
            pass

    def do_GET(self):
        self.respond(405)

    do_DELETE = do_GET

    def respond(self, status, body=b""):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def reply(self, request_id, **payload):
        self.respond(200, encode({"jsonrpc": "2.0", "id": request_id, **payload}))

    def _event(self, row):
        self.wfile.write(b"event: message\ndata: " + encode({"jsonrpc": "2.0", **row}) + b"\n")
        self.wfile.flush()

    def result_stream(self, request_id, progress):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        bridge = self.server.bridge
        count = 0
        while not bridge.closed.is_set() and count * 5 < TOOL_TIMEOUT:
            try:
                result = bridge.replies.get(timeout=5)
            except Empty:
                count += 1
                if type(progress) in (str, int):
                    self._event({"method": "notifications/progress",
                                 "params": {"progressToken": progress, "progress": count}})
                continue
            self._event({"id": request_id, "result": {"content": result}})
            return
        bridge.failed.set()
        self._event({"id": request_id, "error": {"code": -32603, "message": "Hermes tool result not available"}})


@contextmanager
def serve(bridge):
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.bridge = bridge
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield {"type": "http", "name": SERVER_NAME, "url": f"http://127.0.0.1:{server.server_port}/mcp",
                   "headers": [{"name": "Authorization", "value": f"Bearer {bridge.token}"}]}
        finally:
            bridge.closed.set()
            server.shutdown()
            thread.join()
