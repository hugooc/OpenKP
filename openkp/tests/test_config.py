"""Smoke tests for openkp.config."""


import pytest

from openkp.config import load_config


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    # load_dotenv() walks up from config.py's location, so it always finds
    # the project .env. Stub it out so tests only see monkeypatched env vars.
    monkeypatch.setattr("openkp.config.load_dotenv", lambda *a, **kw: None)


def test_load_config_missing_username(monkeypatch, tmp_path):
    monkeypatch.delenv("KP_USERNAME", raising=False)
    monkeypatch.delenv("KP_PASSWORD", raising=False)
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="KP_USERNAME"):
        load_config()


def test_load_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KP_USERNAME", "test@example.com")
    monkeypatch.setenv("KP_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    cfg = load_config()
    assert cfg.username == "test@example.com"
    assert cfg.password == "hunter2"
    assert cfg.data_dir == tmp_path
