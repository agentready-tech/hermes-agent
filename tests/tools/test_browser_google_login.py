"""Native settings control with synthetic profiles; no browser/provider/model is contacted."""
import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.bot_desktop import browser, browser_launch, browser_login, lease, runtime
from hermes_cli.web_routers import display
from hermes_constants import hermes_home_key


@pytest.fixture
def own(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    lease._reset_for_tests()
    token = display._ViewerStreams(active=1, issued=1, opened=1)
    display._viewer_streams[(hermes_home_key(), "synthetic-viewer")] = token
    monkeypatch.setattr(browser, "open_google", Mock(return_value=True))
    yield tmp_path
    display._viewer_streams.clear()
    lease._reset_for_tests()


def test_fixed_action_is_atomic_and_idempotent_per_human_lease_without_raw_capability_receipt(own):
    first = browser_login.open_google("synthetic-viewer", 0)
    assert first["opened"] and first["lease"]["holder"] == "human"
    assert first["lease"]["viewer_id"] is None and "expires_at" not in first["lease"]
    assert browser_login.open_google("synthetic-viewer", 0) == first
    assert browser_login.open_google("synthetic-viewer", first["lease"]["epoch"]) == first
    browser.open_google.assert_called_once()
    raw = (own / "bot-desktop/google-login.json").read_text()
    assert "synthetic-viewer" not in raw and hashlib.sha256(b"synthetic-viewer").hexdigest() in raw


def test_active_foreign_human_and_stale_epoch_refuse_without_eviction_or_browser_action(own):
    held = lease.acquire("foreign-viewer")
    with pytest.raises(browser_login.BrowserLoginError, match="control_changed"):
        browser_login.open_google("synthetic-viewer", held.epoch)
    assert lease.get().viewer_id == "foreign-viewer"
    lease.release("foreign-viewer")
    with pytest.raises(browser_login.BrowserLoginError, match="control_changed"):
        browser_login.open_google("synthetic-viewer", held.epoch)
    browser.open_google.assert_not_called()


def test_no_rfb_or_replaced_generation_refuses_before_native_launch(own, monkeypatch):
    display._viewer_streams.clear()
    with pytest.raises(browser_login.BrowserLoginError, match="screen_not_connected"):
        browser_login.open_google("synthetic-viewer", 0)
    assert lease.get().holder == lease.AGENT
    monkeypatch.setattr(display, "connected_viewer_generation", Mock(side_effect=[1, 2]))
    with pytest.raises(browser_login.BrowserLoginError, match="control_changed"):
        browser_login.open_google("synthetic-viewer", 0)
    browser.open_google.assert_not_called()


def test_ambiguous_launch_failure_is_not_blindly_retried_and_existing_profile_survives(own):
    sentinel = own / "profile-sentinel"; sentinel.write_text("preserve")
    browser.open_google.side_effect = RuntimeError("synthetic private error never relayed")
    for epoch in (0, lease.get().epoch + 1):
        with pytest.raises(browser_login.BrowserLoginError, match="outcome_unknown"):
            browser_login.open_google("synthetic-viewer", epoch)
    browser.open_google.assert_called_once()
    assert sentinel.read_text() == "preserve"


def test_corrupt_human_authority_and_unknown_receipt_fail_closed(own):
    path = own / "bot-desktop/lease.json"; path.parent.mkdir(exist_ok=True); path.write_text("unknown")
    with pytest.raises(browser_login.BrowserLoginError, match="control_changed"):
        browser_login.open_google("synthetic-viewer", 0)
    browser.open_google.assert_not_called()
    path.unlink()
    receipt = path.with_name("google-login.json"); receipt.write_text("unknown")
    with pytest.raises(browser_login.BrowserLoginError, match="receipt_unavailable"):
        browser_login.open_google("synthetic-viewer", 0)
    assert receipt.read_text() == "unknown"


def test_native_rpc_requires_connection_minted_viewer_and_closed_schema(own, monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(runtime, "rfb_socket_path", lambda: own / "rfb.sock")
    def call(method, params):
        return server.handle_request({"jsonrpc": "2.0", "id": 8, "method": method, "params": params})
    mine = call("display.observe", {})["result"]["viewer_id"]
    display._viewer_streams[(hermes_home_key(), mine)] = display._ViewerStreams(active=1)
    for params in ({"viewer_id": "synthetic-viewer", "expected_epoch": 0},
                   {"viewer_id": mine, "expected_epoch": 0, "url": "https://invalid.invalid"},
                   {"viewer_id": mine, "expected_epoch": 0, "profile": "foreign"},
                   {"viewer_id": mine, "expected_epoch": True}):
        assert "error" in call("display.browser.google_login", params)
    assert call("display.browser.google_login", {"viewer_id": mine, "expected_epoch": 0})["result"]["opened"]
    browser.open_google.assert_called_once()


@pytest.mark.parametrize("controls,configures", [(False, False), (True, False), (False, True)])
def test_native_reverse_filter_requires_both_permissions(controls, configures):
    from hermes_runtime.standalone.panel import screen_request
    raw = json.dumps({"id": 1, "method": "display.browser.google_login", "params": {"viewer_id": "synthetic", "expected_epoch": 0}})
    with pytest.raises(ValueError):
        screen_request(raw, can_control=controls, can_configure=configures)
    assert json.loads(screen_request(raw, can_control=True, can_configure=True))["params"]["expected_epoch"] == 0


def test_launcher_forwards_fixed_address_only_to_admitted_existing_owner(own, monkeypatch):
    argv = ["/synthetic/chrome", f"--user-data-dir={own / 'browser-profile'}"]
    monkeypatch.setattr(browser_launch, "_fence", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(browser_launch.instance, "inspect_profile", lambda *a, **k: {"state": "attachable", "port": 9222})
    monkeypatch.setattr(browser_launch.instance, "_lock", lambda *a: {"pid": 123})
    monkeypatch.setattr(browser_launch.instance, "_generation", lambda *a: "generation")
    monkeypatch.setattr(browser_launch, "_owner", Mock(return_value=({}, {})))
    forward = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(browser_launch.subprocess, "run", forward)
    spawn = Mock(side_effect=AssertionError("An existing browser must not be spawned again"))
    monkeypatch.setattr(browser_launch.subprocess, "Popen", spawn)
    assert browser_launch.open_google(argv) == {"opened": True}
    assert forward.call_args.args[0] == [*argv, "--new-tab", "https://accounts.google.com/"]
    spawn.assert_not_called()
    monkeypatch.setattr(browser_launch.instance, "inspect_profile", lambda *a, **k: {"state": "blocked"})
    with pytest.raises(browser_launch.instance.ProfileUnavailable): browser_launch.open_google(argv)
    assert forward.call_count == 1


def test_absent_browser_uses_existing_launcher_once_with_google_as_initial_normal_page(own, monkeypatch):
    argv = ["/synthetic/chrome", f"--user-data-dir={own / 'browser-profile'}"]
    monkeypatch.setattr(browser_launch, "_fence", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(browser_launch.instance, "inspect_profile", lambda *a, **k: {"state": "absent"})
    monkeypatch.setattr(browser_launch, "_positive_port", lambda: 9222)
    spawn = Mock(return_value=object()); monkeypatch.setattr(browser_launch.subprocess, "Popen", spawn)
    monkeypatch.setattr(browser_launch, "_wait_ready", lambda *a: {"state": "attachable", "port": 9222})
    forward = Mock(side_effect=AssertionError("No duplicate Google tab")); monkeypatch.setattr(browser_launch.subprocess, "run", forward)
    assert browser_launch.open_google(argv) == {"opened": True}
    assert spawn.call_count == 1 and spawn.call_args.args[0][-1] == "https://accounts.google.com/"
    assert "--new-window" not in spawn.call_args.args[0]
    forward.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rpc", "rfb"])
async def test_settings_intent_selects_only_explicit_own_gateway_authority(kind, tmp_path):
    from hermes_runtime.standalone.panel import bridge
    own = SimpleNamespace(home=tmp_path, gateway_owned=True, endpoint=Mock(),
        rpc_connection=Mock(side_effect=ValueError("synthetic-before-network")),
        display_url=Mock(side_effect=ValueError("synthetic-before-network")))
    ambient = Mock(); ambient.gateway_owned = False
    command = {"id":"synthetic", "kind":kind,"googleLogin":True,"canControl":True,"canConfigure":True,"ticket":"synthetic"}
    with pytest.raises(ValueError, match="synthetic-before-network"):
        await bridge(command, {"url":"http://127.0.0.1:1","environment_id":"env_fixture"}, 1, "synthetic", ambient, google_auth=own)
    (own.rpc_connection if kind == "rpc" else own.display_url).assert_called_once()
    assert ambient.mock_calls == []
    own.endpoint.side_effect = ValueError("own-gateway-not-ready")
    with pytest.raises(ValueError, match="own-gateway-not-ready"):
        await bridge(command, {}, 1, "synthetic", ambient, google_auth=own)
    assert ambient.mock_calls == []


def test_remote_browser_unavailable_never_falls_back_to_host(monkeypatch):
    from tools.bot_desktop import placement
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL,"kubernetes"))
    monkeypatch.setattr(runtime, "_owned_sandbox_env", lambda: None)
    host = Mock(side_effect=AssertionError("host fallback forbidden"));monkeypatch.setattr(browser,"executable",host)
    with pytest.raises(RuntimeError, match="own desktop environment is unavailable"):browser.open_google()
    host.assert_not_called()


def test_remote_fixed_open_uses_same_native_profile_user_launcher_and_headroom_gate(monkeypatch):
    from tools.bot_desktop import sandbox_host, browser_memory_guard
    env = object(); gate = Mock(); run = Mock(return_value=SimpleNamespace(returncode=0,stdout=b'{"opened":true}'))
    monkeypatch.setattr(sandbox_host,"chromium_executable",lambda e:'/native/chrome')
    monkeypatch.setattr(sandbox_host,"desktop_home",lambda e:'/own-remote-home')
    monkeypatch.setattr(sandbox_host,"browser_launch_path",lambda e:'/own-remote-home/native/browser_launch.py')
    monkeypatch.setattr(sandbox_host,"chromium_sandbox_bypass",lambda e:False)
    monkeypatch.setattr(sandbox_host,"_user_for",lambda e:'hermes')
    monkeypatch.setattr(sandbox_host.streams,"run_in",run)
    monkeypatch.setattr(browser_memory_guard,"before_tool",gate)
    monkeypatch.setattr(browser_memory_guard,"executable",lambda e,f:'/own-remote-home/guarded-chrome')
    monkeypatch.setattr(runtime,"published_env",lambda:{'DISPLAY':':7'})
    monkeypatch.setattr(browser,"proxy_args",list)
    assert sandbox_host.open_google(env)
    gate.assert_called_once_with(env)
    assert run.call_args.args == (env,['python3','/own-remote-home/native/browser_launch.py'])
    request=json.loads(run.call_args.kwargs['stdin'])
    assert request['open_google'] is True and request['argv'][0]=='/own-remote-home/guarded-chrome'
    assert '--user-data-dir=/own-remote-home/.hermes/bot-desktop/browser-profile' in request['argv']
    assert run.call_args.kwargs['user']=='hermes' and run.call_args.kwargs['child_env']=={'DISPLAY':':7'}


def test_native_rpc_does_not_expose_private_filesystem_failure(own, monkeypatch):
    from tui_gateway import server
    import utils
    monkeypatch.setattr(runtime,"rfb_socket_path",lambda:own/'rfb.sock')
    def call(method,params):return server.handle_request({'jsonrpc':'2.0','id':9,'method':method,'params':params})
    viewer=call('display.observe',{})['result']['viewer_id']
    display._viewer_streams[(hermes_home_key(),viewer)]=display._ViewerStreams(active=1)
    monkeypatch.setattr(utils,'atomic_write_text',Mock(side_effect=OSError('synthetic private path must not cross RPC')))
    reply=call('display.browser.google_login',{'viewer_id':viewer,'expected_epoch':0})
    assert reply['error']['data']['code']=='browser_login_unavailable'
    assert 'synthetic private' not in json.dumps(reply)
    browser.open_google.assert_not_called()
