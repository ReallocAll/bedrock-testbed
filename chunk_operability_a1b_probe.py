"""Single-use, bounded A1b comparison of fixed Bedrock world operations.

One baseline BDS and exactly four sequential condition BDS processes.  All
online block-type readback is deliberately omitted.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path
from urllib.request import urlopen

import chunk_operability_probe as a1
from server import PLAYER, PROPERTIES, ROOT, Server, configure

SEED = "73005203"
SHA = "1d530f0efbb3b0ac03f0945f2f5d89efa81eee3a"
BRANCH = "test/chunk-operability-a1b-20261101"
CONDITIONS = (("A", "raknet", 4), ("B", "raknet", 6),
              ("C", "nethernet", 4), ("D", "nethernet", 6))
OPERATIONS = (
    ("remote_point", "setblock 80 99 0 minecraft:stone", "setblock", ((5, 0),)),
    ("near_point", "setblock 64 99 0 minecraft:stone", "setblock", ((4, 0),)),
    ("near_cross", "fill 63 99 0 64 99 0 minecraft:stone", "fill", ((3, 0), (4, 0))),
    ("remote_cross", "fill 79 99 0 80 99 0 minecraft:stone", "fill", ((4, 0), (5, 0))),
    ("POST_DIAGNOSTIC_FULL_FILL", "fill 0 99 -2 80 99 2 minecraft:stone", "fill", a1.TARGET_COLUMNS),
)
OUT = ROOT / "work" / "a1b"
MAX_BYTES = 2 * 1024 * 1024


class Blocked(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def emit(path: Path, value):
    content = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(content) > MAX_BYTES:
        raise Blocked(f"structured_json_budget_exceeded:{path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def manifest(worlds: Path) -> dict:
    if not worlds.is_dir():
        raise Blocked("world_directory_missing")
    rows = [{"path": str(p.relative_to(worlds)), "size": p.stat().st_size,
             "sha256": digest(p)} for p in sorted(worlds.rglob("*")) if p.is_file()]
    if not rows:
        raise Blocked("world_manifest_empty")
    body = json.dumps(rows, separators=(",", ":"), sort_keys=True).encode()
    return {"count": len(rows), "files": rows, "hash": hashlib.sha256(body).hexdigest()}


def preseed(directory: Path):
    directory.mkdir(parents=True)
    settings = {**PROPERTIES, "level-name": "Bedrock level", "level-seed": SEED}
    (directory / "server.properties").write_text(
        "".join(f"{k}={v}\n" for k, v in settings.items()), encoding="utf-8")


def prepare_properties(folder: Path, transport: str, tick: int):
    path = folder / "server.properties"
    configure(path, transport)  # server.configure owns defaults, including tick-distance=4.
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [f"tick-distance={tick}" if row.startswith("tick-distance=") else row for row in lines]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    parsed = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    expected = {"transport": transport, "tick-distance": str(tick), "view-distance": "12",
                "level-seed": SEED, "gamemode": "creative", "default-player-permission-level": "operator"}
    if any(parsed.get(key) != value for key, value in expected.items()):
        raise Blocked(f"EXPERIMENT_CONFIGURATION_UNVERIFIED:{transport}:{tick}")
    if any(sum(1 for line in lines if line.startswith(key + "=")) != 1 for key in expected):
        raise Blocked("duplicate_effective_property")
    return parsed


def assert_version(server: Server):
    if not any("Version: 1.26.52.3" in line for line in server.snapshot()):
        raise Blocked("exact_bds_patch_version_not_verified")


def mapped_loaded(snapshot: dict, covered) -> dict:
    record = snapshot.get("plugin_record") or {}
    states = record.get("target_column_states")
    if record.get("status") != "ok" or not isinstance(states, list):
        raise Blocked("snapshot_record_incomplete")
    mapping = {(r["chunk_x"], r["chunk_z"]): r["loaded"] for r in states}
    if set(mapping) != set(a1.TARGET_COLUMNS) or any(type(v) is not bool for v in mapping.values()):
        raise Blocked("snapshot_target_columns_incomplete")
    return {f"{x},{z}": mapping[x, z] for x, z in covered}


def classify_operation(row: dict, loaded: dict) -> dict:
    status = row["status"]
    labels = ["OPERATION_RESULT_ONLY"]
    if status == "failure" and loaded and all(loaded.values()):
        labels.append("REPORTED_LOADED_BUT_OPERATION_REJECTED")
    if status in ("timeout", "server_stopped", "ambiguous"):
        labels.append("INCONCLUSIVE")
    return {"status": status, "labels": labels, "loaded": loaded}


def contaminate(previous: dict, status: str, covered) -> dict:
    updated = previous.copy()
    for column in covered:
        key = f"{column[0]},{column[1]}"
        updated[key] = ("KNOWN_CHANGED_BY_PRIOR_COMMAND" if status == "success" else
                        "PARTIAL_OR_UNKNOWN" if status in ("failure", "timeout", "server_stopped", "ambiguous")
                        else updated.get(key, "PRESTATE_UNKNOWN"))
    return updated


def unique_response(lines: list[str], kind: str) -> bool:
    """Reject missing/multiple terminal outputs and mixed, unidentifiable command replies."""
    candidates = []
    for line in lines:
        response = a1.command_response([line], kind)
        if response != "pending":
            candidates.append(response)
    return len(candidates) == 1


async def strict_command(server, command, kind, phase, deadline, before, evidence, target):
    # Single flight, log offset, unique sequence and quiet-window duplicate detection.
    # Reject any late completion arriving immediately before the next command.
    quiet_from = len(server.snapshot())
    await asyncio.sleep(min(0.25, a1.remaining(deadline)))
    if any(any(a1.command_response([line], candidate) != "pending"
               for candidate in ("fill", "setblock", "tp"))
           for line in server.snapshot(quiet_from)):
        raise Blocked(f"late_reply_before_command:{phase}")
    offset = len(server.snapshot())
    row = await a1.wait_for_command(server, command, kind, phase, deadline,
                                    before, evidence, timeout=6.0, target=target)
    if row["status"] in ("timeout", "server_stopped"):
        raise Blocked(f"command_completion_unknown:{phase}:{row['status']}")
    await asyncio.sleep(min(0.20, a1.remaining(deadline)))
    all_lines = server.snapshot(offset)
    if (not unique_response(all_lines, kind) or
        any(a1.command_response([line], other) not in ("pending",)
            for line in all_lines
            for other in ("fill", "setblock", "tp") if other != kind
            and a1.command_response([line], kind) == "pending")):
        row["status"] = "ambiguous"
        row["raw_lines"] = all_lines[-40:]
        raise Blocked(f"command_response_ambiguous:{phase}")
    row["raw_lines"] = all_lines[-40:]
    row["raw_text"] = "\n".join(all_lines)[:a1.MAX_RESPONSE_CHARS]
    if kind == "fill":
        match = a1.SUCCESS_FILL.search(row["raw_text"])
        row["reported_block_count"] = int(match.group("count")) if match else None
        row["successful_block_count"] = (row["reported_block_count"] if row["status"] == "success" else None)
    a1.ensure_json_size(evidence)
    return row


def validate_order(evidence):
    rows = evidence["commands"]
    expected = [a1.SAFE_LANDING_COMMAND, f"tp {PLAYER} 0.5 100 0.5"]
    if evidence["safety_gate"]["status"] == "ALLOW":
        expected += [operation[1] for operation in OPERATIONS]
    actual = [row["command"] for row in rows]
    if actual != expected[:len(actual)] or len(actual) > len(expected):
        raise Blocked("invalid_command_sequence")
    if [row["sequence"] for row in rows] != list(range(1, len(rows) + 1)):
        raise Blocked("invalid_command_ids")
    if evidence["safety_gate"]["status"] != "ALLOW" and len(rows) != 1:
        raise Blocked("unsafe_command_after_blocked_landing")
    if len(rows) > 7:
        raise Blocked("world_command_budget_exceeded")
    return len(rows) == len(expected)


async def check_position(server, bot, phase, deadline, evidence):
    row = await a1.stable_authoritative_position(server, phase, deadline, evidence)
    status = a1.client_status(bot)
    if status["session_id"] != evidence["session"] or status["dimension"] != 0:
        raise Blocked("session_or_dimension_changed")
    if phase != "initial":
        x, y, z = a1.position_values(row)
        client = status["position"]
        if (math.hypot(x - .5, z - .5) > .1 or abs(y - 101.62001) > .5 or
            math.hypot(x-client["x"], z-client["z"]) > .1 or abs(y-client["y"]) > .5):
            raise Blocked("authority_or_client_position_mismatch")
    evidence.setdefault("positions", {})[phase] = {"bds": row, "client": status}
    return row


async def run_condition(server, transport, evidence):
    from endpilot import BotOptions, Runtime
    deadline = time.monotonic() + 120.0
    evidence["started_at_utc"] = utc()
    endpoint = "http://127.0.0.1:19132" if transport == "nethernet" else "raknet://127.0.0.1:19132"
    if transport == "nethernet":
        with urlopen("http://127.0.0.1:19132/v1/join", timeout=8) as response:
            if not 200 <= response.status < 300:
                raise Blocked("nethernet_signaling_unavailable")
    async with a1.ObservedRuntimeContext(Runtime(), evidence["cleanup"]) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=endpoint, transport=transport, protocol=a1.EXPECTED_PROTOCOL,
            name=PLAYER, chunk_radius=8, client_cache=False, validate_all_packets=True))
        await bot.wait_ready(min(70.0, a1.remaining(deadline)))
        evidence["session"] = a1.client_status(bot)["session_id"]
        online_lines = await asyncio.to_thread(server.wait_player, True, 15)
        # Server.wait_player returns a list of server-console lines, not a (bool, lines) tuple.
        # It raises if the authoritative list command cannot confirm the player online.
        evidence["online"] = online_lines[-20:]
        await check_position(server, bot, "initial", deadline, evidence)
        await a1.wait_plugin_ready(server, deadline)
        before = await a1.request_snapshot(server, "initial", deadline, evidence)
        evidence["initial_snapshot_id"] = before["snapshot_id"]
        landing = await strict_command(server, a1.SAFE_LANDING_COMMAND, "fill", "safe_landing_block",
                                       deadline, before["snapshot_id"], evidence, {"x": [0,0],"y":[99,99],"z":[0,0]})
        evidence["safe_landing"] = landing
        gate = a1.safe_landing_gate(landing)
        evidence["safety_gate"] = gate
        # A failed safety gate is terminal; do not request a diagnostic command or TP.
        if gate["status"] != "ALLOW":
            evidence["teleport"] = {"status": "skipped_safe_landing_not_confirmed",
                                    "reason": gate["reason"], "bds_horizontal_movement": 0}
            raise Blocked(f"SAFETY_BLOCKED:{gate['reason']}")
        after = await a1.request_snapshot(server, "post_landing", deadline, evidence)
        landing["post_snapshot_id"] = after["snapshot_id"]
        tp = await strict_command(server, f"tp {PLAYER} 0.5 100 0.5", "tp", "conditional_teleport",
                                  deadline, after["snapshot_id"], evidence, {"entity":PLAYER,"position":[.5,100,.5]})
        evidence["teleport"] = tp
        if tp["status"] != "success":
            raise Blocked("teleport_not_confirmed")
        before = await a1.request_snapshot(server, "post_tp", deadline, evidence)
        await check_position(server, bot, "post_tp", deadline, evidence)
        pollution = {f"{x},{z}":"PRESTATE_UNKNOWN" for x,z in a1.TARGET_COLUMNS}
        pollution["0,0"] = "KNOWN_CHANGED_BY_PRIOR_COMMAND"
        for phase, command, kind, covered in OPERATIONS:
            if len(evidence["operations"]) >= 5:
                raise Blocked("diagnostic_operation_budget_exceeded")
            states = mapped_loaded(before, covered)
            row = await strict_command(server, command, kind, phase, deadline,
                                       before["snapshot_id"], evidence, {"chunks":covered})
            row["coverage_loaded_before"] = states
            row["pollution_before"] = {f"{x},{z}": pollution[f"{x},{z}"] for x,z in covered}
            row["classification"] = classify_operation(row, states)
            row["pollution_after"] = contaminate(pollution, row["status"], covered)
            pollution = row["pollution_after"]
            evidence["operations"].append(row)
            after = await a1.request_snapshot(server, f"post_{phase}", deadline, evidence)
            row["post_snapshot_id"] = after["snapshot_id"]
            row["coverage_loaded_after"] = mapped_loaded(after, covered)
            before = after
            emit(Path(evidence["output_path"]), evidence)
        await check_position(server, bot, "final", deadline, evidence)
        evidence["status"] = "EXECUTION_PASS"
        evidence["completed_at_utc"] = utc()


async def prime_world(server):
    from endpilot import BotOptions, Runtime
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=a1.EXPECTED_PROTOCOL, name=PLAYER, chunk_radius=8, client_cache=False,
            validate_all_packets=True))
        await bot.wait_ready(70)
        await asyncio.to_thread(server.wait_player, True, 15)


def compare(rows):
    """Only attribute a directional 4/6 change when all prerequired comparisons pass.

    Operation results alone cannot establish the original target block type, nor
    SubChunk initialization.  An incomplete experimental control stays inconclusive.
    """
    by = {r.get("id"): r for r in rows}
    conclusion = {"simulation_distance": "INCONCLUSIVE",
                  "subchunk": "SUBCHUNK_EFFECT_UNVERIFIED",
                  "original_block": "NO_CHANGE_EXPLANATION_UNVERIFIED",
                  "cross_chunk": "CROSS_CHUNK_EXTRA_VALIDATION_UNVERIFIED"}
    if set(by) != set("ABCD") or any(r.get("status") != "EXECUTION_PASS" for r in rows):
        return {**conclusion, "reason": "incomplete_conditions"}
    expected_hashes = {r.get("baseline_hash") for r in rows} | {r.get("world_copy_hash") for r in rows}
    if len(expected_hashes) != 1 or None in expected_hashes:
        return {**conclusion, "reason": "baseline_world_hashes_not_equal"}
    positions = []
    primary = []
    for name in "ABCD":
        row = by[name]
        if (row.get("safety_gate", {}).get("status") != "ALLOW" or
                len(row.get("operations", [])) != 5 or
                row.get("tick_distance") != (4 if name in "AC" else 6) or
                row.get("transport") != ("raknet" if name in "AB" else "nethernet")):
            return {**conclusion, "reason": f"failed_control_{name}"}
        pos = row.get("positions", {}).get("post_tp", {}).get("bds", {})
        try:
            positions.append(a1.position_values(pos))
        except (KeyError, TypeError, ValueError):
            return {**conclusion, "reason": f"missing_authoritative_post_tp_{name}"}
        first = row["operations"][0]
        if (first.get("phase") != "remote_point" or
                first.get("command") != OPERATIONS[0][1] or
                first.get("pollution_before") != {"5,0": "PRESTATE_UNKNOWN"} or
                set(first.get("coverage_loaded_before", {})) != {"5,0"} or
                first.get("status") not in ("success", "failure", "no_change")):
            return {**conclusion, "reason": f"primary_operation_not_comparable_{name}"}
        primary.append(first)
    if any(max(abs(a - b) for a, b in zip(positions[0], point)) > 0.1 for point in positions[1:]):
        return {**conclusion, "reason": "initial_authoritative_positions_not_comparable",
                "post_tp_positions": positions}
    statuses = [r["status"] for r in primary]
    result = {**conclusion, "primary_statuses": statuses,
              "post_tp_positions": positions,
              "initial_loaded": [r["coverage_loaded_before"]["5,0"] for r in primary]}
    # Failure/success is a stronger oracle than no-change, which may simply
    # indicate an unchanged original block.  Require the same direction in both
    # transport pairs; failures still need scrutiny of their original BDS logs.
    if (statuses[0] != statuses[1] and statuses[2] != statuses[3]
            and (statuses[0], statuses[1]) == (statuses[2], statuses[3])
            and set(statuses) == {"success", "failure"}):
        result["simulation_distance"] = "SIMULATION_DISTANCE_SUPPORTED"
        result["qualification"] = "bounded operation-result association, not a demonstrated chunk-loading mechanism"
    elif (statuses[0] != statuses[1]) != (statuses[2] != statuses[3]):
        result["simulation_distance"] = "TRANSPORT_SPECIFIC_DISTANCE_ASSOCIATION"
    else:
        result["reason"] = "no_comparable_same_direction_primary_result_difference"
    return result


def sums(root):
    entries=[]
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS" and not p.name.endswith(".tmp"):
            entries.append(f"{digest(p)}  {p.relative_to(root)}")
    (root/"SHA256SUMS").write_text("\n".join(entries)+"\n",encoding="utf-8")
    for row in entries:
        sha, file = row.split("  ",1)
        if digest(root/file) != sha:
            raise Blocked("SHA256SUMS_verification_failed")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--server-python",type=Path,required=True)
    parser.add_argument("--plugin-wheel",type=Path,required=True)
    parser.add_argument("--endpilot-commit",required=True)
    args=parser.parse_args()
    work=ROOT/"work"
    if work.exists():
        shutil.rmtree(work)
    OUT.mkdir(parents=True)
    summary={"status":"EXECUTION_BLOCKED","seed":SEED,"expected_sha":SHA,
             "started_at_utc":utc(),"conditions":[],"subchunk":"SUBCHUNK_EFFECT_UNVERIFIED",
             "readback_count":0,"bds_lifecycles":0,"protocol":2193,"endstone":"0.11.14","bds":"1.26.52.3"}
    base=None
    current=None
    entries=[]
    try:
        if args.endpilot_commit != SHA:
            raise Blocked("unexpected_sdk_pin")
        direct=distribution("endpilot").read_text("direct_url.json")
        if not direct or json.loads(direct).get("vcs_info",{}).get("commit_id") != SHA:
            raise Blocked("installed_endpilot_sha_mismatch")
        a1.verify_plugin_wheel(args.plugin_wheel.resolve())
        baseline=work/"baseline"
        preseed(baseline)
        base=Server(args.server_python.absolute(),baseline,OUT/"baseline-bds.log")
        summary["bds_lifecycles"]+=1
        base.start();base.wait_ready();assert_version(base)
        asyncio.run(prime_world(base))
        base.stop();base=None
        original=manifest(baseline/"worlds")
        emit(OUT/"baseline-world-manifest.json",{"seed":SEED,**original})
        summary["baseline_hash"]=original["hash"]
        # Copy *all* conditions before any condition BDS starts.
        for name,transport,tick in CONDITIONS:
            folder=work/f"condition-{name}"
            shutil.copytree(baseline,folder)
            if manifest(folder/"worlds")["hash"] != original["hash"]:
                raise Blocked("BASELINE_WORLD_ISOLATION_BLOCKED")
            (OUT/"conditions"/name).mkdir(parents=True)
            props=prepare_properties(folder,transport,tick)
            shutil.copy2(folder/"server.properties",OUT/"conditions"/name/"server.properties")
            plugin=folder/"plugins"
            plugin.mkdir(parents=True,exist_ok=True)
            shutil.copy2(args.plugin_wheel,plugin/args.plugin_wheel.name)
            entries.append((name,transport,tick,folder,props))
        for name,transport,tick,folder,props in entries:
            if summary["bds_lifecycles"]>=5:
                raise Blocked("bds_lifecycle_limit")
            data={"id":name,"transport":transport,"tick_distance":tick,"view_distance":12,
                  "seed":SEED,"baseline_hash":original["hash"],"world_copy_hash":manifest(folder/"worlds")["hash"],
                  "bds_version":"1.26.52.3","endstone_version":"0.11.14","endpilot_sha":SHA,
                  "protocol":2193,"python_client":sys.version.split()[0],
                  "status":"EXECUTION_BLOCKED","commands":[],"operations":[],"snapshots":[],
                  "authoritative_positions":[],"observer_cleanup":{},"session":None,
                  "cleanup":{},"safety_gate":{"status":"UNVERIFIED"},"positions":{},
                  "output_path":str(OUT/"conditions"/name/"evidence.json"),
                  "budget":{"max_diagnostic_seconds":120,"max_operations":5,"max_snapshots":24,
                            "max_read_batches":12,"subchunk_requests":0,"packet_events":0}}
            summary["conditions"].append({"id":name,"status":"EXECUTION_BLOCKED"})
            emit(OUT/"conditions"/name/"evidence.json",data)
            current=Server(args.server_python.absolute(),folder,OUT/"conditions"/name/"bds.log")
            summary["bds_lifecycles"]+=1
            try:
                # Verify the same effective properties while still stopped.
                prepare_properties(folder,transport,tick)
                current.start();current.wait_ready();assert_version(current)
                effective = dict(line.split("=", 1) for line in (folder / "server.properties").read_text().splitlines()
                                 if "=" in line and not line.startswith("#"))
                required = ("transport", "tick-distance", "view-distance", "level-seed",
                            "gamemode", "default-player-permission-level")
                if any(effective.get(k) != props.get(k) for k in required):
                    raise Blocked("EXPERIMENT_CONFIGURATION_UNVERIFIED:effective_after_bds_start")
                data["effective_config"] = {k: effective[k] for k in required}
                shutil.copy2(folder / "server.properties", OUT / "conditions" / name / "server.properties")
                asyncio.run(run_condition(current,transport,data))
                if not validate_order(data):
                    raise Blocked("incomplete_operation_sequence")
            except BaseException as exc:
                data["error"]=f"{type(exc).__name__}: {exc}"[:700]
                data["status"]="SAFETY_BLOCKED" if "SAFETY_BLOCKED" in str(exc) else "EXECUTION_BLOCKED"
            finally:
                if current.process is not None:
                    try:
                        current.stop()
                        data["cleanup"]["bds_graceful_stop"]=True
                        data["cleanup"]["observer_disable_hook_observed"]=any(
                            "CHUNKOP_PLUGIN_DISABLED" in line for line in current.snapshot())
                        if not data["cleanup"]["observer_disable_hook_observed"]:
                            data["status"]="EXECUTION_FAIL"
                    except BaseException as error:
                        data["status"]="EXECUTION_FAIL"
                        data["cleanup"]["stop_error"]=str(error)[:400]
                current=None
                data["budget"]["snapshots_used"]=len(data["snapshots"])
                data["budget"]["operations_used"]=len(data["operations"])
                data["cleanup"].setdefault("runtime_context_closed",False)
                if data["status"]=="EXECUTION_PASS" and (not data["cleanup"].get("runtime_context_closed") or
                    not data["cleanup"].get("observer_disable_hook_observed")):
                    data["status"]="EXECUTION_FAIL"
                emit(OUT/"conditions"/name/"evidence.json",data)
                emit(OUT/"conditions"/name/"result.json",{"status":data["status"],"error":data.get("error"),
                     "operations":[{"phase":v["phase"],"status":v["status"]} for v in data["operations"]]})
                summary["conditions"][-1]={"id":name,"status":data["status"],"error":data.get("error")}
            if data["status"]!="EXECUTION_PASS":
                raise Blocked(f"condition_{name}_blocked:{data.get('error',data['status'])}")
        records=[]
        for name,_,_,_,_ in entries:
            records.append(json.loads((OUT/"conditions"/name/"evidence.json").read_text()))
        summary["causal"]=compare(records)
        summary["status"]="EXECUTION_PASS"
    except BaseException as error:
        summary["error"]=f"{type(error).__name__}: {error}"[:700]
    finally:
        for server in (current,base):
            if server is not None and server.process is not None:
                try:server.stop()
                except BaseException as exc:summary["cleanup_error"]=str(exc)[:500]
        summary["finished_at_utc"]=utc()
        emit(OUT/"summary.json",summary)
        emit(work/"result.json",summary)
        sums(OUT)
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    return 0 if summary["status"]=="EXECUTION_PASS" else 1


if __name__=="__main__":
    sys.exit(main())
