#!/usr/bin/env python3
"""List the English source strings that need translations (for locales/*.json).

Usage: python3 tools/extract_strings.py            # print missing keys per language
       python3 tools/extract_strings.py --keys     # print all keys as JSON
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def literal_calls() -> set[str]:
    keys: set[str] = set()
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "tr")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
    return keys


def table_strings() -> set[str]:
    """Strings translated at runtime through tables rather than literal tr() calls."""
    import dahua_api
    import dahua_discovery
    import dahua_protocol
    import device_columns
    import sadp_protocol
    keys = {title for _k, title, *_r in device_columns.COLUMNS}
    keys |= set(device_columns.TYPE_FILTERS) | set(device_columns.BRAND_FILTERS)
    keys |= set(dahua_api.UPGRADE_STATES.values()) | {dahua_api.LOCK_HINT}
    keys |= set(dahua_protocol.DAHUA_ERRORS.values()) | set(sadp_protocol.PASSWORD_LEVELS)
    keys |= {name for _bit, name in dahua_discovery.PWD_RESET_WAYS} | set(sadp_protocol._FAILURE_HINTS.values())
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PAGES" for t in node.targets):
            for item in node.value.elts:
                keys.add(item.elts[1].value)
    return keys


def all_keys() -> list[str]:
    keys = literal_calls() | table_strings()
    keys -= {"IP", "MAC", "IPv6", "DHCP", "No.", "IPC", "NVR", "DVR", "XVR", "HCVR", "SD", "VTO", "VTH",
             "Dahua", "Hikvision", "e-mail"}
    return sorted(k for k in keys if any(ch.isalpha() for ch in k))


def main() -> int:
    keys = all_keys()
    if "--keys" in sys.argv:
        print(json.dumps(keys, ensure_ascii=False, indent=1))
        return 0
    status = 0
    for path in sorted((ROOT / "locales").glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        missing = [k for k in keys if k not in catalog]
        unused = [k for k in catalog if k not in keys]
        print(f"{path.name}: {len(catalog)} entries, missing {len(missing)}, unused {len(unused)}")
        for key in missing:
            print("  -", key)
        status |= bool(missing)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
