"""The configured STT gateway participates in both availability and dispatch."""

from unittest.mock import Mock

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home
from tools import transcription_cloud, transcription_tools


def test_explicit_managed_stt_uses_employee_key_without_tts_or_openai_key(monkeypatch, tmp_path):
    config = {"provider": "openai", "openai": {"model": "fixture/transcribe",
        "base_url": "https://panel.invalid/app/inference/v1", "key_env": "AGENTSEE_INFERENCE_KEY"}}
    atomic_config_write(get_hermes_home() / "config.yaml", {"stt": config})
    monkeypatch.setattr(transcription_tools, "_HAS_OPENAI", True)
    legacy = Mock(side_effect=AssertionError("STT must not borrow TTS credentials"))
    monkeypatch.setattr(transcription_tools, "_resolve_openai_audio_client_config", legacy)
    monkeypatch.setattr(transcription_cloud, "_resolve_openai_audio_client_config", legacy)
    calls = []
    monkeypatch.setattr(transcription_cloud, "_with_openai_client",
        lambda key, url, file_path, label, body: calls.append((key, url)) or {"success": True, "transcript": "Synthetic voice"})
    token = set_secret_scope({"AGENTSEE_INFERENCE_KEY": "fixture-employee-key"})
    try:
        assert transcription_tools._resolve_explicit_provider("openai") == "openai"
        result = transcription_cloud._transcribe_openai(str(tmp_path / "voice.wav"), "fixture/transcribe")
    finally:
        reset_secret_scope(token)
    assert result["transcript"] == "Synthetic voice"
    assert calls == [("fixture-employee-key", "https://panel.invalid/app/inference/v1")]
    legacy.assert_not_called()
