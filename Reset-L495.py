#!/usr/bin/env python3
"""Reset terminal dos contadores de almofadas da Epson L495 no Windows."""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
PYTHON = RUNTIME / "python" / "Scripts" / "python.exe"
WDI = RUNTIME / "usb-driver" / "wdi-simple.exe"
DRIVER_DIR = RUNTIME / "usb-driver" / "package"
OPENWIC_DIR = RUNTIME / "openwicreset"
LOG_DIR, BACKUP_DIR = ROOT / "logs", ROOT / "backups"

MODEL, SERIAL = "L495", "583250323031373284"
INSTANCE = r"USB\VID_04B8&PID_1121&MI_01\7&2820e842&0&0001"
PARENT = rf"USB\VID_04B8&PID_1121\{SERIAL}"
TARGET = {0x18: 0, 0x19: 0, 0x1C: 0, 0x1D: 0, 0x1E: 0, 0x2E: 0x5E}
LOG_LINES: list[str] = []


def say(message: str = "") -> None:
    print(message, flush=True)
    LOG_LINES.append(message)


def save_log(command: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{command}.log"
    path.write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")
    return path


def run(args: list[str], check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if proc.stdout:
        LOG_LINES.extend(proc.stdout.rstrip().splitlines())
    if check and proc.returncode:
        raise RuntimeError(f"Falha ({proc.returncode}): {' '.join(args)}\n{proc.stdout}")
    return proc


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def relaunch_as_admin() -> int:
    args = [str(PYTHON), str(Path(__file__).resolve()), "--interno-elevado", *sys.argv[1:]]
    array = "@(" + ",".join(ps_quote(x) for x in args[1:]) + ")"
    command = (f"$p=Start-Process -FilePath {ps_quote(args[0])} -ArgumentList {array} "
               f"-WorkingDirectory {ps_quote(str(ROOT))} -Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    say("Solicitando privilegio de administrador (UAC)...")
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", command]).returncode


def require_runtime() -> None:
    missing = [p for p in (PYTHON, WDI, OPENWIC_DIR / "openwicreset.py") if not p.exists()]
    if missing:
        raise RuntimeError("Runtime incompleto: " + ", ".join(map(str, missing)))


def pnp_details() -> str:
    return run(["pnputil.exe", "/enum-devices", "/instanceid", INSTANCE, "/drivers"], False).stdout


def active_driver() -> str | None:
    found = re.findall(r"\b(?:usbprint|oem\d+)\.inf\b", pnp_details(), flags=re.I)
    return found[0].lower() if found else None


def close_epson_processes() -> None:
    ps = ("Get-Process -ErrorAction SilentlyContinue | "
          "Where-Object { $_.ProcessName -match 'epson|epusb|wicreset' } | "
          "Stop-Process -Force -ErrorAction SilentlyContinue")
    run(["powershell.exe", "-NoProfile", "-Command", ps], False)


def install_winusb() -> str:
    current = active_driver()
    if current and current.startswith("oem"):
        say(f"Driver USB direto ja ativo: {current}")
        return current
    if current != "usbprint.inf":
        raise RuntimeError(f"Driver inicial inesperado na MI_01: {current or 'nao identificado'}")
    DRIVER_DIR.mkdir(parents=True, exist_ok=True)
    say("Aplicando WinUSB temporario somente na interface MI_01...")
    result = run([str(WDI), "-t", "0", "-n", "EPSON L495 Direct Reset", "-m", "EPSON",
                  "-v", "0x04B8", "-p", "0x1121", "-i", "1", "-f", "epson_l495_winusb.inf",
                  "-d", str(DRIVER_DIR), "-o", "120000", "-l", "2"])
    current = active_driver()
    if not current or not current.startswith("oem"):
        raise RuntimeError("WinUSB nao ficou ativo.\n" + result.stdout)
    say(f"WinUSB ativo: {current}")
    return current


def restore_usbprint(oem: str | None) -> None:
    current = active_driver()
    candidate = current if current and current.startswith("oem") else oem
    if candidate and candidate.startswith("oem"):
        say(f"Restaurando driver Microsoft; removendo {candidate}...")
        run(["pnputil.exe", "/delete-driver", candidate, "/uninstall", "/force"], False)
        time.sleep(3)
    current = active_driver()
    if current != "usbprint.inf":
        run(["pnputil.exe", "/scan-devices"], False)
        time.sleep(3)
        current = active_driver()
    if current != "usbprint.inf":
        raise RuntimeError(f"Nao foi possivel restaurar usbprint.inf; ativo={current}")
    say("Driver de impressao restaurado: usbprint.inf")


def load_engine():
    sys.path.insert(0, str(OPENWIC_DIR))
    import openwicreset as engine  # type: ignore
    return engine


def read_counters(engine):
    dev, epson, label = engine.open_printer(MODEL)
    reset_map = engine.reset_map_from_spec(epson.spec)
    if reset_map != TARGET:
        raise RuntimeError(f"Mapa L495 inesperado: {reset_map}")
    values = engine.read_map(epson, sorted(reset_map))
    serial = epson.info.get("SN") or epson.info.get("serial_number") or "?"
    if str(serial) != SERIAL:
        raise RuntimeError(f"Serial inesperado: {serial}")
    return dev, epson, label, values


def print_values(title: str, values: dict[int, int]) -> None:
    say(title)
    for address in sorted(TARGET):
        say(f"  0x{address:02X} = {values[address]:3d} -> alvo {TARGET[address]}")


def write_backup(values: dict[int, int], driver: str) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"L495-{SERIAL}-{datetime.now():%Y%m%d-%H%M%S}-antes.json"
    data = {"timestamp": datetime.now().astimezone().isoformat(), "model": MODEL, "serial": SERIAL,
            "driver_temporario": driver,
            "contadores": {f"0x{k:02X}": v for k, v in sorted(values.items())},
            "alvos": {f"0x{k:02X}": v for k, v in sorted(TARGET.items())}}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def restart_printer_usb() -> None:
    say("Reiniciando logicamente o dispositivo USB para validar persistencia...")
    run(["pnputil.exe", "/restart-device", PARENT])
    time.sleep(4)


def ensure_targets(values: dict[int, int], stage: str) -> None:
    wrong = {a: (values.get(a), target) for a, target in TARGET.items() if values.get(a) != target}
    if wrong:
        raise RuntimeError(f"Verificacao falhou em {stage}: {wrong}")


def command_detect() -> int:
    details = pnp_details()
    if "VID_04B8&PID_1121" not in details.upper():
        say("Epson L495 USB nao encontrada.")
        return 2
    say(f"Epson L495 detectada; serial {SERIAL}; driver {active_driver() or '?'}")
    return 0


def command_status() -> int:
    oem = None
    try:
        close_epson_processes()
        oem = install_winusb()
        engine = load_engine()
        dev, epson, label, values = read_counters(engine)
        say(f"Impressora: {MODEL}; serial: {SERIAL}; interface: {label}")
        print_values("Contadores atuais:", values)
        del dev, epson
        gc.collect()
        return 0
    finally:
        restore_usbprint(oem)


def command_reset(confirm: bool) -> int:
    if not confirm:
        raise RuntimeError("Use --confirmar-manutencao depois de limpar ou trocar as almofadas.")
    oem = None
    try:
        close_epson_processes()
        oem = install_winusb()
        engine = load_engine()
        dev, epson, _, before = read_counters(engine)
        print_values("Leitura anterior ao reset:", before)
        backup = write_backup(before, oem)
        say(f"Backup: {backup}")
        key = engine.detect_write_key(epson, TARGET)
        if not key:
            raise RuntimeError("Nenhuma chave de escrita foi aceita; reset nao executado.")
        say(f"Chave de escrita confirmada: {key.decode('ascii', errors='replace')}")
        ok = epson.write_eeprom(*TARGET.items(), wkey=key, check_read=True, atomic=True)
        after = engine.read_map(epson, sorted(TARGET))
        if not ok:
            raise RuntimeError("A escrita nao retornou confirmacao completa.")
        ensure_targets(after, "releitura imediata")
        print_values("Releitura imediata confirmada:", after)
        del dev, epson
        gc.collect()
        restart_printer_usb()
        dev2, epson2, _, persisted = read_counters(engine)
        ensure_targets(persisted, "releitura depois do reinicio USB")
        print_values("Persistencia depois do reinicio confirmada:", persisted)
        del dev2, epson2
        gc.collect()
        say("RESET L495 CONCLUIDO E VERIFICADO.")
        return 0
    finally:
        restore_usbprint(oem)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reset terminal dos contadores da Epson L495")
    p.add_argument("--interno-elevado", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("detectar", help="detectar a L495 e mostrar o driver")
    sub.add_parser("status", help="ler contadores sem alterar a EEPROM")
    reset = sub.add_parser("resetar", help="resetar, reler, reiniciar USB e reler novamente")
    reset.add_argument("--confirmar-manutencao", action="store_true")
    sub.add_parser("restaurar-driver", help="restaurar usbprint.inf apos uma interrupcao")
    return p


def main() -> int:
    os.environ["PYTHONIOENCODING"] = "utf-8"
    require_runtime()
    args = build_parser().parse_args()
    if args.command != "detectar" and not is_admin():
        return relaunch_as_admin()
    try:
        if args.command == "detectar":
            code = command_detect()
        elif args.command == "status":
            code = command_status()
        elif args.command == "resetar":
            code = command_reset(args.confirmar_manutencao)
        else:
            restore_usbprint(None)
            code = 0
        log = save_log(args.command)
        say(f"Log: {log}")
        return code
    except Exception as exc:
        say(f"ERRO: {exc}")
        log = save_log(args.command)
        say(f"Log: {log}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
