"""Isolated real-BDS EndPilot capacity probe; no SDK instrumentation or production access."""
from __future__ import annotations

import asyncio
import csv
import faulthandler
import json
import os
import signal
import threading
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from server import ROOT, PLAYER, PLAYER_COUNT, Server



def patch_reader_yield():
    """Patch only the pinned installed copy, preserving packet handling and ordering."""
    from importlib.metadata import distribution

    session_path = Path(distribution("endpilot").locate_file("endpilot/session.py"))
    source = session_path.read_text()
    anchor = "                    await self.handle(decoded.payload)\n                except _MalformedPacket as error:\n                    self.decode_error(decoded.header.packet_id, str(error), recoverable=False)\n                    raise\n\n    def decode_error(self, packet_id, reason, *, recoverable):\n"
    replacement = anchor.replace(
        "\n    def decode_error",
        "\n            if getattr(Session, \"_capacity_reader_yield\", False):\n                await asyncio.sleep(0)\n\n    def decode_error",
    )
    patched = "            if getattr(Session, \"_capacity_reader_yield\", False):\n                await asyncio.sleep(0)\n\n    def decode_error(self, packet_id, reason, *, recoverable):\n"
    if source.count(patched) == 1 and source.count(anchor) == 0:
        return session_path
    if source.count(anchor) != 1 or source.count(patched):
        raise RuntimeError(f"Pinned Session.reader patch anchor is not unique: {session_path}")
    updated = source.replace(anchor, replacement, 1)
    compile(updated, str(session_path), "exec")
    session_path.write_text(updated)
    return session_path


def install_mailbox_tracking():
    """Track mailbox instances without changing mailbox operations or ordering."""
    from raknet.asyncio import _AsyncMailbox

    mailboxes = []
    original_init = _AsyncMailbox.__init__

    def mailbox_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        mailboxes.append(self)

    _AsyncMailbox.__init__ = mailbox_init
    return _AsyncMailbox, original_init, mailboxes


def install_receive_timing(stage):
    """Sample total RakPeer.receive Python-call duration, including native Receive and GIL reacquisition/scheduling."""
    from raknet.raw import RakPeer

    original_receive = RakPeer.receive
    receive_counter = threading.local()
    receive_rows = []

    def receive(self, *args, **kwargs):
        call_count = getattr(receive_counter, "count", 0) + 1
        receive_counter.count = call_count
        if call_count % 1024:
            return original_receive(self, *args, **kwargs)
        native_id = threading.get_native_id()
        stage_label = stage()
        begin = time.perf_counter_ns()
        returned_packet = False
        try:
            result = original_receive(self, *args, **kwargs)
            returned_packet = result is not None
            return result
        finally:
            receive_rows.append((time.monotonic(), stage_label, native_id,
                                 (time.perf_counter_ns() - begin) / 1000, returned_packet))

    RakPeer.receive = receive
    return RakPeer, original_receive, receive_rows


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
    if not 1 <= count <= 100:
        raise ValueError("capacity count must be 1..100")
    if count == 50 and transport == "raknet":
        patch_reader_yield()
    from endpilot import BotOptions, BotState, EventKind, Input, Runtime, event_mask
    from endpilot.session import Session
    Session._capacity_reader_yield = False
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
        "acceptance_failures": [],
    }
    ticks = defaultdict(lambda: defaultdict(list))
    probe_samples = []
    phase = "join"
    started = time.monotonic()

    def observe(bot, event):
        if event.__class__.__name__ != "TickEvent" or not (phase in ("idle", "active") or phase.startswith("idle_")):
            return
        ticks[phase][bot].append(int(event.lateness_us))
        if phase == "active":
            status = bot.get_status()
            bot.set_input(Input(position=status.position, yaw=float(event.tick % 360)))

    async def sampler(mailboxes):
        previous_sample = time.monotonic()
        while True:
            now = time.monotonic()
            sizes = [getattr(box, "_size", 0) for box in mailboxes]
            probe_samples.append({
                "elapsed_s": round(now - started, 3), "phase": phase,
                "sample_gap_ms": round((now - previous_sample) * 1000, 3),
                "mailbox_size_total": sum(sizes), "mailbox_size_max": max(sizes, default=0),
                "mailboxes_nonempty": sum(size > 0 for size in sizes),
            })
            previous_sample = now
            await asyncio.sleep(1)

    async def stage(label, bots, seconds=60):
        nonlocal phase
        phase = label
        beginning = process_usage(os.getpid())
        inputs_before = {bot: bot.capture_statistics().play_packet_counts_out[144] for bot in bots}
        begin = time.monotonic()
        await asyncio.sleep(seconds)
        elapsed = time.monotonic() - begin
        ending = process_usage(os.getpid())
        inputs = {names[i]: bot.capture_statistics().play_packet_counts_out[144] - inputs_before[bot]
                  for i, bot in enumerate(bots)}
        lateness = [value for bot in bots for value in ticks[label][bot]]
        executed = len(lateness)
        expected = elapsed * 20 * len(bots)
        data = {
            "duration_s": round(elapsed, 3),
            "start_monotonic_s": round(begin, 6),
            "end_monotonic_s": round(begin + elapsed, 6),
            "tick_observed": executed,
            "tick_expected_approx": round(expected, 1),
            "tick_missing_fraction_approx": round(max(0, 1 - executed / expected), 5),
            "lateness_us_p50": pctl(lateness, 0.50),
            "lateness_us_p95": pctl(lateness, 0.95),
            "lateness_us_p99": pctl(lateness, 0.99),
            "lateness_us_max": max(lateness, default=None),
            "client_cpu_cores": cpu_cores(beginning, ending, elapsed),
            "client_rss_kib_end": ending["rss_kib"] if ending else None,
            "input_packets_per_bot": inputs,
            "input_packets_per_second": round(sum(inputs.values()) / elapsed, 3),
            "tasks_end": len(asyncio.all_tasks()),
        }
        report["phases"][label] = data
        # Record capacity failures without cutting off subsequent diagnostic stages.
        # Connection/process failures still terminate the run as before.
        if data["input_packets_per_second"] < 0.8 * expected / elapsed:
            report["acceptance_failures"].append(
                f"{label} PlayerAuthInput under-rate: "
                f"{data['input_packets_per_second']} vs expected {expected / elapsed:.1f}/s"
            )
        if data["tick_missing_fraction_approx"] > 0.1 or (data["lateness_us_p99"] or 0) > 100_000:
            report["acceptance_failures"].append(
                f"{label} tick degradation: missing={data['tick_missing_fraction_approx']}, "
                f"p99_us={data['lateness_us_p99']}"
            )
        if not all(number > 0 for number in inputs.values()):
            report["acceptance_failures"].append(
                f"some bots sent no PlayerAuthInput in {label}: {inputs}"
            )

    # The timed diagnostics below avoid synchronous process-group scans in the event loop.
    mailbox_tracking = None
    receive_timing = None
    sample_task = None
    profile_process = None
    profile_log = None
    stack_log = None
    try:
        if count == 50 and transport == "raknet":
            mailbox_tracking = install_mailbox_tracking()
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
            if count == 50 and transport == "raknet":
                # All peers and their mailboxes now exist; begin low-rate sampling after warmup.
                mailboxes = mailbox_tracking[2]
                sample_task = asyncio.create_task(sampler(mailboxes), name="capacity-probe-sampler")
                receive_timing = install_receive_timing(lambda: phase)
                # Exact native-ID ↔ Python-thread mapping; sampled from the event loop once.
                (ROOT / "work" / "capacity_thread_map.json").write_text(
                    json.dumps([{"name": t.name, "ident": t.ident, "native_id": t.native_id}
                                for t in threading.enumerate()], indent=2) + "\n")
                # CPython's signal-handler traceback dumps are intentionally sparse.
                stack_log = (ROOT / "work" / "capacity_thread_stacks.log").open("w")
                faulthandler.register(signal.SIGUSR2, file=stack_log, all_threads=True)
                profile_log = (ROOT / "work" / "capacity_thread_cpu.log").open("w")
                profile_process = subprocess.Popen(
                    [
                        sys.executable, str(Path(__file__).with_name("capacity_thread_cpu.py")),
                        "--pid", str(os.getpid()), "--duration", "205",
                        "--interval", "1", "--signal-seconds", "12,47,82,117,170,190",
                        "--output", str(ROOT / "work" / "capacity_thread_cpu.csv"),
                    ],
                    stdout=profile_log, stderr=subprocess.STDOUT,
                )
                # Same-process BABA; each setting applies at the end of a complete decoded batch.
                for label, enabled in (("idle_b1", True), ("idle_a1", False),
                                       ("idle_b2", True), ("idle_a2", False)):
                    Session._capacity_reader_yield = enabled
                    await stage(label, bots, seconds=35)
                Session._capacity_reader_yield = False
            else:
                await stage("idle", bots)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            await stage("active", bots)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            phase = "teardown"
        report["players_after_close"] = await asyncio.to_thread(check_players, server, names, 0)
        if report["acceptance_failures"]:
            raise AssertionError("capacity acceptance failed: " + "; ".join(report["acceptance_failures"]))
        report["status"] = "PASS"
        results["checks"].append(f"{count}_bots_ready_input_and_cleanup")
    except BaseException as error:
        report["status"] = "FAIL"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        Session._capacity_reader_yield = False
        if receive_timing is not None:
            peer_cls, original_receive, receive_rows = receive_timing
            peer_cls.receive = original_receive
        else:
            receive_rows = []
        if mailbox_tracking is not None:
            mailbox_cls, original_init, _ = mailbox_tracking
            mailbox_cls.__init__ = original_init
        if sample_task is not None:
            sample_task.cancel()
            await asyncio.gather(sample_task, return_exceptions=True)
        if profile_process is not None:
            if profile_process.poll() is None:
                try:
                    profile_process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    profile_process.terminate()
                    try:
                        profile_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        profile_process.kill()
                        profile_process.wait()
            report["thread_cpu_profile"] = {
                "sampler_pid": profile_process.pid,
                "exit_code": profile_process.returncode,
                "independent_process": True,
                "duration_limit_s": 205,
                "signal_seconds": [12, 47, 82, 117, 170, 190],
                "thread_map": "capacity_thread_map.json",
                "python_stacks": "capacity_thread_stacks.log",
                "interval_s": 1,
                "raw_csv": "capacity_thread_cpu.csv",
                "log": "capacity_thread_cpu.log",
            }
        if profile_log is not None:
            profile_log.close()
        if stack_log is not None:
            faulthandler.unregister(signal.SIGUSR2)
            stack_log.close()
        report["environment"]["cpu_stat_final"] = cgroup_throttle()
        report["probe_samples"] = len(probe_samples)
        work = ROOT / "work"
        (work / "capacity.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        if probe_samples:
            with (work / "capacity_probe_samples.csv").open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(probe_samples[0]))
                writer.writeheader()
                writer.writerows(probe_samples)
        if receive_rows:
            with (work / "capacity_rakpeer_receive.csv").open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(("monotonic_s", "phase", "native_thread_id",
                                 "total_python_call_duration_us_including_native_receive_gil_reacquisition_and_scheduling",
                                 "returned_packet"))
                writer.writerows(receive_rows)
        results["capacity"] = {key: value for key, value in report.items() if key != "environment"}
