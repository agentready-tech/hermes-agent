"""Bounded JSON HTTP operations for the first-party API MCP adapter."""

import json
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE = 2 * 1024 * 1024


def validate_api(spec):
    from tools.connectors.custom import endpoint
    if not isinstance(spec, dict) or set(spec) - {"base_url", "auth", "operations"}:
        raise ValueError("API definitions accept base_url, auth and declared operations only")
    endpoint(spec.get("base_url"))
    if len(json.dumps(spec)) > 128 * 1024:
        raise ValueError("API definition exceeds the size limit")
    operations = spec.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 40:
        raise ValueError("Declare between 1 and 40 API operations")
    names = set()
    for operation in operations:
        _validate_operation(operation)
        if operation["name"] in names:
            raise ValueError("Operation names must be unique")
        names.add(operation["name"])
    # Copy producer-owned data; registration must not mutate caller arguments.
    return json.loads(json.dumps(spec))


def _validate_operation(operation):
    from jsonschema import Draft202012Validator
    fields = {"name", "description", "method", "path", "input_schema"}
    if not isinstance(operation, dict) or set(operation) != fields:
        raise ValueError("Each operation needs name, description, method, path and input_schema")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", str(operation["name"])):
        raise ValueError("Invalid operation name")
    if not isinstance(operation["description"], str) or not 1 <= len(operation["description"]) <= 4000:
        raise ValueError("Provide a bounded operation description")
    if operation["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("Unsupported API method")
    path = operation["path"]
    if (not isinstance(path, str) or len(path) > 2048 or path.startswith("//")
            or urlsplit(path).scheme or "?" in path or "#" in path or ".." in path.split("/")):
        raise ValueError("Use a relative operation path; pass query parameters separately")
    schema = operation["input_schema"]
    if (not isinstance(schema, dict) or schema.get("type") != "object"
            or set(schema.get("properties", {})) - {"path", "query", "body"}):
        raise ValueError("Operation input_schema describes an object with path, query and/or body")
    _local_schema_refs(schema)
    Draft202012Validator.check_schema(schema)


def _local_schema_refs(value):
    if isinstance(value, dict):
        if "$ref" in value and (not isinstance(value["$ref"], str) or not value["$ref"].startswith("#")):
            raise ValueError("Bundle schemas locally; external schema references are not fetched")
        for item in value.values():
            _local_schema_refs(item)
    elif isinstance(value, list):
        for item in value:
            _local_schema_refs(item)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never move this connection's authorization to another host.
        return None


def request_parts(spec, operation, arguments):
    from jsonschema import Draft202012Validator
    if not isinstance(arguments, dict) or set(arguments) - {"path", "query", "body"}:
        raise ValueError("API arguments may contain path, query and body only")
    if not Draft202012Validator(operation["input_schema"]).is_valid(arguments):
        raise ValueError("API arguments do not match the declared operation schema")
    path_args, query = arguments.get("path", {}), arguments.get("query", {})
    if not isinstance(path_args, dict) or not isinstance(query, dict):
        raise ValueError("Path and query parameters must be objects")
    path = operation["path"]
    required = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", path))
    if set(path_args) != required:
        raise ValueError("Path parameters must match the operation's placeholders")
    for key in required:
        value = path_args[key]
        if not isinstance(value, (str, int)) or str(value) in {".", ".."}:
            raise ValueError("Invalid path parameter")
        path = path.replace("{" + key + "}", quote(str(value), safe=""))
    url = spec["base_url"].rstrip("/") + "/" + path.lstrip("/")
    if query:
        url += "?" + urlencode(query, doseq=True)
    body = None
    if "body" in arguments:
        if operation["method"] == "GET":
            raise ValueError("GET operations cannot have a request body")
        body = json.dumps(arguments["body"]).encode()
        if len(body) > MAX_RESPONSE:
            raise ValueError("Request body is too large")
    return url, body


def execute(spec, name, arguments, headers):
    operation = next((op for op in spec["operations"] if op["name"] == name), None)
    if operation is None:
        raise ValueError("Unknown API operation")
    url, body = request_parts(spec, operation, arguments)
    request = Request(url, data=body, method=operation["method"],
                      headers={"Accept": "application/json", "Content-Type": "application/json", **headers})
    status, data = response_data(request)
    if len(data) > MAX_RESPONSE:
        raise ValueError("API response exceeded the size limit")
    # Do not expose a provider's reflected authorization, including JSON-escaped values.
    text = data.decode("utf-8", errors="replace")
    for value in headers.values():
        for secret in [value, value.removeprefix("Bearer ")]:
            if secret:
                text = text.replace(secret, "[REDACTED]").replace(json.dumps(secret)[1:-1], "[REDACTED]")
    try:
        result = json.loads(text)
    except ValueError:
        result = {"text": text}
    return {"status": status, "data": result}


def response_data(request):
    """Bounded HTTP I/O follows the existing terminal backend, including its egress."""
    from tools.environments.network_transport import SandboxTransportUnavailable, http_transport, sandbox_environment
    try:
        environment = sandbox_environment()
    except SandboxTransportUnavailable:
        raise OSError("API connection cannot use its configured sandbox") from None
    if environment is None:
        opener = build_opener(NoRedirect())
        try:
            response = opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.read(MAX_RESPONSE + 1)
    import httpx
    try:
        with httpx.Client(transport=http_transport(httpx, async_mode=False), trust_env=False,
                          follow_redirects=False, timeout=30) as client:
            with client.stream(request.method, request.full_url, content=request.data, headers=dict(request.header_items())) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk[:MAX_RESPONSE + 1 - len(data)])
                    if len(data) > MAX_RESPONSE:
                        break
                return response.status_code, bytes(data)
    except (httpx.HTTPError, SandboxTransportUnavailable):
        raise OSError("API connection failed inside its configured sandbox") from None
