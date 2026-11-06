"""Read-only experiment snapshots must release SQLite before a trial resets its own folders."""
from contextlib import closing
import sqlite3

import pytest

from experiments.shared_runtime.image_config import project_snapshot
from experiments.shared_runtime.paid_probe import usage


@pytest.mark.parametrize('reader', [project_snapshot, usage])
@pytest.mark.parametrize('invalid_schema', [False, True])
def test_snapshot_releases_its_connection_before_return_or_error(tmp_path, monkeypatch, reader, invalid_schema):
    with closing(sqlite3.connect(tmp_path / 'state.db')) as db:
        if not invalid_schema:
            db.executescript('''
                CREATE TABLE delegated_projects(subagent_id TEXT,session_id TEXT,project_key TEXT);
                CREATE TABLE delegated_project_messages(subagent_id TEXT,state TEXT,published INTEGER);
                CREATE TABLE messages(content TEXT);
                CREATE TABLE session_model_usage(api_call_count INTEGER,input_tokens INTEGER,output_tokens INTEGER);
                INSERT INTO session_model_usage VALUES(1,20,5);
            ''')
    actual_connect, handles = sqlite3.connect, []

    def connect(*args, **kwargs):
        connection = actual_connect(*args, **kwargs)
        handles.append(connection)  # keep a reference so GC cannot hide a missing close
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    try:
        if invalid_schema:
            with pytest.raises(sqlite3.OperationalError, match='no such table'):
                reader(tmp_path)
        else:
            result = reader(tmp_path)
            assert result['apiCalls' if reader is usage else 'historyCount'] == (1 if reader is usage else 0)
        assert handles
        for handle in handles:
            with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
                handle.execute('SELECT 1')
    finally:
        for handle in handles:
            handle.close()
