"""Resume an accepted project input without admitting its original platform ID twice."""

import uuid


def resume_input(child, history, text):
    if history and history[-1].get("role") == "user":
        # A delivered reply may be the unanswered tail. Re-stage that exact durable
        # dict, removing it only from the passed prefix so native turn setup appends
        # it once. Earlier requests and completed tool results remain untouched.
        latest = history[-1]
        child._pending_cli_user_message = latest
        child._delegate_replaying_input = True
        child._delegate_resume_kwargs.update(conversation_history=history[:-1],
            persist_user_platform_id=latest.get("message_id") or latest.get("platform_message_id"))
        return latest["content"]
    # Native turn setup needs a user input after a tool/assistant tail. This is an
    # attributed recovery control, not another admission of the original request.
    child._delegate_resume_kwargs.update(persist_user_platform_id="project-recovery-" + uuid.uuid4().hex,
                                         persist_user_display_kind="auto_continue")
    return ("Continue the interrupted project task below. Review the existing tool outcomes and check actual "
            "external state before repeating actions whose outcome is unknown.\n\n" + text)
