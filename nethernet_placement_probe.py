"""Exercise EndPilot player placement against a real Endstone cancel/allow listener."""
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
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    result = {
        "status": "FAIL", "transport": args.transport, "bds": "1.26.52",
        "endstone": "0.11.14", "protocol": 2193,
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
        result["checks"].append("installed_client_commit_confirmed")

        folder = work / "server"
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        configure(folder / "server.properties", args.transport)
        shutil.rmtree(folder / "worlds", ignore_errors=True)

        # Build the actual Endstone plugin package and load it in this fresh BDS only.
        wheels = work / "wheels"
        wheels.mkdir()
        subprocess.run(
            [str(args.server_python.absolute()), "-m", "pip", "wheel",
             "--no-deps", "--wheel-dir", str(wheels), str(ROOT / "placement_gate")],
            check=True,
        )
        plugins = folder / "plugins"
        plugins.mkdir(exist_ok=True)
        shutil.copy2(next(wheels.glob("endstone_placement_gate-*.whl")), plugins)

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()

        def server_has(marker, start=0, timeout=10):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                lines = server.snapshot(start)
                if any(marker.lower() in line.lower() for line in lines):
                    return [line for line in lines if marker.lower() in line.lower()]
                if server.process.poll() is not None:
                    raise RuntimeError(f"BDS exited waiting for {marker}")
                time.sleep(0.1)
            raise TimeoutError(f"No server marker {marker}: {server.snapshot(start)[-12:]}")

        server_has("placement_gate_enabled")
        result["checks"].append("endstone_plugin_loaded")

        url = ("http://127.0.0.1:19132" if args.transport == "nethernet"
               else "raknet://127.0.0.1:19132")
        client = subprocess.Popen(
            [sys.executable, str(args.probe.absolute()), url, "--name", "EndPilotTest"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
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

        def input_line(text):
            assert client.stdin is not None
            client.stdin.write(text + "\n")
            client.stdin.flush()

        server.wait_player(True)
        until("spawned EndPilotTest")
        result["checks"].append("player_spawn_confirmed")

        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.8)
        filled(server, "fill -8 100 -8 8 105 8 air")
        filled(server, "fill -8 99 -8 8 99 8 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(1.0)

        input_line("watch")
        until("support_watchers_ready")
        server.command("setblock 2 99 0 gold_block")
        server.command("setblock 3 99 0 gold_block")
        supports = json.loads(until("supports_observed=").partition("supports_observed=")[2])
        assert len(supports) == 2 and all(int(i) > 0 for i in supports), supports
        result["support_runtime_ids"] = supports
        result["checks"].append("client_observed_actual_support_blocks")

        # Discover the target's air runtime ID on the client before either placement.
        for x in (2, 3):
            server.command(f"setblock {x} 100 0 stone")
        time.sleep(0.5)
        input_line("baseline")
        until("target_watchers_ready")
        for x in (2, 3):
            server.command(f"setblock {x} 100 0 air")
        targets = json.loads(until("targets_observed=").partition("targets_observed=")[2])
        assert len(targets) == 2 and all(
            int(targets[i]) != int(supports[i]) for i in range(2)
        ), targets
        result["target_air_runtime_ids"] = targets
        result["checks"].append("client_observed_initial_air_targets")

        server.command("replaceitem entity EndPilotTest slot.hotbar 0 gold_block 8")
        time.sleep(0.4)
        for x, cancelled, expected in ((2, 1, "air"), (3, 0, "gold_block")):
            offset = len(server.snapshot())
            input_line("place")
            line = until("placement_submitted=")
            submitted = json.loads(line.partition("placement_submitted=")[2])
            assert submitted["x"] == x and submitted["tick"] > 0, submitted
            server_has(f"placement_gate_event={x - 1} cancelled={cancelled}", offset)
            offset = server.command(f"testforblock {x} 100 0 {expected}")
            server_has("successfully found", offset)
            client_line = until("placement_update=")
            update = json.loads(client_line.partition("placement_update=")[2])
            assert update["x"] == x and update["layer"] == 0, update
            if cancelled:
                assert update["runtime_id"] == targets[0] and update["effect"] == "unchanged", update
                result["cancelled_update"] = update
            else:
                assert update["runtime_id"] == supports[1] and update["effect"] == "placed", update
                result["allowed_update"] = update
            result["checks"].append(
                "endstone_cancel_and_bds_air" if cancelled else "endstone_allow_bds_gold_and_client_update"
            )

        until("placement_checks_ready")
        input_line("close")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0
        server.wait_player(False)
        result["checks"].append("client_left_and_bds_list_empty")
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
