"""Confirm EndPilot furnace input/fuel/output and optional chest storage on real BDS."""
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
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--store", action="store_true")
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
            raise ValueError("expected an exact EndPilot commit")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {},
        ).get("commit_id")
        assert installed == args.endpilot_commit, (installed, args.endpilot_commit)
        result["checks"].append("pinned_sdk_installed")

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

        def until(marker, timeout=45):
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

        def send(text):
            client.stdin.write(text + "\n")
            client.stdin.flush()

        def server_has(marker, start):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if any(marker.lower() in line.lower() for line in server.snapshot(start)):
                    return
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited unexpectedly")
                time.sleep(0.1)
            raise AssertionError(f"BDS missing {marker}: {server.snapshot(start)[-15:]}")

        server.wait_player(True)
        until("spawned EndPilotTest")
        result["checks"].append("player_spawned")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.8)
        filled(server, "fill -4 100 -4 4 104 4 air")
        filled(server, "fill -4 99 -4 4 99 4 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        server.command("gamemode survival EndPilotTest")
        server.command("setblock 1 100 0 stone")
        time.sleep(0.3)

        send("watch")
        until("furnace_watch_ready")
        server.command("setblock 1 100 0 furnace")
        observed = json.loads(until("furnace_block="))
        assert observed["runtime_id"] != 0, observed
        result["checks"].append("client_observed_furnace_block")
        start = server.command("testforblock 1 100 0 furnace")
        server_has("successfully found", start)
        result["checks"].append("bds_confirmed_furnace_block")

        start = server.command("give EndPilotTest raw_iron 1")
        server_has("gave", start)
        start = server.command("give EndPilotTest coal 1")
        server_has("gave", start)
        send("process")
        initial = json.loads(until("furnace_inputs="))
        assert initial["ingredient_count"] == 1 and initial["fuel_count"] == 1, initial
        result["initial"] = initial

        window = json.loads(until("furnace_window="))
        assert window["type"] == "FURNACE" and window["position"] == [1, 100, 0], window
        result["window"] = window
        result["checks"].append("bds_opened_furnace_window")
        contents = json.loads(until("furnace_initial_slots="))
        assert contents["count"] == 3 and contents["nonempty"] == [], contents
        result["checks"].append("bds_sent_three_initial_furnace_slots")

        stage = [json.loads(until("furnace_stage=")) for _ in range(2)]
        assert [x["kind"] for x in stage] == ["ingredient", "fuel"], stage
        assert [x["request_id"] for x in stage] == [-1, -3], stage
        assert all(x["result"] == "SUCCESS" and x["furnace_count"] == 1
                   and x["inventory_count"] == 0 for x in stage), stage
        result["staging"] = stage
        result["checks"].append("server_accepted_input_and_fuel_slot_requests")

        output_item = json.loads(until("furnace_output=", timeout=45))
        assert output_item["count"] == 1 and output_item["item_id"] != 0, output_item
        result["output"] = output_item
        result["checks"].append("bds_emitted_smelted_output_in_window")
        taken = json.loads(until("furnace_take="))
        assert taken == {
            "request_id": -5, "result": "SUCCESS",
            "item_id": output_item["item_id"], "inventory_count": 1,
            "furnace_count": 0,
        }, taken
        result["take"] = taken
        result["checks"].append("accepted_output_take_and_inventory_reconciliation")

        # Independent server oracle: the new iron ingot exists on the player.
        server.command(
            "execute as @a[hasitem={item=iron_ingot,quantity=1..}] "
            "run setblock 2 100 0 gold_block"
        )
        start = server.command("testforblock 2 100 0 gold_block")
        server_has("successfully found", start)
        result["checks"].append("independent_bds_iron_ingot_inventory_oracle")

        closed = json.loads(until("furnace_closed="))
        assert closed == {"id": window["id"], "open_window": False}, closed
        result["checks"].append("matching_window_close")

        if args.store:
            until("furnace_storage_watch_ready")
            server.command("setblock 1 100 0 chest")
            start = server.command("testforblock 1 100 0 chest")
            server_has("successfully found", start)
            result["checks"].append("bds_replaced_furnace_with_chest")
            stored = json.loads(until("furnace_storage="))
            result["stored"] = stored
            assert stored["request_id"] == -7 and stored["result"] == "SUCCESS", stored
            assert stored["response_chest_id"] == output_item["item_id"], stored
            assert stored["response_chest_count"] == 1, stored
            assert stored["reopened_chest_id"] == output_item["item_id"], stored
            assert stored["reopened_chest_count"] == 1, stored
            assert stored["inventory_count"] == 0 and not stored["open_window"], stored
            result["checks"].append("server_accepted_ingot_storage_and_reconciled_slots")
            result["checks"].append("fresh_bds_chest_content_after_reopen")

            # Independent player-inventory oracle: storage removed the ingot.
            server.command(
                "execute as @a[hasitem={item=iron_ingot,quantity=1..}] "
                "run setblock 3 100 0 diamond_block"
            )
            start = server.command("testforblock 3 100 0 air")
            server_has("successfully found", start)
            result["checks"].append("bds_confirmed_no_remaining_player_ingot")

        send("disconnect")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0
        server.wait_player(False)
        result["checks"].append("empty_player_list")
        server.stop()
        server = None
        result["checks"].append("clean_bds_stop")
        result["status"] = "PASS"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.wait(timeout=10)
        if server is not None:
            try:
                server.stop()
            except Exception as exc:
                result["stop_error"] = repr(exc)
                result["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print("FURNACE_RESULT=" + json.dumps(result), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
