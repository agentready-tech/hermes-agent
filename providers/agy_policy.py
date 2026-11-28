"""Reserved route and public failure vocabulary; never reads profile settings."""

import re

PROVIDER = "gemini-antigravity"
MODEL = "gemini-3.8-flash-low"
ACP_FLASH = "gemini-3.8-flash"
CAPABILITY = "gemini-antigravity-v1"
BINARY = "/opt/hermes/inert/antigravity/agy"
SETTINGS = "/opt/hermes/inert/antigravity/settings.json"
LOCAL_ENDPOINT = "agy://gemini-antigravity"
PROMPT_LIMIT = 1024 * 1024

_MESSAGES = {
    "unavailable": "Gemini Antigravity is unavailable on this runtime.",
    "auth_required": "Connect Gemini Antigravity in the protected Panel first.",
    "auth_expired": "Reconnect Gemini Antigravity in the protected Panel.",
    "transport": "Gemini Antigravity did not complete the request.",
    "protocol": "Gemini Antigravity returned an invalid response.",
    "containment": "Gemini Antigravity violated the process policy; the route is closed.",
    "unsupported_input": "Gemini Antigravity accepts only text and canonical tool history.",
    "unsupported_model": "The selected model is unavailable for this Gemini subscription.",
    "input_too_large": "Gemini Antigravity request exceeds the local size limit.",
    "duplicate_tool_call": "Gemini Antigravity repeated a tool call identifier.",
    "cancelled": "Gemini Antigravity request cancelled.",
}


class AgyError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


def is_agy(provider):
    return isinstance(provider, str) and provider.strip().lower() in (PROVIDER, f"custom:{PROVIDER}")


def valid_model_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,159}", value) is not None


def require_model(value):
    if not valid_model_id(value):
        raise AgyError("unsupported_model")
