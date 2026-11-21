"""Compare public SDK forward, backward, lateral and sprinting input against real BDS."""

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
    args = parser.parse_args()

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
        filled(server, "fill -8 100 -10 8 104 10 air")
        filled(server, "fill -8 99 -10 8 99 10 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)

        ground = server_position(server)
        time.sleep(0.25)
        stable = server_position(server)
        assert abs(stable["y"] - ground["y"]) < 0.12, (ground, stable)
        result["checks"].append("stable_server_ground_before_comparison")

        send("walk")
        walk_ticks = int(until("walk_ticks="))
        assert walk_ticks == 18, walk_ticks
        time.sleep(0.3)
        walked = server_position(server)
        assert abs(walked["y"] - stable["y"]) < 0.2, (stable, walked)
        result["checks"].append("normal_walk_remains_grounded")
        walk_distance = walked["z"] - stable["z"]
        assert walk_distance > 0.5, (stable, walked)
        result["checks"].append("independent_bds_normal_walk")

        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        backward_start = server_position(server)
        assert abs(backward_start["z"] - stable["z"]) < 0.2, (stable, backward_start)
        send("backward")
        backward_ticks = int(until("backward_ticks="))
        assert backward_ticks == 18
        time.sleep(0.3)
        backward_end = server_position(server)
        assert abs(backward_end["y"] - backward_start["y"]) < 0.2, (
            backward_start, backward_end)
        assert backward_end["z"] < backward_start["z"] - 0.5, (
            backward_start, backward_end)
        assert abs(backward_end["x"] - backward_start["x"]) < 0.35, (
            backward_start, backward_end)
        result["checks"].append("independent_bds_backward_displacement")

        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        left_start = server_position(server)
        send("left")
        left_ticks = int(until("left_ticks="))
        assert left_ticks == 18
        time.sleep(0.3)
        left_end = server_position(server)
        assert left_end["x"] > left_start["x"] + 0.5, (left_start, left_end)
        assert abs(left_end["z"] - left_start["z"]) < 0.35, (left_start, left_end)
        assert abs(left_end["y"] - left_start["y"]) < 0.2, (left_start, left_end)
        result["checks"].append("independent_bds_left_strafe")

        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        right_start = server_position(server)
        send("right")
        right_ticks = int(until("right_ticks="))
        assert right_ticks == 18
        time.sleep(0.3)
        right_end = server_position(server)
        assert right_end["x"] < right_start["x"] - 0.5, (right_start, right_end)
        assert abs(right_end["z"] - right_start["z"]) < 0.35, (right_start, right_end)
        assert abs(right_end["y"] - right_start["y"]) < 0.2, (right_start, right_end)
        result["checks"].append("independent_bds_right_strafe")

        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        sprint_start = server_position(server)
        assert abs(sprint_start["z"] - stable["z"]) < 0.2, (stable, sprint_start)
        send("sprint")
        key_tick = int(until("sprint_key_tick="))
        sprint_ticks = int(until("sprint_ticks="))
        release_tick = int(until("sprint_release_tick="))
        assert key_tick > 0 and sprint_ticks == 18 and release_tick > key_tick
        time.sleep(0.3)
        sprinted = server_position(server)
        assert abs(sprinted["y"] - sprint_start["y"]) < 0.2, (sprint_start, sprinted)
        result["checks"].append("sprint_remains_grounded")
        sprint_distance = sprinted["z"] - sprint_start["z"]
        result["movement"] = {
            "walk_ticks": walk_ticks, "backward_ticks": backward_ticks,
            "left_ticks": left_ticks, "right_ticks": right_ticks,
            "left_start": left_start, "left_end": left_end,
            "right_start": right_start, "right_end": right_end,
            "backward_start": backward_start, "backward_end": backward_end,
            "backward_displacement_z": backward_end["z"] - backward_start["z"],
            "sprint_ticks": sprint_ticks,
            "key_tick": key_tick, "release_tick": release_tick,
            "walk_start": stable, "walk_end": walked, "sprint_start": sprint_start,
            "sprint_end": sprinted, "walk_distance": walk_distance,
            "sprint_distance": sprint_distance,
        }
        assert sprint_distance > walk_distance + 0.18, result["movement"]
        result["checks"].append("independent_bds_sprint_exceeds_walk_at_equal_ticks")

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
        print("SPRINT_RESULT=" + json.dumps(result), flush=True)
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
