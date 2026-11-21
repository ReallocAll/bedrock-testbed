"""Confirm one inventory auto-craft through the public SDK and a real BDS inventory oracle."""

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

from nethernet_walk_probe import filled
from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--store", action="store_true", help="craft then store the output in a chest")
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
            raise ValueError("EndPilot commit must be a full SHA")
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
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()

        url = ("http://127.0.0.1:19132" if args.transport == "nethernet"
               else "raknet://127.0.0.1:19132")
        client = subprocess.Popen(
            [sys.executable, str(args.probe.absolute()), url]
            + (["--store"] if args.store else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        output = queue.Queue()

        def forward():
            for line in client.stdout:
                print("[client] " + line.rstrip(), flush=True)
                output.put(line)

        threading.Thread(target=forward, daemon=True).start()

        def until(marker, timeout=30):
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

        def send(line):
            client.stdin.write(line + "\n")
            client.stdin.flush()

        def server_has(marker, offset):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                lines = server.snapshot(offset)
                if any(marker.lower() in line.lower() for line in lines):
                    return
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited unexpectedly")
                time.sleep(0.1)
            raise AssertionError(f"BDS missing {marker}: {server.snapshot(offset)[-15:]}")

        server.wait_player(True)
        until("spawned EndPilotTest")
        result["checks"].append("player_spawned")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(1)
        filled(server, "fill -3 100 -3 3 104 3 air")
        filled(server, "fill -3 99 -3 3 99 3 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        server.command("gamemode survival EndPilotTest")

        chosen = json.loads(until("recipe_selected="))
        assert re.fullmatch(r"minecraft:[a-z0-9_]+", chosen["ingredient"]), chosen
        assert re.fullmatch(r"minecraft:[a-z0-9_]+", chosen["output"]), chosen
        assert chosen["ingredient_count"] > 0 and chosen["output_count"] > 0, chosen
        result["recipe"] = chosen
        result["checks"].append("server_advertised_recipe_and_net_id")
        start = server.command(
            f"give EndPilotTest {chosen['ingredient']} {chosen['ingredient_count']}"
        )
        server_has("gave", start)
        send("craft")
        crafted = json.loads(until("craft_result="))
        result["craft"] = crafted
        assert crafted["result"] == "SUCCESS", crafted
        assert crafted["request_id"] < 0 and crafted["request_id"] % 2 == 1, crafted
        assert crafted["source_before"] >= chosen["ingredient_count"], crafted
        assert crafted["source_after"] == crafted["source_before"] - chosen["ingredient_count"], crafted
        assert crafted["output_id"] == chosen["output_id"], crafted
        assert crafted["output_count"] == chosen["output_count"], crafted
        result["checks"].append("matching_item_stack_response_and_inventory_slots")

        # Independent authority: only a BDS player with the crafted output
        # can place the oracle block through the server's execute command.
        server.command(
            f"execute as @a[hasitem={{item={chosen['output']},"
            f"quantity={chosen['output_count']}..}}] run setblock 2 100 0 gold_block"
        )
        start = server.command("testforblock 2 100 0 gold_block")
        server_has("successfully found", start)
        result["checks"].append("bds_independently_confirmed_crafted_output")

        if args.store:
            until("storage_chest_watch_ready")
            server.command("setblock 1 100 0 chest")
            start = server.command("testforblock 1 100 0 chest")
            server_has("successfully found", start)
            result["checks"].append("bds_chest_placed_after_client_watch")
            stored = json.loads(until("storage_result="))
            result["stored"] = stored
            assert stored["result"] == "SUCCESS", stored
            assert stored["request_id"] < 0 and stored["request_id"] % 2 == 1, stored
            assert stored["request_id"] != crafted["request_id"], stored
            assert stored["response_chest_id"] == chosen["output_id"], stored
            assert stored["response_chest_count"] == chosen["output_count"], stored
            assert stored["reopened_chest_id"] == chosen["output_id"], stored
            assert stored["reopened_chest_count"] == chosen["output_count"], stored
            assert stored["inventory_output_count"] == 0 and not stored["open_window"], stored
            result["checks"].append("response_reconciled_crafted_output_storage")
            result["checks"].append("fresh_bds_chest_content_after_reopen")

            # The crafted stack must no longer be held by the player, as
            # independently checked with BDS's hasitem selector.
            server.command(
                f"execute as @a[hasitem={{item={chosen['output']},quantity=1..}}] "
                "run setblock 3 100 0 diamond_block"
            )
            start = server.command("testforblock 3 100 0 air")
            server_has("successfully found", start)
            result["checks"].append("bds_confirmed_no_remaining_crafted_output")

        send("close")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0
        server.wait_player(False)
        result["checks"].append("zero_players_after_leave")
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
                result["checks"].append("clean_bds_stop")
            except Exception as error:
                result["status"] = "FAIL"
                result["stop_error"] = repr(error)
                traceback.print_exc()
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print("CRAFT_RESULT=" + json.dumps(result), flush=True)
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
