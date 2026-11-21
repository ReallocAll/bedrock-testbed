"""Check MCP stdio walk start, observation, stop and resumed use on BDS."""
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

from nethernet_settings_probe import server_target
from nethernet_walk_probe import filled
from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--handoff", action="store_true")
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    result = {"status": "FAIL", "transport": args.transport, "bds": "1.26.52",
              "endstone": "0.11.14", "protocol": 2193,
              "endpilot_commit": args.endpilot_commit, "checks": []}
    server = client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("expected exact EndPilot commit SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {}).get("commit_id")
        if installed != args.endpilot_commit:
            raise AssertionError((installed, args.endpilot_commit))
        result["checks"].append("installed_exact_sdk")

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
            text=True, bufsize=1)
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
                    line = output.get(timeout=min(.5, max(.01, deadline - time.monotonic())))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError("client exited before " + marker)
                    continue
                if marker in line:
                    return line.partition(marker)[2].strip()
            raise TimeoutError("missing client marker " + marker)

        def send(value):
            client.stdin.write(value + "\n")
            client.stdin.flush()

        server.wait_player(True)
        until("spawned EndPilotTest")
        until("mcp_walk_ready")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(.7)
        filled(server, "fill -8 100 -8 8 105 8 air")
        filled(server, "fill -8 99 -8 8 99 8 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(.6)
        origin = server_target(server)
        result["origin"] = origin
        if abs(origin["position"]["z"] - .5) > .2:
            raise AssertionError("unexpected BDS origin: " + str(origin))
        result["checks"].append("flat_lane_and_server_origin")

        send("go")
        running = json.loads(until("mcp_walk_running="))
        if running["state"] != "running":
            raise AssertionError(running)
        result["running"] = running
        time.sleep(.55)
        moving = server_target(server)
        result["moving"] = moving
        if moving["position"]["z"] - origin["position"]["z"] < .25:
            raise AssertionError("BDS did not observe MCP-controlled walk: " + str(moving))
        result["checks"].append("bds_observed_running_walk")

        send("stop")
        stopped = json.loads(until("mcp_walk_stopped="))
        if stopped["state"] != "stopped" or not 1 <= stopped["submitted_frames"] < 160:
            raise AssertionError(stopped)
        result["stopped"] = stopped
        time.sleep(.4)
        settled = server_target(server)
        time.sleep(.7)
        resting = server_target(server)
        result["settled"] = settled
        result["resting"] = resting
        if abs(resting["position"]["z"] - settled["position"]["z"]) > .35:
            raise AssertionError("BDS player continued walking after stop")
        result["checks"].append("stop_released_input_and_bds_stable")

        send("resume")
        resumed = json.loads(until("mcp_walk_resumed="))
        if resumed["state"] != "finished" or resumed["submitted_frames"] != 12:
            raise AssertionError(resumed)
        time.sleep(.4)
        after = server_target(server)
        result["resumed"] = resumed
        if args.handoff:
            if resumed.get("controller") != "python_sdk":
                raise AssertionError("script did not resume with direct SDK control: " + str(resumed))
            result["checks"].append("same_live_player_handed_to_python_sdk")
        result["after"] = after
        if after["position"]["z"] - resting["position"]["z"] < .3:
            raise AssertionError("BDS did not observe follow-up walk on same connection")
        result["checks"].append("same_connection_resumed_and_bds_moved")

        send("close")
        until("disconnected EndPilotTest")
        if client.wait(timeout=10) != 0:
            raise AssertionError("MCP stdio process failed: " + str(client.returncode))
        server.wait_player(False)
        result["checks"].append("player_left")
        server.stop()
        server = None
        result["checks"].append("bds_clean_stop")
        result["status"] = "PASS"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.communicate()
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
