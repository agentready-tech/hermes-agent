"""The opt-in egress setting changes browser launch flags, never the process-wide proxy."""

import os

import pytest

from tools.bot_desktop import browser


def test_private_proxy_flags_are_shared_and_do_not_disable_chromium_sandbox(monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"browser": {"proxy": "http://10.217.0.2:3128"}})
    previous = dict(os.environ)
    flags = browser.proxy_args()
    assert "--proxy-server=http://10.217.0.2:3128" in flags
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in flags
    assert all("," not in flag for flag in flags)  # agent-browser's documented ARG list delimiter
    dock = browser.dock_argv("/opt/chrome", "/profile", sandbox_bypass=False)
    assert set(flags) <= set(dock)
    assert "--no-sandbox" not in dock
    assert dict(os.environ) == previous


@pytest.mark.parametrize("value", ["http://user:secret@10.217.0.2:3128", "http://127.0.0.1:3128", "http://169.254.169.254", "http://0.0.0.0", "http://public.example", "file:///etc/passwd", "http://10.217.0.2/private"])
def test_proxy_rejects_credentials_local_services_and_nonoperator_targets(value):
    with pytest.raises(ValueError):
        browser.proxy_args(value)


def test_no_proxy_is_a_noop():
    assert browser.proxy_args("") == []
