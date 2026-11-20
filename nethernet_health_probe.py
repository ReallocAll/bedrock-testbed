"""Check live health updates against a fresh BDS player on both transports."""
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
from nethernet_settings_probe import server_position
from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--respawn", action="store_true")
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
        # Keep the survival player stable and on solid ground before damage.
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.7)
        filled(server, "fill -8 99 -8 8 99 8 grass_block")
        if args.respawn:
            filled(server, "fill -8 100 -8 8 105 8 air")
        server.command("tp EndPilotTest 0.5 100 0.5")
        server.command("gamemode survival EndPilotTest")
        time.sleep(1)

        if args.respawn:
            send("watch")
            until("death_watcher_ready")
            start = server.command("kill EndPilotTest")
            dead = float(until("death_health=", timeout=20))
            if dead > 0:
                raise AssertionError(f"Player did not report death: health={dead}")
            if not any("kill" in line.lower() for line in server.snapshot(start)):
                raise AssertionError(f"No BDS kill response: {server.snapshot(start)}")
            result["death_health"] = dead
            result["checks"].append("bds_kill_and_client_death_health_confirmed")

            send("respawn")
            spawn = json.loads(until("respawn_ready=", timeout=20))
            health = float(until("respawn_health=", timeout=20))
            if health <= 0:
                raise AssertionError(f"Respawn did not restore health: {health}")
            result["respawn"] = {"server_position": spawn, "health": health}
            result["checks"].append("client_received_server_respawn_ready_and_health")

            # Independent BDS oracle: move on a prepared lane after respawning.
            server.command("tp EndPilotTest 0.5 100 0.5")
            time.sleep(0.8)
            before = server_position(server)
            send("go")
            frames = int(until("walk_frames=", timeout=25))
            time.sleep(0.6)
            after = server_position(server)
            result["post_respawn_walk"] = {"frames": frames, "before": before, "after": after}
            if frames != 24 or after["z"] - before["z"] < 0.65:
                raise AssertionError(f"BDS did not accept post-respawn movement: {result['post_respawn_walk']}")
            result["checks"].append("bds_querytarget_confirms_post_respawn_walking")
        else:
            send("watch")
            until("health_watcher_ready")
            result["checks"].append("client_health_watcher_armed")
            start = server.command("damage EndPilotTest 4")
            observed = float(until("health_update="))
            if observed != 16.0:
                raise AssertionError(f"Unexpected server-reported post-damage health: {observed}")
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if any("damag" in line.lower() for line in server.snapshot(start)):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"BDS did not confirm damage command: {server.snapshot(start)}")
            result["health_after_damage"] = observed
            result["checks"].append("bds_damage_and_client_health_update_confirmed")

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
