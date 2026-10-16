"""Reusable project delegates built with the native child runner and completion delivery."""

import hashlib
import json
import logging
import time

from tools.delegate_project_store import ProjectStore, _transaction

logger = logging.getLogger(__name__)


def _origin():
    from tools.delegate_tool_dispatch import _capture_origin
    from tools.async_delegation import _capture_routing_origin
    wake, ui, _, _, delivery = _capture_origin()
    return {"session_key": wake, "origin_ui_session_id": ui, "wake": delivery, **_capture_routing_origin()}


def _publish_completed(store, project, message, result):
    """Repair the crash window between project receipt and native completion publication."""
    from tools.async_delegation import _persist_completion, _persist_dispatch
    from tools.process_registry import process_registry
    origin = json.loads(message["origin_json"])
    if not origin.get("wake"):
        return
    with _transaction() as conn:
        row = conn.execute("SELECT state,owner_pid,owner_started_at FROM async_delegations WHERE delegation_id=?", (message["message_id"],)).fetchone()
    if row and row[0] not in ("running", "finalizing"):
        return
    from tools.async_delegation import _owner_liveness
    alive = _owner_liveness()
    if row and (alive is None or alive(row[1], row[2])):
        return
    event = {"type": "async_delegation", "delegation_id": message["message_id"],
        "parent_session_id": store.parent_id, "origin_session_id": store.parent_id,
        "goal": f"Project {project['project_key']} ({project['subagent_id']}): {project['goal']}",
        "status": result.get("status", "failed"), "summary": result.get("summary"),
        "error": result.get("error"), "dispatched_at": message["accepted_at"],
        "completed_at": message.get("completed_at") or time.time(), **{k: v for k, v in origin.items() if k != "wake"}}
    event["session_key"] = event.get("session_key") or store.parent_id
    if not row:
        _persist_dispatch({**event, "status": event["status"]})
    _persist_completion(event, result)
    process_registry.completion_queue.put(event)


def _build_child(parent, store, project, message):
    from tools.delegate_tool import _build_child_preserving_parent_tools, _load_config, _resolve_delegation_credentials
    from tools.delegate_tool_dispatch import _capture_origin
    _, owner_ui, owner_transport, owner_record, _ = _capture_origin()
    cfg = _load_config()
    creds = _resolve_delegation_credentials(cfg, parent)
    sid = store.resolve(project["session_id"])
    child = _build_child_preserving_parent_tools(task_index=0, goal=project["goal"], context=project["context"],
        toolsets=None, model=creds.get("model"), max_iterations=cfg.get("max_iterations", 250), task_count=1,
        parent_agent=parent, session_id=sid, subagent_id=project["subagent_id"], override_provider=creds["provider"],
        override_base_url=creds["base_url"], override_api_key=creds["api_key"],
        override_api_mode=creds["api_mode"], override_request_overrides=creds.get("request_overrides"),
        override_acp_command=creds.get("command"), override_acp_args=creds.get("args"), routing_cfg=cfg)
    child.ephemeral_system_prompt = store.instruction(project["subagent_id"], child.ephemeral_system_prompt)
    child._persistent_project_id = project["subagent_id"]
    child._delegation_id = message["message_id"]
    child._progress_identity_ref["delegation_id"] = message["message_id"]
    child._delegate_owner_kwargs = {"owner_session_id": owner_ui or None,
        "owner_transport": owner_transport, "owner_session_record": owner_record}
    workspace = json.loads(project["workspace_json"])
    child._persistent_cwd = workspace.get("cwd")
    child._delegate_existing_worktree = workspace.get("worktree")
    if child._delegate_existing_worktree:
        from pathlib import Path
        if not Path(child._delegate_existing_worktree["path"]).is_dir():
            child.close()
            raise ValueError("The project's saved worktree is missing; restore it before resuming this project")
    child._delegate_save_workspace = lambda info: _save_workspace(parent, project["subagent_id"], info)
    history = store.history(sid) if store.session_exists(sid) else []
    # A crash may have already admitted this exact user row; continue from that request,
    # without inserting a second copy or discarding completed tool results.
    pending = next((m for m in reversed(history) if m.get("role") == "user" and m.get("message_id") == message["message_id"]), None)
    text = message["content"]
    options = json.loads(message["options_json"])
    child._delegate_output_schema = options.get("output_schema")
    if options.get("output_schema"):
        from tools.delegation_output_schema import append_output_contract
        text = append_output_contract(text, options["output_schema"])
    if not pending:
        child._delegate_images = options.get("images")
    if pending:
        text = ("Continue the interrupted project task below. Review the existing tool outcomes and check actual "
                "external state before repeating actions whose outcome is unknown.\n\n" + text)
    manager = getattr(parent, "_memory_manager", None)
    if manager and not pending:
        try:
            facts = manager.prefetch_all(text, session_id=sid)
            if facts:
                text += "\n\nRelevant memory (verify its sources):\n" + facts
        except Exception:
            logger.warning("Project memory recall failed", exc_info=True)
    child._delegate_resume_kwargs = {"conversation_history": history,
                                   "persist_user_platform_id": message["message_id"]}
    return child, text


def _run(parent, store, project, message, child, text):
    from tools.delegate_tool import _run_single_child
    try:
        result = _run_single_child(task_index=0, goal=text, child=child, parent_agent=parent,
                                   **child._delegate_owner_kwargs)
        result.update(subagent_id=project["subagent_id"], session_id=child.session_id,
                      project=project["project_key"], message_id=message["message_id"])
        store.settle(project, message, result)
        return result
    except Exception as exc:
        logger.debug("Persistent project execution failed", exc_info=True)
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "summary": None,
                  "subagent_id": project["subagent_id"], "session_id": project["session_id"]}
        try:
            store.settle(project, message, result)
        except Exception:
            store.release(message)
            raise
        return result


def recover_projects(parent):
    """Drain saved project inputs; a dead worker never owns the durable project identity."""
    with ProjectStore(parent) as store:
        return _recover_projects(parent, store)


def _recover_projects(parent, store):
    from tools.async_delegation import dispatch_async_delegation
    from tools.delegate_tool import _get_max_concurrent_children, is_spawn_paused
    if is_spawn_paused():
        return []
    outcomes = []
    for project in store.projects():
        for receipt in store.receipts(project["subagent_id"]):
            _publish_completed(store, project, receipt, json.loads(receipt["result_json"]))
        if project["archived"]:
            continue
        while (message := store.claim(project)) is not None:
            result = store.committed_result(project, message)
            if result:
                store.settle(project, message, result)
                _publish_completed(store, project, message, result)
                outcomes.append(result)
                continue
            try:
                child, text = _build_child(parent, store, project, message)
            except Exception:
                store.release(message)
                raise
            origin = json.loads(message["origin_json"])
            if origin.get("wake"):
                # Capture each iteration's objects, never the loop's mutable cells.
                def runner(p=project, m=message, c=child, t=text):
                    try:
                        with ProjectStore(parent) as worker_store:
                            return _run(parent, worker_store, p, m, c, t)
                    finally:
                        recover_projects(parent)
                handle = dispatch_async_delegation(goal=f"Project {project['project_key']}: {project['goal']}",
                    context=project["context"], toolsets=None, role="leaf", model=child.model,
                    session_key=origin.get("session_key") or store.parent_id, parent_session_id=store.parent_id,
                    origin_ui_session_id=origin.get("origin_ui_session_id", ""), origin_session_id=store.parent_id,
                    delegation_id=message["message_id"], routing_origin=origin, runner=runner,
                    interrupt_fn=lambda sid=project["subagent_id"]: _pause_child(parent, sid),
                    max_async_children=_get_max_concurrent_children())
                if handle["status"] != "dispatched":
                    child.close()
                    store.release(message)
                outcomes.append(handle)
                break
            result = _run(parent, store, project, message, child, text)
            outcomes.append(result)
    return outcomes


def include_projects(parent, payload, query=""):
    if getattr(parent, "_session_db", None) is None:
        return payload
    payload.update(project_action(parent, "list", query=query))
    if payload.get("projects"):
        payload.pop("note", None)
    return payload


def project_action(parent, action, *, tasks=None, subagent_id=None, message=None, query="", event_id=None):
    with ProjectStore(parent) as store:
        return _project_action(parent, store, action, tasks, subagent_id, message, query, event_id)


def _project_action(parent, store, action, tasks, subagent_id, message, query, event_id):
    if action == "list":
        projects = store.projects(query)
        fields = ("subagent_id", "session_id", "project_key", "goal", "status", "last_status", "last_summary", "last_message_id")
        return {"projects": [{k: p[k] for k in fields if k in p} for p in projects[:50]], "project_count": len(projects)}
    if action in ("stop", "archive"):
        store.get(subagent_id)
        store.pause(subagent_id, archive=action == "archive")
        from tools.delegate_tool_registry import interrupt_subagent
        interrupt_subagent(subagent_id)
        return {"subagent_id": subagent_id, "status": "archived" if action == "archive" else "paused"}
    if action == "read":
        project = store.get(subagent_id)
        sid = store.resolve(project["session_id"])
        history = store.history(sid) if store.session_exists(sid) else []
        return {"project": {k: v for k, v in project.items() if k != "instruction"}, "messages": [{"role": m["role"], "content": str(m.get("content") or "")[:2000]} for m in history[-12:]],
                "note": "Recent conversation excerpt; use session_search with this session_id for older evidence."}
    origin = _origin()
    entries = []
    if action == "spawn":
        if not tasks or any(not t.get("project") or not isinstance(t.get("goal"), str) or not t["goal"].strip() for t in tasks):
            raise ValueError("Use a nonempty project key and goal on every persistent task; submit ephemeral tasks separately")
        # Validate the whole batch before accepting any project input.
        for task in tasks:
            if not isinstance(task["project"], str) or not task["project"].strip():
                raise ValueError("project must be a nonempty string")
            if task.get("context") is not None and not isinstance(task["context"], str):
                raise ValueError("Project context must be text")
            if task.get("group"):
                raise ValueError("Persistent project messages return independently; omit group on project tasks")
        archived = {p["project_key"] for p in store.projects() if p["archived"]}
        if any(t["project"].strip() in archived for t in tasks):
            raise ValueError("A project in this batch is archived; choose another key")
        for task in tasks:
            from tools.delegate_tool_progress import _resolve_workspace_hint
            project = store.create(task["project"], task["goal"], task.get("context"), _resolve_workspace_hint(parent))
            text = task["goal"] + ("\n\n" + task["context"] if task.get("context") else "")
            options = {k: task[k] for k in ("images", "output_schema") if task.get(k) is not None}
            seed = "spawn:" + hashlib.sha256(json.dumps([text, options], sort_keys=True).encode()).hexdigest()
            mid = store.enqueue(project, text, origin, seed, options)
            entries.append({"subagent_id": project["subagent_id"], "session_id": project["session_id"], "message_id": mid})
    elif action == "send":
        project = store.get(subagent_id)
        store.pause(subagent_id, False)
        mid = store.enqueue(project, message, origin, event_id)
        entries.append({"subagent_id": project["subagent_id"], "session_id": project["session_id"], "message_id": mid})
    else:
        raise ValueError("Unknown project action")
    results = recover_projects(parent)
    for entry in entries:
        receipt = store.message(entry["message_id"])
        entry["status"] = receipt["state"]
        if receipt["result_json"]:
            entry["result"] = json.loads(receipt["result_json"])
    return {"projects": entries, "results": results}


def _pause_child(parent, subagent_id):
    from tools.delegate_tool_registry import interrupt_subagent
    with ProjectStore(parent) as store:
        store.pause(subagent_id)
    return interrupt_subagent(subagent_id)


def _save_workspace(parent, subagent_id, info):
    with ProjectStore(parent) as store:
        store.save_workspace(subagent_id, info)


def recover_project_work(parent):
    """Root turn entry: replay accepted inputs even when the model does not call delegation."""
    if getattr(parent, "_delegate_depth", 0) or getattr(parent, "_session_db", None) is None:
        return
    names = getattr(parent, "valid_tool_names", ())
    if not isinstance(names, (set, list, tuple, frozenset)) or "delegate_task" not in names:
        return
    try:
        recover_projects(parent)
    except Exception:
        logger.exception("Could not resume pending project delegates; inputs remain saved")
