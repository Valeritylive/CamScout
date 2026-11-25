#!/usr/bin/env python3
"""Headless command-line interface for CamScout (no GTK required).

Examples:
    camscout discover
    camscout discover --format json --inactive
    camscout export devices.csv
    HIK_PASSWORD='Str0ngPass' camscout activate --all-inactive
    camscout set-ip 44:19:b6:00:00:01 --ip 192.168.1.64 --mask 255.255.255.0 \\
        --gateway 192.168.1.1 --password-env HIK_PASSWORD --yes
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
from dataclasses import asdict
from typing import Callable, Sequence

from sadp_discovery import SADP_SOCKET_LOCK, Device, discover, local_interfaces, write_devices_csv

COMMANDS = ("discover", "export", "activate", "set-ip", "interfaces")
METHODS = ("auto", "sadp", "isapi")
DEFAULT_PASSWORD_ENV = "HIK_PASSWORD"

EXIT_OK = 0
EXIT_FAILED = 1   # at least one device operation failed
EXIT_USAGE = 2    # bad arguments, nothing found, nothing to do


class CliError(Exception):
    """User-facing error that aborts the command with EXIT_USAGE."""


def is_cli_invocation(argv: Sequence[str]) -> bool:
    return bool(argv) and (argv[0] in COMMANDS or argv[0] == "--cli")


def _err(message: str) -> None:
    print(message, file=sys.stderr)


# --- discovery --------------------------------------------------------------------------

def _select_interfaces(names: list[str] | None) -> list[tuple[str, str, str]]:
    available = local_interfaces()
    if not names:
        if not available:
            raise CliError("Не найдено активных сетевых интерфейсов с IPv4.")
        return available
    by_name = {item[0]: item for item in available}
    missing = [name for name in names if name not in by_name]
    if missing:
        known = ", ".join(by_name) or "нет"
        raise CliError(f"Интерфейс не найден или без IPv4: {', '.join(missing)} (доступны: {known}).")
    return [by_name[name] for name in names]


def scan(args: argparse.Namespace,
         discover_fn: Callable[..., list[Device]] | None = None) -> list[Device]:
    discover_fn = discover_fn or discover
    devices: dict[str, Device] = {}
    for interface in _select_interfaces(args.interface):
        try:
            found = discover_fn(interface, timeout=args.timeout)
        except OSError as exc:
            _err(f"{interface[0]}: ошибка поиска: {exc}")
            continue
        for device in found:
            devices.setdefault(device.mac or device.serial or device.ip, device)
    return sorted(devices.values(), key=lambda d: (ipaddress.IPv4Address(d.ip) if _is_ipv4(d.ip)
                                                   else ipaddress.IPv4Address(0), d.mac))


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def _normalize_mac(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", value.lower())


def _filter_activation(devices: list[Device], args: argparse.Namespace) -> list[Device]:
    if getattr(args, "inactive", False):
        return [d for d in devices if d.activated is False]
    if getattr(args, "active", False):
        return [d for d in devices if d.activated is True]
    return devices


def resolve_targets(targets: list[str], args: argparse.Namespace,
                    discover_fn: Callable[..., list[Device]] | None = None) -> list[Device]:
    """Match each target (IPv4, MAC or serial) to a discovered device.

    With --no-discover, targets must be IPv4 addresses and the default ports are assumed.
    An IPv4 target that SADP did not see is still used (with a warning), so devices that
    answer ISAPI but not multicast can be managed.
    """
    if args.no_discover:
        bad = [t for t in targets if not _is_ipv4(t)]
        if bad:
            raise CliError(f"С --no-discover цели должны быть IPv4-адресами: {', '.join(bad)}")
        return [Device(ip=t, http_port=args.http_port, device_port=args.device_port) for t in targets]
    found = scan(args, discover_fn)
    result: list[Device] = []
    for target in targets:
        mac = _normalize_mac(target)
        match = next((d for d in found if d.ip == target), None)
        if match is None and len(mac) == 12:
            match = next((d for d in found if _normalize_mac(d.mac) == mac), None)
        if match is None:
            match = next((d for d in found if d.serial and d.serial.lower() == target.lower()), None)
        if match is None:
            if not _is_ipv4(target):
                raise CliError(f"Устройство «{target}» не найдено поиском SADP.")
            _err(f"Предупреждение: {target} не ответил на SADP, используем HTTP-порт {args.http_port}.")
            match = Device(ip=target, http_port=args.http_port, device_port=args.device_port)
        if match not in result:
            result.append(match)
    return result


# --- passwords --------------------------------------------------------------------------

def read_password(args: argparse.Namespace, *, confirm: bool = False,
                  prompt: str = "Пароль администратора: ") -> str:
    if args.password_file:
        try:
            with open(args.password_file, encoding="utf-8") as handle:
                password = handle.readline().rstrip("\r\n")
        except OSError as exc:
            raise CliError(f"Не удалось прочитать файл пароля: {exc}") from exc
    elif args.password_env:
        password = os.environ.get(args.password_env, "")
        if not password:
            raise CliError(f"Переменная окружения {args.password_env} пуста или не задана.")
    elif args.password is not None:
        password = args.password
    elif os.environ.get(DEFAULT_PASSWORD_ENV):
        password = os.environ[DEFAULT_PASSWORD_ENV]
    elif sys.stdin.isatty():
        password = getpass.getpass(prompt)
        if confirm and getpass.getpass("Повторите пароль: ") != password:
            raise CliError("Пароли не совпадают.")
    else:
        raise CliError("Пароль не задан: используйте --password-env, --password-file "
                       f"или переменную {DEFAULT_PASSWORD_ENV}.")
    if not password:
        raise CliError("Пароль пуст.")
    return password


# --- output helpers ---------------------------------------------------------------------

def _activation_text(device: Device) -> str:
    return {True: "да", False: "нет"}.get(device.activated, "?")


def print_table(devices: list[Device], out=sys.stdout) -> None:
    headers = ["IPv4", "MAC", "Модель", "Серийный номер", "Прошивка", "Активно", "HTTP", "SDK"]
    rows = [[d.ip, d.mac, d.model, d.serial, d.firmware, _activation_text(d),
             str(d.http_port), str(d.device_port)] for d in devices]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    for row in (headers, *rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip(), file=out)


def _device_json(device: Device) -> dict:
    data = asdict(device)
    data.pop("raw", None)
    data.pop("interface", None)
    return data


# --- native SADP (UDP 37020), like the Windows tool -------------------------------------

def native_activate(device: Device, password: str) -> None:
    import sadp_protocol
    with SADP_SOCKET_LOCK, sadp_protocol.SadpTransport(device.interface[1], device.interface[2]) as transport:
        sadp_protocol.activate(transport, device.mac, password)


def native_set_network(device: Device, password: str, params) -> None:
    import sadp_protocol
    with SADP_SOCKET_LOCK, sadp_protocol.SadpTransport(device.interface[1], device.interface[2]) as transport:
        sadp_protocol.modify_network(transport, device.mac, password, params, probe_fields=device.raw)


def _can_use_sadp(device: Device, method: str) -> bool:
    if method == "isapi":
        return False
    if device.mac and device.interface:
        return True
    if method == "sadp":
        raise CliError(f"{device.ip or device.serial}: для SADP нужен MAC из поиска (без --no-discover).")
    return False


def _oneline(exc: BaseException) -> str:
    return str(exc).replace("\n", "; ")


# --- commands ---------------------------------------------------------------------------

def cmd_interfaces(args: argparse.Namespace) -> int:
    interfaces = local_interfaces()
    if args.format == "json":
        json.dump([{"name": n, "address": a, "netmask": m} for n, a, m in interfaces],
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        for name, address, netmask in interfaces:
            print(f"{name}\t{address}\t{netmask}")
    return EXIT_OK if interfaces else EXIT_USAGE


def cmd_discover(args: argparse.Namespace) -> int:
    devices = _filter_activation(scan(args), args)
    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        if args.format == "json":
            json.dump([_device_json(d) for d in devices], out, ensure_ascii=False, indent=2)
            print(file=out)
        elif args.format == "csv":
            write_devices_csv(devices, out)
        else:
            print_table(devices, out)
    finally:
        if out is not sys.stdout:
            out.close()
    if args.format == "table" and not args.output:
        _err(f"Найдено устройств: {len(devices)}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    devices = _filter_activation(scan(args), args)
    if args.file == "-":
        write_devices_csv(devices, sys.stdout)
    else:
        # utf-8-sig like the GUI export, so Excel/LibreOffice detect Cyrillic headers.
        with open(args.file, "w", newline="", encoding="utf-8-sig") as output:
            write_devices_csv(devices, output)
        _err(f"Экспортировано устройств: {len(devices)} → {args.file}")
    return EXIT_OK


def _report(results: list[tuple[Device, str | None]], as_json: bool, ok_text: str) -> int:
    failed = [r for r in results if r[1]]
    if as_json:
        json.dump([{"ip": d.ip, "mac": d.mac, "serial": d.serial, "ok": error is None,
                    **({"error": error} if error else {})} for d, error in results],
                  sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        for device, error in results:
            label = device.ip + (f" ({device.mac})" if device.mac else "")
            if error:
                print(f"ОШИБКА  {label}: {error}")
            else:
                print(f"OK      {label}: {ok_text}")
        _err(f"Успешно: {len(results) - len(failed)}. Ошибок: {len(failed)}.")
    return EXIT_FAILED if failed else EXIT_OK


def cmd_activate(args: argparse.Namespace, api=None) -> int:
    if api is None:
        import hikvision_api as api
    if args.all_inactive:
        if args.targets:
            raise CliError("Укажите либо цели, либо --all-inactive.")
        devices = [d for d in scan(args) if d.activated is False]
        if not devices:
            _err("Неактивированных устройств не найдено.")
            return EXIT_OK
    elif args.targets:
        devices = resolve_targets(args.targets, args)
    else:
        raise CliError("Укажите устройства (IPv4, MAC или серийный номер) или --all-inactive.")
    for device in devices:
        _can_use_sadp(device, args.method)  # fail early on --method sadp without a MAC
    password = read_password(args, confirm=True, prompt="Новый пароль администратора: ")
    from sadp_protocol import SadpError, check_new_password
    try:
        check_new_password(password)  # the same 8-16 chars / two classes rule as Windows SADP
    except SadpError as exc:
        raise CliError(str(exc)) from exc
    results: list[tuple[Device, str | None]] = []
    for device in devices:
        if device.activated is True and not args.force:
            results.append((device, "уже активировано (используйте --force, чтобы попробовать всё равно)"))
            continue
        sadp_error = None
        if _can_use_sadp(device, args.method):
            # Like Windows SADP: activate over SADP itself, so a device in a foreign subnet works.
            try:
                native_activate(device, password)
                results.append((device, None))
                continue
            except (SadpError, OSError) as exc:
                sadp_error = exc
                if args.method == "sadp":
                    results.append((device, f"SADP: {_oneline(exc)}"))
                    continue
        try:
            api.activate_device(device.ip, device.http_port, password, device.device_port)
            results.append((device, None))
        except api.HikvisionError as exc:
            prefix = f"SADP: {_oneline(sadp_error)}; ISAPI: " if sadp_error else ""
            results.append((device, prefix + _oneline(exc)))
    return _report(results, args.json, "активировано")


def plan_addresses(count: int, start: str, mask: str, gateway: str) -> list[str]:
    """Consecutive IPv4 addresses from `start`, validated against the subnet (same rules as the GUI)."""
    try:
        first = ipaddress.IPv4Address(start)
        network = ipaddress.IPv4Network(f"{first}/{mask}", strict=False)
    except ValueError as exc:
        raise CliError(f"Проверьте --ip и --mask: {exc}") from exc
    if gateway:
        try:
            if ipaddress.IPv4Address(gateway) not in network:
                raise CliError("Шлюз должен быть в той же подсети, что и IP.")
        except ValueError as exc:
            raise CliError(f"Проверьте --gateway: {exc}") from exc
    if int(first) + count - 1 > 0xFFFFFFFF:
        raise CliError("Диапазон адресов выходит за пределы IPv4.")
    proposed = [ipaddress.IPv4Address(int(first) + offset) for offset in range(count)]
    if any(ip not in network for ip in proposed):
        raise CliError("Диапазон IP выходит за указанную подсеть.")
    if network.prefixlen < 31 and any(ip in (network.network_address, network.broadcast_address)
                                     for ip in proposed):
        raise CliError("Нельзя назначить адрес сети или broadcast-адрес.")
    return [str(ip) for ip in proposed]


def cmd_set_ip(args: argparse.Namespace, api=None) -> int:
    if api is None:
        import hikvision_api as api
    if not args.dhcp and not (args.ip and args.mask):
        raise CliError("Для статического адреса укажите --ip и --mask (или --dhcp).")
    devices = resolve_targets(args.targets, args)
    plan = ["DHCP"] * len(devices) if args.dhcp else plan_addresses(len(devices), args.ip,
                                                                     args.mask, args.gateway)
    for device, new_ip in zip(devices, plan):
        _err(f"{device.ip} ({device.mac or 'MAC ?'}) → {new_ip}")
    if args.dry_run:
        return EXIT_OK
    if not args.yes:
        if not sys.stdin.isatty():
            raise CliError("Изменение IP требует подтверждения: добавьте --yes.")
        if input(f"Изменить сеть на {len(devices)} устройствах? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            _err("Отменено.")
            return EXIT_USAGE
    use_isapi_only = args.method == "isapi" or bool(args.dns1 or args.dns2)
    if args.method == "sadp" and (args.dns1 or args.dns2):
        raise CliError("SADP не передаёт DNS: уберите --dns1/--dns2 или используйте --method isapi.")
    for device in devices:
        _can_use_sadp(device, args.method)
    password = read_password(args)
    from sadp_protocol import NetworkParams, SadpError, SadpTimeout
    results: list[tuple[Device, str | None]] = []
    for device, new_ip in zip(devices, plan):
        if not use_isapi_only and _can_use_sadp(device, args.method):
            ipv6 = device.ipv6 if device.ipv6 and device.ipv6 != "::" else "::"
            try:
                prefix = int((device.raw or {}).get("ipv6masklen") or 64)
            except ValueError:
                prefix = 64
            params = NetworkParams(ipv4=device.ip if args.dhcp else new_ip,
                                   mask=(args.mask or device.subnet_mask) if not args.dhcp else device.subnet_mask,
                                   gateway=args.gateway if not args.dhcp else device.gateway,
                                   dhcp=args.dhcp, command_port=device.device_port, http_port=device.http_port,
                                   ipv6=ipv6, ipv6_gateway=device.ipv6_gateway or "::", ipv6_prefix=prefix)
            try:
                native_set_network(device, password, params)
                results.append((device, None))
                continue
            except SadpTimeout as exc:
                # No reply means nothing was tried with the password, so ISAPI is safe to try.
                if args.method == "sadp":
                    results.append((device, f"SADP: {_oneline(exc)}"))
                    continue
            except (SadpError, OSError) as exc:
                # A rejected password is not retried over ISAPI: repeated failures lock the account.
                results.append((device, f"SADP: {_oneline(exc)}"))
                continue
        try:
            interface_id = args.interface_id
            if not interface_id:
                interfaces = api.get_network_interfaces(device.ip, device.http_port, args.username, password)
                interface_id = next((i for i in interfaces if i["ip"] == device.ip), interfaces[0])["id"]
            api.set_ipv4(device.ip, device.http_port, args.username, password, interface_id,
                         "" if args.dhcp else new_ip, args.mask or "", args.gateway, args.dns1,
                         args.dns2, args.dhcp)
            results.append((device, None))
        except (api.HikvisionError, ValueError) as exc:
            results.append((device, str(exc)))
    return _report(results, args.json, "сеть настроена" + ("" if args.dhcp else ", новый адрес применён"))


# --- argument parsing -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camscout hikvision",
        description="Поиск и настройка устройств Hikvision без графического интерфейса.",
        epilog=f"Без аргументов запускается графический интерфейс. Пароль можно передать через "
               f"переменную {DEFAULT_PASSWORD_ENV}. Коды выхода: 0 — успех, 1 — ошибка на части "
               f"устройств, 2 — ошибка аргументов или устройство не найдено.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА")

    scan_opts = argparse.ArgumentParser(add_help=False)
    scan_opts.add_argument("-i", "--interface", action="append", metavar="ИМЯ",
                           help="сетевой интерфейс для поиска (можно несколько раз; по умолчанию все)")
    scan_opts.add_argument("-t", "--timeout", type=float, default=3.0, metavar="СЕК",
                           help="время поиска на интерфейс, по умолчанию 3")

    state_opts = argparse.ArgumentParser(add_help=False)
    state = state_opts.add_mutually_exclusive_group()
    state.add_argument("--inactive", action="store_true", help="только неактивированные")
    state.add_argument("--active", action="store_true", help="только активированные")

    target_opts = argparse.ArgumentParser(add_help=False)
    target_opts.add_argument("--no-discover", action="store_true",
                             help="не искать по SADP, цели — IPv4-адреса")
    target_opts.add_argument("--http-port", type=int, default=80, help="HTTP-порт для целей вне SADP")
    target_opts.add_argument("--device-port", type=int, default=8000, help="SDK-порт для целей вне SADP")
    target_opts.add_argument("--json", action="store_true", help="результат в JSON")

    pw = argparse.ArgumentParser(add_help=False)
    pw_group = pw.add_mutually_exclusive_group()
    pw_group.add_argument("--password-env", metavar="ПЕРЕМЕННАЯ",
                          help=f"взять пароль из переменной окружения (по умолчанию {DEFAULT_PASSWORD_ENV}, если задана)")
    pw_group.add_argument("--password-file", metavar="ФАЙЛ", help="взять пароль из первой строки файла")
    pw_group.add_argument("--password", metavar="ПАРОЛЬ",
                          help="пароль в командной строке (небезопасно: виден в списке процессов)")

    p = sub.add_parser("interfaces", help="показать сетевые интерфейсы для поиска")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(func=cmd_interfaces)

    p = sub.add_parser("discover", parents=[scan_opts, state_opts], help="найти устройства по SADP")
    p.add_argument("-f", "--format", choices=("table", "json", "csv"), default="table")
    p.add_argument("-o", "--output", metavar="ФАЙЛ", help="записать результат в файл")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("export", parents=[scan_opts, state_opts],
                       help="найти устройства и сохранить CSV (как экспорт в GUI)")
    p.add_argument("file", metavar="ФАЙЛ", help="путь к CSV или «-» для stdout")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("activate", parents=[scan_opts, target_opts, pw], help="активировать устройства")
    p.add_argument("targets", nargs="*", metavar="ЦЕЛЬ", help="IPv4, MAC или серийный номер")
    p.add_argument("--all-inactive", action="store_true", help="активировать все найденные неактивные")
    p.add_argument("--force", action="store_true", help="пробовать даже если SADP сообщает, что уже активно")
    p.add_argument("--method", choices=METHODS, default="auto",
                   help="auto — сначала SADP по UDP 37020 (как Windows SADP), затем ISAPI; "
                        "sadp — только SADP; isapi — только HTTP")
    p.set_defaults(func=cmd_activate)

    p = sub.add_parser("set-ip", parents=[scan_opts, target_opts, pw],
                       help="изменить сетевые настройки IPv4 (несколько целей — IP подряд от --ip)")
    p.add_argument("targets", nargs="+", metavar="ЦЕЛЬ", help="IPv4, MAC или серийный номер")
    p.add_argument("--dhcp", action="store_true", help="включить DHCP")
    p.add_argument("--ip", help="новый (начальный) IPv4-адрес")
    p.add_argument("--mask", help="маска подсети, например 255.255.255.0")
    p.add_argument("--gateway", default="", help="шлюз IPv4")
    p.add_argument("--dns1", default="", help="основной DNS")
    p.add_argument("--dns2", default="", help="дополнительный DNS")
    p.add_argument("-u", "--username", default="admin", help="пользователь, по умолчанию admin")
    p.add_argument("--interface-id", help="ID сетевого интерфейса устройства (по умолчанию определяется)")
    p.add_argument("--method", choices=METHODS, default="auto",
                   help="auto — SADP по UDP 37020, если устройство найдено поиском и DNS не задан, "
                        "иначе ISAPI; при отказе SADP по паролю ISAPI не пробуется")
    p.add_argument("--dry-run", action="store_true", help="только показать план изменений")
    p.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    p.set_defaults(func=cmd_set_ip)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--cli"]:
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        _err(f"Ошибка: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        _err("Прервано.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
