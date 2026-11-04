"""Non-secret model input for profile-owned remote MCP and HTTP API connections."""

AUTH = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["none", "bearer", "api_key"]},
        "header": {"type": "string", "description": "API-key header, e.g. X-Api-Key. Never put a key here."},
    },
    "required": ["type"], "additionalProperties": False,
}
CUSTOM_CONNECTION_FIELDS = {
    "replace_key": {"type": "boolean", "description": "For a failed new setup, ask again with the protected form when the user wants to replace the key."},
    "server": {
        "type": "object",
        "description": "Custom remote HTTP MCP, including servers outside the catalog. No commands or secret values.",
        "properties": {"url": {"type": "string"}, "auth": AUTH},
        "required": ["url"], "additionalProperties": False,
    },
    "api": {
        "type": "object",
        "description": "Create a profile-owned MCP adapter for a JSON HTTP API from its documentation. Declare operations; never provide credentials.",
        "properties": {
            "base_url": {"type": "string"}, "auth": AUTH,
            "operations": {
                "type": "array", "minItems": 1, "maxItems": 40,
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "description": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "path": {"type": "string", "description": "Relative API path, optionally with {parameter} placeholders."},
                    "input_schema": {"type": "object", "description": "JSON Schema object for path, query and body parameters."},
                }, "required": ["name", "description", "method", "path", "input_schema"], "additionalProperties": False},
            },
        },
        "required": ["base_url", "operations"], "additionalProperties": False,
    },
}
