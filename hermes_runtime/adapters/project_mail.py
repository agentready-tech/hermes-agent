"""Dispatch managed subagent inboxes without admitting a main conversation turn."""

import asyncio
import json
from types import SimpleNamespace


def is_project_mail(event):
    from hermes_runtime.adapters.hermes import enabled

    identity = str(event.get("delegation_id") or "")
    return (event.get("type") == "async_delegation" and event.get("durable_recovery") is True
            and ((event.get("agent_message") is True and identity.startswith("agent-mail-"))
                 or (event.get("mail_project_message_id") and identity.startswith("project-recovery-")))
            and enabled())


async def dispatch_project_mail(runner, event, parent_session):
    """Return None for mail addressed to main, False for retry, True for a saved dispatch."""
    handled, parent, project_id = await asyncio.to_thread(_prepare, runner, event, parent_session)
    if parent is not None:
        from tools.delegate_projects import recover_project_work

        task = asyncio.create_task(asyncio.to_thread(recover_project_work, parent, {project_id}))
        runner._retain_background_task(task)
    return handled


def _prepare(runner, event, parent_session):
    from hermes_runtime.adapters.state import resolve_home
    from tools.delegate_child_goals import child_event_state
    from tools.delegate_mail_store import MailStore, pending_mail_rows
    from tools.delegate_mail_work import ensure_mail_input
    from tools.delegate_project_store import ProjectStore, _goal_input_state, _transaction

    root = resolve_home(parent_session)
    if root is None:
        return None, None, None
    db = runner._session_db
    if db is None:
        return False, None, None
    recovery_id = event.get("mail_project_message_id")
    origin = {k: event[k] for k in ("session_key", "origin_session_id", "origin_ui_session_id", "platform", "profile",
                                   "chat_id", "chat_type", "thread_id", "user_id", "scope_id") if k in event}
    origin["project_mail_root"] = root
    root_actor = SimpleNamespace(session_id=root, _session_db=db, _project_mail_origin=origin)
    with MailStore(root_actor) as mail:
        if recovery_id:
            saved = mail.db._read_one("SELECT subagent_id, options_json FROM delegated_project_messages WHERE message_id=?",
                                      (recovery_id,))
            if not saved or not (mail_id := json.loads(saved[1]).get("agent_mail_id")):
                return True, None, None
        else:
            mail_id = event["delegation_id"]
        row = mail.db._read_one("SELECT * FROM delegation_mail WHERE message_id=?", (mail_id,))
        if row is None or mail.tip(row["root_session_id"]) != root:
            return True, None, None
        target = mail.tip(row["target_session_id"])
        if target == root:
            return None, None, None
        if row["delivered_at"] is not None and not recovery_id:
            return True, None, None
        path = []
        while target != root:
            project = mail.nodes.get(target)
            if not project or project in path or project["archived"]:
                return True, None, None  # Ephemeral recipients drain their own live inboxes.
            if project["paused"]:
                return False, None, None
            path.append(project)
            target = mail.parent(target)
        path.reverse()

    with _transaction() as conn:
        if not recovery_id and not pending_mail_rows(conn, message_ids=[mail_id]):
            origin = json.loads(row["origin_json"])
            state = (child_event_state(conn, row["origin_json"]) if row["kind"] == "child_goal"
                     else _goal_input_state(conn, origin.get("message_goal_project_id")))
            return state != "paused", None, None
    if recovery_id:
        depth = next((i for i, project in enumerate(path) if project["subagent_id"] == saved[0]), None)
        if depth is None:
            return True, None, None
    else:
        # A scoped Child Goal still uses its existing orchestrator carrier so its
        # evidence is reviewed by the actor who owns the stage.
        scoped = json.loads(row["origin_json"]).get("message_goal_project_id")
        depth = 0 if scoped else len(path) - 1
    project = path[depth]
    owner = root_actor if depth == 0 else SimpleNamespace(
        session_id=project["parent_session_id"], _session_db=db, _delegate_depth=depth, _project_mail_origin=origin)
    with ProjectStore(owner) as projects:
        queued = recovery_id or ensure_mail_input(owner, projects, project, mail_id=mail_id)
        if queued is None:
            # A running recipient will pick it up at its next safe boundary;
            # leave the transport retryable until its history or queue owns it.
            return False, None, None
    parent = _project_owner(runner, event, root_actor, path[:depth])
    return parent is not None, parent, project["subagent_id"]


def _project_owner(runner, event, root_actor, ancestors):
    """Resolve the same profile model and inherited tool restrictions as normal delegation."""
    from gateway.run import _load_gateway_config, _platform_config_key
    from hermes_cli.fallback_config import get_fallback_chain
    from tools.delegate_tool import _load_config, _resolve_delegation_credentials
    from tools.delegate_tool_config import _resolve_child_credential_pool, _resolve_child_runtime
    from tools.delegate_tool_toolsets import _resolve_child_toolsets

    source = runner._build_process_event_source(event)
    if source is None:
        return None
    config = _load_gateway_config()
    model, runtime = runner._resolve_session_agent_runtime(source=source, session_key=event["session_key"],
                                                          user_config=config, report_fallback=False)
    enabled, disabled = runner._resolve_turn_toolsets(config, source, _platform_config_key(source.platform))
    root_actor.__dict__.update({**runtime, "acp_command": runtime.get("command"), "acp_args": runtime.get("args")},
                               model=model, enabled_toolsets=enabled, disabled_toolsets=disabled,
                               valid_tool_names=("delegate_task",), _fallback_chain=get_fallback_chain(config),
                               _credential_pool=runtime.get("credential_pool"))
    delegation = _load_config()
    parent = root_actor
    for depth, ancestor in enumerate(ancestors, 1):
        creds = _resolve_delegation_credentials(delegation, parent)
        runtime = _resolve_child_runtime(parent, delegation, parent.api_key, model=creds["model"],
            override_provider=creds["provider"], override_base_url=creds["base_url"], override_api_key=creds["api_key"],
            override_api_mode=creds["api_mode"], override_acp_command=creds.get("command"), override_acp_args=creds.get("args"))
        enabled, disabled = _resolve_child_toolsets(parent, None, "orchestrator")
        pool = _resolve_child_credential_pool(runtime["provider"], parent, runtime["base_url"],
                                              effective_requested_provider=runtime.get("requested_provider"))
        parent = SimpleNamespace(**runtime, session_id=ancestor["session_id"], _session_db=parent._session_db,
            enabled_toolsets=enabled, disabled_toolsets=disabled, valid_tool_names=("delegate_task",),
            _delegate_depth=depth, _subagent_id=ancestor["subagent_id"], _fallback_chain=runtime.get("fallback_model"),
            _credential_pool=pool,
            _project_mail_origin=root_actor._project_mail_origin, request_overrides=creds.get("request_overrides"))
    return parent
