"""Confirm an SDK hand-swing AnimatePacket is received by real BDS."""

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
        "status": "FAIL", "bds": "1.26.52", "endstone": "0.11.14",
        "protocol": 2193, "transport": args.transport,
        "endpilot_commit": args.endpilot_commit, "checks": [],
    }
    server = client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("EndPilot commit must be an exact SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {},
        ).get("commit_id")
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

        wheels = work / "wheels"
        wheels.mkdir()
        subprocess.run([
            str(args.server_python.absolute()), "-m", "pip", "wheel", "--no-deps",
            "--wheel-dir", str(wheels), str(ROOT / "combat_probe"),
        ], check=True)
        plugins = folder / "plugins"
        plugins.mkdir(exist_ok=True)
        shutil.copy2(next(wheels.glob("endstone_combat_probe-*.whl")), plugins)

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()

        def server_has(marker, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                lines = server.snapshot()
                if any(marker in line for line in lines):
                    return
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited while waiting for " + marker)
                time.sleep(0.1)
            raise AssertionError(f"BDS missing {marker}: {server.snapshot()[-20:]}")

        server_has("combat_probe_enabled")
        result["checks"].append("endstone_packet_observer_enabled")

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
        # Join does not imply a post-spawn position sync. Force an authoritative
        # teleport so the SDK input stream starts from server-owned coordinates.
        # A solid floor is required: otherwise this test may compare sneak
        # states while the player is falling into an unprepared world.
        # RakNet may spawn before the target area is loaded. Prime it as in
        # nethernet_entity_probe before running any fill command.
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(3.0)
        filled(server, "fill -3 100 -3 3 104 3 air")
        filled(server, "fill -3 99 -3 3 99 3 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")

        before = sum("swing_animate_received=" in line for line in server.snapshot())
        send("swing")
        until("swing_submitted")
        server_has("swing_animate_received=")
        after = sum("swing_animate_received=" in line for line in server.snapshot())
        assert after == before + 1, f"Expected one new server-received animation, got {before} -> {after}"
        result["received_animations"] = after - before
        result["checks"].append("bds_received_one_sdk_animate_packet")

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
        print("SWING_RESULT=" + json.dumps(result), flush=True)
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
