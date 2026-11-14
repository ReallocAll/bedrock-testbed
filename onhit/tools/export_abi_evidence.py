#!/usr/bin/env python3
"""Read-only exact-BDS ELF ABI evidence export. Never installs or executes hooks.

Usage: python3 tools/export_abi_evidence.py /path/to/bedrock_server > abi-evidence.txt
Requires GNU readelf and objdump. Output is disassembly evidence, NOT ABI approval.
"""
import hashlib
import pathlib
import re
import subprocess
import sys

EXPECTED_SHA256 = "ea16b541a448e07c337848d2c5f37321b706554b5216dd942f3f11e7a8ed2bad"
EXPECTED_BUILD_ID = "52576b75b6af5a09a6bde582be804e29657dbbf3"
ENTRIES = {"reset": 0x7143370, "parse": 0x7143660, "cleanup": 0x7757840}
CALLSITES = {0xB38FE01: ("call", ENTRIES["parse"]),
             0x7757F05: ("call", ENTRIES["reset"]),
             0x7756A0E: ("call", ENTRIES["cleanup"]),
             0x7756A24: ("call", ENTRIES["cleanup"]),
             0x7756AD7: ("call", ENTRIES["cleanup"]),
             0x7756B98: ("call", ENTRIES["cleanup"]),
             0x7758522: ("call", ENTRIES["cleanup"]),
             0x77569D0: ("jmp", ENTRIES["cleanup"])}


def run(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: export_abi_evidence.py /path/to/bedrock_server")
    path = pathlib.Path(sys.argv[1]).resolve(strict=True)
    if not path.is_file():
        sys.exit("not a regular file")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    notes = run("readelf", "-n", str(path))
    ids = re.findall(r"Build ID:\s*([0-9a-fA-F]+)", notes)
    if digest != EXPECTED_SHA256 or EXPECTED_BUILD_ID not in [x.lower() for x in ids]:
        sys.exit("REFUSED: ELF SHA-256 or GNU Build ID does not match target")
    print("EXACT ELF identity PASS:", digest, EXPECTED_BUILD_ID)
    print("Read-only static evidence; ABI, trampoline unwind and quiescence NOT verified.")
    print("\n=== Program headers ===")
    print(run("readelf", "-W", "-l", str(path)))
    for name, ea in ENTRIES.items():
        print(f"\n=== {name} entry 0x{ea:x} (raw bytes and instructions) ===")
        print(run("objdump", "-d", "-M", "intel", "--show-raw-insn",
                  f"--start-address={ea}", f"--stop-address={ea+128}", str(path)))
    for ea, (opcode, target) in CALLSITES.items():
        print(f"\n=== callsite vicinity 0x{ea:x} ===")
        disassembly = run("objdump", "-d", "-M", "intel", "--show-raw-insn",
                          f"--start-address={ea}", f"--stop-address={ea+16}", str(path))
        # Match the instruction at the precise EA, not a nearby instruction or
        # a textual mention of the destination elsewhere in the disassembly.
        lines = [line for line in disassembly.splitlines()
                 if re.match(rf"^\s*{ea:x}:", line)]
        if len(lines) != 1:
            sys.exit(f"REFUSED: expected exactly one instruction at 0x{ea:x}")
        match = re.match(r"^\s*[0-9a-f]+:\s+((?:[0-9a-f]{2}\s+)+)\s*(call|jmp)\s+([0-9a-f]+)\b", lines[0])
        if not match or match.group(2) != opcode or int(match.group(3), 16) != target:
            sys.exit(f"REFUSED: callsite 0x{ea:x} does not branch to expected 0x{target:x}: {lines[0]}")
        raw = bytes.fromhex(match.group(1))
        expected_prefix = 0xe8 if opcode == "call" else 0xe9
        if len(raw) != 5 or raw[0] != expected_prefix or ea + 5 + int.from_bytes(raw[1:], "little", signed=True) != target:
            sys.exit(f"REFUSED: callsite 0x{ea:x} has unexpected branch encoding")
        print(f"Verified {opcode} at 0x{ea:x} -> 0x{target:x}")
        print(disassembly)
    print("\nReview register and stack dataflow beyond each window; no automatic ABI verdict.")


if __name__ == "__main__":
    main()
