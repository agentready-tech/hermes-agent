"""Durable admission of trusted producer inputs into an existing managed main."""

from dataclasses import dataclass
from functools import partial
import hashlib
import json
from pathlib import Path
from typing import Literal

from gateway.session_authorities import authority_for_home, owner_scope, require_serving
from hermes_constants import get_hermes_home
from hermes_runtime.adapters.hermes import managed_home
from hermes_runtime.standalone.linear_webhook import CheckedLinearDelivery
from hermes_state_main_admission import LinearKey, _admit_linear_input, _linear_request_id
from hermes_state_runtime import RuntimeStoreError


@dataclass(frozen=True)
class LinearInput:
    delivery: CheckedLinearDelivery
    subscription_id: str
    authority: object


@dataclass(frozen=True)
class LinearReceipt:
    status: Literal['accepted', 'duplicate', 'ignored']
    admission_id: str | None = None


@dataclass(frozen=True)
class WorkspaceMail:
    prompt: str
    source: str
    event_id: str
    metadata: dict | None = None
    session_store: object = None


def _require_home(home):
    if Path(get_hermes_home()).resolve() != home or managed_home() != home:
        raise RuntimeStoreError('profile_mismatch')


def _require_authority(authority, home):
    _require_home(home)
    require_serving(authority)
    if (Path(authority.profile_id).resolve() != home or Path(authority.db.db_path).resolve() != home / 'state.db'
            or authority_for_home(authority.runner, home) is not authority):
        raise RuntimeStoreError('profile_mismatch')
    authority._require_admission_open()


def _main_snapshot(authority, delivery, request_id):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session_automation import snapshot_automation
    from gateway.session_contract import SessionRef
    from hermes_runtime.events import event_prompt
    from hermes_runtime.sessions import HOME_SESSION_ID, home_session_key

    runner = authority.runner
    route = home_session_key()
    entry = runner.session_store.lookup_by_session_key(route)
    live = authority.sessions.get(HOME_SESSION_ID)
    if (entry is None or entry.suspended or entry.origin is None or live is None or live.route != route
            or entry.origin.platform in {Platform.LOCAL, Platform.API_SERVER}
            or authority.logical_owner(entry.session_id) != HOME_SESSION_ID):
        raise RuntimeStoreError('not_found')
    adapter = runner._adapter_for_source(entry.origin)
    if adapter is None:
        raise RuntimeStoreError('not_found')
    content = event_prompt(json.dumps(delivery.issue_fields, sort_keys=True), 'linear', request_id, None)
    event = MessageEvent(text=content, source=entry.origin, internal=True,
                         metadata={'gateway_session_key': route, 'gateway_session_id': entry.session_id})
    payload, bound = snapshot_automation(authority, adapter, event, request_id)
    descriptor = payload.get('local_automation_v1') or payload['native_text_v1']['automation']
    if bound.session_key != route or descriptor['owner'] != HOME_SESSION_ID:
        raise RuntimeStoreError('profile_mismatch')
    return SessionRef(authority.profile_id, HOME_SESSION_ID), event, payload, route


async def _admit_linear(home, incoming):
    from gateway.session_runtime_workers import tracked_write

    authority = incoming.authority
    delivery = incoming.delivery
    with owner_scope(authority):
        _require_authority(authority, home)
        if not isinstance(delivery, CheckedLinearDelivery) or not isinstance(incoming.subscription_id, str) or not incoming.subscription_id:
            raise RuntimeStoreError('invalid_params')
        if delivery.freshness == 'fresh' and delivery.issue_fields is None:
            return LinearReceipt('ignored')
        request_id = _linear_request_id()
        ref, event, payload, route = _main_snapshot(authority, delivery, request_id)
        key = LinearKey(incoming.subscription_id, delivery.delivery_id, delivery.body_digest,
                        delivery.freshness == 'stale')

        def authorize(_conn):
            _require_authority(authority, home)

        def committed(result):
            admission_id, row = result
            if row is None:
                return LinearReceipt('duplicate', admission_id)
            authority._admitted(ref, event, row)
            return LinearReceipt('accepted', admission_id)

        def schedule(receipt):
            if receipt.status == 'accepted':
                authority._schedule_admitted(ref)

        return await tracked_write(authority, partial(
            _admit_linear_input, authority.db, epoch=authority.epoch, key=key,
            principal_id='automation:' + route, session_id=ref.session_id,
            request_id=request_id, payload=payload, authorize=authority._admission_gate(authorize)),
            then=committed, ordered=True, after=schedule)


def _write_mail(conn, inbox, incoming):
    from hermes_runtime.adapters.state import current_home_session_id, main_gateway_origin
    from hermes_runtime.events import event_prompt
    from tools.delegate_project_recovery import _write_wakeup_in_transaction

    key = json.dumps([incoming.source, incoming.event_id])
    legacy = conn.execute('SELECT message_id,delivered_at FROM agentready_inbox WHERE event_key=?', (key,)).fetchone()
    if legacy is not None:
        return {'message_id': legacy[0], 'pending': legacy[1] is None}, False
    identity = 'main-mail-' + hashlib.sha256(key.encode()).hexdigest()
    if conn.execute('SELECT 1 FROM async_delegations WHERE delegation_id=?', (identity,)).fetchone():
        return {'message_id': identity}, False

    content = event_prompt(incoming.prompt, incoming.source, incoming.event_id, incoming.metadata)
    origin = main_gateway_origin(session_store=incoming.session_store)
    if origin:
        _write_wakeup_in_transaction(conn, identity, current_home_session_id(), origin, content, agent_message=False,
                                     goal='Process the incoming email in the existing main conversation')
        return {'message_id': identity}, False
    _, identity = inbox._enqueue_identity(content, None)
    message_id, pending = inbox._enqueue_in_transaction(conn, content, key=key, identity=identity)
    return {'message_id': message_id, 'pending': pending}, pending


def _admit_mail(home, incoming):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import _forward_pending_event
    from tools.async_delegation_worker import require_ledger_owner

    _require_home(home)
    require_ledger_owner()
    if not str(incoming.prompt).strip() or not incoming.event_id or not incoming.source:
        raise ValueError('An event requires nonempty content, source and stable event_id')
    inbox = HomeInbox(home)
    with inbox.transaction() as conn:
        receipt, forward = _write_mail(conn, inbox, incoming)
    if forward:
        _forward_pending_event(inbox, receipt['message_id'])
    return receipt


def admit_main(home, incoming):
    """Workspace poll commits synchronously; Linear runs on the owning gateway loop."""
    home = Path(home).resolve()
    if isinstance(incoming, WorkspaceMail):
        return _admit_mail(home, incoming)
    if isinstance(incoming, LinearInput):
        return _admit_linear(home, incoming)
    raise TypeError('Expected Workspace mail or a checked Linear input')
