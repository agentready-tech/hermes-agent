"""Managed unattended review does not publish service messages after a turn."""
from types import SimpleNamespace

import pytest

from agent import background_review
from hermes_runtime.adapters import hermes


@pytest.mark.parametrize("managed,explicit,notify", [
    (True, False, False), (True, True, True), (False, False, True),
])
@pytest.mark.parametrize("fail", [False, True])
def test_review_keeps_only_requested_or_upstream_chat_notices(
    monkeypatch, tmp_path, managed, explicit, notify, fail,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "_home", None)
    if managed:
        hermes.activate(tmp_path)
    notifications = []
    parent = SimpleNamespace(
        _safe_print=lambda *_: None,
        background_review_callback=notifications.append,
        _emit_auxiliary_failure=lambda *_: notifications.append("failure"),
    )

    def run_fork(*_args):
        if fail:
            raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(background_review, "_parent_can_emit_tool_calls", lambda _agent: True)
    monkeypatch.setattr(background_review, "_run_review_fork", run_fork)
    monkeypatch.setattr(background_review, "summarize_background_review_actions", lambda *_a, **_kw: ["Memory updated"])
    background_review._run_review_in_thread(parent, [], "review", explicit=explicit)
    assert len(notifications) == int(notify)
