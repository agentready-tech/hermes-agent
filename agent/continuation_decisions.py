"""Binary stop review through OpenRouter's Decisions endpoint, not chat completions."""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import httpx

from agent.secret_scope import get_secret_str

logger = logging.getLogger(__name__)

_BASE_URL = "https://openrouter.ai"
_MAX_TIMEOUT_SECONDS = 10.0
_CRITERIA = {
    "stop": "Permit THIS candidate_response as final: completion is evidenced, the user explicitly paused, the candidate itself clearly tells the user about a blocker or asks the necessary question, or a registered wakeup ensures a return. Being blocked alone is NOT sufficient.",
    "continue": "Reject THIS candidate_response as final: an authorized step remains, a wakeup is missing, or the assistant must tell the user about a blocker that the candidate omits. Explaining a blocker is an available action even when technical progress is impossible; a false completion claim needs correction.",
}


def _submit(client: httpx.Client, request: dict[str, Any], key: str) -> Any:
    messages = request["messages"]
    started = time.monotonic()
    response = client.post(
        "/api/alpha/decisions", headers={"Authorization": "Bearer " + key},
        json={"model": request["model"], "state": json.loads(messages[1]["content"]), "questions": {
            "verdict": {"type": "choice", "instructions": messages[0]["content"], "criteria": _CRITERIA},
        }},
    )
    if not response.is_success:
        from agent.redact import redact_sensitive_text

        logger.warning(
            "Continuation Decisions HTTP %s request_id=%s body=%s", response.status_code,
            response.headers.get("x-request-id", ""),
            redact_sensitive_text(response.text.replace(key, "[REDACTED]"), force=True),
        )
    response.raise_for_status()
    body = response.json()
    answer = body["answers"]["verdict"]
    verdict = answer.get("choice")
    if answer.get("type") != "choice" or verdict not in _CRITERIA:
        raise ValueError("Decisions did not return a binary stop choice")
    usage = body.get("usage") or {}
    logger.info(
        "Continuation Decisions verdict=%s model=%s request_id=%s latency_ms=%d input_tokens=%s cost=%s",
        verdict, body.get("model", request["model"]), body.get("id", ""),
        round((time.monotonic() - started) * 1000), usage.get("input_tokens"), usage.get("cost"),
    )
    # The shared auxiliary relay observes a chat-shaped result, just as it does for
    # native Responses/Messages adapters. The actual wire remains Decisions-only.
    return SimpleNamespace(
        id=body.get("id", ""), model=body.get("model", request["model"]),
        choices=[SimpleNamespace(message=SimpleNamespace(
            role="assistant", content=json.dumps({"verdict": verdict}), tool_calls=None,
        ), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=usage.get("input_tokens", 0),
                              completion_tokens=usage.get("output_tokens", 0),
                              total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0)),
    )


def judge_continuation(payload: dict[str, Any], instructions: str, timeout: float, model: str) -> str:
    """One profile-scoped, observed attempt. No retries or fallback to another model."""
    from agent.auxiliary_client import (
        _relay_aux_call_scope, _relay_sync_completion, _set_relay_auxiliary_route,
        _validate_llm_response,
    )

    key = get_secret_str("OPENROUTER_API_KEY").strip()
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not configured for the owning profile")
    if not model:
        raise ValueError("A continuation guard Decisions model must be configured")
    request = {"model": model, "messages": [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]}
    with (
        _relay_aux_call_scope((), {"task": "goal_judge"}),
        httpx.Client(base_url=_BASE_URL, timeout=min(timeout, _MAX_TIMEOUT_SECONDS), follow_redirects=False) as client,
    ):
        _set_relay_auxiliary_route("openrouter", model, "decisions")
        response = _relay_sync_completion(
            client, request, provider="openrouter", api_mode="decisions",
            create=lambda prepared: _submit(client, prepared, key),
        )
        response = _validate_llm_response(response, "goal_judge", provider="openrouter", base_url=_BASE_URL)
        return json.loads(response.choices[0].message.content)["verdict"]
