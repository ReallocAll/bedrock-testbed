"""Isolated BDS 1.26.60.6 passive native OnHit plugin acceptance.

Reuse the existing Server lifecycle. Do not touch the regular testbed world or
the user's production server. No experimental hooks are installed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from server import ROOT, Server, configure

EXPECTED_BDS_SHA256 = "ea16b541a448e07c337848d2c5f37321b706554b5216dd942f3f11e7a8ed2bad"
EXPECTED_BDS_BUILD_ID = "52576b75b6af5a09a6bde582be804e29657dbbf3"


def _verify_binary(folder: Path) -> dict:
    binaries = sorted(p for p in folder.rglob("bedrock_server") if p.is_file())
    if len(binaries) != 1:
        raise RuntimeError(f"Expected one unpacked BDS ELF, found {len(binaries)}")
    path = binaries[0]
    digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
    notes = subprocess.run(
        ["readelf", "-n", str(path)], check=True, capture_output=True, text=True
    ).stdout
    build_ids = [line.strip().split("Build ID: ", 1)[1]
                 for line in notes.splitlines() if "Build ID: " in line]
    if digest != EXPECTED_BDS_SHA256 or EXPECTED_BDS_BUILD_ID not in build_ids:
        raise RuntimeError(
            f"BDS identity mismatch: sha256={digest}, build_ids={build_ids}"
        )
    return {"sha256": digest, "build_id": EXPECTED_BDS_BUILD_ID,
            "size_bytes": path.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--onhit-commit", required=True)
    args = parser.parse_args()
    work = ROOT / "work-onhit"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    folder = work / "server"
    plugin = args.plugin.resolve()
    result = {
        "status": "FAIL",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "endstone": "0.11.15", "bds": "1.26.60.6",
        "onhit_commit": args.onhit_commit, "hooks_enabled": False,
        "checks": [],
    }
    running: Server | None = None
    try:
        if not plugin.is_file() or plugin.stat().st_size == 0:
            raise FileNotFoundError(f"Plugin shared object missing: {plugin}")
        result["plugin_sha256"] = hashlib.file_digest(
            plugin.open("rb"), "sha256"
        ).hexdigest()

        def new_server() -> Server:
            return Server(args.server_python.absolute(), folder,
                          work / "bds.log",
                          expected_endstone="0.11.15", expected_bds="26.60")

        # Bootstrap official Endstone/BDS once; preserve the same world after
        # every stop, so it can later be reused for Beta APIs/SimulatedPlayer.
        running = new_server()
        running.start()
        running.wait_ready()
        running.stop()
        running = None
        result["checks"].append("bootstrap_start_and_clean_stop")
        result["binary_identity"] = _verify_binary(folder)
        result["checks"].append("exact_executable_sha256_and_build_id")

        configure(folder / "server.properties", "raknet")
        target = folder / "plugins" / "endstone_onhit_lifecycle.so"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plugin, target)
        running = new_server()
        running.start()
        running.wait_ready()
        lines = "\n".join(running.snapshot())
        for text in (
            "Loading onhit_lifecycle v0.1.0",
            "Enabling onhit_lifecycle v0.1.0",
            "Exact BDS image verified",
            "Diagnostics passive only",
        ):
            if text not in lines:
                raise AssertionError(f"Plugin acceptance log missing: {text}")
        if "EXPERIMENTAL OnHit hooks active" in lines:
            raise AssertionError("Active hook unexpectedly installed")
        result["checks"].append("native_plugin_loaded_with_identity_gate")
        result["checks"].append("no_experimental_detours")
        running.stop()
        running = None
        result["checks"].append("plugin_graceful_stop_no_processes")
        result["status"] = "PASS"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if running is not None:
            try:
                running.stop()
            except BaseException as error:
                result["cleanup_error"] = f"{type(error).__name__}: {error}"
                result["status"] = "FAIL"
        (work / "result.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
