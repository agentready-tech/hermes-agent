"""Archive-backed working context when a semantic summary is unavailable."""

import copy


def recovery_context(messages, handoff, current_index, fits):
    """Keep the current event and its complete tail when they fit; otherwise offload them.

    The caller has already archived every source message. No model-generated text is
    clipped or presented as an accepted summary by this emergency path.
    """
    head = [copy.deepcopy(message) for message in messages if message.get("role") == "system"]
    prefix = [*head, handoff]
    if current_index is not None:
        tail = copy.deepcopy(messages[current_index:])
        if fits([*prefix, *tail]):
            return [*prefix, *tail]
        current = copy.deepcopy(messages[current_index])
        if fits([*prefix, current]):
            return [*prefix, current]
        current = {
            "role": "user",
            "content": (
                "The latest user event is preserved verbatim as message " + str(current_index)
                + " (zero-based) in the recovery transcript referenced above. Read that event "
                "with bounded file reads before deciding what to do; its full text did not fit "
                "in the model context. Do not infer or replace the user's request."
            ),
        }
        prefix.append(current)
    if not fits(prefix):
        raise RuntimeError("The model window cannot fit the system, tools and recovery reference")
    return prefix
