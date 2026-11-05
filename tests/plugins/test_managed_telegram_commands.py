"""Managed Telegram hides native administration without changing ordinary Hermes installs."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from hermes_runtime.adapters import hermes
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.fixture(autouse=True)
def telegram_api(monkeypatch):
    """The canonical test environment omits the optional Telegram SDK; fake its data objects only."""
    module = SimpleNamespace(
        BotCommand=lambda command, description: SimpleNamespace(command=command, description=description),
        BotCommandScopeDefault=lambda: SimpleNamespace(type="default"),
        BotCommandScopeAllPrivateChats=lambda: SimpleNamespace(type="all_private_chats"),
        BotCommandScopeAllGroupChats=lambda: SimpleNamespace(type="all_group_chats"),
        BotCommandScopeChat=lambda chat_id: SimpleNamespace(type="chat", chat_id=chat_id),
    )
    monkeypatch.setitem(sys.modules, "telegram", module)


@pytest.fixture
def managed(monkeypatch):
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(hermes, "_home", None)
    hermes.activate(get_hermes_home())


def adapter():
    value = object.__new__(TelegramAdapter)
    value.platform = Platform.TELEGRAM
    value.config = PlatformConfig(enabled=True, token="fixture", extra={})
    value._bot = SimpleNamespace(set_my_commands=AsyncMock())
    value._forum_command_registered = set()
    value._forum_lock = asyncio.Lock()
    value._effective_update_message = lambda update: update.message
    value._should_process_message = Mock(return_value=True)
    value._is_user_authorized_from_message = Mock(return_value=True)
    value._log_blocked_user = Mock()
    value._ensure_forum_commands = AsyncMock()
    value._build_triggered_event = AsyncMock(side_effect=lambda msg, *_: SimpleNamespace(
        text=msg.text, get_command=lambda: msg.text.split()[0][1:].split("@")[0]))
    value.handle_message = AsyncMock()
    value._enqueue_text_event = Mock()
    return value


@pytest.mark.asyncio
async def test_managed_menu_clears_every_native_scope_and_forum(managed):
    value = adapter()
    commands = [("new", "Reset"), ("model", "Change model")]
    with patch("hermes_cli.commands_platforms.telegram_menu_commands", return_value=(commands, 0)):
        await value._register_command_menu()
    calls = value._bot.set_my_commands.await_args_list
    assert {call.kwargs["scope"].type for call in calls} == {"default", "all_private_chats", "all_group_chats"}
    assert all(call.args[0] == [] for call in calls)
    value._ensure_forum_commands = TelegramAdapter._ensure_forum_commands.__get__(value)
    with patch("hermes_cli.commands_platforms.telegram_menu_commands", return_value=(commands, 0)):
        await value._ensure_forum_commands(SimpleNamespace(chat=SimpleNamespace(id=-123, is_forum=True)))
    assert value._bot.set_my_commands.await_args.args[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/new", "/reset", "/model@fixturebot change", "/restart", "/update", "/status", "/profile", "/sessions", "/commands", "/arbitrary_plugin", "/queue " + "x" * 4200])
async def test_managed_commands_are_rejected_before_batching_and_dispatch(managed, text):
    value = adapter()
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    await value._handle_command(SimpleNamespace(message=msg), SimpleNamespace())
    msg.reply_text.assert_awaited_once()
    value.handle_message.assert_not_awaited()
    value._enqueue_text_event.assert_not_called()
    value._ensure_forum_commands.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/start", "/stop", "/stop@fixturebot", "/approve", "/deny"])
async def test_managed_start_stop_and_pending_decisions_reach_native_handlers(managed, text):
    value = adapter()
    msg = SimpleNamespace(text=text, reply_text=AsyncMock())
    await value._handle_command(SimpleNamespace(message=msg), SimpleNamespace())
    value.handle_message.assert_awaited_once()
    msg.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_gate_keeps_native_sender_authorization(managed):
    value = adapter()
    value._is_user_authorized_from_message.return_value = False
    msg = SimpleNamespace(text="/stop", reply_text=AsyncMock())
    await value._handle_command(SimpleNamespace(message=msg), SimpleNamespace())
    value.handle_message.assert_not_awaited()
    value._build_triggered_event.assert_not_awaited()
    msg.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["mm:fixture", "mc:fixture", "cp:fixture", "sc:once:fixture", "update_prompt:fixture", "unknown:fixture"])
async def test_stale_administration_buttons_cannot_dispatch(managed, data):
    value = adapter()
    value._accept_update = Mock()
    query = SimpleNamespace(data=data, answer=AsyncMock())
    await value._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())
    query.answer.assert_awaited_once()
    value._accept_update.assert_not_called()


@pytest.mark.asyncio
async def test_pending_approval_and_secret_controls_remain_available(managed):
    from plugins.platforms.telegram.managed_commands import block_callback

    for data in ("ea:once:fixture", "cl:fixture:0", "gt:fixture", "secret:cancel:fixture"):
        query = SimpleNamespace(data=data, answer=AsyncMock())
        assert await block_callback(query) is False
        query.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_inline_picker_does_not_disclose_the_command_catalog(managed):
    value = adapter()
    query = SimpleNamespace(answer=AsyncMock())
    await value._handle_inline_query(SimpleNamespace(inline_query=query), SimpleNamespace())
    query.answer.assert_awaited_once_with([], cache_time=0, is_personal=True)


@pytest.mark.asyncio
async def test_unmanaged_hermes_keeps_its_menu_commands_and_callbacks(monkeypatch):
    from plugins.platforms.telegram.managed_commands import block_callback, block_inline

    monkeypatch.setattr(hermes, "_home", None)
    assert TelegramAdapter._bot_commands([("new", "Reset")])[0].command == "new"
    value = adapter()
    msg = SimpleNamespace(text="/model", reply_text=AsyncMock())
    await value._handle_command(SimpleNamespace(message=msg), SimpleNamespace())
    value.handle_message.assert_awaited_once()
    query = SimpleNamespace(data="mm:fixture", answer=AsyncMock())
    assert await block_callback(query) is False
    assert await block_inline(query) is False
    query.answer.assert_not_awaited()
