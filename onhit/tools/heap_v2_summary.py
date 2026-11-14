#!/usr/bin/env python3
"""Offline jemalloc heap_v2 snapshot comparison; no process or profiler access.

The inuse_space estimator matches jemalloc jeprof's AdjustSamples algorithm 2,
per stack: sampled_bytes / (1 - exp(-(sampled_bytes/count)/sample_period)).
These estimates do not establish application-level ownership or a leak.
"""
import argparse
import json
import math
import re
from pathlib import Path

STACK = re.compile(r"^@\s+((?:0x[0-9a-fA-F]+\s*)+)$")
SUMMARY = re.compile(r"^\s*t\*:\s*(\d+):\s*(\d+)\s*\[\s*\d+:\s*\d+\]")
PID = re.compile(r"^bds\.(\d+)\.")


def snapshot(path: Path, target_ea: int, image_name: str = "bedrock_server") -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not re.fullmatch(r"heap_v2/(\d+)", lines[0]):
        raise ValueError(f"{path}: not a jemalloc heap_v2 profile")
    period = int(lines[0].split("/")[1])
    if period <= 0:
        raise ValueError("sample period must be positive")

    map_index = lines.index("MAPPED_LIBRARIES:")
    # Main executable's offset-zero VMA is the image load bias for this BDS ELF.
    mappings = []
    for line in lines[map_index + 1:]:
        fields = line.split()
        if (len(fields) >= 6 and fields[2] == "00000000" and
                Path(fields[-1]).name == image_name and "r" in fields[1]):
            mappings.append(int(fields[0].split("-")[0], 16))
    if len(mappings) != 1:
        raise ValueError(f"{path}: expected one offset-zero {image_name} mapping, got {len(mappings)}")
    target_pc = mappings[0] + target_ea

    total_count = target_count = total_bytes = target_bytes = 0
    weighted_total = weighted_target = 0.0
    in_stack = False
    current_is_target = False
    for line in lines[1:map_index]:
        if line.startswith("@ "):
            match = STACK.fullmatch(line)
            if not match:
                raise ValueError(f"{path}: malformed stack line")
            pcs = {int(pc, 16) for pc in match.group(1).split()}
            current_is_target = target_pc in pcs
            in_stack = True
        elif in_stack and (match := SUMMARY.match(line)):
            count, size = int(match.group(1)), int(match.group(2))
            if count == 0:
                continue
            if size <= 0:
                raise ValueError("nonzero sampled allocation count with zero bytes")
            # Poisson sampling: this is the exact per-stack heap_v2 jeprof adjustment.
            ratio = size / count / period
            weighted_size = size / -math.expm1(-ratio)
            total_count += count
            total_bytes += size
            weighted_total += weighted_size
            if current_is_target:
                target_count += count
                target_bytes += size
                weighted_target += weighted_size

    return {
        "snapshot": path.name,
        "sample_period": period,
        "image_base": hex(mappings[0]),
        "target_pc": hex(target_pc),
        "sampled_count": total_count,
        "sampled_payload_bytes": total_bytes,
        "weighted_inuse_space_bytes": round(weighted_total, 3),
        "target_sampled_count": target_count,
        "target_sampled_payload_bytes": target_bytes,
        "target_weighted_inuse_space_bytes": round(weighted_target, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--target-return-ea", type=lambda x: int(x, 0), default=0x71439E0,
                        help="Raw heap stack return PC offset (not a function entry EA)")
    args = parser.parse_args()
    before_pid, after_pid = PID.match(args.before.name), PID.match(args.after.name)
    if before_pid and after_pid and before_pid.group(1) != after_pid.group(1):
        parser.error("snapshots are from different BDS process IDs")
    before = snapshot(args.before, args.target_return_ea)
    after = snapshot(args.after, args.target_return_ea)
    if (before["sample_period"] != after["sample_period"] or
            before["image_base"] != after["image_base"]):
        parser.error("sampling rate or ELF load bias differs")
    diff = {key: round(after[key] - before[key], 3) for key in (
        "sampled_count", "sampled_payload_bytes", "weighted_inuse_space_bytes",
        "target_sampled_count", "target_sampled_payload_bytes", "target_weighted_inuse_space_bytes")}
    print(json.dumps({"before": before, "after": after, "delta": diff,
                      "caveat": "Sampled weighted retention estimate; cannot prove object leaks."}, indent=2))


if __name__ == "__main__":
    main()
