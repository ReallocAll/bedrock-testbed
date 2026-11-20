"""Observe a fresh server block update through EndPilot on an isolated BDS."""
from __future__ import annotations

import argparse
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from nethernet_walk_probe import filled
from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--subchunk", action="store_true")
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    results = {
        "status": "FAIL", "transport": args.transport,
        "bds": "1.26.52", "endstone": "0.11.14", "protocol": 2193,
        "endpilot_commit": args.endpilot_commit,
        "timestamp": datetime.now(timezone.utc).isoformat(), "checks": [],
    }
    server = None
    client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("--endpilot-commit requires an exact SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {}
        ).get("commit_id")
        assert installed == args.endpilot_commit, (installed, args.endpilot_commit)
        results["checks"].append("installed_client_commit_confirmed")

        folder = work / "server"
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        configure(folder / "server.properties", args.transport)
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        results["checks"].append("fresh_bds_started")

        url = ("http://127.0.0.1:19132" if args.transport == "nethernet"
               else "raknet://127.0.0.1:19132")
        client = subprocess.Popen(
            [sys.executable, str(args.probe.absolute()), url, "--name", "EndPilotTest"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        output = queue.Queue()

        def forward():
            for line in client.stdout:
                output.put(line)

        threading.Thread(target=forward, daemon=True).start()

        def until(marker, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=min(1, deadline - time.monotonic()))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError(f"Client exited before {marker!r}")
                    continue
                print("[client] " + line.rstrip(), flush=True)
                if marker in line:
                    return line
            raise TimeoutError(f"Client did not print {marker!r}")

        server.wait_player(True)
        until("spawned EndPilotTest")
        results["checks"].append("bds_spawn_and_player_list_confirmed")
        # The new block must be in the player's subscribed chunk; prepare air first.
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.8)
        filled(server, "fill -8 100 -8 8 105 8 air")
        filled(server, "fill -8 99 -8 8 99 8 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(1)

        if args.subchunk:
            server.command("setblock 2 100 0 gold_block")
            for pos, name in (((2, 100, 0), "gold_block"),
                              ((3, 100, 0), "air"),
                              ((2, 99, 0), "stone")):
                offset = server.command(f"testforblock {pos[0]} {pos[1]} {pos[2]} {name}")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    evidence = server.snapshot(offset)
                    if any("successfully found" in line.lower() for line in evidence):
                        break
                    if any("failed" in line.lower() or "not found" in line.lower() for line in evidence):
                        raise AssertionError(f"BDS block oracle rejected {pos}={name}: {evidence}")
                    time.sleep(.1)
                else:
                    raise TimeoutError(f"No BDS block oracle for {pos}={name}: {server.snapshot(offset)}")
            results["checks"].append("bds_gold_air_stone_testforblock_confirmed")

        client.stdin.write("go\n")
        client.stdin.flush()
        if args.subchunk:
            until("sub_chunk_request_submitted")
            line = until("block_snapshots=", timeout=25)
            event = json.loads(line.partition("block_snapshots=")[2])
            samples = event["samples"]
            assert event["unloaded"] is None and event["responses"] > 0, event
            assert [sample["position"] for sample in samples] == [
                [2, 100, 0], [3, 100, 0], [2, 99, 0],
            ], samples
            assert [sample["is_air"] for sample in samples] == [False, True, False], samples
            assert all(sample["runtime_id"] is not None for sample in samples), samples
            assert samples[0]["runtime_id"] != samples[1]["runtime_id"], samples
            assert samples[2]["runtime_id"] != samples[1]["runtime_id"], samples
            results["block_snapshots"] = event
            results["checks"].append("requested_sub_chunk_response_and_known_air_vs_solid")
            results["checks"].append("distant_unloaded_remains_unknown")
        else:
            until("block_watcher_ready")
            results["checks"].append("client_watch_armed_before_change")

            server.command("setblock 2 100 0 gold_block")
            offset = server.command("testforblock 2 100 0 gold_block")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                evidence = server.snapshot(offset)
                if any("successfully found" in line.lower() for line in evidence):
                    break
                if any("failed" in line.lower() or "not found" in line.lower() for line in evidence):
                    raise AssertionError(f"BDS rejected block oracle: {evidence}")
                time.sleep(0.1)
            else:
                raise TimeoutError(f"No positive BDS testforblock result: {server.snapshot(offset)}")
            results["checks"].append("bds_testforblock_gold_block_confirmed")

            line = until("block_update=", timeout=20)
            event = json.loads(line.partition("block_update=")[2])
            assert event["position"] == [2, 100, 0] and event["layer"] == 0, event
            assert int(event["runtime_id"]) > 0, event
            results["block_update"] = event
            results["checks"].append("player_observed_targeted_server_block_update")
        until("disconnected EndPilotTest", timeout=15)
        assert client.wait(timeout=10) == 0
        server.wait_player(False)
        results["checks"].append("client_left_and_bds_list_empty")
        server.stop()
        server = None
        results["checks"].append("bds_clean_stop")
        results["status"] = "PASS"
    except BaseException as error:
        results["error"] = f"{type(error).__name__}: {error}"
        print(results["error"], file=sys.stderr, flush=True)
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.communicate()
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                results["cleanup_error"] = repr(error)
                results["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
