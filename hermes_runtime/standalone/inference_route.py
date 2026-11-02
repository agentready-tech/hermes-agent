"""Resolve an explicitly configured, profile-owned auxiliary gateway route."""

from urllib.parse import urlsplit


def auxiliary_route(section):
    from agent.secret_scope import get_secret_str

    url = str(section.get("base_url") or "").rstrip("/")
    key_env = str(section.get("key_env") or "")
    if not url and not key_env:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Auxiliary gateway base_url must be an HTTPS URL")
    key = get_secret_str(key_env)
    if not key:
        raise ValueError("Auxiliary gateway credential is absent in the owning profile")
    return url, key
