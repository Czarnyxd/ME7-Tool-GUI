#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import shutil
import os
import atexit
import subprocess
import sys
import traceback
import re
import tempfile
import time
from html import escape
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QInputDialog,
    QMessageBox,
    QTextEdit,
    QTextBrowser,
)
from PySide6.QtUiTools import QUiLoader

# PyInstaller one-file runtime: copy bundled resources to the system TEMP folder.
def _prepare_runtime_dir() -> tuple[Path, Path | None]:
    frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return Path(__file__).resolve().parent, None

    bundle = Path(getattr(sys, "_MEIPASS")).resolve()
    temp_root = Path(tempfile.gettempdir())

    # Remove only abandoned runtime folders older than 24 hours.
    # This avoids touching another currently running application instance.
    now = time.time()
    for old_runtime in temp_root.glob("ME7_Desktop_runtime_*"):
        try:
            if old_runtime.is_dir() and now - old_runtime.stat().st_mtime > 24 * 60 * 60:
                shutil.rmtree(old_runtime, ignore_errors=True)
        except OSError:
            pass

    runtime = temp_root / f"ME7_Desktop_runtime_{os.getpid()}"
    runtime.mkdir(parents=True, exist_ok=True)

    try:
        for name in ("tools", "defs"):
            src = bundle / name
            dst = runtime / name
            if src.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)

        for name in ("ME7.5 TOOL.ui", "ME7.5 CONFIG.ui", "ME7.5 info.ui", "ico.png"):
            src = bundle / name
            if src.exists():
                shutil.copy2(src, runtime / name)

        # Empty work directories are created at runtime because PyInstaller does
        # not preserve empty directories.
        (runtime / "work").mkdir(parents=True, exist_ok=True)
        (runtime / "ecus").mkdir(parents=True, exist_ok=True)
        (runtime / "tools" / "ecus").mkdir(parents=True, exist_ok=True)
    except Exception:
        shutil.rmtree(runtime, ignore_errors=True)
        raise

    os.environ["ME7_RUNTIME_DIR"] = str(runtime)
    return runtime, runtime

APP_RUNTIME_DIR, _FROZEN_RUNTIME_DIR = _prepare_runtime_dir()

def _cleanup_frozen_runtime() -> None:
    if _FROZEN_RUNTIME_DIR and _FROZEN_RUNTIME_DIR.exists():
        shutil.rmtree(_FROZEN_RUNTIME_DIR, ignore_errors=True)

atexit.register(_cleanup_frozen_runtime)

from me7_core import (
    new_job_dir,
    analyze_file,
    patch_pipeline,
    finalize_checksum,
    generate_ecu,
    WORK_DIR,
    read_bin,
    detect_launch_patch,
    detect_rolling_patch,
    ALS_MAPS,
    write_config_value,
    parse_ecu_file,
    patch_lc_activator,
    write_rolling_config,
)

APP_DIR = APP_RUNTIME_DIR
UI_MAIN = APP_DIR / "ME7.5 TOOL.ui"
UI_CONFIG = APP_DIR / "ME7.5 CONFIG.ui"
UI_INFO = APP_DIR / "ME7.5 info.ui"

# Fallback tylko dla plików pobranych z ChatGPT, gdzie system dopisuje (1)/(2).
# W normalnym folderze programu używane są oryginalne nazwy z Qt Designer.
def _first_existing(*names: str) -> Path:
    for name in names:
        p = APP_DIR / name
        if p.exists():
            return p
    return APP_DIR / names[0]

UI_MAIN = _first_existing("ME7.5 TOOL.ui", "ME7.5 TOOL(5).ui", "ME7.5 TOOL(4).ui", "ME7.5 TOOL(3).ui", "ME7.5 TOOL(2).ui", "ME7.5 TOOL(1).ui")
UI_CONFIG = _first_existing("ME7.5 CONFIG.ui", "ME7.5 CONFIG(3).ui", "ME7.5 CONFIG(2).ui", "ME7.5 CONFIG(1).ui")
UI_INFO = _first_existing("ME7.5 info.ui", "ME7.5 info(2).ui", "ME7.5 info(1).ui")

LC_ACTIVATORS = [
    ("Clutch", "B_kuppl"),
    ("Brake", "B_brems"),
]
ROLLING_ACTIVATORS = [
    ("Cruise SET", "cruise_set"),
    ("Cruise RES", "cruise_res"),
    ("Cruise MAIN", "cruise_main"),
    ("Brake", "brake"),
]



# -----------------------------------------------------------------------------
# Rolling trigger patch helper
# -----------------------------------------------------------------------------

ROLLING_TRIGGER_ALIASES = {
    "cruise_set": ["b_fgrsec", "s_fgrsv", "b_fgrtdc"],
    "cruise_res": ["b_fgrwac", "b_fgrtuc", "s_fgrwb"],
    "cruise_main": ["b_fgrhsc", "s_fgrhs"],
    "brake": ["b_br"],
    "clutch": ["b_kuppl"],
}

ROLLING_TRIGGER_LABELS = {
    "cruise_set": "Cruise SET",
    "cruise_res": "Cruise RES",
    "cruise_main": "Cruise MAIN",
    "brake": "Brake",
    "clutch": "Clutch",
}


def _parse_int_auto_app(value: str) -> int:
    s = str(value or "").strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s, 10)


def _parse_ecu_symbols_for_rolling(ecu_path: Path) -> dict:
    """Parse .ecu file only for the symbols needed by Rolling trigger selection."""
    result = {}
    try:
        lines = ecu_path.read_text(encoding="latin-1", errors="replace").splitlines()
    except Exception:
        return result

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith((";", "#", "/", "[")) or "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        name = parts[0].lower().strip()
        try:
            addr = _parse_int_auto_app(parts[2])
            mask = _parse_int_auto_app(parts[4])
        except Exception:
            continue
        if mask <= 0:
            continue
        bit = mask.bit_length() - 1
        if not (0 <= bit <= 15):
            continue
        result[name] = {"address": addr, "bit": bit, "mask": mask}
    return result


def _rolling_trigger_encoding_from_ecu(ecu_path: Path, trigger_key: str) -> Optional[dict]:
    key = str(trigger_key or "cruise_set").strip().lower()
    symbols = _parse_ecu_symbols_for_rolling(ecu_path)
    for sym_name in ROLLING_TRIGGER_ALIASES.get(key, []):
        sym = symbols.get(sym_name.lower())
        if not sym:
            continue
        addr = int(sym["address"])
        bit = int(sym["bit"])
        low = addr & 0xFFFF
        if not (0xFD00 <= low <= 0xFDFF):
            continue
        return {
            "key": key,
            "label": ROLLING_TRIGGER_LABELS.get(key, key),
            "symbol": sym_name,
            "address": addr,
            "bit": bit,
            "addr_byte": (low - 0xFD00) // 2,
            "mask_byte": (bit & 0x0F) << 4,
        }
    return None


def _find_rolling_code_candidates(data: bytes, vars_addr: Optional[int], rolling_patch: Optional[dict] = None) -> list[int]:
    candidates: list[int] = []

    def add(pos: Optional[int]):
        if pos is None:
            return
        try:
            p = int(pos)
        except Exception:
            return
        if 0 <= p <= len(data) - 16 and p not in candidates:
            candidates.append(p)

    # Best source: find the exact Rolling code header that references Rolling vars at +14.
    if vars_addr is not None:
        vars_le = (int(vars_addr) & 0xFFFF).to_bytes(2, "little", signed=False)
        for i in range(0, len(data) - 16):
            if (
                data[i] == 0x9A and data[i + 2] == 0x12
                and data[i + 4] == 0xC2 and data[i + 5] == 0xF4
                and data[i + 12] == 0xC2 and data[i + 13] == 0xF9
                and data[i + 14:i + 16] == vars_le
            ):
                add(i)

    # Analyzer-provided offsets, if present.
    rp = rolling_patch or {}
    for k in ("code", "code_cave", "function_offset", "offset"):
        if k in rp and rp.get(k) is not None:
            add(rp.get(k))

    # Last resort: generic Rolling-like header. Usually only one is present.
    if not candidates:
        for i in range(0, len(data) - 16):
            if (
                data[i] == 0x9A and data[i + 2] == 0x12
                and data[i + 4] == 0xC2 and data[i + 5] == 0xF4
                and data[i + 8] == 0xD7 and data[i + 9] == 0x00
                and data[i + 12] == 0xC2 and data[i + 13] == 0xF9
            ):
                add(i)
    return candidates


def patch_rolling_trigger_in_bin_file(bin_path: Path, job_dir: Path, trigger_key: str, log: list[str]) -> bool:
    """Patch Rolling activation bytes after rolling_chain.py installation.

    rolling_chain.py always stores the trigger in the first four bytes of the
    Rolling code cave:

        9A <FDxx offset byte> 12 <bit_index << 4>

    This function rewrites only those two trigger bytes. Launch logic is not touched.
    """
    key = str(trigger_key or "cruise_set").strip().lower()
    if key not in ROLLING_TRIGGER_ALIASES:
        log.append(f"Rolling trigger patch skipped: unsupported trigger '{trigger_key}'.")
        return False

    if not bin_path.exists():
        log.append("Rolling trigger patch skipped: BIN file not found.")
        return False

    data = bytearray(bin_path.read_bytes())
    rolling_patch = detect_rolling_patch(data, job_dir)
    vars_addr = rolling_patch.get("vars") if isinstance(rolling_patch, dict) else None
    vars_int = int(vars_addr) if vars_addr is not None else None

    if not (isinstance(rolling_patch, dict) and rolling_patch.get("installed")):
        log.append("Rolling trigger patch skipped: Rolling Anti-Lag not detected in current BIN.")
        return False

    ecu = generate_ecu(bin_path, job_dir, log)
    if not ecu or not ecu.exists():
        log.append("Rolling trigger patch skipped: ECU definition could not be generated.")
        return False

    enc = _rolling_trigger_encoding_from_ecu(ecu, key)
    if not enc:
        log.append(f"Rolling trigger patch skipped: trigger '{key}' not available in ECU definition.")
        return False

    candidates = _find_rolling_code_candidates(data, vars_int, rolling_patch)
    if not candidates:
        log.append("Rolling trigger patch skipped: Rolling code cave was not found.")
        return False

    changed = False
    for code in candidates[:1]:
        old = bytes(data[code:code + 4])
        data[code] = 0x9A
        data[code + 1] = int(enc["addr_byte"]) & 0xFF
        data[code + 2] = 0x12
        data[code + 3] = int(enc["mask_byte"]) & 0xFF
        new = bytes(data[code:code + 4])
        changed = True
        log.append(
            "GUI_ROLLING_PATCH "
            f"trigger={enc['label']} symbol={enc['symbol']} "
            f"addr=0x{int(enc['address']):06X}.{int(enc['bit'])} "
            f"code=0x{code:X} old={old.hex(' ').upper()} new={new.hex(' ').upper()}"
        )
        log.append(f"GUI_ROLLING trigger={enc['label']}")
        log.append(f"GUI_ROLLING code=0x{code:X}")

    if changed:
        bin_path.write_bytes(data)
    return changed


def patch_pipeline_with_final_rolling_trigger(*args, rolling_trigger: str = "cruise_set", **kwargs) -> dict:
    """Run normal patch pipeline, then force selected Rolling trigger bytes."""
    result = patch_pipeline(*args, rolling_trigger=rolling_trigger, **kwargs)
    try:
        bin_path = Path(args[0])
        job_dir = Path(args[1])
        raw = result.get("raw") if isinstance(result, dict) else None
        target = job_dir / raw if raw else bin_path
        logs = result.setdefault("log", [])
        if kwargs.get("rolling") and target.exists():
            patch_rolling_trigger_in_bin_file(target, job_dir, rolling_trigger, logs)
    except Exception as e:
        try:
            result.setdefault("log", []).append(f"Rolling trigger final patch error: {type(e).__name__}: {e}")
        except Exception:
            pass
    return result


def configure_file_no_checksum(
    bin_path: Path,
    values: dict,
    activator: str,
    job_dir: Path,
    *,
    rolling_values: Optional[dict] = None,
    rolling_trigger: str = "cruise_set",
) -> dict:
    """Zapisuje konfigurację Launch/Rolling do RAW, ale NIE liczy checksum.

    Checksum is calculated only after all selected configuration values are written.
    """
    log: list[str] = []
    data = read_bin(bin_path)
    patch = detect_launch_patch(data, job_dir)
    rolling_patch = detect_rolling_patch(data, job_dir)

    if (not patch.installed or patch.config_base is None) and not (rolling_patch.get("installed") and rolling_patch.get("vars") is not None):
        log.append("Brak wykrytego configu Launch/Rolling — pomijam zapis konfiguracji.")
        return {"ok": True, "raw": bin_path.name, "log": log}

    if patch.installed and patch.config_base is not None:
        for name, unit, rel, size, scale, mn, mx in ALS_MAPS:
            if name in values:
                write_config_value(data, patch.config_base, rel, size, scale, float(values[name]))

        ecu = generate_ecu(bin_path, job_dir, log)
        switches = parse_ecu_file(ecu) if ecu else {}
        if patch.function_offset is not None and activator in switches:
            patch_lc_activator(data, patch.function_offset, switches[activator])
            log.append("Aktywator Launch ustawiony: " + switches[activator].pretty())

    else:
        log.append("Launch config pominięty — brak wykrytego config_base Launch.")

    if rolling_patch.get("installed") and rolling_patch.get("vars") is not None:
        write_rolling_config(data, int(rolling_patch["vars"]), rolling_values or {}, log)
    elif rolling_values:
        log.append("Rolling config z GUI odebrany, ale brak wykrytego Rolling vars — nie zapisano Rolling.")

    raw = job_dir / f"{bin_path.stem}_configured_no_checksum.bin"
    raw.write_bytes(data)

    # Force Rolling trigger after configuration save. This edits only the Rolling
    # trigger bytes 9A XX 12 YY in the Rolling code cave.
    patch_rolling_trigger_in_bin_file(raw, job_dir, rolling_trigger, log)
    log.append("Zapisano konfigurację BEZ checksum: " + raw.name)
    return {"ok": True, "raw": raw.name, "log": log}


def save_full_pipeline(
    bin_path: Path,
    job_dir: Path,
    *,
    has_config: bool,
    values: dict,
    activator: str,
    rolling_values: dict,
    rolling_trigger: str = "cruise_set",
) -> dict:
    """Final save: configuration -> checksum."""
    log: list[str] = []
    current = bin_path

    log.append("===== START FINAL SAVE =====")
    log.append("Order: Launch/Rolling configuration -> checksum.")

    if has_config:
        cfg = configure_file_no_checksum(
            current,
            values,
            activator,
            job_dir,
            rolling_values=rolling_values,
            rolling_trigger=rolling_trigger,
        )
        log.extend(cfg.get("log", []))
        if cfg.get("raw"):
            current = job_dir / cfg["raw"]

    cs = finalize_checksum(current, job_dir)
    log.extend(cs.get("log", []))
    cs = dict(cs)
    cs["log"] = log
    log.append("===== END FINAL SAVE =====")
    return cs


# -----------------------------------------------------------------------------
# Professional log renderer
# -----------------------------------------------------------------------------



def detect_ftomn_in_bin(data: bytes) -> dict:
    """Find FTOMN using the same heuristic as the original launch.php.

    Primary 1MB pattern: address = match_start + 22
    Fallback pattern:    address = match_start + 11
    512k fallback:       C2 F4 .. .. 40 94 9D 02 C2 F9 + pointer

    """
    found: list[int] = []
    n = len(data)

    # Primary launch.php pattern.
    for i in range(0, max(0, n - 26)):
        if data[i] != 0x05:
            continue
        if data[i + 1] == 0x05:
            continue
        if data[i + 11] != 0x05:
            continue
        if data[i + 24] != 0x08 or data[i + 25] != 0x05:
            continue
        found.append(i + 22)

    # Fallback launch.php pattern.
    method = "primary"
    if not found:
        method = "fallback"
        for i in range(0, max(0, n - 13)):
            if data[i] != 0x05:
                continue
            if data[i + 1] == 0x05:
                continue
            if data[i + 11] != 0x05:
                continue
            if data[i + 12] != 0x07:
                continue
            found.append(i + 11)

    # 512 kB fallback from launch.php.
    if not found and n == 512 * 1024:
        method = "512k-regex"
        m = list(re.finditer(rb"\xC2\xF4..\x40\x94\x9D\x02\xC2\xF9", data, re.S))
        if m:
            temp = m[-1].start() + 10
            if temp + 2 <= n:
                ptr = data[temp:temp + 2]
                addr = int("1" + ptr[::-1].hex(), 16)
                if 0 <= addr < n:
                    found.append(addr)

    if not found:
        return {"found": False, "count": 0, "method": method}

    addr = found[0]
    val = data[addr] if 0 <= addr < n else None
    return {
        "found": val is not None,
        "count": len(found),
        "method": method,
        "address": addr,
        "value": val,
        "all": found,
    }


def _last_regex(logs: list[str], pattern: str, flags: int = re.IGNORECASE | re.S) -> Optional[re.Match]:
    text = "\n".join(str(x) for x in logs)
    matches = list(re.finditer(pattern, text, flags))
    return matches[-1] if matches else None


def _all_regex(logs: list[str], pattern: str, flags: int = re.IGNORECASE | re.S) -> list[re.Match]:
    text = "\n".join(str(x) for x in logs)
    return list(re.finditer(pattern, text, flags))


def _clean_ecu_value(value: str) -> str:
    return (value or "").strip().strip("'").strip()


def _yes(status: bool) -> str:
    return "Installed" if status else "Not Detected"


def _status_line(label: str, value: str, width: int = 22) -> str:
    return f"{label:<{width}} {value}"


def _kv(label: str, value: object, width: int = 22) -> str:
    if value is None or value == "":
        return ""
    return f"    {label:<{width}} {value}"


def _extract_summary(logs: list[str], current_name: str = "", output_name: str = "") -> dict:
    text = "\n".join(str(x) for x in logs)
    d: dict[str, object] = {}

    # Input file / ECU identity
    m = re.search(r"Wczytano\s+([^,\n]+),\s+rozmiar\s+(\d+)\s+bajt", text, re.I)
    if m:
        d["input_name"] = Path(m.group(1).strip()).name
        d["input_size"] = int(m.group(2))
    elif current_name:
        d["input_name"] = current_name

    ecu_patterns = {
        "hardware": r"'([^']+)'\s*\(SSECUHN\)",
        "software": r"'([^']+)'\s*\(SSECUSN\)",
        "part": r"'([^']+)'\s*\(VAG part number\)",
        "swver": r"'([^']+)'\s*\(VAG sw number\)",
        "engine": r"'([^']+)'\s*\(engine id\)",
    }
    for key, pat in ecu_patterns.items():
        m = re.search(pat, text, re.I)
        if m:
            d[key] = _clean_ecu_value(m.group(1))

    d["firmware_ok"] = "No errors found. File is OK" in text
    d["definition_ok"] = bool(re.search(r"ECU skopiowany do job_dir|written output to file", text, re.I))

    # Switches
    m = re.search(r"B_kuppl:\s*(0x[0-9A-F]+\.\d+).*?B_brems:\s*(0x[0-9A-F]+\.\d+)", text, re.I | re.S)
    if m:
        d["clutch"] = m.group(1).upper().replace("X", "x")
        d["brake"] = m.group(2).upper().replace("X", "x")
    # Rolling trigger from patcher output. Use LAST occurrence, because one
    # application session can contain several analyses/patches.
    trigger_matches = re.findall(r"trigger selected:\s*([^\s]+)\s+at\s+([0-9A-F]{4,6}\.\d+)", text, re.I)
    if trigger_matches:
        trig_sym, trig_addr_raw = trigger_matches[-1]
        trig_sym = trig_sym.strip().lower()
        trig_addr = "0x" + trig_addr_raw.upper()
        d["rolling_trigger_symbol"] = trig_sym
        trigger_symbol_labels = {
            "b_br": "Brake",
            "b_brems": "Brake",
            "b_kuppl": "Clutch",
            "b_fgrsec": "Cruise SET",
            "s_fgrsv": "Cruise SET",
            "b_fgrtdc": "Cruise SET",
            "b_fgrwac": "Cruise RES",
            "b_fgrtuc": "Cruise RES",
            "s_fgrwb": "Cruise RES",
        }
        if trig_sym in trigger_symbol_labels:
            d["rolling_trigger"] = trigger_symbol_labels[trig_sym]
        if trig_sym in ("b_fgrsec", "s_fgrsv", "b_fgrtdc"):
            d["cruise_set"] = trig_addr
        elif trig_sym in ("b_fgrwac", "b_fgrtuc", "s_fgrwb"):
            d["cruise_res"] = trig_addr

    # Stable GUI input markers. These are preferred over the raw tool text because
    # they can include all detected inputs, not only clutch/brake.
    inputs = []
    for im in re.finditer(r"GUI_INPUT\s+label=(.*?)\s+addr=(0x[0-9A-F]+\.\d+)", text, re.I):
        label = im.group(1).strip()
        addr = im.group(2).strip().upper().replace("X", "x")
        if label and addr and (label, addr) not in inputs:
            inputs.append((label, addr))
    if inputs:
        d["inputs"] = inputs

    # Launch
    d["launch_installed"] = (
        "code writed successfully" in text.lower()
        or "Znaleziono funkcję Setzi" in text
        or bool(re.search(r"GUI_DETECT\s+launch_installed=1", text, re.I))
    )
    m = re.search(r"GUI_LAUNCH\s+hook=(0x[0-9A-F]+)", text, re.I)
    if m:
        d["launch_hook"] = m.group(1).upper().replace("X", "x")
    m = re.search(r"GUI_LAUNCH\s+code=(0x[0-9A-F]+)", text, re.I)
    if m:
        d["launch_code"] = m.group(1).upper().replace("X", "x")
    m = re.search(r"GUI_LAUNCH\s+config=(0x[0-9A-F]+)", text, re.I)
    if m:
        d["launch_config"] = m.group(1).upper().replace("X", "x")
    m = re.search(r"FTOMN found:\s*([0-9A-Fx]+).*?FTOMN IS:\s*([0-9A-F]{2}).*?FTOMN CHANGED TO\s*0x([0-9A-F]{2})", text, re.I | re.S)
    if m:
        d["ftomn_addr"] = "0x" + m.group(1).replace("0x", "").upper()
        d["ftomn_from"] = m.group(2).upper()
        d["ftomn_to"] = m.group(3).upper()
    m = re.search(r"GUI_FTOMN\s+addr=(0x[0-9A-F]+)\s+value=(0x[0-9A-F]{2})\s+count=(\d+)\s+method=([^\s]+)", text, re.I)
    if m:
        d["ftomn_addr"] = m.group(1).upper().replace("X", "x")
        d["ftomn_value"] = m.group(2).upper().replace("X", "x")
        d["ftomn_count"] = m.group(3)
        d["ftomn_method"] = m.group(4)
    launch_section = ""
    m = re.search(r"Uruchamiam:.*?launch\.exe.*?===== KONIEC LOG launch\.exe", text, re.I | re.S)
    if m:
        launch_section = m.group(0)
    spaces = re.findall(r"space located at:\s*0x([0-9A-F]+)", launch_section, re.I)
    if len(spaces) >= 1:
        d["launch_code"] = "0x" + spaces[0].upper()
    if len(spaces) >= 2:
        d["launch_config"] = "0x" + spaces[1].upper()
    m = re.search(r"call will be located at:\s*0x([0-9A-F]+)", launch_section, re.I)
    if m:
        d["launch_hook"] = "0x" + m.group(1).upper()

    # Rolling
    d["rolling_installed"] = (
        ("Result written successfully" in text and "Rolling code cave" in text)
        or bool(re.search(r"GUI_DETECT\s+rolling_installed=1", text, re.I))
    )
    m = re.search(r"GUI_ROLLING\s+code=(0x[0-9A-F]+)", text, re.I)
    if m:
        d["rolling_code"] = m.group(1).upper().replace("X", "x")
    m = re.search(r"GUI_ROLLING\s+vars=(0x[0-9A-F]+)", text, re.I)
    if m:
        d["rolling_vars"] = m.group(1).upper().replace("X", "x")
    m = re.search(r"GUI_ROLLING\s+mode=([^\s]+)", text, re.I)
    if m:
        d["rolling_mode"] = m.group(1).upper()
    matches = re.findall(r"GUI_ROLLING\s+trigger=([^\n]+)", text, re.I)
    if matches:
        d["rolling_trigger"] = matches[-1].strip()
    m = re.search(r"existing DA hook detected\s*->\s*(CHAIN MODE)", text, re.I)
    if m:
        d["rolling_mode"] = "CHAIN"
    m = re.search(r"Rolling code cave:\s*0x([0-9A-F]+)", text, re.I)
    if m:
        d["rolling_code"] = "0x" + m.group(1).upper()
    m = re.search(r"Rolling vars:\s*0x([0-9A-F]+)", text, re.I)
    if m:
        d["rolling_vars"] = "0x" + m.group(1).upper()
    m = re.search(r"main hook offset:\s*0x([0-9A-F]+)", text, re.I)
    if m and "launch_hook" not in d:
        d["launch_hook"] = "0x" + m.group(1).upper()

    # Pops
    d["pops_installed"] = "Pops and Bangs utworzył" in text or "Zapisano:" in text and "KFZWMN" in text
    for key in ["KFZWMN", "KFNWEGM", "KFTVSA", "KFTVSAKAT"]:
        m = re.search(rf"{key}:\s*0x([0-9A-F]+)", text, re.I)
        if m:
            d[key.lower()] = "0x" + m.group(1).upper()
    m = re.search(r"Zmian bajtow:\s*(\d+)", text, re.I)
    if m:
        d["pops_bytes"] = m.group(1)

    # Config values final/analyzed
    patterns = {
        "launch_rpm": r"LaunchRPM=([0-9.]+)",
        "speed": r"SpeedThreshold=([0-9.]+)",
        "rpm_threshold": r"RPMThreshold=([0-9.]+)",
        "throttle": r"AccPedalThreshold=([0-9.]+)",
        "ign_cut": r"IgnitionCutDuration=([0-9.]+)",
        "rolling_rpm": r"RPM Rolling=([0-9.]+)",
        "rolling_pedal": r"Próg pedału gazu=([0-9.]+)",
    }
    for key, pat in patterns.items():
        matches = re.findall(pat, text, re.I)
        if matches:
            d[key] = matches[-1]

    gui_map = {
        "launch_rpm": "LaunchRPM",
        "speed": "SpeedThreshold",
        "rpm_threshold": "RPMThreshold",
        "throttle": "AccPedalThreshold",
        "ign_cut": "IgnitionCutDuration",
    }
    for out_key, gui_key in gui_map.items():
        matches = re.findall(rf"GUI_CONFIG\s+{gui_key}=([0-9.]+)", text, re.I)
        if matches:
            d[out_key] = matches[-1]

    matches = re.findall(r"GUI_ROLLING_CONFIG\s+RollingRPM=([0-9.]+)", text, re.I)
    if matches:
        d["rolling_rpm"] = matches[-1]
    matches = re.findall(r"GUI_ROLLING_CONFIG\s+RollingPedalPercent=([0-9.]+)", text, re.I)
    if matches:
        d["rolling_pedal"] = matches[-1]
    if "activation" not in d:
        m = re.search(r"Aktywator Launch ustawiony:\s*([^:]+):", text, re.I)
        if m:
            name = m.group(1).strip()
            d["activation"] = "Clutch" if "kuppl" in name.lower() else "Brake"

    # Checksum / output
    m = re.search(r"DONE!\s*(\d+)/(\d+)\s*error", text, re.I)
    if m:
        d["checksum_fixed"] = f"{m.group(1)} / {m.group(2)}"
    d["checksum_ok"] = "Checksum OK:" in text or "DONE!" in text
    m = re.search(r"Checksum OK:\s*([^\n]+\.bin)", text, re.I)
    if m:
        d["output_name"] = Path(m.group(1).strip()).name
    if output_name:
        d["output_name"] = Path(output_name).name

    return d


def _build_professional_log(logs: list[str], current_name: str = "", output_name: str = "", *, selected: Optional[dict] = None, line_width: int = 78) -> str:
    s = _extract_summary(logs, current_name, output_name)
    selected = selected or {}
    selected_launch = bool(selected.get("launch", s.get("launch_installed")))
    selected_rolling = bool(selected.get("rolling", s.get("rolling_installed")))
    selected_pops = bool(selected.get("pops", s.get("pops_installed")))

    lines: list[str] = []
    add = lines.append
    line_width = max(40, min(120, int(line_width or 78)))
    sep = "─" * line_width
    top = "═" * line_width

    def section(title: str):
        add("")
        add(sep)
        add(title)
        add(sep)
        add("")

    def block(title: str, status: str = ""):
        add(f"✔ {title}" + (f"\n\n{_kv('Status', status)}" if status else ""))

    def add_kv(label, val):
        line = _kv(label, val)
        if line:
            add(line)

    add(top)
    add("                           ME7 DESKTOP TOOL")
    add(top)

    section("[1/5] ECU ANALYSIS")
    add("✔ Input File")
    add("")
    add_kv("Name", s.get("input_name") or current_name or "-")
    size = s.get("input_size")
    if isinstance(size, int):
        add_kv("Size", f"{size:,} bytes (1 MB)" if size == 1048576 else f"{size:,} bytes")
    add("")
    add("✔ Firmware Integrity")
    add("")
    add_kv("Status", "PASSED" if s.get("firmware_ok") else "PENDING")
    add("")
    add("✔ ECU Information")
    add("")
    add_kv("Hardware Number", s.get("hardware"))
    add_kv("Software Number", s.get("software"))
    add_kv("Part Number", s.get("part"))
    add_kv("Software Version", s.get("swver"))
    add_kv("Engine", s.get("engine"))
    add("")
    add("✔ ECU Definition Analysis")
    add("")
    add_kv("Status", "COMPLETED" if s.get("definition_ok") else "PENDING")

    section("[2/5] FEATURE DETECTION")
    add(_status_line("Launch Control", _yes(bool(s.get("launch_installed")))))
    add(_status_line("Rolling Anti-Lag", _yes(bool(s.get("rolling_installed")))))
    add(_status_line("Pops & Bangs", _yes(bool(s.get("pops_installed")))))
    add("")
    add("Input Detection")
    add("")
    if s.get("inputs"):
        for label, addr in s.get("inputs"):
            add_kv(label, addr)
    else:
        add_kv("Clutch Switch", s.get("clutch"))
        add_kv("Brake Switch", s.get("brake"))
        add_kv("Cruise SET", s.get("cruise_set"))

    section("[3/5] PATCH INSTALLATION")
    if selected_launch or s.get("launch_installed"):
        add("✔ Launch Control")
        add("")
        add_kv("Status", "INSTALLED" if s.get("launch_installed") else "PENDING")
        add_kv("Hook Offset", s.get("launch_hook"))
        add_kv("Code Cave", s.get("launch_code"))
        add_kv("Config Block", s.get("launch_config"))
        if s.get("ftomn_addr"):
            add_kv("FTOMN", f"{s.get('ftomn_addr')} ({s.get('ftomn_from')} → {s.get('ftomn_to')})")
        add("")
    else:
        add("○ Launch Control")
        add("")
        add_kv("Status", "Skipped")
        add("")

    if selected_rolling or s.get("rolling_installed"):
        add("✔ Rolling Anti-Lag")
        add("")
        add_kv("Status", "INSTALLED" if s.get("rolling_installed") else "PENDING")
        add_kv("Installation Mode", s.get("rolling_mode") or ("CHAIN" if s.get("rolling_installed") else "-"))
        add_kv("Trigger", s.get("rolling_trigger") or ("Unknown" if s.get("rolling_installed") else "-"))
        add("")
        add_kv("Code Cave", s.get("rolling_code"))
        add_kv("Variables", s.get("rolling_vars"))
        add("")
    else:
        add("○ Rolling Anti-Lag")
        add("")
        add_kv("Status", "Skipped")
        add("")

    if selected_pops or s.get("pops_installed"):
        add("✔ Pops & Bangs")
        add("")
        add_kv("Status", "INSTALLED" if s.get("pops_installed") else "PENDING")
        add("")
        add_kv("KFZWMN", s.get("kfzwmn"))
        add_kv("KFNWEGM", s.get("kfnwegm"))
        add_kv("KFTVSA", s.get("kftvsa"))
        add_kv("KFTVSAKAT", s.get("kftvsakat"))
        add("")
        add_kv("Modified Bytes", s.get("pops_bytes"))
        add("")
    else:
        add("○ Pops & Bangs")
        add("")
        add_kv("Status", "Skipped")
        add("")


    section("[4/5] CONFIGURATION")
    add("Launch Control")
    add("")
    add_kv("Activation", selected.get("launch_activation") or s.get("activation") or "Unknown")
    add_kv("Launch RPM", f"{float(s.get('launch_rpm')):.0f} rpm" if s.get("launch_rpm") else "")
    add_kv("Speed Threshold", f"{float(s.get('speed')):.0f} km/h" if s.get("speed") else "")
    add_kv("RPM Threshold", f"{float(s.get('rpm_threshold')):.0f} rpm" if s.get("rpm_threshold") else "")
    add_kv("Throttle Threshold", f"{float(s.get('throttle')):.0f} %" if s.get("throttle") else "")
    add_kv("Ignition Cut", f"{float(s.get('ign_cut')):.0f} ms" if s.get("ign_cut") else "")
    add("")
    add("Rolling Anti-Lag")
    add("")
    add_kv("Trigger", s.get("rolling_trigger") or ("Unknown" if s.get("rolling_installed") else ""))
    add_kv("Rolling RPM", f"{float(s.get('rolling_rpm')):.0f} rpm" if s.get("rolling_rpm") else "")
    add_kv("Throttle Threshold", f"{float(s.get('rolling_pedal')):.1f} %" if s.get("rolling_pedal") else "")
    add("")

    section("[5/5] FINALIZATION")
    add("✔ Configuration")
    add("")
    add_kv("Status", "SAVED" if "Zapisano konfigurację" in "\n".join(logs) or s.get("checksum_ok") else "PENDING")
    add("")
    add("✔ Checksum Calculation")
    add("")
    add_kv("Status", "COMPLETED" if s.get("checksum_ok") else "PENDING")
    add_kv("Corrected", s.get("checksum_fixed"))
    add("")
    add("✔ Final Firmware Verification")
    add("")
    add_kv("Status", "PASSED" if s.get("firmware_ok") and s.get("checksum_ok") else "PENDING")

    section("PATCH SUMMARY")
    add(_status_line("Launch Control", "✔ Installed" if s.get("launch_installed") else "○ Skipped"))
    add(_status_line("Rolling Anti-Lag", "✔ Installed" if s.get("rolling_installed") else "○ Skipped"))
    add(_status_line("Pops & Bangs", "✔ Installed" if s.get("pops_installed") else "○ Skipped"))
    add("")
    add(_status_line("Checksum", "✔ Valid" if s.get("checksum_ok") else "○ Pending"))
    add(_status_line("Firmware Verification", "✔ Passed" if s.get("firmware_ok") and (s.get("checksum_ok") or not logs) else "○ Pending"))

    section("FIRMWARE MEMORY MAP")
    if s.get("launch_installed") or selected_launch:
        add("Launch Control")
        add("")
        add_kv("Hook Offset", s.get("launch_hook"))
        add_kv("Code Cave", s.get("launch_code"))
        add_kv("Config Block", s.get("launch_config"))
        add_kv("FTOMN Address", s.get("ftomn_addr"))
        add_kv("FTOMN Value", s.get("ftomn_value") or s.get("ftomn_from"))
        add("")
    if s.get("rolling_installed") or selected_rolling:
        add("Rolling Anti-Lag")
        add("")
        add_kv("Installation Mode", s.get("rolling_mode") or "CHAIN")
        add_kv("Code Cave", s.get("rolling_code"))
        add_kv("Variables", s.get("rolling_vars"))
        add("")
    if s.get("pops_installed") or selected_pops:
        add("Pops & Bangs")
        add("")
        add_kv("KFZWMN", s.get("kfzwmn"))
        add_kv("KFNWEGM", s.get("kfnwegm"))
        add_kv("KFTVSA", s.get("kftvsa"))
        add_kv("KFTVSAKAT", s.get("kftvsakat"))
        add("")

    section("OUTPUT FILE")
    add(str(s.get("output_name") or output_name or "Pending"))
    add("")
    add("Workspace")
    add("")
    add_kv("Temporary Workspace", "Cleaned after closing the application")
    add("")
    add(top)
    if s.get("checksum_ok") and (s.get("firmware_ok") or "No errors found" in "\n".join(logs)):
        add("                    OPERATION COMPLETED SUCCESSFULLY")
    else:
        add("                         OPERATION IN PROGRESS")
    add(top)
    return "\n".join(lines)

def load_ui(path: Path) -> QMainWindow:
    if not path.exists():
        raise FileNotFoundError(f"Brak pliku UI: {path.name}")
    loader = QUiLoader()
    win = loader.load(str(path))
    if win is None:
        raise RuntimeError(f"Nie można wczytać UI: {path.name}")
    return win


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.done.emit(self.fn(*self.args, **self.kwargs))
        except Exception:
            self.failed.emit(traceback.format_exc())


class DesktopController:
    def __init__(self):
        self.main = load_ui(UI_MAIN)
        self.config = load_ui(UI_CONFIG)
        self.info = load_ui(UI_INFO)

        self.job_dir: Optional[Path] = None
        self.current_bin: Optional[Path] = None
        self.analysis: dict = {}
        self.full_log: list[str] = []
        self.worker: Optional[Worker] = None
        self.output_ready: Optional[Path] = None
        self.pending_checksum = False
        self.selected_features = {"launch": False, "rolling": False, "pops": False}
        self.detected_features = {"launch": False, "rolling": False, "pops": False}
        self.detected_rolling_trigger: Optional[str] = None
        self.pops_profile = "medium"
        self._pops_dialog_active = False

        self._prepare_widgets()
        self._connect_signals()
        self._set_busy(False)

    def _prepare_widgets(self):
        self.main.setWindowTitle("ME7 Desktop Tool")
        icon_path = APP_DIR / "ico.png"
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            self.main.setWindowIcon(icon)
            self.config.setWindowIcon(icon)
            self.info.setWindowIcon(icon)
        self.config.setWindowTitle("ME7 Configuration")
        self.info.setWindowTitle("ME7 Information")

        # The main GUI now contains a QTextEdit named LOG.
        # We use it directly, so the log size and position are controlled only by Qt Designer.
        # This prevents the log window from covering feature checkboxes or buttons.
        self.log_box = self.main.findChild(QTextEdit, "LOG")

        if self.log_box is None:
            # Backward-compatible fallback for older UI files that used a QWidget placeholder named LOG.
            old = self.main.findChild(object, "LOG")
            self.log_box = QTextEdit(self.main)
            self.log_box.setObjectName("LOG_TEXT")
            if old is not None:
                self.log_box.setGeometry(old.geometry())
                old.hide()
                old.setParent(None)
            else:
                self.log_box.setGeometry(10, 75, 390, 165)

        self.log_box.setReadOnly(True)
        self.log_box.setLineWrapMode(QTextEdit.NoWrap)
        self.log_box.setStyleSheet("QTextEdit { font-family: 'DejaVu Sans Mono', 'Consolas', monospace; font-size: 9pt; }")
        self.log_box.clear()
        self.log_box.raise_()

        # Domyślne wartości configu.
        for box_name, items in (("comboBox", LC_ACTIVATORS), ("comboBox_2", ROLLING_ACTIVATORS)):
            box = getattr(self.config, box_name, None)
            if box:
                box.clear()
                for label, value in items:
                    box.addItem(label, value)

        self._set_config_enabled(False)

    def _connect_signals(self):
        self.main.pushButton.clicked.connect(self.select_file)
        self.main.pushButton_2.clicked.connect(self.close_all)
        self.main.pushButton_3.clicked.connect(self.patch_selected)
        self.main.pushButton_4.clicked.connect(self.show_info)

        # CONFIG button can be named pushButton_5 in the new UI.
        # If the object name changes in Qt Designer, fall back to finding a button
        # whose visible text is "Config".
        config_btn = getattr(self.main, "pushButton_5", None)
        if config_btn is None:
            for child in self.main.findChildren(object):
                try:
                    if callable(getattr(child, "text", None)) and child.text().strip().lower() == "config":
                        config_btn = child
                        break
                except Exception:
                    pass
        if config_btn is not None:
            config_btn.clicked.connect(self.open_config)

        self.config.pushButton_2.clicked.connect(self.config.hide)
        self.config.pushButton_3.clicked.connect(self.save_config_or_checksum)
        self.info.pushButton_2.clicked.connect(self.info.hide)
        pops_box = getattr(self.main, "checkBox_2", None)
        if pops_box is not None:
            # Use toggled(bool), which is stable across PySide6 versions.
            # The profile dialog appears immediately after Pops & Bangs is enabled.
            pops_box.toggled.connect(self._pops_checkbox_toggled)

        self.main.destroyed.connect(self.cleanup_temp)
        QApplication.instance().aboutToQuit.connect(self.cleanup_temp)

    def _log_line_width(self) -> int:
        """Return the number of monospace characters that fit inside the LOG widget."""
        try:
            fm = self.log_box.fontMetrics()
            char_width = max(1, fm.horizontalAdvance("M"))
            viewport_width = max(1, self.log_box.viewport().width())
            return max(40, min(120, int(viewport_width / char_width) - 2))
        except Exception:
            return 78

    def _redraw_log(self):
        current_name = self.current_bin.name if self.current_bin else ""
        output_name = str(self.output_ready.name) if self.output_ready else ""
        try:
            selected = dict(self.selected_features)
            try:
                if getattr(self.config, "comboBox", None):
                    selected["launch_activation"] = self.config.comboBox.currentText().strip()
                if getattr(self.config, "comboBox_2", None):
                    selected["rolling_trigger_text"] = self.config.comboBox_2.currentText().strip()
            except Exception:
                pass
            pretty = _build_professional_log(
                self.full_log,
                current_name=current_name,
                output_name=output_name,
                selected=selected,
                line_width=self._log_line_width(),
            )
        except Exception:
            pretty = "\n".join(str(x) for x in self.full_log[-200:])
        self.log_box.setPlainText(pretty)
        self.log_box.ensureCursorVisible()

    def _append_log(self, text: str):
        if not text:
            return
        self.full_log.append(str(text))
        self._redraw_log()

    def _append_logs(self, logs):
        self.full_log.extend(str(line) for line in (logs or []) if line is not None)
        self._redraw_log()

    def open_config(self):
        self._set_config_enabled(True)
        self.config.show()
        self.config.raise_()

    def _set_busy(self, busy: bool):
        for w in [self.main.pushButton, self.main.pushButton_3, self.main.pushButton_4, self.config.pushButton_3]:
            if w:
                w.setEnabled(not busy)
        if hasattr(self.main, "statusbar") and self.main.statusbar:
            if busy:
                self.main.statusbar.showMessage("Working...")
            else:
                self.main.statusbar.showMessage("Ready")

    def _set_config_enabled(self, enabled: bool):
        for name in [
            "SpeedThreshold", "LaunchRPM", "IgnitionCutDuration", "RPMThreshold",
            "lineEdit_5", "comboBox", "comboBox_2", "lineEdit", "lineEdit_2",
        ]:
            w = getattr(self.config, name, None)
            if w:
                w.setEnabled(enabled)

    def _run(self, title: str, fn, *args, callback=None, **kwargs):
        self.full_log.append(f"===== {title} =====")
        self._redraw_log()
        self._set_busy(True)
        self.worker = Worker(fn, *args, **kwargs)
        self.worker.done.connect(lambda result: self._worker_done(result, callback))
        self.worker.failed.connect(self._worker_failed)
        self.worker.start()

    def _worker_done(self, result, callback):
        self._set_busy(False)
        if callback:
            callback(result)

    def _worker_failed(self, err: str):
        self._set_busy(False)
        self._append_log(err)
        QMessageBox.critical(self.main, "Błąd", err[-2000:])

    def select_file(self):
        path, _ = QFileDialog.getOpenFileName(self.main, "Select BIN file", str(APP_DIR), "BIN (*.bin)")
        if not path:
            return
        src = Path(path)
        if src.suffix.lower() != ".bin":
            QMessageBox.warning(self.main, "BIN", "Select a .bin file.")
            return

        self.cleanup_temp()
        self.job_dir = new_job_dir()
        self.current_bin = self.job_dir / src.name.replace(" ", "_")
        shutil.copy2(src, self.current_bin)
        self.output_ready = None
        self.pending_checksum = False
        self.selected_features = {"launch": False, "rolling": False, "pops": False}
        self.detected_features = {"launch": False, "rolling": False, "pops": False}
        self.detected_rolling_trigger: Optional[str] = None
        self.pops_profile = "medium"
        self._pops_dialog_active = False
        self.full_log.clear()
        self.log_box.clear()
        self.full_log.append(f"Input file selected: {src}")
        self._redraw_log()
        self._run("ANALIZA", analyze_file, self.current_bin, self.job_dir, callback=self._after_analyze)

    def _analysis_has_feature(self, name: str) -> bool:
        """Prawdziwe wykrywanie funkcji z wyniku analyze_file(), nie z tekstu logu."""
        a = self.analysis or {}
        if name == "launch":
            patch = a.get("patch") or {}
            return bool(patch.get("installed"))
        if name == "rolling":
            rolling = a.get("rolling") or {}
            return bool(rolling.get("installed"))
        if name == "pops":
            pops = a.get("pops") or a.get("pops_bangs") or {}
            return bool(pops.get("installed") or pops.get("detected"))
        return False

    def _refresh_detected_features(self):
        self.detected_features = {
            "launch": self._analysis_has_feature("launch"),
            "rolling": self._analysis_has_feature("rolling"),
            "pops": self._analysis_has_feature("pops"),
        }

    def _latest_ecu_file(self) -> Optional[Path]:
        if not self.job_dir or not self.job_dir.exists():
            return None
        candidates = []
        if self.current_bin:
            candidates.extend(self.job_dir.glob(f"{self.current_bin.stem}*.ecu"))
        candidates.extend(self.job_dir.glob("*.ecu"))
        candidates = [p for p in candidates if p.exists()]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _parse_ecu_inputs_for_gui(self) -> dict:
        """Read relevant input bits from the generated .ecu file.

        Cruise MAIN is intentionally not shown in the main log, as requested.
        """
        ecu = self._latest_ecu_file()
        if not ecu:
            return {}
        aliases = {
            "Clutch Switch": ["b_kuppl"],
            "Brake Switch": ["b_br", "b_brems"],
            "Cruise SET": ["b_fgrsec", "s_fgrsv", "b_fgrtdc"],
            "Cruise RES": ["b_fgrwac", "b_fgrtuc", "s_fgrwb"],
        }
        found = {}
        try:
            for raw in ecu.read_text(encoding="latin-1", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith((";", "#", "/", "[")) or "," not in line:
                    continue
                parts = [x.strip() for x in line.split(",")]
                if len(parts) < 5:
                    continue
                name = parts[0].strip().lower()
                try:
                    addr = int(parts[2], 16) if parts[2].lower().startswith("0x") else int(parts[2])
                    mask = int(parts[4], 16) if parts[4].lower().startswith("0x") else int(parts[4])
                except Exception:
                    continue
                if mask <= 0:
                    continue
                bit = mask.bit_length() - 1
                if not (0 <= bit <= 15):
                    continue
                for label, names in aliases.items():
                    if label not in found and name in names:
                        found[label] = {"name": name, "address": addr, "bit": bit}
        except Exception:
            return {}
        return found

    def _detect_rolling_trigger_from_bin(self, rolling: dict, inputs: dict) -> Optional[str]:
        """Decode the Rolling trigger from the first four bytes of Rolling code.

        rolling_chain.py writes the activation check at the start of the code cave:
            9A <fdxx_offset_byte> 12 <bit_index << 4>
        This function compares those two encoded bytes against all known trigger
        aliases from the generated .ecu file. No default SET fallback is used.
        """
        self.detected_rolling_code = None
        if not self.current_bin or not self.current_bin.exists() or not rolling.get("installed"):
            return None
        try:
            data = self.current_bin.read_bytes()
        except Exception:
            return None

        def read_header(code_off: int):
            try:
                code_off = int(code_off)
            except Exception:
                return None
            if code_off < 0 or code_off + 6 >= len(data):
                return None
            if data[code_off] == 0x9A and data[code_off + 2] == 0x12 and data[code_off + 4] == 0xC2 and data[code_off + 5] == 0xF4:
                return data[code_off + 1], data[code_off + 3] & 0xF0, data[code_off:code_off + 4].hex(" ").upper()
            return None

        def expected_encodings() -> dict:
            out = {}
            ecu = self._latest_ecu_file()
            symbols = _parse_ecu_symbols_for_rolling(ecu) if ecu else {}
            for key, aliases in ROLLING_TRIGGER_ALIASES.items():
                for sym_name in aliases:
                    sym = symbols.get(sym_name.lower())
                    if not sym:
                        continue
                    try:
                        addr = int(sym["address"])
                        bit = int(sym["bit"])
                        low = addr & 0xFFFF
                        if not (0xFD00 <= low <= 0xFDFF):
                            continue
                        off_byte = (low - 0xFD00) // 2
                        mask_byte = (bit & 0x0F) << 4
                        out[(off_byte & 0xFF, mask_byte & 0xF0)] = (key, sym_name, addr, bit)
                    except Exception:
                        continue
            # Also add visible input detections as fallback.
            label_to_key = {
                "Brake Switch": "brake",
                "Clutch Switch": "clutch",
                "Cruise SET": "cruise_set",
                "Cruise RES": "cruise_res",
            }
            for label, item in (inputs or {}).items():
                key = label_to_key.get(label)
                if not key:
                    continue
                try:
                    addr = int(item["address"])
                    bit = int(item["bit"])
                    low = addr & 0xFFFF
                    if 0xFD00 <= low <= 0xFDFF:
                        out[((low - 0xFD00) // 2, (bit & 0x0F) << 4)] = (key, str(item.get("name", label)), addr, bit)
                except Exception:
                    continue
            return out

        enc = expected_encodings()
        candidates: list[tuple[str, int]] = []
        seen = set()

        def add_candidate(source: str, pos):
            try:
                pos = int(pos)
            except Exception:
                return
            if pos in seen:
                return
            if read_header(pos):
                candidates.append((source, pos))
                seen.add(pos)

        # 1) analyzer-provided code cave first
        for k in ("code", "code_cave", "function_offset", "offset"):
            if rolling.get(k) is not None:
                add_candidate(k, rolling.get(k))

        # 2) locate by embedded vars reference: C2 F9 <vars_low_word>, header normally starts 12 bytes before C2
        vars_addr = rolling.get("vars")
        try:
            vars_le = (int(vars_addr) & 0xFFFF).to_bytes(2, "little") if vars_addr is not None else None
        except Exception:
            vars_le = None
        if vars_le:
            needle = b"\xC2\xF9" + vars_le
            pos = data.find(needle)
            while pos >= 0:
                for off in (pos - 12, pos - 14, pos - 10):
                    add_candidate("vars", off)
                pos = data.find(needle, pos + 1)

        # 3) generic scan last
        if not candidates:
            for i in range(0, max(0, len(data) - 16)):
                if data[i] == 0x9A and data[i + 2] == 0x12 and data[i + 4] == 0xC2 and data[i + 5] == 0xF4:
                    add_candidate("scan", i)

        for source, code_off in candidates:
            header = read_header(code_off)
            if not header:
                continue
            off_byte, mask_byte, raw4 = header
            match = enc.get((off_byte, mask_byte))
            if match:
                key, sym_name, addr, bit = match
                self.detected_rolling_code = int(code_off)
                self.full_log.append(
                    f"GUI_ROLLING_DECODE source={source} code=0x{int(code_off):X} "
                    f"trigger={key} symbol={sym_name} addr=0x{addr:06X}.{bit} raw={raw4}"
                )
                return key
            else:
                self.full_log.append(
                    f"GUI_ROLLING_DECODE source={source} code=0x{int(code_off):X} "
                    f"trigger=unknown raw={raw4} off_byte=0x{off_byte:02X} mask=0x{mask_byte:02X}"
                )
        return None

    def _detect_launch_activator_from_bin(self, patch: dict) -> Optional[str]:
        """Decode Launch activation from the installed Launch code bytes.

        This mirrors me7_core.detect_current_activator but keeps the GUI independent
        from default combo values. Launch stores the selected input at relative
        offsets +1 and +3 in the Launch function payload.
        """
        if not self.current_bin or not self.current_bin.exists():
            return None
        try:
            function_offset = patch.get("function_offset")
            if function_offset is None:
                return None
            function_offset = int(function_offset)
            data = self.current_bin.read_bytes()
            if function_offset + 4 >= len(data):
                return None
            b = data[function_offset + 1]
            m = data[function_offset + 3]
            ecu = self._latest_ecu_file()
            if not ecu:
                return None
            switches = parse_ecu_file(ecu)
            for key, sw in switches.items():
                try:
                    if b == int(sw.patch_bit_address_byte) and m == int(sw.patch_bitmask_byte):
                        if "kuppl" in key.lower():
                            return "B_kuppl"
                        if "br" in key.lower() or "brems" in key.lower():
                            return "B_brems"
                        return key
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _append_analysis_markers(self):
        """Dodaje stabilne markery dla renderera logu.

        Okno konfiguracji już bazuje na analyze_file(); te markery sprawiają,
        że profesjonalny log pokazuje ten sam stan co CONFIG.
        """
        a = self.analysis or {}
        patch = a.get("patch") or {}
        rolling = a.get("rolling") or {}
        cfg = a.get("config") or {}
        rolling_cfg = a.get("rolling_config") or {}

        markers = ["===== GUI ANALYSIS SNAPSHOT ====="]
        inputs = self._parse_ecu_inputs_for_gui()
        self.detected_rolling_trigger = self._detect_rolling_trigger_from_bin(rolling, inputs)
        if self.detected_rolling_trigger:
            self.selected_features["rolling_trigger"] = self.detected_rolling_trigger
            self._set_rolling_trigger_combo(self.detected_rolling_trigger)
        for label, item in inputs.items():
            markers.append(f"GUI_INPUT label={label} addr=0x{int(item['address']):06X}.{int(item['bit'])}")
        markers.append(f"GUI_DETECT launch_installed={int(bool(patch.get('installed')))}")
        markers.append(f"GUI_DETECT rolling_installed={int(bool(rolling.get('installed')))}")
        launch_act = self._detect_launch_activator_from_bin(patch) or a.get("current_activator")
        if launch_act and str(launch_act).lower() != "unknown":
            launch_label = "Brake" if ("br" in str(launch_act).lower() or "brems" in str(launch_act).lower()) else "Clutch"
            markers.append(f"GUI_LAUNCH_ACTIVATION key={launch_act} label={launch_label}")
        if patch.get("hook_offset") is not None:
            markers.append(f"GUI_LAUNCH hook=0x{int(patch.get('hook_offset')):X}")
        if patch.get("function_offset") is not None:
            markers.append(f"GUI_LAUNCH code=0x{int(patch.get('function_offset')):X}")
        if patch.get("config_base") is not None:
            markers.append(f"GUI_LAUNCH config=0x{int(patch.get('config_base')):X}")
        rolling_code_for_log = rolling.get("code")
        if rolling_code_for_log is None:
            rolling_code_for_log = rolling.get("code_cave")
        if rolling_code_for_log is None and getattr(self, "detected_rolling_code", None) is not None:
            rolling_code_for_log = self.detected_rolling_code
        if rolling_code_for_log is not None:
            markers.append(f"GUI_ROLLING code=0x{int(rolling_code_for_log):X}")
        if rolling.get("vars") is not None:
            markers.append(f"GUI_ROLLING vars=0x{int(rolling.get('vars')):X}")
        if rolling.get("installed"):
            markers.append("GUI_ROLLING mode=CHAIN")
            trig = self.detected_rolling_trigger or self.selected_features.get("rolling_trigger")
            if trig:
                markers.append(f"GUI_ROLLING trigger={self._rolling_trigger_label(trig)}")
            else:
                markers.append("GUI_ROLLING trigger=Unknown")
        try:
            if self.current_bin and self.current_bin.exists():
                ft = detect_ftomn_in_bin(self.current_bin.read_bytes())
                if ft.get("found"):
                    markers.append(
                        f"GUI_FTOMN addr=0x{int(ft['address']):X} "
                        f"value=0x{int(ft['value']):02X} "
                        f"count={int(ft.get('count', 1))} "
                        f"method={ft.get('method', '-')} "
                    )
                else:
                    markers.append("GUI_FTOMN not_found=1")
        except Exception as e:
            markers.append(f"GUI_FTOMN error={type(e).__name__}")
        for k, v in cfg.items():
            markers.append(f"GUI_CONFIG {k}={v}")
        for k, v in rolling_cfg.items():
            markers.append(f"GUI_ROLLING_CONFIG {k}={v}")
        markers.append("===== END GUI ANALYSIS SNAPSHOT =====")
        self._append_logs(markers)

    def _after_analyze(self, result: dict):
        self.analysis = result or {}
        self._append_logs(self.analysis.get("log", []))
        self._refresh_detected_features()
        self._append_analysis_markers()
        self._fill_config(self.analysis)
        has_config = bool(
            self.analysis.get("patch", {}).get("installed")
            or self.analysis.get("rolling", {}).get("installed")
        )
        self._set_config_enabled(has_config)
        if has_config:
            self.config.show()
            self.config.raise_()
        else:
            self.config.hide()

    def _collect_values(self) -> dict:
        fields = {
            "SpeedThreshold": self.config.SpeedThreshold,
            "LaunchRPM": self.config.LaunchRPM,
            "IgnitionCutDuration": self.config.IgnitionCutDuration,
            "RPMThreshold": self.config.RPMThreshold,
            "AccPedalThreshold": self.config.lineEdit_5,
        }
        values = {}
        for key, widget in fields.items():
            txt = widget.text().strip().replace(",", ".")
            if txt:
                values[key] = float(txt)
        return values

    def _collect_rolling_values(self) -> dict:
        values = {}
        rpm = self.config.lineEdit.text().strip().replace(",", ".")
        pedal = self.config.lineEdit_2.text().strip().replace(",", ".")
        if rpm:
            values["RollingRPM"] = float(rpm)
        if pedal:
            values["RollingPedalPercent"] = float(pedal)
        return values

    def _fill_config(self, result: dict):
        cfg = result.get("config") or {}
        rolling_cfg = result.get("rolling_config") or {}
        mapping = {
            "SpeedThreshold": self.config.SpeedThreshold,
            "LaunchRPM": self.config.LaunchRPM,
            "IgnitionCutDuration": self.config.IgnitionCutDuration,
            "RPMThreshold": self.config.RPMThreshold,
            "AccPedalThreshold": self.config.lineEdit_5,
        }
        for key, widget in mapping.items():
            if key in cfg:
                widget.setText(str(round(float(cfg[key]), 3)))
        if "RollingRPM" in rolling_cfg:
            self.config.lineEdit.setText(str(rolling_cfg["RollingRPM"]))
        if "RollingPedalPercent" in rolling_cfg:
            self.config.lineEdit_2.setText(str(rolling_cfg["RollingPedalPercent"]))

        current = self._detect_launch_activator_from_bin(result.get("patch") or {}) or result.get("current_activator")
        if current and str(current).lower() != "unknown":
            idx = self.config.comboBox.findData(current)
            if idx >= 0:
                self.config.comboBox.setCurrentIndex(idx)

        # Keep Rolling trigger combo synchronized with the value selected by the user.
        # This avoids the GUI returning to Cruise SET after analysis when Brake/RES was selected.
        current_rolling_trigger = self.detected_rolling_trigger or result.get("current_rolling_trigger") or result.get("rolling_trigger")
        if current_rolling_trigger:
            self._set_rolling_trigger_combo(str(current_rolling_trigger))
            self.selected_features["rolling_trigger"] = str(current_rolling_trigger)
        elif self.selected_features.get("rolling_trigger"):
            self._set_rolling_trigger_combo(str(self.selected_features.get("rolling_trigger")))

    def _normalize_rolling_trigger_value(self, value) -> str:
        """Return rolling_chain.py trigger key from combo data/text/symbol.

        This fixes cases where QComboBox.currentData() is empty after UI changes.
        We always fall back to currentText() and normalize visible labels like
        "Cruise RES" to the exact CLI value "cruise_res".
        """
        raw = str(value or "").strip().lower().replace("-", " ").replace("_", " ")
        raw = " ".join(raw.split())
        aliases = {
            "cruise set": "cruise_set",
            "cruise control set": "cruise_set",
            "set": "cruise_set",
            "b fgrsec": "cruise_set",
            "s fgrsv": "cruise_set",
            "b fgrtdc": "cruise_set",
            "cruise res": "cruise_res",
            "cruise control res": "cruise_res",
            "res": "cruise_res",
            "b fgrwac": "cruise_res",
            "b fgrtuc": "cruise_res",
            "s fgrwb": "cruise_res",
            "cruise main": "cruise_main",
            "cruise control main": "cruise_main",
            "main": "cruise_main",
            "b fgrhsc": "cruise_main",
            "s fgrhs": "cruise_main",
            "brake": "brake",
            "hamulec": "brake",
            "b br": "brake",
            "b brems": "brake",
            "clutch": "clutch",
            "sprzeglo": "clutch",
            "sprzęgło": "clutch",
            "b kuppl": "clutch",
        }
        if raw in aliases:
            return aliases[raw]
        raw2 = str(value or "").strip().lower()
        if raw2 in {"cruise_set", "cruise_res", "cruise_main", "brake", "clutch"}:
            return raw2
        return "cruise_set"

    def _get_selected_rolling_trigger(self) -> str:
        box = getattr(self.config, "comboBox_2", None)
        if not box:
            return "cruise_set"

        # IMPORTANT:
        # The visible text is the source of truth here. In some .ui versions
        # QComboBox.currentData() can still contain the old/default value
        # (usually cruise_set) even when the user selected "Cruise Control RES".
        # Therefore we normalize currentText() first and only use currentData()
        # as a fallback when the visible text cannot be interpreted.
        text = box.currentText()
        text_key = self._normalize_rolling_trigger_value(text)
        if text_key in {"cruise_set", "cruise_res", "cruise_main", "brake", "clutch"}:
            return text_key

        data = box.currentData()
        data_key = self._normalize_rolling_trigger_value(data)
        if data_key in {"cruise_set", "cruise_res", "cruise_main", "brake", "clutch"}:
            return data_key

        return "cruise_set"

    def _pops_checkbox_toggled(self, checked: bool):
        """Ask for the Pops & Bangs power immediately after enabling it."""
        if self._pops_dialog_active or not checked:
            return

        self._pops_dialog_active = True
        try:
            labels = ["Low", "Medium", "High"]
            current = {"low": 0, "medium": 1, "high": 2}.get(self.pops_profile, 1)
            choice, ok = QInputDialog.getItem(
                self.main,
                "Pops & Bangs power",
                "Select Pops & Bangs power profile:",
                labels,
                current,
                False,
            )

            if not ok or not choice:
                # Cancelling means Pops & Bangs was not selected.
                self.main.checkBox_2.blockSignals(True)
                self.main.checkBox_2.setChecked(False)
                self.main.checkBox_2.blockSignals(False)
                self.selected_features["pops"] = False
                return

            self.pops_profile = choice.strip().lower()
            self.selected_features["pops"] = True
            self.selected_features["pops_profile"] = self.pops_profile
            self._append_log(f"Pops & Bangs profile selected: {choice.upper()}")
        finally:
            self._pops_dialog_active = False

    def patch_selected(self):
        if not self.current_bin or not self.job_dir:
            QMessageBox.information(self.main, "File", "Select a BIN file first.")
            return

        launch = bool(getattr(self.main, "checkBox_5", None) and self.main.checkBox_5.isChecked())
        rolling = bool(getattr(self.main, "checkBox", None) and self.main.checkBox.isChecked())
        pops = bool(getattr(self.main, "checkBox_2", None) and self.main.checkBox_2.isChecked())
        checksum_only = bool(getattr(self.main, "checkBox_4", None) and self.main.checkBox_4.isChecked())

        self.selected_features.update({
            "launch": launch,
            "rolling": rolling,
            "pops": pops,
            "pops_profile": self.pops_profile,
        })
        self._redraw_log()

        if checksum_only and not any([launch, rolling, pops]):
            self.save_config_or_checksum()
            return

        already = []
        if launch and self.detected_features.get("launch"):
            already.append("Launch Control")
        if rolling and self.detected_features.get("rolling"):
            already.append("Rolling Anti-Lag")
        if pops and self.detected_features.get("pops"):
            already.append("Pops & Bangs")
        if already:
            msg = "These features are already installed and will not be patched again:\n\n" + "\n".join("- " + x for x in already)
            self._append_log("Patch blocked: already installed -> " + ", ".join(already))
            QMessageBox.warning(self.main, "Patch blocked", msg)
            return

        if not any([launch, rolling, pops]):
            QMessageBox.information(self.main, "Patch", "Select Launch Control, Rolling Anti-Lag or Pops & Bangs.")
            return

        activator = self.config.comboBox.currentData() or "B_kuppl"
        rolling_trigger = self._get_selected_rolling_trigger()
        self.selected_features["rolling_trigger"] = rolling_trigger
        try:
            box = getattr(self.config, "comboBox_2", None)
            if box is not None:
                self.full_log.append(
                    f"GUI_ROLLING_COMBO index={box.currentIndex()} "
                    f"text={box.currentText()} data={box.currentData()} normalized={rolling_trigger}"
                )
        except Exception:
            pass
        self._append_log(f"GUI_ROLLING trigger={self._rolling_trigger_label(rolling_trigger)}")

        self._run(
            "PATCH",
            patch_pipeline_with_final_rolling_trigger,
            self.current_bin,
            self.job_dir,
            launch=launch,
            rolling=rolling,
            pops=pops,
            pops_profile=self.pops_profile,
            values=self._collect_values(),
            activator=activator,
            rolling_trigger=rolling_trigger,
            rolling_config=self._collect_rolling_values(),
            callback=self._after_patch,
        )

    def _after_patch(self, result: dict):
        self._append_logs(result.get("log", []))
        raw = result.get("raw")
        if raw:
            self.current_bin = self.job_dir / raw
            self.pending_checksum = True
            self._run("ANALIZA PO PATCHU", analyze_file, self.current_bin, self.job_dir, callback=self._after_analyze)
        QMessageBox.information(self.main, "Patch", "Patch installation completed. Click Save to write configuration and calculate checksum.")

    def save_config_or_checksum(self):
        if not self.current_bin or not self.job_dir:
            QMessageBox.information(self.main, "File", "Select a BIN file first.")
            return

        has_config = bool(
            self.analysis.get("patch", {}).get("installed")
            or self.analysis.get("rolling", {}).get("installed")
        )
        self.selected_features["rolling_trigger"] = self._get_selected_rolling_trigger()
        self._redraw_log()

        self._run(
            "FINAL SAVE",
            save_full_pipeline,
            self.current_bin,
            self.job_dir,
            has_config=has_config,
            values=self._collect_values(),
            activator=self.config.comboBox.currentData() or "B_kuppl",
            rolling_values=self._collect_rolling_values(),
            rolling_trigger=self._get_selected_rolling_trigger(),
            callback=self._after_save_checksum,
        )

    def _after_save_checksum(self, result: dict):
        self._append_logs(result.get("log", []))
        out_name = result.get("output")
        if not out_name:
            QMessageBox.warning(self.main, "Save", "Checksum output file was not created. Check detailed log.")
            return
        out_path = self.job_dir / out_name
        save_to, _ = QFileDialog.getSaveFileName(self.main, "Save final BIN", str(APP_DIR / out_name), "BIN (*.bin)")
        if save_to:
            final_path = Path(save_to)
            shutil.copy2(out_path, final_path)

            self.output_ready = final_path
            self.current_bin = out_path
            self.pending_checksum = False
            self._redraw_log()

            QMessageBox.information(
                self.main,
                "Save",
                f"Final file saved:\n{final_path}\n\nTemporary files will be removed when the application is closed.",
            )
            self._run("FINAL ANALYSIS", analyze_file, self.current_bin, self.job_dir, callback=self._after_analyze)


    def _unique_output_dir(self, base_dir: Path, stem: str) -> Path:
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or "me7_output"
        candidate = base_dir / safe_stem
        if not candidate.exists():
            return candidate
        idx = 2
        while True:
            candidate = base_dir / f"{safe_stem}_{idx:02d}"
            if not candidate.exists():
                return candidate
            idx += 1

    def _write_result_log(self, result_dir: Path, final_path: Path) -> Path:
        selected = dict(self.selected_features)
        try:
            if getattr(self.config, "comboBox", None):
                selected["launch_activation"] = self.config.comboBox.currentText().strip()
            if getattr(self.config, "comboBox_2", None):
                selected["rolling_trigger_text"] = self.config.comboBox_2.currentText().strip()
        except Exception:
            pass
        pretty = _build_professional_log(
            self.full_log,
            current_name=self.current_bin.name if self.current_bin else "",
            output_name=final_path.name,
            selected=selected,
            line_width=100,
        )
        raw = "\n".join(str(x) for x in self.full_log)
        log_path = result_dir / f"{final_path.stem}_full_log.txt"
        log_path.write_text(
            pretty
            + "\n\n\n"
            + "=" * 100
            + "\nRAW DIAGNOSTIC LOG\n"
            + "=" * 100
            + "\n"
            + raw,
            encoding="utf-8",
            errors="replace",
        )
        return log_path

    def _rolling_trigger_label(self, value: str) -> str:
        labels = {
            "cruise_set": "Cruise SET",
            "cruise_res": "Cruise RES",
            "cruise_main": "Cruise MAIN",
            "brake": "Brake",
            "clutch": "Clutch",
        }
        return labels.get(str(value or "").strip().lower(), str(value or "Cruise SET"))

    def _set_rolling_trigger_combo(self, value: str) -> None:
        box = getattr(self.config, "comboBox_2", None)
        if not box:
            return
        raw = str(value or "").strip()
        aliases = {
            "cruise set": "cruise_set",
            "cruise control set": "cruise_set",
            "set": "cruise_set",
            "b_fgrsec": "cruise_set",
            "cruise res": "cruise_res",
            "cruise control res": "cruise_res",
            "res": "cruise_res",
            "b_fgrwac": "cruise_res",
            "cruise main": "cruise_main",
            "cruise control main": "cruise_main",
            "main": "cruise_main",
            "b_fgrhsc": "cruise_main",
            "brake": "brake",
            "hamulec": "brake",
            "b_br": "brake",
        }
        key = aliases.get(raw.lower(), None)
        if key is None:
            key = self._normalize_rolling_trigger_value(raw)
        idx = box.findData(key)
        if idx < 0:
            for i in range(box.count()):
                if box.itemText(i).strip().lower() == raw.lower():
                    idx = i
                    break
        if idx >= 0:
            box.setCurrentIndex(idx)

    def show_info(self):
        # The INFO window is reserved only for the program description written in the UI.
        # Do not inject logs, file paths or runtime status here.
        self.info.show()
        self.info.raise_()

    def close_all(self):
        self.cleanup_temp()
        _cleanup_frozen_runtime()
        QApplication.quit()

    def cleanup_temp(self):
        # Usuwa tylko katalogi robocze tworzone przez program w work/.
        if self.job_dir and self.job_dir.exists():
            try:
                shutil.rmtree(self.job_dir, ignore_errors=True)
            except Exception:
                pass
        self.job_dir = None
        try:
            WORK_DIR.mkdir(exist_ok=True)
            for p in WORK_DIR.iterdir():
                if p.is_dir() and len(p.name) == 12:
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


def main():
    app = QApplication(sys.argv)
    controller = DesktopController()
    controller.main.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
