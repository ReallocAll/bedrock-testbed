"""Verify optional identity pins via real BDS joins and authoritative server player lists."""

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from nethernet import PlayerSigner, connect_http

from server import ROOT, Server, configure


async def observed_server_key(url):
    """Use the BDS assertion as a test fixture; this is not an independent trust anchor."""
    identities = []

    async def capture(identity):
        identities.append(identity.key_digest)

    key = ec.generate_private_key(ec.SECP384R1())
    connection = await connect_http(
        url, identity_signer=PlayerSigner(key, domain="self"), on_server_identity=capture
    )
    await connection.close()
    if len(identities) != 1:
        raise AssertionError("BDS did not provide exactly one verified server identity")
    return identities[0]


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
    folder = work / "server"
    result = {
        "status": "FAIL",
        "bds": "1.26.52",
        "endstone": "0.11.14",
        "transport": "nethernet",
        "endpilot_commit": args.endpilot_commit,
        "checks": [],
    }
    server = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("endpilot_commit must be a full SHA")
        metadata = distribution("endpilot").read_text("direct_url.json")
        actual = json.loads(metadata or "{}").get("vcs_info", {}).get("commit_id")
        if actual != args.endpilot_commit:
            raise AssertionError(f"installed EndPilot commit differs: {actual!r}")
        result["checks"].append("endpilot_commit_verified")

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        configure(folder / "server.properties", "nethernet")
        shutil.rmtree(folder / "worlds", ignore_errors=True)

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        result["checks"].append("real_bds_ready")

        url = "http://127.0.0.1:19132"
        pin = asyncio.run(observed_server_key(url))
        result["observed_key_sha256"] = pin
        result["checks"].append("upstream_verified_bds_operator_assertion")

        for label, candidate in (("unpinned", None), ("matching_pin", pin), ("wrong_pin", "0" * 64)):
            command = [sys.executable, str(args.probe.absolute()), url, "--name", "EndPilotTest", "--hold", "2"]
            if candidate is not None:
                command += ["--server-key-sha256", candidate]
            start = len(server.snapshot())
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if label != "wrong_pin":
                    server.wait_player(True, timeout=25)
                stdout, stderr = process.communicate(timeout=30)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            print(f"[{label}] exit={process.returncode} stdout={stdout.strip()} stderr={stderr.strip()}", flush=True)
            if label == "wrong_pin":
                if process.returncode == 0 or "InvalidIdentity" not in stderr:
                    raise AssertionError(f"wrong pin did not fail as InvalidIdentity: {stdout} {stderr}")
                if any("Player Spawned: EndPilotTest" in line for line in server.snapshot(start)):
                    raise AssertionError("BDS spawned a player despite wrong identity pin")
                server.wait_player(False)
                result["checks"].append("wrong_pin_rejected_before_bds_spawn")
            else:
                if process.returncode != 0 or "spawned EndPilotTest" not in stdout or "disconnected EndPilotTest" not in stdout:
                    raise AssertionError(f"{label} did not join and leave: {stdout} {stderr}")
                server.wait_player(False)
                result["checks"].append(label + "_bds_join_spawn_leave")

        server.stop()
        server = None
        result["checks"].append("clean_bds_shutdown")
        result["status"] = "PASS"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(result["error"], file=sys.stderr, flush=True)
    finally:
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
