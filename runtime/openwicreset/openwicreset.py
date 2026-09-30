#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
openwicreset — reset Epson EcoTank waste-ink counters over USB.

Why this exists
---------------
Epson resets its waste-ink pad counter with an internal service command. The
excellent open-source tools that already do this (reinkpy, epson_print_conf)
work great on Linux, but on **macOS** they trip over the OS holding the USB
printer-class interface (`detach_kernel_driver` -> "Access denied"), and newer
firmware blocks the SNMP path entirely.

openwicreset talks the Epson **D4 control protocol over the vendor
"EPSON Utility" USB interface** — which the OS leaves unclaimed and which recent
firmware does NOT block. Pure Python, so it's cross-platform: tested on macOS,
and runs on Linux/Windows given libusb access (see README for per-OS setup).

It stands entirely on the reverse-engineering + D4/USB implementation of:
  * reinkpy          https://codeberg.org/atufi/reinkpy      (AGPL-3.0)
  * epson_print_conf https://github.com/Ircama/epson_print_conf

so it is AGPL-3.0 too, and it uses reinkpy as its engine.

!!! READ THIS FIRST !!!
-----------------------
The counter guards a real sponge/pad inside the printer that soaks up waste ink.
Resetting the counter does NOT empty the pad. If the pad is saturated and you
keep printing, ink can overflow and leak. REPLACE OR CLEAN THE PAD (or fit an
external waste tank) BEFORE you reset. This tool refuses to pretend otherwise.
"""
from __future__ import annotations
import argparse
import os
import sys

EPSON_VID = 0x04B8

# libusb locations to try before falling back to pyusb's own search (Linux).
LIBUSB_CANDIDATES = (
    "/opt/homebrew/lib/libusb-1.0.dylib",      # macOS (Apple Silicon, Homebrew)
    "/usr/local/lib/libusb-1.0.dylib",         # macOS (Intel, Homebrew)
    "/opt/homebrew/lib/libusb-1.0.0.dylib",
)

# Percentage divider for the main waste counter, per model family (from
# epson_print_conf). Only used to pretty-print a %; raw values are always shown.
MAIN_DIVIDER = {0x364A: 63.46}  # ET-1810/2400/2800/2810/2820/2860/2870/4800, L1xxx/L3xxx/L5xxx


def install_backend():
    """Make every pyusb find() use a libusb we can locate, on any OS."""
    import usb.core
    import usb.backend.libusb1

    path = next((p for p in LIBUSB_CANDIDATES if os.path.exists(p)), None)
    backend = (usb.backend.libusb1.get_backend(find_library=lambda _: path)
               if path else usb.backend.libusb1.get_backend())
    if backend is None:
        # Windows / unusual setups: fall back to the pip-installed libusb binary.
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception:
            backend = None
    if backend is None:
        sys.exit("error: libusb not found.\n"
                 "  macOS:   brew install libusb\n"
                 "  Linux:   sudo apt install libusb-1.0-0\n"
                 "  Windows: pip install libusb-package  (+ WinUSB via Zadig)")
    _orig = usb.core.find

    def _find(*a, **k):
        k.setdefault("backend", backend)
        return _orig(*a, **k)

    usb.core.find = _find
    return backend


def load_reinkpy():
    """Import reinkpy and apply the macOS interface fix."""
    try:
        from reinkpy.usb import UsbIO, get_bulk_io
        from reinkpy.d4 import D4Link
        from reinkpy.epson import EpsonD4
    except ImportError:
        sys.exit("error: reinkpy is not installed. Install the engine:\n"
                 "  pip install pyusb\n"
                 "  pip install 'reinkpy[usb]@git+https://codeberg.org/atufi/reinkpy'")

    # macOS: the vendor 'EPSON Utility' interface needs no kernel-driver detach,
    # and reinkpy's detach call fails on the 2nd channel open. Make enter/exit
    # no-ops; pyusb auto-claims the interface on first transfer and holds it.
    UsbIO.__enter__ = lambda self: self
    UsbIO.__exit__ = lambda self, *exc: None
    return UsbIO, get_bulk_io, D4Link, EpsonD4


def find_printer():
    import usb.core
    devs = list(usb.core.find(find_all=True, idVendor=EPSON_VID))
    if not devs:
        sys.exit("error: no Epson USB device found. Is the printer plugged in and powered on?")
    if len(devs) > 1:
        print(f"note: {len(devs)} Epson devices found; using the first.", file=sys.stderr)
    return devs[0]


def pick_interface(dev, get_bulk_io):
    """Prefer the vendor 'EPSON Utility' interface (unclaimed by the OS);
    fall back to the printer-class interface."""
    import usb.util
    cfg = dev.get_active_configuration()
    utility = printer = None
    for ifc in cfg:
        if ifc.bAlternateSetting != 0:
            continue
        eps = get_bulk_io(ifc)
        if not eps:
            continue
        try:
            label = usb.util.get_string(dev, ifc.iInterface) if ifc.iInterface else ""
        except Exception:
            label = ""
        if ifc.bInterfaceClass == 0xFF and "Utility" in (label or ""):
            utility = (ifc, eps, label or "EPSON Utility")
        elif ifc.bInterfaceClass == 0x07 and printer is None:
            printer = (ifc, eps, label or "Printer")
    choice = utility or printer
    if not choice:
        sys.exit("error: no usable USB interface found on the printer.")
    return cfg, choice


def open_printer(model=None):
    backend = install_backend()
    reink = load_reinkpy()
    UsbIO, get_bulk_io, D4Link, EpsonD4 = reink
    dev = find_printer()
    dev.default_timeout = 4000
    cfg, (ifc, (ep_in, ep_out), label) = pick_interface(dev, get_bulk_io)
    io = UsbIO(ep_in, ep_out, ifc, cfg, dev)
    e = EpsonD4(D4Link(io))
    e.configure(model) if model else e.configure()
    if not getattr(e.spec, "model", None):
        # Last resort: derive model from the USB product string, e.g. "ET-2820 Series"
        prod = (getattr(dev, "product", "") or "").replace(" Series", "").strip()
        if prod:
            e.configure(prod)
    return dev, e, label


def reset_map_from_spec(spec):
    """Build {address: reset_value} from the model's memory spec.
    Addresses with no explicit reset value default to 0."""
    m = {}
    for grp in getattr(spec, "mem", []) or []:
        addrs = grp.get("addr", [])
        vals = grp.get("reset") or grp.get("min") or [0] * len(addrs)
        for a, v in zip(addrs, vals):
            m[a] = v
    return m


def read_map(e, addrs):
    return {a: v for a, v in e.read_eeprom(*addrs)}


def main_percent(e, values):
    """Return (raw, pct_or_None) for the main waste counter (bytes 0x30,0x31)."""
    if 0x30 not in values or 0x31 not in values:
        return None, None
    raw = (values[0x30] or 0) | ((values[0x31] or 0) << 8)
    div = MAIN_DIVIDER.get(getattr(e.spec, "rkey", None))
    return raw, (round(raw / div, 2) if div else None)


def detect_write_key(e, rmap):
    """Find the working write key by writing value+1 to a scratch address and
    restoring it. Tries the model's own keys first. Returns bytes or None."""
    candidates = []
    for k in (getattr(e.spec, "wkey", None), getattr(e.spec, "wkey1", None)):
        if not k:
            continue
        kb = k if isinstance(k, (bytes, bytearray)) else str(k).encode("latin-1")
        if kb not in candidates:
            candidates.append(bytes(kb))
    # scratch = a reset-map address whose current value isn't 0xFF (avoid +1 overflow)
    scratch = next((a for a in rmap if (read_map(e, [a])[a] or 0) != 0xFF), None)
    if scratch is None:
        return None
    cur = read_map(e, [scratch])[scratch] or 0
    for kb in candidates:
        if e.write_eeprom((scratch, (cur + 1) & 0xFF), wkey=kb, check_read=True):
            e.write_eeprom((scratch, cur), wkey=kb)  # restore
            return kb
    return None


def cmd_status(args):
    dev, e, label = open_printer(args.model)
    print(f"Printer : {e.spec.model or '?'}  (USB interface: {label})")
    print(f"Serial  : {(e.info.get('SN') or e.info.get('serial_number') or '?')}")
    rmap = reset_map_from_spec(e.spec)
    if not rmap:
        sys.exit("error: no waste-counter map for this model in reinkpy's database.")
    vals = read_map(e, sorted(rmap))
    raw, pct = main_percent(e, vals)
    if raw is not None:
        pct_s = f"~{pct}%" if pct is not None else "(no % table for this model)"
        print(f"Main waste counter : {raw} raw  {pct_s}")
    print("\nCounter bytes (address = current -> reset target):")
    for a in sorted(rmap):
        print(f"  0x{a:02X} = {vals.get(a)!s:>4}  -> {rmap[a]}")
    print("\nRun `openwicreset.py reset` to zero the counters (replace the pad first!).")


def cmd_reset(args):
    dev, e, label = open_printer(args.model)
    rmap = reset_map_from_spec(e.spec)
    if not rmap:
        sys.exit("error: no waste-counter map for this model in reinkpy's database.")
    before = read_map(e, sorted(rmap))
    raw, pct = main_percent(e, before)
    print(f"Printer : {e.spec.model or '?'}  (USB interface: {label})")
    if raw is not None:
        print(f"Main waste counter now: {raw} raw" + (f"  (~{pct}%)" if pct is not None else ""))

    if not args.yes:
        print("\n*** The waste pad must already be REPLACED or CLEANED. ***")
        if input("Type 'reset' to zero the counters: ").strip().lower() != "reset":
            sys.exit("Aborted. Nothing was written.")

    key = detect_write_key(e, rmap)
    if not key:
        sys.exit("error: could not find a working write key for this model. Nothing written.")
    print(f"Write key confirmed: {key!r}")

    ok = e.write_eeprom(*rmap.items(), wkey=key, check_read=True, atomic=True)
    after = read_map(e, sorted(rmap))
    raw2, pct2 = main_percent(e, after)
    print(f"Write verified: {ok}")
    if raw2 is not None:
        print(f"Main waste counter now: {raw2} raw" + (f"  (~{pct2}%)" if pct2 is not None else ""))
    if ok and (raw2 == 0 or raw2 is None):
        print("\n✔ RESET COMPLETE — now power-cycle the printer (off, wait 10s, on) to clear the error.")
    else:
        sys.exit("\n(!) Reset did not fully take — see values above.")


def build_parser():
    p = argparse.ArgumentParser(
        prog="openwicreset",
        description="Reset Epson EcoTank waste-ink counters over USB (macOS-friendly).",
        epilog="Replace the physical waste pad before resetting. Right to repair, responsibly.",
    )
    p.add_argument("-m", "--model", help="force a model (e.g. ET-2820) if auto-detect fails")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show the printer and current waste-counter levels (read-only)")
    r = sub.add_parser("reset", help="reset (zero) the waste-ink counters")
    r.add_argument("-y", "--yes", action="store_true", help="skip the interactive confirmation")
    return p


def main():
    args = build_parser().parse_args()
    if args.cmd == "reset":
        cmd_reset(args)
    else:
        cmd_status(args)  # default: read-only status


if __name__ == "__main__":
    main()
