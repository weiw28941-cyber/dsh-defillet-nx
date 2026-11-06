import json

from webapp.backend.app import config
from webapp.start import parse_args


def test_opencode_config_requires_explicit_path(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_PATH", raising=False)
    assert config._opencode_provider() == {}
    monkeypatch.setenv("OPENCODE_CONFIG_PATH", "  ")
    assert config._opencode_provider() == {}


def test_opencode_provider_loads_selected_provider(tmp_path, monkeypatch):
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps({
        "model": "example/test-model",
        "provider": {"example": {"options": {
            "baseURL": "https://example.invalid/v1",
            "apiKey": "test-only-not-a-credential",
            "headers": {"X-Test": "test-user"},
        }}},
    }), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG_PATH", str(path))
    assert config._opencode_provider() == {
        "path": str(path.resolve()),
        "provider": "example",
        "base_url": "https://example.invalid/v1",
        "api_key": "test-only-not-a-credential",
        "model": "test-model",
        "headers": {"X-Test": "test-user"},
    }


def test_opencode_config_missing_or_invalid(tmp_path, monkeypatch):
    path = tmp_path / "opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG_PATH", str(path))
    assert config._opencode_provider() == {}
    path.write_text("not json", encoding="utf-8")
    assert config._opencode_provider() == {}


def test_launcher_uses_active_or_explicit_python(monkeypatch):
    import sys

    monkeypatch.delenv("DEFEATURE_PYTHON", raising=False)
    assert parse_args([]).python == sys.executable
    monkeypatch.setenv("DEFEATURE_PYTHON", "configured-python")
    assert parse_args([]).python == "configured-python"
    assert parse_args(["--python", "explicit-python"]).python == "explicit-python"
