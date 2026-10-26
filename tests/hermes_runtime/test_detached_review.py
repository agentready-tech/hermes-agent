"""A cache-sharing maintenance fork cannot become the persistent conversation's owner."""

import os
import threading
from pathlib import Path
from types import SimpleNamespace

from tests.waiting import wait_until


def test_detached_review_does_not_own_main_inbox_or_delay_followup(monkeypatch):
    from agent.memory_provider import spawn_context_thread
    from hermes_runtime.adapters import hermes, turns
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.standalone.events import enqueue
    from hermes_state import SessionDB

    home = Path(os.environ["HERMES_HOME"]).resolve()
    monkeypatch.setattr(hermes, "_home", home)
    ensure_home_session()
    db = SessionDB()
    db.append_message("agentready_home", "user", "Keep existing history")
    db.append_message("agentready_home", "assistant", "Previous answer")
    db.close()
    review = SimpleNamespace(session_id="agentready_home", _persist_disabled=True)
    main = SimpleNamespace(session_id="agentready_home")
    review_started, release_review, main_finished = (threading.Event() for _ in range(3))
    errors = []

    def maintenance(*_args, **_kwargs):
        review_started.set()
        assert release_review.wait(10)
        return {"messages": [], "final_response": "review finished"}

    def answer(*_args, **_kwargs):
        records = main._agentready_active_turn.take_records()
        assert len(records) == 1
        assert "Follow-up while reviewing" in records[0][1]
        database = SessionDB()
        try:
            database.append_message("agentready_home", "user", records[0][1], platform_message_id=records[0][0])
            database.append_message("agentready_home", "assistant", "Follow-up answered")
        finally:
            database.close()
        return {"messages": [], "final_response": "Follow-up answered"}

    def run(agent, callback):
        try:
            turns.run_conversation(agent, callback, "Continue")
            if agent is main:
                main_finished.set()
        except BaseException as error:
            errors.append(error)

    background = spawn_context_thread(lambda: run(review, maintenance), name="test-detached-review")
    foreground = spawn_context_thread(lambda: run(main, answer), name="test-main-followup")
    try:
        background.start()
        assert review_started.wait(5)
        enqueue("Follow-up while reviewing", "telegram", "followup-during-review")
        foreground.start()
        assert main_finished.wait(3), "maintenance must not delay the main conversation"
        assert not release_review.is_set()
        assert getattr(review, "_agentready_active_turn", None) is None
        assert wait_until(lambda: not HomeInbox().pending(), timeout=3)
        database = SessionDB()
        try:
            messages = database.get_messages("agentready_home")
        finally:
            database.close()
        assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[0]["content"] == "Keep existing history"
        assert messages[-1]["content"] == "Follow-up answered"
    finally:
        release_review.set()
        background.join(5)
        if foreground.ident is not None:
            foreground.join(5)
    assert not background.is_alive() and not foreground.is_alive()
    assert not errors
