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
    conditional_yield = (
        '            if getattr(Session, "_capacity_reader_yield", False):\n'
        '                await asyncio.sleep(0)\n'
    )
    method = "\n    def decode_error(self, packet_id, reason, *, recoverable):\n"
    baseline_anchor = (
        "                    await self.handle(decoded.payload)\n"
        "                except _MalformedPacket as error:\n"
        "                    self.decode_error(decoded.header.packet_id, str(error), recoverable=False)\n"
        "                    raise\n"
        + method
    )
    production_anchor = "\n            await asyncio.sleep(0)\n" + method
    conditional_anchor = "\n" + conditional_yield + method

    baseline_count = source.count(baseline_anchor)
    production_count = source.count(production_anchor)
    conditional_count = source.count(conditional_anchor)
    if conditional_count == 1 and baseline_count == 0 and production_count == 0:
        updated = source
    elif baseline_count == 1 and production_count == 0 and conditional_count == 0:
        updated = source.replace(baseline_anchor, baseline_anchor.replace(method, conditional_yield + method), 1)
    elif production_count == 1 and baseline_count == 0 and conditional_count == 0:
        updated = source.replace(production_anchor, "\n" + conditional_yield + method, 1)
    else:
        raise RuntimeError(f"Pinned Session.reader patch anchor is not unique: {session_path}")
    # The timing probe is applied only to the installed Testbed venv copy.
    # Keep every source anchor exact and unique so dependency drift fails closed.
    reader_anchors = {
        "recv": "            data = await self.connection.recv()\n",
        "batch_decode": (
            "                batch = self.batch.decode(\n"
            "                    data,\n"
            "                    packet_ids=None if self.options.validate_all_packets else REQUIRED_IDS,\n"
            "                    recover_packet_errors=self.options.recover_packet_errors,\n"
            "                )\n"
        ),
        "packet_next": "                decoded = next(decoded_packets)\n",
        "handler": "                    await self.handle(decoded.payload)\n",
    }
    timing_marker = "_capacity_probe_rows = []\n"
    if timing_marker not in source:
        for name, anchor in reader_anchors.items():
            if source.count(anchor) != 1:
                raise RuntimeError(f"Pinned Session.{name} timing anchor is not unique: {session_path}")
        source = updated
        source = source.replace(
            reader_anchors["recv"],
            "            _capacity_batch_number = getattr(self, '_capacity_batch_number', 0) + 1\n"
            "            self._capacity_batch_number = _capacity_batch_number\n"
            "            _capacity_batch_sample = _capacity_batch_number % 64 == 0\n"
            "            _capacity_recv_start = time.perf_counter_ns() if _capacity_batch_sample else 0\n"
            "            data = await self.connection.recv()\n"
            "            if _capacity_batch_sample:\n"
            "                _capacity_probe_record('recv', None, (time.perf_counter_ns() - _capacity_recv_start) / 1000, len(data), None)\n"
            "            _capacity_batch_start = time.perf_counter_ns() if _capacity_batch_sample else 0\n",
            1,
        )
        source = source.replace(
            reader_anchors["batch_decode"],
            "                _capacity_decode_start = time.perf_counter_ns() if _capacity_batch_sample else 0\n"
            + reader_anchors["batch_decode"]
            + "                if _capacity_batch_sample:\n"
            "                    _capacity_probe_record('batch_decode', None, (time.perf_counter_ns() - _capacity_decode_start) / 1000, len(data), batch.packet_count)\n",
            1,
        )
        source = source.replace(
            reader_anchors["packet_next"],
            "                _capacity_packet_id = header.packet_id\n"
            "                _capacity_packet_sample = False\n"
            "                if _capacity_packet_id in (18, 19, 111):\n"
            "                    _capacity_packet_count = getattr(self, '_capacity_packet_samples', None)\n"
            "                    if _capacity_packet_count is None:\n"
            "                        _capacity_packet_count = self._capacity_packet_samples = {}\n"
            "                    _capacity_packet_count[_capacity_packet_id] = _capacity_packet_count.get(_capacity_packet_id, 0) + 1\n"
            "                    _capacity_packet_sample = _capacity_packet_count[_capacity_packet_id] % 256 == 0\n"
            "                _capacity_next_start = time.perf_counter_ns() if _capacity_packet_sample else 0\n"
            + reader_anchors["packet_next"]
            + "                if _capacity_packet_sample:\n"
            "                    _capacity_probe_record('packet_decode', _capacity_packet_id, (time.perf_counter_ns() - _capacity_next_start) / 1000, len(data), batch.packet_count)\n",
            1,
        )
        source = source.replace(
            reader_anchors["handler"],
            "                    _capacity_handler_start = time.perf_counter_ns() if _capacity_packet_sample else 0\n"
            + reader_anchors["handler"]
            + "                    if _capacity_packet_sample:\n"
            "                        _capacity_probe_record('handler', _capacity_packet_id, (time.perf_counter_ns() - _capacity_handler_start) / 1000, len(data), batch.packet_count)\n",
            1,
        )
        yield_anchor = "            if getattr(Session, \"_capacity_reader_yield\", False):\n"
        if source.count(yield_anchor) != 1:
            raise RuntimeError(f"Pinned Session batch-end timing anchor is not unique: {session_path}")
        source = source.replace(
            yield_anchor,
            "            if _capacity_batch_sample:\n"
            "                _capacity_probe_record('batch_total', None, (time.perf_counter_ns() - _capacity_batch_start) / 1000, len(data), batch.packet_count)\n"
            + yield_anchor,
            1,
        )
        marker = "class Session:\n"
        if source.count(marker) != 1:
            raise RuntimeError(f"Pinned Session class anchor is not unique: {session_path}")
        helper = (
            "_capacity_probe_phase = 'join'\n"
            "_capacity_probe_rows = []\n"
            "_capacity_probe_serial = 0\n\n"
            "def _capacity_probe_record(point, packet_id, duration_us, batch_bytes, batch_packet_count):\n"
            "    global _capacity_probe_serial\n"
            "    _capacity_probe_serial += 1\n"
            "    _capacity_probe_rows.append((_capacity_probe_phase, point, packet_id, _capacity_probe_serial, duration_us, batch_bytes, batch_packet_count))\n\n\n"
        )
        source = source.replace(marker, helper + marker, 1)
    elif source.count(timing_marker) != 1:
        raise RuntimeError(f"Pinned Session timing helper anchor is not unique: {session_path}")

    # Keep the ablation at the two remote movement dispatch points only. The
    # local-player ID 19 path above remains live, as do decode, metrics, and
    # packet_decoded delivery in the reader.
    state_method = "    def handle_state(self, packet):\n"
    id19_remote = (
        "            else:\n"
        "                actor = self.actor_state.get(packet.player_id)\n"
        "                if actor is not None and actor.type == \"minecraft:player\":\n"
        "                    self.move_actor(actor, vec(packet.pos), packet.rot.x, packet.rot.y, packet.y_head_rot)\n"
    )
    id19_remote_patched = (
        "            else:\n"
        "                if not getattr(Session, \"_capacity_skip_remote_actor_motion\", False):\n"
        "                    actor = self.actor_state.get(packet.player_id)\n"
        "                    if actor is not None and actor.type == \"minecraft:player\":\n"
        "                        self.move_actor(actor, vec(packet.pos), packet.rot.x, packet.rot.y, packet.y_head_rot)\n"
    )
    motion_branch = (
        "        elif id in (18, 111):\n"
        "            motion = self.actor_state.motion(packet, self.options.protocol)\n"
        "            if motion is not None:\n"
        "                self.move_actor(*motion)\n"
    )
    motion_branch_patched = (
        "        elif id in (18, 111):\n"
        "            if not getattr(Session, \"_capacity_skip_remote_actor_motion\", False):\n"
        "                motion = self.actor_state.motion(packet, self.options.protocol)\n"
        "                if motion is not None:\n"
        "                    self.move_actor(*motion)\n"
    )
    if source.count(state_method) != 1:
        raise RuntimeError(f"Pinned Session.handle_state anchor is not unique: {session_path}")
    branch_counts = (
        source.count(id19_remote), source.count(id19_remote_patched),
        source.count(motion_branch), source.count(motion_branch_patched),
    )
    if branch_counts == (1, 0, 1, 0):
        source = source.replace(id19_remote, id19_remote_patched, 1)
        source = source.replace(motion_branch, motion_branch_patched, 1)
    elif branch_counts != (0, 1, 0, 1):
        raise RuntimeError(f"Pinned Session.handle_state movement anchors are not unique: {session_path}")
    compile(source, str(session_path), "exec")
    if source != updated:
        session_path.write_text(source)
    import importlib

    session_module = importlib.import_module("endpilot.session")
    if not hasattr(session_module, "_capacity_probe_rows"):
        session_module._capacity_probe_phase = "join"
        session_module._capacity_probe_rows = []
        session_module._capacity_probe_serial = 0
    return session_module, session_module._capacity_probe_rows


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
    reader_timing = None
    if count == 50 and transport == "raknet":
        reader_timing, _ = patch_reader_yield()
    from endpilot import BotOptions, BotState, EventKind, Input, Runtime, event_mask
    from endpilot.session import Session
    Session._capacity_reader_yield = False
    Session._capacity_skip_remote_actor_motion = False
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
    if count == 50 and transport == "raknet":
        report["actor_motion_ablation"] = {
            "reader_yield": True,
            "phases": {
                "actor_on1": {"skip_remote_actor_motion": False},
                "actor_off1": {"skip_remote_actor_motion": True},
                "actor_on2": {"skip_remote_actor_motion": False},
                "actor_off2": {"skip_remote_actor_motion": True},
            },
        }
    ticks = defaultdict(lambda: defaultdict(list))
    probe_samples = []
    phase = "join"
    started = time.monotonic()

    def observe(bot, event):
        if event.__class__.__name__ != "TickEvent" or not (
            phase in ("idle", "active") or phase.startswith(("idle_", "actor_"))
        ):
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
        if reader_timing is not None:
            reader_timing._capacity_probe_phase = label
        beginning = process_usage(os.getpid())
        inputs_before = {}
        inbound_counts_before = {}
        inbound_batch_bytes_before = {}
        for bot in bots:
            statistics = bot.capture_statistics()
            inputs_before[bot] = statistics.play_packet_counts_out[144]
            inbound_counts_before[bot] = [int(value) for value in statistics.play_packet_counts_in]
            inbound_batch_bytes_before[bot] = int(statistics.batch_bytes_in)
        begin = time.monotonic()
        await asyncio.sleep(seconds)
        elapsed = time.monotonic() - begin
        ending = process_usage(os.getpid())
        inputs = {}
        inbound_packet_counts = defaultdict(int)
        inbound_batch_bytes = 0
        for i, bot in enumerate(bots):
            statistics = bot.capture_statistics()
            inputs[names[i]] = statistics.play_packet_counts_out[144] - inputs_before[bot]
            inbound_counts_after = [int(value) for value in statistics.play_packet_counts_in]
            for packet_id in range(max(len(inbound_counts_before[bot]), len(inbound_counts_after))):
                before = inbound_counts_before[bot][packet_id] if packet_id < len(inbound_counts_before[bot]) else 0
                after = inbound_counts_after[packet_id] if packet_id < len(inbound_counts_after) else 0
                delta = after - before
                if delta > 0:
                    inbound_packet_counts[str(packet_id)] += delta
            inbound_batch_bytes += int(statistics.batch_bytes_in) - inbound_batch_bytes_before[bot]
        lateness = [value for bot in bots for value in ticks[label][bot]]
        executed = len(lateness)
        expected = elapsed * 20 * len(bots)
        data = {
            "duration_s": round(elapsed, 3),
            "skip_remote_actor_motion": bool(Session._capacity_skip_remote_actor_motion),
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
            "inbound_packet_counts_by_id": dict(inbound_packet_counts),
            "inbound_gameplay_packets": sum(inbound_packet_counts.values()),
            "inbound_batch_bytes": inbound_batch_bytes,
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
    server_profile_process = None
    server_profile_log = None
    server_profile_report = None
    server_sampler_invalid = False
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
            if reader_timing is not None:
                reader_timing._capacity_probe_phase = phase
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
                        "--pid", str(os.getpid()), "--duration", "330",
                        "--interval", "1", "--signal-seconds", "12,47,82,117,170,190",
                        "--output", str(ROOT / "work" / "capacity_thread_cpu.csv"),
                    ],
                    stdout=profile_log, stderr=subprocess.STDOUT,
                )
                if server.process is None:
                    raise RuntimeError("BDS process is unavailable for server CPU sampling")
                server_pgid = server.process.pid
                server_profile_report = {
                    "target_pgid": server_pgid,
                    "sampler_pid": None,
                    "exit_code": None,
                    "independent_process": True,
                    "duration_limit_s": 330,
                    "interval_s": 1,
                    "raw_csv": "capacity_server_thread_cpu.csv",
                    "thread_map": "capacity_server_thread_map.json",
                    "log": "capacity_server_thread_cpu.log",
                }
                server_profile_log = (ROOT / "work" / "capacity_server_thread_cpu.log").open("w")
                server_profile_process = subprocess.Popen(
                    [
                        sys.executable, str(Path(__file__).with_name("capacity_thread_cpu.py")),
                        "--pgid", str(server_pgid), "--duration", "330", "--interval", "1",
                        "--output", str(ROOT / "work" / "capacity_server_thread_cpu.csv"),
                        "--thread-map", str(ROOT / "work" / "capacity_server_thread_map.json"),
                    ],
                    stdout=server_profile_log, stderr=subprocess.STDOUT,
                )
                # Same-process ABAB actor-motion ablation with reader yielding held on.
                # Each setting applies at the end of a complete decoded batch.
                Session._capacity_reader_yield = True
                for label, skip_remote_motion in (("actor_on1", False), ("actor_off1", True),
                                                   ("actor_on2", False), ("actor_off2", True)):
                    Session._capacity_skip_remote_actor_motion = skip_remote_motion
                    await stage(label, bots, seconds=35)
                Session._capacity_skip_remote_actor_motion = False
            else:
                await stage("idle", bots)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            Session._capacity_reader_yield = True
            await stage("active", bots)
            assert all(bot.get_status().state == BotState.ready for bot in bots)
            phase = "teardown"
            if reader_timing is not None:
                reader_timing._capacity_probe_phase = phase
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
        Session._capacity_skip_remote_actor_motion = False
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
                "duration_limit_s": 330,
                "signal_seconds": [12, 47, 82, 117, 170, 190],
                "thread_map": "capacity_thread_map.json",
                "python_stacks": "capacity_thread_stacks.log",
                "interval_s": 1,
                "raw_csv": "capacity_thread_cpu.csv",
                "log": "capacity_thread_cpu.log",
            }
        if profile_log is not None:
            profile_log.close()
        if server_profile_process is not None:
            if server_profile_process.poll() is None:
                try:
                    server_profile_process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    server_profile_process.terminate()
                    try:
                        server_profile_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server_profile_process.kill()
                        server_profile_process.wait()
            server_profile_report["sampler_pid"] = server_profile_process.pid
            server_profile_report["exit_code"] = server_profile_process.returncode
        if server_profile_report is not None:
            server_csv = ROOT / "work" / "capacity_server_thread_cpu.csv"
            required_server_phases = ("actor_on1", "actor_off1", "actor_on2", "actor_off2", "active")
            phase_windows = {
                label: (report["phases"][label]["start_monotonic_s"],
                       report["phases"][label]["end_monotonic_s"])
                for label in required_server_phases if label in report["phases"]
            }
            delta_samples = {label: set() for label in required_server_phases}
            positive_cpu_samples = {label: set() for label in required_server_phases}
            positive_cpu_pids = {label: set() for label in required_server_phases}
            bds_cpu_samples = {label: set() for label in required_server_phases}
            bds_pids = {label: set() for label in required_server_phases}
            observed_pids = set()
            process_identities = {}
            delta_rows = 0
            sample_rows = 0
            try:
                with server_csv.open(newline="") as stream:
                    for row in csv.DictReader(stream):
                        sample_rows += 1
                        try:
                            pid = int(row["pid"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        observed_pids.add(pid)
                        try:
                            ppid = int(row["ppid"])
                        except (KeyError, TypeError, ValueError):
                            ppid = None
                        process_identities.setdefault(pid, {
                            "ppid": ppid,
                            "process_comm": row.get("process_comm", ""),
                            "exe_basename": row.get("exe_basename", ""),
                        })
                        if not row.get("delta_sched_cpu_ns"):
                            continue
                        try:
                            sample = int(row["sample"])
                            sampled_at = float(row["monotonic_s"])
                            interval = float(row["interval_s"])
                            int(row["sched_cpu_ns"])
                            delta_sched_cpu_ns = int(row["delta_sched_cpu_ns"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if interval <= 0:
                            continue
                        interval_start = sampled_at - interval
                        for label, (phase_start, phase_end) in phase_windows.items():
                            if phase_start <= interval_start and sampled_at <= phase_end:
                                delta_rows += 1
                                delta_samples[label].add(sample)
                                if delta_sched_cpu_ns > 0:
                                    positive_cpu_samples[label].add(sample)
                                    positive_cpu_pids[label].add(pid)
                                    if (pid != server_profile_report["target_pgid"]
                                            and row.get("exe_basename") == "bedrock_server"):
                                        bds_cpu_samples[label].add(sample)
                                        bds_pids[label].add(pid)
                                break
            except (OSError, csv.Error):
                sample_rows = 0
                delta_rows = 0
                delta_samples = {label: set() for label in required_server_phases}
                positive_cpu_samples = {label: set() for label in required_server_phases}
                positive_cpu_pids = {label: set() for label in required_server_phases}
                bds_cpu_samples = {label: set() for label in required_server_phases}
                bds_pids = {label: set() for label in required_server_phases}
                observed_pids = set()
                process_identities = {}
            server_profile_report["sample_rows"] = sample_rows
            server_profile_report["observed_pids"] = sorted(observed_pids)
            server_profile_report["process_identities_by_pid"] = {
                str(pid): process_identities[pid] for pid in sorted(process_identities)
            }
            server_profile_report["delta_rows"] = delta_rows
            server_profile_report["delta_samples_by_phase"] = {
                label: len(delta_samples[label]) for label in required_server_phases
            }
            server_profile_report["positive_cpu_samples_by_phase"] = {
                label: len(positive_cpu_samples[label]) for label in required_server_phases
            }
            server_profile_report["positive_cpu_pids_by_phase"] = {
                label: sorted(positive_cpu_pids[label]) for label in required_server_phases
            }
            server_profile_report["bds_cpu_samples_by_phase"] = {
                label: len(bds_cpu_samples[label]) for label in required_server_phases
            }
            server_profile_report["bds_pids_by_phase"] = {
                label: sorted(bds_pids[label]) for label in required_server_phases
            }
            missing_phases = [label for label in required_server_phases if not bds_cpu_samples[label]]
            server_profile_report["valid"] = (
                server_profile_report["exit_code"] == 0
                and not missing_phases
            )
            if not server_profile_report["valid"]:
                server_sampler_invalid = True
                message = (
                    "BDS thread CPU sampler invalid: "
                    f"exit_code={server_profile_report['exit_code']}, "
                    f"bds_cpu_samples={server_profile_report['bds_cpu_samples_by_phase']}, "
                    f"missing_phases={missing_phases}"
                )
                report["status"] = "FAIL"
                if not report.get("error"):
                    report["error"] = message
                report["acceptance_failures"].append(message)
            report["server_thread_cpu_profile"] = server_profile_report
        if server_profile_log is not None:
            server_profile_log.close()
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
        if reader_timing is not None and reader_timing._capacity_probe_rows:
            with (work / "capacity_reader_timing.csv").open("w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(("phase", "point", "packet_id", "sample_no", "duration_us",
                                 "batch_bytes", "batch_packet_count"))
                writer.writerows(reader_timing._capacity_probe_rows)
        results["capacity"] = {key: value for key, value in report.items() if key != "environment"}
    if server_sampler_invalid:
        raise RuntimeError(report["error"])
