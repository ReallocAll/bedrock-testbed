"""Run a clean Endstone/BDS lifecycle and EndPilot public-API integration tests.

Ubuntu Linux only. Endstone and EndPilot are installed in separate venvs by CI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from importlib.metadata import distribution
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True, help="Full SHA expected from the EndPilot VCS install")
    parser.add_argument("--transport", choices=("raknet", "nethernet"), default="raknet")
    parser.add_argument("--stability", action="store_true", help="Run 600s two-Bot soak and BDS SIGKILL recovery")
    args = parser.parse_args()
    # Preserve the venv executable path; resolve() follows its symlink to base Python.
    python = args.server_python.absolute()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    folder = work / "server"
    logfile = work / "bds.log"
    results = {
        "status": "FAIL",
        "endstone": "0.11.14",
        "bds": "1.26.52",
        "protocol": 2193,
        "transport": args.transport,
        "python": sys.version.split()[0],
        "endpilot_commit": args.endpilot_commit,
        "installed_endpilot_commit": None,
        "checks": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    server = None
    try:
        direct_url = distribution("endpilot").read_text("direct_url.json")
        if direct_url is None:
            raise RuntimeError("EndPilot has no PEP 610 VCS installation metadata")
        vcs = json.loads(direct_url).get("vcs_info", {})
        installed_commit = vcs.get("commit_id")
        results["installed_endpilot_commit"] = installed_commit
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("--endpilot-commit must be a full lowercase 40-character SHA")
        if installed_commit != args.endpilot_commit:
            raise RuntimeError(
                f"Installed EndPilot commit {installed_commit!r} does not match requested {args.endpilot_commit}"
            )
        results["checks"].append("endpilot_installed_commit_verified")
        server = Server(python, folder, logfile)
        server.start()
        server.wait_ready()
        results["checks"].append("official_bds_bootstrap_version_verified")
        server.stop()
        server = None

        configure(folder / "server.properties", args.transport)
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(python, folder, logfile)
        server.start()
        server.wait_ready()
        results["checks"].append("clean_configured_bds_ready")
        if args.transport == "nethernet":
            from urllib.request import urlopen

            with urlopen("http://127.0.0.1:19132/v1/join", timeout=8) as response:
                status = response.status
            assert 200 <= status < 300, f"NetherNet HTTP capability rejected: {status}"
            results["signaling"] = {"endpoint": "http://127.0.0.1:19132/v1/join", "status": status}
            results["checks"].append("nethernet_http_signaling_server_confirmed")
        from scenarios import exercise
        from stability import exercise_stability
        asyncio.run((exercise_stability if args.stability else exercise)(server, results, args.transport))
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
        (work / "result.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(results, ensure_ascii=False), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
