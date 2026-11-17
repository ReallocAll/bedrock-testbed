"""Independent, bounded native-thread CPU/scheduler sampler for RakNet A/B.

schedstat wait is runnable run-queue delay, not evidence of GIL wait.
wchan is an instantaneous wait location, not lock ownership attribution.
"""
from __future__ import annotations

import argparse
import csv
import json
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


def process_group_pids(pgid: int):
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            raw = (entry / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] not in ("Z", "X") and int(fields[2]) == pgid:
                pids.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return sorted(pids)


def read_group_threads(pgid: int):
    rows = []
    for pid in process_group_pids(pgid):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            end = raw.rfind(")")
            fields = raw[end + 2:].split()
            ppid = int(fields[1])
            process_comm = raw[raw.find("(") + 1:end]
        except (OSError, ValueError, IndexError):
            ppid, process_comm = None, ""
        try:
            exe_basename = Path(os.readlink(f"/proc/{pid}/exe")).name
        except OSError:
            exe_basename = ""
        rows.extend(
            (pid, ppid, process_comm, exe_basename, *thread)
            for thread in read_threads(pid)
        )
    return rows


def main():
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pid", type=int)
    target.add_argument("--pgid", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=68)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--signal-seconds", default="")
    parser.add_argument("--thread-map", type=Path)
    args = parser.parse_args()
    target_id = args.pid if args.pid is not None else args.pgid
    if target_id <= 0 or not 0 < args.duration <= 240 or not 0 < args.interval <= 5:
        parser.error("invalid pid, duration, or interval")
    if args.pgid is not None and args.signal_seconds:
        parser.error("--signal-seconds is only supported with --pid")
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
    if args.pgid is not None:
        columns = (
            *columns[:4], "pid", "ppid", "process_comm", "exe_basename",
            *columns[4:12], "delta_sched_cpu_ns", *columns[12:],
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.thread_map is not None:
        args.thread_map.parent.mkdir(parents=True, exist_ok=True)
        thread_map_written = False
        map_identity = {"pgid": args.pgid} if args.pgid is not None else {"pid": args.pid}
        args.thread_map.write_text(json.dumps({
            **map_identity, "sample_monotonic_s": None, "threads": [],
        }, indent=2) + "\n")
    else:
        thread_map_written = True
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        sample_index = 0
        sample_rows = 0
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
            current = (read_group_threads(args.pgid) if args.pgid is not None else
                       [(args.pid, None, "", "", *thread)
                        for thread in read_threads(args.pid)])
            if not current:
                break
            if not thread_map_written:
                args.thread_map.write_text(json.dumps({
                    **map_identity,
                    "sample_monotonic_s": round(captured, 6),
                    "threads": [
                        {**({"pid": pid, "ppid": ppid, "process_comm": process_comm,
                             "exe_basename": exe_basename} if args.pgid is not None else {}),
                         "tid": tid, "comm": comm, "initial_state": state}
                        for pid, ppid, process_comm, exe_basename, tid, comm, state, *_ in current
                    ],
                }, indent=2) + "\n")
                thread_map_written = True
            interval = captured - previous_at if previous_at is not None else None
            next_previous = {}
            for pid, ppid, process_comm, exe_basename, tid, comm, state, user, system, sched, wchan, voluntary, nonvoluntary in current:
                total = user + system
                wait = sched[1]
                thread_key = (pid, tid)
                old = previous.get(thread_key)
                previous_values = (total, sched[0], wait, voluntary, nonvoluntary)
                next_previous[thread_key] = previous_values
                delta = max(0, total - old[0]) if old is not None else None
                delta_sched_cpu = (max(0, sched[0] - old[1])
                                   if old and sched[0] is not None and old[1] is not None else None)
                delta_wait = max(0, wait - old[2]) if old and wait is not None and old[2] is not None else None
                delta_vol = max(0, voluntary - old[3]) if old and voluntary is not None and old[3] is not None else None
                delta_nonvol = max(0, nonvoluntary - old[4]) if old and nonvoluntary is not None and old[4] is not None else None
                row = {
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
                }
                if args.pgid is not None:
                    row["pid"] = pid
                    row["ppid"] = ppid
                    row["process_comm"] = process_comm
                    row["exe_basename"] = exe_basename
                    row["delta_sched_cpu_ns"] = delta_sched_cpu
                writer.writerow(row)
                sample_rows += 1
            stream.flush()
            previous, previous_at = next_previous, captured
            sample_index += 1
    if args.thread_map is not None and sample_rows == 0:
        raise SystemExit("no thread sample rows were written")


if __name__ == "__main__":
    main()
