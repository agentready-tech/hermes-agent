"""Second-review regressions: shared config transaction, request-boundary receipt, mixed version, fence lifetime."""

import asyncio
import json
import types
from threading import Event, Thread

import pytest
import hermes_yaml

from hermes_runtime.standalone.agy_selection import read_selection, selection_config
from providers.agy_policy import AgyError, MODEL, PROVIDER
from tests.hermes_runtime.test_panel_agy_login import VIEWER, setup

pytestmark = pytest.mark.platforms("posix")
REVISION = "a" * 32


def _read(home):
    return hermes_yaml.safe_load((home / "config.yaml").read_text())


def _managed_config(home, *, generation=None, revision=REVISION):
    config = {"model": {"provider": "custom", "default": "managed-fixture", "key_env": "AGENTSEE_INFERENCE_KEY",
                        "base_url": "https://panel.fixture.invalid/app/api/agents/A/inference/v1"},
              "autonomous": {"panel": {"url": "https://panel.fixture.invalid", "environment_id": "A",
                                       "model_selection_revision": revision}}}
    if generation is not None:
        config["autonomous"]["panel"]["agy_selection"] = selection_config(config, generation)
    (home / "config.yaml").write_text(json.dumps(config))
    return config


def _writer(service, competitor):
    from hermes_cli import config, personality, update_channel
    from hermes_runtime.workspace.transport import profile_scope

    with profile_scope(service.home):
        if competitor == "config-set":
            config.set_config_value("display.skin", "fixture-skin")
        elif competitor == "personality":
            assert personality.persist_personality("fixture-personality")
        else:
            assert update_channel._write_channel_record("0123456789abcdef", "/fixture/channel", "stable")


def _assert_preserved(service, competitor, revision):
    saved = _read(service.home)
    assert saved["model"] == {"provider": PROVIDER, "default": MODEL}
    assert saved["autonomous"]["panel"]["model_selection_revision"] == revision
    assert saved["agent"]["max_turns"] == 77
    if competitor == "config-set":
        assert saved["display"]["skin"] == "fixture-skin"
    elif competitor == "personality":
        assert saved["display"]["personality"] == "fixture-personality"
    else:
        assert saved["update"]["installs"]["0123456789abcdef"]["channel"] == "stable"


@pytest.mark.asyncio
@pytest.mark.parametrize("competitor", ["config-set", "personality", "channel-record"])
async def test_native_writer_inside_the_panel_transaction_loses_nothing(tmp_path, monkeypatch, competitor):
    service, _, store, _ = setup(tmp_path, monkeypatch)
    assert store.promote(service.home, store.stage(service.home))
    base = _read(service.home)
    base["agent"] = {"max_turns": 77}
    (service.home / "config.yaml").write_text(json.dumps(base))
    write_ready, writer_started, writer_done = Event(), Event(), Event()
    original = service._write

    def paused_panel_write(data):
        write_ready.set()  # Panel holds the transaction after a fresh read and edit.
        assert writer_started.wait(5)
        assert not writer_done.is_set()
        original(data)

    def run_writer():
        assert write_ready.wait(5)
        writer_started.set()
        _writer(service, competitor)
        writer_done.set()

    monkeypatch.setattr(service, "_write", paused_panel_write)
    thread = Thread(target=run_writer)
    thread.start()
    _, revision, _ = service._save_selection("gemini", MODEL, False, None, store, store.active(service.home))
    thread.join(15)
    assert writer_done.is_set()
    _assert_preserved(service, competitor, revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("competitor", ["config-set", "personality", "channel-record"])
async def test_native_writer_started_before_the_panel_transaction_loses_nothing(tmp_path, monkeypatch, competitor):
    from hermes_cli.config import config_write_lock
    service, _, store, _ = setup(tmp_path, monkeypatch)
    assert store.promote(service.home, store.stage(service.home))
    config = _read(service.home)
    config["agent"] = {"max_turns": 77}
    (service.home / "config.yaml").write_text(json.dumps(config))
    writer_started, writer_done = Event(), Event()

    def run_writer():
        writer_started.set()
        _writer(service, competitor)
        writer_done.set()

    result = {}

    def run_panel():
        result["selected"] = service._save_selection("gemini", MODEL, False, None, store, store.active(service.home))

    # A third holder (here: the test) keeps both writers queued; each one attempts
    # its read only after this transaction is released.
    with config_write_lock(service.home / "config.yaml"):
        writer = Thread(target=run_writer); writer.start()
        panel = Thread(target=run_panel); panel.start()
        assert writer_started.wait(5)
        assert not writer_done.is_set()
    writer.join(15); panel.join(15)
    assert writer_done.is_set() and "selected" in result
    _assert_preserved(service, competitor, result["selected"][1])


@pytest.mark.asyncio
async def test_return_receipt_is_published_only_at_the_request_boundary(tmp_path, monkeypatch):
    from hermes_cli.config import set_config_value
    from hermes_runtime.workspace.transport import profile_scope

    service, _, store, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(service.home))
    assert store.promote(service.home, store.stage(service.home))
    config = _managed_config(service.home, generation="fixture-generation")
    receipt = service.home / "subscriptions" / "agy" / "native-applied.json"

    class Agent:
        provider, model, session_id = "custom", "managed-fixture", "agentready_home"
        base_url = config["model"]["base_url"]

        def __init__(self):
            self._panel_selection = None

    agent = Agent()
    ticket = read_selection(service.home, config)
    assert ticket is not None and ticket.provider == "custom"
    ticket.attach(agent)
    assert agent._panel_selection is ticket and not receipt.exists(), "turn attach is not an applied request"

    with profile_scope(service.home):
        set_config_value("model.default", "fixture-newer-model")
    with pytest.raises(AgyError):
        ticket.publish_applied(agent)
    assert not receipt.exists(), "a stale revision must not leave an applied receipt"

    _managed_config(service.home, generation="fixture-generation")
    fresh = read_selection(service.home, _read(service.home))
    fresh.publish_applied(agent)
    assert json.loads(receipt.read_text())["revision"] == REVISION
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_legacy_viewer_never_receives_the_gemini_card(tmp_path, monkeypatch):
    service, _, store, _ = setup(tmp_path, monkeypatch)
    assert store.promote(service.home, store.stage(service.home))
    legacy = await service.handle("subscriptions.status", {})
    assert [row["id"] for row in legacy["providers"]] == ["claude", "openai"]
    bound = await service.handle("subscriptions.status", {}, viewer=VIEWER)
    gemini = [row for row in bound["providers"] if row["id"] == "gemini"]
    assert gemini and gemini[0]["capability"] == "gemini-antigravity-v1" and gemini[0]["connected"]


@pytest.mark.asyncio
async def test_disconnect_clears_the_stamp_and_both_neighbours_stay_untouched(tmp_path, monkeypatch):
    service, _, store, store_b = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(service.home))
    assert store.promote(service.home, store.stage(service.home))
    config = _managed_config(service.home, generation=store.active(service.home))
    ticket = read_selection(service.home, config)
    assert ticket is not None
    agent = type("Agent", (), {"provider": "custom", "model": "managed-fixture", "session_id": "agentready_home",
                               "base_url": config["model"]["base_url"]})()
    ticket.attach(agent)
    ticket.publish_applied(agent)
    assert ticket.was_applied()
    before_b = (store_b.home / "config.yaml").read_bytes()
    assert await service.handle("subscriptions.disconnect", {"provider": "gemini"}, viewer=VIEWER) == {"disconnected": True}
    saved = _read(service.home)
    assert "agy_selection" not in saved["autonomous"]["panel"]
    assert not (service.home / "subscriptions" / "agy" / "native-applied.json").exists()
    assert read_selection(service.home, saved) is None
    assert (store_b.home / "config.yaml").read_bytes() == before_b


def test_tui_one_shot_return_does_not_outrank_a_later_explicit_pick(tmp_path, monkeypatch):
    from hermes_runtime.sessions import HOME_SESSION_ID
    from tui_gateway import model_switch

    home = tmp_path / "profile"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = _managed_config(home, generation="fixture-generation")
    applied = []
    monkeypatch.setitem(vars(model_switch), "_config_model_target", lambda: ("managed-fixture", "custom"))
    monkeypatch.setattr(model_switch, "_apply_model_switch",
                        lambda *args, **kwargs: applied.append(kwargs) or {"value": "x", "warning": ""})
    target = ("managed-fixture", "custom")
    session = {"session_key": HOME_SESSION_ID, "agent": object(), "model_override": {"model": "later-pick"},
               "composer_override_profile": {"model": target[0], "provider": target[1]},
               "panel_transition_revision": REVISION, "config_model_seen": target}
    model_switch._sync_agent_model_with_config("fixture-sid", session)
    assert not applied, "an adopted one-shot return must not override the later explicit pick"
    assert session["model_override"] == {"model": "later-pick"}

    # Before the return was adopted the same ticket is still the authoritative fence.
    session.pop("panel_transition_revision")
    session.pop("model_override")
    session.pop("composer_override_profile")
    monkeypatch.setitem(vars(model_switch), "_persist_live_session_runtime", lambda *_a, **_k: None)
    model_switch._sync_agent_model_with_config("fixture-sid", session)
    assert applied and session["panel_transition_revision"] == REVISION
    assert config["autonomous"]["panel"]["agy_selection"]["generation"] == "fixture-generation"


def test_transition_ticket_is_scoped_to_its_session():
    """Two concurrent gateway turns: a foreign turn never takes the home ticket."""
    from threading import Barrier, Lock
    from hermes_runtime.adapters.main_model import stash_panel_ticket, take_panel_ticket
    from hermes_runtime.sessions import HOME_SESSION_KEY

    class Runner:
        pass

    runner = Runner()
    sentinel = object()
    stash_panel_ticket(runner, HOME_SESSION_KEY, sentinel)
    barrier, results, lock = Barrier(2), {}, Lock()

    def foreign():
        barrier.wait()
        with lock:
            results["foreign"] = take_panel_ticket(runner, "agent:main:agentready:other")

    def home():
        barrier.wait()
        with lock:
            results["home"] = take_panel_ticket(runner, HOME_SESSION_KEY)

    threads = [Thread(target=foreign), Thread(target=home)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == {"foreign": None, "home": sentinel}
    assert take_panel_ticket(runner, HOME_SESSION_KEY) is None  # consumed once


def test_no_request_turn_releases_the_fence_and_later_request_has_no_receipt(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.agy_selection import Selection, read_selection, release_fence

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = _managed_config(home, generation="fixture-generation")
    ticket = read_selection(home, config)
    assert ticket is not None and ticket.provider == "custom"
    published = []
    monkeypatch.setattr(Selection, "publish_applied", lambda _ticket, agent: published.append(agent))

    class InertTransport:
        def __init__(self):
            self.chat = types.SimpleNamespace(completions=self)

        def create(self, **_request):
            return "fixture-only"

    monkeypatch.setattr("agent.agent_runtime_helpers.create_openai_client", lambda *_a, **_k: InertTransport())
    agent = AIAgent(provider="custom", model="managed-fixture", session_id=HOME_SESSION_ID, quiet_mode=True,
                    skip_memory=True, skip_context_files=True, enabled_toolsets=[], base_url=config["model"]["base_url"],
                    api_key="fixture")
    try:
        ticket.attach(agent)
        assert agent._panel_selection is ticket
        # The turn ends before any request (preflight refusal): teardown drops the fence.
        release_fence(agent)
        assert agent._panel_selection is None
        # A later explicit /model is a different route; the pending request must not inherit it.
        agent.model = "later-explicit-model"
        client = agent._create_request_openai_client(reason="fixture_request")
        assert isinstance(client, InertTransport) and published == []
        assert not (home / "subscriptions" / "agy" / "native-applied.json").exists()
    finally:
        agent.close()


def test_gateway_turn_teardown_is_scoped_and_releases_the_fence(tmp_path, monkeypatch):
    """The real turn path: fence gone after success and after an exception, foreign slot untouched."""
    from gateway.run import GatewayConfig, GatewayRunner
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from hermes_runtime.adapters.main_model import stash_panel_ticket, take_panel_ticket
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ticket = read_selection(home, _managed_config(home, generation="fixture-generation"))
    runner = GatewayRunner(GatewayConfig())
    source = types.SimpleNamespace(user_id="fixture", user_name="fixture", is_bot=False)
    foreign_key, foreign_ticket = "agent:main:agentready:fixture-foreign", object()

    class Agent:
        _panel_selection = None

        def __init__(self, outcome):
            self.outcome = outcome

        def run_conversation(self, _message, **_kwargs):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    def turn(agent, session_key=HOME_SESSION_KEY):
        ctx = TurnContext(source=source, session_id=HOME_SESSION_ID, session_key=session_key,
                          message="echo fixture")
        TurnRunner(runner, ctx)._run_conversation_with_approval(agent, [], None, None, None)
        return agent

    stash_panel_ticket(runner, HOME_SESSION_KEY, ticket)
    stash_panel_ticket(runner, foreign_key, foreign_ticket)
    assert turn(Agent("fixture only"))._panel_selection is None
    assert take_panel_ticket(runner, HOME_SESSION_KEY) is None and \
        take_panel_ticket(runner, foreign_key) is foreign_ticket

    stash_panel_ticket(runner, HOME_SESSION_KEY, ticket)
    with pytest.raises(RuntimeError, match="fixture failure"):
        turn(Agent(RuntimeError("fixture failure")))
    assert take_panel_ticket(runner, HOME_SESSION_KEY) is None

    stash_panel_ticket(runner, HOME_SESSION_KEY, ticket)
    assert turn(Agent("foreign only"), session_key=foreign_key)._panel_selection is None
    assert take_panel_ticket(runner, HOME_SESSION_KEY) is ticket


@pytest.mark.parametrize("outcome", ["fixture only", RuntimeError("fixture failure")])
def test_tui_turn_teardown_releases_the_fence(tmp_path, monkeypatch, outcome):
    """The TUI turn boundary as installed on server.py, with the real attach import."""
    import threading
    from tui_gateway import server
    from tui_gateway.prompt_turn import _TurnRun
    from hermes_runtime.sessions import HOME_SESSION_ID

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ticket = read_selection(home, _managed_config(home, generation="fixture-generation"))

    class Agent:
        _panel_selection = None

        def run_conversation(self, _message, **_kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    agent = Agent()
    ticket.attach(agent)
    assert agent._panel_selection is ticket
    stop = threading.Event()
    monkeypatch.setattr(server, "_start_usage_ticker",
                        lambda *_a, **_k: (stop, types.SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(server, "_adopt_submit_user_row", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr("agent.notification_presentation.event_presentation_muted", lambda *_a: True)
    session = {"history_lock": threading.Lock(), "session_key": HOME_SESSION_ID}
    state = _TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None,
                                        receipt_committed=False)
    if isinstance(outcome, Exception):
        with pytest.raises(RuntimeError, match="fixture failure"):
            server._invoke_agent("fixture-sid", session, state, "echo", "echo", None, [], None, None)
    else:
        server._invoke_agent("fixture-sid", session, state, "echo", "echo", None, [], None, None)
        assert state.result == outcome
    assert agent._panel_selection is None
    assert not (home / "subscriptions" / "agy" / "native-applied.json").exists()
