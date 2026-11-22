"""AGE-5 receipt retention follows the native FIFO lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Barrier

import pytest

from hermes_state import SessionDB
from hermes_state_main_admission import LinearKey, _admit_linear_input, _linear_request_id
from hermes_state_runtime import (RuntimeStoreError, begin_runtime_epoch, cancel_session_input,
                                  claim_session_input, get_session_admission, recover_session_inputs,
                                  resolve_unknown_session_input, settle_session_input)


def submit(db, epoch, label, *, key=None):
    return _admit_linear_input(db, epoch=epoch, key=key or LinearKey('fixture-subscription', label, label, False),
                               principal_id='automation:fixture-main', session_id='main',
                               request_id=_linear_request_id(), payload={'text': 'Inert input'}, authorize=None)


def setup(db):
    db.create_session('main', 'cli')
    return begin_runtime_epoch(db, instance_id='fixture')


def test_two_connections_commit_one_receipt_and_one_admission(tmp_path):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = setup(db)
    barrier = Barrier(2)

    def write():
        with closing(SessionDB(path)) as db:
            barrier.wait(timeout=10)
            return submit(db, epoch, 'same')

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = (future.result() for future in [pool.submit(write), pool.submit(write)])
    assert a[0] == b[0]
    assert sum(row is not None for _, row in (a, b)) == 1
    with closing(SessionDB(path)) as db:
        assert db._read_one('SELECT COUNT(*) FROM session_admissions')[0] == 1
        assert db._read_one('SELECT COUNT(*) FROM linear_main_receipts')[0] == 1
    with closing(SessionDB(tmp_path / 'second.db')) as db:
        assert submit(db, setup(db), 'same')[0] != a[0]


def test_additive_open_compaction_and_terminal_retirement_keep_receipt(tmp_path):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = setup(db)
        db._execute_write(lambda conn: conn.execute('DROP TRIGGER linear_main_receipts_settle'))
        db._execute_write(lambda conn: conn.execute('DROP TABLE linear_main_receipts'))
    with closing(SessionDB(path)) as db:
        identity, _ = submit(db, epoch, 'one')
        db.publish_compression_child(parent_session_id='main', child_session_id='tip', source='cli',
                                     messages=[{'role': 'assistant', 'content': 'Inert summary'}],
                                     require_compression_lease=False)
    with closing(SessionDB(path)) as db:
        assert db.get_compression_tip('main') == 'tip'
        assert get_session_admission(db, admission_id=identity)['status'] == 'queued'
        assert submit(db, epoch, 'one') == (identity, None)
        with pytest.raises(RuntimeStoreError, match='session_busy'):
            db.delete_session('main')
        cancel_session_input(db, epoch=epoch, admission_id=identity)
        assert db._read_one('SELECT terminal_at FROM linear_main_receipts')[0] is not None
        db.delete_session('main')
        assert get_session_admission(db, admission_id=identity)['status'] == 'terminal'
        assert submit(db, epoch, 'one') == (identity, None)
        assert db._read_one('SELECT COUNT(*) FROM session_admissions')[0] == 0


@pytest.mark.parametrize('terminal', ['finish', 'cancel', 'resolve-unknown'])
def test_terminal_timestamp_and_bounded_cleanup_keep_live_rows(tmp_path, terminal):
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = setup(db)
        identity, _ = submit(db, epoch, 'terminal')
        if terminal == 'cancel':
            cancel_session_input(db, epoch=epoch, admission_id=identity)
        else:
            claimed = claim_session_input(db, epoch=epoch, session_id='main')
            if terminal == 'finish':
                settle_session_input(db, epoch=epoch, admission_id=identity,
                                     generation=claimed['generation'], outcome='completed')
            else:
                epoch = begin_runtime_epoch(db, instance_id='new-owner')
                recover_session_inputs(db, epoch=epoch)
                resolve_unknown_session_input(db, epoch=epoch, admission_id=identity,
                                              generation=claimed['generation'])
        stamp = db._read_one('SELECT terminal_at FROM linear_main_receipts WHERE admission_id=?', (identity,))[0]
        assert stamp is not None
        assert submit(db, epoch, 'terminal') == (identity, None)
        active, _ = submit(db, epoch, 'active')
        claim_session_input(db, epoch=epoch, session_id='main')
        assert get_session_admission(db, admission_id=active)['status'] == 'started'
        db._execute_write(lambda conn: conn.execute('UPDATE linear_main_receipts SET terminal_at=? WHERE admission_id=?',
                                                  (stamp - 86402, identity)))
        submit(db, epoch, 'new')
        assert db._read_one('SELECT 1 FROM linear_main_receipts WHERE admission_id=?', (identity,)) is None
        assert db._read_one('SELECT terminal_at FROM linear_main_receipts WHERE admission_id=?', (active,))[0] is None
        epoch = begin_runtime_epoch(db, instance_id='again')
        recover_session_inputs(db, epoch=epoch)
        assert get_session_admission(db, admission_id=active)['status'] == 'unknown'
        submit(db, epoch, 'next')
        assert submit(db, epoch, 'active', key=replace(LinearKey('fixture-subscription', 'active', 'active', False),
                                                       stale=True)) == (active, None)
