"""Offline contract fixtures for the AGE-5 validation slice."""

import hashlib
import hmac
import json
from dataclasses import replace

import pytest

from hermes_runtime.standalone.linear_webhook import (
    InvalidLinearDelivery,
    LinearSubscription,
    check_linear_delivery,
)

_NOW = 1_800_000_000_000
_ORG, _HOOK, _TEAM, _ISSUE, _OLD, _NEW, _DELIVERY = (
    f"f1000000-0000-4000-8000-{index:012x}" for index in range(1, 8)
)
_SUBSCRIPTION = LinearSubscription(_ORG, _HOOK, _TEAM, frozenset({"Issue/create", "Issue/update"}), b"fixture-key")


def _payload(**updates):
    return {"type": "Issue", "action": "create", "organizationId": _ORG, "webhookId": _HOOK,
            "webhookTimestamp": _NOW, "data": {"teamId": _TEAM, "id": _ISSUE, "description": "Заметка"},
            **updates}


def _signed(payload):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    signature = hmac.new(_SUBSCRIPTION.secret, body, "sha256").hexdigest()
    return body, [("lInEaR-Signature", signature), ("Linear-Delivery", _DELIVERY)]


def _check(payload, subscription=_SUBSCRIPTION):
    return check_linear_delivery(*_signed(payload), subscription, now_ms=_NOW)


@pytest.mark.parametrize("age", [-60_000, 0, 60_000])
def test_verifies_raw_utf8_and_projects_only_issue_id(age):
    body, headers = _signed(_payload(webhookTimestamp=_NOW - age))
    checked = check_linear_delivery(body, headers, _SUBSCRIPTION, now_ms=_NOW)
    assert checked.delivery_id == _DELIVERY
    assert checked.body_digest == hashlib.sha256(body).hexdigest()
    assert checked.freshness == "fresh"
    assert checked.issue_fields == {"kind": "Issue/create", "issue_id": _ISSUE}


def test_old_delivery_is_only_an_exact_receipt_candidate():
    body, headers = _signed(_payload(webhookTimestamp=_NOW - 60_001))
    checked = check_linear_delivery(body, headers, _SUBSCRIPTION, now_ms=_NOW)
    assert checked.freshness == "stale"
    assert checked.issue_fields is None
    assert (checked.delivery_id, checked.body_digest) == (_DELIVERY, hashlib.sha256(body).hexdigest())


@pytest.mark.parametrize("changes,status", [
    ({"webhookTimestamp": _NOW + 60_001}, 401),
    ({"webhookTimestamp": str(_NOW)}, 400),
    ({"webhookTimestamp": True}, 400),
    ({"webhookTimestamp": None}, 400),
    ({"organizationId": _HOOK}, 404),
    ({"webhookId": _ORG}, 404),
])
def test_signed_time_and_subscription_rules(changes, status):
    with pytest.raises(InvalidLinearDelivery) as rejection:
        _check(_payload(**changes))
    assert rejection.value.status == status


@pytest.mark.parametrize("variant,status", [
    ("different-bytes", 401), ("wrong-subscription-key", 401),
    ("missing-signature", 401), ("duplicate-signature", 401),
    ("missing-delivery", 400), ("duplicate-delivery", 400), ("non-v4-delivery", 400),
])
def test_requires_one_signature_and_one_uuid4_delivery(variant, status):
    body, headers = _signed(_payload())
    subscription = _SUBSCRIPTION
    if variant == "different-bytes":
        body += b"\n"
    elif variant == "wrong-subscription-key":
        subscription = replace(subscription, secret=b"different-fixture-key")
    elif variant == "missing-signature":
        headers.pop(0)
    elif variant == "duplicate-signature":
        headers.append(("Linear-Signature", headers[0][1]))
    elif variant == "missing-delivery":
        headers.pop()
    elif variant == "duplicate-delivery":
        headers.append(("linear-delivery", _DELIVERY))
    else:
        headers[1] = ("Linear-Delivery", "f1000000-0000-5000-8000-000000000007")
    with pytest.raises(InvalidLinearDelivery) as rejection:
        check_linear_delivery(body, headers, subscription, now_ms=_NOW)
    assert rejection.value.status == status


def test_only_requested_team_and_real_status_changes_are_projected():
    update = _payload(action="update", data={"teamId": _TEAM, "id": _ISSUE, "stateId": _NEW},
                      updatedFrom={"stateId": _OLD})
    assert _check(update).issue_fields == {"kind": "Issue/update", "issue_id": _ISSUE,
                                          "previous_state_id": _OLD, "state_id": _NEW}
    assert _check({**update, "updatedFrom": {}}).issue_fields is None
    assert _check({**update, "updatedFrom": {"stateId": _NEW}}).issue_fields is None
    assert _check(_payload(type="Comment")).issue_fields is None
    assert _check(_payload(data={"teamId": _NEW, "id": _ISSUE})).issue_fields is None
    assert _check(update, replace(_SUBSCRIPTION, events=frozenset({"Issue/create"}))).issue_fields is None


@pytest.mark.parametrize("body,status", [
    (b"[]", 400), (b"\xff", 400), (b'{"type":"Issue","type":"Comment"}', 400),
    (b" " * (256 * 1024 + 1), 413),
])
def test_rejects_invalid_or_oversize_body(body, status):
    signature = hmac.new(_SUBSCRIPTION.secret, body, "sha256").hexdigest()
    headers = [("Linear-Signature", signature), ("Linear-Delivery", _DELIVERY)]
    with pytest.raises(InvalidLinearDelivery) as rejection:
        check_linear_delivery(body, headers, _SUBSCRIPTION, now_ms=_NOW)
    assert rejection.value.status == status