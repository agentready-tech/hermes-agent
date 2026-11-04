"""First-party stdio MCP for declared HTTP APIs; never executes generated source."""

import json
import re
import sys
from http.client import HTTPException


def connection(name):
    from hermes_cli.config import read_raw_config_readonly
    from hermes_constants import get_hermes_home
    from dotenv import dotenv_values
    from pathlib import Path
    from tools.connectors.http_api import validate_api
    cfg = (read_raw_config_readonly().get("mcp_servers") or {}).get(name) or {}
    spec = validate_api(cfg.get("hermes_api"))
    headers = {}
    for header, template in cfg.get("hermes_api_headers", {}).items():
        match = re.fullmatch(r"(Bearer )?\$\{(HERMES_INTEGRATION_[A-F0-9]{12}_API_KEY)\}", template)
        if not match:
            raise ValueError("Invalid credential reference")
        value = dotenv_values(Path(get_hermes_home()) / ".env").get(match[2])
        if not value:
            raise ValueError("Integration credential is unavailable")
        if "\n" in value or "\r" in value:
            raise ValueError("Integration credential cannot contain a line break")
        headers[header] = (match[1] or "") + value
    return spec, headers


def dispatch(request, spec, headers):
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "Hermes HTTP API", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": op["name"], "description": op["description"],
                            "inputSchema": op["input_schema"]} for op in spec["operations"]]}
    if method != "tools/call":
        raise ValueError("Unsupported MCP method")
    from tools.connectors.http_api import execute
    try:
        result = execute(spec, params.get("name"), params.get("arguments", {}), headers)
        return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": not 200 <= result["status"] < 300}
    except (OSError, ValueError, TypeError, HTTPException):
        # No raw URL, credential, argument or transport exception enters the transcript.
        return {"content": [{"type": "text", "text": "API request failed. Check the declared operation, service availability and authentication. A failed response does not establish whether a write took effect; verify its result before retrying."}], "isError": True}


def serve(name, source, destination):
    spec, headers = connection(name)
    for line in source:
        if len(line) > 4 * 1024 * 1024:
            raise ValueError("MCP input exceeds limit")
        request = json.loads(line)
        if "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = dispatch(request, spec, headers)
        except (ValueError, TypeError, KeyError):
            response["error"] = {"code": -32602, "message": "Invalid MCP request"}
        destination.write(json.dumps(response) + "\n")
        destination.flush()


if __name__ == "__main__":
    serve(sys.argv[1], sys.stdin, sys.stdout)
