"""Encrypted connection grants; only public metadata leaves this profile."""

import copy
import json
import re
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path

from agent.vault_store import VaultStore

SERVICES = {"mailRead", "mailSend", "drive", "sheets", "docs", "slides", "calendar"}
FIELDS = {"id", "environmentId", "provider", "name", "address", "services", "inbound", "outbound",
          "proactive", "senderPolicy", "allowedSenders", "since", "gmailHistoryId", "enabled", "status",
          "createdAt", "verifiedAt", "lastCheckedAt", "lastError"}


class WorkspaceError(Exception):
    """Only stable codes from this class are suitable for external errors."""


def now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r'[^\s@<>"\\]+@[^\s@<>"\\]+\.[^\s@<>"\\]+', value):
        raise WorkspaceError("INVALID_MAIL_ADDRESS")
    return value.lower()


def rules(value):
    policy = value.get("senderPolicy", "allowlist")
    senders = value.get("allowedSenders", [])
    if policy not in {"allowlist", "all"} or not isinstance(senders, list) or len(senders) > 100:
        raise WorkspaceError("INVALID_MAIL_SENDERS")
    senders = [address(item) for item in senders]
    proactive = value.get("proactive") is True
    if proactive and policy == "allowlist" and not senders:
        raise WorkspaceError("INVALID_MAIL_SENDERS")
    return {"proactive": proactive, "senderPolicy": policy, "allowedSenders": senders}


def validate_connection(value, environment_id=None):
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise WorkspaceError("INVALID_WORKSPACE_CONNECTION")
    result = copy.deepcopy(value)
    if not re.fullmatch(r"connection_[a-f0-9]{32}", str(result.get("id", ""))):
        raise WorkspaceError("INVALID_WORKSPACE_CONNECTION")
    if not re.fullmatch(r"env_[a-f0-9]{32}", str(result.get("environmentId", ""))):
        raise WorkspaceError("FORBIDDEN")
    if environment_id and result["environmentId"] != environment_id:
        raise WorkspaceError("FORBIDDEN")
    if result.get("provider") not in {"imap_smtp", "google"}:
        raise WorkspaceError("INVALID_WORKSPACE_CONNECTION")
    services = result.get("services")
    if not isinstance(services, list) or not services or any(item not in SERVICES for item in services):
        raise WorkspaceError("INVALID_WORKSPACE_PERMISSIONS")
    if result["provider"] == "imap_smtp" and set(services) - {"mailRead", "mailSend"}:
        raise WorkspaceError("INVALID_WORKSPACE_PERMISSIONS")
    result["address"] = address(result.get("address"))
    result["name"] = str(result.get("name", result["address"]))[:100]
    result.update(rules(result))
    if "senderPolicy" not in value:
        result.pop("senderPolicy")
    if result["proactive"] and "mailRead" not in services:
        raise WorkspaceError("INVALID_MAIL_READ_REQUIRED")
    for key in ("since", "createdAt"):
        try:
            datetime.fromisoformat(result[key])
        except (KeyError, AttributeError, TypeError, ValueError):
            raise WorkspaceError("INVALID_WORKSPACE_CONNECTION") from None
    for key in ("inbound", "outbound"):
        endpoint = result.get(key)
        if endpoint is not None and (not isinstance(endpoint, dict) or set(endpoint) != {"host", "port", "security"}
                or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", str(endpoint.get("host", "")))
                or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535
                or endpoint.get("security") not in {"tls", "starttls"}):
            raise WorkspaceError("INVALID_MAIL_SERVER")
    return result


def validate_credential(provider, value):
    keys = {"username", "password"} if provider == "imap_smtp" else {"refreshToken"}
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(v, str) or not v or len(v) > 8192 for v in value.values()):
        raise WorkspaceError("INVALID_WORKSPACE_CREDENTIAL")
    return copy.deepcopy(value)


def public_connection(record):
    return {**copy.deepcopy(record["connection"]), "credentialConfigured": bool(record.get("credential"))}


def validate_google_settings(value):
    if not isinstance(value, dict) or set(value) != {"clientId", "clientSecret"} or any(not isinstance(v, str) or not v or len(v) > 8192 for v in value.values()):
        raise WorkspaceError("INVALID_GOOGLE_OAUTH_NOT_CONFIGURED")
    return copy.deepcopy(value)


def google_connection_order(record):
    moment = datetime.fromisoformat(record["connection"]["createdAt"])
    return moment.replace(tzinfo=UTC).timestamp() if moment.tzinfo is None else moment.timestamp(), record["connection"]["id"]


def google_delivery_rules(records):
    variants = {}
    for record in records:
        connection = record["connection"]
        value = rules(connection)
        if value["proactive"] or value["senderPolicy"] != "allowlist" or value["allowedSenders"]:
            identity = value["proactive"], value["senderPolicy"], tuple(sorted(set(value["allowedSenders"])))
            variants.setdefault(identity, {key: copy.deepcopy(connection[key]) for key in ("proactive", "senderPolicy", "allowedSenders") if key in connection})
    if len(variants) > 1:
        raise WorkspaceError("INVALID_GOOGLE_DUPLICATE_RULES_CONFLICT")
    return next(iter(variants.values()), {})


class ConnectionStore:
    """Reuse the native vault's crypto, locks and durable writes in a separate vault slot."""

    def __init__(self, home):
        self.home = Path(home)
        self.vault = VaultStore(self.home / "vault" / "workspace")

    @contextmanager
    def transaction(self):
        with self.vault._locked():
            items = self.vault._read_all()
            state = items[0] if items else {"connections": {}, "google": {}}
            original = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != original:
                self.vault._write_all([state])

    def read(self):
        with self.transaction() as state:
            return copy.deepcopy(state)

    def list(self):
        return [public_connection(record) for record in self.read()["connections"].values()]

    def get(self, identity, permission=None):
        record = self.read()["connections"].get(identity)
        if not record or not record["connection"].get("enabled", True):
            raise WorkspaceError("NOT_FOUND")
        if permission and permission not in record["connection"]["services"]:
            raise WorkspaceError("FORBIDDEN")
        return record

    def update(self, identity, values):
        with self.transaction() as state:
            record = state["connections"].get(identity)
            if not record:
                raise WorkspaceError("NOT_FOUND")
            record["connection"].update(values)
            return public_connection(record)

    def upsert(self, value, credential=None, *, environment_id=None, importing=False):
        connection = validate_connection(value, environment_id)
        with self.transaction() as state:
            records = state["connections"]
            previous = records.get(connection["id"])
            if previous and (previous["connection"]["environmentId"] != connection["environmentId"] or previous["connection"]["provider"] != connection["provider"]):
                raise WorkspaceError("FORBIDDEN")
            if not previous and len(records) >= 5:
                raise WorkspaceError("INVALID_WORKSPACE_CONNECTION_LIMIT")
            secret = validate_credential(connection["provider"], credential) if credential is not None else previous and previous["credential"]
            if not secret:
                raise WorkspaceError("INVALID_WORKSPACE_CREDENTIAL")
            record = {"connection": connection, "credential": secret}
            if importing and previous and previous != record:
                raise WorkspaceError("INVALID_WORKSPACE_IMPORT_CONFLICT")
            records[connection["id"]] = record
            return public_connection(record)

    def settings(self, google):
        google = validate_google_settings(google)
        with self.transaction() as state:
            if state["google"].get("clientId") not in {None, google["clientId"]} and any(record["connection"]["provider"] == "google" for record in state["connections"].values()):
                raise WorkspaceError("INVALID_GOOGLE_CLIENT_IN_USE")
            state["google"] = copy.deepcopy(google)

    def reconnect_google(self, value, credential, google, expected_google, *, environment_id):
        connection = validate_connection(value, environment_id)
        secret = validate_credential("google", credential)
        google = validate_google_settings(google)
        if connection["provider"] != "google" or set(connection["services"]) != SERVICES:
            raise WorkspaceError("INVALID_WORKSPACE_PERMISSIONS")
        with self.transaction() as state:
            records = state["connections"]
            if state["google"] not in (expected_google, google):
                raise WorkspaceError("INVALID_GOOGLE_CLIENT_IN_USE")
            previous = records.get(connection["id"])
            if previous and (any(previous["connection"].get(key) != connection[key] for key in ("environmentId", "provider")) or address(previous["connection"].get("address")) != connection["address"]):
                raise WorkspaceError("FORBIDDEN")
            matches = [record for record in records.values() if record["connection"]["provider"] == "google" and address(record["connection"]["address"]) == connection["address"]]
            if any(record["connection"]["environmentId"] != environment_id for record in matches):
                raise WorkspaceError("FORBIDDEN")
            matches.sort(key=google_connection_order)
            delivery = google_delivery_rules(matches)
            if not matches and len(records) >= 5:
                raise WorkspaceError("INVALID_WORKSPACE_CONNECTION_LIMIT")
            if matches:
                current = copy.deepcopy(matches[0]["connection"])
                current.update(delivery)
                for key in ("address", "services", "enabled", "status", "verifiedAt"):
                    current[key] = copy.deepcopy(connection[key])
                current.pop("lastError", None)
                if not current.get("gmailHistoryId") and connection.get("gmailHistoryId"):
                    current["gmailHistoryId"] = connection["gmailHistoryId"]
                connection = validate_connection(current, environment_id)
            record = {"connection": connection, "credential": secret}
            state["google"] = google
            for duplicate in matches:
                del records[duplicate["connection"]["id"]]
            records[connection["id"]] = record
            return public_connection(record)
