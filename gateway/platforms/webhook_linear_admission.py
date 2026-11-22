"""Connection-independent Linear main admission, after server authentication.

The listener must supply an authenticated per-request scope and independently
loaded applied subscription. This module does not authenticate a caller, read
credentials, resolve workers, or register a route.
"""

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from hermes_constants import profile_name_for_home
from hermes_runtime.standalone.linear_webhook import InvalidLinearDelivery, LinearSubscription, check_linear_delivery
from hermes_runtime.standalone.main_admission import LinearInput, admit_main
from hermes_state_main_admission import LinearKey, read_linear_receipt
from hermes_state_runtime import RuntimeStoreError

if TYPE_CHECKING:
    from gateway.session_authority import SessionAuthority

_PROTOCOL = 'linear-main-v1'
_TIMEOUT_SECONDS = 3
_REFUSALS = {'admission_conflict': 409, 'stale_delivery': 401, 'profile_mismatch': 404}


@dataclass(frozen=True)
class LinearBinding:
    environment_id: str
    worker_id: str
    profile_id: str
    route_id: str
    subscription_id: str
    version: str

    def wire(self):
        return {'environmentId': self.environment_id, 'workerId': self.worker_id, 'profileId': self.profile_id,
                'routeId': self.route_id, 'subscriptionId': self.subscription_id, 'version': self.version}


@dataclass(frozen=True)
class AuthenticatedLinearAdmission:
    """Scoped by the server for the Linear admission operation, never parsed from wire data."""

    binding: LinearBinding


@dataclass(frozen=True)
class AppliedLinearSubscription:
    """Current subscription loaded server-side for the local worker and owning profile."""

    binding: LinearBinding
    home: Path
    authority: 'SessionAuthority' = field(repr=False)
    subscription: LinearSubscription = field(repr=False)
    enabled: bool = False


def _matches(requested, authenticated, applied, local_worker_id):
    if (type(authenticated) is not AuthenticatedLinearAdmission or type(applied) is not AppliedLinearSubscription
            or type(requested) is not LinearBinding or applied.enabled is not True
            or authenticated.binding != requested or applied.binding != requested
            or requested.worker_id != local_worker_id or applied.authority is None):
        return False
    return (all(isinstance(value, str) and value and value == value.strip() for value in requested.wire().values())
            and applied.home.is_absolute() and str(applied.home) == applied.authority.profile_id
            and profile_name_for_home(applied.home) == requested.profile_id)


async def _settle(applied, delivery):
    binding = applied.binding
    authority = applied.authority
    receipt = await admit_main(applied.home, LinearInput(delivery, binding.subscription_id, authority))
    if receipt.status == 'ignored':
        return {'outcome': 'ignored'}
    key = LinearKey(binding.subscription_id, delivery.delivery_id, delivery.body_digest, delivery.freshness == 'stale')
    durable = await asyncio.to_thread(read_linear_receipt, authority.db, key)
    if durable is None or durable != receipt.admission_id:
        raise RuntimeStoreError('receipt_unavailable')
    return {'outcome': receipt.status, 'receiptId': durable}


async def admit_linear_webhook(body, headers, *, requested, now_ms, authenticated=None, applied=None, local_worker_id=None):
    """Return a status and a bounded response; only a matching committed readback is a receipt."""
    try:
        if not _matches(requested, authenticated, applied, local_worker_id):
            return 404, None
        if not isinstance(body, bytes):
            return 400, None
        if not applied.subscription.secret:
            return 503, None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TIMEOUT_SECONDS
        async with asyncio.timeout_at(deadline):
            checked = check_linear_delivery(body, headers, applied.subscription, now_ms=now_ms)
            result = await _settle(applied, checked)
        if loop.time() >= deadline:
            return 503, None
    except InvalidLinearDelivery as error:
        return error.status, None
    except RuntimeStoreError as error:
        return _REFUSALS.get(error.reason, 503), None
    except (OSError, RuntimeError, sqlite3.Error):
        # TimeoutError is an OSError. A late committed writer remains recoverable by retry.
        return 503, None
    return 200, {'protocol': _PROTOCOL, 'binding': requested.wire(), 'deliveryId': checked.delivery_id,
                 'bodySha256': checked.body_digest, **result}
