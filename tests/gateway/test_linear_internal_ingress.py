"""AGE-5's unregistered service boundary, using the real owner and temporary SQLite."""

import asyncio
import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from threading import Event

import pytest
import pytest_asyncio

from gateway.platforms import webhook_linear_admission as ingress
from hermes_state import SessionDB
from tests.hermes_runtime.test_linear_webhook import _DELIVERY, _NOW, _SUBSCRIPTION, _payload, _signed
from tests.hermes_runtime.test_main_cron_wake import owner


@pytest_asyncio.fixture
async def target(tmp_path, monkeypatch):
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home

    profile = 'env_' + '1' * 32
    home = tmp_path / 'profiles' / profile
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setattr(hermes, '_home', None)
    activate_home(home)
    with closing(SessionDB(db_path=home / 'state.db')) as db:
        _, authority = await owner(home, db, monkeypatch, 'fixture-linear-service')
        binding = ingress.LinearBinding(profile, 'worker-a', profile, 'route-a', 'subscription-a', 'version-a')
        yield ingress.AppliedLinearSubscription(binding, home, authority, _SUBSCRIPTION, enabled=True)


def receive(target, body, headers, **overrides):
    options = dict(requested=target.binding, authenticated=ingress.AuthenticatedLinearAdmission(target.binding),
                   applied=target, local_worker_id=target.binding.worker_id, now_ms=_NOW)
    return ingress.admit_linear_webhook(body, headers, **(options | overrides))


def counts(db):
    return (db._read_one('SELECT COUNT(*) FROM session_admissions')[0],
            db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['absent', 'header-is-not-auth', 'environment_id', 'worker_id', 'profile_id',
                                 'route_id', 'subscription_id', 'version', 'wrong-local-worker', 'disabled', 'unapplied'])
async def test_binding_mismatch_or_missing_context_has_no_side_effect(target, monkeypatch, bad):
    db = target.authority.db
    scheduled = []
    monkeypatch.setattr(target.authority, '_schedule', scheduled.append)
    body, headers = _signed(_payload())
    headers.append(('X-Worker-Id', target.binding.worker_id))
    overrides = {
        'absent': {'authenticated': None},
        'header-is-not-auth': {'authenticated': target.binding.wire()},
        'wrong-local-worker': {'local_worker_id': 'worker-b'},
        'disabled': {'applied': replace(target, enabled=False)},
        'unapplied': {'applied': None},
    }.get(bad)
    if overrides is None:
        other = replace(target.binding, **{bad: 'other'})
        # Prove both comparisons are needed: context differs from the request, or
        # context and request agree but disagree with this worker's applied state.
        assert await receive(target, body, headers, requested=other) == (404, None)
        overrides = {'requested': other, 'authenticated': ingress.AuthenticatedLinearAdmission(other)}
    assert await receive(target, body, headers, **overrides) == (404, None)
    assert counts(db) == (0, 0) and not scheduled


@pytest.mark.asyncio
async def test_exact_bytes_signed_readback_and_duplicate_without_inference(target, monkeypatch):
    db = target.authority.db
    scheduled = []
    monkeypatch.setattr(target.authority, '_schedule', scheduled.append)
    body, headers = _signed(_payload())
    assert await receive(target, body + b'\n', headers) == (401, None)
    assert counts(db) == (0, 0) and not scheduled

    status, result = await receive(target, body, headers)
    row = db._read_one('SELECT body_sha256,admission_id FROM linear_main_receipts')
    assert status == 200 and counts(db) == (1, 1)
    assert result == {'protocol': 'linear-main-v1', 'binding': target.binding.wire(), 'deliveryId': _DELIVERY,
                      'bodySha256': hashlib.sha256(body).hexdigest(), 'outcome': 'accepted', 'receiptId': row[1]}
    assert row[0] == result['bodySha256'] and len(scheduled) == 1
    assert await receive(target, body, headers, now_ms=_NOW + 60_001) == (200, result | {'outcome': 'duplicate'})
    ignored_body, ignored_headers = _signed(_payload(type='Comment'))
    status, ignored = await receive(target, ignored_body, ignored_headers)
    assert status == 200 and ignored['outcome'] == 'ignored' and 'receiptId' not in ignored
    assert counts(db) == (1, 1) and len(scheduled) == 1


@pytest.mark.asyncio
async def test_storage_abort_and_readback_failure_are_not_success(target, monkeypatch):
    db = target.authority.db
    body, headers = _signed(_payload())
    db._execute_write(lambda conn: conn.execute('''CREATE TRIGGER fixture_receipt_abort BEFORE INSERT ON linear_main_receipts
        BEGIN SELECT RAISE(ABORT,'fixture rollback'); END'''))
    assert await receive(target, body, headers) == (503, None)
    assert counts(db) == (0, 0)
    db._execute_write(lambda conn: conn.execute('DROP TRIGGER fixture_receipt_abort'))

    def unavailable(_db, _key):
        raise sqlite3.OperationalError('fixture readback unavailable')

    with monkeypatch.context() as m:
        m.setattr(ingress, 'read_linear_receipt', unavailable)
        assert await receive(target, body, headers) == (503, None)
    assert counts(db) == (1, 1)
    status, result = await receive(target, body, headers)
    assert status == 200 and result['outcome'] == 'duplicate' and counts(db) == (1, 1)


@pytest.mark.asyncio
async def test_deadline_during_real_readback_has_no_false_ack(target, monkeypatch):
    body, headers = _signed(_payload())
    db = target.authority.db
    entered = Event()
    release = Event()
    original = ingress.read_linear_receipt

    def delayed(db, key):
        entered.set()
        try:
            assert release.wait(timeout=10)
            return original(db, key)
        finally:
            finished.set()

    finished = Event()
    with monkeypatch.context() as m:
        m.setattr(ingress, '_TIMEOUT_SECONDS', 2)
        m.setattr(ingress, 'read_linear_receipt', delayed)
        try:
            assert await receive(target, body, headers) == (503, None)
            assert entered.is_set() and counts(db) == (1, 1)
        finally:
            release.set()
            if entered.is_set():
                assert await asyncio.to_thread(finished.wait, 5)
    status, result = await receive(target, body, headers)
    assert status == 200 and result['outcome'] == 'duplicate' and counts(db) == (1, 1)
