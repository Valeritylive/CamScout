#!/usr/bin/env python3
"""Headless command-line interface for Dahua Device Manager (no GTK required).

Examples:
    dahua-device-manager discover
    dahua-device-manager discover --format json --uninitialized
    dahua-device-manager discover --range 10.0.0.1-10.0.0.254
    dahua-device-manager export devices.csv
    DAHUA_PASSWORD='Str0ngPass1' dahua-device-manager init --all-uninitialized
    dahua-device-manager set-ip 3c:ef:8c:00:00:01 --ip 192.168.1.108 --mask 255.255.255.0 \\
        --gateway 192.168.1.1 --password-env DAHUA_PASSWORD --yes
    dahua-device-manager reboot 192.168.1.108 --password-env DAHUA_PASSWORD --yes
    dahua-device-manager sync-time --all --password-env DAHUA_PASSWORD
    dahua-device-manager upgrade 192.168.1.108 --firmware ./fw.bin --password-env DAHUA_PASSWORD --yes
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import sys
from dataclasses import asdict
from typing import Callable, Sequence

import dahua_discovery
import dahua_ops
from dahua_api import DahuaHttpError
from dahua_discovery import (
    DISCOVERY_LOCK,
    Device,
    discover,
    ip_range,
    ip_sort_key,
    local_interfaces,
    normalize_mac,
    probe_ip,
    status_text,
    wait_for_device,
    write_devices_csv,
)
from dahua_protocol import password_problems

COMMANDS = ("discover", "export", "init", "set-ip", "reboot", "sync-time", "info", "upgrade", "interfaces", "p2p")
DEFAULT_PASSWORD_ENV = "DAHUA_PASSWORD"

EXIT_OK = 0
EXIT_FAILED = 1   # at least one device operation failed
EXIT_USAGE = 2    # bad arguments, nothing found, nothing to do


class CliError(Exception):
    """User-facing error that aborts the command with EXIT_USAGE."""


def is_cli_invocation(argv: Sequence[str]) -> bool:
    return bool(argv) and (argv[0] in COMMANDS or argv[0] in ("--cli", "-h", "--help"))


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def parse_range(text: str) -> list[str]:
    """``10.0.0.1-10.0.0.50``, ``10.0.0.1-50`` or ``10.0.0.0/24``."""
    text = text.strip()
    try:
        if "/" in text:
            return [str(ip) for ip in ipaddress.IPv4Network(text, strict=False).hosts()]
        if "-" in text:
            start, end = (part.strip() for part in text.split("-", 1))
            if end.isdigit():
                end = start.rsplit(".", 1)[0] + "." + end
            return ip_range(start, end)
        ipaddress.IPv4Address(text)
        return [text]
    except ValueError as exc:
        raise CliError(f"Неверный диапазон «{text}»: {exc}") from exc


def _select_interfaces(names: list[str] | None) -> list[tuple[str, str, str]]:
    available = local_interfaces()
    if not names:
        if not available:
            raise CliError("Не найдено активных сетевых интерфейсов с IPv4.")
        return available
    by_name = {item[0]: item for item in available}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise CliError(f"Интерфейс не найден или без IPv4: {', '.join(missing)} "
                       f"(доступны: {', '.join(by_name) or 'нет'}).")
    return [by_name[name] for name in names]


def scan(args: argparse.Namespace, discover_fn: Callable[..., list[Device]] | None = None) -> list[Device]:
    discover_fn = discover_fn or discover
    targets: list[str] = []
    for item in getattr(args, "range", None) or []:
        targets.extend(parse_range(item))
    devices: dict[str, Device] = {}
    for interface in _select_interfaces(args.interface):
        try:
            with DISCOVERY_LOCK:
                found = discover_fn(interface, timeout=args.timeout, unicast_targets=targets)
        except OSError as exc:
            _err(f"{interface[0]}: ошибка поиска: {exc}")
            continue
        for device in found:
            devices.setdefault(device.key, device)
    return sorted(devices.values(), key=lambda d: (ip_sort_key(d.ip), d.mac))


def resolve_targets(targets: list[str], args: argparse.Namespace,
                    discover_fn: Callable[..., list[Device]] | None = None) -> list[Device]:
    """Match each target (IPv4, MAC or serial) to a discovered device.

    An IPv4 target that did not answer the multicast search is probed by unicast,
    and if it still is silent it is used with default ports (HTTP only).
    """
    if args.no_discover:
        bad = [t for t in targets if not _is_ipv4(t)]
        if bad:
            raise CliError(f"С --no-discover цели должны быть IPv4-адресами: {', '.join(bad)}")
        return [Device(ip=t, http_port=args.http_port, protocol="HTTP") for t in targets]
    found = scan(args, discover_fn)
    result: list[Device] = []
    for target in targets:
        mac = normalize_mac(target)
        match = next((d for d in found if d.ip == target), None)
        if match is None and len(mac.replace(":", "")) == 12:
            match = next((d for d in found if d.mac == mac), None)
        if match is None:
            match = next((d for d in found if d.serial and d.serial.lower() == target.lower()), None)
        if match is None:
            if not _is_ipv4(target):
                raise CliError(f"Устройство «{target}» не найдено поиском.")
            match = probe_ip(target)
            if match is None:
                _err(f"Предупреждение: {target} не ответил на поиск, используем HTTP-порт {args.http_port}.")
                match = Device(ip=target, http_port=args.http_port, protocol="HTTP")
        if match not in result:
            result.append(match)
    return result


def read_password(args: argparse.Namespace, *, confirm: bool = False,
                  prompt: str = "Пароль администратора: ") -> str:
    if getattr(args, "password_file", None):
        try:
            with open(args.password_file, encoding="utf-8") as handle:
                password = handle.readline().rstrip("\r\n")
        except OSError as exc:
            raise CliError(f"Не удалось прочитать файл пароля: {exc}") from exc
    elif os.environ.get(getattr(args, "password_env", None) or DEFAULT_PASSWORD_ENV):
        password = os.environ[args.password_env or DEFAULT_PASSWORD_ENV]
    elif sys.stdin.isatty():
        password = getpass.getpass(prompt)
        if confirm and getpass.getpass("Повторите пароль: ") != password:
            raise CliError("Пароли не совпадают.")
    else:
        raise CliError(f"Пароль не задан: используйте {DEFAULT_PASSWORD_ENV}, --password-env или --password-file.")
    if not password:
        raise CliError("Пустой пароль.")
    return password


def _confirm(args: argparse.Namespace, question: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise CliError("Без терминала нужна опция --yes.")
    answer = input(f"{question} [y/N] ").strip().lower()
    if answer not in ("y", "yes", "д", "да"):
        raise CliError("Отменено.")


def _device_dict(device: Device) -> dict:
    data = asdict(device)
    data.pop("interface", None)
    data["status"] = status_text(device)
    return data


def _print_table(devices: list[Device]) -> None:
    headers = ["№", "Статус", "Тип", "Модель", "IP", "MAC", "Версия"]
    rows = [[str(i), status_text(d), d.device_class, d.model, d.ip, d.mac, d.firmware]
            for i, d in enumerate(devices, 1)]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def _filter_init(devices: list[Device], args: argparse.Namespace) -> list[Device]:
    if getattr(args, "uninitialized", False):
        return [d for d in devices if d.initialized is False]
    if getattr(args, "initialized", False):
        return [d for d in devices if d.initialized is not False]
    return devices


# --- commands ------------------------------------------------------------------------

def cmd_interfaces(args: argparse.Namespace) -> int:
    for name, address, mask in local_interfaces():
        print(f"{name}\t{address}\t{mask}")
    return EXIT_OK


def cmd_discover(args: argparse.Namespace) -> int:
    devices = _filter_init(scan(args), args)
    if args.format == "json":
        print(json.dumps([_device_dict(d) for d in devices], ensure_ascii=False, indent=2))
    elif args.format == "csv":
        write_devices_csv(devices, sys.stdout)
    else:
        _print_table(devices)
        _err(f"Найдено устройств: {len(devices)}")
    return EXIT_OK if devices else EXIT_USAGE


def cmd_export(args: argparse.Namespace) -> int:
    devices = _filter_init(scan(args), args)
    if args.output == "-":
        write_devices_csv(devices, sys.stdout)
    else:
        with open(args.output, "w", newline="", encoding="utf-8-sig") as handle:
            write_devices_csv(devices, handle)
        _err(f"Сохранено устройств: {len(devices)} → {args.output}")
    return EXIT_OK


def _report(args: argparse.Namespace, results: list[dict]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(results, ensure_ascii=False, indent=2))


def _collector(results: list[dict]):
    def on_result(device: Device, error: str | None, info: str) -> None:
        results.append({"ip": device.ip, "mac": device.mac, "ok": error is None, "info": info, "error": error})
        print(f"{device.ip or device.mac}: {'ОШИБКА ' + error if error else info}",
              file=sys.stderr)
    return on_result


def _targets_or_all(args: argparse.Namespace) -> list[Device]:
    if getattr(args, "all", False):
        devices = scan(args)
    elif getattr(args, "all_uninitialized", False):
        devices = [d for d in scan(args) if d.initialized is False]
    else:
        if not args.targets:
            raise CliError("Укажите устройства (IP, MAC, серийный номер) или --all.")
        devices = resolve_targets(args.targets, args)
    if not devices:
        raise CliError("Нет подходящих устройств.")
    return devices


def cmd_init(args: argparse.Namespace) -> int:
    devices = _targets_or_all(args)
    password = read_password(args, confirm=True, prompt="Новый пароль администратора: ")
    problems = password_problems(password)
    if problems:
        raise CliError("Пароль не подходит: " + "; ".join(problems))
    results: list[dict] = []
    failures = dahua_ops.for_each(
        devices, lambda d: dahua_ops.initialize(d, password, args.email, args.phone, args.user) or "Инициализировано",
        _collector(results))
    _report(args, results)
    return EXIT_FAILED if failures else EXIT_OK


def cmd_set_ip(args: argparse.Namespace) -> int:
    devices = resolve_targets(args.targets, args)
    if not args.dhcp and not args.ip:
        raise CliError("Укажите --ip (начальный адрес) или --dhcp.")
    ips = [""] * len(devices) if args.dhcp else dahua_ops.sequential_ips(args.ip, len(devices))
    for device, ip in zip(devices, ips):
        print(f"{device.ip or device.mac} → {ip or 'DHCP'}", file=sys.stderr)
    if args.dry_run:
        return EXIT_OK
    password = read_password(args)
    _confirm(args, f"Изменить сеть у {len(devices)} устройств?")
    results: list[dict] = []
    collect = _collector(results)
    failures = 0
    for device, ip in zip(devices, ips):
        settings = dahua_ops.NetworkSettings(ip=ip, mask=args.mask, gateway=args.gateway, dhcp=args.dhcp,
                                             dns1=args.dns1 or "", dns2=args.dns2 or "")
        try:
            via = dahua_ops.modify_ip(device, args.user, password, settings, args.method)
            info = f"{ip or 'DHCP'} ({via})"
            if args.wait > 0 and device.mac:
                interfaces = _select_interfaces(args.interface)
                found = wait_for_device(device.mac, interfaces, ip, args.wait)
                info += f"; снова на связи: {found.ip}" if found else f"; не ответило за {args.wait:g} с"
            collect(device, None, info)
        except dahua_ops.OperationError as exc:
            failures += 1
            collect(device, str(exc), ip)
    _report(args, results)
    return EXIT_FAILED if failures else EXIT_OK


def cmd_reboot(args: argparse.Namespace) -> int:
    devices = _targets_or_all(args)
    password = read_password(args)
    _confirm(args, f"Перезагрузить {len(devices)} устройств?")
    results: list[dict] = []
    failures = dahua_ops.for_each(
        devices, lambda d: dahua_ops.http_client(d, args.user, password).reboot() or "Перезагрузка",
        _collector(results))
    _report(args, results)
    return EXIT_FAILED if failures else EXIT_OK


def cmd_sync_time(args: argparse.Namespace) -> int:
    devices = _targets_or_all(args)
    password = read_password(args)
    results: list[dict] = []
    failures = dahua_ops.for_each(
        devices, lambda d: dahua_ops.http_client(d, args.user, password).set_time() or "Время синхронизировано",
        _collector(results))
    _report(args, results)
    return EXIT_FAILED if failures else EXIT_OK


def cmd_info(args: argparse.Namespace) -> int:
    devices = resolve_targets(args.targets, args)
    password = read_password(args)
    output = []
    failures = 0
    for device in devices:
        try:
            client = dahua_ops.http_client(device, args.user, password)
            info = client.system_info()
            info["CurrentTime"] = client.get_time()
            entry: dict = {"ip": device.ip, "info": info}
            for key, read in (("video_inputs", client.video_inputs), ("p2p", client.get_p2p)):
                try:
                    entry[key] = read()
                except DahuaHttpError as exc:
                    if exc.auth_failed:
                        raise
            if args.intercom:
                entry["intercom"] = client.existing_configs(dahua_ops.INTERCOM_SECTIONS)
            output.append(entry)
        except DahuaHttpError as exc:
            failures += 1
            output.append({"ip": device.ip, "error": str(exc)})
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return EXIT_FAILED if failures else EXIT_OK


def cmd_p2p(args: argparse.Namespace) -> int:
    devices = resolve_targets(args.targets, args)
    password = read_password(args)
    results: list[dict] = []

    def action(device: Device) -> str:
        client = dahua_ops.http_client(device, args.user, password)
        if args.enable or args.disable:
            client.set_p2p(bool(args.enable))
        table = client.get_p2p()
        state = table.get("T2UServer.Enable", "?")
        return f"P2P: {'включён' if state.lower() == 'true' else 'выключен' if state.lower() == 'false' else state}" \
               + (f", SN {device.serial}" if device.serial else "")
    failures = dahua_ops.for_each(devices, action, _collector(results))
    _report(args, results)
    return EXIT_FAILED if failures else EXIT_OK


def cmd_upgrade(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.firmware):
        raise CliError(f"Файл прошивки не найден: {args.firmware}")
    devices = resolve_targets(args.targets, args)
    password = read_password(args)
    _confirm(args, f"Обновить прошивку на {len(devices)} устройствах? Не выключайте питание во время обновления.")
    results: list[dict] = []

    def upgrade(device: Device) -> str:
        def progress(state: str, percent: float) -> None:
            print(f"\r{device.ip}: {state} {percent:5.1f}%", end="", file=sys.stderr, flush=True)
        state = dahua_ops.http_client(device, args.user, password).upgrade(args.firmware, progress)
        print(file=sys.stderr)
        return state

    failures = dahua_ops.for_each(devices, upgrade, _collector(results))
    _report(args, results)
    return EXIT_FAILED if failures else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="camscout dahua",
                                     description="Поиск и настройка устройств Dahua (как ConfigTool), без графики.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, credentials: bool = True) -> None:
        p.add_argument("-i", "--interface", action="append", help="сетевой интерфейс (можно несколько)")
        p.add_argument("-t", "--timeout", type=float, default=3.0, help="время поиска, с (3)")
        p.add_argument("--range", action="append",
                       help="искать также в диапазоне IPv4 (10.0.0.1-10.0.0.254, 10.0.0.1-254, 10.0.0.0/24)")
        p.add_argument("--no-discover", action="store_true", help="не искать, цели — IPv4-адреса")
        p.add_argument("--http-port", type=int, default=80)
        p.add_argument("--trace", metavar="ФАЙЛ",
                       help="записать пакеты DHIP/DVRIP в файл (для отладки; пароли заменяются на ***)")
        if credentials:
            p.add_argument("-u", "--user", default="admin")
            p.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV, help="переменная окружения с паролем")
            p.add_argument("--password-file", help="файл, первая строка — пароль")
            p.add_argument("--json", action="store_true", help="результат по каждому устройству в JSON")

    p = sub.add_parser("interfaces", help="сетевые интерфейсы для поиска")
    p.set_defaults(func=cmd_interfaces)

    p = sub.add_parser("discover", help="таблица найденных устройств")
    common(p, credentials=False)
    p.add_argument("--format", choices=("table", "json", "csv"), default="table")
    p.add_argument("--uninitialized", action="store_true", help="только неинициализированные")
    p.add_argument("--initialized", action="store_true", help="только инициализированные")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("export", help="сохранить список в CSV (или «-» для stdout)")
    common(p, credentials=False)
    p.add_argument("output")
    p.add_argument("--uninitialized", action="store_true")
    p.add_argument("--initialized", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("init", help="инициализировать (задать пароль) новые устройства")
    common(p)
    p.add_argument("targets", nargs="*")
    p.add_argument("--all-uninitialized", action="store_true")
    p.add_argument("--email", default="", help="e-mail для сброса пароля")
    p.add_argument("--phone", default="", help="телефон для сброса пароля")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("set-ip", help="изменить IPv4 (несколько целей получают адреса подряд)")
    common(p)
    p.add_argument("targets", nargs="+")
    p.add_argument("--ip", help="IPv4 (начальный для нескольких устройств)")
    p.add_argument("--mask", default="255.255.255.0")
    p.add_argument("--gateway", default="")
    p.add_argument("--dns1")
    p.add_argument("--dns2")
    p.add_argument("--dhcp", action="store_true")
    p.add_argument("--method", choices=dahua_ops.METHODS, default="auto")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--wait", type=float, default=0, metavar="С",
                   help="после смены дождаться устройства на новом адресе (до С секунд)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_set_ip)

    for name, func, text in (("reboot", cmd_reboot, "перезагрузить"),
                             ("sync-time", cmd_sync_time, "синхронизировать время с компьютером")):
        p = sub.add_parser(name, help=text)
        common(p)
        p.add_argument("targets", nargs="*")
        p.add_argument("--all", action="store_true", help="все найденные устройства")
        p.add_argument("--yes", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("info", help="сведения об устройстве через HTTP API")
    common(p)
    p.add_argument("targets", nargs="+")
    p.add_argument("--intercom", action="store_true", help="также разделы настроек домофонии (VTO/VTH)")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("p2p", help="P2P (Easy4ip): показать, включить или выключить")
    common(p)
    p.add_argument("targets", nargs="+")
    state = p.add_mutually_exclusive_group()
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")
    p.set_defaults(func=cmd_p2p)

    p = sub.add_parser("upgrade", help="обновить прошивку")
    common(p)
    p.add_argument("targets", nargs="+")
    p.add_argument("--firmware", required=True, help="файл прошивки .bin")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_upgrade)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--cli":
        argv = argv[1:]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    trace_file = None
    if getattr(args, "trace", None):
        try:
            trace_file = open(args.trace, "a", encoding="utf-8")
        except OSError as exc:
            _err(f"Не удалось открыть {args.trace}: {exc}")
            return EXIT_USAGE
        trace_lock = __import__("threading").Lock()

        def write_trace(direction: str, peer: str, data: bytes) -> None:
            with trace_lock:
                trace_file.write(dahua_discovery.format_packet(direction, peer, data))
                trace_file.flush()
        dahua_discovery.packet_trace = write_trace
    try:
        return args.func(args)
    except CliError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except dahua_ops.OperationError as exc:
        _err(str(exc))
        return EXIT_FAILED
    except KeyboardInterrupt:
        return 130
    finally:
        if trace_file is not None:
            dahua_discovery.packet_trace = None
            trace_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
