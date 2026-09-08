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


def save_config(config: dict) -> None:
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_api_key() -> str | None:
    """Resolve the Anthropic API key.

    Order: ANTHROPIC_API_KEY env var first (recommended), then the key the
    user saved through the Settings dialog in data/config.json.
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    return load_config().get("api_key") or None


def get_model() -> str:
    return load_config().get("model") or DEFAULT_MODEL
