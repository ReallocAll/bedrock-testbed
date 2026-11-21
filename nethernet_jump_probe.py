"""Validate the public one-tick jump against independent BDS coordinates."""

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
import traceback
from importlib.metadata import distribution
from pathlib import Path

from nethernet_settings_probe import server_position
from nethernet_walk_probe import filled
from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument("--forward", action="store_true", help="Exercise a forward-moving jump")
    direction.add_argument("--backward", action="store_true", help="Exercise a backward-moving jump")
    args = parser.parse_args()
    moving_jump = args.forward or args.backward
    direction = "backward" if args.backward else "forward"

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    result = {
        "status": "FAIL", "transport": args.transport,
        "bds": "1.26.52", "endstone": "0.11.14", "protocol": 2193,
        "endpilot_commit": args.endpilot_commit, "checks": [],
    }
    server = client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("EndPilot commit must be an exact SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {}).get("commit_id")
        assert installed == args.endpilot_commit, (installed, args.endpilot_commit)
        result["checks"].append("pinned_installed_sdk")

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

        url = ("http://127.0.0.1:19132" if args.transport == "nethernet"
               else "raknet://127.0.0.1:19132")
        client = subprocess.Popen(
            [sys.executable, str(args.probe.absolute()), url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        output = queue.Queue()

        def forward():
            for line in client.stdout:
                print("[client] " + line.rstrip(), flush=True)
                output.put(line)
        threading.Thread(target=forward, daemon=True).start()

        def until(marker, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError("client exited before " + marker)
                    continue
                if marker in line:
                    return line.partition(marker)[2].strip()
            raise TimeoutError("missing client marker " + marker)

        def send(command):
            assert client.stdin is not None
            client.stdin.write(command + "\n")
            client.stdin.flush()

        server.wait_player(True)
        until("spawned EndPilotTest")
        result["checks"].append("server_confirmed_player_join")

        # Prepare a loaded, level platform before observing vertical motion.
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        filled(server, "fill -3 100 -3 3 104 3 air")
        filled(server, "fill -3 99 -3 3 99 3 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)

        ground = server_position(server)
        time.sleep(0.25)
        stable = server_position(server)
        assert abs(stable["y"] - ground["y"]) < 0.12, (ground, stable)
        result["checks"].append("stable_server_ground_before_jump")

        send("jump")
        if moving_jump:
            until(f"jump_{direction}_started")
        else:
            tick = int(until("jump_tick="))
            assert tick > 0
            result["jump_tick"] = tick
        samples = []
        deadline = time.monotonic() + 1.1
        while time.monotonic() < deadline:
            samples.append(server_position(server))
            time.sleep(0.04)
        if moving_jump:
            ticks = int(until(f"jump_{direction}_ticks="))
            assert ticks == 18, ticks
            result[f"jump_{direction}_ticks"] = ticks
        until("jump_finished")
        rise = max((p["y"] for p in samples), default=stable["y"]) - stable["y"]
        landed = server_position(server)
        result["jump"] = {
            "ground": stable, "sampled_positions": samples,
            "rise": rise, "landed": landed,
        }
        assert rise > 0.25, result["jump"]
        result["checks"].append("independent_bds_querytarget_jump_rise")
        assert abs(landed["y"] - stable["y"]) < 0.2, result["jump"]
        result["checks"].append("independent_bds_querytarget_landing")
        if moving_jump:
            displacement_z = landed["z"] - stable["z"]
            assert displacement_z < -0.5 if args.backward else displacement_z > 0.5, result["jump"]
            assert abs(landed["x"] - stable["x"]) < 0.35, result["jump"]
            result["jump"]["displacement_z"] = displacement_z
            result["checks"].append(f"independent_bds_{direction}_jump_displacement")

        send("close")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0
        server.wait_player(False)
        result["checks"].append("no_remaining_players")
        server.stop()
        server = None
        result["checks"].append("clean_bds_stop")
        result["status"] = "PASS"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc()
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.wait(timeout=10)
        if server is not None:
            try:
                server.stop()
            except Exception as error:
                result["status"] = "FAIL"
                result["stop_error"] = repr(error)
                traceback.print_exc()
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print("JUMP_RESULT=" + json.dumps(result), flush=True)
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
