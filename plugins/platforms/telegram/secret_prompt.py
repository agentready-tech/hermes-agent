"""Private-chat secret replies, consumed before PTB handlers, hooks or message history.

The bot-authored marker also quarantines late replies after a gateway restart. Only
request metadata lives in the broker; values travel through the waiting future to
the vault callback. Telegram itself still transports the plaintext messages.
"""

import asyncio
import logging
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)
MARKER = "🔐 Запрос секрета Hermes · "
TIMEOUT_SECONDS = 300
MAX_FILE_BYTES = 20 * 1024 * 1024


class _PayloadFilter(logging.Filter):
    def filter(self, record):
        # PTB logs raw getUpdates results BEFORE process_update, and queued Update
        # reprs before dispatch. Redacting only after receipt would be too late.
        if (record.levelno == logging.DEBUG and record.args) or str(record.msg).startswith("Error while parsing updates!"):
            record.msg, record.args = "Telegram SDK payload omitted", ()
            record.exc_info, record.exc_text = None, None
        return True


_PAYLOAD_FILTER = _PayloadFilter()


@dataclass
class _Request:
    chat: str
    user: str
    thread: str | None
    future: asyncio.Future
    message_id: int | None = None


class SecretPrompts:
    def __init__(self, adapter):
        self.adapter = adapter
        self.pending: dict[str, _Request] = {}
        for name in ("telegram.Bot", "telegram.ext.ExtBot", "telegram.ext.Application"):
            logging.getLogger(name).addFilter(_PAYLOAD_FILTER)

    def close(self):
        for item in tuple(self.pending.values()):
            if not item.future.done():
                item.future.set_result("")

    async def ask(self, source, *, label, site, timeout=TIMEOUT_SECONDS):
        from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

        if source.chat_type != "dm" or not source.user_id or not self.adapter.is_connected:
            return ""
        chat, user = str(source.chat_id), str(source.user_id)
        thread = str(source.thread_id) if source.thread_id is not None else None
        if any((r.chat, r.user, r.thread) == (chat, user, thread) for r in self.pending.values()):
            return ""
        token = secrets.token_hex(12)
        item = _Request(chat, user, thread, asyncio.get_running_loop().create_future())
        self.pending[token] = item
        bot = self.adapter._bot
        controls = None
        accepted = False
        kwargs = {"message_thread_id": int(thread)} if thread is not None else {}
        try:
            async with asyncio.timeout(timeout):
                controls = await bot.send_message(
                    chat_id=int(chat), text="Ввод секрета · запрос действует 5 минут",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Отменить", callback_data=f"secret:cancel:{token}")]]), **kwargs)
                prompt = await bot.send_message(
                    chat_id=int(chat), parse_mode=None,
                    text=(f"{MARKER}{token}\n{label}\nСайт: {site}\n\n"
                          "Ответьте именно на это сообщение. Можно отправить многострочный текст "
                          "или UTF-8 файл до 20 МБ. Ответ будет удалён и передан инструменту, "
                          "минуя чат модели."),
                    reply_markup=ForceReply(input_field_placeholder=str(label)[:64]), **kwargs)
                item.message_id = prompt.message_id
                value = await item.future
                accepted = bool(value)
                return value
        except TimeoutError:
            return ""
        except Exception:  # health: allow BLE001 -- secret transport errors must never log payloads or tracebacks
            logger.warning("Telegram secret prompt could not complete")
            return ""
        finally:
            self.pending.pop(token, None)
            if not item.future.done():
                item.future.cancel()
            if controls is not None:
                await self._delete(chat, controls.message_id)
            if item.message_id is not None:
                try:
                    await bot.edit_message_text(
                        chat_id=int(chat), message_id=item.message_id, parse_mode=None,
                        text=f"{MARKER}{token}\n" + ("Принято инструментом." if accepted else "Запрос закрыт."))
                except Exception:  # health: allow BLE001 -- secret transport errors must never log payloads or tracebacks
                    logger.warning("Telegram secret prompt status could not be updated")

    async def _delete(self, chat, message_id):
        try:
            await self.adapter._bot.delete_message(chat_id=int(chat), message_id=message_id)
            return True
        except Exception:  # health: allow BLE001 -- secret transport errors must never log payloads or tracebacks
            # Do not log SDK exceptions: they may contain secret-bearing payloads.
            logger.warning("Telegram secret message deletion failed (chat=%s, message=%s)", chat, message_id)
            return False

    async def _notice(self, chat, text, thread=None):
        try:
            kwargs = {"message_thread_id": int(thread)} if thread is not None else {}
            await self.adapter._bot.send_message(chat_id=int(chat), text=text, **kwargs)
        except Exception:  # health: allow BLE001 -- secret transport errors must never log payloads or tracebacks
            logger.warning("Telegram secret status notice failed")

    def _reply_token(self, message):
        parent = message.reply_to_message
        if parent is None or parent.from_user is None or parent.from_user.id != self.adapter._bot.id:
            return None
        text = parent.text or ""
        return text[len(MARKER):].split("\n", 1)[0] if text.startswith(MARKER) else None

    async def _read_value(self, message):
        if message.text is not None:
            return message.text
        document = message.document
        if document is None or document.file_size is None or document.file_size > MAX_FILE_BYTES:
            raise ValueError("Expected text or a bounded UTF-8 file")
        remote = await document.get_file()
        data = await remote.download_as_bytearray()
        try:
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("File exceeds the secret input limit")
            return data.decode("utf-8")
        finally:
            data[:] = b"\x00" * len(data)

    async def consume(self, update):
        query = update.callback_query
        if query is not None:
            if isinstance(query.data, str) and query.data.startswith("secret:cancel:"):
                return await self._cancel(query)
            return False
        message = update.effective_message
        if message is None:
            return False
        token = self._reply_token(message)
        chat = str(message.chat_id)
        user = str(message.from_user.id) if message.from_user is not None else ""
        thread = self.adapter._effective_message_thread_id(message)
        thread = str(thread) if thread is not None else None
        # A mistaken unthreaded reply during entry must not become a model message.
        waiting = any((r.chat, r.user, r.thread) == (chat, user, thread) for r in self.pending.values())
        if token is None and not waiting:
            return False
        if token is None and message.text in ("/stop", "/new"):
            self._settle_lane(chat, user, thread)
            return False
        item = self.pending.get(token)
        valid = (item is not None and not item.future.done() and message.chat.type == "private"
                 and (item.chat, item.user, item.thread) == (chat, user, thread)
                 and update.edited_message is None
                 and (item.message_id is None or item.message_id == message.reply_to_message.message_id))
        value = ""
        if valid:
            try:
                value = await self._read_value(message)
            except Exception:  # health: allow BLE001 -- secret transport errors must never log payloads or tracebacks
                # Invalid UTF-8 and download errors must not include the value or file URL.
                await self._notice(chat, "Нужен текст или UTF-8 файл до 20 МБ. Повторите ответ на запрос.", thread)
        deleted = await self._delete(chat, message.message_id)
        if not deleted:
            await self._notice(chat, "Telegram не подтвердил удаление ответа. Удалите его вручную.", thread)
        if valid and value and not item.future.done():
            item.future.set_result(value)
        elif token is None or not valid:
            await self._notice(chat, "Ответ не передан агенту. Используйте ответ на действующий запрос секрета.", thread)
        return True

    def _settle_lane(self, chat, user, thread):
        for item in self.pending.values():
            if (item.chat, item.user, item.thread) == (chat, user, thread) and not item.future.done():
                item.future.set_result("")

    async def _cancel(self, query):
        token = query.data.removeprefix("secret:cancel:")
        item = self.pending.get(token)
        message = query.message
        if (item is not None and message is not None and str(message.chat.id) == item.chat
                and str(query.from_user.id) == item.user and not item.future.done()):
            item.future.set_result("")
        try:
            await query.answer()
        except Exception:  # health: allow BLE001 -- secret transport errors must never log payloads or tracebacks
            logger.warning("Telegram secret cancellation acknowledgement failed")
        return True


class TelegramSecretPromptMixin:
    async def request_secret(self, source, *, label: str, site: str) -> str:
        """Return a private reply directly to the waiting tool, never to message handlers."""
        return await self._secret_prompts.ask(source, label=label, site=site)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a bot-posted message (Bot API allows it within 48h); failures are non-fatal.

        Used by the stream consumer's fresh-final cleanup path (ported from openclaw/openclaw#72038) to
        remove long-lived preview messages after sending the completed reply as a fresh message. Telegram's
        Bot API ``deleteMessage`` works for bot-posted messages in the last 48 hours. Failures are non-fatal
        — the caller leaves the preview in place and logs at debug level.
        """
        from plugins.platforms.telegram.adapter import _redact_telegram_error_text
        from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

        if not self._bot:
            return False
        try:
            await self._bot.delete_message(chat_id=normalize_telegram_chat_id(chat_id), message_id=int(message_id))
            return True
        except Exception as e:  # health: allow BLE001 -- retain the existing redacted deletion error boundary
            logger.debug("[%s] Failed to delete Telegram message %s: %s", self.name, message_id, _redact_telegram_error_text(e))
            return False

