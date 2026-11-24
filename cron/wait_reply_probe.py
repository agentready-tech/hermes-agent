"""Observe replies through an owning profile's declared read-only API operation."""

import hashlib
import json
import re
from datetime import UTC, datetime


def probe_schema() -> dict:
    selector = {"type": "array", "items": {"type": "string"}, "maxItems": 6}
    props = {
        "connector": {"type": "string", "description": "Name of the existing HTTP API connection"},
        "operation": {"type": "string", "description": "Declared GET operation for the exact conversation or thread"},
        "arguments": {"type": "object", "description": "The operation's authorized path and query arguments"},
        "items_path": {**selector, "description": "Keys from the JSON response body to its message array"},
        "id_path": {**selector, "description": "Keys from a message to its stable ID"},
        "sender_path": {**selector, "description": "Keys from a message to its stable sender ID"},
        "thread_path": {**selector, "description": "Keys from a message to its conversation or thread ID"},
        "time_path": {**selector, "description": "Keys from a message to its timezone-aware ISO timestamp"},
        "sender": {"type": "string", "description": "Exact ID of the person whose reply is expected"},
        "thread": {"type": "string", "description": "Exact ID of the conversation where the original request was sent"},
        "after": {"type": "string", "description": "Timezone-aware ISO time of the original request"},
    }
    return {"type": "object", "description": "Check the authorized read operation before waking the Goal",
            "properties": props, "required": list(props), "additionalProperties": False}


def _instant(value: object) -> datetime:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise ValueError("Expected a timezone-aware reply timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Expected a timezone-aware reply timestamp")
    return moment.astimezone(UTC)


def _configured_get(probe: dict) -> tuple[dict, dict, str]:
    from tools.connectors.api_mcp import connection
    from tools.connectors.http_api import request_parts

    spec, headers = connection(probe["connector"], allowed_operation=probe["operation"])
    method = next((op for op in spec["operations"] if op["name"] == probe["operation"]), None)
    if method is None or method["method"] != "GET":
        raise ValueError("Reply polling requires an existing declared GET operation")
    url, _body = request_parts(spec, method, probe["arguments"])
    # A custom API has no independent principal resolver. Rotated credentials
    # and account replacements both require an explicit new wait binding.
    identity = json.dumps([url, {key.lower(): value for key, value in headers.items()}], sort_keys=True)
    return spec, headers, hashlib.blake2b(identity.encode(), digest_size=32).hexdigest()


def build_probe(value: object, deadline: datetime) -> dict:
    if not isinstance(value, dict) or set(value) != set(probe_schema()["properties"]):
        raise ValueError("Supply the connector, exact conversation read and reply selectors")
    for name in ("connector", "operation", "sender", "thread"):
        text = value[name]
        if not isinstance(text, str) or not 0 < len(text) <= 256 or text.strip() != text:
            raise ValueError("Expected a configured connector, operation, exact sender and thread")
    args = value["arguments"]
    if not isinstance(args, dict) or set(args) - {"path", "query"} or len(json.dumps(args)) > 8000:
        raise ValueError("Reply polling accepts only bounded path and query arguments")
    for name in ("items_path", "id_path", "sender_path", "thread_path", "time_path"):
        path = value[name]
        if (not isinstance(path, list) or not (0 if name == "items_path" else 1) <= len(path) <= 6
                or not all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key) for key in path)):
            raise ValueError("Expected literal JSON keys for the reply selectors")
    after = _instant(value["after"])
    if after >= deadline:
        raise ValueError("The original request must precede the wait deadline")
    normalized = {**value, "arguments": json.loads(json.dumps(args, sort_keys=True)), "after": after.isoformat()}
    _spec, _headers, target = _configured_get(normalized)
    return {**normalized, "target": target}


def _value(row: object, path: list[str]):
    for key in path:
        if not isinstance(row, dict) or key not in row:
            raise ValueError("Reply data does not match the saved selectors")
        row = row[key]
    return row


def _position(reply: dict) -> tuple[datetime, str]:
    return _instant(reply["received_at"]), reply["message_id"]


def bound_read(wait: dict) -> tuple[dict, dict]:
    probe = wait["reply_probe"]
    spec, headers, target = _configured_get(probe)
    if target != probe["target"] or not probe.get("thread_path") or not probe.get("thread"):
        raise ValueError("The bound conversation or its credentials have changed")
    return spec, headers


def find_reply(wait: dict) -> dict | None:
    from tools.connectors.http_api import execute

    probe = wait["reply_probe"]
    spec, headers = bound_read(wait)
    result = execute(spec, probe["operation"], probe["arguments"], headers)
    if not 200 <= result["status"] < 300:
        raise ValueError("The configured conversation read failed")
    rows = _value(result["data"], probe["items_path"])
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Expected a bounded message list from the conversation read")
    after, deadline = _instant(probe["after"]), _instant(wait["deadline"])
    cursor = _position(wait["reply_cursor"]) if "reply_cursor" in wait else (after, "")
    matches = []
    for row in rows:
        sender = _value(row, probe["sender_path"])
        if sender != probe["sender"] or _value(row, probe["thread_path"]) != probe["thread"]:
            continue
        identity = _value(row, probe["id_path"])
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,200}", identity):
            raise ValueError("Expected a stable message ID from the conversation read")
        received = _instant(_value(row, probe["time_path"]))
        if after < received < deadline and (received, identity) > cursor and identity not in wait.get("reply_receipts", ()):
            matches.append({"message_id": identity, "received_at": received.isoformat()})
    return min(matches, key=_position, default=None)


def reply_reference(reply: dict) -> dict:
    return {"kind": "polled_reply", "message_id": reply["message_id"]}


def proves_polled_reply(wait: dict, ref: dict) -> bool:
    if set(ref) != {"kind", "message_id"} or not isinstance(ref["message_id"], str) or not wait.get("reply_probe"):
        return False
    pending = wait.get("pending_reply") or {}
    return ref["message_id"] in wait.get("reply_receipts", ()) or ref["message_id"] == pending.get("message_id")


def stage_reply(tag: dict, job_id: str, wait: dict, prompt: str) -> dict | None:
    from cron.wait_binding import update_admitted_wait

    if reply := wait.get("pending_reply"):
        return reply
    observed = find_reply(wait)
    if observed is None:
        return None
    reply = {**observed, "prompt": prompt}
    # The exact receipt is recoverable even if the process exits before publication.
    update_admitted_wait(tag, job_id, lambda current: current.update(pending_reply=reply))
    return reply


def reply_request_id(reply: dict) -> str:
    digest = hashlib.sha256(reply["message_id"].encode()).hexdigest()
    return f"reply:{digest}:{reply.get('attempt', 0)}"


def record_delivery(wait: dict, reply: dict, event_id: str) -> None:
    wait["reply_cursor"] = {key: reply[key] for key in ("message_id", "received_at")}
    wait.pop("pending_reply")
    receipts = wait.setdefault("reply_receipts", {})
    if isinstance(receipts, list):
        receipts = wait["reply_receipts"] = dict.fromkeys(receipts)
    receipts[reply["message_id"]] = event_id


def accept_main_reply(store, project_id, header):
    """Accept the exact staged observation at the owning main's turn boundary."""
    from cron.wait_binding import matches_wait
    from tools.delegate_project_goal import active_goal

    metadata = header["metadata"]
    job_id, event_id, ref = metadata.get("job_id"), header.get("event_id"), metadata["reply_ref"]
    if not isinstance(event_id, str):
        return False

    def accept(workspace, conn):
        wait = workspace.get("external_wait")
        if (not matches_wait(wait, metadata.get("wait_id"), job_id)
                or wait["mode"] != "resume_main" or wait["state"] != "BOUND"
                or not isinstance(ref, dict) or not proves_polled_reply(wait, ref)
                or _instant(wait["deadline"]) <= datetime.now(UTC)):
            return False
        goal = workspace["goal_state"]
        archived, paused = conn.execute(
            "SELECT archived,paused FROM delegated_projects WHERE subagent_id=?", (project_id,),
        ).fetchone()
        if archived or not active_goal(goal):
            return False
        active = not paused and goal["status"] != "blocked"
        reply = wait.get("pending_reply")
        if reply and ref == reply_reference(reply) and event_id == f"{job_id}:{reply_request_id(reply)}":
            if active:
                record_delivery(wait, reply, event_id)
            else:
                # The old admission is retired while paused. Keep the verified
                # observation for the next runnable timer occurrence.
                reply["attempt"] = reply.get("attempt", 0) + 1
            return active
        receipts = wait.get("reply_receipts")
        return active and isinstance(receipts, dict) and receipts.get(ref["message_id"]) == event_id

    return store.mutate_workspace(project_id, accept, active_only=False)
