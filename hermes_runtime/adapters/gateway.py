"""Hermes gateway routing and business-event translation."""

import logging
import uuid
from hermes_runtime.events import event_prompt
from hermes_runtime.sessions import (
    HOME_SESSION_ID,
    HOME_SESSION_TITLE,
    HOME_SESSION_KEY,
)
from hermes_runtime.adapters.turns import forward_to_active_turn


def recover_home(self, recovered, *, session_key, source, now):
    if recovered is not None:
        return recovered
    db = getattr(self, "_db", None)
    if db is None:
        return None
    try:
        target = HOME_SESSION_ID
        resolve_tip = getattr(db, "resolve_resume_session_id", None)
        if callable(resolve_tip):
            target = resolve_tip(target) or target
        row = db.get_session(target)
        if not row:
            return None
        db.reopen_session(target)
        entry = self._create_entry_from_recovered_row(
            row=row,
            session_key=HOME_SESSION_KEY,
            source=source,
            now=now,
        )
        self._record_gateway_session_peer(
            target,
            HOME_SESSION_KEY,
            source,
            display_name=HOME_SESSION_TITLE,
        )
        return entry
    except Exception:
        logging.getLogger(__name__).debug("Managed gateway recovery unavailable", exc_info=True)
        return None


def prepare_event(event, source):
    try:
        text = str(getattr(event, "text", "") or "")
        if not text.startswith("[AgentReady event]"):
            platform = (
                source.platform.value
                if hasattr(source.platform, "value")
                else str(source.platform)
            )
            message_id = str(getattr(event, "message_id", "") or "")
            event.text = event_prompt(
                text,
                platform,
                "{}:{}".format(
                    platform, message_id or int(__import__("time").time() * 1000)
                ),
                {
                    "chat_id": str(getattr(source, "chat_id", "") or ""),
                    "thread_id": str(getattr(source, "thread_id", "") or ""),
                    "user_id": str(getattr(source, "user_id", "") or ""),
                },
            )
    except Exception:
        logging.getLogger(__name__).debug("Managed event translation failed", exc_info=True)


async def busy_event(self, proceed, event, session_key):
    accepted = await admit_busy_event(self, event, session_key)
    return await proceed(event, session_key) if accepted is None else accepted


async def admit_busy_event(self, event, session_key):
    """Join the one managed inbox; ordinary events never redirect or cancel its writer.

    None leaves native sessions/control commands alone. False means the managed
    owner cannot take this event now: preserve it in native admission/queue.
    """
    from hermes_runtime.adapters.hermes import enabled
    if not enabled() or session_key != HOME_SESSION_KEY:
        return None
    from hermes_cli.commands import resolve_command
    from tools.approval import has_blocking_approval

    # Leave authorization, explicit commands and approval responses with
    # Hermes. Only business events join the active model run.
    if (
        not self._is_user_authorized_for_source(event.source)
        or self._draining
        or (event.get_command() and resolve_command(event.get_command()) is not None)
    ):
        return None
    if has_blocking_approval(session_key):
        return False
    text = await self._prepare_profile_scoped_inbound_message_text(
        event=event,
        source=event.source,
        history=[],
        session_key=session_key,
    )
    if text is None:
        return True
    platform = event.source.platform.value
    content = event_prompt(
        text,
        platform,
        f"{platform}:{event.message_id or uuid.uuid4().hex}",
        {
            "chat_id": str(event.source.chat_id or ""),
            "thread_id": str(event.source.thread_id or ""),
            "user_id": str(event.source.user_id or ""),
            "internal": bool(getattr(event, "internal", False)),
        },
    )
    images = self._consume_pending_native_image_paths(session_key)
    if images:
        from agent.image_routing import build_native_content_parts

        content, skipped = build_native_content_parts(content, images)
        if skipped:
            raise RuntimeError("Cannot read attached images: " + ", ".join(skipped))
    import asyncio

    if await asyncio.to_thread(forward_to_active_turn, content):
        state = self._peek_session_state(session_key)
        if state is not None and state.turn.agent is not None:
            self._fold_into_running_turn(state.turn.agent, session_key, event)
        return True
    # No active owner yet (agent startup), or it has already closed its
    # mailbox. Preserve the original event for ordinary next-turn routing.
    return False
