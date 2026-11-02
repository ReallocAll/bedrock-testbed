"""Disposable, bounded BDS 26.52 block read side-effect calibration."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from server import PLAYER, ROOT, PROPERTIES, Server

SEED = "73005203"
ENDPILOT_SHA = "1d530f0efbb3b0ac03f0945f2f5d89efa81eee3a"
BRANCH = "test/block-read-side-effects"
MARK_SNAPSHOT = "CHUNKOP_SNAPSHOT "
MARK_READ = "CHUNKOP_READ "
CASES = [("L", a) for a in ("control", "handle", "type", "data", "command")] + [
    ("U", a) for a in ("control", "type", "command")]


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def world_manifest(worlds):
    rows = []
    for file in sorted(p for p in worlds.rglob("*") if p.is_file()):
        rows.append({"path": file.relative_to(worlds).as_posix(), "size": file.stat().st_size,
                     "sha256": digest(file)})
    if not rows:
        raise RuntimeError("world manifest is empty")
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"files": rows, "hash": hashlib.sha256(raw).hexdigest(), "count": len(rows)}


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def marker(lines, prefix, request_id):
    matches = []
    for line in lines:
        if prefix not in line:
            continue
        try:
            row = json.loads(line.split(prefix, 1)[1].strip())
        except json.JSONDecodeError:
            continue
        if row.get("request_id") == request_id:
            matches.append(row)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {prefix} response for {request_id}, got {len(matches)}")
    return matches[0]


async def marker_command(server, command, prefix, timeout=7):
    request_id = uuid.uuid4().hex
    offset = server.command(command.format(id=request_id))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            return marker(server.snapshot(offset), prefix, request_id)
        except RuntimeError:
            await asyncio.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for {prefix} {request_id}: {server.snapshot(offset)[-10:]}")


def parse_target(server, offset):
    lines = server.snapshot(offset)
    fields = [line.partition("]:")[2].strip() for line in lines]
    begin = next((i for i, s in enumerate(fields) if s.startswith("Target data:")), None)
    if begin is None:
        return None
    end = next((i for i in range(begin + 1, len(fields)) if fields[i] == "]"), None)
    if end is None:
        return None
    raw = fields[begin].split("Target data:", 1)[1] + " ".join(fields[begin + 1:end + 1])
    try:
        rows = json.loads(raw)
        if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
            return rows[0]
    except json.JSONDecodeError:
        pass
    return None


async def authoritative_position(server):
    offset = server.command(f"querytarget {PLAYER}")
    end = time.monotonic() + 7
    while time.monotonic() < end:
        row = parse_target(server, offset)
        if row is not None:
            if row.get("dimension") != 0 or not isinstance(row.get("position"), dict):
                raise RuntimeError(f"Player not in overworld: {row}")
            return row
        await asyncio.sleep(0.05)
    raise RuntimeError("BDS querytarget did not return authoritative position")


async def with_player(server, task):
    from endpilot import BotOptions, Runtime
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet", protocol=2193,
            name=PLAYER, chunk_radius=8, client_cache=False, validate_all_packets=True))
        await bot.wait_ready(70)
        online, lines = await asyncio.to_thread(server.player_online)
        if not online:
            raise RuntimeError(f"BDS player presence not confirmed: {lines[-8:]}")
        position = await authoritative_position(server)
        return await task(position)


async def snapshot(server):
    row = await marker_command(server, "chunkop snapshot {id}", MARK_SNAPSHOT)
    if row.get("status") != "ok" or sorted(row.get("targets", {})) != ["L", "U"]:
        raise RuntimeError(f"Invalid snapshot: {row}")
    return row


async def one_case(server, label, operation, targets):
    async def observe(position):
        started = time.monotonic()
        await asyncio.sleep(5.0)
        before = await snapshot(server)
        expected = label == "L"
        if before["targets"][label]["loaded"] is not expected:
            raise RuntimeError(f"{label} target did not meet precondition: {before}")
        action = {"kind": operation, "status": "skipped_control"}
        if operation in ("handle", "type", "data"):
            action = await marker_command(
                server, f"calread {{id}} {operation} {label}", MARK_READ)
            if action.get("status") != "ok":
                raise RuntimeError(f"Target read error: {action}")
        elif operation == "command":
            x, y, z = targets[label]
            offset = server.command(f"testforblock {x} {y} {z} minecraft:air")
            await asyncio.sleep(0.5)
            lines = server.snapshot(offset)
            if not lines:
                raise RuntimeError("testforblock command has no BDS response")
            action = {"kind": "command", "status": "response_recorded", "raw_lines": lines[-12:]}
        else:
            await asyncio.sleep(0.5)
        after = await snapshot(server)
        if time.monotonic() - started > 60:
            raise RuntimeError("Per-group observation budget exceeded")
        return {"status": "PASS", "condition": label, "operation": operation, "position": position,
                "before": before, "action": action, "after": after,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "changed_targets": [k for k in ("L", "U")
                                    if before["targets"][k]["loaded"] != after["targets"][k]["loaded"]]}
    return await with_player(server, observe) if label == "L" else await observe(None)


def make_properties(path):
    path.mkdir(parents=True, exist_ok=True)
    props = {**PROPERTIES, "level-name": "Bedrock level", "level-seed": SEED}
    (path / "server.properties").write_text("\n".join(f"{k}={v}" for k, v in props.items()) + "\n")


def sha_sums(work):
    names = ["calibration-summary.json", "calibration-conditions.json",
             "baseline-world-manifest.json", "bds.log", "result.json"]
    lines = [f"{digest(work / name)}  {name}" for name in names if (work / name).is_file()]
    (work / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    for line in lines:
        hashed, name = line.split("  ", 1)
        assert digest(work / name) == hashed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--plugin-wheel", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    summary = {"status": "BLOCKED", "conclusion": "CALIBRATION_INCONCLUSIVE",
               "started_at_utc": utc(), "seed": SEED, "branch": BRANCH,
               "bds_expected": "1.26.52.3", "endstone_expected": "0.11.14",
               "subchunk_status": "SUBCHUNK_EFFECT_UNVERIFIED", "conditions": []}
    conditions = []
    base_server = None
    running = None
    baseline = work / "baseline"
    try:
        if args.endpilot_commit != ENDPILOT_SHA:
            raise RuntimeError("Unexpected EndPilot commit request")
        meta = distribution("endpilot").read_text("direct_url.json")
        if not meta or json.loads(meta).get("vcs_info", {}).get("commit_id") != ENDPILOT_SHA:
            raise RuntimeError("EndPilot installed commit mismatch")
        if not args.plugin_wheel.is_file():
            raise RuntimeError("Observer wheel missing")
        make_properties(baseline)
        base_server = Server(args.server_python.absolute(), baseline, work / "bds.log")
        base_server.start()
        base_server.wait_ready()
        if not any("Version: 1.26.52.3" in line for line in base_server.snapshot()):
            raise RuntimeError("BDS patch version is not independently confirmed")
        if f"level-seed={SEED}" not in (baseline / "server.properties").read_text():
            raise RuntimeError("Fixed world seed property missing")
        initial_position = asyncio.run(with_player(base_server, lambda position: asyncio.sleep(0, result=position)))
        base_server.stop()
        base_server = None
        worlds = baseline / "worlds"
        original = world_manifest(worlds)
        dump(work / "baseline-world-manifest.json", {"seed": SEED, **original})
        pos = initial_position["position"]
        loaded = [math.floor(float(pos["x"])), 80, math.floor(float(pos["z"]))]
        unloaded = [40000, 80, 40000]
        targets = {"L": loaded, "U": unloaded}
        summary.update(initial_position=initial_position, targets=targets,
                       tick_distance=PROPERTIES["tick-distance"],
                       view_distance=PROPERTIES["view-distance"],
                       baseline_manifest_sha256=original["hash"], baseline_world_files=original["count"])
        os.environ["CALIBRATION_TARGETS_JSON"] = json.dumps(targets, separators=(",", ":"))
        for index, (label, operation) in enumerate(CASES, 1):
            if index > 8:
                raise RuntimeError("Case budget exceeded")
            trial_dir = work / f"trial-{index:02d}-{label}-{operation}"
            shutil.copytree(baseline, trial_dir)
            if world_manifest(trial_dir / "worlds")["hash"] != original["hash"]:
                raise RuntimeError("Initial world copy mismatch")
            plugin_dir = trial_dir / "plugins"
            plugin_dir.mkdir(exist_ok=True)
            shutil.copy2(args.plugin_wheel, plugin_dir / args.plugin_wheel.name)
            running = Server(args.server_python.absolute(), trial_dir, work / "bds.log")
            row = {"index": index, "condition": label, "operation": operation,
                   "world_copy_verified": True, "target_xyz": targets[label],
                   "target_chunk": [targets[label][0] // 16, targets[label][2] // 16],
                   "status": "BLOCKED"}
            conditions.append(row)
            running.start()
            running.wait_ready()
            if not any("Version: 1.26.52.3" in line for line in running.snapshot()):
                raise RuntimeError("Trial BDS patch version mismatch")
            end = time.monotonic() + 8
            while time.monotonic() < end:
                if any("CHUNKOP_PLUGIN_READY" in line for line in running.snapshot()):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Observer plugin ready marker absent")
            row.update(asyncio.run(one_case(running, label, operation, targets)))
            running.stop()
            if not any("CHUNKOP_PLUGIN_DISABLED" in line for line in running.snapshot()):
                raise RuntimeError("Observer did not unload cleanly")
            row["graceful_stop"] = True
            row["offline_world_comparison"] = "OFFLINE_STATE_COMPARISON_INCONCLUSIVE"
            row["changed_files"] = sorted({r["path"] for r in world_manifest(trial_dir / "worlds")["files"]
                                           if r not in original["files"]})[:100]
            running = None
            dump(work / "calibration-conditions.json", conditions)
        summary["conditions"] = [{"condition": r["condition"], "operation": r["operation"],
                                  "changed_targets": r["changed_targets"]} for r in conditions]
        summary["status"] = "PASS"
        controls = {r["condition"]: r for r in conditions if r["operation"] == "control"}
        if any(controls[k]["changed_targets"] for k in ("L", "U")):
            summary["conclusion"] = "CALIBRATION_INCONCLUSIVE"
            summary["status"] = "BLOCKED"
            summary["error"] = "Control snapshots drifted; natural Tick confounding cannot be excluded"
        elif any(r["changed_targets"] for r in conditions if r["operation"] != "control"):
            summary["conclusion"] = "OBSERVABLE_SIDE_EFFECT_CONFIRMED"
        else:
            summary["conclusion"] = "NO_OBSERVABLE_CHUNK_EFFECT_IN_TESTED_SCOPE"
    except BaseException as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"[:800]
        if conditions:
            conditions[-1]["error"] = summary["error"]
        print(summary["error"], file=sys.stderr, flush=True)
    finally:
        for process in (running, base_server):
            if process is not None and process.process is not None:
                try:
                    process.stop()
                except BaseException as exc:
                    summary["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:500]
                    summary["status"] = "FAIL"
        os.environ.pop("CALIBRATION_TARGETS_JSON", None)
        summary["finished_at_utc"] = utc()
        dump(work / "calibration-summary.json", summary)
        dump(work / "calibration-conditions.json", conditions)
        dump(work / "result.json", {"status": summary["status"], "conclusion": summary["conclusion"],
                                    "error": summary.get("error"), "checks": len(conditions)})
        sha_sums(work)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
