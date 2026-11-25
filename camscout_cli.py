"""Command line entry of CamScout: `camscout dahua …` and `camscout hikvision …`.

The two brand CLIs come from the earlier Dahua Device Manager and the SADP tool;
this module only picks one by the first argument. GTK is never loaded.
"""

from __future__ import annotations

import sys
from typing import Sequence

BRANDS = {"dahua": "dahua", "hikvision": "hikvision", "hik": "hikvision", "sadp": "hikvision"}

USAGE = """usage: camscout [dahua|hikvision] <command> [options]

  camscout                       start the graphical interface
  camscout dahua --help          Dahua commands (DHIP): discover, init, set-ip, reboot, ...
  camscout hikvision --help      Hikvision commands (SADP/ISAPI): discover, activate, set-ip, ...
"""


def is_cli_invocation(argv: Sequence[str]) -> bool:
    return bool(argv) and (argv[0].lower() in BRANDS or argv[0] in ("-h", "--help", "--cli"))


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--cli"]:
        argv = argv[1:]
    if not argv or argv[0] in ("-h", "--help") or argv[0].lower() not in BRANDS:
        print(USAGE, end="")
        return 0 if not argv or argv[0] in ("-h", "--help") else 2
    import i18n
    i18n.init_from_settings()
    brand, rest = BRANDS[argv[0].lower()], argv[1:]
    if brand == "dahua":
        import dahua_cli
        return dahua_cli.main(rest)
    import sadp_cli
    return sadp_cli.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
