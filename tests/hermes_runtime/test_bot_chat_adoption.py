"""Bot Mode must adopt the managed home without forking its durable conversation."""

from pathlib import Path

import pytest


@pytest.fixture
def legacy_home(monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters import hermes
    from hermes_state import SessionDB

    home = Path(get_hermes_home()).resolve()
    monkeypatch.setattr(hermes, "_home", home)
    db = SessionDB()
    db.create_session("agentready_home", source="telegram", cwd=str(home / "workspace"))
    db.set_session_title("agentready_home", "AgentReady Home")
    db.append_message("agentready_home", "user", "Continue the existing project")
    db.append_message("agentready_home", "assistant", "The existing finding")
    try:
        yield home, db
    finally:
        db.close()


def _desktop_chat(db):
    db.create_session("desktop-chat", source="desktop")
    db.set_session_title("desktop-chat", "Bot Chat")
    db.set_session_hidden("desktop-chat", True)
    db.append_message("desktop-chat", "assistant", "Keep this separate conversation too")


@pytest.mark.parametrize("desktop_chat", [False, True])
def test_native_bot_lookup_adopts_main_and_preserves_history_and_ingress(legacy_home, desktop_chat):
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.state import current_home_session_id, ensure_home_session
    from hermes_runtime.standalone.events import CRON_ORIGIN, admit_cron, enqueue
    from tui_gateway import server

    home, db = legacy_home
    if desktop_chat:
        _desktop_chat(db)
    db.create_session("project-child", source="subagent", parent_session_id="agentready_home",
                      model_config={"_delegate_from": "agentready_home"})
    db.append_message("project-child", "assistant", "Persistent project finding")
    child = db.get_session("project-child")
    transcripts = {sid: db.get_messages(sid, include_inactive=True) for sid in
                   ("agentready_home", "project-child", *(["desktop-chat"] if desktop_chat else []))}
    enqueue("Telegram follow-up", "telegram", "tg-followup")
    enqueue("Email follow-up", "gmail", "mail-followup")
    admitted = admit_cron({"id": "routine", "prompt": "Scheduled follow-up",
                          "origin": {"platform": CRON_ORIGIN}}, execution_id="tick")
    assert admitted[0] is True
    pending = HomeInbox().pending()

    ensure_home_session()
    ensure_home_session()  # a process restart must not move the Bot Chat elsewhere

    canonical = server._canonical_session_row(db, home)
    listed = server._session_list_by_title("bot-click", db, "Bot Chat")["result"]["sessions"]
    assert canonical["id"] == canonical["resolved_id"] == "agentready_home"
    assert [row["id"] for row in listed] == ["agentready_home"]
    assert current_home_session_id() == "agentready_home"
    assert db.resolve_resume_session_id("agentready_home") == "agentready_home"
    assert db.get_session("agentready_home")["source"] == "telegram"
    assert db.get_session("agentready_home")["hidden"]
    assert db.get_session("project-child") == child
    assert HomeInbox().pending() == pending
    for sid, messages in transcripts.items():
        assert db.get_messages(sid, include_inactive=True) == messages
    if desktop_chat:
        previous = db.get_session("desktop-chat")
        assert previous["title"] != "Bot Chat"
        assert not previous["hidden"] and not previous["archived"]
    with pytest.raises(ValueError, match="canonical Bot Chat"):
        db.set_session_title("agentready_home", "Accidentally detach the bot")


def test_adoption_keeps_an_existing_compression_tip_canonical(legacy_home):
    from hermes_runtime.adapters.state import ensure_home_session
    from tui_gateway import server

    home, db = legacy_home
    db.end_session("agentready_home", "compression")
    db.create_session("main-tip", source="telegram", parent_session_id="agentready_home")
    db.append_message("main-tip", "assistant", "Continue from the compacted history")
    db.set_session_title("main-tip", "Bot Chat")
    db.set_session_hidden("main-tip", True)
    before = db.get_messages("main-tip", include_inactive=True)

    ensure_home_session()
    ensure_home_session()

    canonical = server._canonical_session_row(db, home)
    assert canonical["id"] == canonical["resolved_id"] == "main-tip"
    assert db.get_session("main-tip")["parent_session_id"] == "agentready_home"
    assert db.get_session("agentready_home")["hidden"]
    assert db.get_messages("main-tip", include_inactive=True) == before


def test_failed_adoption_rolls_back_the_previous_chat_identity(legacy_home, monkeypatch):
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_state import SessionDB

    _, db = legacy_home
    _desktop_chat(db)
    before = {sid: db.get_session(sid) for sid in ("agentready_home", "desktop-chat")}
    original = SessionDB._set_session_title_in_transaction

    def unavailable(self, conn, session_id, title, *, source):
        if session_id == "agentready_home" and title == "Bot Chat":
            raise RuntimeError("Cannot publish the main chat")
        return original(self, conn, session_id, title, source=source)

    monkeypatch.setattr(SessionDB, "_set_session_title_in_transaction", unavailable)
    with pytest.raises(RuntimeError, match="Cannot publish"):
        ensure_home_session()
    assert {sid: db.get_session(sid) for sid in before} == before
