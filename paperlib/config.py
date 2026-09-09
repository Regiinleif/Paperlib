"""Configuration and paths for PaperLib.

Everything the app needs to find on disk lives here so the rest of the code
never hard-codes a path. The layout under the project root is:

    D:\\paperlib\\
        papers\\        <- drop your downloaded PDFs here (the "drop box")
        data\\          <- library.db (metadata) and config.json (settings)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Project root = the folder that contains the `paperlib` package.
ROOT = Path(__file__).resolve().parent.parent

PAPERS_DIR = ROOT / "papers"
DATA_DIR = ROOT / "data"
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


def load_dotenv_file() -> dict:
    """Read the project's ``.env`` file into a dict, dependency-free.

    We deliberately avoid python-dotenv to keep the project dependency-light.
    Parses simple ``KEY=VALUE`` lines from ``ROOT / ".env"``:

    - blank lines and lines starting with ``#`` are ignored,
    - a leading ``export `` is tolerated (``export KEY=VALUE``),
    - surrounding whitespace is stripped from both key and value,
    - a single pair of matching surrounding quotes is stripped from the value.

    Never raises: if the file is missing or a line is malformed it is simply
    skipped, and ``{}`` is returned for a missing/unreadable file.
    """
    env_path = ROOT / ".env"
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


def save_config(config: dict) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_api_key() -> str | None:
    """Resolve the Anthropic API key.

    Resolution order (first non-empty wins):
      1. the real ANTHROPIC_API_KEY environment variable (a shell env var
         always wins),
      2. ANTHROPIC_API_KEY from a ``.env`` file in the project root, and
      3. the ``api_key`` the user saved through the Settings dialog in
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
    # 3. Finally, the key saved via the Settings dialog.
    return load_config().get("api_key") or None


def get_model() -> str:
    return load_config().get("model") or DEFAULT_MODEL
