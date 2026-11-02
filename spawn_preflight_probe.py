"""One fresh BDS lifecycle to preflight a fixed natural-spawn seed.

Never modifies blocks, players, world spawn, ticking areas or SubChunks.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import distribution, version
from pathlib import Path

import chunk_operability_probe as a1
from server import PLAYER, PROPERTIES, ROOT, Server

SEED = "0"  # One predetermined, unproven control seed; never fallback.
SHA = "1d530f0efbb3b0ac03f0945f2f5d89efa81eee3a"
BRANCH = "test/spawn-observer-readiness"
OUT = ROOT / "work" / "spawn-preflight"
REQUIRED = {
    **PROPERTIES,
    "level-seed": SEED,
    "level-name": "Spawn preflight 20261102",
}
CHECKED = {
    "transport": "raknet", "level-seed": SEED, "tick-distance": "4",
    "view-distance": "12", "gamemode": "creative",
    "default-player-permission-level": "operator",
    "client-side-chunk-generation-enabled": "false",
}


class Blocked(RuntimeError):
    pass


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if len(data.encode("utf-8")) > 2_000_000:
        raise Blocked("evidence_size_limit")
    path.write_text(data, encoding="utf-8")


def properties(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise Blocked(f"duplicate_property:{key}")
        values[key] = value
    if any(values.get(key) != value for key, value in CHECKED.items()):
        raise Blocked(f"effective_properties_mismatch:{values}")
    return values


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def write_hashes() -> None:
    entries = [p for p in OUT.rglob("*") if p.is_file() and p.name != "SHA256SUMS"]
    entries.sort()
    (OUT / "SHA256SUMS").write_text(
        "".join(f"{sha256(p)}  {p.relative_to(OUT)}\n" for p in entries),
        encoding="utf-8",
    )
    for path in entries:
        if not sha256(path):
            raise Blocked("hash_unavailable")


async def observe(server: Server, evidence: dict) -> None:
    from endpilot import BotOptions, Runtime

    deadline = asyncio.get_running_loop().time() + 120
    async with a1.ObservedRuntimeContext(Runtime(), evidence["cleanup"]) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132",
            transport="raknet", protocol=2193, name=PLAYER,
            chunk_radius=8, client_cache=False, validate_all_packets=True,
        ))
        await bot.wait_ready(min(70.0, a1.remaining(deadline)))
        status = a1.client_status(bot)
        evidence["client_ready"] = status
        evidence["session"] = status["session_id"]
        evidence["online_lines"] = (await asyncio.to_thread(server.wait_player, True, 15))[-20:]
        await a1.wait_plugin_ready(server, deadline)
        location = await a1.stable_authoritative_position(server, "natural_spawn", deadline, evidence)
        evidence["authoritative_position"] = location
        if location["entity"].get("dimension") != 0 or status["dimension"] != 0:
            raise Blocked("not_overworld")
        if status["session_id"] != evidence["session"]:
            raise Blocked("session_changed")
        x, y, z = a1.position_values(location)
        if not all(map(math.isfinite, (x, y, z))):
            raise Blocked("nonfinite_querytarget")
        evidence["position"] = {"x": x, "y": y, "z": z}
        evidence["chunk"] = {"x": math.floor(x / 16), "z": math.floor(z / 16)}
        evidence["horizontal_distance_to_origin_center"] = math.hypot(x - .5, z - .5)
        snapshot = await a1.request_snapshot(server, "natural_spawn", deadline, evidence)
        evidence["snapshot_id"] = snapshot["request_id"]
        states = snapshot["target_column_states"]
        if len(states) != 12 or len({(s["chunk_x"], s["chunk_z"]) for s in states}) != 12:
            raise Blocked("snapshot_cardinality_not_12")
        evidence["target_column_states"] = states
        final_client = a1.client_status(bot)
        evidence["client_after_snapshot"] = final_client
        if final_client["session_id"] != evidence["session"] or final_client["dimension"] != 0:
            raise Blocked("session_or_dimension_changed_after_snapshot")
        evidence["origin_loaded"] = next(
            s["loaded"] for s in states if (s["chunk_x"], s["chunk_z"]) == (0, 0)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--plugin-wheel", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    folder = work / "server"
    OUT.mkdir(parents=True, exist_ok=True)
    server = None
    evidence = {
        "status": "SPAWN_PREFLIGHT_EXECUTION_BLOCKED",
        "started_at_utc": utc(), "seed": SEED, "seed_reason": "fixed zero control; origin spawn unverified",
        "testbed_parent": "27e31dc2a96f3271b8b2e470099165293d628520",
        "expected_branch": BRANCH, "endpilot_sha": SHA,
        "bds_expected": "1.26.52.3", "endstone_expected": "0.11.14",
        "protocol": 2193, "transport": "raknet", "server_python_expected": "3.13",
        "client_python_expected": "3.12", "dimension_expected": "overworld",
        "target_columns": [list(c) for c in a1.TARGET_COLUMNS],
        "world_mutation_commands": [], "bds_lifecycles": 0,
        "authoritative_positions": [], "snapshots": [],
        "observer_cleanup": {"one_shot_snapshot_requests": 0, "one_shot_snapshot_responses": 0},
        "cleanup": {"runtime_context_entered": False, "runtime_context_closed": None,
                    "runtime_context_exit_invoked": False, "bds_graceful_stop": False,
                    "observer_disable_hook_observed": False},
    }
    reason = None
    try:
        if args.endpilot_commit != SHA or not str(args.server_python).endswith("python"):
            raise Blocked("pin_or_server_python_invalid")
        if sys.version_info[:2] != (3, 12):
            raise Blocked("client_python_version_mismatch")
        direct = distribution("endpilot").read_text("direct_url.json")
        if not direct or json.loads(direct).get("vcs_info", {}).get("commit_id") != SHA:
            raise Blocked("installed_endpilot_commit_mismatch")
        if version("bedrock-protocol-native") != "0.5.0":
            raise Blocked("protocol_native_version_mismatch")
        server_info = subprocess.check_output([
            str(args.server_python.absolute()), "-c",
            'import json,sys; from importlib.metadata import version; print(json.dumps([list(sys.version_info[:2]),version("endstone")]))',
        ], text=True, timeout=10)
        if json.loads(server_info) != [[3, 13], "0.11.14"]:
            raise Blocked("server_python_or_endstone_version_mismatch")
        evidence["server_python_endstone"] = json.loads(server_info)
        evidence["observer_wheel"] = a1.verify_plugin_wheel(args.plugin_wheel.resolve())
        if len(a1.TARGET_COLUMNS) != 12 or (0, 0) not in a1.TARGET_COLUMNS:
            raise Blocked("observer_target_contract_changed")
        if folder.exists():
            raise Blocked("world_not_fresh")
        folder.mkdir()
        (folder / "server.properties").write_text(
            "".join(f"{k}={v}\n" for k, v in REQUIRED.items()), encoding="utf-8"
        )
        evidence["prestart_properties"] = properties(folder / "server.properties")
        plugins = folder / "plugins"
        plugins.mkdir()
        shutil.copy2(args.plugin_wheel, plugins / args.plugin_wheel.name)
        evidence["observer_install"] = str((plugins / args.plugin_wheel.name).relative_to(ROOT))
        server = Server(args.server_python.absolute(), folder, OUT / "bds.log")
        evidence["bds_lifecycles"] += 1
        server.start()
        server.wait_ready(timeout=420)
        if not any("Version: 1.26.52.3" in line for line in server.snapshot()):
            raise Blocked("exact_bds_version_not_confirmed")
        evidence["effective_properties"] = properties(folder / "server.properties")
        evidence["version_txt"] = (folder / "version.txt").read_text().strip()
        asyncio.run(observe(server, evidence))
    except BaseException as error:
        reason = f"{type(error).__name__}: {error}"[:800]
        evidence["error"] = reason
    finally:
        if server is not None and server.process is not None:
            try:
                server.stop()
                evidence["cleanup"]["bds_graceful_stop"] = True
                evidence["cleanup"]["observer_disable_hook_observed"] = any(
                    "CHUNKOP_PLUGIN_DISABLED api_version=0.11" in line for line in server.snapshot()
                )
            except BaseException as error:
                evidence["cleanup"]["bds_stop_error"] = str(error)[:800]
        for source, name in ((folder / "server.properties", "server.properties"),
                             (folder / "version.txt", "version.txt")):
            if source.exists():
                shutil.copy2(source, OUT / name)
        complete = all((OUT / p).is_file() for p in ("bds.log", "server.properties", "version.txt"))
        good_cleanup = (evidence["cleanup"].get("runtime_context_closed") is True and
                        evidence["cleanup"].get("bds_graceful_stop") is True and
                        evidence["cleanup"].get("observer_disable_hook_observed") is True)
        if reason is None and good_cleanup and complete and evidence["bds_lifecycles"] == 1:
            if (evidence.get("origin_loaded") is True and
                    evidence.get("horizontal_distance_to_origin_center", float("inf")) <= 16):
                evidence["status"] = "SPAWN_PREFLIGHT_PASS"
            else:
                evidence["status"] = "SPAWN_CANDIDATE_REJECTED"
        elif reason is None:
            evidence["status"] = "SPAWN_PREFLIGHT_INCONCLUSIVE"
            evidence["error"] = "cleanup_or_required_artifact_incomplete"
        elif evidence["bds_lifecycles"] > 0:
            evidence["status"] = "SPAWN_PREFLIGHT_INCONCLUSIVE"
        evidence["finished_at_utc"] = utc()
        summary = {k: evidence.get(k) for k in (
            "status", "seed", "position", "chunk", "horizontal_distance_to_origin_center",
            "origin_loaded", "snapshot_id", "bds_lifecycles", "cleanup", "error",
        )}
        summary["decision"] = (
            "CANDIDATE_SEED_READY_FOR_A1B_REVIEW"
            if evidence["status"] == "SPAWN_PREFLIGHT_PASS" else "NOT_APPROVED"
        )
        save_json(OUT / "spawn-preflight-evidence.json", evidence)
        save_json(OUT / "spawn-preflight-summary.json", summary)
        write_hashes()
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if evidence["status"] in ("SPAWN_PREFLIGHT_PASS", "SPAWN_CANDIDATE_REJECTED") else 1



# Executor #28: isolated two-lifecycle landing operability preflight.
# Deliberately reuse A1b's existing manifest, properties and strict reply oracle.
import time as _landing_time
from server import configure as _landing_configure
from chunk_operability_a1b_probe import (
    manifest as _landing_manifest,
    strict_command as _landing_strict_command,
)

LANDING_BRANCH = "test/spawn-landing-seed-zero"
LANDING_OUT = ROOT / "work" / "spawn-operability-seed0"
LANDING_MESSAGE = "test(spawn): verify seed zero landing operability [20261102]"


class LandingServer(Server):
    """Observe, never modify, Server.command's native log-offset contract."""

    def __init__(self, python: Path, folder: Path, logfile: Path):
        super().__init__(python, folder, logfile)
        self.fill_offsets: list[int] = []
        self.world_commands: list[str] = []

    def command(self, value):
        if value.split(maxsplit=1)[0].lower() in {
            "fill", "setblock", "tp", "teleport", "tickingarea", "clone",
            "setworldspawn", "spawnpoint", "structure", "place", "summon",
            "kill", "execute", "setblock", "gamerule", "weather", "time",
        }:
            if value != a1.SAFE_LANDING_COMMAND or self.world_commands:
                raise Blocked("prohibited_world_mutation_command:" + value[:120])
            self.world_commands.append(value)
        offset = super().command(value)
        if value == a1.SAFE_LANDING_COMMAND:
            self.fill_offsets.append(offset)
        return offset


def landing_prepare(folder: Path) -> dict:
    _landing_configure(folder / "server.properties", "raknet")
    return properties(folder / "server.properties")


def landing_stable_manifest(worlds: Path) -> dict:
    first = _landing_manifest(worlds)
    _landing_time.sleep(0.25)
    second = _landing_manifest(worlds)
    if first != second:
        raise Blocked("world_manifest_not_quiescent")
    return second


async def landing_prime(server: Server, evidence: dict) -> None:
    from endpilot import BotOptions, Runtime

    async with a1.ObservedRuntimeContext(Runtime(), evidence["cleanup"]) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=2193, name=PLAYER, chunk_radius=8,
            client_cache=False, validate_all_packets=True,
        ))
        await bot.wait_ready(70)
        evidence["ready"] = a1.client_status(bot)
        evidence["online"] = (await asyncio.to_thread(server.wait_player, True, 15))[-20:]
    evidence["offline"] = (await asyncio.to_thread(server.wait_player, False, 15))[-20:]


async def landing_condition(server: LandingServer, evidence: dict) -> None:
    from endpilot import BotOptions, Runtime

    deadline = asyncio.get_running_loop().time() + 120
    # Observer is installed only on this copy; READY is an async lifecycle event.
    await a1.wait_plugin_ready(server, deadline)
    async with a1.ObservedRuntimeContext(Runtime(), evidence["cleanup"]) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=2193, name=PLAYER, chunk_radius=8,
            client_cache=False, validate_all_packets=True,
        ))
        await bot.wait_ready(min(70, a1.remaining(deadline)))
        first_status = a1.client_status(bot)
        evidence["bot_ready"] = first_status
        evidence["session"] = first_status["session_id"]
        evidence["online"] = (await asyncio.to_thread(server.wait_player, True, 15))[-20:]
        first = await a1.stable_authoritative_position(server, "initial", deadline, evidence)
        evidence["authoritative_position"] = first
        if first_status["dimension"] != 0 or first["entity"].get("dimension") != 0:
            raise Blocked("not_overworld")
        snapshot = await a1.request_snapshot(server, "pre_landing", deadline, evidence)
        evidence["snapshot_id"] = snapshot["snapshot_id"]
        states = snapshot["target_column_states"]
        if (len(states) != 12 or
                {(s["chunk_x"], s["chunk_z"]) for s in states} != set(a1.TARGET_COLUMNS)):
            raise Blocked("target_column_set_invalid")
        evidence["target_column_states"] = states
        followup = await a1.stable_authoritative_position(server, "after_snapshot", deadline, evidence)
        evidence["authoritative_after_snapshot"] = followup
        second_status = a1.client_status(bot)
        evidence["bot_after_snapshot"] = second_status
        if (second_status["session_id"] != evidence["session"] or
                second_status["dimension"] != 0 or
                followup["entity"].get("dimension") != 0 or
                max(abs(x - y) for x, y in zip(a1.position_values(first),
                                               a1.position_values(followup))) > .05):
            raise Blocked("session_dimension_or_authoritative_position_changed")
        origin = [s for s in states if (s["chunk_x"], s["chunk_z"]) == (0, 0)]
        if len(origin) != 1 or type(origin[0]["loaded"]) is not bool:
            raise Blocked("origin_loaded_state_ambiguous")
        evidence["origin_loaded"] = origin[0]["loaded"]
        if not origin[0]["loaded"]:
            evidence["status"] = "LANDING_OPERABILITY_REJECTED"
            evidence["reason"] = "origin_not_reported_loaded_precondition"
        else:
            if server.world_commands or server.fill_offsets:
                raise Blocked("world_write_before_landing")
            landing = await _landing_strict_command(
                server, a1.SAFE_LANDING_COMMAND, "fill", "safe_landing_block",
                deadline, snapshot["snapshot_id"], evidence,
                {"x": [0, 0], "y": [99, 99], "z": [0, 0]},
            )
            landing["log_offset"] = server.fill_offsets[0] if len(server.fill_offsets) == 1 else None
            evidence["safe_landing"] = landing
            evidence["safety_gate"] = a1.safe_landing_gate(landing)
            evidence["status"] = (
                "LANDING_OPERABILITY_PASS" if evidence["safety_gate"]["status"] == "ALLOW"
                else "LANDING_OPERABILITY_REJECTED"
            )
            evidence["reason"] = evidence["safety_gate"]["reason"]
            # A1b's gate is unchanged but intentionally never authorizes TP here.
    evidence["offline"] = (await asyncio.to_thread(server.wait_player, False, 15))[-20:]


def landing_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--plugin-wheel", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    LANDING_OUT.mkdir(parents=True)
    summary = {
        "status": "LANDING_OPERABILITY_EXECUTION_BLOCKED",
        "seed": SEED, "world_internal_seed": "UNVERIFIED",
        "testbed_parent": "fb4f0aa395c8d2bad665988bedc96c9cf5b3160a",
        "expected_branch": LANDING_BRANCH, "expected_commit_message": LANDING_MESSAGE,
        "endpilot_sha": SHA, "bds_version": "1.26.52.3", "endstone": "0.11.14",
        "protocol": 2193, "native_package": "0.5.0", "transport": "raknet",
        "server_python": "3.13", "client_python": "3.12",
        "started_at_utc": utc(), "bds_lifecycles": 0,
        "fill_count": 0, "teleport_count": 0, "diagnostic_count": 0,
        "online_block_readbacks": 0, "active_chunk_requests": 0,
        "snapshots_used": 0,
    }
    base_info = {"cleanup": {"runtime_context_closed": False, "bds_graceful_stop": False}}
    evidence = {
        "status": "LANDING_OPERABILITY_INCONCLUSIVE", "seed": SEED,
        "commands": [], "snapshots": [], "authoritative_positions": [],
        "observer_cleanup": {"one_shot_snapshot_requests": 0, "one_shot_snapshot_responses": 0},
        "cleanup": {"runtime_context_closed": False, "bds_graceful_stop": False,
                    "observer_disable_hook_observed": False},
        "safety_gate": {"status": "UNVERIFIED"},
        "teleport": "NOT_EXECUTED", "diagnostic_operations": [],
    }
    base: LandingServer | None = None
    condition: LandingServer | None = None
    baseline_dir = work / "baseline"
    condition_dir = work / "condition-A"
    try:
        if args.endpilot_commit != SHA or sys.version_info[:2] != (3, 12):
            raise Blocked("sdk_or_client_python_pin_invalid")
        direct = distribution("endpilot").read_text("direct_url.json")
        if not direct or json.loads(direct).get("vcs_info", {}).get("commit_id") != SHA:
            raise Blocked("installed_endpilot_commit_mismatch")
        if version("bedrock-protocol-native") != "0.5.0":
            raise Blocked("native_package_pin_invalid")
        server_info = subprocess.check_output([
            str(args.server_python.absolute()), "-c",
            'import json,sys; from importlib.metadata import version; print(json.dumps([list(sys.version_info[:2]),version("endstone")]))',
        ], text=True, timeout=10)
        if json.loads(server_info) != [[3, 13], "0.11.14"]:
            raise Blocked("server_python_or_endstone_pin_invalid")
        summary["observer_wheel"] = a1.verify_plugin_wheel(args.plugin_wheel.resolve())
        if len(a1.TARGET_COLUMNS) != 12 or (0, 0) not in a1.TARGET_COLUMNS:
            raise Blocked("observer_columns_contract_invalid")
        baseline_dir.mkdir()
        (baseline_dir / "server.properties").write_text(
            "".join(f"{key}={value}\n" for key, value in
                    {**PROPERTIES, "level-name": "Bedrock level", "level-seed": SEED}.items()),
            encoding="utf-8",
        )
        base_info["server_properties"] = properties(baseline_dir / "server.properties")
        base = LandingServer(args.server_python.absolute(), baseline_dir, LANDING_OUT / "baseline-bds.log")
        summary["bds_lifecycles"] += 1
        base.start()
        base.wait_ready()
        if not any("Version: 1.26.52.3" in x for x in base.snapshot()):
            raise Blocked("baseline_bds_version_invalid")
        base_info["effective_properties"] = properties(baseline_dir / "server.properties")
        base_info["version_txt"] = (baseline_dir / "version.txt").read_text().strip()
        asyncio.run(landing_prime(base, base_info))
        base.stop()
        base_info["world_mutation_commands"] = base.world_commands[:]  # zero by contract
        base_info["cleanup"]["bds_graceful_stop"] = True
        base = None
        if base_info["cleanup"].get("runtime_context_closed") is not True:
            raise Blocked("baseline_runtime_not_closed")
        original = landing_stable_manifest(baseline_dir / "worlds")
        summary["baseline_world_hash"] = original["hash"]
        save_json(LANDING_OUT / "baseline-world-manifest.json", original)
        shutil.copytree(baseline_dir, condition_dir)
        copy_before = landing_stable_manifest(condition_dir / "worlds")
        if copy_before != original:
            raise Blocked("BASELINE_WORLD_ISOLATION_BLOCKED")
        summary["copy_world_hash"] = copy_before["hash"]
        save_json(LANDING_OUT / "condition-world-manifest.json", copy_before)
        evidence["world_copy_hash"] = copy_before["hash"]
        evidence["baseline_world_hash"] = original["hash"]
        evidence["prestart_properties"] = landing_prepare(condition_dir)
        props_path = condition_dir / "server.properties"
        if any(properties(props_path).get(key) != value for key, value in CHECKED.items()):
            raise Blocked("condition_properties_invalid")
        plugins = condition_dir / "plugins"
        plugins.mkdir(exist_ok=True)
        shutil.copy2(args.plugin_wheel, plugins / args.plugin_wheel.name)
        shutil.copy2(props_path, LANDING_OUT / "server.properties")
        if landing_stable_manifest(condition_dir / "worlds") != original:
            raise Blocked("world_changed_before_condition_start")
        condition = LandingServer(args.server_python.absolute(), condition_dir, LANDING_OUT / "condition-bds.log")
        summary["bds_lifecycles"] += 1
        condition.start()
        condition.wait_ready()
        if not any("Version: 1.26.52.3" in x for x in condition.snapshot()):
            raise Blocked("condition_bds_version_invalid")
        evidence["effective_properties"] = properties(props_path)
        asyncio.run(landing_condition(condition, evidence))
    except BaseException as error:
        summary["error"] = f"{type(error).__name__}: {error}"[:1000]
        if evidence.get("status") != "LANDING_OPERABILITY_REJECTED":
            evidence["status"] = "LANDING_OPERABILITY_INCONCLUSIVE"
            evidence["error"] = summary["error"]
    finally:
        for kind, current, info in (("condition", condition, evidence), ("baseline", base, base_info)):
            if current is not None:
                summary["fill_count"] += len(current.fill_offsets)
                info["world_mutation_commands"] = current.world_commands[:]
                if current.process is not None:
                    try:
                        current.stop()
                        info["cleanup"]["bds_graceful_stop"] = True
                        if kind == "condition":
                            info["cleanup"]["observer_disable_hook_observed"] = any(
                                "CHUNKOP_PLUGIN_DISABLED api_version=0.11" in x for x in current.snapshot()
                            )
                    except BaseException as error:
                        info["cleanup"]["bds_stop_error"] = str(error)[:800]
        if condition is not None and condition.fill_offsets and evidence["commands"]:
            evidence["commands"][0].setdefault("log_offset", condition.fill_offsets[0])
        summary["snapshots_used"] = len(evidence["snapshots"])
        summary["baseline_cleanup"] = base_info["cleanup"]
        summary["condition_cleanup"] = evidence["cleanup"]
        summary["status"] = evidence["status"]
        if (summary["status"] in ("LANDING_OPERABILITY_PASS", "LANDING_OPERABILITY_REJECTED") and
                not (base_info["cleanup"].get("bds_graceful_stop") is True and
                     base_info["cleanup"].get("runtime_context_closed") is True and
                     evidence["cleanup"].get("runtime_context_closed") is True and
                     evidence["cleanup"].get("bds_graceful_stop") is True and
                     evidence["cleanup"].get("observer_disable_hook_observed") is True and
                     summary["bds_lifecycles"] == 2 and summary["fill_count"] <= 1)):
            summary["status"] = "LANDING_OPERABILITY_INCONCLUSIVE"
            summary["error"] = "cleanup_or_budget_evidence_incomplete"
        if (summary["status"] == "LANDING_OPERABILITY_PASS" and
                not (summary["fill_count"] == 1 and
                     len(evidence["commands"]) == 1 and
                     evidence["commands"][0].get("log_offset") is not None and
                     evidence.get("safety_gate", {}).get("status") == "ALLOW")):
            summary["status"] = "LANDING_OPERABILITY_INCONCLUSIVE"
            summary["error"] = "landing_command_or_correlation_incomplete"
        if summary["status"] == "LANDING_OPERABILITY_INCONCLUSIVE" and summary["bds_lifecycles"] == 0:
            summary["status"] = "LANDING_OPERABILITY_EXECUTION_BLOCKED"
        summary["decision"] = (
            "SEED_0_OPERATIONALLY_ELIGIBLE_FOR_A1B_REVIEW" if summary["status"] == "LANDING_OPERABILITY_PASS"
            else "NOT_APPROVED"
        )
        summary["finished_at_utc"] = utc()
        save_json(LANDING_OUT / "condition-evidence.json", evidence)
        save_json(LANDING_OUT / "condition-result.json", {
            "status": summary["status"], "reason": evidence.get("reason"),
            "safe_landing": evidence.get("safe_landing"),
            "safety_gate": evidence["safety_gate"],
        })
        save_json(LANDING_OUT / "baseline-session.json", base_info)
        save_json(LANDING_OUT / "summary.json", summary)
        write_landing_hashes()
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["status"] in ("LANDING_OPERABILITY_PASS", "LANDING_OPERABILITY_REJECTED") else 1


def write_landing_hashes() -> None:
    entries = sorted(p for p in LANDING_OUT.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    rows = [f"{sha256(p)}  {p.relative_to(LANDING_OUT)}" for p in entries]
    (LANDING_OUT / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")
    if any(sha256(LANDING_OUT / r.split("  ", 1)[1]) != r.split("  ", 1)[0] for r in rows):
        raise Blocked("artifact_hash_verification_failed")

if __name__ == "__main__":
    if "--landing-operability" in sys.argv:
        sys.argv.remove("--landing-operability")
        sys.exit(landing_main())
    sys.exit(main())
