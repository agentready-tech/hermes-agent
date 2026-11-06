"""Identical logical session ids must not expose another profile's masked prompt."""
from concurrent.futures import ThreadPoolExecutor

from gateway.session_events import SessionEvents
from gateway import session_sudo_prompt as prompts
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_masked_request_replay_and_response_are_owned_by_home(tmp_path):
    from tests.waiting import wait_until
    from types import SimpleNamespace

    alpha, bravo = tmp_path / 'alpha', tmp_path / 'bravo'
    alpha.mkdir(); bravo.mkdir()
    events, frames = SessionEvents(), []
    events.fanout.attach(SimpleNamespace(write=lambda frame: frames.append(frame) or True))

    def ask():
        token = set_hermes_home_override(alpha)
        try:
            return prompts.ask_viewer(events, 'agentready_home', 'secret', {'label': 'fixture'}, timeout=5)
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(ask)
        wait_until(lambda: bool(frames), timeout=3, what='owned masked prompt')
        frame = {'id': frames[0]['id'], 'result': {'value': 'synthetic-answer'}}
        token = set_hermes_home_override(bravo)
        try:
            assert prompts.open_requests('agentready_home') == []
            assert not prompts.resolve(frame)
        finally:
            reset_hermes_home_override(token)
            token = set_hermes_home_override(alpha)
            try:
                assert len(prompts.open_requests('agentready_home')) == 1
                assert prompts.resolve(frame)
            finally:
                reset_hermes_home_override(token)
        assert pending.result(timeout=3) == 'synthetic-answer'
