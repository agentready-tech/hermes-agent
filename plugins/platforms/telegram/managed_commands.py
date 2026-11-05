"""The managed employee exposes chat and pending decisions, not Hermes administration."""

from gateway.platforms.base import MessageType

_COMMANDS = frozenset({"start", "stop", "approve", "deny"})
_DECISION_CALLBACKS = ("ea:", "cl:", "gt:", "secret:cancel:")
_UNAVAILABLE = "Технические команды здесь недоступны. Напишите задачу обычным сообщением; настройки сотрудника — в agentsee."


def managed() -> bool:
    from hermes_runtime.adapters.hermes import enabled

    return enabled()


def visible_commands(commands):
    return [] if managed() else commands


async def block_callback(query) -> bool:
    if not managed() or str(query.data).startswith(_DECISION_CALLBACKS):
        return False
    await query.answer(text="Эта кнопка больше недоступна. Настройки сотрудника — в agentsee.")
    return True


async def block_inline(query) -> bool:
    if not managed():
        return False
    await query.answer([], cache_time=0, is_personal=True)
    return True


async def handle_command(adapter, update) -> None:
    """Gate before batching/queueing, so busy turns and long pastes cannot execute commands."""
    msg = adapter._effective_update_message(update)
    if not msg or not msg.text or not adapter._should_process_message(msg, is_command=True):
        return
    if not adapter._is_user_authorized_from_message(msg):
        adapter._log_blocked_user(msg)
        return
    event = await adapter._build_triggered_event(msg, update, MessageType.COMMAND)
    if managed():
        from hermes_cli.commands import resolve_command

        command = event.get_command()
        definition = resolve_command(command) if command else None
        if (definition.name if definition else command) not in _COMMANDS:
            await msg.reply_text(_UNAVAILABLE)
            return
    await adapter._ensure_forum_commands(msg)
    # Preserve native aggregation of commands split by Telegram at its message limit.
    if len(event.text or "") >= adapter._SPLIT_THRESHOLD:
        adapter._enqueue_text_event(event)
        return
    await adapter.handle_message(event)
