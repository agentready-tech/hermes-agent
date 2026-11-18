"""Admit a Panel clarification to the existing managed main turn."""

import asyncio
from dataclasses import asdict

from gateway.session_ingress_media import capture_lease
from gateway.session_runtime_workers import track_mutation
from hermes_state_keys import admission_fingerprint
from hermes_state_runtime import RuntimeStoreError, find_session_admission
from hermes_runtime.adapters.hermes import enabled
from hermes_runtime.adapters.inbox import HomeInbox
from hermes_runtime.adapters.main_transport import require_owner
from hermes_runtime.adapters.turns import forward_to_active_turn
from hermes_runtime.events import event_prompt
from hermes_runtime.sessions import HOME_SESSION_ID


async def submit(connection, request):
    authority, actor, ref = connection.authority, connection.actor, request.ref
    require_owner(connection)
    if not enabled() or ref.session_id != HOME_SESSION_ID:
        raise RuntimeStoreError('permission_denied')
    authority.authorize(actor, ref, 'session:submit')

    # An accepted inbox receipt and a queued admission are mutually exclusive for
    # this nonce, including when the caller loses the acknowledgement.
    return await asyncio.shield(track_mutation(authority, _admit(authority, actor, request)))


async def _admit(authority, actor, request):
    ref = request.ref
    async with authority.sessions[ref.session_id].mutation_lock:
        authority.authorize(actor, ref, 'session:submit')
        authority._require_admission_open()
        prior = await asyncio.to_thread(find_session_admission, authority.db,
                                        principal_id=actor.subject, session_id=ref.session_id,
                                        request_id=request.request_id)
        if prior is None:
            receipt = await _steer(authority, actor, request)
            if receipt is not None:
                return receipt
        with capture_lease():
            return asdict(await authority._submit(actor, request))


async def _steer(authority, actor, request):
    from tools.approval import has_blocking_approval

    target = request.ref.session_id
    nonce = admission_fingerprint(canonical_target=target,
                                  payload={'principal': actor.subject, 'request': request.request_id})
    event_id = 'panel:' + nonce
    identity = 'inbox-' + admission_fingerprint(canonical_target=event_id, payload=request.payload)
    inbox = HomeInbox()
    prior = await asyncio.to_thread(inbox.event_receipt, 'agentready', event_id)
    if prior is not None:
        if prior != identity:
            raise RuntimeStoreError('admission_conflict')
    else:
        text = request.payload['text']
        if (not isinstance(text, str) or not text.strip() or set(request.payload) - {'text', 'clarify_batch'}
                or has_blocking_approval(authority.sessions[target].route)):
            return None
        content = event_prompt(text, 'agentready', event_id, {})
        if not await asyncio.to_thread(forward_to_active_turn, content, input_id=identity):
            return None
    return {'admission_id': identity, 'status': 'steered'}
