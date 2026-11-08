"""Protected settings-only stdin transport; never exposes agent tools to Panel."""

import argparse
import json
import re
import sys
from pathlib import Path

from agent.skill_library import SkillLibraryError
from hermes_runtime.skill_inventory import SkillReadError
from hermes_constants import get_hermes_home
from hermes_runtime.workspace import CAPABILITY
from hermes_runtime.workspace.service import Workspace
from hermes_runtime.workspace.store import WorkspaceError, now, public_connection, rules, validate_connection, validate_credential

OPERATIONS = {
    "list": set(), "import": {"connections", "google"}, "upsert": {"connection", "credential"},
    "verify": {"connectionId"}, "disconnect": {"connectionId"},
    "rules": {"connectionId", "proactive", "senderPolicy", "allowedSenders"}, "discover": {"address"},
    "google_exchange": {"connectionId", "environmentId", "code", "verifier", "redirectUri", "clientId", "clientSecret", "services"},
    "settings": {"google"},
}


def environment_id(home):
    from hermes_runtime.standalone.config import settings
    value = settings(home).get("workspace", {}).get("environment_id")
    if not isinstance(value, str) or not re.fullmatch(r"env_[a-f0-9]{32}", value):
        raise WorkspaceError("INVALID_WORKSPACE_ENVIRONMENT_NOT_PINNED")
    return value


def import_connections(workspace, request, environment):
    entries = request.get("connections")
    if not isinstance(entries, list) or len(entries) > 5:
        raise WorkspaceError("INVALID_WORKSPACE_CONNECTION_LIMIT")
    prepared = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"connection", "credential"}:
            raise WorkspaceError("INVALID_WORKSPACE_CONNECTION")
        connection = validate_connection(entry["connection"], environment)
        prepared.append({"connection": connection, "credential": validate_credential(connection["provider"], entry["credential"])})
    imported = 0
    with workspace.store.transaction() as state:
        if len(set(state["connections"]) | {row["connection"]["id"] for row in prepared}) > 5:
            raise WorkspaceError("INVALID_WORKSPACE_CONNECTION_LIMIT")
        for record in prepared:
            identity = record["connection"]["id"]
            existing = state["connections"].get(identity)
            if existing and existing != record:
                raise WorkspaceError("INVALID_WORKSPACE_IMPORT_CONFLICT")
        for record in prepared:
            identity = record["connection"]["id"]
            imported += identity not in state["connections"]
            state["connections"][identity] = record
        if "google" in request:
            google = request["google"]
            if not isinstance(google, dict) or set(google) != {"clientId", "clientSecret"} or any(not isinstance(v, str) or not v or len(v) > 8192 for v in google.values()):
                raise WorkspaceError("INVALID_GOOGLE_OAUTH_NOT_CONFIGURED")
            if state["google"] and state["google"] != google:
                raise WorkspaceError("INVALID_WORKSPACE_IMPORT_CONFLICT")
            state["google"] = google
    return {"connections": workspace.store.list(), "imported": imported, "capability": CAPABILITY}


def google_exchange(workspace, request, environment):
    if environment and request.get("environmentId") != environment:
        raise WorkspaceError("FORBIDDEN")
    google = {"clientId": request["clientId"], "clientSecret": request["clientSecret"]}
    current = workspace.store.read()
    if request["connectionId"] in current["connections"] or len(current["connections"]) >= 5:
        raise WorkspaceError("INVALID_WORKSPACE_CONNECTION_LIMIT")
    if current["google"].get("clientId") not in {None, google["clientId"]} and any(record["connection"]["provider"] == "google" for record in current["connections"].values()):
        raise WorkspaceError("INVALID_GOOGLE_CLIENT_IN_USE")
    result = workspace.runner(workspace.home, "sandbox_google", {**request, "operation": "exchange", "google": google})
    moment = now()
    connection = {"id": request["connectionId"], "environmentId": request["environmentId"], "provider": "google",
        "name": result["address"], "address": result["address"], "services": request["services"], "proactive": False,
        "senderPolicy": "allowlist", "allowedSenders": [], "since": moment, "enabled": True, "status": "connected",
        "createdAt": moment, "verifiedAt": moment}
    if result.get("gmailHistoryId"):
        connection["gmailHistoryId"] = result["gmailHistoryId"]
    # Both the application and grant are committed inside one native vault transaction.
    value = validate_connection(connection, environment)
    secret = validate_credential("google", result["credential"])
    with workspace.store.transaction() as state:
        if value["id"] in state["connections"] or len(state["connections"]) >= 5:
            raise WorkspaceError("INVALID_WORKSPACE_CONNECTION_LIMIT")
        state["google"] = google
        state["connections"][value["id"]] = {"connection": value, "credential": secret}
    return {"connection": {**value, "credentialConfigured": True}}


def dispatch(home, request, *, workspace=None):
    if not isinstance(request, dict):
        raise WorkspaceError("INVALID_WORKSPACE_OPERATION")
    operation = request.get("operation")
    if operation == "skills_validate":
        from hermes_runtime.skill_inventory import validate_control
        return validate_control(home, request)
    if operation not in OPERATIONS or set(request) - (OPERATIONS[operation] | {"operation", "environmentId"}):
        raise WorkspaceError("INVALID_WORKSPACE_OPERATION")
    workspace = workspace or Workspace(home)
    environment = environment_id(home)
    if "environmentId" in request and request["environmentId"] != environment:
        raise WorkspaceError("FORBIDDEN")
    if operation == "list":
        return {"connections": workspace.store.list(), "capability": CAPABILITY}
    if operation == "import":
        return import_connections(workspace, request, environment)
    if operation == "upsert":
        return {"connection": workspace.store.upsert(request["connection"], request.get("credential"), environment_id=environment)}
    if operation == "verify":
        return workspace.verify(request["connectionId"])
    if operation == "discover":
        return workspace.runner(home, "sandbox_discovery", {"operation": "discover", "address": request["address"]})
    if operation == "google_exchange":
        return google_exchange(workspace, request, environment)
    if operation == "settings":
        workspace.store.settings(request["google"])
        return {"configured": True}
    if operation == "rules":
        identity = request["connectionId"]
        current = workspace.store.get(identity)
        changes = rules(request)
        if changes["proactive"] and "mailRead" not in current["connection"]["services"]:
            raise WorkspaceError("INVALID_MAIL_READ_REQUIRED")
        return {"connection": workspace.store.update(identity, changes)}
    with workspace.store.transaction() as state:
        if request["connectionId"] not in state["connections"]:
            raise WorkspaceError("NOT_FOUND")
        del state["connections"][request["connectionId"]]
    return {"disconnected": True}


def response(home, request, *, workspace=None):
    try:
        return {"ok": True, "value": dispatch(home, request, workspace=workspace)}
    except (WorkspaceError, SkillLibraryError, SkillReadError) as error:
        return {"ok": False, "error": str(error)}
    except Exception:  # health: allow BLE001 -- no request, credential, vendor body or exception text crosses this protected boundary
        return {"ok": False, "error": "INVALID_WORKSPACE_REQUEST"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args()
    home = (args.home or get_hermes_home()).resolve()
    raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    try:
        request = json.loads(raw) if len(raw) <= 2 * 1024 * 1024 else None
    except (ValueError, UnicodeDecodeError):
        request = None
    print(json.dumps(response(home, request), ensure_ascii=True))


if __name__ == "__main__":
    main()
