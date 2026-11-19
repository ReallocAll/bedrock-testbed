"""Run the released NetherNet settings probe against a fresh real BDS."""
from __future__ import annotations

import argparse
import json
import math
import time
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from server import ROOT, Server, configure


def server_position(server):
    """Return BDS querytarget coordinates, independent of client prediction."""
    offset = server.command("querytarget EndPilotTest")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lines = server.snapshot(offset)
        fields = [line.partition("]:")[2].strip() for line in lines]
        start = next((i for i, s in enumerate(fields) if s.startswith("Target data:")), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(fields)) if fields[i] == "]"), None)
            if end is not None:
                raw = fields[start].split("Target data:", 1)[1] + " ".join(fields[start + 1:end + 1])
                targets = json.loads(raw)
                if len(targets) != 1:
                    raise AssertionError(f"Unexpected querytarget result: {targets}")
                return targets[0]["position"]
        if any("unknown command" in s.lower() or "syntax error" in s.lower() for s in lines):
            raise RuntimeError(f"querytarget unavailable: {lines[-6:]}")
        time.sleep(0.05)
    raise TimeoutError(f"No complete querytarget response: {server.snapshot(offset)[-6:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--join", action="store_true", help="Verify server-side player entry and exit")
    parser.add_argument("--walk", action="store_true", help="Verify server-authoritative forward displacement")
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    folder = work / "server"
    results = {
        "status": "FAIL",
        "bds": "1.26.52",
        "endstone": "0.11.14",
        "protocol": 2193,
        "transport": "nethernet",
        "endpilot_commit": args.endpilot_commit,
        "installed_endpilot_commit": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": [],
    }
    server = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("--endpilot-commit must be a full lowercase SHA")
        metadata = distribution("endpilot").read_text("direct_url.json")
        installed = json.loads(metadata or "{}").get("vcs_info", {}).get("commit_id")
        results["installed_endpilot_commit"] = installed
        if installed != args.endpilot_commit:
            raise RuntimeError(f"Installed EndPilot commit {installed!r} does not match requested SHA")
        results["checks"].append("endpilot_installed_commit_verified")

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        results["checks"].append("official_bds_bootstrap_version_verified")

        configure(folder / "server.properties", "nethernet")
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        results["checks"].append("clean_nethernet_bds_ready")

        command = [sys.executable, str(args.probe.absolute()), "http://127.0.0.1:19132"]
        if args.join or args.walk:
            command += ["--name", "EndPilotTest"]
            if args.walk:
                command += ["--before", "5", "--after", "6"]
            else:
                command += ["--hold", "15"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                server.wait_player(True, timeout=25)
                results["checks"].append("bds_list_confirmed_player_online")
                if args.walk:
                    origin = server_position(server)
                    deadline = time.monotonic() + 16
                    while time.monotonic() < deadline:
                        current = server_position(server)
                        distance = math.hypot(current["x"] - origin["x"], current["z"] - origin["z"])
                        if distance >= 0.5:
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.35)
                    results["movement"] = {"origin": origin, "final_observed": current, "horizontal_displacement": round(distance, 3)}
                    if distance < 0.5:
                        raise AssertionError(f"BDS did not observe forward movement: {results['movement']}")
                    results["checks"].append("real_bds_querytarget_displacement_confirmed")
                stdout, stderr = process.communicate(timeout=30)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            if stdout:
                print("[join] " + stdout.strip(), flush=True)
            if stderr:
                print(stderr, file=sys.stderr, flush=True)
            if process.returncode != 0 or "spawned EndPilotTest" not in stdout or "disconnected EndPilotTest" not in stdout or (args.walk and "submitted 34 input frames" not in stdout):
                raise RuntimeError(f"EndPilot join/leave failed (exit={process.returncode})")
            results["checks"].append("client_reached_spawn_and_closed")
            server.wait_player(False, timeout=20)
            results["checks"].append("bds_list_confirmed_player_offline")
        else:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if completed.stdout:
                print("[probe] " + completed.stdout.strip(), flush=True)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, flush=True)
            if completed.returncode != 0:
                raise RuntimeError(f"EndPilot probe exited with code {completed.returncode}")
            algorithm, threshold = completed.stdout.strip().split()
            results["compression"] = {"algorithm": algorithm, "threshold": int(threshold)}
            results["checks"].append("real_bds_network_settings_received")

        server.stop()
        server = None
        results["checks"].append("bds_graceful_stop_no_processes")
        results["status"] = "PASS"
    except BaseException as error:
        results["error"] = f"{type(error).__name__}: {error}"
        print(results["error"], file=sys.stderr, flush=True)
    finally:
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                results["cleanup_error"] = f"{type(error).__name__}: {error}"
                results["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
