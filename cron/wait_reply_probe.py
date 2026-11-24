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
        "time_path": {**selector, "description": "Keys from a message to its timezone-aware ISO timestamp"},
        "sender": {"type": "string", "description": "Exact ID of the person whose reply is expected"},
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
    return spec, headers, hashlib.sha256(url.encode()).hexdigest()


def build_probe(value: object, deadline: datetime) -> dict:
    if not isinstance(value, dict) or set(value) != set(probe_schema()["properties"]):
        raise ValueError("Supply the connector, exact conversation read and reply selectors")
    for name in ("connector", "operation", "sender"):
        text = value[name]
        if not isinstance(text, str) or not 0 < len(text) <= 256 or text.strip() != text:
            raise ValueError("Expected a configured connector, operation and exact sender")
    args = value["arguments"]
    if not isinstance(args, dict) or set(args) - {"path", "query"} or len(json.dumps(args)) > 8000:
        raise ValueError("Reply polling accepts only bounded path and query arguments")
    for name in ("items_path", "id_path", "sender_path", "time_path"):
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


def find_reply(wait: dict) -> dict | None:
    from tools.connectors.http_api import execute

    probe = wait["reply_probe"]
    spec, headers, target = _configured_get(probe)
    if target != probe["target"]:
        raise ValueError("The bound conversation read has changed")
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
        if sender != probe["sender"]:
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

    reply = wait.get("pending_reply")
    if reply is None and (observed := find_reply(wait)) is not None:
        reply = {**observed, "prompt": prompt}
    if reply is not None:
        # The exact receipt is recoverable even if the process exits before publication.
        update_admitted_wait(tag, job_id, lambda current: current.update(pending_reply=reply))
    return reply


def commit_reply(tag: dict, job_id: str, reply: dict) -> None:
    from cron.wait_binding import update_admitted_wait

    def commit(wait):
        if wait.get("pending_reply") != reply:
            raise ValueError("The staged reply no longer matches the admitted reply")
        wait["reply_cursor"] = {key: reply[key] for key in ("message_id", "received_at")}
        wait.pop("pending_reply")
        wait.setdefault("reply_receipts", []).append(reply["message_id"])

    update_admitted_wait(tag, job_id, commit)
