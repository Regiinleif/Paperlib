"""Configuration and paths for PaperLib.

Everything the app needs to find on disk lives here so the rest of the code
never hard-codes a path. The layout under the project root is:

    <project-root>/
        papers/        <- drop your downloaded PDFs here (the "drop box")
        data/          <- library.db (metadata) and config.json (settings)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Are we running from a PyInstaller-frozen build (installed .exe) or from
# source?  When frozen, ``__file__`` points into a temporary extraction dir
# that is wiped on exit, so user data must live somewhere writable and
# persistent instead.
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # ROOT is the folder that actually contains the running executable, e.g.
    # ``C:\Program Files\PaperLib``.  We treat this as read-only (it may live
    # under Program Files) and only use it to look for a user-supplied .env.
    ROOT = Path(sys.executable).resolve().parent
    # User data (papers, database, settings) goes in a per-user writable
    # location so an installed, non-admin user can still use the app.
    _DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PaperLib"
else:
    # Running from source: root = the folder that contains the `paperlib`
    # package.  Behaviour is unchanged from the original project layout.
    ROOT = Path(__file__).resolve().parent.parent
    _DATA_ROOT = ROOT

PAPERS_DIR = _DATA_ROOT / "papers"
DATA_DIR = _DATA_ROOT / "data"
DB_PATH = DATA_DIR / "library.db"
CONFIG_PATH = DATA_DIR / "config.json"

# Default Claude model. Change in data/config.json if you like.
DEFAULT_MODEL = "claude-opus-5"


def ensure_dirs() -> None:
    """Create the papers/ and data/ folders if they do not exist yet."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load data/config.json, returning {} if it is missing or corrupt."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _dotenv_search_dirs() -> list[Path]:
    """Directories to look in for a ``.env`` file, most-preferred first.

    - From source: just the project root (unchanged behaviour; the tests
      monkeypatch ``config.ROOT`` and rely on this).
    - Frozen/installed: next to the executable first (so a user can drop a
      ``.env`` beside ``PaperLib.exe``), then the per-user data dir.
    """
    if FROZEN:
        return [ROOT, DATA_DIR]
    return [ROOT]


def _parse_dotenv(env_path: Path) -> dict:
    """Parse a single ``.env`` file into a dict; never raises."""
    values: dict[str, str] = {}
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                # Skip blank lines and comments.
                if not line or line.startswith("#"):
                    continue
                # Tolerate an optional leading "export ".
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                # A valid line must contain "=" to split KEY from VALUE.
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                # Strip a single pair of matching surrounding quotes.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                values[key] = value
    except OSError:
        # Missing or unreadable .env -> behave as if it did not exist.
        return {}
    return values


def load_dotenv_file() -> dict:
    """Read a ``.env`` file into a dict, dependency-free.

    We deliberately avoid python-dotenv to keep the project dependency-light.
    Parses simple ``KEY=VALUE`` lines:

    - blank lines and lines starting with ``#`` are ignored,
    - a leading ``export `` is tolerated (``export KEY=VALUE``),
    - surrounding whitespace is stripped from both key and value,
    - a single pair of matching surrounding quotes is stripped from the value.

    From source this reads ``ROOT / ".env"``.  When frozen it also looks in the
    per-user data dir, with the file next to the executable winning on conflict.

    Never raises: missing or malformed files/lines are simply skipped.
    """
    merged: dict[str, str] = {}
    # Later dirs must not override earlier (more-preferred) ones.
    for base in reversed(_dotenv_search_dirs()):
        merged.update(_parse_dotenv(base / ".env"))
    return merged


def save_config(config: dict) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_api_key() -> str | None:
    """Resolve the Anthropic API key.

    Resolution order (first non-empty wins):
      1. the real ANTHROPIC_API_KEY environment variable (a shell env var
         always wins),
      2. ANTHROPIC_API_KEY from a ``.env`` file in the project root,
      3. Azure Key Vault, if ``AZURE_KEY_VAULT_URL`` is set (this is how the
         deployed Container App gets its key, via its managed identity), and
      4. the ``api_key`` the user saved through the Settings dialog in
         data/config.json.

    Returns ``None`` if none of these provide a key.
    """
    # 1. A real shell environment variable takes precedence.
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    # 2. Fall back to the .env file in the project root.
    dotenv_key = load_dotenv_file().get("ANTHROPIC_API_KEY")
    if dotenv_key:
        return dotenv_key
    # 3. Azure Key Vault (deployed app: managed identity + AZURE_KEY_VAULT_URL).
    vault_key = _key_from_vault(os.environ.get("AZURE_KEY_VAULT_URL", ""))
    if vault_key:
        return vault_key
    # 4. Finally, the key saved via the Settings dialog.
    return load_config().get("api_key") or None


def get_model() -> str:
    """Resolve the Claude model.

    Order (first non-empty wins): the ``PAPERLIB_MODEL`` environment variable
    (so a container can be pinned to a cheaper model without a config file),
    then ``model`` in data/config.json, then :data:`DEFAULT_MODEL`.
    """
    env_model = os.environ.get("PAPERLIB_MODEL")
    if env_model:
        return env_model
    return load_config().get("model") or DEFAULT_MODEL


# The Key Vault secret name the API key is stored under. Kept as a constant so
# the deploy scripts and the code agree on one name.
KEY_VAULT_SECRET_NAME = "anthropic-api-key"

# Cache the key fetched from Key Vault so we don't make a network round-trip on
# every request (get_api_key runs per chat/agent call). Keyed by vault URL.
_vault_key_cache: dict[str, str] = {}


def _key_from_vault(vault_url: str, client=None) -> str | None:
    """Fetch the Anthropic API key from Azure Key Vault, or None on any failure.

    Uses ``DefaultAzureCredential`` so it works with the Container App's managed
    identity in Azure (and with ``az login`` / env creds locally). Robustness
    first: a missing SDK, missing identity, network error or absent secret all
    return None so key resolution can fall through instead of hard-failing. The
    result is cached per vault URL. A ``client`` may be injected for tests.
    """
    if not vault_url:
        return None
    if vault_url in _vault_key_cache:
        return _vault_key_cache[vault_url]
    try:
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            client = SecretClient(
                vault_url=vault_url, credential=DefaultAzureCredential()
            )
        value = client.get_secret(KEY_VAULT_SECRET_NAME).value or None
    except Exception:
        return None
    if value:
        _vault_key_cache[vault_url] = value
    return value
