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
pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools', 'tests.hermes_runtime.test_mail_reply_route']


def checked(*, delivery_id=_DELIVERY, now=_NOW, **changes):
    body, headers = _signed(_payload(**changes))
    headers[1] = ('Linear-Delivery', delivery_id)
    return check_linear_delivery(body, headers, _SUBSCRIPTION, now_ms=now)


@pytest.mark.asyncio
async def test_linear_committed_messaging_origin_is_revalidated(telegram_home, monkeypatch):
    from contextlib import closing

    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.session_envelope import restore_native
    from gateway.session_ingress_context import native_callback
    from hermes_state import SessionDB
    from hermes_state_runtime import get_session_admission
    from plugins.platforms.telegram.adapter import TelegramAdapter

    home, entry = telegram_home
    with closing(SessionDB(db_path=home / 'state.db')) as db:
        runner, authority = await owner(home, db, monkeypatch, 'linear-messaging')
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-only', typing_indicator=False))
        runner.adapters[Platform.TELEGRAM] = adapter
        runner._wire_adapter_handlers(adapter)
        monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', entry.origin.user_id)
        incoming = LinearInput(checked(), 'fixture-subscription', authority)
        with pytest.raises(RuntimeStoreError, match='not_found'):
            await admit_main(home, incoming)
        assert db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0] == 0

        source = adapter.build_source(chat_id=entry.origin.chat_id, chat_type=entry.origin.chat_type,
                                      user_id=entry.origin.user_id, thread_id=entry.origin.thread_id)
        human = MessageEvent(text='Inert messaging input', source=source, message_id='fixture-human')
        with native_callback(runner, human, home):
            earlier = await authority.admit_native(human)
        receipt = await admit_main(home, incoming)
        row = get_session_admission(db, admission_id=receipt.admission_id)
        assert earlier.ref.session_id == row['target_session_id'] == 'agentready_home'
        restored = restore_native(row['payload'], runner)
        assert restored.internal and restored.source.to_dict() == entry.origin.to_dict()


@pytest.mark.asyncio
async def test_same_checked_delivery_only_deduplicates_within_the_owning_profile(main_home, tmp_path, monkeypatch):
    from contextlib import closing

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home
    from hermes_state import SessionDB

    first_home, first_db = main_home
    _, first_owner = await owner(first_home, first_db, monkeypatch, 'linear-first-profile')
    delivery = checked()
    incoming = LinearInput(delivery, 'fixture-subscription', first_owner)
    first = await admit_main(first_home, incoming)
    second_home = tmp_path / 'second-profile'
    second_home.mkdir()
    token = set_hermes_home_override(str(second_home))
    try:
        monkeypatch.setattr(hermes, '_home', None)
        monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', second_home / 'state.db')
        activate_home(second_home)
        with closing(SessionDB(db_path=second_home / 'state.db')) as second_db:
            _, second_owner = await owner(second_home, second_db, monkeypatch, 'linear-second-profile')
            with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
                await admit_main(second_home, incoming)
            assert second_db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0] == 0
            second_input = replace(incoming, authority=second_owner)
            second = await admit_main(second_home, second_input)
            assert second.status == 'accepted' and second.admission_id != first.admission_id
            assert await admit_main(second_home, second_input) == replace(second, status='duplicate')
    finally:
        reset_hermes_home_override(token)
    assert first_db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0] == 1


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


@pytest.mark.asyncio
async def test_panel_busy_linear_and_mail_survive_compaction_and_restart(main_home, monkeypatch):
    from queue import Queue
    from types import SimpleNamespace

    from gateway.session_controls import AuthorityConnection
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.workspace import admit_mail
    from hermes_state_runtime import claim_session_input, settle_session_input
    from tools.async_delegation import restore_undelivered_completions

    home, db = main_home
    runner, authority = await owner(home, db, monkeypatch, 'mixed-before-restart')
    root = 'agentready_home'
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _: True), {
        'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
        'instance_id': authority.instance_id, 'native_bootstrap': True})
    resumed = await connection.dispatch({'id': 1, 'method': 'session.resume',
        'params': {'session_id': root, 'display_projection': True}})
    assert 'error' not in resumed, resumed
    queued = await connection.dispatch({'id': 2, 'method': 'prompt.submit',
        'params': {'session_id': root, 'submission_id': 'earlier-panel', 'text': 'Inert earlier input'}})
    assert 'error' not in queued, queued
    started = claim_session_input(db, epoch=authority.epoch, session_id=root)
    assert started['request_id'] == 'earlier-panel'

    incoming = LinearInput(checked(), 'fixture-subscription', authority)
    linear = await admit_main(home, incoming)
    mail = admit_mail('Inert email', 'email:fixture', 'mixed')
    assert admit_mail('Inert email retry', 'email:fixture', 'mixed') == mail
    assert await admit_main(home, incoming) == replace(linear, status='duplicate')
    assert not HomeInbox(home).pending()
    rows = list_session_admissions(db, session_id=root)
    assert [(r['admission_id'], r['status']) for r in rows] == [
        (started['admission_id'], 'started'), (linear.admission_id, 'queued')]
    assert rows[0]['seq'] < rows[1]['seq'] and claim_session_input(db, epoch=authority.epoch, session_id=root) is None
    settle_session_input(db, epoch=authority.epoch, admission_id=started['admission_id'],
                         generation=started['generation'], outcome='completed')

    tip = 'fixture-mixed-tip'
    db.publish_compression_child(parent_session_id=root, child_session_id=tip, source='agentready',
                                 messages=[{'role': 'assistant', 'content': 'Inert summary'}],
                                 require_compression_lease=False)
    route = authority.sessions[root].route
    assert runner.session_store.advance_compression_session(route, root, tip) is not None
    reopened, restored = await owner(home, db, monkeypatch, 'mixed-after-restart')
    assert not restored.sessions[root].subscribers
    assert await admit_main(home, replace(incoming, authority=restored)) == replace(linear, status='duplicate')
    queue = Queue()
    assert restore_undelivered_completions(queue) == 1
    event = queue.get_nowait()
    assert event['delegation_id'] == mail['message_id'] and event['session_key'] == route
    assert await reopened._deliver_completion_notification(event['summary'], event) is True
    assert await reopened._deliver_completion_notification(event['summary'], event) is None
    remaining = list_session_admissions(db, session_id=root)
    assert len(remaining) == 2 and remaining[0]['admission_id'] == linear.admission_id
    assert remaining[0]['seq'] == rows[1]['seq'] < remaining[1]['seq']
    assert all(row['status'] == 'queued' for row in remaining)
    assert db._read_one('SELECT delivery_state FROM async_delegations WHERE delegation_id=?',
                        (mail['message_id'],))[0] == 'delivered'
