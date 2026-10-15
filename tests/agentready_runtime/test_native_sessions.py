"""Managed home continuity must not capture independent native sessions."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_home_lineage_keeps_delegations_and_branches_independent(monkeypatch):
    from agentready_runtime.adapters import hermes
    from agentready_runtime.adapters.state import belongs_to_home_lineage, ensure_home_session
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory(prefix="ar-") as directory:
        home = Path(directory)
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(hermes, "_home", home)
        ensure_home_session()
        db = SessionDB()
        try:
            db.create_session("delegate", source="subagent", parent_session_id="agentready_home",
                              model_config={"_delegate_from": "agentready_home"})
            db.create_session("branch", source="cli", parent_session_id="agentready_home",
                              model_config={"_branched_from": "agentready_home"})
            db.create_session("fresh", source="cli")
            for sid in ("delegate", "branch", "fresh"):
                db.append_message(sid, "user", "Independent work: " + sid)
                assert not belongs_to_home_lineage(sid), sid
                assert db.resolve_resume_session_id(sid) == sid
            db.end_session("agentready_home", "compression")
            db.create_session("compressed", source="agentready", parent_session_id="agentready_home")
            assert belongs_to_home_lineage("compressed")
            # Child kind remains authoritative even if its parent was subsequently compressed.
            assert not belongs_to_home_lineage("delegate")
            assert not belongs_to_home_lineage("branch")
        finally:
            db.close()


@pytest.mark.parametrize("command", ["reset", "resume", "branch"])
def test_managed_gateway_keeps_native_manual_session_commands(monkeypatch, command):
    from agentready_runtime.adapters import hermes
    from gateway.slash_commands_session import GatewaySessionCommandsMixin
    from hermes_constants import get_hermes_home

    class NativeResetBoundary(Exception):
        pass

    def session_key(source):
        raise NativeResetBoundary()

    host = SimpleNamespace(_session_db=None, _session_key_for_source=session_key,
                           _session_db_unavailable_reply=lambda: "Native session storage unavailable")
    event = SimpleNamespace(source=object())
    handler = getattr(GatewaySessionCommandsMixin, "_handle_" + command + "_command")
    for managed in (False, True):
        monkeypatch.setattr(hermes, "_home", Path(get_hermes_home()).resolve() if managed else None)
        if command == "reset":
            with pytest.raises(NativeResetBoundary):
                asyncio.run(handler(host, event))
        else:
            assert asyncio.run(handler(host, event)) == "Native session storage unavailable"
