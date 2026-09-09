"""Tests for config: .env parsing and API-key precedence.

These never touch the user's real .env — ROOT/CONFIG_PATH are pointed at a
temp location and the environment variable is controlled via monkeypatch.
"""

import pytest

from paperlib import config


@pytest.fixture
def env_root(tmp_path, monkeypatch):
    """Point config at a throwaway root/data dir, with no real env var set."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", data_dir / "config.json")
    # Ensure no inherited shell env var leaks into these tests.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


def _write_env(root, body):
    (root / ".env").write_text(body, encoding="utf-8")


# ---- .env parsing --------------------------------------------------------

def test_load_dotenv_parses_lines(env_root):
    _write_env(env_root, "\n".join([
        "# a comment line",
        "",
        "ANTHROPIC_API_KEY=plain-value",
        "QUOTED=\"double quoted\"",
        "SINGLE='single quoted'",
        "export EXPORTED=exported-value",
        "  SPACED = spaced ",
        "malformed line without equals",
    ]))
    values = config.load_dotenv_file()
    assert values["ANTHROPIC_API_KEY"] == "plain-value"
    assert values["QUOTED"] == "double quoted"
    assert values["SINGLE"] == "single quoted"
    assert values["EXPORTED"] == "exported-value"
    assert values["SPACED"] == "spaced"
    assert "malformed line without equals" not in values


def test_load_dotenv_missing_file_returns_empty(env_root):
    # No .env written -> {} and never raises.
    assert config.load_dotenv_file() == {}


# ---- get_api_key precedence ---------------------------------------------

def test_env_var_beats_dotenv_and_config(env_root, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell-env")
    _write_env(env_root, "ANTHROPIC_API_KEY=from-dotenv")
    config.save_config({"api_key": "from-config"})
    assert config.get_api_key() == "from-shell-env"


def test_dotenv_beats_config(env_root):
    _write_env(env_root, "ANTHROPIC_API_KEY=from-dotenv")
    config.save_config({"api_key": "from-config"})
    assert config.get_api_key() == "from-dotenv"


def test_config_used_when_no_env_or_dotenv(env_root):
    config.save_config({"api_key": "from-config"})
    assert config.get_api_key() == "from-config"


def test_none_when_nothing_set(env_root):
    assert config.get_api_key() is None
