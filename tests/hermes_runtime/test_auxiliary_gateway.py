"""Configured auxiliary routes use only the owning employee's gateway credential."""

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_runtime.standalone.inference_route import auxiliary_route


def test_profile_credential_is_required_and_direct_path_stays_default():
    token = set_secret_scope({"AGENTSEE_INFERENCE_KEY": "synthetic-employee-key"})
    try:
        assert auxiliary_route({}) is None
        assert auxiliary_route({"base_url": "https://panel.invalid/app/inference/v1/", "key_env": "AGENTSEE_INFERENCE_KEY"}) == (
            "https://panel.invalid/app/inference/v1", "synthetic-employee-key")
        for url in ("http://panel.invalid", "https://user:pass@panel.invalid", "https://panel.invalid?secret=x"):
            with pytest.raises(ValueError):
                auxiliary_route({"base_url": url, "key_env": "AGENTSEE_INFERENCE_KEY"})
        with pytest.raises(ValueError):
            auxiliary_route({"base_url": "https://panel.invalid", "key_env": "ABSENT_FIXTURE_KEY"})
    finally:
        reset_secret_scope(token)
