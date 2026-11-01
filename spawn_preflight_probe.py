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
BRANCH = "test/spawn-preflight-20261102"
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
        "testbed_parent": "ed5ec9fd2bfec7ec3956fa396daa26266681657e",
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
        if not any("CHUNKOP_PLUGIN_READY api_version=0.11" in line for line in server.snapshot()):
            raise Blocked("observer_enable_not_confirmed")
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


if __name__ == "__main__":
    sys.exit(main())
