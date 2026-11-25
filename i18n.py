"""Interface languages and saved preferences for CamScout.

Source strings in the code are English. ``tr()`` looks them up in
``locales/<code>.json`` for the chosen language and falls back to English.
The language is a process-wide setting, so backend modules can translate
their error messages too. Kept free of GTK so it works in the CLI and tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# (code, name shown in the language list). English is the default.
LANGUAGES: list[tuple[str, str]] = [
    ("en", "English"),
    ("ru", "Русский"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("hy", "Հայերեն"),
]
DEFAULT_LANGUAGE = "en"
LOCALE_DIR = Path(__file__).resolve().parent / "locales"

_language = DEFAULT_LANGUAGE
_catalog: dict[str, str] = {}


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "camscout" / "settings.json"


def load_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(values: dict) -> None:
    path = settings_path()
    data = load_settings()
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_catalog(code: str) -> dict[str, str]:
    if code == DEFAULT_LANGUAGE:
        return {}
    try:
        with open(LOCALE_DIR / f"{code}.json", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, str) and value}


def set_language(code: str) -> str:
    """Switch the interface language; unknown codes fall back to English."""
    global _language, _catalog
    if code not in {c for c, _name in LANGUAGES}:
        code = DEFAULT_LANGUAGE
    _language = code
    _catalog = load_catalog(code)
    return code


def language() -> str:
    return _language


def tr(text: str) -> str:
    """Translate an English source string into the current language."""
    return _catalog.get(text, text)


def init_from_settings() -> str:
    """Apply the saved language (CAMSCOUT_LANG overrides it, e.g. for screenshots)."""
    return set_language(os.environ.get("CAMSCOUT_LANG") or load_settings().get("language", DEFAULT_LANGUAGE))


_ = tr
