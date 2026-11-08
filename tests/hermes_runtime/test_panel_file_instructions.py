"""The real main first-request suffix advertises only implemented native delivery."""
from contextlib import nullcontext
import pytest
from tests.hermes_runtime.test_schedule_instructions import _agent

pytest_plugins = ["tests.hermes_runtime.test_schedule_instructions"]

@pytest.mark.parametrize('kind', ['main', 'child', 'isolated', 'ordinary-profile'])
def test_real_first_api_request_has_file_delivery_only_for_managed_main(runtime, monkeypatch, kind):
    from agent.delegation_context import delegated_child_context
    from hermes_runtime.adapters import hermes
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.main_files import HINT
    _, db, seen = runtime
    sid = 'separate-session' if kind == 'isolated' else HOME_SESSION_ID
    if kind == 'ordinary-profile': monkeypatch.setattr(hermes, '_home', None)
    with delegated_child_context(sid) if kind == 'child' else nullcontext():
        agent = _agent(db, sid)
    try:
        assert (HINT in (agent.ephemeral_system_prompt or '')) == (kind == 'main')
        agent.run_conversation('Please deliver the generated file')
        assert (HINT in seen[0]['messages'][0]['content']) == (kind == 'main')
    finally: agent.close()
