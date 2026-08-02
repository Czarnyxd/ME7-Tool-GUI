#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rolling_chain.py

Rolling Anti-Lag patcher for Bosch ME7.5 1MB, rewritten from rollingv3 PHP logic.

Modes:
  SOLO  - original BIN, normal hook -> Rolling code cave -> return
  CHAIN - BIN already patched by launch.exe / ALS/LC/NLS:
          existing DA hook -> Rolling code cave -> old launch.exe code cave -> return

Usage:
  python3 rolling_chain.py ecu.bin dump.ecu [0x_Rolling_Code|auto] [0x_Rolling_Vars|auto] [trigger]

Examples:
  python3 rolling_chain.py ecu.bin dump.ecu
  python3 rolling_chain.py ecu.bin dump.ecu 0xA3000 0x17A00 cruise_set
  python3 rolling_chain.py ecu_mod.bin dump.ecu auto auto cruise_set
  python3 rolling_chain.py ecu_mod.bin dump.ecu auto auto brake

Triggers:
  brake       -> b_br
  clutch      -> b_kuppl
  cruise_set  -> tries b_fgrsec, s_fgrsv, b_fgrtdc
  cruise_res  -> tries b_fgrwac, b_fgrtuc, s_fgrwb
  cruise_main -> tries b_fgrhsc, s_fgrhs

WARNING:
  - Correct checksums after patching before flashing.
  - Test on bench/logs first. This only edits bytes; it cannot prove ECU logic is safe for your exact software.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ECUMap = Dict[str, List[str]]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def info(msg: str) -> None:
    print(msg)


def remove0x(value: object) -> str:
    s = str(value).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.lstrip("0")
    return s if s else "0"


def parse_hex(value: object) -> int:
    return int(remove0x(value), 16)


def byte_hex(data: bytearray | bytes, idx: int) -> str:
    return f"{data[idx]:02X}"


def gen_output_name(target_name: str) -> str:
    p = Path(target_name)
    if p.suffix:
        return str(p.with_name(f"{p.stem}_rolling{p.suffix}"))
    return f"{target_name}_rolling.bin"


def bitmask2int(bitmask: object) -> int:
    v = parse_hex(bitmask)
    i = 0
    while v > 1:
        v //= 2
        i += 1
    return i


def offset2bit(offset: object) -> int:
    relative = parse_hex(offset) - int("FD00", 16)
    return relative // 2


def prepare_array(text: str) -> ECUMap:
    """Parse ME7Info .ecu text into a dict compatible with the old PHP script."""
    ignored = {";", "#", "/", "["}
    result: ECUMap = {}

    for raw_line in text.splitlines():
        line = raw_line.replace("\r", "")
        if not line or line[0] in ignored:
            continue

        pieces = line.split("{")
        comments: List[str] = []
        for idx in range(1, len(pieces)):
            end = pieces[idx].find("}")
            if end == -1:
                continue
            comments.append(pieces[idx][:end])
            pieces[idx] = f"#COMMENT{len(comments) - 1}" + pieces[idx][end:]

        line = "{".join(pieces)
        line = line.replace("\t", "").replace(" ", "")
        cols = line.split(",")
        if len(cols) < 10:
            continue

        name = cols[0].lower()
        values: List[str] = []
        for col in cols[1:]:
            for z, comment in enumerate(comments):
                col = col.replace(f"#COMMENT{z}", comment)
            values.append(col)
        result[name] = values

    return result


def ecu_obn(ecu: ECUMap, name: str) -> Optional[str]:
    row = ecu.get(name.lower())
    if not row or len(row) < 2 or not row[1]:
        return None
    v = row[1]
    return v[2:] if v.lower().startswith("0x") else v


def ecu_maskbit(ecu: ECUMap, name: str) -> Optional[int]:
    row = ecu.get(name.lower())
    if not row or len(row) < 4 or not row[3]:
        return None
    return bitmask2int(row[3])


def choose_trigger(ecu: ECUMap, trigger: str) -> Tuple[str, str, int]:
    trigger = trigger.lower().strip()
    candidates = {
        "brake": ["b_br"],
        "clutch": ["b_kuppl"],
        "cruise_set": ["b_fgrsec", "s_fgrsv", "b_fgrtdc"],
        "cruise_res": ["b_fgrwac", "b_fgrtuc", "s_fgrwb"],
        "cruise_main": ["b_fgrhsc", "s_fgrhs"],
    }.get(trigger)

    if not candidates:
        fail("Unknown trigger. Use: brake, clutch, cruise_set, cruise_res, cruise_main")

    for name in candidates:
        addr = ecu_obn(ecu, name)
        mask = ecu_maskbit(ecu, name)
        if addr is not None and mask is not None:
            return name, addr, mask

    fail(f"Trigger '{trigger}' not found in ECU dump. Tried: {', '.join(candidates)}")
    raise AssertionError("unreachable")


def find_ftomn(data: bytearray) -> List[int]:
    found: List[int] = []
    ln = len(data)

    for i in range(0, ln - 26):
        if data[i] != 0x05 or data[i + 1] == 0x05 or data[i + 11] != 0x05:
            continue
        if data[i + 24] != 0x08 or data[i + 25] != 0x05:
            continue
        found.append(i + 22)

    if not found:
        for i in range(0, ln - 13):
            if data[i] != 0x05 or data[i + 1] == 0x05 or data[i + 11] != 0x05 or data[i + 12] != 0x07:
                continue
            found.append(i + 11)

    return found


def find_hole(
    data: bytearray,
    size: int = 256,
    start: int = 0,
    end: int = 0,
    avoid_start: int = -1,
    avoid_end: int = -1,
) -> Optional[int]:
    ff_count = 0
    bin_size = len(data)
    if end >= bin_size or end == 0:
        end = bin_size - 64
    if start < 0:
        start = 0

    for i in range(end, start, -1):
        if data[i] == 0xFF:
            ff_count += 1
        elif ff_count >= size:
            this_offset = 16 - ((i + 16) % 16) + i + 16
            space = ff_count - (this_offset - i)
            cave_end = this_offset + size
            overlaps = avoid_start >= 0 and this_offset < avoid_end and cave_end > avoid_start
            if space > size and not overlaps:
                return this_offset
            ff_count = 0
        else:
            ff_count = 0
    return None


def find_hook_offset(data: bytearray) -> int:
    pattern = b"\xD7\x40\x06\x02\x03\xF8"
    search = 0
    jump: Optional[int] = None
    while True:
        pos = data.find(pattern, search + 1)
        if pos == -1:
            break
        if pos != 0:
            jump = pos - 4
        search = pos
    if jump is None:
        fail("Cannot find hook pattern D7 40 06 02 03 F8")
    return jump


def da_target_to_bytes(addr: int) -> Tuple[int, int, int]:
    value = addr + int("800000", 16)
    hx = f"{value:06X}"
    # This is the same unusual byte order used by the original PHP.
    return int(hx[0:2], 16), int(hx[4:6], 16), int(hx[2:4], 16)


def write_da_call(data: bytearray, offset: int, target_addr: int) -> None:
    b1, b2, b3 = da_target_to_bytes(target_addr)
    data[offset] = 0xDA
    data[offset + 1] = b1
    data[offset + 2] = b2
    data[offset + 3] = b3


def read_da_target(data: bytearray, offset: int) -> Optional[int]:
    if data[offset] != 0xDA:
        return None
    hx = f"{data[offset + 1]:02X}{data[offset + 3]:02X}{data[offset + 2]:02X}"
    return int(hx, 16) - int("800000", 16)


def emit_word_from_int(data: bytearray, counter: int, value: int) -> int:
    data[counter] = value & 0xFF
    data[counter + 1] = (value >> 8) & 0xFF
    return counter + 2


def emit_word_from_hex(data: bytearray, counter: int, value: str) -> int:
    return emit_word_from_int(data, counter, parse_hex(value))


def emit_word_plus_8000_from_hex(data: bytearray, counter: int, value: str) -> int:
    return emit_word_from_int(data, counter, parse_hex(value) + int("8000", 16))


def write_default_config(data: bytearray, vars_addr: int) -> None:
    # Original rollingv3 default config bytes.
    defaults = [0xE6, 0xFF, 0xB0, 0x36, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
    for i, b in enumerate(defaults):
        data[vars_addr + i] = b


def write_rolling_code(
    data: bytearray,
    codecave: int,
    vars_addr: int,
    trigger_addr: str,
    trigger_mask_bit: int,
    wped: str,
    nmot_w: str,
    tsrldyn: str,
    jumpback1: int,
    jumpback2: int,
    chain_target: Optional[int],
) -> int:
    c = codecave

    # Trigger bit check: originally b_br. Now selectable.
    data[c] = 0x9A; c += 1
    data[c] = offset2bit(trigger_addr) & 0xFF; c += 1
    data[c] = 0x12; c += 1
    data[c] = int(f"{trigger_mask_bit:X}0", 16) & 0xFF; c += 1
    data[c] = 0xC2; c += 1
    data[c] = 0xF4; c += 1
    c = emit_word_plus_8000_from_hex(data, c, wped)
    data[c] = 0xD7; c += 1
    data[c] = 0x00; c += 1
    data[c] = 0x81; c += 1
    data[c] = 0x00; c += 1
    data[c] = 0xC2; c += 1
    data[c] = 0xF9; c += 1
    c = emit_word_from_int(data, c, vars_addr)

    data[c] = 0x40; c += 1
    data[c] = 0x49; c += 1
    data[c] = 0xFD; c += 1
    data[c] = 0x0A; c += 1
    data[c] = 0xF2; c += 1
    data[c] = 0xF4; c += 1
    c = emit_word_from_hex(data, c, nmot_w)
    data[c] = 0xD7; c += 1
    data[c] = 0x00; c += 1
    data[c] = 0x81; c += 1
    data[c] = 0x00; c += 1
    data[c] = 0xF2; c += 1
    data[c] = 0xF9; c += 1
    c = emit_word_from_int(data, c, vars_addr + 2)

    data[c] = 0x40; c += 1
    data[c] = 0x49; c += 1
    data[c] = 0xFD; c += 1
    data[c] = 0x02; c += 1
    data[c] = 0xF7; c += 1
    data[c] = 0x8E; c += 1
    c = emit_word_plus_8000_from_hex(data, c, tsrldyn)

    if chain_target is not None:
        # Chain mode: call old launch.exe/ALS code cave, then return.
        b1, b2, b3 = da_target_to_bytes(chain_target)
        data[c] = 0xDA; c += 1
        data[c] = b1; c += 1
        data[c] = b2; c += 1
        data[c] = b3; c += 1
        data[c] = 0xDB; c += 1
        data[c] = 0x00; c += 1
    else:
        # Solo mode: execute overwritten original instruction tail and return.
        data[c] = 0xF3; c += 1
        data[c] = 0xF8; c += 1
        data[c] = jumpback1; c += 1
        data[c] = jumpback2; c += 1
        data[c] = 0xDB; c += 1
        data[c] = 0x00; c += 1

    return c - codecave


def patch_file(
    bin_file: Path,
    ecu_file: Path,
    code_arg: str = "auto",
    vars_arg: str = "auto",
    trigger_name: str = "cruise_set",
    output: Optional[Path] = None,
) -> Path:
    if not bin_file.exists():
        fail(f"BIN not found: {bin_file}")
    if not ecu_file.exists():
        fail(f"ECU dump not found: {ecu_file}")

    ecu = prepare_array(ecu_file.read_text(errors="replace"))
    data = bytearray(bin_file.read_bytes())
    if len(data) < 1024:
        fail("Cannot read BIN or file too small")

    info("finding tsrldyn...")
    tsrldyn = ecu_obn(ecu, "tsrldyn")
    if not tsrldyn:
        fail("tsrldyn not found")
    info(f"found: {tsrldyn}")

    info("finding nmot_w...")
    nmot_w = ecu_obn(ecu, "nmot_w")
    if not nmot_w:
        fail("nmot_w not found")
    info(f"found: {nmot_w}")

    info("finding wped...")
    wped = ecu_obn(ecu, "wped")
    dwped = ecu_obn(ecu, "dwped")
    if wped:
        info(f"found: {wped}")
    elif dwped:
        wped = f"{parse_hex(dwped) + 2:X}"
        info(f"wped not found, using dwped + 2: {wped}")
    else:
        fail("wped/dwped not found")

    trig_name, trig_addr, trig_mask = choose_trigger(ecu, trigger_name)
    info(f"trigger selected: {trig_name} at {trig_addr}.{trig_mask}")

    ftomn = find_ftomn(data)
    if ftomn:
        ft = ftomn[0]
        info(f"FTOMN found: 0x{ft:X} IS: {byte_hex(data, ft)} -> CHANGED TO 00")
        data[ft] = 0x00
    else:
        info("FTOMN not found; continuing without FTOMN change")

    jump = find_hook_offset(data)
    info(f"main hook offset: 0x{jump:X}")

    chain_target: Optional[int] = None
    jumpback1 = data[jump + 2]
    jumpback2 = data[jump + 3]

    if data[jump] == 0xDA:
        old = read_da_target(data, jump)
        if old is None or old < 0 or old >= len(data):
            fail("Existing DA hook target cannot be decoded")
        chain_target = old
        info("existing DA hook detected -> CHAIN MODE")
        info(f"old launch/ALS code cave target: 0x{chain_target:X}")
    else:
        info("no DA hook detected -> SOLO MODE")

    if code_arg.lower() in {"", "auto"}:
        codecave = find_hole(
            data,
            256,
            avoid_start=chain_target if chain_target is not None else -1,
            avoid_end=(chain_target + 512) if chain_target is not None else -1,
        )
        if codecave is None:
            fail("Cannot find space for Rolling main code")
    else:
        codecave = parse_hex(code_arg)
    info(f"Rolling code cave: 0x{codecave:X}")

    if vars_arg.lower() in {"", "auto"}:
        vars_addr = find_hole(data, 32, int("17000", 16), int("18000", 16))
        if vars_addr is None:
            fail("Cannot find space for Rolling variables in 0x17000-0x18000")
    else:
        vars_addr = parse_hex(vars_arg)
    info(f"Rolling vars: 0x{vars_addr:X}")

    write_da_call(data, jump, codecave)
    write_default_config(data, vars_addr)
    written = write_rolling_code(
        data=data,
        codecave=codecave,
        vars_addr=vars_addr,
        trigger_addr=trig_addr,
        trigger_mask_bit=trig_mask,
        wped=wped,
        nmot_w=nmot_w,
        tsrldyn=tsrldyn,
        jumpback1=jumpback1,
        jumpback2=jumpback2,
        chain_target=chain_target,
    )

    out = output if output is not None else Path(gen_output_name(str(bin_file)))
    out.write_bytes(data)

    info(f"Rolling code bytes written: {written}")
    info(f"Result written successfully: {out}")
    info("REMEMBER: correct checksums before flashing.")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Rolling Anti-Lag ME7.5 patcher with launch.exe chain mode")
    parser.add_argument("bin", help="Input ECU BIN file")
    parser.add_argument("ecu", help="ME7Info .ecu dump")
    parser.add_argument("code", nargs="?", default="auto", help="Rolling code cave offset, e.g. 0xA3000, or auto")
    parser.add_argument("vars", nargs="?", default="auto", help="Rolling variable offset, e.g. 0x17A00, or auto")
    parser.add_argument("trigger", nargs="?", default="cruise_set", help="brake, clutch, cruise_set, cruise_res, cruise_main")
    parser.add_argument("-o", "--output", help="Output BIN path")
    args = parser.parse_args()

    patch_file(
        bin_file=Path(args.bin),
        ecu_file=Path(args.ecu),
        code_arg=args.code,
        vars_arg=args.vars,
        trigger_name=args.trigger,
        output=Path(args.output) if args.output else None,
    )


if __name__ == "__main__":
    main()
