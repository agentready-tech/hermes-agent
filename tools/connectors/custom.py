"""Use native MCP lifecycle and secret capture from the owning Hermes process."""

import hashlib
import ipaddress
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from tools.registry import tool_error


def available():
    from hermes_runtime.adapters.hermes import enabled
    return enabled()


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("A bounded HTTP endpoint is required")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTP endpoint without credentials, query or fragment; authentication uses protected input")
    if parsed.hostname.lower() in {"localhost", "metadata.google.internal"} or parsed.hostname.endswith(".localhost"):
        raise ValueError("Integration endpoints cannot target local or metadata services")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Integration endpoints cannot target local, private or metadata addresses")
    return value


def credential(name, spec, *, replace=False):
    """Return header templates only. The secret answer is owned by native capture."""
    from agent.secret_scope import refresh_installed_secret_scope
    from hermes_constants import get_hermes_home
    from dotenv import dotenv_values
    from tools.skills_tool_setup import _capture_required_environment_variables
    auth = spec.get("auth") or {"type": "none"}
    if not isinstance(auth, dict) or set(auth) - {"type", "header"}:
        raise ValueError("Credentials must be entered in the protected form, never in tool arguments")
    kind = auth.get("type", "none")
    if kind == "none":
        return {}, None
    if kind not in {"bearer", "api_key"}:
        raise ValueError("Use none, bearer or api_key authentication")
    header = "Authorization" if kind == "bearer" else auth.get("header", "X-Api-Key")
    if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header):
        raise ValueError("Invalid credential header")
    if header.lower() in {"host", "content-length", "connection", "transfer-encoding", "proxy-authorization"}:
        raise ValueError("This header cannot carry integration credentials")
    suffix = hashlib.sha256(name.encode()).hexdigest()[:12].upper()
    variable = "HERMES_INTEGRATION_" + suffix + "_API_KEY"
    def present():
        return bool(dotenv_values(Path(get_hermes_home()) / ".env").get(variable))
    if replace or not present():
        result = _capture_required_environment_variables("Connection " + name, [{
            "name": variable, "prompt": "API-ключ для «" + name + "» (" + urlsplit(spec.get("url") or spec.get("base_url")).netloc + "). Значение сохраняется отдельно от переписки и не передаётся модели.",
            "required_for": "Authenticate only this integration",
        }])
        if result["missing_names"]:
            return {}, variable
        refresh_installed_secret_scope(Path(get_hermes_home()))
    if not present():
        return {}, variable
    prefix = "Bearer " if kind == "bearer" else ""
    return {header: prefix + "${" + variable + "}"}, None


def install(args, names):
    """Only add a new explicit connection; never overwrite an existing grant/config."""
    if not available():
        return tool_error("Custom connection setup is not enabled for this runtime")
    try:
        return _install(args, names)
    except (ValueError, TypeError, KeyError) as error:
        return tool_error(str(error))
    except Exception:
        # Vendor failures can echo authorization values; do not relay their raw text.
        return tool_error("Connection verification failed; existing connections were preserved. Check the endpoint and authentication.")


def _install(args, names):
    from hermes_constants import get_hermes_home
    from hermes_cli.mcp_config import _get_mcp_servers, _probe_single_server, _save_mcp_server
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_config import _interpolate_env_vars
    from tools.connectors.mcp import _registered_tool_names
    if args.get("action") != "install" or len(names) != 1:
        raise ValueError("Custom setup requires action install and one named mcp:true connector")
    if set(args) - {"action", "connectors", "server", "api", "replace_key"}:
        raise ValueError("Custom setup accepts non-secret definitions only; enter keys in the protected form")
    name = names[0]
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
        raise ValueError("Use a short lowercase connection name")
    if name in _get_mcp_servers():
        raise ValueError("Connection already exists; use its tools or enable/authorize it instead of replacing it")
    if bool(args.get("server")) == bool(args.get("api")):
        raise ValueError("Provide exactly one server or api definition")
    spec = args.get("server") or args["api"]
    if not isinstance(spec, dict):
        raise ValueError("Invalid connection definition")
    if args.get("server"):
        if set(spec) - {"url", "auth"}:
            raise ValueError("Custom MCP accepts only url and auth; no commands or raw credentials")
        cfg = {"url": endpoint(spec["url"]), "timeout": 180}
    else:
        from tools.connectors.http_api import validate_api
        spec = validate_api(spec)
        cfg = {"command": sys.executable, "args": ["-m", "tools.connectors.api_mcp", name],
               "cwd": str(Path(__file__).resolve().parents[2]),
               "env": {"HERMES_HOME": str(get_hermes_home())}, "hermes_api": spec}
    headers, missing = credential(name, spec, replace=args.get("replace_key") is True)
    if missing:
        return json.dumps({"targets": [{"name": name, "kind": "mcp", "state": "not_connected"}],
                           "secret_required": True, "secret_ref": missing,
                           "message": "Protected key entry was unavailable or cancelled. No connection was installed; never ask for the key in a chat message."})
    if headers:
        cfg["headers" if args.get("server") else "hermes_api_headers"] = headers
    if args.get("server"):
        from tools.mcp_oauth import suppress_interactive_oauth
        try:
            with suppress_interactive_oauth():
                _probe_single_server(name, _interpolate_env_vars(cfg), connect_timeout=30)
        except Exception:
            raise ConnectionError("Remote MCP verification failed") from None
    if not _save_mcp_server(name, cfg):
        raise ValueError("Native MCP configuration validation rejected the connection")
    try:
        register_mcp_servers({name: _interpolate_env_vars(cfg)})
        tools = _registered_tool_names(name)
    except Exception:
        raise ConnectionError("MCP registration failed") from None
    return json.dumps({"targets": [{"name": name, "kind": "mcp", "state": "connected", "tools": tools}],
                       "persistent": True, "authenticationVerified": bool(args.get("server")),
                       "message": "Connection saved in this agent's persistent home. For an API adapter, registration verifies its tools, not a live API operation. Use its callable tools and create a reusable skill with the existing skill manager; keep credentials out of skill text."})
