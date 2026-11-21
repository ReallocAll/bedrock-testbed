"""Verify the public EndPilot API opens a real BDS chest and observes its window."""
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
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--take", action="store_true", help="take half the chest stack")
    parser.add_argument("--chat-during-take", action="store_true",
                        help="submit chat while an item response is pending")
    parser.add_argument("--server-grant-during-take", action="store_true",
                        help="BDS grants another inventory stack after the chest request is sent")
    parser.add_argument("--put", action="store_true", help="take two and place one back")
    parser.add_argument("--equip", action="store_true", help="take a helmet and equip it after closing")
    parser.add_argument("--server-close", action="store_true", help="BDS breaks the open chest")
    parser.add_argument("--drop", action="store_true", help="drop one item after taking two from chest")
    parser.add_argument("--split", action="store_true", help="split one chest-taken stack into two player slots")
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
    server = client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("expected exact EndPilot commit SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {}
        ).get("commit_id")
        assert installed == args.endpilot_commit, (installed, args.endpilot_commit)
        result["checks"].append("installed_sdk_commit")

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
            + (["--take-count", "1" if args.equip else "2", "--destination-slot", "2"]
               if (args.take or args.put or args.equip or args.drop or args.chat_during_take or args.server_grant_during_take) else [])
            + (["--chat-during-take"] if args.chat_during_take else [])
            + (["--server-grant-during-take"] if args.server_grant_during_take else [])
            + (["--put-back-count", "1"] if args.put else [])
            + (["--equip-armor-slot", "0"] if args.equip else [])
            + (["--expect-server-close"] if args.server_close else [])
            + (["--drop-count", "1"] if args.drop else [])
            + (["--split-slot", "3"] if args.split else []),
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
                    line = output.get(timeout=min(0.5, deadline - time.monotonic()))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError(f"Client exited before {marker}")
                    continue
                if marker in line:
                    return line.partition(marker)[2].strip()
            raise TimeoutError(f"Missing client marker {marker}")

        def send(value):
            assert client.stdin is not None
            client.stdin.write(value + "\n")
            client.stdin.flush()

        def server_has(marker, start, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                output_lines = server.snapshot(start)
                if any(marker.lower() in line.lower() for line in output_lines):
                    return
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited waiting for " + marker)
                time.sleep(0.1)
            raise AssertionError(f"BDS missing {marker}: {server.snapshot(start)[-12:]}")

        server.wait_player(True)
        until("spawned EndPilotTest")
        result["checks"].append("player_spawned")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.8)
        filled(server, "fill -4 100 -4 4 103 4 air")
        filled(server, "fill -4 99 -4 4 99 4 stone")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(0.8)
        if args.drop:
            server.command("gamemode survival EndPilotTest")
        server.command("setblock 1 100 0 stone")
        time.sleep(0.3)

        send("watch")
        until("chest_watch_ready")
        server.command("setblock 1 100 0 chest")
        chest_runtime_id = int(until("chest_runtime_id="))
        assert chest_runtime_id != 0, chest_runtime_id
        result["chest_runtime_id"] = chest_runtime_id
        result["checks"].append("client_observed_bds_chest_update")
        start = server.command("testforblock 1 100 0 chest")
        server_has("successfully found", start)
        result["checks"].append("bds_chest_block_confirmed")

        chest_item = "iron_helmet" if args.equip else "dirt"
        chest_amount = 1 if args.equip else 4
        start = server.command(
            f"replaceitem block 1 100 0 slot.container 0 {chest_item} {chest_amount}"
        )
        server_has("replaced", start)
        result["checks"].append("bds_populated_chest_slot_zero")

        send("open")
        opened = json.loads(until("container_opened="))
        assert opened["position"] == [1, 100, 0], opened
        assert isinstance(opened["id"], int) and opened["id"] >= 0, opened
        assert isinstance(opened["type"], str) and opened["type"], opened
        result["server_window"] = opened
        result["checks"].append("server_confirmed_chest_window_open")
        server.wait_player(True)
        contents = json.loads(until("container_slots="))
        assert contents["count"] == 27, contents
        assert len(contents["nonempty"]) == 1, contents
        item = contents["nonempty"][0]
        assert (item["slot"] == 0 and item["item_id"] > 0
                and item["stack_size"] == chest_amount), contents
        result["server_chest_contents"] = contents
        result["checks"].append("server_confirmed_chest_slots")

        if args.server_grant_during_take:
            until("server_grant_after_request_sent")
            start = server.command("replaceitem entity EndPilotTest slot.hotbar 4 dirt 1")
            server_has("replaced", start)
            send("server_grant_done")
            result["checks"].append("bds_granted_independent_hotbar_slot_after_request_sent")

        if args.chat_during_take:
            chat = json.loads(until("chat_during_take="))
            assert chat == {
                "message": "endpilot-container-interleave",
                "item_request_pending_at_chat": True,
            }, chat
            server_has("endpilot-container-interleave", 0)
            result["chat_during_take"] = chat
            result["checks"].append("bds_received_chat_while_item_response_was_pending")

        if args.server_grant_during_take:
            grant = json.loads(until("server_grant_observed="))
            assert grant["slot"] == 4 and grant["stack_size"] == 1, grant
            assert grant["item_id"] == item["item_id"], grant
            assert grant["inventory_count"] == 3, grant
            result["server_grant"] = grant
            result["checks"].append("client_observed_independent_server_inventory_update")

        if (args.take or args.put or args.equip or args.drop
                or args.chat_during_take or args.server_grant_during_take):
            transfer = json.loads(until("container_take_response="))
            assert transfer["request_id"] < 0 and transfer["request_id"] % 2 != 0, transfer
            assert transfer["result"] == "SUCCESS", transfer
            assert transfer["chest_slot_count"] == (0 if args.equip else 2), transfer
            assert transfer["inventory_count"] == (3 if args.server_grant_during_take else
                                                   1 if args.equip else 2), transfer
            result["transfer"] = transfer
            result["checks"].append("server_accepted_chest_stack_split_into_inventory")
            if args.server_grant_during_take:
                for slot, block, quantity in ((2, "diamond_block", 2),
                                              (4, "emerald_block", 1)):
                    server.command(
                        f"execute as @a[hasitem={{item=dirt,quantity={quantity},"
                        f"location=slot.hotbar,slot={slot}}}] "
                        f"run setblock {slot} 100 1 {block}"
                    )
                    start = server.command(f"testforblock {slot} 100 1 {block}")
                    server_has("successfully found", start)
                result["checks"].append("bds_independently_confirmed_both_inventory_slots")
            if args.chat_during_take and not args.server_grant_during_take:
                server.command(
                    "execute as @a[hasitem={item=dirt,quantity=2}] "
                    "run setblock 2 100 0 gold_block"
                )
                start = server.command("testforblock 2 100 0 gold_block")
                server_has("successfully found", start)
                result["checks"].append("bds_independently_confirmed_two_dirt_after_chat")
        if args.put:
            returned = json.loads(until("container_put_response="))
            assert returned["request_id"] < 0 and returned["request_id"] % 2 != 0, returned
            assert returned["request_id"] != transfer["request_id"], returned
            assert returned["result"] == "SUCCESS", returned
            assert returned["chest_slot_count"] == 3, returned
            assert returned["inventory_count"] == 1, returned
            result["return_transfer"] = returned
            result["checks"].append("server_accepted_inventory_to_chest_place")

        if args.server_close:
            until("server_close_watch_ready")
            server.command("setblock 1 100 0 air")
            start = server.command("testforblock 1 100 0 air")
            server_has("successfully found", start)
            closed = json.loads(until("server_container_closed="))
            assert closed == {
                "id": opened["id"], "server_initiated": True, "open_window": False,
            }, closed
            result["checks"].append("bds_server_initiated_chest_close_and_client_state")
        else:
            send("close_window")
            closed = json.loads(until("container_closed="))
            assert closed["id"] == opened["id"], (closed, opened)
            assert closed["open_window"] is False, closed
            result["checks"].append("server_confirmed_chest_window_close")
        server.wait_player(True)

        if args.drop:
            screen = json.loads(until("player_inventory_opened="))
            assert isinstance(screen["id"], int) and screen["id"] >= 0, screen
            result["checks"].append("server_confirmed_player_inventory_open")
            dropped = json.loads(until("inventory_drop_response="))
            assert dropped["request_id"] == -3, dropped
            assert dropped["result"] == "SUCCESS", dropped
            assert dropped["inventory_count"] == 1, dropped
            assert dropped["item_id"] == item["item_id"], dropped
            assert dropped["drop_item_id"] == "minecraft:dirt", dropped
            assert dropped["drop_count"] == 1 and dropped["drop_runtime_id"] > 0, dropped
            result["drop"] = dropped
            result["checks"].append("server_response_and_client_item_actor_confirmed")
            screen_closed = json.loads(until("player_inventory_closed="))
            assert screen_closed["id"] == screen["id"] and screen_closed["open_window"] is False
            result["checks"].append("server_confirmed_player_inventory_close")
            server.command(
                "execute as @a[hasitem={item=dirt,quantity=1}] "
                "run setblock 2 100 0 gold_block"
            )
            start = server.command("testforblock 2 100 0 gold_block")
            server_has("successfully found", start)
            result["checks"].append("bds_independently_confirmed_remaining_inventory_count")
            server.command("execute if entity @e[type=item] run setblock 3 100 0 diamond_block")
            start = server.command("testforblock 3 100 0 diamond_block")
            server_has("successfully found", start)
            result["checks"].append("bds_independently_confirmed_world_drop")

        if args.split:
            screen = json.loads(until("player_inventory_opened="))
            assert isinstance(screen["id"], int) and screen["id"] >= 0, screen
            result["checks"].append("server_confirmed_inventory_screen_for_split")
            split = json.loads(until("inventory_split_response="))
            assert split["request_id"] == -3, split
            assert split["result"] == "SUCCESS", split
            assert (split["source_before"], split["source_after"],
                    split["target_count"]) == (2, 1, 1), split
            assert split["source_item_id"] == split["target_item_id"] == item["item_id"], split
            result["split"] = split
            result["checks"].append("server_accepted_player_stack_split_and_reconciled_slots")
            screen_closed = json.loads(until("player_inventory_closed="))
            assert screen_closed["id"] == screen["id"] and screen_closed["open_window"] is False
            result["checks"].append("server_confirmed_inventory_screen_close")
            for slot, block in ((2, "gold_block"), (3, "diamond_block")):
                server.command(
                    f"execute as @a[hasitem={{item=dirt,quantity=1,"
                    f"location=slot.hotbar,slot={slot}}}] run setblock {slot} 100 1 {block}"
                )
                start = server.command(f"testforblock {slot} 100 1 {block}")
                server_has("successfully found", start)
            result["checks"].append("bds_independently_confirmed_both_hotbar_slot_counts")

        if args.equip:
            screen = json.loads(until("player_inventory_opened="))
            assert isinstance(screen["id"], int) and screen["id"] >= 0, screen
            assert isinstance(screen["type"], str) and screen["type"], screen
            result["player_inventory_window"] = screen
            result["checks"].append("server_confirmed_player_inventory_open")

            equipped = json.loads(until("armor_equip_response="))
            assert equipped["request_id"] < 0 and equipped["request_id"] % 2 != 0, equipped
            assert equipped["request_id"] != transfer["request_id"], equipped
            assert equipped["result"] == "SUCCESS", equipped
            assert equipped["armor_item_id"] == item["item_id"], equipped
            assert equipped["armor_count"] == 1 and equipped["inventory_count"] == 0, equipped
            result["armor_equip"] = equipped
            result["checks"].append("server_accepted_helmet_equip_and_slot_corrections")
            closed_screen = json.loads(until("player_inventory_closed="))
            assert closed_screen["id"] == screen["id"], (closed_screen, screen)
            assert closed_screen["open_window"] is False, closed_screen
            result["checks"].append("server_confirmed_player_inventory_close")

            # Verify the actual armor slot independently from the packet response.
            server.command(
                "execute as @a[hasitem={item=iron_helmet,location=slot.armor.head}] "
                "run setblock 2 100 0 gold_block"
            )
            start = server.command("testforblock 2 100 0 gold_block")
            server_has("successfully found", start)
            result["checks"].append("bds_independently_confirmed_helmet_armor_slot")

        send("disconnect")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0
        server.wait_player(False)
        result["checks"].append("player_list_empty")
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
