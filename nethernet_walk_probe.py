"""Exercise the new Python SDK's input path against a fresh real NetherNet BDS."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from nethernet_settings_probe import server_position
from server import ROOT, Server, configure


def filled(server, command):
    for _ in range(5):
        offset = server.command(command)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lines = server.snapshot(offset)
            if any("blocks filled" in line.lower() for line in lines):
                return
            if any("outside of the world" in line.lower() for line in lines):
                break
            time.sleep(0.05)
        else:
            raise TimeoutError(f"BDS did not answer {command}: {lines[-5:]}")
        time.sleep(0.6)
    raise AssertionError(f"BDS could not prepare walking lane: {lines[-5:]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    result = {
        "status": "FAIL", "transport": "nethernet", "bds": "1.26.52",
        "endstone": "0.11.14", "protocol": 2193,
        "endpilot_commit": args.endpilot_commit,
        "timestamp": datetime.now(timezone.utc).isoformat(), "checks": [],
    }
    server = None
    player = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("endpilot_commit requires the exact lowercase SHA")
        meta = distribution("endpilot").read_text("direct_url.json")
        installed = json.loads(meta or "{}").get("vcs_info", {}).get("commit_id")
        result["installed_endpilot_commit"] = installed
        if installed != args.endpilot_commit:
            raise AssertionError(f"Installed EndPilot commit differs: {installed}")
        result["checks"].append("installed_endpilot_commit_verified")

        folder = work / "server"
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        result["checks"].append("official_bds_bootstrap_verified")

        configure(folder / "server.properties", "nethernet")
        props_path = folder / "server.properties"
        props_path.write_text(props_path.read_text() + "\nserver-authoritative-movement=server-auth\n")
        properties = (folder / "server.properties").read_text().splitlines()
        authoritative = next((line for line in properties
                              if line.startswith("server-authoritative-movement=")), None)
        result["movement_mode"] = authoritative
        print("[testbed] " + str(authoritative), flush=True)
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()

        player = subprocess.Popen(
            [sys.executable, str(args.probe.absolute()), "http://127.0.0.1:19132",
             "--name", "EndPilotTest", "--ticks", "24", "--hold", "4"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        server.wait_player(True, timeout=30)
        result["checks"].append("bds_list_player_online")
        deadline = time.monotonic() + 12
        while not any("Player Spawned: EndPilotTest" in x for x in server.snapshot()):
            if player.poll() is not None:
                raise RuntimeError("Player exited before BDS spawn")
            if time.monotonic() > deadline:
                raise TimeoutError("BDS did not confirm spawn")
            time.sleep(0.1)

        # Load the chunk before editing blocks; BDS can reject remote fills.
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        filled(server, "fill -8 100 -8 8 105 8 air")
        filled(server, "fill -8 99 -8 8 99 8 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.6)
        before = server_position(server)
        result["checks"].append("flat_lane_and_server_origin_confirmed")

        start = time.monotonic()
        player.stdin.write("go\n")
        player.stdin.flush()
        time.sleep(3)
        after = server_position(server)
        result["walking"] = {
            "before": before, "after": after,
            "forward_distance": after["z"] - before["z"],
            "server_observation_ms": round((time.monotonic() - start) * 1000, 2),
        }
        stdout, stderr = player.communicate(timeout=30)
        print("[client] " + stdout, flush=True)
        if stderr:
            print(stderr, file=sys.stderr, flush=True)
        result["walking"]["client_exit_code"] = player.returncode
        result["walking"]["client_stderr"] = stderr[-3000:]
        if after["z"] - before["z"] < 1.0:
            raise AssertionError(f"BDS did not accept forward movement: {result['walking']}")
        result["checks"].append("bds_querytarget_confirmed_walk")
        if player.returncode != 0 or "disconnected EndPilotTest" not in stdout:
            raise RuntimeError(f"Walking script failed: exit={player.returncode}: {stderr}")
        match = re.search(r"^walk_input_submitted=(.+)$", stdout, re.M)
        if match is None:
            raise AssertionError(f"No input submission record: {stdout}")
        frames = json.loads(match.group(1))
        if frames["frames"] != 24:
            raise AssertionError(f"Incorrect input count: {frames}")
        result["walking"]["input"] = frames
        result["checks"].append("client_submitted_24_frames_and_cleanly_left")
        server.wait_player(False, timeout=15)
        result["checks"].append("bds_list_player_offline")
        server.stop()
        server = None
        result["checks"].append("bds_graceful_stop")
        result["status"] = "PASS"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(result["error"], file=sys.stderr, flush=True)
    finally:
        if player is not None and player.poll() is None:
            player.kill()
            player.communicate()
        if server is not None:
            try:
                server.stop()
            except BaseException as exc:
                result["cleanup_error"] = repr(exc)
                result["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
