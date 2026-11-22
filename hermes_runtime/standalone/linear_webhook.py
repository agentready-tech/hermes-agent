"""Validate an AGE-5 delivery after trusted profile/route resolution.

No listener, profile lookup, receipt lookup or wakeup happens here. A checked
delivery is not an admission receipt, even when its signature is valid.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal


_WINDOW_MS = 60_000
_BODY_LIMIT = 256 * 1024
_HEX_HMAC = re.compile(r"[0-9a-fA-F]{64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class InvalidLinearDelivery(ValueError):
    def __init__(self, status: int):
        super().__init__("Linear delivery rejected")
        self.status = status


@dataclass(frozen=True)
class LinearSubscription:
    organization_id: str
    webhook_id: str
    team_id: str
    events: frozenset[str]
    secret: bytes = field(repr=False)


@dataclass(frozen=True)
class CheckedLinearDelivery:
    delivery_id: str
    body_digest: str
    freshness: Literal["fresh", "stale"]
    issue_fields: dict[str, str] | None


def _header_values(headers, name):
    # The caller must supply all original occurrences, not a flattened dict.
    return [value for key, value in headers if key.lower() == name]


def _json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _invalid_constant(_value):
    raise ValueError("Non-JSON constant")


def _required_uuid(value):
    # Linear Issue identifiers are data, not instructions. Keep only the
    # canonical identifier shape; do not pass arbitrary body strings to main.
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise InvalidLinearDelivery(400)
    return value


def _issue_fields(payload, subscription):
    event_type, action = payload.get("type"), payload.get("action")
    if not isinstance(event_type, str) or not isinstance(action, str):
        raise InvalidLinearDelivery(400)
    if event_type != "Issue" or action not in {"create", "update"}:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidLinearDelivery(400)
    team_id = _required_uuid(data.get("teamId"))
    kind = f"Issue/{action}"
    fields = {"kind": kind, "issue_id": _required_uuid(data.get("id"))}
    if action == "update":
        previous = payload.get("updatedFrom", {})
        if not isinstance(previous, dict):
            raise InvalidLinearDelivery(400)
        if "stateId" not in previous:
            return None
        old, new = _required_uuid(previous["stateId"]), _required_uuid(data.get("stateId"))
        if old == new:
            return None
        fields.update(previous_state_id=old, state_id=new)
    if team_id != subscription.team_id or kind not in subscription.events:
        return None
    return fields


def check_linear_delivery(body: bytes, headers: Iterable[tuple[str, str]], subscription: LinearSubscription,
                          *, now_ms: int) -> CheckedLinearDelivery:
    """Return the minimum data for the durable boundary, or a safe rejection.

    ``stale`` is only a candidate for exact receipt lookup; it must never
    proceed to a new admission or be acknowledged as ignored.
    """
    if len(body) > _BODY_LIMIT:
        raise InvalidLinearDelivery(413)
    headers = tuple(headers)
    signatures = _header_values(headers, "linear-signature")
    if not subscription.secret or len(signatures) != 1 or not _HEX_HMAC.fullmatch(signatures[0]):
        raise InvalidLinearDelivery(401)
    expected = hmac.digest(subscription.secret, body, "sha256")
    if not hmac.compare_digest(expected, bytes.fromhex(signatures[0])):
        raise InvalidLinearDelivery(401)
    deliveries = _header_values(headers, "linear-delivery")
    if len(deliveries) != 1:
        raise InvalidLinearDelivery(400)
    delivery_id = _required_uuid(deliveries[0])
    if delivery_id[14] != "4" or delivery_id[19] not in "89ab":
        raise InvalidLinearDelivery(400)
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_json_object,
                             parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise InvalidLinearDelivery(400) from None
    if not isinstance(payload, dict) or type(payload.get("webhookTimestamp")) is not int:
        raise InvalidLinearDelivery(400)
    if (_required_uuid(payload.get("organizationId")) != subscription.organization_id
            or _required_uuid(payload.get("webhookId")) != subscription.webhook_id):
        raise InvalidLinearDelivery(404)
    age = now_ms - payload["webhookTimestamp"]
    if age < -_WINDOW_MS:
        raise InvalidLinearDelivery(401)
    digest = hashlib.sha256(body).hexdigest()
    if age > _WINDOW_MS:
        return CheckedLinearDelivery(delivery_id, digest, "stale", None)
    return CheckedLinearDelivery(delivery_id, digest, "fresh", _issue_fields(payload, subscription))