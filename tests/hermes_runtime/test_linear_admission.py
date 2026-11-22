"""AGE-5 signed fixtures through the serving owner and its real FIFO."""
import asyncio
from dataclasses import replace
import sqlite3
from threading import Event

import pytest

from hermes_runtime.standalone.linear_webhook import InvalidLinearDelivery, check_linear_delivery
from hermes_runtime.standalone.main_admission import LinearInput, admit_main
from hermes_state_runtime import ADMISSION_QUEUE_MAX, RuntimeStoreError, admit_session_input, list_session_admissions
from tests.hermes_runtime.test_linear_webhook import _DELIVERY, _NOW, _SUBSCRIPTION, _payload, _signed
from tests.hermes_runtime.test_main_cron_wake import owner

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def checked(*, delivery_id=_DELIVERY, now=_NOW, **changes):
    body, headers = _signed(_payload(**changes))
    headers[1] = ('Linear-Delivery', delivery_id)
    return check_linear_delivery(body, headers, _SUBSCRIPTION, now_ms=now)


@pytest.mark.asyncio
async def test_linear_commits_once_and_resolves_scoped_duplicates_before_capacity(main_home, monkeypatch):
    home, db = main_home
    _, authority = await owner(home, db, monkeypatch, 'linear-owner')
    wake = []
    monkeypatch.setattr(authority, '_schedule', wake.append)
    incoming = LinearInput(checked(), 'fixture-subscription', authority)
    one, two = await asyncio.gather(admit_main(home, incoming), admit_main(home, incoming))
    assert {one.status, two.status} == {'accepted', 'duplicate'}
    assert one.admission_id == two.admission_id
    assert len(wake) == 1
    rows = list_session_admissions(db, session_id='agentready_home')
    assert [row['admission_id'] for row in rows] == [one.admission_id]
    text = rows[0]['payload']['text']
    assert '[UNTRUSTED EVENT DATA]' in text and 'Issue/create' in text and 'description' not in text

    other = replace(incoming, subscription_id='another-subscription')
    assert (await admit_main(home, other)).status == 'accepted'
    for n in range(ADMISSION_QUEUE_MAX - 2):
        admit_session_input(db, epoch=authority.epoch, principal_id='fixture', session_id='agentready_home',
                            request_id=f'fixture-{n}', payload={'text': 'Inert queue entry'})
    alias = _DELIVERY[:-1] + 'a'
    dup = await admit_main(home, replace(incoming, delivery=checked(delivery_id=alias)))
    assert (dup.status, dup.admission_id) == ('duplicate', one.admission_id)
    assert (await admit_main(home, replace(incoming, delivery=checked(now=_NOW + 60_001)))).admission_id == one.admission_id
    with pytest.raises(RuntimeStoreError, match='stale_delivery'):
        await admit_main(home, replace(incoming, delivery=checked(delivery_id=alias, now=_NOW + 60_001)))
    with pytest.raises(RuntimeStoreError, match='admission_conflict'):
        await admit_main(home, replace(incoming, delivery=checked(fixture_extra='different signed bytes')))
    with pytest.raises(RuntimeStoreError, match='queue_full'):
        await admit_main(home, replace(incoming, delivery=checked(delivery_id=alias, fixture_extra='new body')))
    assert len(wake) == 2
    assert len(list_session_admissions(db, session_id='agentready_home')) == ADMISSION_QUEUE_MAX
    assert db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0] == 2


@pytest.mark.asyncio
async def test_linear_refusals_rollback_and_reopen_without_second_wake(main_home, monkeypatch):
    home, db = main_home
    _, authority = await owner(home, db, monkeypatch, 'linear-faults')
    wake = []
    monkeypatch.setattr(authority, '_schedule', wake.append)
    incoming = LinearInput(checked(), 'fixture-subscription', authority)
    assert (await admit_main(home, replace(incoming, delivery=checked(type='Comment')))).status == 'ignored'
    with pytest.raises(InvalidLinearDelivery):
        checked(webhookTimestamp=_NOW + 60_001)
    with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
        await admit_main(home / 'foreign', incoming)
    monkeypatch.setattr(authority.runner, '_draining', True)
    with pytest.raises(RuntimeStoreError, match='runtime_draining'):
        await admit_main(home, incoming)
    monkeypatch.setattr(authority.runner, '_draining', False)
    epoch = authority.epoch
    monkeypatch.setattr(authority, 'epoch', epoch + 1)
    with pytest.raises(RuntimeStoreError):
        await admit_main(home, incoming)
    monkeypatch.setattr(authority, 'epoch', epoch)
    db._execute_write(lambda conn: conn.execute("CREATE TRIGGER fixture_abort_linear BEFORE INSERT ON linear_main_receipts "
        "BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END"))
    with pytest.raises(sqlite3.IntegrityError, match='fixture rollback'):
        await admit_main(home, incoming)
    assert list_session_admissions(db, session_id='agentready_home') == []
    assert db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0] == 0
    assert wake == []
    db._execute_write(lambda conn: conn.execute('DROP TRIGGER fixture_abort_linear'))
    accepted = await admit_main(home, incoming)
    _, reopened_owner = await owner(home, db, monkeypatch, 'linear-reopened')
    monkeypatch.setattr(reopened_owner, '_schedule', wake.append)
    again = await admit_main(home, replace(incoming, authority=reopened_owner))
    assert (again.status, again.admission_id) == ('duplicate', accepted.admission_id)
    assert len(wake) == 1
    assert len(list_session_admissions(db, session_id='agentready_home')) == 1


@pytest.mark.asyncio
async def test_linear_lost_result_still_publishes_and_schedules_once(main_home, monkeypatch):
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_runtime.standalone import main_admission

    home, db = main_home
    _, authority = await owner(home, db, monkeypatch, 'linear-lost-result')
    wake = asyncio.Event()
    scheduled = []

    def schedule(ref):
        scheduled.append(ref)
        wake.set()

    monkeypatch.setattr(authority, '_schedule', schedule)
    committed = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    original = main_admission._admit_linear_input

    def pause_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        loop.call_soon_threadsafe(committed.set)
        assert release.wait(timeout=10)
        return result

    monkeypatch.setattr(main_admission, '_admit_linear_input', pause_after_commit)
    incoming = LinearInput(checked(), 'fixture-subscription', authority)
    waiter = asyncio.create_task(admit_main(home, incoming))
    try:
        await asyncio.wait_for(committed.wait(), timeout=10)
        assert not waiter.done() and not scheduled
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
        await asyncio.gather(*mutation_tasks(authority))
    await asyncio.wait_for(wake.wait(), timeout=10)
    retry = await admit_main(home, incoming)
    rows = list_session_admissions(db, session_id='agentready_home')
    assert retry.status == 'duplicate' and [row['admission_id'] for row in rows] == [retry.admission_id]
    assert len(scheduled) == 1
