"""Verify live target tracking against independent BDS player position/yaw."""
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
from nethernet_settings_probe import server_target
from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--close-active", action="store_true")
    parser.add_argument("--kick-active", action="store_true")
    parser.add_argument("--switch", action="store_true")
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    outcome = {
        "status": "FAIL", "bds": "1.26.52", "endstone": "0.11.14",
        "protocol": 2193, "transport": args.transport,
        "endpilot_commit": args.endpilot_commit,
        "timestamp": datetime.now(timezone.utc).isoformat(), "checks": [],
    }
    server = None
    client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("expected full EndPilot commit SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {}
        ).get("commit_id")
        if installed != args.endpilot_commit:
            raise AssertionError((installed, args.endpilot_commit))
        outcome["checks"].append("exact_sdk_install_confirmed")

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
            [sys.executable, str(args.probe.absolute()), url] +
            (["--switch"] if args.switch else ["--stop"] if args.stop else
             ["--close-active"] if args.close_active else ["--kick-active"] if args.kick_active else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
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
                    line = output.get(timeout=min(0.5, deadline - time.monotonic()))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError(f"Client exited before {marker}")
                    continue
                if marker in line:
                    return line.partition(marker)[2].strip()
            raise TimeoutError(f"Missing client marker {marker}")

        def send(value):
            client.stdin.write(value + "\n")
            client.stdin.flush()

        server.wait_player(True)
        until("spawned EndPilotTest")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(.7)
        filled(server, "fill -8 100 -8 8 105 8 air")
        filled(server, "fill -8 99 -8 8 99 8 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(.6)
        origin = server_target(server)
        if abs(origin["position"]["x"] - .5) > .2:
            raise AssertionError(f"Unexpected BDS origin: {origin}")
        outcome["origin"] = origin
        outcome["checks"].append("flat_world_and_bds_origin_confirmed")

        send("start")
        until("follow_spawn_watcher_ready")
        server.command("summon cow -6 100 0")
        spawn = json.loads(until("follow_spawn="))
        if int(spawn["runtime_id"]) <= 0 or spawn["position"][0] >= -4:
            raise AssertionError(f"Unexpected BDS cow spawn: {spawn}")
        outcome["spawn"] = spawn
        send("go")
        until("follow_started")
        time.sleep(.85)
        west = server_target(server)
        outcome["west"] = west
        if west["position"]["x"] >= -.05 or abs(float(west["yRot"]) - 90) > 8:
            raise AssertionError(f"Server did not observe westward tracking: {west}")
        outcome["checks"].append("server_observed_initial_westward_follow")

        if args.close_active:
            send("close")
            payload = json.loads(until("follow_closed=", timeout=12))
            outcome["input"] = payload
            if not (5 <= payload["frames"] < 160):
                raise AssertionError(f"Closing did not interrupt active follow: {payload}")
            outcome["checks"].append("active_follow_interrupted_by_bot_close")
        elif args.kick_active:
            until("follow_kick_watcher_ready")
            send("kick")
            server.command("kick EndPilotTest EndPilotKickFollow")
            payload = json.loads(until("follow_kicked=", timeout=15))
            outcome["kick"] = payload
            if payload["packet"] != "DisconnectPacket" or payload["action_error"] != "ConnectionError":
                raise AssertionError(f"Follow did not propagate the typed server kick: {payload}")
            outcome["checks"].append("server_kick_interrupts_active_follow")
        elif args.switch:
            until("follow_switch_watcher_ready")
            server.command("summon cow 6 100 0")
            switched = json.loads(until("follow_switched=", timeout=12))
            if (int(switched["runtime_id"]) == int(spawn["runtime_id"])
                    or switched["position"][0] < 4):
                raise AssertionError(f"Not a distinct eastward BDS cow: {switched}")
            outcome["switched"] = switched
            time.sleep(.85)
            east = server_target(server)
            outcome["east"] = east
            if east["position"]["x"] <= west["position"]["x"] + 1.0:
                raise AssertionError(f"BDS saw no displacement after target switch: {west} -> {east}")
            if abs(float(east["yRot"]) + 90) > 8:
                raise AssertionError(f"BDS did not turn toward second cow: {east}")
            outcome["checks"].append("server_observed_live_target_switch")
            send("stop")
            payload = json.loads(until("follow_switched_stopped=", timeout=12))
            outcome["input"] = payload
            if not (5 <= payload["frames"] < 160):
                raise AssertionError(f"Switch/stop did not end active follow: {payload}")
            time.sleep(.4)
            settled = server_target(server)
            time.sleep(.7)
            rested = server_target(server)
            outcome["settled"] = settled
            outcome["rested"] = rested
            delta = abs(rested["position"]["x"] - settled["position"]["x"])
            if delta > .35:
                raise AssertionError(f"BDS still moving after stop: {settled} -> {rested}")
            outcome["checks"].append("server_observed_stop_after_switch")
        elif args.stop:
            send("stop")
            payload = json.loads(until("follow_stopped=", timeout=12))
            outcome["input"] = payload
            if not (5 <= payload["frames"] < 160):
                raise AssertionError(f"Stop did not interrupt active follow: {payload}")
            # Sample after queued server input has settled; compare two
            # independent BDS querytarget readings while the cow remains west.
            time.sleep(.4)
            settled = server_target(server)
            time.sleep(.7)
            rested = server_target(server)
            outcome["settled"] = settled
            outcome["rested"] = rested
            delta = abs(rested["position"]["x"] - settled["position"]["x"])
            outcome["stopped_displacement_x"] = delta
            if delta > .35:
                raise AssertionError(f"BDS still moving after explicit stop: {settled} -> {rested}")
            outcome["checks"].append("server_observed_stop_while_target_still_present")
        else:
            command = server.command("tp @e[type=cow] 6 100 0")
            payload = json.loads(until("follow_input_submitted=", timeout=20))
            lines = server.snapshot(command)
            if not any("teleported" in line.lower() for line in lines):
                raise AssertionError(f"BDS did not confirm target teleport: {lines[-8:]}")
            east = server_target(server)
            outcome["east"] = east
            outcome["input"] = payload
            if east["position"]["x"] <= west["position"]["x"] + 1.0:
                raise AssertionError(f"Server saw no eastward chase: {west}, {east}")
            if abs(float(east["yRot"]) + 90) > 8:
                raise AssertionError(f"Server did not observe turn toward teleported target: {east}")
            if payload["frames"] < 20:
                raise AssertionError(f"Too few moving input frames: {payload}")
            outcome["checks"].append("bds_target_teleport_then_server_observed_eastward_chase")

        if not (args.close_active or args.kick_active):
            send("close")
        until("disconnected EndPilotTest")
        if client.wait(timeout=10) != 0:
            raise AssertionError(f"Client exited with {client.returncode}")
        server.wait_player(False)
        outcome["checks"].append("bds_player_list_empty_after_disconnect")
        server.stop()
        server = None
        outcome["checks"].append("bds_clean_shutdown")
        outcome["status"] = "PASS"
    except BaseException as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        print(outcome["error"], file=sys.stderr, flush=True)
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.communicate()
        if server is not None:
            try:
                server.stop()
            except BaseException as exc:
                outcome["cleanup_error"] = repr(exc)
                outcome["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")
        print(json.dumps(outcome), flush=True)
    return 0 if outcome["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
