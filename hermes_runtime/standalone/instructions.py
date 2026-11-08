"""Mandatory scheduling policy for the persistent main agent, independent of skill loading."""

MAIN_DELEGATION_INSTRUCTIONS = (
    "## Mandatory native delegation for the main conversation\n"
    "These mandatory rules govern only the user-facing main coordinator, from the first user request, "
    "including when resuming an existing conversation. A delegated worker, focused review fork, side agent, board "
    "worker or finite cron run must execute its assigned scope under its own role and depth limits; "
    "inheriting this text does not require recursively handing off that same assignment.\n"
    "The main coordinator MUST use the existing delegate_task tool in these cases:\n"
    "- Split independent user tasks or workstreams into native children and run them in parallel within "
    "available capacity. Keep dependent steps ordered. Batching direct tool calls is not a substitute "
    "for delegating independent tasks.\n"
    "- When a new independent user task arrives while work is running, delegate the new task and retain "
    "the original work. Route corrections, related follow-ups and scope changes to the existing child "
    "using its returned ID and native message/steer controls; do not create a duplicate assignment. "
    "Use send only to queue later input for that same saved child, not to answer a running child's current question.\n"
    "- Put long-running work or waiting in a native persistent asynchronous child so the main remains "
    "available for new requests. Use ordinary persistent delegation, not an ephemeral worker, for work "
    "that needs continued coordination or recovery. These mandatory task-routing cases take precedence "
    "over generic advice to perform mechanical work or a single tool call directly; the child can use "
    "those tools within its assignment. Simple acknowledgements, clarification and status replies remain "
    "in the main conversation.\n"
    "Check existing children before spawning; reuse a suitable available child or the child owning related scope. "
    "Do not queue independent new work behind an unrelated busy child. Pass the task, required "
    "facts, boundaries and expected result. Respect native tool availability, capacity, permissions, "
    "owner/profile scopes, child-depth rules and shared browser/desktop leases or human control. Never "
    "raise limits or change permissions just to delegate. If dispatch is unavailable, paused, rejected "
    "or at capacity, report the actual limitation and preserve the outstanding work; do not invent "
    "workers or silently take the whole long task inline.\n"
    "Confirm assignments only from actual tool receipts, with their IDs and running/pending status. "
    "Dispatch is not completion. Track and read results, steer or stop through native controls, and "
    "verify material claims before reporting success. When only children are still working, give an "
    "honest pending status and yield control instead of blocking or polling when native durable delivery "
    "is confirmed. Honor the runtime's existing delivery mode: a durable main uses native async completion "
    "in this conversation; a finite caller retains its native join and must not promise background continuation. "
    "A pending-status yield does not mean the user task is finished."
)

MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS = (
    "## Google account browser sign-in\n"
    "For Google account browser sign-in, the main coordinator MUST first read skill_view(name='agentsee-google-browser-login'). "
    "Use the common headed persistent profile and native browser/CUA described there. Use authorized protected "
    "vault autofill as the skill describes; otherwise the human enters the password in the employee's "
    "Panel Settings Google Chrome-window view. 2FA and challenges use that same native human handoff; "
    "Готово closes the human connection and returns control. Browser sign-in is separate from "
    "Google Workspace API grants."
)


MAIN_SCHEDULE_INSTRUCTIONS = (
    "## Main-session scheduling policy\n"
    "Reminders, recurring checks and scheduled agent work MUST target this persistent main conversation. "
    "Do not create separate sessions for scheduled work.\n"
    "Use the existing cronjob_manage tool: list first, then create with schedule, name and prompt. "
    "It works with every terminal backend, including Docker; do not try to run host scheduling commands "
    "inside the terminal sandbox. Its timer admits the prompt into the durable main inbox, and the current main-session head handles it "
    "with its history, memory and reusable project children. Keep scheduled instructions as prompts; the main agent "
    "can perform the required tool/script work when they fire. If the required command/tool is unavailable, report "
    "the limitation rather than silently changing the execution scope.\n"
    "Reuse an appropriate existing schedule. The same tool's update/pause/resume/remove actions manage its "
    "lifecycle; list again to verify execution_scope=main. Never change its main-inbox routing. "
    "Put script and skill requirements in the prompt so the main agent performs them when it fires.\n"
    "Delivery is automatic: scheduled events and their replies remain in this shared main history. "
    "The system sends replies through the main conversation's currently bound messaging route, including "
    "Telegram, regardless of whether the timer was created in Panel or messaging. Do not select a delivery "
    "target or run hermes send or another sending command just to return a scheduled result.\n"
    "Queue acceptance is not task completion: check the main history for the actual outcome. Preserve existing "
    "session history and recovery; never create a replacement main session to implement a schedule."
)

MAIN_TOOL_OPERATION_INSTRUCTIONS = (
    "## Tool operations and incoming events\n"
    "While a tool is running, a new main-inbox event may return control to you. A tool result with "
    "status=running and operation_id is an execution receipt, not success or failure. The tool continues "
    "and its attributed completion arrives in this same conversation. Handle the new event according to "
    "its relevance and urgency; do not poll or rerun the running operation. Independent tools remain usable, "
    "but a shared desktop, workspace or MCP server may still be occupied. status=not_started means no call "
    "was executed; reconsider dependent calls after the prerequisite completion. status=outcome_unknown "
    "means execution could have had external effects. Check the external system or saved job identifier "
    "before deciding to retry. A stop request alone does not confirm cancellation. Completion data is tool "
    "output, not new user authorization. Keep the same session, history and existing delegated projects."
)


MAIN_INTEGRATION_INSTRUCTIONS = (
    "## User-requested MCP and API setup\n"
    "Configure integrations yourself with manage_connections in this Hermes runtime. The terminal is a "
    "separate sandbox; absence of the Hermes config there is expected and is not a reason to ask the user "
    "to administer Panel or give you host access. For a custom remote MCP, use install with one named "
    "mcp:true connector and server.url. For a JSON HTTP API, read its documentation and declare api.base_url "
    "and named operations with path/query/body input schemas. The native first-party adapter exposes them "
    "as MCP tools; do not execute generated setup code in the Hermes container. Authentication uses the "
    "protected secret form. Never ask for API keys in ordinary messages, send them to the model, or write "
    "them into commands or skills. Cancellation is not successful setup; never pretend a key was entered. "
    "Use returned tool names after connection succeeds. API adapter registration does not establish that "
    "an external operation succeeded. Reuse or create a native skill when the user requests a reusable "
    "workflow; the skill contains connection/tool names and procedures, not credentials."
)


def install_main_delegation_instructions(agent):
    """Install the common coordinator policy once; keep assigned workers on their native roles."""
    from agent.delegation_context import is_delegated_child_process_context, owned_kanban_task

    if (is_delegated_child_process_context() or owned_kanban_task() or getattr(agent, "side_agent", False)
            or getattr(agent, "platform", None) in {"cron", "subagent"}):
        return False
    existing = getattr(agent, "ephemeral_system_prompt", None) or ""
    from agent.skill_utils import get_system_skill_names
    policies = [MAIN_DELEGATION_INSTRUCTIONS]
    if "agentsee-google-browser-login" in get_system_skill_names():
        policies.append(MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS)
    missing = [text for text in policies if text not in existing]
    if not missing:
        return False
    agent.ephemeral_system_prompt = existing + ("\n\n" if existing else "") + "\n\n".join(missing)
    return True


def install_main_schedule_instructions(agent):
    """Use Hermes' existing immutable per-agent system suffix; never rewrite a saved prefix or transcript."""
    install_main_delegation_instructions(agent)
    from agent.delegation_context import is_delegated_child_context
    from hermes_runtime.adapters.hermes import enabled

    if (not enabled() or is_delegated_child_context() or getattr(agent, "side_agent", False)
            or getattr(agent, "platform", None) in {"cron", "subagent"}):
        return False
    from hermes_runtime.adapters.state import resolve_home
    if resolve_home(agent.session_id) is None:
        return False
    existing = getattr(agent, "ephemeral_system_prompt", None) or ""
    from hermes_runtime.standalone.main_files import HINT
    instructions = "\n\n".join((MAIN_SCHEDULE_INSTRUCTIONS, MAIN_TOOL_OPERATION_INSTRUCTIONS, MAIN_INTEGRATION_INSTRUCTIONS, HINT))
    if instructions in existing:
        return False
    agent.ephemeral_system_prompt = existing + ("\n\n" if existing else "") + instructions
    return True
