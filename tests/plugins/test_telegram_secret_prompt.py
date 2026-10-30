"""Secret entry through real Telegram admission and the native vault callback."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("telegram")
from telegram import Document, ForceReply, Message, Update
from telegram.ext import ExtBot, TypeHandler

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_vault_prompt import vault_prompt_scope
from plugins.platforms.telegram.secret_prompt import MARKER, SecretPrompts
from tests.plugins.test_telegram_update_admission import connected


def source(**kwargs):
    return SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="88", **kwargs)


def reply(bot, prompt, value="test-only-secret", *, uid=51, user=88, chat=42, edited=False, document=None):
    message = {"message_id": uid + 1000, "date": 1800000000,
               "chat": {"id": chat, "type": "private"},
               "from": {"id": user, "is_bot": False, "first_name": "Human"},
               "reply_to_message": prompt.to_dict()}
    message.update({"document": document} if document else {"text": value})
    return Update.de_json({"update_id": uid, "edited_message" if edited else "message": message}, bot)


@asynccontextmanager
async def secret_transport(monkeypatch, **kwargs):
    prompts, notices, deleted = asyncio.Queue(), [], []

    async def send(bot, **params):
        notices.append(params["text"])
        message = Message.de_json({
            "message_id": 100 + len(notices), "date": 1800000000,
            "from": {"id": bot.id, "is_bot": True, "first_name": "Offline"},
            "chat": {"id": int(params["chat_id"]), "type": "private"}, "text": params["text"],
        }, bot)
        if isinstance(params.get("reply_markup"), ForceReply):
            prompts.put_nowait(message)
        return message

    async def delete(bot, **params):
        deleted.append(params["message_id"])
        return True

    monkeypatch.setattr(ExtBot, "send_message", send)
    monkeypatch.setattr(ExtBot, "delete_message", delete)
    monkeypatch.setattr(ExtBot, "edit_message_text", AsyncMock())
    async with connected(monkeypatch, **kwargs) as (adapter, app, delivered):
        yield adapter, app, delivered, prompts, notices, deleted


@pytest.mark.asyncio
async def test_native_save_login_roundtrip_encrypted_no_model_or_plugin_dispatch(monkeypatch, tmp_path, caplog):
    from agent.vault_backends import unlock
    from agent.vault_store import get_vault_store
    from tools import browser_vault_tool as vault
    from tools.thread_context import propagate_context_to_thread

    password = "  canary-password\nwith arbitrary Unicode: Ж 🔑  "
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *args: None)
    monkeypatch.setattr(vault, "_current_page_origin", lambda task: "https://shop.test")
    monkeypatch.setattr(vault, "browser_vault_fill", lambda *args, **kw: '{"success":true}')
    caplog.set_level(logging.DEBUG)
    async with secret_transport(monkeypatch) as (adapter, app, delivered, prompts, notices, deleted):
        observer = AsyncMock()
        app.add_handler(TypeHandler(Update, observer), group=-100)
        loop = asyncio.get_running_loop()
        turn = SimpleNamespace(
            _runner=SimpleNamespace(_delivery_adapter_for=lambda src: adapter),
            _ctx=SimpleNamespace(source=source(), _loop_for_step=loop, _run_still_current=lambda: True))

        def save():
            old = unlock.get_save_login_prompt_callback()
            with vault_prompt_scope(turn, SimpleNamespace(_interrupt_requested=False)):
                assert unlock.can_prompt_here()
                # Tools dispatched onto another worker inherit the existing callback bridge.
                with ThreadPoolExecutor(max_workers=1) as worker:
                    result = worker.submit(propagate_context_to_thread(vault.browser_vault_save_login)).result(timeout=10)
            assert unlock.get_save_login_prompt_callback() is old
            return json.loads(result)

        task = asyncio.create_task(asyncio.to_thread(save))
        login_prompt = await asyncio.wait_for(prompts.get(), 5)
        await app.process_update(reply(app.bot, login_prompt, "human@example.test"))
        password_prompt = await asyncio.wait_for(prompts.get(), 5)
        inbound = reply(app.bot, password_prompt, password, uid=52)
        await app.process_update(inbound)
        await app.process_update(inbound)  # redelivery is metadata-only deduplication
        result = await asyncio.wait_for(task, 5)
        assert result["success"] and result["identifier"] == "human@example.test"
        assert password not in json.dumps(result)
        assert not delivered and not observer.called
        assert 1051 in deleted and 1052 in deleted
        [item] = get_vault_store().list_items()
        assert item.origin == "https://shop.test"
        assert get_vault_store().resolve_secret(item.id)["password"] == password
        # Exercise the SDK's logging before admission as well as this module's own logs.
        logging.getLogger("telegram.ext.ExtBot").debug("API result %s", {"text": password})
        logging.getLogger("telegram.ext.Application").debug("Processing update %s", inbound)
        logging.getLogger("telegram.ext.ExtBot").critical("Error while parsing updates! Received data was %r", password)
        assert "canary-password" not in caplog.text
        assert all("canary-password" not in text for text in notices)
        if adapter._update_receipt_flush:
            await adapter._update_receipt_flush
        from hermes_constants import get_hermes_home
        assert all(b"canary-password" not in path.read_bytes()
                   for path in get_hermes_home().rglob("*") if path.is_file())


@pytest.mark.asyncio
async def test_wrong_sender_chat_and_expired_reply_never_reach_handlers(monkeypatch):
    async with secret_transport(monkeypatch) as (adapter, app, delivered, prompts, _notices, deleted):
        task = asyncio.create_task(adapter.request_secret(source(), label="Секрет", site="test"))
        prompt = await asyncio.wait_for(prompts.get(), 5)
        for uid, changes in enumerate(({"user": 99}, {"chat": 43}, {"edited": True}), 60):
            await app.process_update(reply(app.bot, prompt, uid=uid, **changes))
        assert not task.done() and not delivered
        await app.process_update(reply(app.bot, prompt, "accepted", uid=64))
        assert await task == "accepted"
        # A replacement/restarted adapter has no pending state; bot-authored marker quarantines it.
        adapter._secret_prompts = SecretPrompts(adapter)
        await app.process_update(reply(app.bot, prompt, "late-secret", uid=65))
        assert not delivered and 1065 in deleted


@pytest.mark.asyncio
async def test_text_document_keeps_multiline_and_whitespace_without_disk_cache(monkeypatch):
    value = "  long-secret Ж\n" * 1000
    data = bytearray(value.encode())
    download = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=data)))
    monkeypatch.setattr(Document, "get_file", download)
    async with secret_transport(monkeypatch) as (adapter, app, delivered, prompts, _notices, deleted):
        task = asyncio.create_task(adapter.request_secret(source(), label="Произвольный секрет", site="test"))
        prompt = await asyncio.wait_for(prompts.get(), 5)
        doc = {"file_id": "test-file", "file_unique_id": "test-file", "file_size": len(data)}
        await app.process_update(reply(app.bot, prompt, document=doc))
        assert await task == value
        assert not any(data) and not delivered and 1051 in deleted


@pytest.mark.asyncio
async def test_cancel_deletion_failure_and_nonreply_are_not_model_messages(monkeypatch, caplog):
    async with secret_transport(monkeypatch) as (adapter, app, delivered, prompts, notices, _deleted):
        task = asyncio.create_task(adapter.request_secret(source(), label="Секрет", site="test"))
        prompt = await asyncio.wait_for(prompts.get(), 5)
        payload = reply(app.bot, prompt).to_dict()
        del payload["message"]["reply_to_message"]
        await app.process_update(Update.de_json(payload, app.bot))
        assert not task.done() and not delivered
        token = prompt.text.split("\n")[0].removeprefix(MARKER)
        cancel = Update.de_json({"update_id": 70, "callback_query": {
            "id": "cancel", "from": {"id": 88, "is_bot": False, "first_name": "Human"},
            "chat_instance": "chat", "data": f"secret:cancel:{token}", "message": prompt.to_dict(),
        }}, app.bot)
        await app.process_update(cancel)
        assert await task == ""
        monkeypatch.setattr(ExtBot, "delete_message", AsyncMock(side_effect=RuntimeError("do-not-log-this-secret")))
        await app.process_update(reply(app.bot, prompt, "late-secret", uid=71))
        assert "do-not-log-this-secret" not in caplog.text
        assert any("вручную" in text for text in notices)
        assert not delivered


@pytest.mark.asyncio
async def test_disconnect_and_timeout_close_waiters_and_groups_cannot_prompt(monkeypatch):
    async with secret_transport(monkeypatch) as (adapter, _app, _delivered, prompts, _notices, _deleted):
        assert await adapter.request_secret(source(chat_type="group"), label="secret", site="test") == ""
        assert await adapter._secret_prompts.ask(source(), label="secret", site="test", timeout=0) == ""
        assert not adapter._secret_prompts.pending
        while not prompts.empty():
            prompts.get_nowait()
        task = asyncio.create_task(adapter.request_secret(source(), label="secret", site="test"))
        await asyncio.wait_for(prompts.get(), 5)
        adapter._secret_prompts.close()
        assert await asyncio.wait_for(task, 5) == ""
        assert not adapter._secret_prompts.pending


@pytest.mark.asyncio
async def test_code_callback_is_bound_to_receiving_bot_and_restored(monkeypatch):
    from agent.vault_backends import unlock

    async with secret_transport(monkeypatch) as (adapter, app, delivered, prompts, _notices, _deleted):
        loop = asyncio.get_running_loop()
        origin = source()
        resolved = []

        def delivery(src):
            resolved.append(src)
            return adapter

        turn = SimpleNamespace(_runner=SimpleNamespace(_delivery_adapter_for=delivery),
                               _ctx=SimpleNamespace(source=origin, _loop_for_step=loop, _run_still_current=lambda: True))

        def code():
            previous = unlock.get_code_prompt_callback()
            with vault_prompt_scope(turn, SimpleNamespace(_interrupt_requested=False)):
                result = unlock.get_code_prompt_callback()("shop.test", "")
            assert unlock.get_code_prompt_callback() is previous
            return result

        task = asyncio.create_task(asyncio.to_thread(code))
        prompt = await asyncio.wait_for(prompts.get(), 5)
        await app.process_update(reply(app.bot, prompt, "A12-BC34-more-than-six"))
        assert await asyncio.wait_for(task, 5) == "A12-BC34-more-than-six"
        assert resolved == [origin] and not delivered
