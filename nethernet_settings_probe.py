"""Run the released NetherNet settings probe against a fresh real BDS."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from server import ROOT, Server, configure


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
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
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if completed.stdout:
            print("[probe] " + completed.stdout.strip(), flush=True)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, flush=True)
        if completed.returncode != 0:
            raise RuntimeError(f"EndPilot probe exited with code {completed.returncode}")

        algorithm, threshold = completed.stdout.strip().split()
        results["compression"] = {
            "algorithm": algorithm,
            "threshold": int(threshold),
        }
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
