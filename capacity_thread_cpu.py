"""Independent, bounded native-thread CPU/scheduler sampler for RakNet A/B.

schedstat wait is runnable run-queue delay, not evidence of GIL wait.
wchan is an instantaneous wait location, not lock ownership attribution.
"""
from __future__ import annotations

import argparse
import csv
import os
import signal
import time
from pathlib import Path


def read_threads(pid: int):
    try:
        tids = list(Path(f"/proc/{pid}/task").iterdir())
    except OSError:
        return []
    rows = []
    for entry in tids:
        try:
            raw = (entry / "stat").read_text()
            end = raw.rfind(")")
            fields = raw[end + 2:].split()
            try:
                sched = [int(x) for x in (entry / "schedstat").read_text().split()[:3]]
            except (OSError, ValueError):
                sched = [None, None, None]
            try:
                wchan = (entry / "wchan").read_text().strip()
            except OSError:
                wchan = ""
            try:
                status = (entry / "status").read_text()
                switches = {
                    k: int(v) for line in status.splitlines()
                    if (parts := line.split(":")) and len(parts) == 2
                    for k, v in [(parts[0], parts[1].strip())]
                    if k in ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")
                }
            except (OSError, ValueError):
                switches = {}
            rows.append((
                int(entry.name), raw[raw.find("(") + 1:end], fields[0],
                int(fields[11]), int(fields[12]), sched, wchan,
                switches.get("voluntary_ctxt_switches"),
                switches.get("nonvoluntary_ctxt_switches"),
            ))
        except (OSError, ValueError, IndexError):
            continue
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=68)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--signal-seconds", default="")
    args = parser.parse_args()
    if args.pid <= 0 or not 0 < args.duration <= 150 or not 0 < args.interval <= 5:
        parser.error("invalid pid, duration, or interval")
    signals = sorted(float(x) for x in args.signal_seconds.split(",") if x)
    tick_rate = os.sysconf("SC_CLK_TCK")
    started = time.monotonic()
    previous = {}
    previous_at = None
    columns = (
        "sample", "monotonic_s", "elapsed_s", "interval_s", "tid",
        "comm", "state", "utime_ticks", "stime_ticks",
        "delta_cpu_ticks", "cpu_cores", "sched_cpu_ns",
        "sched_wait_ns", "sched_slices", "delta_sched_wait_ns",
        "wchan", "voluntary_switches", "nonvoluntary_switches",
        "delta_voluntary_switches", "delta_nonvoluntary_switches",
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
            elapsed = captured - started
            if elapsed >= args.duration:
                break
            while signals and elapsed >= signals[0]:
                threshold = signals.pop(0)
                try:
                    os.kill(args.pid, signal.SIGUSR2)
                    print(f"stack signal at {elapsed:.3f}s, threshold={threshold}", flush=True)
                except ProcessLookupError:
                    pass
            current = read_threads(args.pid)
            if not current:
                break
            interval = captured - previous_at if previous_at is not None else None
            next_previous = {}
            for tid, comm, state, user, system, sched, wchan, voluntary, nonvoluntary in current:
                total = user + system
                wait = sched[1]
                old = previous.get(tid)
                previous_values = (total, wait, voluntary, nonvoluntary)
                next_previous[tid] = previous_values
                delta = max(0, total - old[0]) if old is not None else None
                delta_wait = max(0, wait - old[1]) if old and wait is not None and old[1] is not None else None
                delta_vol = max(0, voluntary - old[2]) if old and voluntary is not None and old[2] is not None else None
                delta_nonvol = max(0, nonvoluntary - old[3]) if old and nonvoluntary is not None and old[3] is not None else None
                writer.writerow({
                    "sample": sample_index, "monotonic_s": round(captured, 6),
                    "elapsed_s": round(elapsed, 4),
                    "interval_s": round(interval, 4) if interval else "",
                    "tid": tid, "comm": comm, "state": state,
                    "utime_ticks": user, "stime_ticks": system,
                    "delta_cpu_ticks": delta if delta is not None else "",
                    "cpu_cores": round(delta / tick_rate / interval, 4)
                        if delta is not None and interval and interval > 0 else "",
                    "sched_cpu_ns": sched[0], "sched_wait_ns": wait,
                    "sched_slices": sched[2],
                    "delta_sched_wait_ns": delta_wait,
                    "wchan": wchan, "voluntary_switches": voluntary,
                    "nonvoluntary_switches": nonvoluntary,
                    "delta_voluntary_switches": delta_vol,
                    "delta_nonvoluntary_switches": delta_nonvol,
                })
            stream.flush()
            previous, previous_at = next_previous, captured
            sample_index += 1


if __name__ == "__main__":
    main()
