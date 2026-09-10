"""Tests for config: .env parsing and API-key precedence.

These never touch the user's real .env — ROOT/CONFIG_PATH are pointed at a
temp location and the environment variable is controlled via monkeypatch.
"""

import pytest

from src import config


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
    monkeypatch.delenv("PAPERLIB_MODEL", raising=False)
    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    # The Key Vault key cache is module-level; give each test a fresh one.
    monkeypatch.setattr(config, "_vault_key_cache", {})
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


# ---- get_model ------------------------------------------------------------

def test_model_env_var_overrides_config(env_root, monkeypatch):
    monkeypatch.setenv("PAPERLIB_MODEL", "claude-haiku-4-5")
    config.save_config({"model": "claude-opus-5"})
    assert config.get_model() == "claude-haiku-4-5"


def test_model_config_then_default(env_root):
    config.save_config({"model": "claude-opus-5"})
    assert config.get_model() == "claude-opus-5"
    config.save_config({})
    assert config.get_model() == config.DEFAULT_MODEL


# ---- Key Vault key resolution --------------------------------------------

class _FakeSecret:
    def __init__(self, value):
        self.value = value


class _FakeSecretClient:
    """Mimics azure.keyvault.secrets.SecretClient.get_secret()."""

    def __init__(self, value):
        self._value = value
        self.calls = 0

    def get_secret(self, name):
        self.calls += 1
        return _FakeSecret(self._value)


class _BoomSecretClient:
    def get_secret(self, name):
        raise RuntimeError("no access / network down")


def test_key_from_vault_with_injected_client_and_caches(env_root):
    client = _FakeSecretClient("vault-secret")
    url = "https://v.vault.azure.net/"
    assert config._key_from_vault(url, client=client) == "vault-secret"
    # Second call is served from the cache, not the client.
    assert config._key_from_vault(url, client=client) == "vault-secret"
    assert client.calls == 1


def test_key_from_vault_error_returns_none(env_root):
    assert config._key_from_vault("https://v.vault.azure.net/",
                                  client=_BoomSecretClient()) is None


def test_key_from_vault_empty_url_returns_none(env_root):
    assert config._key_from_vault("") is None


def test_get_api_key_uses_vault_when_set(env_root, monkeypatch):
    # No env, no .env, no config -> Key Vault provides the key.
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://v.vault.azure.net/")
    monkeypatch.setattr(config, "_key_from_vault", lambda url: "from-vault")
    assert config.get_api_key() == "from-vault"


def test_env_var_beats_vault(env_root, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell-env")
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://v.vault.azure.net/")
    # If the vault were consulted it would raise; env must win before that.
    monkeypatch.setattr(config, "_key_from_vault",
                        lambda url: (_ for _ in ()).throw(AssertionError("vault consulted")))
    assert config.get_api_key() == "from-shell-env"
