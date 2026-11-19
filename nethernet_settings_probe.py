"""Run new-client NetherNet settings, join or position smoke on fresh BDS."""
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

from server import ROOT, Server, configure


def server_position(server):
    """Read the position accepted by BDS, not a client-side prediction."""
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
                if len(targets) != 1 or targets[0]["dimension"] != 0:
                    raise AssertionError(f"Unexpected querytarget result: {targets}")
                return targets[0]["position"]
        time.sleep(0.05)
    raise TimeoutError(f"No BDS querytarget response: {server.snapshot(offset)[-6:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--join", action="store_true")
    parser.add_argument("--position", action="store_true")
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
        if args.join or args.position:
            command += ["--name", "EndPilotTest", "--hold", "2" if args.position else "15"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                server.wait_player(True, timeout=25)
                results["checks"].append("bds_list_confirmed_player_online")
                if args.position:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        if any("Player Spawned: EndPilotTest" in line for line in server.snapshot()):
                            break
                        if process.poll() is not None:
                            raise RuntimeError("position client exited before BDS spawn")
                        time.sleep(0.1)
                    else:
                        raise TimeoutError("BDS never confirmed player spawn")
                    server.command("tp EndPilotTest 0.5 68 0.5")
                    time.sleep(0.5)
                    authoritative = server_position(server)
                    results["checks"].append("bds_teleport_and_querytarget_confirmed")
                stdout, stderr = process.communicate(timeout=30)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            if stdout:
                print("[join] " + stdout.strip(), flush=True)
            if stderr:
                print(stderr, file=sys.stderr, flush=True)
            if process.returncode != 0 or "spawned EndPilotTest" not in stdout or "disconnected EndPilotTest" not in stdout:
                raise RuntimeError(f"EndPilot player script failed (exit={process.returncode}): {stdout} {stderr}")
            results["checks"].append("client_reached_spawn_and_closed")
            if args.position:
                match = re.search(r"^server_position_update=(.+)$", stdout, re.M)
                if not match:
                    raise AssertionError(f"Client did not observe BDS teleport: {stdout}")
                observed = json.loads(match.group(1))
                if any(abs(float(authoritative[k]) - float(observed[k])) > 0.05 for k in ("x", "y", "z")):
                    raise AssertionError(f"BDS/client positions differ: {authoritative} vs {observed}")
                results["position_sync"] = {"bds": authoritative, "client": observed}
                results["checks"].append("server_teleport_observed_by_client")
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
