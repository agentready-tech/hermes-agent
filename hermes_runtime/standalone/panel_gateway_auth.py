"""Reuse the profile's live gateway API without starting a dashboard or another owner."""
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayPanelAuth:
    gateway_owned = True

    def __init__(self, home):
        self.home = Path(home).resolve()
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def endpoint(self):
        from hermes_cli.gateway_runtime import discover_gateway_endpoint
        found = discover_gateway_endpoint(self.home)
        if found.state != "ready" or found.endpoint is None:
            raise ValueError("The existing gateway is not ready")
        return found.endpoint

    def rpc_connection(self, events=False):
        from hermes_cli.gateway_client import _session_ticket, gateway_ws_target
        if events:
            raise ValueError("Gateway events use the existing RPC connection")
        endpoint = self.endpoint()
        url, protocols = gateway_ws_target(endpoint, _session_ticket(self.home, endpoint))
        return url, {"subprotocols": protocols}

    def display_url(self, ticket):
        return self.endpoint().api_origin.replace("http:", "ws:") + "/api/display/ws?" + urlencode({"display_ticket": ticket})

    def authenticated_request(self, path, payload=None, *, method=None):
        from hermes_cli.gateway_client import _session_ticket
        if not path.startswith("/api/") or "\r" in path or "\n" in path:
            raise ValueError("A local gateway API path is required")
        endpoint = self.endpoint()
        ticket = _session_ticket(self.home, endpoint, purpose="native-http")
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(endpoint.api_origin + path, data=data, method=method,
                          headers={"Content-Type": "application/json", "X-Hermes-Gateway-Ticket": ticket})
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)
