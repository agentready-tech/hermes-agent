"""Managed messaging must not inherit a canonical local viewer's identity or queue."""
from pathlib import Path
from types import SimpleNamespace

import pytest


def managed_home(monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session

    home = Path(get_hermes_home())
    monkeypatch.setattr(hermes, "_home", home.resolve())
    ensure_home_session()
    return home


def runner(home):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    store = SessionStore(home / "sessions", GatewayConfig())
    return SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False)


async def local_chat(host, home, monkeypatch):
    from gateway import run
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session

    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}})
    authority = await initialize_session_authority(host, profile_id=str(home), instance_id="fixture")
    actor = Principal("owner", str(home), frozenset({"session:create", "session:read"}), "socket")
    params = {"request_id": "web-chat", "source": "tui", "cwd": str(home), "model": "fixture", "toolsets": []}
    ref = create_local_session(authority, actor, params)
    return authority, ref, actor, params


def telegram_source():
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.TELEGRAM, chat_id="1", user_id="1", chat_type="dm")


@pytest.mark.asyncio
async def test_web_chat_cannot_capture_the_managed_messaging_route(monkeypatch):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY

    home = managed_home(monkeypatch)
    host = runner(home)
    authority, ref, _, _ = await local_chat(host, home, monkeypatch)
    entry = host.session_store.get_or_create_session(telegram_source())
    assert entry.session_id == HOME_SESSION_ID
    assert entry.session_key == HOME_SESSION_KEY
    local = authority.sessions[ref.session_id]
    assert local.route != HOME_SESSION_KEY
    other = host.session_store.get_or_create_session(
        SessionSource(platform=Platform.LOCAL, chat_id="another-local", user_id="owner", chat_type="dm"))
    assert other.session_id not in {HOME_SESSION_ID, ref.session_id}


@pytest.mark.asyncio
async def test_legacy_collision_preserves_local_history_and_recovers_unstarted_messaging(monkeypatch):
    from gateway.session import SessionStore, build_session_key
    from gateway.session_local import create_local_session
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    from hermes_state_local import local_receipt
    from hermes_state_runtime import admit_session_input, get_session_admission, list_session_admissions

    home = managed_home(monkeypatch)
    host = runner(home)
    # Reproduce the shipped collision through real native local creation.
    with monkeypatch.context() as legacy:
        legacy.setattr(SessionStore, "_generate_session_key", lambda *a, **kw: HOME_SESSION_KEY)
        authority, ref, actor, params = await local_chat(host, home, monkeypatch)
    db = authority.db
    db.append_message(HOME_SESSION_ID, "assistant", "MAIN_HISTORY")
    db.append_message(ref.session_id, "assistant", "LOCAL_HISTORY")
    payload = {"text": "Accepted Telegram work", "native_text_v1": {
        "source": telegram_source().to_dict(), "route": HOME_SESSION_KEY, "event": {}, "timestamp": "fixture"}}
    original = admit_session_input(db, epoch=authority.epoch, principal_id="telegram:1",
        session_id=ref.session_id, request_id="telegram-message", payload=payload)
    # A previously started claim must never be replayed by the migration.
    uncertain = admit_session_input(db, epoch=authority.epoch, principal_id="telegram:1",
        session_id=ref.session_id, request_id="uncertain", payload=payload)
    db._write_sql("UPDATE session_admissions SET status='unknown',generation=1 WHERE admission_id=?",
                  (uncertain["admission_id"],))

    ensure_home_session()
    ensure_home_session()  # restart/idempotency
    expected = build_session_key(authority.sessions[ref.session_id].source)
    assert local_receipt(db, ref.session_id)["route"] == expected
    assert db.get_session(ref.session_id)["session_key"] == expected
    assert db.get_messages(ref.session_id)[0]["content"] == "LOCAL_HISTORY"
    assert db.get_messages(HOME_SESSION_ID)[0]["content"] == "MAIN_HISTORY"
    assert get_session_admission(db, admission_id=original["admission_id"])["outcome"] == "cancelled"
    assert get_session_admission(db, admission_id=uncertain["admission_id"])["status"] == "unknown"
    recovered = list_session_admissions(db, session_id=HOME_SESSION_ID)
    assert len(recovered) == 1 and recovered[0]["payload"] == payload
    assert recovered[0]["request_id"] == original["request_id"]

    cold = runner(home)
    restored, _, _, _ = await local_chat(cold, home, monkeypatch)
    assert create_local_session(restored, actor, params) == ref
    assert cold.session_store.get_or_create_session(telegram_source()).session_id == HOME_SESSION_ID
