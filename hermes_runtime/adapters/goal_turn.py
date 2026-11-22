"""Continue an admitted Bot Mode project across native main turns."""

import json
import logging
import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tools.delegate_project_goal import (
    active_goal, add_evidence, close_goal, mutate_goal, operation_returns, owned_operation, read_goal,
)
from tools.delegate_project_store import ProjectStore

logger = logging.getLogger(__name__)
admitted_input = ContextVar("managed_goal_admitted_input", default=None)

_JUDGE = (
    "Which of these project goals have reached the final result specified by their contract? "
    "Use only the saved evidence supplied for each project. A worker report is evidence of what "
    "it reported, not automatically proof that every verification condition passed. A status "
    "update or an assertion alone cannot complete a goal. Return a JSON object mapping each "
    "project_id to an empty array if open, or to the exact evidence IDs supporting completion."
)


@dataclass
class GoalSegment:
    input_id: str | None
    internal: bool
    history_tool_results: set[str] = field(default_factory=set)
    project_ids: set[str] = field(default_factory=set)
    bound_project_ids: set[str] = field(default_factory=set)
    wake_ids: dict[str, str] = field(default_factory=dict)
    selected_id: str | None = None
    notify: bool = False
    stale_inputs: int = 0
    obsolete_records: set[str] = field(default_factory=set)
    quiet_source: bool = False
    quiet: bool = False

    def touch(self, project_id):
        self.project_ids.add(project_id)
        self.selected_id = project_id

    def bind(self, project_id):
        self.bound_project_ids.add(project_id)
        self.project_ids.add(project_id)
        self.selected_id = project_id if len(self.project_ids) == 1 else None


def current_segment(agent):
    segment = getattr(getattr(agent, "_agentready_active_turn", None), "goal_segment", None)
    return segment if isinstance(segment, GoalSegment) else None


def goal_in_scope(agent, project_id):
    segment = current_segment(agent)
    return (segment is None or not segment.internal or not segment.bound_project_ids
            or project_id in segment.bound_project_ids)


def _automation(row):
    payload = row.get("payload", {})
    return payload.get("local_automation_v1") or payload.get("native_text_v1", {}).get("automation")


def _automation_receipts(descriptor):
    if not descriptor:
        return ()
    identities = descriptor.get("identities") or [descriptor.get("identity")]
    result = []
    for encoded in identities:
        try:
            kind, identity, _ = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if kind == "async_delegation" and isinstance(identity, str):
            result.append(identity)
    return result


def _event_header(content):
    if isinstance(content, str) and content.startswith("[AgentReady event]\n"):
        try:
            header = json.loads(content.split("\n\n", 1)[0].split("\n", 1)[1])
        except ValueError:
            return {}
        if isinstance(header, dict):
            return header
    return {}


def goal_inbox_event(content):
    header = _event_header(content)
    metadata = header.get("metadata")
    return header.get("source") == "goal" or (
        header.get("source") in {"cron", "tool_operation"}
        and isinstance(metadata, dict) and metadata.get("project_goal") is True
    )


def _bound_event(store, segment, header):
    source, metadata = header.get("source"), header.get("metadata")
    if source == "tool_operation":
        return _tool_completion(store, segment, header.get("event_id"))
    if source not in {"goal", "cron"} or not isinstance(metadata, dict):
        return False
    project_id = metadata.get("project_id")
    if not isinstance(project_id, str):
        return False
    try:
        goal = read_goal(store, project_id)
    except ValueError:
        return False  # A native AGE-6 timer can belong to a legacy project.
    if not active_goal(goal) or goal["status"] == "blocked":
        segment.stale_inputs += 1
        return True
    if source == "goal":
        wake_id = header.get("event_id")
        if not wake_id or goal.get("wake_id") != wake_id:
            segment.stale_inputs += 1
            return True
        segment.wake_ids[project_id] = wake_id
    else:
        wait = json.loads(store.get(project_id)["workspace_json"]).get("external_wait") or {}
        if (wait.get("state") != "BOUND" or wait.get("wait_id") != metadata.get("wait_id")
                or wait.get("job_id") != metadata.get("job_id")
                or datetime.fromisoformat(wait["deadline"]) <= datetime.now(UTC)):
            segment.stale_inputs += 1
            return True
    segment.bind(project_id)
    return True


def _record_result(store, segment, project_id, identity, kind, result):
    current = read_goal(store, project_id)
    if (not active_goal(current) or current["status"] == "blocked"
            or identity in current["evidence"] and current["revision"] == current["checked_revision"]):
        return False

    def record(goal):
        if active_goal(goal) and add_evidence(goal, identity, kind, result[:6000]):
            goal["status"] = "ready"

    mutate_goal(store, project_id, record)
    segment.bind(project_id)
    return True


def _tool_completion(store, segment, identity):
    if not isinstance(identity, str):
        return False
    projects = store.db._read_all(
        "SELECT subagent_id FROM delegated_projects WHERE json_extract(workspace_json, '$.goal_state.operation_id')=?",
        (identity,),
    )
    if not projects:
        return False
    _, receipt = owned_operation(store, identity)
    if not receipt or receipt["state"] == "running" or not receipt["deferred"]:
        return False
    result = json.dumps({"state": receipt["state"], "result": json.loads(receipt["result_json"] or "null")})
    recognized = False
    available = False
    for (project_id,) in projects:
        try:
            available |= _record_result(store, segment, project_id, identity, "native operation", result)
        except ValueError:
            continue
        recognized = True
    if recognized and not available:
        segment.stale_inputs += 1
    return recognized


def _native_completion(store, segment, identity):
    if identity.startswith("project-msg-"):
        message = store.message(identity)
        if not message or not message.get("result_json"):
            return False
        worker_id = message["subagent_id"]
        store.get(worker_id)
        origin = json.loads(message["origin_json"])
        project_id = origin.get("goal_project_id") or worker_id
        try:
            if not _record_result(store, segment, project_id, identity, "worker report", message["result_json"]):
                segment.stale_inputs += 1
        except ValueError:
            return False
        return True

    row = store.db._read_one("SELECT event_json FROM async_delegations WHERE delegation_id=?", (identity,))
    if not row or not row[0]:
        return False
    event = json.loads(row[0])
    return _bound_event(store, segment, event.get("main_project_event", {}))


def _ingest(agent, segment, records, receipts=()):
    from hermes_runtime.adapters.turns import _human_followup

    recognized = 0
    with ProjectStore(agent) as store:
        for identity in receipts:
            try:
                recognized += int(_native_completion(store, segment, identity))
            except ValueError:
                continue  # Another conversation's receipt is never a Project Goal input.
        for identity, content in records:
            header = _event_header(content)
            previous = segment.stale_inputs
            if _bound_event(store, segment, header):
                recognized += 1
                if segment.stale_inputs > previous:
                    segment.obsolete_records.add(identity)
            elif not header or _human_followup(content):
                segment.input_id = identity
                segment.internal = False
    return recognized


def ingest_records(agent, records):
    segment = current_segment(agent)
    if segment is None or not records:
        return records
    if _ingest(agent, segment, records) < len(records):
        segment.quiet_source = False
        if not segment.internal:
            segment.quiet = False
    return _without_obsolete(agent._agentready_active_turn, segment, records)


def _without_obsolete(turn, segment, records):
    if not segment.obsolete_records:
        return records
    turn.inbox.acknowledge_obsolete(segment.obsolete_records)
    return [(identity, content) for identity, content in records if identity not in segment.obsolete_records]


def _input_segment(agent, kwargs, records):
    row = admitted_input.get() or {}
    descriptor = _automation(row) if not records else None
    receipts = _automation_receipts(descriptor)
    segment = GoalSegment(
        row.get("admission_id") if not records else None,
        bool(descriptor or records),
        {item["tool_call_id"] for item in (kwargs.get("conversation_history") or ())
         if item.get("role") == "tool" and item.get("tool_call_id")},
    )
    recognized = _ingest(agent, segment, records, receipts) if records or receipts else 0
    segment.quiet_source = bool(recognized and recognized == len(receipts) + len(records) and segment.internal)
    only_stale = segment.quiet_source and segment.stale_inputs == recognized
    return segment, only_stale


def _home_recovery_records(turn):
    receipts = _automation_receipts(_automation(admitted_input.get() or {}))
    if len(receipts) == 1 and receipts[0].startswith("home-recovery-"):
        return turn.take_records(only_if=goal_inbox_event)
    return []


def _queued_prompt(records, steer, kwargs):
    from hermes_runtime.events import combine_event_content

    kwargs.pop("user_message", None)
    kwargs.pop("persist_user_message", None)
    kwargs["persist_user_platform_id"] = records[0][0] if records else None
    kwargs["persist_user_display_metadata"] = {"agentready_inbox_ids": [i for i, _ in records]}
    return combine_event_content([*([steer] if steer else []), *(content for _, content in records)])


def run_segment(agent, turn, proceed, *args, goal_records=(), goal_steer=None, **kwargs):
    from agent.notification_presentation import notification_turn
    from tools.delegate_external_wait import _owns_main

    if not _owns_main(agent):
        return proceed(*args, **kwargs)
    goal_records = goal_records or _home_recovery_records(turn)
    segment, only_stale = _input_segment(agent, kwargs, goal_records)
    turn.goal_segment = segment
    retained = _without_obsolete(turn, segment, goal_records)
    if only_stale and not goal_steer:
        return {"final_response": "", "messages": kwargs.get("conversation_history", []), "completed": True,
                "session_id": agent.session_id, "_notification_reply_muted": True}
    if goal_records:
        args = (_queued_prompt(retained, goal_steer, kwargs), *args[1:])
    if goal_steer:
        segment.quiet_source = False
    original = args[0] if args else kwargs.get("user_message")
    if segment.project_ids and isinstance(original, str):
        prompt = original + "\n\nResume these saved Project Goals by exact project_id: " + ", ".join(sorted(segment.project_ids))
        if args:
            args = (prompt, *args[1:])
        else:
            kwargs["user_message"] = prompt
        kwargs.setdefault("persist_user_message", original)
    segment.quiet = segment.quiet_source
    with notification_turn(agent, muted=segment.quiet_source, session_id=agent.session_id):
        result = proceed(*args, **kwargs)
        if result.get("completed") is False and not result.get("interrupted") and segment.project_ids:
            result["final_response"] = finish_segment(agent, result.get("final_response"), verify=False)
    if segment.quiet:
        return {**result, "final_response": "", "_notification_reply_muted": True}
    return result


def _goal_judgments(goals):
    from agent.auxiliary_client import call_llm
    from hermes_cli.goals import _call_goal_judge_llm, _goal_judge_timeout

    prompt = json.dumps(goals, ensure_ascii=False)
    try:
        result = json.loads(_call_goal_judge_llm(call_llm, _JUDGE, prompt, _goal_judge_timeout()))
    except Exception as error:  # health: allow BLE001 -- provider errors may contain the prompt or credentials
        logger.warning("Project Goal verification failed (%s); checkpoints remain saved", type(error).__name__)
        return {}
    if not isinstance(result, dict):
        return {}
    return {
        project_id: tuple(evidence)
        for project_id, evidence in result.items()
        if project_id in goals and isinstance(evidence, list)
        and all(isinstance(reference, str) and reference in goals[project_id]["evidence"] for reference in evidence)
    }


def _working(store, project_id):
    return bool(store.db._read_one(
        """SELECT 1 FROM delegated_project_messages m JOIN delegated_projects p USING(subagent_id)
           WHERE p.paused=0 AND p.archived=0 AND m.state IN ('pending', 'running')
             AND json_extract(m.origin_json, '$.wake')=1 AND
                 (m.subagent_id=? OR json_extract(m.origin_json, '$.goal_project_id')=?) LIMIT 1""",
        (project_id, project_id),
    ))


def _waiting(store, project_id, *, until_deadline=False):
    from cron.wait_binding import store_home
    from cron.wait_evidence import has_live_timer

    wait = json.loads(store.get(project_id)["workspace_json"]).get("external_wait") or {}
    if not wait:
        return None
    try:
        store_home()
    except (ValueError, RuntimeError, OSError):
        return None
    if has_live_timer(wait, project_id, datetime.now(UTC), until_deadline=until_deadline):
        return wait["wait_id"]
    return None


def _awaiting_operation(store, goal):
    identity = goal.get("operation_id")
    return bool(identity and identity not in goal["evidence"] and operation_returns(store, identity))


def _branch(store, project_id, goal):
    if _working(store, project_id):
        return {"status": "running", "wake_id": None, "attempts_without_progress": 0}
    if _awaiting_operation(store, goal):
        return {"status": "awaiting_operation", "wake_id": None, "attempts_without_progress": 0}
    if wait_id := _waiting(store, project_id, until_deadline=goal["status"] == "waiting"):
        return {"status": "waiting", "wait_id": wait_id, "wake_id": None, "attempts_without_progress": 0}
    return {"status": "ready"}


def _write_transition(store, project_id, branch, checked_revision, consumed):
    from hermes_cli.goals import DEFAULT_MAX_TURNS

    def advance(goal):
        if not active_goal(goal) or goal["status"] == "blocked":
            return goal
        if checked_revision is not None:
            goal["checked_revision"] = checked_revision
        goal.update(branch)
        if goal["status"] != "waiting":
            goal.pop("wait_id", None)
        if goal["status"] != "ready":
            return goal
        if consumed and consumed == goal.get("wake_id"):
            goal["attempts_without_progress"] += 1
            goal["wake_id"] = None
        if goal["attempts_without_progress"] >= DEFAULT_MAX_TURNS:
            goal.update(status="blocked", blocker="No verified progress after the Goal turn budget",
                        unblock="Review the last recorded step and resume with a concrete next action")
        elif not goal.get("wake_id"):
            goal["wake_sequence"] += 1
            goal["wake_id"] = f'{project_id}:{goal["wake_sequence"]}'
        return goal
    return mutate_goal(store, project_id, advance)


def _publish(project_id, goal):
    from hermes_runtime.standalone.events import _admit_scheduled_prompt

    if goal["status"] == "ready" and goal.get("wake_id"):
        _admit_scheduled_prompt(
            "Continue the saved Project Goal. Read its last confirmed stage and reconcile any accepted tool work "
            "before performing the next applicable step. Do not repeat external effects with unknown outcomes.",
            goal["wake_id"], {"project_id": project_id}, source="goal",
        )


def _consumed_wake(store, wake_id):
    from gateway.session_automation import find_automation_admission
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.events import scheduled_prompt_id

    if HomeInbox(store.db_path.resolve().parent).event_receipt("goal", wake_id, delivered=True):
        return True
    native_id = scheduled_prompt_id("goal", wake_id)
    ledger = store.db._read_one("SELECT delivery_state FROM async_delegations WHERE delegation_id=?", (native_id,))
    if not ledger or ledger[0] != "delivered":
        return False
    identity = json.dumps(["async_delegation", native_id, ""], separators=(",", ":"))
    admission = find_automation_admission(store.db, HOME_SESSION_ID, identity)
    return bool(admission and admission["status"] == "terminal" and admission["outcome"] in {"completed", "failed"})


def _recover_ready(store, project_id, goal):
    wake_id = goal.get("wake_id")
    if not wake_id or _consumed_wake(store, wake_id):
        goal = _write_transition(store, project_id, _branch(store, project_id, goal), None, wake_id)
    _publish(project_id, goal)


def finish_segment(agent, response, *, verify=True, final_message=None):
    """Finalize selected goals before the model's final response is presented."""
    segment = current_segment(agent)
    if segment is None or not segment.project_ids:
        return response
    blockers = []
    with ProjectStore(agent) as store:
        snapshots = {pid: read_goal(store, pid) for pid in sorted(segment.project_ids)}
        candidates = {
            pid: {"contract": goal["contract"], "evidence": goal["evidence"]}
            for pid, goal in snapshots.items()
            if verify and active_goal(goal) and goal["revision"] > goal["checked_revision"]
        }
        verdicts = _goal_judgments(candidates) if candidates else {}
        for project_id, goal in snapshots.items():
            if goal["status"] == "done":
                segment.notify = True
            if not active_goal(goal) or goal["status"] == "blocked":
                continue
            evidence = verdicts.get(project_id)
            if evidence:
                try:
                    close_goal(store, project_id, "done", evidence)
                except (ValueError, OSError, RuntimeError, sqlite3.Error):
                    logger.warning("Project Goal cleanup deferred", exc_info=True)
                else:
                    segment.notify = True
                continue
            current = _write_transition(store, project_id, _branch(store, project_id, goal),
                                        goal["revision"] if evidence is not None else None,
                                        segment.wake_ids.get(project_id))
            if current["status"] == "blocked":
                blockers.append(f'{goal["contract"]["outcome"]}\n{current["blocker"]}. {current["unblock"]}.')
                segment.notify = True
            try:
                _publish(project_id, current)
            except (OSError, RuntimeError, sqlite3.Error):
                logger.warning("Project Goal wake deferred; next step remains saved", exc_info=True)
    if segment.quiet_source:
        segment.quiet = not segment.notify
        agent._mute_notification_reply = segment.quiet
    if blockers:
        response = "\n\n".join(filter(None, (response, *blockers)))
        if final_message is not None:
            final_message["api_content" if "api_content" in final_message else "content"] = response
    return response


def recover():
    """Use the existing main inbox recovery pass for interrupted Goal transitions."""
    from types import SimpleNamespace
    from hermes_runtime.adapters.state import current_home_session_id
    from hermes_state import SessionDB

    with SessionDB() as db, ProjectStore(SimpleNamespace(
        _session_db=db, session_id=current_home_session_id()
    )) as store:
        rows = db._read_all(
            """SELECT subagent_id FROM delegated_projects WHERE paused=0 AND archived=0
               AND json_extract(workspace_json, '$.goal_state.status') IN ('ready','closing','waiting','awaiting_operation')"""
        )
        for (project_id,) in rows:
            try:
                goal = read_goal(store, project_id)
                if goal["status"] == "closing":
                    close_goal(store, project_id, goal["outcome"], goal["final_evidence"])
                elif goal["status"] == "ready":
                    _recover_ready(store, project_id, goal)
                elif (goal["status"] == "waiting" and not _waiting(store, project_id, until_deadline=True)
                      or goal["status"] == "awaiting_operation" and not _awaiting_operation(store, goal)):
                    branch = _branch(store, project_id, goal)
                    current = _write_transition(store, project_id, branch, None, None)
                    _publish(project_id, current)
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                logger.debug("Could not reconcile a saved Project Goal", exc_info=True)
