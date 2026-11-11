"""Refresh instructions at admission of managed main and persistent project turns."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

SkillUpdates = dict[tuple[str, str], str]


@dataclass(frozen=True)
class SoulSnapshot:
    parts: tuple[str, ...]
    loaded: bool

    @property
    def stamp(self) -> str:
        digest = sha256("\n\n".join(self.parts).encode("utf-8")).hexdigest()
        return f"[AgentSee SOUL revision: {digest}]"


def snapshot_soul(agent):
    turn = getattr(agent, "_agentready_active_turn", None)
    if turn is None:
        return
    from agent.system_prompt import _context_window, _identity_parts

    parts, loaded = _identity_parts(agent, _context_window(agent))
    turn.soul = SoulSnapshot(tuple(parts), loaded)


def turn_soul(agent) -> SoulSnapshot | None:
    snapshot = getattr(getattr(agent, "_agentready_active_turn", None), "soul", None)
    return snapshot if isinstance(snapshot, SoulSnapshot) else None


def stale_soul(agent, prompt: str | None) -> bool:
    soul = turn_soul(agent)
    return bool(soul and prompt and not prompt.endswith(f"\n\n{soul.stamp}"))


def prepare_turn_prompt(agent, system_message, history, restore_or_build) -> None:
    snapshot_soul(agent)
    if agent._cached_system_prompt is None or stale_soul(agent, agent._cached_system_prompt):
        restore_or_build(agent, system_message, history)


def _skill_result(content: str) -> dict[str, Any]:
    try:
        result = json.loads(content)
    except ValueError:
        return {}
    return result if isinstance(result, dict) else {}


def _needs_skill_replacement(previous: dict, current: dict) -> bool:
    if current.get("reload_required"):
        return any(previous.get(key) != current.get(key) for key in ("source_sha256", "skill_dir"))
    if not current.get("success"):
        return True
    return any(previous.get(key) != current.get(key) for key in ("content", "skill_dir", "linked_files", "path"))


def _refresh_loaded_skills_for(agent) -> bool:
    if getattr(agent, "_agentready_active_turn", None) is not None:
        return True
    if (getattr(agent, "platform", None) != "subagent"
            or not isinstance(getattr(agent, "_persistent_project_id", None), str)
            or getattr(agent, "_persist_disabled", False)):
        return False
    from hermes_runtime.adapters.hermes import enabled

    return enabled()


def snapshot_loaded_skills(agent, messages: list[dict[str, Any]], task_id: str) -> SkillUpdates:
    if not _refresh_loaded_skills_for(agent) or not any(message.get("role") == "tool" for message in messages):
        return {}
    from agent.context_compressor_tail import skill_view_names_by_call_id
    from agent.skill_library import library_scope
    from tools.skills_tool import skill_view

    updates: SkillUpdates = {}
    reads: dict[tuple[str, str | None], tuple[str, dict]] = {}
    calls: dict[str, str] = {}
    with library_scope():
        for message in messages:
            if message.get("role") == "assistant":
                calls = skill_view_names_by_call_id([message])
                continue
            call_id = str(message.get("tool_call_id") or "")
            original = message.get("content")
            if message.get("role") != "tool" or call_id not in calls or not isinstance(original, str):
                continue
            previous = _skill_result(original)
            if not previous.get("success") or previous.get("is_binary") or not isinstance(previous.get("content"), str):
                continue
            name, path = calls[call_id], previous.get("file")
            if path is not None and not isinstance(path, str):
                continue
            key = (name, path)
            if key not in reads:
                refreshed = skill_view(name, file_path=path, task_id=task_id, read_only=True)
                reads[key] = refreshed, _skill_result(refreshed)
            refreshed, current = reads[key]
            if _needs_skill_replacement(previous, current):
                updates[call_id, original] = refreshed
    return updates


def refreshed_skill_content(updates: SkillUpdates, message: dict[str, Any]) -> str | None:
    # Compaction may prune this row after admission. Do not resurrect its body.
    content = message.get("content")
    return updates.get((str(message.get("tool_call_id") or ""), content)) if isinstance(content, str) else None
