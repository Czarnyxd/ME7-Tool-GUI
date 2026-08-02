#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
ME7_CORE_BUILD = "ROLLING_EXE_ONLY_V4_2026-08-02"

import dataclasses
import math
import platform
import re
import shutil
import subprocess
import time
import uuid
import os
from pathlib import Path
from typing import Optional, Dict, Tuple, List


BUNDLE_DIR = Path(__file__).resolve().parent
APP_DIR = Path(os.environ.get("ME7_RUNTIME_DIR", str(BUNDLE_DIR))).resolve()
TOOLS_DIR = APP_DIR / "tools"
ECUS_DIR = APP_DIR / "ecus"
TOOLS_ECUS_DIR = TOOLS_DIR / "ecus"
WORK_DIR = APP_DIR / "work"

ME7INFO_NAMES = ["ME7Info.exe", "me7info.exe"]
ME7CHECK_NAMES = ["ME7Check.exe", "me7check.exe"]
ME7SUM_NAMES = ["me7sum.exe", "ME7Sum.exe"]
LAUNCH_NAMES = ["launch.exe"]
ROLLING_NAMES = ["rolling_chain.exe"]
POPS_NAMES = ["PopsAndBangs_CMD.exe"]

HOOK_NEAR_PATTERN = bytes.fromhex("D7 40 06 02 03 F8")
DEFAULT_CONFIG_BLOCK = bytes.fromhex("A6 01 50 46 0A 00 F0 55 E6")
FUNCTION_LEN = 9 * 16

COND_OFFSETS = {
    "SpeedThreshold": 14,
    "LaunchRPM": 30,
    "RPMThreshold": 60,
    "AccPedalThreshold": 76,
    "IgnitionCutDuration": 96,
}

CLUTCH_PATCH_POINTS = [(1, 3), (43, 45)]

ALS_MAPS = [
    ("SpeedThreshold", "km/h", 0x00, 2, 0.0078125, 0, 80),
    ("LaunchRPM", "rpm", 0x02, 2, 0.25, 1000, 9000),
    ("IgnitionCutDuration", "ms", 0x04, 2, 20.0, 0, 2000),
    ("RPMThreshold", "rpm", 0x06, 2, 0.25, 1000, 9000),
    ("AccPedalThreshold", "%", 0x08, 1, 0.392157, 0, 100),
]


ROLLING_DEFAULT_BYTES = bytes([0xE6, 0xFF, 0xB0, 0x36, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
ROLLING_VARS_LEN = 9


def _read_s16_le(data: bytearray, off: int) -> int:
    return int.from_bytes(data[off:off+2], "little", signed=True)


def _write_s16_le(data: bytearray, off: int, value: int) -> None:
    if not -32768 <= int(value) <= 32767:
        raise ValueError(f"Wartość signed16 poza zakresem: {value}")
    data[off:off+2] = int(value).to_bytes(2, "little", signed=True)


def read_rolling_config(data: bytearray, vars_base: Optional[int]) -> Dict[str, object]:
    """
    Odczyt konfiguracji Rolling zgodny z rolling_chain.exe.

    rolling_chain.exe zapisuje 9 bajtów defaultów pod Rolling vars:
      [E6 FF] [B0 36] [FF FF FF FF FF]
    Kod Rolling realnie używa tylko dwóch słów:
      vars_addr + 0 -> próg WPED / pedału gazu jako signed RAW,
      vars_addr + 2 -> próg obrotów nmot_w jako RAW * 0.25 rpm.
    Reszta bajtów zostaje pokazana jako RAW HEX, bez zgadywania znaczenia.
    """
    if vars_base is None:
        return {}
    if not (0 <= vars_base <= len(data) - ROLLING_VARS_LEN):
        return {}

    pedal_raw_signed = _read_s16_le(data, vars_base)
    pedal_unsigned = pedal_raw_signed + 256 if pedal_raw_signed < 0 else pedal_raw_signed
    pedal_unsigned = max(0, min(255, pedal_unsigned))
    pedal_percent = round(pedal_unsigned * 100.0 / 255.0, 1)
    rpm_raw = int.from_bytes(data[vars_base+2:vars_base+4], "little")
    return {
        # Pola edytowalne i widoczne w GUI:
        # vars+0: próg WPED/pedału gazu. W GUI pokazujemy %, w BIN zapisujemy format rolling_chain.exe.
        "RollingPedalPercent": pedal_percent,
        # vars+2: nmot_w raw * 0.25 rpm. Domyślnie B0 36 = 14000 raw = 3500 rpm.
        "RollingRPM": round(rpm_raw * 0.25, 3),
    }


def write_rolling_config(data: bytearray, vars_base: Optional[int], values: Optional[dict], log: List[str]) -> bool:
    """Zapisuje tylko te pola Rolling, które są realnie używane przez rolling_chain.exe."""
    if not values:
        return False
    if vars_base is None:
        log.append("Rolling config: brak vars_base — nie można zapisać konfiguracji Rolling.")
        return False
    if not (0 <= vars_base <= len(data) - ROLLING_VARS_LEN):
        log.append(f"Rolling config: vars_base poza zakresem BIN: 0x{vars_base:X}")
        return False

    changed = False
    if "RollingPedalPercent" in values and str(values.get("RollingPedalPercent", "")).strip() != "":
        percent = float(str(values["RollingPedalPercent"]).replace(",", "."))
        if not 0 <= percent <= 100:
            raise ValueError(f"Próg pedału gazu Rolling poza zakresem 0-100%: {percent}")
        raw_unsigned = int(round(percent * 255.0 / 100.0))
        raw_unsigned = max(0, min(255, raw_unsigned))
        # rolling_chain.exe używa signed word. Dla typowych progów >50% zapis jest np. 90% -> 230 -> -26 -> E6 FF.
        raw_signed = raw_unsigned - 256 if raw_unsigned >= 128 else raw_unsigned
        _write_s16_le(data, vars_base, raw_signed)
        log.append(f"Rolling config: próg pedału gazu ustawiony na {percent:g}% (RAW unsigned={raw_unsigned}, signed={raw_signed}) pod 0x{vars_base:X} (vars+0).")
        changed = True

    if "RollingRPM" in values and str(values.get("RollingRPM", "")).strip() != "":
        rpm = float(str(values["RollingRPM"]).replace(",", "."))
        raw = int(round(rpm / 0.25))
        if not 0 <= raw <= 0xFFFF:
            raise ValueError(f"RollingRPM poza zakresem RAW 16-bit: rpm={rpm}, raw={raw}")
        data[vars_base+2:vars_base+4] = raw.to_bytes(2, "little")
        log.append(f"Rolling config: próg obrotów Rolling ustawiony na {rpm:g} rpm, RAW=0x{raw:04X}, adres 0x{vars_base+2:X} (vars+2).")
        changed = True

    if changed:
        raw = bytes(data[vars_base:vars_base+ROLLING_VARS_LEN]).hex(" ").upper()
        log.append(f"Rolling config RAW 0x{vars_base:X}: {raw}")
    return changed


def ranges_overlap(a_start: Optional[int], a_len: int, b_start: Optional[int], b_len: int) -> bool:
    if a_start is None or b_start is None:
        return False
    return a_start < b_start + b_len and b_start < a_start + a_len


@dataclasses.dataclass
class SwitchBit:
    name: str
    address: int
    bit_index: int
    source_line: str

    @property
    def patch_bit_address_byte(self) -> int:
        return (self.address & 0xFF) // 2

    @property
    def patch_bitmask_byte(self) -> int:
        # W kodzie Setzi/launch.exe maska dla instrukcji bitowej jest zapisana
        # w górnej połówce bajtu:
        #   bit .3  -> 0x30
        #   bit .14 -> 0xE0
        #   bit .15 -> 0xF0
        #
        # Używamy dolnych 4 bitów indeksu, bo dla rejestrów bitowych C167
        # interesuje nas wartość 0..15 zakodowana w nibblu.
        return (self.bit_index & 0x0F) << 4

    def pretty(self) -> str:
        return (
            f"{self.name}: 0x{self.address:06X}.{self.bit_index} "
            f"-> addr_byte=0x{self.patch_bit_address_byte:02X}, "
            f"mask=0x{self.patch_bitmask_byte:02X}"
        )


@dataclasses.dataclass
class PatchInfo:
    installed: bool
    function_offset: Optional[int] = None
    hook_offset: Optional[int] = None
    config_base: Optional[int] = None
    reason: str = ""


def new_job_dir() -> Path:
    WORK_DIR.mkdir(exist_ok=True)
    p = WORK_DIR / uuid.uuid4().hex[:12]
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_tool(names: List[str]) -> Optional[Path]:
    for folder in (TOOLS_DIR, APP_DIR):
        for name in names:
            p = folder / name
            if p.exists():
                return p
    return None


def is_linux() -> bool:
    return platform.system().lower() == "linux"


def wine_available() -> bool:
    return Path("/usr/bin/wine").exists()


def build_command(args: List[str]) -> List[str]:
    if args and is_linux() and str(args[0]).lower().endswith(".exe"):
        return ["/usr/bin/wine"] + args
    return args


def cmd_str(args: List[str]) -> str:
    return " ".join(str(x) for x in build_command(args))


def run_cmd(args: List[str], cwd: Path, timeout: int = 180) -> Tuple[int, str]:
    if is_linux() and args and str(args[0]).lower().endswith(".exe"):
        if not Path("/usr/bin/wine").exists():
            return -1, "Brak /usr/bin/wine"

    try:
        proc = subprocess.run(
            build_command(args),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

        out = proc.stdout.decode("latin-1", errors="replace")
        err = proc.stderr.decode("latin-1", errors="replace")

        return proc.returncode, out + err

    except Exception as e:
        return -1, str(e)


def read_bin(path: Path) -> bytearray:
    data = bytearray(path.read_bytes())
    if len(data) not in (512 * 1024, 1024 * 1024):
        raise ValueError(
            f"Nietypowy rozmiar BIN: {len(data)} bajtów. "
            "Oczekiwane zwykle 512KB albo 1024KB."
        )
    return data


def parse_bit_index_from_mask(mask: int) -> Optional[int]:
    if mask <= 0 or mask & (mask - 1):
        return None
    return int(math.log2(mask))


def _ecu_line_symbol(line: str) -> str:
    """
    Zwraca nazwę symbolu z początku linii .ecu.

    Obsługiwane przykłady:
      B_kuppl        , {...}, 0x00FD4C, 2, 0x0008, ...
      b_br           , {...}, 0x00FD4A, 2, 0x8000, ...
      B_brems: 0x00FD4A.15
    """
    s = line.strip()
    if not s:
        return ""

    # Format CSV z ME7Info: symbol jest przed pierwszym przecinkiem.
    left = s.split(",", 1)[0].strip()

    # Format diagnostyczny: "B_brems: 0x00FD4A.15"
    left = left.split(":", 1)[0].strip()

    # Gdyby były dodatkowe spacje/taby po symbolu.
    return re.split(r"\s+", left, maxsplit=1)[0].strip()


def _symbol_equals(line: str, aliases: List[str]) -> bool:
    sym = _ecu_line_symbol(line).lower()
    return any(sym == a.lower() for a in aliases)


def parse_switch_from_ecu_line(line: str, symbol: str) -> Optional[SwitchBit]:
    """
    Parsuje jedną linię .ecu i zwraca adres + bit.

    Ważne:
    - NIE szukamy tutaj luźno po słowie "brems", bo wtedy można złapać zły alias.
    - Funkcja zakłada, że linia została już wybrana jako właściwy symbol.
    - Obsługiwane są oba formaty:
        0x00FD4A.15
        0x00FD4A, ..., 0x8000
    """
    m = re.search(r"(?:0x)?([0-9A-Fa-f]{4,6})\s*\.\s*(\d{1,2})", line)
    if m:
        address = int(m.group(1), 16)
        bit_index = int(m.group(2))
        if 0 <= bit_index <= 15:
            return SwitchBit(symbol, address, bit_index, line.strip())
        return None

    # Typowa linia .ecu z ME7Info zawiera adres oraz później bitmaskę, np.
    # B_br, {...}, 0x00FD4A, 2, 0x8000, ...
    hexes = re.findall(r"0x[0-9A-Fa-f]+", line)
    if hexes:
        address = int(hexes[0], 16)
        bit_index = None

        for hx in hexes[1:]:
            idx = parse_bit_index_from_mask(int(hx, 16))
            if idx is not None and 0 <= idx <= 15:
                bit_index = idx
                break

        if bit_index is not None:
            return SwitchBit(symbol, address, bit_index, line.strip())

    return None


def _find_switch_by_alias_groups(lines: List[str], symbol: str, alias_groups: List[List[str]]) -> Optional[SwitchBit]:
    """
    Szuka symbolu w kilku grupach priorytetów.
    Dla hamulca najpierw MUSI być dokładne b_br, potem B_brems.
    Nie używamy luźnego wyszukiwania "brems", bo to właśnie potrafiło dać .14 zamiast .15.
    """
    for aliases in alias_groups:
        for line in lines:
            if not _symbol_equals(line, aliases):
                continue

            sw = parse_switch_from_ecu_line(line, symbol)
            if sw:
                return sw

    return None


def parse_ecu_file(ecu_path: Path) -> Dict[str, SwitchBit]:
    result: Dict[str, SwitchBit] = {}
    text = ecu_path.read_text(errors="ignore", encoding="latin-1")
    lines = text.splitlines()

    # Sprzęgło: dokładnie B_kuppl.
    clutch = _find_switch_by_alias_groups(
        lines,
        "B_kuppl",
        [
            ["B_kuppl"],
        ],
    )

    if clutch:
        result["B_kuppl"] = clutch

    # Hamulec: najpierw dokładnie b_br, bo tak zgłasza to launch.exe:
    #   finding b_br (brems), brake pedal...
    #   found: 00FD4A.15
    #
    # Dopiero potem inne dokładne aliasy. Bez luźnego "brems".
    brake = _find_switch_by_alias_groups(
        lines,
        "B_brems",
        [
            ["b_br"],
            ["B_brems"],
            ["B_br"],
            ["B_bremse"],
        ],
    )

    if brake:
        result["B_brems"] = brake

    # Dodatkowy fallback: niektóre .ecu mają inne aliasy/format linii.
    # Najpierw próba luźna dla sprzęgła po tekście "kuppl", ale dopiero gdy dokładne B_kuppl nic nie dało.
    if "B_kuppl" not in result:
        for line in lines:
            if "kuppl" in line.lower():
                sw = parse_switch_from_ecu_line(line, "B_kuppl")
                if sw:
                    result["B_kuppl"] = sw
                    break

    # Luźny fallback dla hamulca po "brems"/"brake" tylko gdy dokładne b_br/B_brems nic nie dało.
    if "B_brems" not in result:
        for line in lines:
            low = line.lower()
            if "brems" in low or "brake" in low:
                sw = parse_switch_from_ecu_line(line, "B_brems")
                if sw:
                    result["B_brems"] = sw
                    break

    # Fallback z PDF-a tylko wtedy, gdy w .ecu naprawdę nie znaleziono hamulca.
    if "B_brems" not in result and "B_kuppl" in result and result["B_kuppl"].bit_index >= 2:
        k = result["B_kuppl"]
        result["B_brems"] = SwitchBit(
            "B_brems",
            k.address,
            k.bit_index - 2,
            "Fallback z B_kuppl wg PDF: ten sam adres, bit -2",
        )

    return result



def _me7info_written_path(out: str, bin_path: Path) -> Optional[Path]:
    """Wyciąga dokładną ścieżkę .ecu z tekstu ME7Info: written output to file Z:\\opt\\..."""
    m = re.search(r"written\s+output\s+to\s+file\s+(.+?\.ecu)", out, flags=re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).strip().strip('"').strip()
    # Wine zapisuje ścieżki jako Z:\opt\me7web\tools\ecus\plik.ecu
    raw = raw.replace('\\\\', '/')
    raw = raw.replace('\\', '/')
    if raw.lower().startswith('z:'):
        raw = raw[2:]
    cand = Path(raw)
    if cand.exists():
        return cand
    # fallback po nazwie pliku, gdy ścieżka z Wine nie mapuje się 1:1
    name = Path(raw).name
    for folder in (bin_path.parent, TOOLS_ECUS_DIR, ECUS_DIR, APP_DIR / 'ecus'):
        c = folder / name
        if c.exists():
            return c
    return None


def _copy_ecu_to_job(ecu: Path, bin_path: Path, job_dir: Path, log: List[str]) -> Path:
    """Zawsze używamy świeżego .ecu z job_dir, żeby nie parsować starego/stale .ecu z tools/ecus."""
    job_ecu = job_dir / (bin_path.stem + '.ecu')
    try:
        if ecu.resolve() != job_ecu.resolve():
            shutil.copy2(ecu, job_ecu)
            log.append(f"ECU skopiowany do job_dir: {job_ecu.name}")
        return job_ecu
    except Exception as e:
        log.append(f"Nie udało się skopiować .ecu do job_dir ({e}), używam: {ecu}")
        return ecu


def parse_switches_from_launch_log(job_dir: Path) -> Dict[str, SwitchBit]:
    """Fallback dla GUI: gdy .ecu nie dało B_kuppl/B_brems, bierzemy adresy z launch_output.txt."""
    result: Dict[str, SwitchBit] = {}
    p = job_dir / "launch_output.txt"
    if not p.exists():
        return result
    txt = p.read_text(errors="ignore", encoding="latin-1")
    m = re.search(r"finding\s+B_kuppl.*?found:\s*([0-9A-Fa-f]{4,6})\.(\d{1,2})", txt, flags=re.IGNORECASE|re.S)
    if m:
        result["B_kuppl"] = SwitchBit("B_kuppl", int(m.group(1),16), int(m.group(2)), "Z launch_output.txt")
    m = re.search(r"finding\s+b_br.*?found:\s*([0-9A-Fa-f]{4,6})\.(\d{1,2})", txt, flags=re.IGNORECASE|re.S)
    if m:
        result["B_brems"] = SwitchBit("B_brems", int(m.group(1),16), int(m.group(2)), "Z launch_output.txt")
    return result

def generate_ecu(bin_path: Path, job_dir: Path, log: List[str]) -> Optional[Path]:
    """Generate a fresh ECU definition for the current BIN.

    ME7Info behaves differently between native Windows and Wine builds, so this
    function uses a local BIN copy, several safe working directories and detects
    the ECU file by both the program output and filesystem changes.
    """
    tool = find_tool(ME7INFO_NAMES)
    if not tool:
        log.append("Brak ME7Info.exe w tools/.")
        return None

    tool = tool.resolve()
    job_dir = job_dir.resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    ECUS_DIR.mkdir(parents=True, exist_ok=True)
    TOOLS_ECUS_DIR.mkdir(parents=True, exist_ok=True)

    # ME7Info is most reliable when the BIN name is simple and available in cwd.
    local_bin = (job_dir / bin_path.name).resolve()
    if bin_path.resolve() != local_bin:
        shutil.copy2(bin_path, local_bin)

    stem = local_bin.stem
    expected_name = stem + ".ecu"

    search_dirs = [
        job_dir,
        job_dir / "ecus",
        local_bin.parent,
        TOOLS_ECUS_DIR,
        ECUS_DIR,
        TOOLS_DIR,
        TOOLS_DIR / "ecus",
        APP_DIR,
        APP_DIR / "ecus",
    ]
    for folder in search_dirs:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # Remove only stale ECU files matching this BIN, never unrelated definitions.
    for folder in search_dirs:
        try:
            stale = folder / expected_name
            if stale.exists():
                stale.unlink()
        except Exception:
            pass

    started_ns = time.time_ns()
    attempts = [
        # Relative BIN name avoids Wine path conversion problems.
        (job_dir, [str(tool), "-n", local_bin.name]),
        (job_dir, [str(tool), local_bin.name]),
        # Fallbacks for builds that require defs relative to APP_DIR/tools.
        (APP_DIR, [str(tool), "-n", str(local_bin)]),
        (APP_DIR, [str(tool), str(local_bin)]),
        (TOOLS_DIR, [str(tool), "-n", str(local_bin)]),
        (TOOLS_DIR, [str(tool), str(local_bin)]),
    ]

    def find_fresh_ecu() -> Optional[Path]:
        candidates: List[Path] = []
        for folder in search_dirs:
            try:
                direct = folder / expected_name
                if direct.is_file():
                    candidates.append(direct)
                for c in folder.glob(f"{stem}*.ecu"):
                    if c.is_file() and c not in candidates:
                        candidates.append(c)
            except Exception:
                continue
        if not candidates:
            return None
        # Prefer a file created/modified during this invocation.
        candidates.sort(key=lambda x: x.stat().st_mtime_ns, reverse=True)
        fresh = [c for c in candidates if c.stat().st_mtime_ns >= started_ns - 2_000_000_000]
        return (fresh or candidates)[0]

    last_output = ""
    last_code = None
    for cwd, args in attempts:
        log.append("Uruchamiam ME7Info: " + cmd_str(args) + f" | cwd={cwd}")
        code, out = run_cmd(args, cwd, 180)
        last_code, last_output = code, out

        if out.strip():
            log.append(out.strip()[-5000:])
        else:
            log.append(f"ME7Info zakończył próbę kodem {code} bez komunikatu.")

        written = _me7info_written_path(out, local_bin)
        if written and written.exists():
            log.append(f"ME7Info wygenerował ECU: {written}")
            return _copy_ecu_to_job(written, local_bin, job_dir, log)

        time.sleep(0.25)
        found = find_fresh_ecu()
        if found:
            log.append(f"Znaleziono świeży ECU: {found}")
            return _copy_ecu_to_job(found, local_bin, job_dir, log)

    detail = last_output.strip()[-2000:] if last_output.strip() else "brak komunikatu"
    log.append(
        "ME7Info nie utworzył .ecu dla aktualnego pliku. "
        f"Ostatni kod={last_code}; log={detail}"
    )
    return None

def normalize_config_base(data: bytearray, base_ref: int) -> int:
    candidates = []

    if base_ref < 0x10000 and base_ref + 0x10000 < len(data):
        candidates.append(base_ref + 0x10000)

    if 0 <= base_ref < len(data):
        candidates.append(base_ref)

    for c in candidates:
        chunk = data[c:c + 16]
        if chunk and chunk not in (b"\xFF" * len(chunk), b"\x00" * len(chunk)):
            return c

    return candidates[0] if candidates else base_ref


def decode_calls_target_to_offset(data: bytearray, hook: int) -> Optional[int]:
    if hook < 0 or hook + 4 > len(data) or data[hook] != 0xDA:
        return None

    bank = data[hook + 1]
    lo = data[hook + 2]
    hi = data[hook + 3]

    return ((bank << 16) | (hi << 8) | lo) & 0xFFFFF


def find_hook(data: bytearray, function_offset: Optional[int] = None) -> Optional[int]:
    start = 0
    best = None

    while True:
        pos = data.find(HOOK_NEAR_PATTERN, start)
        if pos == -1:
            return best

        h = pos - 4

        if h >= 0 and data[h] == 0xDA:
            if function_offset is None:
                return h

            target = decode_calls_target_to_offset(data, h)
            if target == function_offset:
                return h

            best = h

        start = pos + 1


def _scan_setzi_launch_function(data: bytearray) -> PatchInfo:
    """
    Stara, sprawdzona detekcja Launch/Setzi.
    Szuka właściwej funkcji LC/NLS po strukturze 144 bajtów i adresach configu.
    Nie opiera się na hooku, więc działa także wtedy, gdy Rolling przejął hook
    i pracuje w CHAIN MODE.
    """
    scan_from = 0x70000 if len(data) > 0x80000 else 0
    scan_to = max(scan_from, len(data) - FUNCTION_LEN)

    for off in range(scan_from, scan_to):
        if data[off] not in (0x9A, 0x8A):
            continue
        if data[off + FUNCTION_LEN - 2:off + FUNCTION_LEN] != b"\xFF\xFF":
            continue
        if data[off + 4:off + 6] != b"\xF2\xF4":
            continue
        if data[off + 16:off + 20] != b"\x40\x49\x9D\x0B":
            continue

        vals = {
            n: int.from_bytes(data[off + pos:off + pos + 2], "little")
            for n, pos in COND_OFFSETS.items()
        }
        base_ref = vals["SpeedThreshold"]

        if (
            vals["LaunchRPM"] == base_ref + 2
            and vals["IgnitionCutDuration"] == base_ref + 4
            and vals["RPMThreshold"] == base_ref + 6
            and vals["AccPedalThreshold"] == base_ref + 8
        ):
            config_base = normalize_config_base(data, base_ref)
            hook = find_hook(data, off)
            return PatchInfo(
                True,
                off,
                hook,
                config_base,
                f"Znaleziono funkcję Setzi LC/NLS. function=0x{off:X}, config_base=0x{config_base:X}",
            )

    return PatchInfo(False, None, None, None, "Nie znaleziono funkcji Setzi LC/NLS w BIN.")


def detect_patch(data: bytearray) -> PatchInfo:
    """
    Detekcja Launch/ALS/NLS.

    WAŻNE: nie traktujemy samego hooka jako pełnej detekcji Launch, bo po Rolling
    główny hook może wskazywać w rolling code cave. Sam hook bez rozpoznanej
    funkcji Setzi dawał wcześniej objaw: "ALS wykryty", ale bez wartości configu.
    """
    patch = _scan_setzi_launch_function(data)
    if patch.installed:
        return patch

    # Słabszy fallback: sam blok konfiguracji. Daje możliwość odczytu wartości,
    # ale bez function_offset nie zmienimy aktywatora LC.
    idx = data.find(DEFAULT_CONFIG_BLOCK)
    if idx != -1:
        return PatchInfo(True, None, None, idx, "Znaleziono blok konfiguracji LC/NLS.")

    return patch


def _parse_launch_output_for_patch(data: bytearray, job_dir: Path) -> Optional[PatchInfo]:
    """
    Fallback detekcji LC po trybie CHAIN.

    Po dodaniu Rolling główny hook może wskazywać już na rolling code cave, a nie
    bezpośrednio na funkcję Launch. Dlatego klasyczne find_hook() może nie wystarczyć.
    Nie wpisujemy adresów na sztywno — czytamy je z launch_output.txt dla danego joba:
      - pierwsze "space located at" = Main Function / code cave LC,
      - drugie "space located at" = config variables LC.
    """
    launch_log = job_dir / "launch_output.txt"
    if not launch_log.exists():
        return None

    txt = launch_log.read_text(errors="ignore", encoding="latin-1")
    spaces = re.findall(r"space\s+located\s+at:\s*0x([0-9A-Fa-f]+)", txt, flags=re.IGNORECASE)
    if not spaces:
        return None

    function_offset = int(spaces[0], 16) if len(spaces) >= 1 else None
    config_base = int(spaces[1], 16) if len(spaces) >= 2 else None

    if function_offset is not None:
        if not (0 <= function_offset < len(data)):
            function_offset = None
        elif data[function_offset:function_offset + 16] in (b"\x00" * 16, b"\xFF" * 16):
            function_offset = None

    if config_base is not None:
        if not (0 <= config_base < len(data)):
            config_base = None
        elif data[config_base:config_base + 10] in (b"\x00" * 10, b"\xFF" * 10):
            config_base = None

    if function_offset is None and config_base is None:
        return None

    return PatchInfo(
        True,
        function_offset,
        find_hook(data, function_offset) if function_offset is not None else None,
        config_base,
        "Znaleziono LC z launch_output.txt po trybie CHAIN/Rolling."
    )


def detect_launch_patch(data: bytearray, job_dir: Optional[Path] = None) -> PatchInfo:
    patch = detect_patch(data)
    if patch.installed and patch.config_base is not None:
        return patch

    if job_dir is not None:
        fallback = _parse_launch_output_for_patch(data, job_dir)
        if fallback is not None:
            return fallback

    return patch


def _bin_chunk_has_code(data: bytearray, off: Optional[int], size: int = 32) -> bool:
    if off is None or not (0 <= off < len(data)):
        return False
    chunk = data[off:off + size]
    return bool(chunk) and chunk not in (b"\x00" * len(chunk), b"\xFF" * len(chunk))


def _guess_rolling_vars_from_launch(data: bytearray, launch_config_base: Optional[int]) -> Optional[int]:
    """
    rolling_chain.exe z Twojego logu użył Rolling vars dokładnie za blokiem LC:
      LC config: 0x17A60
      Rolling vars: 0x17A80
    Nie wpisujemy adresu na sztywno, tylko liczymy relatywnie od wykrytego
    config_base Launch. Jeżeli obszar wygląda na pusty, nie zgłaszamy vars.
    """
    if launch_config_base is None:
        return None
    cand = launch_config_base + 0x20
    if not (0 <= cand < len(data)):
        return None
    chunk = data[cand:cand + 16]
    if not chunk or chunk in (b"\x00" * len(chunk), b"\xFF" * len(chunk)):
        return None
    return cand


def _read_all_rolling_logs(job_dir: Optional[Path]) -> str:
    """Czyta logi rolling_chain.exe z aktualnego joba.

    Najważniejszy plik to rolling_output.txt zapisywany przez run_tool_exe().
    Dodatkowo sprawdzamy wszystkie pliki tekstowe z nazwą rolling, żeby nie zgubić
    logu przy zmianie nazwy/wersji skryptu.
    """
    if job_dir is None:
        return ""

    paths = []
    p = job_dir / "rolling_output.txt"
    if p.exists():
        paths.append(p)

    for q in sorted(job_dir.glob("*rolling*.txt"), key=lambda x: x.stat().st_mtime, reverse=True):
        if q not in paths:
            paths.append(q)

    txt = []
    for path in paths:
        try:
            txt.append(path.read_text(errors="ignore", encoding="latin-1"))
        except Exception:
            pass
    return "\n".join(txt)


def _parse_rolling_log(txt: str) -> dict:
    """Zwraca dane Rolling z logu rolling_chain.exe bez wpisywania adresów na sztywno."""
    out = {"installed": False, "code_cave": None, "vars": None, "reason": "Nie wykryto Rolling w logu."}
    if not txt.strip():
        return out

    m_code = re.search(r"Rolling\s+code\s+cave:\s*0x([0-9A-Fa-f]+)", txt, flags=re.IGNORECASE)
    m_vars = re.search(r"Rolling\s+vars:\s*0x([0-9A-Fa-f]+)", txt, flags=re.IGNORECASE)
    m_chain = re.search(r"CHAIN\s+MODE|existing\s+DA\s+hook\s+detected", txt, flags=re.IGNORECASE)
    m_ok = re.search(r"Result\s+written\s+successfully", txt, flags=re.IGNORECASE)
    m_hook = re.search(r"main\s+hook\s+offset:\s*0x([0-9A-Fa-f]+)", txt, flags=re.IGNORECASE)
    m_old = re.search(r"old\s+launch/ALS\s+code\s+cave\s+target:\s*0x([0-9A-Fa-f]+)", txt, flags=re.IGNORECASE)

    code = int(m_code.group(1), 16) if m_code else None
    vars_ = int(m_vars.group(1), 16) if m_vars else None
    hook = int(m_hook.group(1), 16) if m_hook else None
    old_launch = int(m_old.group(1), 16) if m_old else None

    if m_ok or m_chain or code is not None or vars_ is not None:
        out.update({
            "installed": True,
            "code_cave": code,
            "vars": vars_,
            "hook_offset": hook,
            "old_launch_target": old_launch,
            "reason": "Wykryto Rolling z logu rolling_chain.exe.",
        })
    return out


def _find_all_hooks(data: bytearray) -> List[Tuple[int, Optional[int]]]:
    """Zwraca wszystkie hooki CALLS znajdujące się przed HOOK_NEAR_PATTERN."""
    hooks: List[Tuple[int, Optional[int]]] = []
    start = 0
    while True:
        pos = data.find(HOOK_NEAR_PATTERN, start)
        if pos == -1:
            break
        h = pos - 4
        if h >= 0 and data[h] == 0xDA:
            hooks.append((h, decode_calls_target_to_offset(data, h)))
        start = pos + 1
    return hooks


def detect_rolling_patch(data: bytearray, job_dir: Optional[Path] = None) -> dict:
    """
    Detekcja Rolling adekwatnie do rolling_chain.exe.

    Rolling w trybie CHAIN robi tak:
      1) stara funkcja Launch/ALS dalej zostaje w BIN,
      2) główny hook CALLS nie celuje już bezpośrednio w Launch,
      3) hook celuje w Rolling code cave,
      4) Rolling chainuje dalej do starego Launch.

    Dlatego Rolling NIE może być wykrywany tak samo jak Launch po funkcji Setzi.
    Launch wykrywamy po funkcji Setzi, a Rolling po zmianie celu hooka / logu / śladzie CALLS.
    """
    result = {"installed": False, "code_cave": None, "vars": None, "reason": "Nie wykryto Rolling."}

    launch = _scan_setzi_launch_function(data)

    # 1) Najpewniej w bieżącym jobie: log rolling_chain.exe.
    log_txt = _read_all_rolling_logs(job_dir)
    from_log = _parse_rolling_log(log_txt)
    if from_log.get("installed"):
        code = from_log.get("code_cave")
        # Jeżeli mamy code cave, potwierdzamy, że w BIN nie jest pusty.
        # Jeśli nie mamy code cave, sam komunikat "Result written successfully" dalej jest dobrym sygnałem w tym jobie.
        if code is None or _bin_chunk_has_code(data, code, 16):
            return from_log

    # 2) Detekcja z BIN bez logu: szukamy wszystkich hooków przy HOOK_NEAR_PATTERN.
    # W czystym Launch target hooka == launch.function_offset. Po Rolling target jest inny.
    if launch.installed and launch.function_offset is not None:
        hooks = _find_all_hooks(data)
        for hook_off, target in hooks:
            if target is None:
                continue
            if target != launch.function_offset and _bin_chunk_has_code(data, target, 16):
                return {
                    "installed": True,
                    "code_cave": target,
                    "vars": _guess_rolling_vars_from_launch(data, launch.config_base),
                    "hook_offset": hook_off,
                    "old_launch_target": launch.function_offset,
                    "reason": (
                        "Wykryto Rolling po BIN/CHAIN MODE: hook CALLS celuje w "
                        f"0x{target:X}, a funkcja Launch jest w 0x{launch.function_offset:X}."
                    ),
                }

        # 3) Dodatkowy ślad CHAIN: dodatkowy CALLS do funkcji Launch z innego obszaru.
        # Pure Launch ma zwykle tylko main hook. Rolling code cave zawiera kolejny CALLS do starego Launch.
        hits = []
        for i in range(0, len(data) - 4):
            if data[i] == 0xDA:
                t = decode_calls_target_to_offset(data, i)
                if t == launch.function_offset:
                    hits.append(i)
                    if len(hits) > 8:
                        break

        main_hook_offsets = {h for h, _ in hooks}
        non_main_hits = [h for h in hits if h not in main_hook_offsets]
        if non_main_hits:
            # Najczęściej pierwszy taki CALLS siedzi w Rolling code cave.
            return {
                "installed": True,
                "code_cave": None,
                "vars": _guess_rolling_vars_from_launch(data, launch.config_base),
                "hook_offset": None,
                "old_launch_target": launch.function_offset,
                "reason": "Wykryto Rolling po dodatkowym CALLS łańcuchującym do funkcji Launch.",
            }

    return result


def read_config(data: bytearray, base: int) -> Dict[str, float]:
    out = {}

    for name, unit, rel, size, scale, mn, mx in ALS_MAPS:
        if size == 2:
            raw = int.from_bytes(data[base + rel:base + rel + size], "little")
        else:
            raw = data[base + rel]

        out[name] = round(raw * scale, 3)

    return out


def write_config_value(data: bytearray, base: int, rel: int, size: int, scale: float, value: float):
    raw = int(round(value / scale))
    max_raw = 0xFFFF if size == 2 else 0xFF

    if not 0 <= raw <= max_raw:
        raise ValueError(f"RAW poza zakresem dla {value}: {raw}")

    if size == 2:
        data[base + rel:base + rel + 2] = raw.to_bytes(2, "little")
    else:
        data[base + rel] = raw


def patch_lc_activator(data: bytearray, function_offset: int, switch: SwitchBit):
    for bitaddr_rel, bitmask_rel in CLUTCH_PATCH_POINTS:
        data[function_offset + bitaddr_rel] = switch.patch_bit_address_byte
        data[function_offset + bitmask_rel] = switch.patch_bitmask_byte


def detect_current_activator(data: bytearray, function_offset: int, switches: Dict[str, SwitchBit]) -> str:
    b = data[function_offset + CLUTCH_PATCH_POINTS[0][0]]
    m = data[function_offset + CLUTCH_PATCH_POINTS[0][1]]

    for key, sw in switches.items():
        if b == sw.patch_bit_address_byte and m == sw.patch_bitmask_byte:
            return key

    return "unknown"


def run_me7check(bin_path: Path, log: List[str]) -> str:
    tool = find_tool(ME7CHECK_NAMES)
    if not tool:
        msg = "Brak ME7Check.exe w tools/."
        log.append(msg)
        return msg

    args = [str(tool), bin_path.name]

    log.append("Uruchamiam: " + cmd_str(args))

    code, out = run_cmd(args, bin_path.parent, 180)

    if out.strip():
        log.append(out.strip()[-6000:])
    else:
        log.append(f"ME7Check zakończony kodem {code}")

    return out


def run_launch_patch(bin_path: Path, ecu_path: Path, job_dir: Path, log: List[str]) -> Optional[Path]:
    tool = find_tool(LAUNCH_NAMES)
    if not tool:
        log.append("Brak launch.exe w tools/.")
        return None

    local_ecu = job_dir / ecu_path.name

    if ecu_path.resolve() != local_ecu.resolve():
        shutil.copy2(ecu_path, local_ecu)

    args = [str(tool), bin_path.name, local_ecu.name]

    log.append("Uruchamiam: " + cmd_str(args))

    code, out = run_cmd(args, job_dir, 240)

    (job_dir / "launch_output.txt").write_text(out, encoding="utf-8", errors="ignore")

    if out.strip():
        log.append("===== LOG launch.exe =====")
        log.append(out.strip())
        log.append("===== KONIEC LOG launch.exe =====")

    mods = sorted(job_dir.glob("*_mod.bin"), key=lambda p: p.stat().st_mtime, reverse=True)

    if mods:
        return mods[0]

    return None




def run_tool_exe(tool: Path, args: List[str], cwd: Path, timeout: int, log: List[str], title: str) -> Tuple[int, str]:
    """Uruchamia pomocniczy program EXE bez zależności od zewnętrznego Pythona."""
    cmd = [str(tool)] + args

    log.append(f"Uruchamiam {title}: " + cmd_str(cmd))
    code, out = run_cmd(cmd, cwd, timeout)

    if out.strip():
        log.append(f"===== LOG {title} =====")
        log.append(out.strip()[-8000:])
        log.append(f"===== KONIEC LOG {title} =====")

    try:
        if "rolling" in title.lower():
            (cwd / "rolling_output.txt").write_text(out, encoding="utf-8", errors="ignore")
        elif "pops" in title.lower():
            (cwd / "pops_output.txt").write_text(out, encoding="utf-8", errors="ignore")
    except Exception:
        pass

    return code, out


def newest_changed_bin(job_dir: Path, before: set[Path], input_path: Path) -> Optional[Path]:
    candidates = [p for p in job_dir.glob("*.bin") if p not in before and p.exists()]
    candidates = [p for p in candidates if p.resolve() != input_path.resolve()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def build_rolling_cli_args(rolling_config: Optional[dict]) -> List[str]:
    """
    Aktualny rolling_chain.exe NIE ma flag typu --rpm/--acc-pedal.
    Parametry Rolling zapisujemy dopiero w kroku konfiguracji przez edycję Rolling vars.
    Ta funkcja celowo nic nie zwraca, żeby nie wywoływać nieobsługiwanych argumentów.
    """
    return []


def run_rolling_patch(bin_path: Path, ecu_path: Optional[Path], job_dir: Path, log: List[str], trigger: str = "auto", rolling_config: Optional[dict] = None) -> Optional[Path]:
    script = find_tool(ROLLING_NAMES)
    if not script:
        log.append("Brak rolling_chain.exe w tools/.")
        return None

    local_bin = job_dir / bin_path.name
    if bin_path.resolve() != local_bin.resolve():
        shutil.copy2(bin_path, local_bin)

    local_ecu = None
    if ecu_path and ecu_path.exists():
        local_ecu = job_dir / ecu_path.name
        if ecu_path.resolve() != local_ecu.resolve():
            shutil.copy2(ecu_path, local_ecu)

    before = set(job_dir.glob("*.bin"))

    # Najważniejszy wariant zgodny z Twoim dotychczasowym użyciem:
    #   rolling_chain.exe K04.bin K04.ecu
    args = [local_bin.name]
    if local_ecu:
        args.append(local_ecu.name)

    rolling_extra = build_rolling_cli_args(rolling_config)
    if rolling_extra:
        log.append("Konfiguracja Rolling z GUI: " + ", ".join(rolling_extra))

    # rolling_chain.exe obsługuje trigger jako 5. argument pozycyjny:
    #   bin ecu [code] [vars] [trigger]
    # Nie używamy --trigger. Sprzęgło jest celowo zablokowane.
    trigger_map = {
        "auto": "auto",
        "brake": "brake",
        "B_brems": "brake",
        "b_br": "brake",
        "cruise_set": "cruise_set",
        "b_fgrsec": "cruise_set",
        "cruise_res": "cruise_res",
        "cruise_main": "cruise_main",
    }
    trig = trigger_map.get(str(trigger or "auto"), "auto")
    if str(trigger) in {"B_kuppl", "clutch", "b_kuppl"}:
        log.append("Aktywacja Rolling przez sprzęgło jest wyłączona w GUI/backend — wymuszam auto/cruise_set.")
        trig = "auto"
    if trig != "auto":
        args.extend(["auto", "auto", trig])
        log.append(f"Rolling trigger z GUI: {trig} (argument pozycyjny rolling_chain.exe).")

    if rolling_extra:
        code, out = run_tool_exe(script, args + rolling_extra, job_dir, 240, log, "rolling_chain.exe")
        out_file = newest_changed_bin(job_dir, before, local_bin)
        if out_file:
            return out_file
        log.append("rolling_chain.exe z konfiguracją nie utworzył pliku, próbuję standardowo bez parametrów GUI.")

    code, out = run_tool_exe(script, args, job_dir, 240, log, "rolling_chain.exe")

    out_file = newest_changed_bin(job_dir, before, local_bin)
    if out_file:
        return out_file

    # Fallback po nazwach najczęściej spotykanych.
    expected = [
        job_dir / f"{local_bin.stem}_rolling.bin",
        job_dir / f"{local_bin.stem}_mod.bin",
    ]
    for p in expected:
        if p.exists() and p.resolve() != local_bin.resolve():
            return p

    log.append("rolling_chain.exe nie utworzył rozpoznanego pliku wyjściowego.")
    return None


def run_pops_patch(bin_path: Path, job_dir: Path, log: List[str], profile: str = "medium") -> Optional[Path]:
    """Run the packaged PopsAndBangs_CMD.exe and return its output BIN.

    The final application is Windows-only and deliberately uses only the EXE.
    Full paths and the short CLI switches (-p/-o) are used because they work
    reliably both in a normal Windows build and in a PyInstaller one-file build.
    """
    tool = find_tool(POPS_NAMES)
    if not tool:
        raise ValueError("Brak tools\\PopsAndBangs_CMD.exe w zasobach aplikacji.")

    profile = str(profile or "medium").strip().lower()
    if profile not in {"low", "medium", "high"}:
        raise ValueError(f"Nieobsługiwany profil Pops & Bangs: {profile}")

    job_dir = job_dir.resolve()
    job_dir.mkdir(parents=True, exist_ok=True)

    local_bin = (job_dir / bin_path.name).resolve()
    if bin_path.resolve() != local_bin:
        shutil.copy2(bin_path, local_bin)

    if not local_bin.is_file():
        raise ValueError(f"Nie udało się przygotować wejściowego BIN: {local_bin}")

    out_path = (job_dir / f"{local_bin.stem}_POPS_{profile.upper()}.bin").resolve()

    # Snapshot is used to detect output even if an older EXE ignores -o and
    # chooses its own file name.
    before_bins = {
        p.resolve(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in job_dir.glob("*.bin")
        if p.is_file()
    }

    # PopsAndBangs_CMD.py (from which the EXE is built) accepts:
    #   input -p PROFILE -o OUTPUT
    # Absolute paths avoid ambiguity caused by the temporary PyInstaller cwd.
    args = [
        str(tool.resolve()),
        str(local_bin),
        "-p", profile,
        "-o", str(out_path),
    ]

    title = tool.name
    log.append(f"Pops & Bangs profile selected: {profile.upper()}")
    log.append("Running Pops & Bangs EXE: " + cmd_str(args))

    code, out = run_cmd(args, job_dir, 300)
    clean_out = out.strip()
    if clean_out:
        log.append(f"===== LOG {title} =====")
        log.append(clean_out[-16000:])
        log.append(f"===== END LOG {title} =====")

    try:
        (job_dir / "pops_output.txt").write_text(out, encoding="utf-8", errors="ignore")
    except Exception:
        pass

    if code != 0:
        detail = clean_out[-3000:] if clean_out else "Brak komunikatu z programu."
        raise ValueError(
            f"{title} zakończył pracę z kodem {code}.\n\n"
            f"Końcówka logu:\n{detail}"
        )

    expected_size = local_bin.stat().st_size

    if out_path.is_file() and out_path.stat().st_size == expected_size:
        log.append("Pops & Bangs created output: " + out_path.name)
        return out_path

    # Accept every new or modified BIN of the correct size. This handles older
    # EXE builds that ignore -o and create a default *_POPS_*.bin or *_mod.bin.
    candidates: List[Path] = []
    for candidate in job_dir.glob("*.bin"):
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved == local_bin:
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if stat.st_size != expected_size:
            continue

        old = before_bins.get(resolved)
        if old is None or old != (stat.st_mtime_ns, stat.st_size):
            candidates.append(candidate)

    # Also check common names in case filesystem timestamp resolution is poor.
    common_names = [
        f"{local_bin.stem}_POPS_{profile.upper()}.bin",
        f"{local_bin.stem}_pops_{profile.lower()}.bin",
        f"{local_bin.stem}_POPS.bin",
        f"{local_bin.stem}_mod.bin",
        f"{local_bin.stem}_modified.bin",
    ]
    for name in common_names:
        candidate = job_dir / name
        if (
            candidate.is_file()
            and candidate.resolve() != local_bin
            and candidate.stat().st_size == expected_size
            and candidate not in candidates
        ):
            candidates.append(candidate)

    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        selected = candidates[0].resolve()
        log.append("Pops & Bangs detected output: " + selected.name)
        return selected

    files_after = ", ".join(sorted(p.name for p in job_dir.iterdir() if p.is_file()))
    detail = clean_out[-3000:] if clean_out else "Brak komunikatu z programu."
    raise ValueError(
        f"{title} zwrócił kod 0, ale nie znaleziono pliku wyjściowego BIN.\n"
        f"Oczekiwano: {out_path.name}\n"
        f"Pliki w katalogu roboczym: {files_after or '(brak)'}\n\n"
        f"Końcówka logu:\n{detail}"
    )


def apply_lc_configuration_no_checksum(bin_path: Path, values: dict, activator: str, job_dir: Path, log: List[str], softcut: bool = False) -> Path:
    data = read_bin(bin_path)
    patch = detect_launch_patch(data, job_dir)

    if not patch.installed or patch.config_base is None:
        log.append("LC/ALS nie jest wykryty albo brak config_base — pomijam konfigurację LC.")
        return bin_path

    if softcut:
        values = dict(values)
        values["IgnitionCutDuration"] = 20
        log.append("Softcut experimental aktywny: wymuszam IgnitionCutDuration = 20 ms i przywracam FTOMN z logu launch.exe.")

    for name, unit, rel, size, scale, mn, mx in ALS_MAPS:
        if name in values and str(values[name]).strip() != "":
            write_config_value(data, patch.config_base, rel, size, scale, float(values[name]))

    ecu = generate_ecu(bin_path, job_dir, log)
    switches = parse_ecu_file(ecu) if ecu else {}

    if patch.function_offset is not None and activator in switches:
        patch_lc_activator(data, patch.function_offset, switches[activator])
        log.append("Aktywator Launch Control ustawiony: " + switches[activator].pretty())

    if softcut:
        apply_softcut_mode(data, patch, job_dir, log)

    out = job_dir / f"{bin_path.stem}_lc_configured_no_checksum.bin"
    out.write_bytes(data)
    log.append("Zapisano etap LC bez checksum: " + out.name)
    return out


def patch_pipeline(
    bin_path: Path,
    job_dir: Path,
    *,
    launch: bool = False,
    rolling: bool = False,
    pops: bool = False,
    pops_profile: str = "medium",
    values: Optional[dict] = None,
    activator: str = "B_kuppl",
    rolling_trigger: str = "cruise_set",
    rolling_config: Optional[dict] = None,
    softcut: bool = False,
) -> dict:
    log: List[str] = []
    current = bin_path

    log.append("===== START PATCHOWANIA WYBRANYCH FUNKCJI =====")
    log.append("Kolejność patchowania: Launch Control -> Rolling -> Pops and Bangs.")
    log.append("Checksum NIE jest liczony w tym kroku. Po patchowaniu kliknij Zapisz, wtedy me7sum liczy sumę na samym końcu.")

    ecu = generate_ecu(current, job_dir, log)

    if launch:
        patch = detect_launch_patch(read_bin(current), job_dir)
        if patch.installed:
            log.append("Launch Control/ALS już wykryty — nie uruchamiam launch.exe drugi raz.")
        else:
            if not ecu:
                raise ValueError("Brak pliku .ecu — nie można uruchomić launch.exe.")
            out = run_launch_patch(current, ecu, job_dir, log)
            if not out:
                raise ValueError("launch.exe nie utworzył pliku _mod.bin.")
            current = out
            ecu = generate_ecu(current, job_dir, log)

    # WAŻNE: ten przycisk ma działać jak pierwotne "Wgraj ALS".
    # Tutaj TYLKO wgrywamy kod funkcji. Konfigurację LC/Rolling użytkownik
    # robi dopiero po ponownej analizie pliku, przyciskiem Zapisz.
    # Dzięki temu po patchowaniu detektor widzi świeżo wgrany kod i GUI
    # odblokowuje pola konfiguracji tak samo jak w starym flow launch.exe.

    if rolling:
        ecu = generate_ecu(current, job_dir, log)
        out = run_rolling_patch(current, ecu, job_dir, log, trigger=rolling_trigger, rolling_config=rolling_config)
        if not out:
            raise ValueError("Rolling nie utworzył pliku wyjściowego.")
        current = out

    if pops:
        log.append("UWAGA: Pops and Bangs jest oznaczony jako TEST / EXPERIMENTAL.")
        out = run_pops_patch(current, job_dir, log, profile=pops_profile)
        current = out

    raw = job_dir / f"{current.stem}_ready_no_checksum.bin"
    if current.resolve() != raw.resolve():
        shutil.copy2(current, raw)

    log.append("Zapisano plik po wybranych patchach BEZ checksum: " + raw.name)
    log.append("Teraz kliknij Zapisz — checksum zostanie przeliczony jeden raz na samym końcu.")
    log.append("===== KONIEC PATCHOWANIA WYBRANYCH FUNKCJI =====")

    return {
        "ok": True,
        "raw": raw.name,
        "output": None,
        "pending_checksum": True,
        "log": log,
    }


def finalize_checksum(bin_path: Path, job_dir: Path) -> dict:
    log: List[str] = []
    log.append("===== START CHECKSUM =====")
    log.append("Plik wejściowy do checksum: " + bin_path.name)

    if bin_path.name.lower().endswith("_csok.bin"):
        out = bin_path
        log.append("Plik wygląda już jak po checksum (_csok.bin). Mimo to zostawiam go jako aktualny do pobrania.")
        run_me7check(out, log)
        log.append("===== KONIEC CHECKSUM =====")
        return {"ok": True, "raw": bin_path.name, "output": out.name, "log": log}

    stem = bin_path.stem
    for suffix in ("_ready_no_checksum", "_final_no_checksum", "_configured_no_checksum", "_no_checksum"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    out = job_dir / f"{stem}_csok.bin"
    ok = run_checksum(bin_path, out, log)
    if ok:
        run_me7check(out, log)

    log.append("===== KONIEC CHECKSUM =====")
    return {
        "ok": ok,
        "raw": bin_path.name,
        "output": out.name if ok else None,
        "log": log,
    }

def run_checksum(input_path: Path, output_path: Path, log: List[str]) -> bool:
    tool = find_tool(ME7SUM_NAMES)
    if not tool:
        log.append("Brak me7sum.exe w tools/.")
        return False

    args = [str(tool), input_path.name, output_path.name]

    log.append("Uruchamiam: " + cmd_str(args))

    code, out = run_cmd(args, input_path.parent, 240)

    if out.strip():
        log.append(out.strip()[-6000:])

    ok = output_path.exists() and output_path.stat().st_size == input_path.stat().st_size

    if ok:
        log.append("Checksum OK: " + output_path.name)
    else:
        log.append("me7sum nie utworzył poprawnego pliku wyjściowego.")

    return ok


def analyze_file(bin_path: Path, job_dir: Path) -> dict:
    log = []

    data = read_bin(bin_path)
    log.append(f"Wczytano {bin_path.name}, rozmiar {len(data)} bajtów")

    run_me7check(bin_path, log)

    ecu = generate_ecu(bin_path, job_dir, log)
    switches = parse_ecu_file(ecu) if ecu and ecu.exists() else {}

    # Fallback po logu launch.exe — szczególnie po patchowaniu i po CHAIN MODE.
    if not switches:
        switches = parse_switches_from_launch_log(job_dir)
        if switches:
            log.append("Przełączniki B_kuppl/B_brems odczytane awaryjnie z launch_output.txt.")

    if ecu and ecu.exists():
        if switches:
            log.append("Przełączniki z .ecu/logu: " + " | ".join(v.pretty() for v in switches.values()))
        else:
            log.append(f"UWAGA: .ecu wygenerowany ({ecu.name}), ale nie znaleziono B_kuppl/B_brems. Patchowanie nadal jest dostępne; aktywator LC może nie być zmienialny.")
    else:
        log.append("UWAGA: ME7Info nie zwrócił .ecu. Patchowanie nadal jest dostępne, ale aktywator LC może nie być zmienialny.")

    patch = detect_launch_patch(data, job_dir)
    rolling_patch = detect_rolling_patch(data, job_dir)

    if rolling_patch.get("installed"):
        details = []
        if rolling_patch.get("code_cave") is not None:
            details.append(f"code_cave=0x{int(rolling_patch['code_cave']):X}")
        if rolling_patch.get("vars") is not None:
            details.append(f"vars=0x{int(rolling_patch['vars']):X}")
        if rolling_patch.get("hook_offset") is not None:
            details.append(f"hook=0x{int(rolling_patch['hook_offset']):X}")
        log.append("Rolling wykryty" + (" (" + ", ".join(details) + ")" if details else "") + ". " + str(rolling_patch.get("reason", "")))
    else:
        log.append("Rolling nie wykryty. " + str(rolling_patch.get("reason", "")))

    # Dodatkowy fallback: gdy ktoś wgra ponownie gotowy plik po restarcie strony,
    # nie mamy już rolling_output.txt w RAM/logach joba. Nazwa pliku z naszego
    # pipeline zawiera zwykle "rolling", więc przynajmniej status Rolling zostaje
    # widoczny w GUI. Adresy nadal nie są wpisywane na sztywno.
    if not rolling_patch.get("installed") and "rolling" in bin_path.name.lower():
        rolling_patch = {
            "installed": True,
            "code_cave": None,
            "vars": None,
            "reason": "Wykryto Rolling po nazwie pliku; brak logu rolling_output.txt po restarcie."
        }

    rolling_cfg = {}
    if rolling_patch.get("installed") and rolling_patch.get("vars") is not None:
        try:
            rolling_cfg = read_rolling_config(data, int(rolling_patch["vars"]))
            if rolling_cfg:
                log.append(
                    f"Rolling config odczytany: RPM Rolling={rolling_cfg.get('RollingRPM')} rpm, "
                    f"Próg pedału gazu={rolling_cfg.get('RollingPedalPercent')}%. "
                    f"Vars base=0x{int(rolling_patch['vars']):X}"
                )
            else:
                log.append("Rolling wykryty, ale nie udało się odczytać bloku Rolling vars.")
        except Exception as e:
            log.append("Błąd odczytu konfiguracji Rolling: " + str(e))
            rolling_cfg = {}

    if patch.config_base is not None and rolling_patch.get("vars") is not None:
        if ranges_overlap(patch.config_base, 16, int(rolling_patch["vars"]), ROLLING_VARS_LEN):
            log.append(f"UWAGA: Rolling vars 0x{int(rolling_patch['vars']):X} KOLIDUJE z Launch config 0x{patch.config_base:X}.")
        else:
            log.append(f"Brak kolizji: Launch config 0x{patch.config_base:X}-0x{patch.config_base+15:X}, Rolling vars 0x{int(rolling_patch['vars']):X}-0x{int(rolling_patch['vars'])+ROLLING_VARS_LEN-1:X}.")

    cfg = {}
    if patch.installed and patch.config_base is not None:
        try:
            cfg = read_config(data, patch.config_base)
            log.append(f"Launch config odczytany z config_base=0x{patch.config_base:X}: " + ", ".join(f"{k}={v}" for k, v in cfg.items()))
        except Exception as e:
            log.append(f"Błąd odczytu konfiguracji Launch z 0x{patch.config_base:X}: {e}")
            cfg = {}
    elif patch.installed:
        log.append("Launch/ALS wykryty, ale brak config_base — pola konfiguracji nie mogą być wypełnione.")

    current = "unknown"

    if patch.function_offset is not None and switches:
        current = detect_current_activator(data, patch.function_offset, switches)

    return {
        "log": log,
        "ecu_path": str(ecu) if ecu else None,
        "switches": {k: v.pretty() for k, v in switches.items()},
        "patch": dataclasses.asdict(patch),
        "rolling": rolling_patch,
        "config": cfg,
        "rolling_config": rolling_cfg,
        "current_activator": current,
    }



def read_launch_softcut_info(job_dir: Path) -> Dict[str, Optional[int]]:
    """
    Czyta indywidualne dane z logu launch.exe dla konkretnego joba.
    Przykład z launch_output.txt:
      FTOMN found: 1a2db
      FTOMN IS: 05
      FTOMN CHANGED TO 0x00
      space located at: 0x17a60
    """
    launch_log = job_dir / "launch_output.txt"
    info: Dict[str, Optional[int]] = {
        "ftomn_addr": None,
        "ftomn_original": None,
        "config_base": None,
    }

    if not launch_log.exists():
        return info

    txt = launch_log.read_text(errors="ignore", encoding="latin-1")

    m_addr = re.search(r"FTOMN\s+found:\s*(?:0x)?([0-9A-Fa-f]+)", txt)
    m_val = re.search(r"FTOMN\s+IS:\s*(?:0x)?([0-9A-Fa-f]+)", txt)

    if m_addr:
        info["ftomn_addr"] = int(m_addr.group(1), 16)
    if m_val:
        info["ftomn_original"] = int(m_val.group(1), 16)

    # launch.exe zwykle wypisuje dwa razy "space located at" — druga wartość to config vars.
    spaces = re.findall(r"space\s+located\s+at:\s*0x([0-9A-Fa-f]+)", txt, flags=re.IGNORECASE)
    if len(spaces) >= 2:
        info["config_base"] = int(spaces[1], 16)
    elif len(spaces) == 1:
        info["config_base"] = int(spaces[0], 16)

    return info


def apply_softcut_mode(data: bytearray, patch: PatchInfo, job_dir: Path, log: List[str]) -> None:
    """
    Softcut experimental:
    - IgnitionCutDuration zawsze 20 ms,
    - FTOMN przywracany indywidualnie z launch_output.txt: FTOMN found + FTOMN IS.
    Nic nie jest wpisywane na sztywno — adres i oryginalna wartość są czytane z logu aktualnego pliku.
    """
    if patch.config_base is None:
        log.append("Softcut: brak config_base, nie mogę ustawić IgnitionCutDuration.")
    else:
        write_config_value(data, patch.config_base, 0x04, 2, 20.0, 20.0)
        log.append("Softcut: IgnitionCutDuration wymuszone na 20 ms.")

    info = read_launch_softcut_info(job_dir)
    ftomn_addr = info.get("ftomn_addr")
    ftomn_original = info.get("ftomn_original")

    if ftomn_addr is None or ftomn_original is None:
        log.append("Softcut: nie znaleziono FTOMN found / FTOMN IS w launch_output.txt — FTOMN nie został przywrócony.")
        return

    if not 0 <= ftomn_addr < len(data):
        log.append(f"Softcut: adres FTOMN poza zakresem BIN: 0x{ftomn_addr:X}")
        return

    if not 0 <= ftomn_original <= 0xFF:
        log.append(f"Softcut: wartość FTOMN poza zakresem 1 bajtu: 0x{ftomn_original:X}")
        return

    current = data[ftomn_addr]
    data[ftomn_addr] = ftomn_original
    log.append(
        f"Softcut: FTOMN przywrócony indywidualnie z logu launch.exe: "
        f"adres 0x{ftomn_addr:X}, było w BIN 0x{current:02X}, przywrócono 0x{ftomn_original:02X}."
    )

    if info.get("config_base") is not None:
        log.append(f"Softcut: launch.exe podał config vars space located at: 0x{info['config_base']:X}")

def configure_file(bin_path: Path, values: dict, activator: str, job_dir: Path, softcut: bool = False, rolling_values: Optional[dict] = None) -> dict:
    log = []

    data = read_bin(bin_path)
    patch = detect_launch_patch(data, job_dir)
    rolling_patch = detect_rolling_patch(data, job_dir)

    if (not patch.installed or patch.config_base is None) and not (rolling_patch.get("installed") and rolling_patch.get("vars") is not None):
        raise ValueError("Ten BIN nie ma wykrytego ALS/LC/NLS ani konfigu Rolling.")

    if softcut:
        values = dict(values)
        values["IgnitionCutDuration"] = 20
        log.append("Softcut experimental aktywny: wartość z GUI zostanie wymuszona na 20 ms, a FTOMN będzie przywrócony z launch_output.txt.")

    if patch.installed and patch.config_base is not None:
        for name, unit, rel, size, scale, mn, mx in ALS_MAPS:
            if name in values:
                write_config_value(
                    data,
                    patch.config_base,
                    rel,
                    size,
                    scale,
                    float(values[name]),
                )

        ecu = generate_ecu(bin_path, job_dir, log)
        switches = parse_ecu_file(ecu) if ecu else {}

        if patch.function_offset is not None and activator in switches:
            patch_lc_activator(data, patch.function_offset, switches[activator])
            log.append("Aktywator Launch ustawiony: " + switches[activator].pretty())

        if softcut:
            apply_softcut_mode(data, patch, job_dir, log)
    else:
        log.append("Launch config pominięty — brak wykrytego config_base Launch.")

    if rolling_patch.get("installed") and rolling_patch.get("vars") is not None:
        write_rolling_config(data, int(rolling_patch["vars"]), rolling_values or {}, log)
    elif rolling_values:
        log.append("Rolling config z GUI odebrany, ale brak wykrytego Rolling vars — nie zapisano Rolling.")

    stem = bin_path.stem

    raw = job_dir / f"{stem}_configured_no_checksum.bin"
    out = job_dir / f"{stem}_configured_csok.bin"

    raw.write_bytes(data)
    log.append("Zapisano roboczy BIN: " + raw.name)

    ok = run_checksum(raw, out, log)

    if ok:
        run_me7check(out, log)

    return {
        "ok": ok,
        "raw": raw.name,
        "output": out.name if ok else None,
        "log": log,
    }
