"""Isolated real-BDS EndPilot capacity probe; no SDK instrumentation or production access."""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from server import ROOT, PLAYER, PLAYER_COUNT, Server


def process_usage(pid: int) -> dict | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        stat = raw[raw.rfind(")") + 2:].split()
        status = Path(f"/proc/{pid}/status").read_text()
        rss = re.search(r"^VmRSS:\s+(\d+) kB", status, re.M)
        return {
            "cpu_ticks": int(stat[11]) + int(stat[12]),
            "rss_kib": int(rss.group(1)) if rss else 0,
            "threads": int(stat[17]),
            "fds": len(list(Path(f"/proc/{pid}/fd").iterdir())),
        }
    except (OSError, ValueError, IndexError):
        return None


def resource_snapshot(server: Server) -> dict:
    client = process_usage(os.getpid())
    members = Server.group_members(server.process.pid) if server.process else []
    processes = [item for pid in members if (item := process_usage(pid)) is not None]
    return {
        "client": client,
        "server": {
            key: sum(p[key] for p in processes)
            for key in ("cpu_ticks", "rss_kib", "threads", "fds")
        },
        "server_pids": len(processes),
        "tasks": len(asyncio.all_tasks()),
    }


def cpu_cores(before: dict, after: dict, duration: float) -> float | None:
    if not before or not after or duration <= 0:
        return None
    ticks = after["cpu_ticks"] - before["cpu_ticks"]
    return round(ticks / os.sysconf("SC_CLK_TCK") / duration, 4)


def pctl(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.5)))]


def cgroup_throttle() -> dict:
    try:
        return dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())
    except (OSError, ValueError):
        return {}


def check_players(server: Server, names: list[str], expected: int) -> dict:
    offset = server.command("list")
    deadline = time.monotonic() + 18
    last = []
    while time.monotonic() < deadline:
        last = server.snapshot(offset)
        counts = [int(match.group(1)) for line in last if (match := PLAYER_COUNT.search(line))]
        all_lines = "\n".join(last).lower()
        if counts and counts[-1] == expected and (expected == 0 or all(n.lower() in all_lines for n in names)):
            return {"count": counts[-1], "names_verified": expected == 0 or all(n.lower() in all_lines for n in names)}
        if server.process is None or server.process.poll() is not None:
            raise RuntimeError("BDS exited while checking online players")
        time.sleep(0.2)
    raise AssertionError(f"BDS list did not confirm {expected} named players; last={last[-6:]}")


async def exercise_capacity(server: Server, results: dict, transport: str, count: int) -> None:
    """Measure 10+ independent sessions in one Runtime and isolate the client process."""
    from endpilot import BotOptions, BotState, EventKind, Input, Runtime, event_mask

    if not 1 <= count <= 100:
        raise ValueError("capacity count must be 1..100")
    endpoint = ("raknet://" if transport == "raknet" else "http://") + "127.0.0.1:19132"
    stagger_ms = 8000 if transport == "raknet" else 1000
    names = [PLAYER, *(f"EndPilotLoad{i:03d}" for i in range(1, count))]
    report = {
        "bots": count, "transport": transport, "stagger_ms": stagger_ms,
        "workload": "shared-area / idle and input-update callback; no navigation",
        "environment": {
            "cpu_count": os.cpu_count(),
            "affinity_count": len(os.sched_getaffinity(0)),
            "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip()
            if Path("/sys/fs/cgroup/cpu.max").exists() else None,
            "cpu_stat_initial": cgroup_throttle(),
        },
        "phases": {},
    }
    ticks = defaultdict(lambda: defaultdict(list))
    samples = []
    phase = "join"
    started = time.monotonic()

    def observe(bot, event):
        if event.__class__.__name__ != "TickEvent" or phase not in ("idle", "active"):
            return
        ticks[phase][bot].append(int(event.lateness_us))
        if phase == "active":
            status = bot.get_status()
            bot.set_input(Input(position=status.position, yaw=float(event.tick % 360)))

    async def sampler():
        while True:
            snap = resource_snapshot(server)
            samples.append({
                "elapsed_s": round(time.monotonic() - started, 3), "phase": phase,
                "client_cpu_ticks": snap["client"]["cpu_ticks"] if snap["client"] else None,
                "client_rss_kib": snap["client"]["rss_kib"] if snap["client"] else None,
                "client_threads": snap["client"]["threads"] if snap["client"] else None,
                "client_fds": snap["client"]["fds"] if snap["client"] else None,
                "server_cpu_ticks": snap["server"]["cpu_ticks"],
                "server_rss_kib": snap["server"]["rss_kib"],
                "server_threads": snap["server"]["threads"],
                "server_pids": snap["server_pids"],
                "tasks": snap["tasks"],
                "cgroup_throttled_usec": cgroup_throttle().get("throttled_usec"),
            })
            await asyncio.sleep(1)

    async def stage(label, bots, seconds=60):
        nonlocal phase
        phase = label
        beginning = resource_snapshot(server)
        inputs_before = {bot: bot.capture_statistics().play_packet_counts_out[144] for bot in bots}
        begin = time.monotonic()
        await asyncio.sleep(seconds)
        elapsed = time.monotonic() - begin
        ending = resource_snapshot(server)
        inputs = {names[i]: bot.capture_statistics().play_packet_counts_out[144] - inputs_before[bot]
                  for i, bot in enumerate(bots)}
        lateness = [value for bot in bots for value in ticks[label][bot]]
        executed = len(lateness)
        expected = elapsed * 20 * len(bots)
        data = {
            "duration_s": round(elapsed, 3),
            "tick_observed": executed,
            "tick_expected_approx": round(expected, 1),
            "tick_missing_fraction_approx": round(max(0, 1 - executed / expected), 5),
            "lateness_us_p50": pctl(lateness, 0.50),
            "lateness_us_p95": pctl(lateness, 0.95),
            "lateness_us_p99": pctl(lateness, 0.99),
            "lateness_us_max": max(lateness, default=None),
            "client_cpu_cores": cpu_cores(beginning["client"], ending["client"], elapsed),
            "server_cpu_cores": cpu_cores(beginning["server"], ending["server"], elapsed),
            "client_rss_kib_end": ending["client"]["rss_kib"] if ending["client"] else None,
            "server_rss_kib_end": ending["server"]["rss_kib"],
            "input_packets_per_bot": inputs,
            "input_packets_per_second": round(sum(inputs.values()) / elapsed, 3),
            "tasks_end": ending["tasks"],
        }
        report["phases"][label] = data
        if not all(number > 0 for number in inputs.values()):
            raise AssertionError(f"some bots sent no PlayerAuthInput in {label}: {inputs}")

    sample_task = asyncio.create_task(sampler(), name="capacity-sampler")
    try:
        async with Runtime(observe, events=event_mask(EventKind.tick)) as runtime:
            bots = [
                runtime.add_bot(BotOptions(
                    server_url=endpoint, transport=transport, protocol=2193,
                    name=name, chunk_radius=4, start_delay_ms=index * stagger_ms,
                    client_side_generation=False, client_cache=False,
                ))
                for index, name in enumerate(names)
            ]
            # The final RakNet join starts after (count - 1) * stagger_ms.
            # A fixed 240 s readiness deadline expires before 50+ Bots can start.
            ready_timeout_s = max(240, (count - 1) * stagger_ms / 1000 + 60)
            await asyncio.gather(*(bot.wait_ready(ready_timeout_s) for bot in bots))
            report["ready_seconds"] = round(time.monotonic() - started, 3)
            report["players_joined"] = await asyncio.to_thread(check_players, server, names, count)
            phase = "warmup"
            await asyncio.sleep(20)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            await stage("idle", bots)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            await stage("active", bots)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            phase = "teardown"
        report["players_after_close"] = await asyncio.to_thread(check_players, server, names, 0)
        report["status"] = "PASS"
        results["checks"].append(f"{count}_bots_ready_input_and_cleanup")
    except BaseException as error:
        report["status"] = "FAIL"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        sample_task.cancel()
        await asyncio.gather(sample_task, return_exceptions=True)
        report["environment"]["cpu_stat_final"] = cgroup_throttle()
        report["samples"] = len(samples)
        work = ROOT / "work"
        (work / "capacity.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        if samples:
            with (work / "capacity_samples.csv").open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(samples[0]))
                writer.writeheader()
                writer.writerows(samples)
        results["capacity"] = {key: value for key, value in report.items() if key != "environment"}
