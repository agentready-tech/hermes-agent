"""Accepted Unix inputs survive owner loss and are retired only after canonical history."""

import json
import tempfile
from pathlib import Path


def test_accepted_input_survives_crash_and_history_commit_window(tmp_path, monkeypatch, request):
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.adapters.turns import ActiveHomeTurn, forward_to_active_turn
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_state import SessionDB

    directory = tempfile.TemporaryDirectory(prefix="hi-")
    request.addfinalizer(directory.cleanup)
    tmp_path = Path(directory.name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(hermes, "_home", tmp_path)
    (tmp_path / "state").mkdir()
    ensure_home_session()
    inbox = HomeInbox()
    event = '[AgentReady event]\n' + json.dumps({"source": "mail", "event_id": "mail-42"}) + '\n\nNew delivery date is Monday'
    original = ActiveHomeTurn()
    try:
        assert forward_to_active_turn(event)
        assert len(inbox.pending()) == 1
        assert forward_to_active_turn(event)  # retry of the accepted source event
        assert len(inbox.pending()) == 1
        assert original.take() == [event]
    finally:
        original.finish(error="simulated owner death before persistence")
    assert len(inbox.pending()) == 1
    resumed = ActiveHomeTurn()
    try:
        assert resumed.take() == [event]
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.append_message("agentready_home", "user", event, platform_message_id=inbox.pending()[0][0])
        finally:
            db.close()
    finally:
        resumed.finish(result={"final_response": "Delivery revised"})
    assert inbox.pending() == []
    # An old identical user row cannot acknowledge a newly accepted plain message.
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.append_message("agentready_home", "user", "Check the delivery again")
        identity, pending = inbox.enqueue("Check the delivery again")
        assert pending
        inbox.acknowledge_recorded()
        assert inbox.pending() == [(identity, "Check the delivery again")]
        duplicate, _ = inbox.enqueue("Check the delivery again")
        db.append_message("agentready_home", "user", "Check the delivery again", platform_message_id=identity)
        inbox.acknowledge_recorded()  # crash after canonical append, before finish()
        assert inbox.pending() == [(duplicate, "Check the delivery again")]
        db.append_message("agentready_home", "user", "Check the delivery again", display_metadata={"agentready_inbox_ids": [duplicate]})
        inbox.acknowledge_recorded()
        assert inbox.pending() == []
    finally:
        db.close()
