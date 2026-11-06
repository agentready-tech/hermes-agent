"""Control turns keep the native delivery owner after a synthetic source acquires identity."""
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.hermes_runtime.test_shared_profile_host']


@pytest.mark.parametrize('name', ['alpha', 'bravo'])
def test_inferred_identity_uses_the_registered_delivery_owner(shared_homes, name, monkeypatch):
    from dataclasses import replace
    import hermes_state
    from experiments.shared_runtime.transport import install
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.session import SessionSource
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_contract import SessionRef
    from gateway.session_identity import resolve_identity
    from gateway.session_ingress_context import register_transport_home
    from hermes_runtime.adapters.main_control_origin import capture_origin
    from hermes_runtime.adapters.state import ensure_home_session

    root, homes = shared_homes
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    config = GatewayConfig(multiplex_profiles=True, sessions_dir=root / 'sessions')
    config._runtime_profile_homes = tuple([('default', root), *homes.items()])
    runner = GatewayRunner(config)
    runner._primary_profile_name = 'default'
    registry = runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    install()
    runner._profile_adapters = {}
    for owner, home in homes.items():
        (home / '.env').write_text('TELEGRAM_ALLOWED_USERS=9001\n')
        registry.add(home, SimpleNamespace(db=SimpleNamespace(db_path=home / 'state.db')), name=owner)
        adapter = runner._instantiate_adapter(Platform.TELEGRAM, PlatformConfig(enabled=True, token='fixture-' + owner))
        adapter.set_owner_profile(owner)
        runner._profile_adapters[owner] = {Platform.TELEGRAM: adapter}
        register_transport_home(runner, owner, home)
    try:
        with _profile_runtime_scope(homes[name]):
            ensure_home_session()
            source = SessionSource(platform=Platform.TELEGRAM, profile=name, chat_id=name, user_id='9001')
            authority = SimpleNamespace(profile_id=str(homes[name]), runner=runner)
            ref = SessionRef(authority.profile_id, 'agentready_home')
            before = capture_origin(authority, ref, source)
            # This is the native resolver a control-created turn enters without a platform callback.
            identity = resolve_identity(source, runner=runner)
            assert identity.transport_inferred and identity.runtime_profile == name
            assert runner._adapter_for_source(source) is runner._profile_adapters[name][Platform.TELEGRAM]
            after = capture_origin(authority, ref, source)
            assert source._identity is identity  # capture never rewrites the running source
            assert before['native_origin_v1']['provenance'] == after['native_origin_v1']['provenance']
            assert after['native_origin_v1']['provenance']['transport_profile'] == name
            source._identity = replace(identity, transport_inferred=False)
            assert capture_origin(authority, ref, source) == {}  # a known absent receiver cannot borrow this bot
    finally:
        runner.session_store.close_all_db_handles()
