"""Independent profile parents may share an id without sharing children or limits."""
from contextlib import contextmanager
import threading

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import async_delegation as ad
from tools.process_registry import process_registry


@contextmanager
def home_scope(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def test_profile_capacity_roster_and_stop_are_independent(tmp_path):
    ad._reset_for_tests()
    homes = {name: tmp_path / name for name in ('alpha', 'bravo')}
    release = threading.Event()
    interrupted = {name: threading.Event() for name in homes}
    handles = {}

    def dispatch(name):
        def run():
            assert release.wait(15)
            return {'status': 'completed', 'summary': name + '-own-result'}
        return ad.dispatch_async_delegation(goal=name + '-task', context=None, toolsets=None,
            role='leaf', model='fixture-model', session_key='agent:' + name + ':agentready:home',
            parent_session_id='agentready_home', runner=run, max_async_children=1,
            interrupt_fn=interrupted[name].set)

    try:
        for name, home in homes.items():
            home.mkdir()
            with home_scope(home):
                handles[name] = dispatch(name)
                assert handles[name]['status'] == 'dispatched'
        for name, home in homes.items():
            with home_scope(home):
                assert {r['delegation_id'] for r in ad.list_async_delegations()} == {
                    handles[name]['delegation_id']}
                assert ad.has_live_for_session(parent_session_id='agentready_home')
                assert dispatch(name)['status'] == 'rejected'
        with home_scope(homes['alpha']):
            assert ad.interrupt_for_session(parent_session_id='agentready_home') == 1
        assert interrupted['alpha'].is_set()
        assert not interrupted['bravo'].is_set()
    finally:
        release.set()
        if ad._executor is not None:
            ad._executor.shutdown(wait=True)
        ad._reset_for_tests()
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()
