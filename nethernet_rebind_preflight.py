"""Single-use, bounded NetherNet C->D TIME_WAIT drain preflight (not A1b)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path
from urllib.request import urlopen

from server import PLAYER, PROPERTIES, ROOT, Server, configure

SHA = "1d530f0efbb3b0ac03f0945f2f5d89efa81eee3a"
SEED = "0"
PORTS = frozenset((19132, 19133))
TABLES = ("tcp", "tcp6", "udp", "udp6")
OUT = ROOT / "work" / "rebind"
WAIT_SECONDS = 90.0
POLL_SECONDS = .25
STABLE_SAMPLES = 3
READ_LIMIT = 1024 * 1024
BIND_ERROR = re.compile(r"address already in use|eaddrinuse|port 19132.*(?:in use|failed|error)|failed.*bind|unable.*bind", re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(name: str, value):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(data) > 3_000_000:
        raise RuntimeError(f"evidence_size_limit:{name}")
    path.write_bytes(data)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for piece in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


def write_sums():
    rows = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            rows.append(f"{sha256(path)}  {path.relative_to(OUT)}")
    (OUT / "SHA256SUMS").write_text("\n".join(rows) + "\n")
    for row in rows:
        digest, name = row.split("  ", 1)
        if sha256(OUT / name) != digest:
            raise RuntimeError("manifest_recheck_failed")


def address(hex_value: str, family: str) -> str:
    length = 8 if family == "ipv4" else 32
    if len(hex_value) != length:
        raise ValueError("bad_address_width")
    raw = bytes.fromhex(hex_value)
    if family == "ipv4":
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    return socket.inet_ntop(socket.AF_INET6, b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))


def parse_table(content: str, table: str) -> list[dict]:
    lines = content.splitlines()
    if not lines or "local_address" not in lines[0] or "inode" not in lines[0]:
        raise ValueError(f"bad_proc_header:{table}")
    family = "ipv6" if table.endswith("6") else "ipv4"
    proto = "udp" if table.startswith("udp") else "tcp"
    result = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 10:
            raise ValueError(f"bad_proc_row:{table}")
        local_addr, local_port = fields[1].rsplit(":", 1)
        remote_addr, remote_port = fields[2].rsplit(":", 1)
        port = int(local_port, 16)
        if port not in PORTS:  # Match LOCAL port, never peer port.
            continue
        state = fields[3].upper()
        if not re.fullmatch(r"[0-9A-F]{2}", state):
            raise ValueError(f"bad_proc_state:{table}")
        if proto == "udp":
            classification = "UDP_BOUND"
        elif state == "0A":
            classification = "TCP_LISTEN"
        elif state == "06":
            classification = "TCP_TIME_WAIT"
        else:
            classification = "TCP_OTHER_" + state
        result.append({
            "protocol": proto, "family": family,
            "local_address": address(local_addr, family), "local_port": port,
            "remote_address": address(remote_addr, family), "remote_port": int(remote_port, 16),
            "socket_inode": int(fields[9]), "tcp_state": state if proto == "tcp" else None,
            "classification": classification,
        })
        if len(result) > 64:
            raise ValueError("too_many_target_sockets")
    return result


def sample(proc: Path = Path("/proc")) -> dict:
    rows = []
    for name in TABLES:
        with (proc / "net" / name).open("r", encoding="ascii") as source:
            content = source.read(READ_LIMIT + 1)
        if len(content) > READ_LIMIT:
            raise ValueError(f"proc_read_limit:{name}")
        rows += parse_table(content, name)
    # TCP TIME_WAIT is a waiting condition, not evidence of an active listener.
    tw = [r for r in rows if r["classification"] == "TCP_TIME_WAIT"]
    conflicts = [r for r in rows if r["classification"] != "TCP_TIME_WAIT"]
    return {"utc": now(), "monotonic": time.monotonic(), "rows": rows,
            "time_wait_count": len(tw), "conflicts": conflicts}


def bounded_sample(remaining: float, sampler=sample) -> dict:
    if remaining <= 0:
        raise TimeoutError("deadline_exceeded")
    old = signal.getsignal(signal.SIGALRM)
    previous = signal.getitimer(signal.ITIMER_REAL)
    if previous[0] > 0:
        raise RuntimeError("existing_alarm_timer")
    def alarm(_sig, _frame):
        raise TimeoutError("proc_observation_timeout")
    try:
        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, min(1.5, remaining))
        return sampler()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def wait_drain(previous_pgid: int, *, seconds=WAIT_SECONDS, sampler=sample,
               clock=time.monotonic, sleep=time.sleep, members=Server.group_members):
    start = clock()
    records = []
    seen = False
    stable = 0
    decision = "TIME_WAIT_NOT_DRAINED_WITHIN_BUDGET"
    while clock() - start < seconds:
        remaining = seconds - (clock() - start)
        try:
            # Poll process group within same bounded deadline as socket observation.
            def observe():
                return members(previous_pgid), sampler()
            active, observation = bounded_sample(remaining, observe)
        except (OSError, ValueError, UnicodeError, TimeoutError, RuntimeError) as exc:
            decision = "PORT_PREFLIGHT_INCONCLUSIVE"
            records.append({"utc": now(), "error": f"{type(exc).__name__}:{exc}"[:400]})
            break
        rec = {"sequence": len(records) + 1, "elapsed_seconds": round(clock() - start, 3),
               "utc": observation["utc"], "process_group_members": active,
               "rows": observation["rows"], "time_wait_count": observation["time_wait_count"]}
        records.append(rec)
        seen |= observation["time_wait_count"] > 0
        if active or observation["conflicts"]:
            decision = "PORT_PREFLIGHT_INCONCLUSIVE"
            break
        if observation["time_wait_count"]:
            stable = 0
        else:
            stable += 1
            if stable >= STABLE_SAMPLES:
                decision = "TIME_WAIT_DRAINED" if seen else "NO_TIME_WAIT_REPRODUCED"
                break
        if len(records) >= 400:
            decision = "PORT_PREFLIGHT_INCONCLUSIVE"
            break
        sleep(min(POLL_SECONDS, max(0, seconds - (clock() - start))))
    return {"decision": decision, "saw_time_wait": seen, "stable_clear_samples": stable,
            "elapsed_seconds": round(clock() - start, 3), "samples": records,
            "budget_seconds": seconds, "poll_seconds": POLL_SECONDS}


def prepare(folder: Path, tick: int):
    folder.mkdir(parents=True, exist_ok=False)
    data = {**PROPERTIES, "transport": "nethernet", "level-name": "Bedrock level",
            "level-seed": SEED, "tick-distance": str(tick)}
    path = folder / "server.properties"
    path.write_text("".join(f"{k}={v}\n" for k, v in data.items()))
    configure(path, "nethernet")
    text = path.read_text()
    text = re.sub(r"(?m)^tick-distance=4$", f"tick-distance={tick}", text)
    path.write_text(text)
    verify_properties(path, tick)
    return path


def verify_properties(path: Path, tick: int):
    lines = [line for line in path.read_text().splitlines() if "=" in line and not line.startswith("#")]
    values = dict(line.split("=", 1) for line in lines)
    desired = {"transport":"nethernet", "tick-distance":str(tick), "view-distance":"12",
               "level-seed":SEED, "server-port":"19132", "server-portv6":"19133",
               "gamemode":"creative", "default-player-permission-level":"operator"}
    if any(values.get(k) != v or sum(line.startswith(k + "=") for line in lines) != 1
           for k, v in desired.items()):
        raise RuntimeError("effective_properties_mismatch")
    return values


def stop_capture(server: Server):
    proc = server.process
    if proc is None:
        return {"graceful": False, "error": "process_missing"}
    pgid = pid = proc.pid   # Server.start uses start_new_session=True.
    error = None
    try:
        server.stop()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
    remaining = Server.group_members(pgid)
    clean_log = any("quit correctly" in x.lower() for x in server.snapshot())
    return {"pid": pid, "pgid": pgid, "returncode": proc.poll(),
            "remaining_processes": remaining, "stop_error": error,
            "quit_correctly": clean_log, "group_quiescent": not remaining,
            "graceful": error is None and proc.returncode == 0
            and not remaining and clean_log, "at_utc": now()}


async def connect_bot(server: Server, info: dict):
    from endpilot import BotOptions, Runtime
    from chunk_operability_probe import ObservedRuntimeContext, client_status
    with urlopen("http://127.0.0.1:19132/v1/join", timeout=8) as res:
        info["nethernet_http_status"] = res.status
        if not 200 <= res.status < 300:
            raise RuntimeError("nethernet_join_http_failed")
    async with ObservedRuntimeContext(Runtime(), info["runtime_cleanup"]) as runtime:
        bot = runtime.add_bot(BotOptions(server_url="http://127.0.0.1:19132",
                                         transport="nethernet", protocol=2193, name=PLAYER,
                                         chunk_radius=8, client_cache=False, validate_all_packets=True))
        await bot.wait_ready(70)
        info["bot_ready"] = client_status(bot)
        info["bds_online_list"] = (await asyncio.to_thread(server.wait_player, True, 20))[-20:]
        info["online_socket_state"] = sample()
    info["bot_context_exited"] = True
    info["bds_offline_list"] = (await asyncio.to_thread(server.wait_player, False, 25))[-20:]
    if info["runtime_cleanup"].get("runtime_context_closed") is not True:
        raise RuntimeError("bot_runtime_close_unverified")


def run(server_python: Path, commit: str):
    OUT.mkdir(parents=True, exist_ok=False)
    summary = {"status":"PORT_PREFLIGHT_EXECUTION_BLOCKED", "started_utc":now(),
               "bds_lifecycles":0, "bot_logins":0, "d_attempts":0, "world_mutation_commands":0,
               "teleports":0, "active_bind_probes":0, "endpilot_sha":SHA, "seed":SEED}
    runtime = {"C": {"started":False, "runtime_cleanup":{}}, "D":{"started":False}}
    handoff = {"decision":"NOT_STARTED", "samples":[]}
    c = d = None
    try:
        if commit != SHA:
            raise RuntimeError("requested_endpilot_pin_mismatch")
        direct = distribution("endpilot").read_text("direct_url.json")
        if not direct or json.loads(direct).get("vcs_info", {}).get("commit_id") != SHA:
            raise RuntimeError("installed_endpilot_pin_mismatch")
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("client_python_version_mismatch")
        from importlib.metadata import version
        if version("bedrock-protocol-native") != "0.5.0":
            raise RuntimeError("native_protocol_version_mismatch")
        server_ver = subprocess.run([str(server_python.absolute()), "-c",
            "import sys,importlib.metadata as m;print(str(sys.version_info.major)+'.'+str(sys.version_info.minor));print(m.version('endstone'))"],
            check=True, capture_output=True, text=True, timeout=12).stdout.splitlines()
        if server_ver != ["3.13", "0.11.14"]:
            raise RuntimeError(f"server_python_endstone_version_mismatch:{server_ver}")
        runtime["installed_versions"] = {"client_python":sys.version.split()[0],
            "server_python":server_ver[0], "endstone":server_ver[1],
            "native":version("bedrock-protocol-native"),"endpilot_sha":SHA}
        cpath = prepare(ROOT / "work" / "rebind-C-world", 4)
        dpath = prepare(ROOT / "work" / "rebind-D-world", 6)
        shutil.copy2(cpath, OUT / "phase-C-server.properties")
        shutil.copy2(dpath, OUT / "phase-D-server.properties")
        c = Server(server_python.absolute(), cpath.parent, OUT / "phase-C-bds.log")
        summary["bds_lifecycles"] += 1
        c.start()
        runtime["C"].update({"started":True, "pid":c.process.pid, "pgid":c.process.pid})
        c.wait_ready()
        if not any("Version: 1.26.52.3" in line for line in c.snapshot()):
            raise RuntimeError("bds_patch_version_mismatch:C")
        verify_properties(cpath, 4)
        runtime["C"]["ready"] = True
        runtime["C"]["ready_socket_state"] = sample()
        summary["bot_login_attempts"] = 1
        asyncio.run(connect_bot(c, runtime["C"]))
        summary["bot_logins"] = 1
        runtime["C"]["post_disconnect_socket_state"] = sample()
        runtime["C"]["exit"] = stop_capture(c)
        if not runtime["C"]["exit"]["graceful"]:
            raise RuntimeError("C_shutdown_not_graceful")
        c = None
        handoff = wait_drain(runtime["C"]["exit"]["pgid"])
        if handoff["decision"] not in ("TIME_WAIT_DRAINED", "NO_TIME_WAIT_REPRODUCED"):
            summary["status"] = ("TIME_WAIT_NOT_DRAINED_WITHIN_BUDGET" if handoff["decision"]
                                 == "TIME_WAIT_NOT_DRAINED_WITHIN_BUDGET" else "PORT_PREFLIGHT_INCONCLUSIVE")
            return summary
        # One and only one D start, after a complete C shutdown and three clean samples.
        d = Server(server_python.absolute(), dpath.parent, OUT / "phase-D-bds.log")
        summary["d_attempts"] += 1
        summary["bds_lifecycles"] += 1
        d.start()
        runtime["D"].update({"started":True, "pid":d.process.pid, "pgid":d.process.pid})
        try:
            d.wait_ready()
            if not any("Version: 1.26.52.3" in line for line in d.snapshot()):
                raise RuntimeError("bds_patch_version_mismatch:D")
            verify_properties(dpath, 6)
            runtime["D"]["ready"] = True
            runtime["D"]["ready_socket_state"] = sample()
            summary["status"] = ("REBINDS_AFTER_TIME_WAIT_DRAIN_OBSERVED" if handoff["saw_time_wait"]
                                 else "REBINDS_WITHOUT_TIME_WAIT_REPRODUCTION")
        except BaseException as exc:
            runtime["D"]["ready_error"] = f"{type(exc).__name__}:{exc}"[:600]
            runtime["D"]["raw_error_log"] = d.snapshot()[-100:]
            try:
                runtime["D"]["post_failure_socket_state"] = bounded_sample(1.5)
            except BaseException as sample_exc:
                runtime["D"]["post_failure_observation_error"] = str(sample_exc)[:400]
            summary["status"] = ("BIND_FAILURE_AFTER_TIME_WAIT_DRAIN" if handoff["saw_time_wait"]
                                 and BIND_ERROR.search("\n".join(d.snapshot()))
                                 else "PORT_PREFLIGHT_INCONCLUSIVE")
        finally:
            runtime["D"]["exit"] = stop_capture(d)
            if runtime["D"].get("ready"):
                if not runtime["D"]["exit"]["graceful"]:
                    summary["cleanup_incomplete"] = True
                    summary["status"] = "PORT_PREFLIGHT_INCONCLUSIVE"
            elif not runtime["D"]["exit"]["group_quiescent"]:
                summary["cleanup_incomplete"] = True
                summary["status"] = "PORT_PREFLIGHT_INCONCLUSIVE"
            d = None
    except BaseException as exc:
        summary["error"] = f"{type(exc).__name__}:{exc}"[:600]
        summary["status"] = "PORT_PREFLIGHT_INCONCLUSIVE"
    finally:
        for name, obj in (("C",c), ("D",d)):
            if obj is not None and obj.process is not None:
                runtime[name]["exit"] = stop_capture(obj)
                if not runtime[name]["exit"]["group_quiescent"]:
                    summary["cleanup_incomplete"] = True
        for name in ("C", "D"):
            path = OUT / f"phase-{name}-bds.log"
            path.touch(exist_ok=True)
            props = OUT / f"phase-{name}-server.properties"
            props.touch(exist_ok=True)
        summary["finished_utc"] = now()
        summary["c_ready"] = runtime["C"].get("ready",False)
        summary["d_ready"] = runtime["D"].get("ready",False)
        summary["saw_time_wait"] = handoff.get("saw_time_wait",False)
        summary["handoff_decision"] = handoff["decision"]
        for name in ("C", "D"):
            folder = ROOT / "work" / f"rebind-{name}-world"
            version = folder / "version.txt"
            runtime[name]["bds_version_txt"] = version.read_text().strip() if version.is_file() else None
        (OUT / "version.txt").write_text(
            "BDS=1.26.52.3\nEndstone=0.11.14\nEndPilot=" + SHA +
            "\nProtocol=2193\nNative=0.5.0\nServerPython=3.13\nClientPython="+sys.version.split()[0]+"\n")
        write_json("port-handoff.json", handoff)
        write_json("runtime-evidence.json", runtime)
        write_json("summary.json", summary)
        write_sums()
        print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server-python", type=Path, required=True)
    p.add_argument("--endpilot-commit", required=True)
    args = p.parse_args()
    result = run(args.server_python, args.endpilot_commit)
    return 0 if result["status"].startswith("REBINDS_") else 1


if __name__ == "__main__":
    sys.exit(main())
