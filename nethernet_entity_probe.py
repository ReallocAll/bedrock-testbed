"""Observe actor spawn and removal from fresh BDS through the public EndPilot SDK."""
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
    parser.add_argument("--attack", action="store_true", help="Kill the observed cow only with player attacks")
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    result = {
        "status": "FAIL", "bds": "1.26.52", "endstone": "0.11.14",
        "protocol": 2193, "transport": args.transport,
        "endpilot_commit": args.endpilot_commit,
        "timestamp": datetime.now(timezone.utc).isoformat(), "checks": [],
    }
    server = None
    client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("expected exact EndPilot commit SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {}
        ).get("commit_id")
        if installed != args.endpilot_commit:
            raise AssertionError((installed, args.endpilot_commit))
        result["checks"].append("installed_endpilot_commit_confirmed")

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
        result["checks"].append("bds_player_spawned")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.8)
        filled(server, "fill -8 100 -8 8 106 8 air")
        filled(server, "fill -8 99 -8 8 99 8 grass_block")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.5)

        send("watch")
        until("entity_spawn_watcher_ready")
        result["checks"].append("spawn_watch_armed_before_summon")
        start = server.command("summon cow 2 100 0")
        spawn = json.loads(until("entity_spawn="))
        if spawn["actor_type"] != "minecraft:cow" or int(spawn["runtime_id"]) <= 0:
            raise AssertionError(f"Unexpected server actor: {spawn}")
        if abs(float(spawn["position"][0]) - 2.5) > 3:
            raise AssertionError(f"Actor not spawned near target: {spawn}")
        if not any("successfully summoned" in line.lower()
                   for line in server.snapshot(start)):
            raise AssertionError(f"BDS did not confirm summoning: {server.snapshot(start)}")
        result["entity_spawn"] = spawn
        result["checks"].append("bds_summon_and_client_typed_spawn_confirmed")

        if args.attack:
            # The cow must remain within normal survival reach while attacking.
            server.command("gamemode survival EndPilotTest")
            server.command("effect @e[type=cow] slowness 60 255 true")
            time.sleep(0.5)
            result["checks"].append("survival_actor_within_reach")
        else:
            # The SDK observer must be armed before BDS moves this exact actor.
            send("6")
            until("entity_movement_watcher_ready")
            start = server.command("tp @e[type=cow,x=2,y=100,z=0,r=8] 6 100 0")
            moved = json.loads(until("entity_movement="))
            if int(moved["runtime_id"]) != int(spawn["runtime_id"]):
                raise AssertionError(f"Moved wrong actor: {moved}, expected {spawn}")
            if moved["x"] is None or abs(float(moved["x"]) - 6.0) >= 1.0:
                raise AssertionError(f"Client did not observe BDS target coordinate: {moved}")
            if float(moved["x"]) - float(spawn["position"][0]) < 2:
                raise AssertionError(f"Actor did not move from its initial position: {spawn}, {moved}")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                lines = server.snapshot(start)
                if any("teleported" in line.lower() for line in lines):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"BDS did not confirm entity teleport: {server.snapshot(start)}")
            result["entity_movement"] = moved
            result["checks"].append("bds_teleport_and_matching_runtime_id_move_confirmed")
        
        send("attack" if args.attack else "watch_remove")
        until("entity_removal_watcher_ready")
        if args.attack:
            attacks = int(until("attacks_submitted=", timeout=40))
            assert attacks > 0, "Client did not submit any attacks"
            removed = json.loads(until("entity_removed="))
            assert removed["entity_id"] == spawn["entity_id"], removed
            # The server must independently report the attacked cow is gone;
            # no server kill/damage command is used in this scene.
            offset = server.command("testfor @e[type=cow]")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                lines = server.snapshot(offset)
                if any("no targets matched" in line.lower() or
                       "no entities found" in line.lower() for line in lines):
                    break
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited during attack verification")
                time.sleep(0.1)
            else:
                raise AssertionError(f"BDS still finds a cow, or missing testfor feedback: {server.snapshot(offset)}")
            result["attacks_submitted"] = attacks
            result["checks"].append("player_attack_caused_client_removal_and_bds_absence")
        else:
            start = server.command("kill @e[type=cow,x=2,y=100,z=0,r=8]")
            removed = json.loads(until("entity_removed="))
            if removed["entity_id"] != spawn["entity_id"]:
                raise AssertionError(f"Removed wrong server entity: {removed}, expected {spawn}")
            if not any("killed" in line.lower() for line in server.snapshot(start)):
                raise AssertionError(f"BDS did not confirm kill: {server.snapshot(start)}")
            result["checks"].append("bds_kill_and_matching_unique_id_removal_confirmed")
        send("close")
        until("disconnected EndPilotTest")
        if client.wait(timeout=10) != 0:
            raise AssertionError(f"Client exited with {client.returncode}")
        server.wait_player(False)
        result["checks"].append("bds_player_list_empty_after_disconnect")
        server.stop()
        server = None
        result["checks"].append("bds_clean_stop")
        result["status"] = "PASS"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        print(result["error"], file=sys.stderr, flush=True)
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.communicate()
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                result["cleanup_error"] = repr(error)
                result["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
