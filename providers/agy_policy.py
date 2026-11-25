"""Reserved route and public failure vocabulary; never reads profile settings."""

PROVIDER = "gemini-antigravity"
MODEL = "gemini-3.8-flash-low"
CAPABILITY = "gemini-antigravity-v1"
BINARY = "/opt/hermes/inert/antigravity/agy"
SETTINGS = "/opt/hermes/inert/antigravity/settings.json"
LOCAL_ENDPOINT = "agy://gemini-antigravity"

_MESSAGES = {
    "unavailable": "Gemini Antigravity is unavailable on this runtime.",
    "auth_required": "Connect Gemini Antigravity in the protected Panel first.",
    "auth_expired": "Reconnect Gemini Antigravity in the protected Panel.",
    "transport": "Gemini Antigravity did not complete the request.",
    "protocol": "Gemini Antigravity returned an invalid response.",
    "containment": "Gemini Antigravity violated the process policy; the route is closed.",
    "unsupported_input": "Gemini Antigravity accepts only text and canonical tool history.",
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
