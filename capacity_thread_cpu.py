"""Bounded, out-of-process native-thread CPU sampler for a capacity experiment.

Read-only /proc sampling. Runs independently of the EndPilot asyncio event loop
and Python GIL. It measures native-thread CPU time, not Python/C++ call stacks.
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path


def read_threads(pid: int) -> list[tuple[int, str, str, int, int]]:
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = list(task_dir.iterdir())
    except OSError:
        return []
    rows = []
    for entry in tids:
        try:
            raw = (entry / "stat").read_text()
            end = raw.rfind(")")
            fields = raw[end + 2:].split()
            rows.append((
                int(entry.name),
                raw[raw.find("(") + 1:end],
                fields[0],
                int(fields[11]),
                int(fields[12]),
            ))
        except (OSError, ValueError, IndexError):
            # Threads may exit between enumeration and reading /proc.
            continue
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=55)
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    if args.pid <= 0 or not 0 < args.duration <= 60 or not 0 < args.interval <= 5:
        parser.error("invalid pid, duration, or interval")

    ticks_per_second = os.sysconf("SC_CLK_TCK")
    started = time.monotonic()
    previous: dict[int, int] = {}
    previous_at: float | None = None
    columns = (
        "sample", "elapsed_s", "interval_s", "tid", "comm", "state",
        "utime_ticks", "stime_ticks", "delta_cpu_ticks", "cpu_cores",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        sample_index = 0
        while True:
            deadline = started + sample_index * args.interval
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            captured = time.monotonic()
            if captured - started >= args.duration:
                break
            current = read_threads(args.pid)
            if not current:
                break
            interval = captured - previous_at if previous_at is not None else None
            next_previous = {}
            for tid, comm, state, utime, stime in current:
                total_ticks = utime + stime
                next_previous[tid] = total_ticks
                old_ticks = previous.get(tid)
                delta = max(0, total_ticks - old_ticks) if old_ticks is not None else None
                writer.writerow({
                    "sample": sample_index,
                    "elapsed_s": round(captured - started, 4),
                    "interval_s": round(interval, 4) if interval is not None else "",
                    "tid": tid, "comm": comm, "state": state,
                    "utime_ticks": utime, "stime_ticks": stime,
                    "delta_cpu_ticks": delta if delta is not None else "",
                    "cpu_cores": round(delta / ticks_per_second / interval, 4)
                    if delta is not None and interval and interval > 0 else "",
                })
            stream.flush()
            previous, previous_at = next_previous, captured
            sample_index += 1


if __name__ == "__main__":
    main()
