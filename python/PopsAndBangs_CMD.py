#!/usr/bin/env python3
"""Bosch ME7.5 Pops & Bangs Installer for command-line use.

Profiles and byte modifications reproduced from the original application:
  Low, Medium, High

This tool does not correct ECU checksums, because the original application
also only writes the modified BIN.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

APP_NAME = "Bosch ME7.5 Pops & Bangs Installer"
APP_VERSION = "1.0"
SUPPORTED_SIZE = 1_048_576


@dataclass(frozen=True)
class Profile:
    name: str
    kfzwmn_value: int
    kftvsa_value: int


PROFILES: dict[str, Profile] = {
    "low": Profile("Low", 230, 150),
    "medium": Profile("Medium", 223, 200),
    "high": Profile("High", 216, 255),
}

KFNWEGM_PATTERN_1 = bytes([0x80] * 30 + [0x1A])
KFNWEGM_PATTERN_2 = bytes([
    0x80, 0x80, 0x7E, 0x7D, 0x7C, 0x7B, 0x91, 0x8D,
    0x80, 0x80, 0x7D, 0x7C, 0x7A, 0x79, 0x94, 0x91,
    0x80, 0x80, 0x7D, 0x7C, 0x7A, 0x79, 0x95, 0x92,
    0x80, 0x80, 0x7D, 0x7C, 0x7A, 0x79, 0x1A,
])

KFZWMN_OFFSETS = (
    96, 97, 108, 109, 120, 121, 132, 133, 144, 145, 156, 157,
    168, 169, 170, 171, 180, 181, 182, 183, 184, 185,
)
KFTVSA_ROWS = (3, 11, 19, 27, 35)
KFTVSAKAT_OFFSETS = (
    4, 5, 6, 7,
    12, 13, 14, 15,
    20, 21, 22, 23,
    28, 29, 30, 31,
)


class PopsAndBangsError(RuntimeError):
    pass


def parse_hex_address(value: str) -> int:
    try:
        return int(value.strip().lower().removeprefix("0x"), 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid hexadecimal address: {value}") from exc


def find_all(data: bytes, pattern: bytes) -> list[int]:
    matches: list[int] = []
    start = 0
    while True:
        pos = data.find(pattern, start)
        if pos < 0:
            return matches
        matches.append(pos)
        start = pos + 1


def find_kfzwmn(data: bytes) -> list[int]:
    """Return map starts using the exact Pops&Bangs condition: match + 30."""
    matches: list[int] = []
    # Original code reads i+193, so at least 194 bytes must remain.
    for i in range(0, len(data) - 193):
        if (
            data[i] == 16
            and data[i + 1] == 12
            and 10 < data[i + 2] < 30
            and data[i + 193] > 128
        ):
            matches.append(i + 30)
    return matches


def find_kfnwegm(data: bytes) -> tuple[list[int], int | None]:
    matches = [pos + 31 for pos in find_all(data, KFNWEGM_PATTERN_1)]
    if matches:
        return matches, 1
    matches = [pos + 31 for pos in find_all(data, KFNWEGM_PATTERN_2)]
    return matches, 2 if matches else None


def choose_detected(name: str, matches: list[int]) -> int:
    if not matches:
        raise PopsAndBangsError(f"{name} was not found automatically.")
    if len(matches) > 1:
        rendered = ", ".join(f"0x{x:06X}" for x in matches)
        raise PopsAndBangsError(
            f"{name} detection is ambiguous ({len(matches)} matches): {rendered}. "
            f"Provide the address manually."
        )
    return matches[0]


def ensure_range(data: bytearray, address: int, offsets: Iterable[int], map_name: str) -> None:
    if address < 0:
        raise PopsAndBangsError(f"{map_name} address cannot be negative.")
    largest = max(offsets, default=0)
    if address + largest >= len(data):
        raise PopsAndBangsError(
            f"{map_name} range exceeds the BIN: address 0x{address:X}, "
            f"last byte 0x{address + largest:X}."
        )


def apply_modifications(
    original: bytes,
    profile: Profile,
    kfzwmn: int,
    kfnwegm: int,
    kftvsa: int,
    kftvsakat: int,
) -> tuple[bytes, list[tuple[int, int, int, str]]]:
    data = bytearray(original)
    changes: list[tuple[int, int, int, str]] = []

    ensure_range(data, kfzwmn, KFZWMN_OFFSETS, "KFZWMN")
    ensure_range(data, kfnwegm, range(40), "KFNWEGM")
    ensure_range(data, kftvsa, [row + 4 for row in KFTVSA_ROWS], "KFTVSA")
    ensure_range(data, kftvsakat, KFTVSAKAT_OFFSETS, "KFTVSAKAT")

    def write(address: int, value: int, map_name: str) -> None:
        old = data[address]
        data[address] = value
        if old != value:
            changes.append((address, old, value, map_name))

    for offset in KFZWMN_OFFSETS:
        write(kfzwmn + offset, profile.kfzwmn_value, "KFZWMN")

    for offset in range(40):
        write(kfnwegm + offset, 0xAA, "KFNWEGM")

    # Original layout for each selected row:
    # base+0 untouched, base+1 = value-100, base+2 = value-50,
    # base+3..base+5 = value. KFTVSA_ROWS stores base+1.
    for start in KFTVSA_ROWS:
        values = (
            profile.kftvsa_value - 100,
            profile.kftvsa_value - 50,
            profile.kftvsa_value,
            profile.kftvsa_value,
            profile.kftvsa_value,
        )
        for delta, value in enumerate(values):
            write(kftvsa + start + delta, value, "KFTVSA")

    for offset in KFTVSAKAT_OFFSETS:
        write(kftvsakat + offset, 0xFF, "KFTVSAKAT")

    return bytes(data), changes


def select_profile() -> Profile:
    print("\nSelect profile:")
    print("  [1] Low")
    print("  [2] Medium")
    print("  [3] High")
    print("  [0] Exit")
    choices = {"1": PROFILES["low"], "2": PROFILES["medium"], "3": PROFILES["high"]}
    while True:
        choice = input("\nChoice: ").strip()
        if choice == "0":
            raise KeyboardInterrupt
        if choice in choices:
            return choices[choice]
        print("Invalid choice. Enter 0, 1, 2 or 3.")


def default_output_path(input_path: Path, profile: Profile) -> Path:
    return input_path.with_name(f"{input_path.stem}_POPS_{profile.name.upper()}{input_path.suffix}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bosch ME7.5 Pops & Bangs Installer for VAG 20VT BIN files."
    )
    parser.add_argument("input", type=Path, help="Input BIN file")
    parser.add_argument(
        "-p", "--profile", choices=tuple(PROFILES),
        help="Profile: low, medium or high. Without this option an interactive menu is shown.",
    )
    parser.add_argument("-o", "--output", type=Path, help="Output BIN path")
    parser.add_argument("--kfzwmn", type=parse_hex_address, help="Manual KFZWMN address in hex")
    parser.add_argument("--kfnwegm", type=parse_hex_address, help="Manual KFNWEGM address in hex")
    parser.add_argument("--kftvsa", type=parse_hex_address, help="Manual KFTVSA address in hex")
    parser.add_argument("--kftvsakat", type=parse_hex_address, help="Manual KFTVSAKAT address in hex")
    parser.add_argument(
        "--allow-any-size", action="store_true",
        help="Allow a BIN size other than 1 MiB (the original program did not validate size).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    print("=" * 60)
    print(f"{APP_NAME:^60}")
    print("=" * 60)
    print(f"Version          : {APP_VERSION}")

    input_path: Path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise PopsAndBangsError(f"Input file not found: {input_path}")

    original = input_path.read_bytes()
    print(f"Input file       : {input_path.name}")
    print(f"File size        : {len(original)} bytes")
    if len(original) != SUPPORTED_SIZE and not args.allow_any_size:
        raise PopsAndBangsError(
            f"Expected a 1 MiB BIN ({SUPPORTED_SIZE} bytes). "
            "Use --allow-any-size only when you intentionally want original-app behavior."
        )

    print("\nAutomatic detection...")

    if args.kfzwmn is None:
        kfzwmn_matches = find_kfzwmn(original)
        print(f"KFZWMN matches   : {len(kfzwmn_matches)}")
        kfzwmn = choose_detected("KFZWMN", kfzwmn_matches)
    else:
        kfzwmn = args.kfzwmn
        print("KFZWMN source    : manual")

    if args.kfnwegm is None:
        kfnwegm_matches, algorithm = find_kfnwegm(original)
        print(f"KFNWEGM algorithm: {algorithm or 'not found'}")
        print(f"KFNWEGM matches  : {len(kfnwegm_matches)}")
        kfnwegm = choose_detected("KFNWEGM", kfnwegm_matches)
    else:
        kfnwegm = args.kfnwegm
        print("KFNWEGM source   : manual")

    # Exact original calculation: +40 (0x28), then another +40.
    kftvsa = args.kftvsa if args.kftvsa is not None else kfnwegm + 40
    kftvsakat = args.kftvsakat if args.kftvsakat is not None else kftvsa + 40

    print("\nDetected addresses")
    print("-" * 72)
    print(f"KFZWMN           : 0x{kfzwmn:06X}")
    print(f"KFNWEGM          : 0x{kfnwegm:06X}")
    print(f"KFTVSA           : 0x{kftvsa:06X}")
    print(f"KFTVSAKAT        : 0x{kftvsakat:06X}")

    profile = PROFILES[args.profile] if args.profile else select_profile()
    print(f"\nSelected profile : {profile.name}")
    print(f"KFZWMN value     : {profile.kfzwmn_value} (0x{profile.kfzwmn_value:02X})")
    print(f"KFNWEGM value    : 170 (0xAA)")
    print(f"KFTVSA base      : {profile.kftvsa_value} (0x{profile.kftvsa_value:02X})")
    print("KFTVSAKAT value  : 255 (0xFF)")

    modified, changes = apply_modifications(
        original, profile, kfzwmn, kfnwegm, kftvsa, kftvsakat
    )

    output_path = (args.output.expanduser().resolve() if args.output else default_output_path(input_path, profile))
    if output_path == input_path:
        raise PopsAndBangsError("Output path must be different from the input path.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(modified)

    log_path = output_path.with_suffix(".log")
    log_lines = [
        f"{APP_NAME} v{APP_VERSION}",
        f"Input: {input_path}",
        f"Output: {output_path}",
        f"Profile: {profile.name}",
        f"KFZWMN: 0x{kfzwmn:06X}",
        f"KFNWEGM: 0x{kfnwegm:06X}",
        f"KFTVSA: 0x{kftvsa:06X}",
        f"KFTVSAKAT: 0x{kftvsakat:06X}",
        f"Changed bytes: {len(changes)}",
        "",
        "ADDRESS  OLD NEW MAP",
    ]
    log_lines.extend(
        f"0x{address:06X}  {old:02X}  {new:02X}  {map_name}"
        for address, old, new, map_name in changes
    )
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print("\nResult")
    print("-" * 72)
    print(f"Changed bytes    : {len(changes)}")
    print(f"Output BIN       : {output_path}")
    print(f"Log file         : {log_path}")
    print("Checksum         : NOT corrected (same behavior as the original app)")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except PopsAndBangsError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print(f"\n[ERROR] File operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
