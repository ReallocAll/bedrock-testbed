"""Exercise public villager quote observation on an isolated real Bedrock server."""

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
    parser.add_argument("--interrupt", action="store_true", help="cancel a staged trade through the public SDK")
    parser.add_argument("--two-input", action="store_true", help="verify a quoted two-input trade")
    parser.add_argument("--server-interrupt", action="store_true",
                        help="kick the player after the server accepts trade staging")
    parser.add_argument("--reconnect", action="store_true",
                        help="rejoin the kicked player and compare inventory to BDS")
    args = parser.parse_args()
    if args.reconnect and not args.server_interrupt:
        parser.error("--reconnect requires --server-interrupt")

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
            "vcs_info", {},
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
            + (["--interrupt"] if args.interrupt else [])
            + (["--two-input"] if args.two_input else [])
            + (["--server-interrupt"] if args.server_interrupt else [])
            + (["--reconnect"] if args.reconnect else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        output = queue.Queue()

        def read_lines():
            for line in client.stdout:
                print("[client] " + line.rstrip(), flush=True)
                output.put(line)

        threading.Thread(target=read_lines, daemon=True).start()

        def until(marker, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=max(0.01, min(0.5, deadline - time.monotonic())))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError(f"client exited before {marker}")
                    continue
                if marker in line:
                    return line.partition(marker)[2].strip()
            raise TimeoutError(f"missing client marker {marker}")

        def send(line):
            assert client.stdin is not None
            client.stdin.write(line + "\n")
            client.stdin.flush()

        def wait_server(marker, offset, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                lines = server.snapshot(offset)
                if any(marker in line.lower() for line in lines):
                    return
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited while waiting for " + marker)
                time.sleep(0.1)
            raise AssertionError(f"BDS did not report {marker}: {server.snapshot(offset)[-15:]}")

        server.wait_player(True)
        until("spawned EndPilotTest")
        result["checks"].append("player_spawned")
        catalog = json.loads(until("server_recipe_catalog=", timeout=20))
        assert catalog["count"] > 0, catalog
        assert catalog["recipe_id"] and int(catalog["net_id"]) >= 0, catalog
        assert catalog["kind"] in (
            "shaped_recipes", "shapeless_recipes", "user_data_shapeless_recipes",
            "shapeless_chemistry_recipes", "shaped_chemistry_recipes",
        ), catalog
        assert catalog["first_result_id"] != 0 and catalog["first_result_count"] > 0, catalog
        assert catalog["first_ingredient_count"] > 0, catalog
        result["recipes"] = catalog
        result["checks"].append("server_crafting_data_catalog_and_id_lookup")
        server.command("tp EndPilotTest 0.5 100 0.5")
        time.sleep(3)
        filled(server, "fill -6 100 -6 6 107 6 air")
        filled(server, "fill -6 99 -6 6 99 6 stone")
        server.command("setblock 4 100 0 " + ("lectern" if args.two_input else "composter"))
        server.command("tp EndPilotTest 0.5 100 0.5")
        server.command("gamemode survival EndPilotTest")
        time.sleep(0.5)

        send("watch")
        until("villager_watch_ready")
        profession = "librarian" if args.two_input else "farmer"
        start = server.command(f"summon villager 2 100 0 0 0 minecraft:become_{profession}")
        spawned = json.loads(until("villager_spawn="))
        assert spawned["actor_type"] in ("minecraft:villager", "minecraft:villager_v2"), spawned
        assert int(spawned["runtime_id"]) > 0, spawned
        wait_server("successfully summoned", start)
        result["spawn"] = spawned
        result["checks"].append("bds_villager_spawn_observed")

        send("interact")
        offers = json.loads(until("villager_quotes=", timeout=25))
        assert offers["trader_unique_id"] == spawned["entity_id"], (offers, spawned)
        assert int(offers["window_id"]) >= 0, offers
        assert profession in offers["display_name"].lower(), offers
        assert offers["offers"], offers
        assert any(
            quote["buy_a"] is not None and quote["sell"] is not None
            and quote["buy_a"]["count"] > 0 and quote["sell"]["count"] > 0
            for quote in offers["offers"]
        ), offers
        result["trade"] = offers
        result["checks"].append("server_trade_quote_with_item_pricing")

        if args.two_input:
            selected = next((
                quote for quote in offers["offers"]
                if quote["buy_a"] is not None and quote["buy_b"] is not None
                and quote["sell"] is not None and quote["recipe_net_id"] is not None
                and quote["uses"] is not None and quote["max_uses"] is not None
                and quote["uses"] < quote["max_uses"]
            ), None)
            assert selected is not None, "no server-issued two-input librarian quote"
            reported = json.loads(until("two_input_offer=", timeout=10))
            assert reported == selected, (reported, selected)
            result["selected_two_input_offer"] = selected
            previous_request = 0
            for number, material in enumerate((selected["buy_a"], selected["buy_b"]), 1):
                assert re.fullmatch(r"minecraft:[a-z0-9_]+", material["name"]), material
                count = int(material["count"])
                assert 1 <= count <= 64, material
                server.command(f"give EndPilotTest {material['name']} {count}")
                send("stage")
                staged = json.loads(until(f"trade_input_{number}_result=", timeout=30))
                assert staged["result"] == "SUCCESS", staged
                assert staged["ingredient"] == number and staged["name"] == material["name"], staged
                assert staged["amount"] == count and staged["before"] >= count, staged
                assert staged["after"] == staged["before"] - count, staged
                assert staged["request_id"] < previous_request, staged
                assert any(
                    c["name"] == f"TRADE2_INGREDIENT_{number}_CONTAINER"
                    and any(slot["slot"] == number + 3 and slot["amount"] == count
                            for slot in c["slots"])
                    for c in staged["input_containers"]
                ), staged
                result[f"input_{number}"] = staged
                result["checks"].append(f"bds_accepted_confirmed_trade_input_{number}")
                previous_request = staged["request_id"]

            exchanged = json.loads(until("trade_exchange_result=", timeout=30))
            result["exchange"] = exchanged
            assert exchanged["result"] == "SUCCESS", exchanged
            assert exchanged["request_id"] < previous_request, exchanged
            assert exchanged["output_slot"] == 9 and exchanged["output_id"] != 0, exchanged
            assert exchanged["output_count"] == selected["sell"]["count"], exchanged
            assert any(
                c["name"] in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                              "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
                and any(p["slot"] == 9 and p["amount"] == selected["sell"]["count"]
                        for p in c["slots"])
                for c in exchanged["containers"]
            ), exchanged
            for number in (1, 2):
                assert any(
                    c["name"] == f"TRADE2_INGREDIENT_{number}_CONTAINER"
                    and any(p["slot"] == number + 3 and p["amount"] == 0
                            for p in c["slots"])
                    for c in exchanged["containers"]
                ), exchanged
            result["checks"].append("bds_confirmed_both_trade_inputs_consumed")
            result["checks"].append("bds_confirmed_two_input_exchange_and_output")
            sell = selected["sell"]
            assert re.fullmatch(r"minecraft:[a-z0-9_]+", sell["name"]), sell
            server.command(
                f"execute as @a[hasitem={{item={sell['name']},quantity={sell['count']}..}}] "
                "run setblock 6 100 0 gold_block"
            )
            start = server.command("testforblock 6 100 0 gold_block")
            wait_server("successfully found", start)
            result["checks"].append("independent_bds_two_input_output_oracle")
            closed = int(until("trade_window_closed=", timeout=15))
            assert closed == offers["window_id"], (closed, offers)
            result["checks"].append("two_input_window_close_ack")
        else:
            selected = next(
                quote for quote in offers["offers"]
                if quote["buy_a"] is not None and quote["buy_b"] is None
                and quote["uses"] is not None and quote["max_uses"] is not None
                and quote["uses"] < quote["max_uses"]
            )
            material = selected["buy_a"]
            assert re.fullmatch(r"minecraft:[a-z0-9_]+", material["name"]), material
            count = int(material["count"])
            assert 1 <= count <= 64, material
            server.command(f"give EndPilotTest {material['name']} {count}")
            send("stage")
            staged = json.loads(until("trade_ingredient_result=", timeout=30))
            result["staged"] = staged
            assert staged["result"] == "SUCCESS", staged
            assert staged["before"] >= count, staged
            assert staged["after"] == staged["before"] - count, staged
            assert staged["amount"] == count, staged
            assert any(
                c["name"] in ("TRADE_INGREDIENT_1_CONTAINER", "TRADE2_INGREDIENT_1_CONTAINER")
                and any(slot["amount"] == count for slot in c["slots"])
                for c in staged["input_containers"]
            ), staged
            result["checks"].append("server_accepted_trade_ingredient_and_reconciled_slots")
    
            if args.server_interrupt:
                until("trade_server_interrupt_ready")
                server.command("kick EndPilotTest Server interrupted the staged trade")
                interrupted = json.loads(until("trade_server_interrupted=", timeout=20))
                assert interrupted == {
                    "packet": "DisconnectPacket", "window_id": offers["window_id"],
                    "staging_request_id": staged["request_id"],
                    "staging_result": "SUCCESS", "exchange_submitted": False,
                }, interrupted
                server.wait_player(False)
                result["interruption"] = interrupted
                result["checks"].append("bds_initiated_disconnect_after_confirmed_stage")
                if args.reconnect:
                    send("rejoin")
                    recovered = json.loads(until("trade_reconnected=", timeout=40))
                    server.wait_player(True)
                    assert recovered["item_id"] != 0, recovered
                    assert recovered["expected_count"] == staged["before"], recovered
                    assert recovered["observed_slots"] >= 9, recovered
                    assert recovered["open_window"] is False, recovered
                    assert recovered["stale_exchange_blocked"] is True, recovered
                    result["reconnected"] = recovered
                    result["checks"].append("same_bot_rejoined_with_fresh_inventory_and_no_trade_window")

                    # Independent server oracle: the kick need not refund a staged
                    # ingredient. Compare BDS's persisted inventory with the SDK.
                    selector = f"@a[hasitem={{item={material['name']},quantity={count}..}}]"
                    server.command(
                        f"execute if entity {selector} run setblock 6 100 0 gold_block"
                    )
                    server.command(
                        f"execute unless entity {selector} run setblock 6 100 0 diamond_block"
                    )
                    expected_block = (
                        "gold_block" if recovered["observed_count"] >= count
                        else "diamond_block"
                    )
                    start = server.command(f"testforblock 6 100 0 {expected_block}")
                    wait_server("successfully found", start)
                    result["bds_ingredient_available"] = expected_block == "gold_block"
                    result["checks"].append("independent_bds_reconnect_inventory_oracle")
                    # Rejoin is not an automatic retry. The client waits for a
                    # newly observed actor and newly quoted offer; BDS controls
                    # when the replacement trader and ingredient become available.
                    until("trade_retry_watch_ready", timeout=25)
                    server.command("kill @e[type=villager]")
                    start = server.command(
                        "summon villager 2 100 0 0 0 minecraft:become_farmer"
                    )
                    retry_spawn = json.loads(until("trade_retry_spawn=", timeout=25))
                    assert retry_spawn["entity_id"] != spawned["entity_id"], retry_spawn
                    assert retry_spawn["actor_type"] in (
                        "minecraft:villager", "minecraft:villager_v2"
                    ), retry_spawn
                    wait_server("successfully summoned", start)
                    result["retry_spawn"] = retry_spawn
                    result["checks"].append("reconnected_player_observed_new_villager")
                    # Reconnect does not imply a MovePlayer correction. As in
                    # the initial world setup, BDS sends one after teleport.
                    server.command("tp EndPilotTest 0.5 100 1.5")
                    send("interact")
                    retry_quote = json.loads(until("trade_retry_offer=", timeout=25))
                    assert retry_quote["trader_unique_id"] == retry_spawn["entity_id"], retry_quote
                    offer = retry_quote["offer"]
                    assert offer["buy_a"] is not None and offer["buy_b"] is None, offer
                    assert offer["sell"] is not None and offer["recipe_net_id"] is not None, offer
                    assert offer["uses"] < offer["max_uses"], offer
                    assert re.fullmatch(r"minecraft:[a-z0-9_]+", offer["buy_a"]["name"]), offer
                    assert re.fullmatch(r"minecraft:[a-z0-9_]+", offer["sell"]["name"]), offer
                    amount = int(offer["buy_a"]["count"])
                    assert 1 <= amount <= 64, offer
                    result["retry_quote"] = retry_quote
                    server.command(f"give EndPilotTest {offer['buy_a']['name']} {amount}")
                    send("stage")
                    retried = json.loads(until("trade_reconnect_retry_result=", timeout=35))
                    assert retried["trader_unique_id"] == retry_spawn["entity_id"], retried
                    assert retried["window_id"] == retry_quote["window_id"], retried
                    assert retried["closed_window"] == retry_quote["window_id"], retried
                    assert retried["ingredient_name"] == offer["buy_a"]["name"], retried
                    assert retried["ingredient_count"] == amount, retried
                    assert retried["ingredient_after"] == retried["ingredient_before"] - amount, retried
                    assert retried["stage_result"] == retried["result"] == "SUCCESS", retried
                    assert retried["stage_request_id"] < 0, retried
                    assert retried["request_id"] < retried["stage_request_id"], retried
                    assert retried["output_name"] == offer["sell"]["name"], retried
                    assert retried["output_slot"] == 9 and retried["output_id"] != 0, retried
                    assert retried["output_count"] == offer["sell"]["count"], retried
                    result["retry"] = retried
                    result["checks"].append("fresh_server_quote_staged_and_exchanged_after_reconnect")

                    # An ItemStackResponse cannot independently prove the BDS
                    # inventory effect. Query the live player's actual inventory.
                    server.command("setblock 5 100 0 air")
                    sell = offer["sell"]
                    server.command(
                        f"execute as @a[hasitem={{item={sell['name']},quantity={sell['count']}..}}] "
                        "run setblock 5 100 0 gold_block"
                    )
                    start = server.command("testforblock 5 100 0 gold_block")
                    wait_server("successfully found", start)
                    result["checks"].append("independent_bds_post_reconnect_trade_output_oracle")
            elif args.interrupt:
                until("trade_abort_ready")
                send("abort")
                unstaged = json.loads(until("trade_unstage_result=", timeout=30))
                assert unstaged["result"] == "SUCCESS", unstaged
                assert unstaged["request_id"] < staged["request_id"], unstaged
                result["unstaged"] = unstaged
                result["checks"].append("server_accepted_trade_input_return")
                aborted = json.loads(until("trade_aborted=", timeout=30))
                assert aborted["window_id"] == offers["window_id"], aborted
                assert aborted["close_acknowledged"] is True, aborted
                assert aborted["open_window"] is False, aborted
                assert aborted["stale_exchange_blocked"] is True, aborted
                assert aborted["original_count"] == staged["before"], aborted
                observed_return = aborted["observed_count"] >= staged["before"]
                assert observed_return is True, aborted
                result["aborted"] = aborted
                result["checks"].append("trade_cancel_ack_and_stale_quote_rejection")
    
                # Ask BDS independently whether the original stack returned. The
                # close response alone does not guarantee an inventory correction.
                selector = f"@a[hasitem={{item={material['name']},quantity={count}..}}]"
                server.command(
                    f"execute if entity {selector} run setblock 6 100 0 gold_block"
                )
                server.command(
                    f"execute unless entity {selector} run setblock 6 100 0 diamond_block"
                )
                expected_block = "gold_block" if observed_return else "diamond_block"
                start = server.command(f"testforblock 6 100 0 {expected_block}")
                wait_server("successfully found", start)
                result["bds_ingredient_returned"] = observed_return
                result["checks"].append("independent_bds_inventory_matches_sdk_observation")
    
                send("retry")
                retried = json.loads(until("trade_retry_result=", timeout=35))
                assert retried["uses_after_reopen"] == retried["uses_before"] == selected["uses"], retried
                assert retried["stage_result"] == retried["result"] == "SUCCESS", retried
                assert retried["stage_request_id"] < unstaged["request_id"], retried
                assert retried["request_id"] < retried["stage_request_id"], retried
                assert retried["output_slot"] == 9 and retried["output_id"] != 0, retried
                assert retried["output_count"] == selected["sell"]["count"], retried
                assert retried["ingredient_after"] == staged["before"] - count, retried
                assert retried["closed_window"] == retried["reopened_window"], retried
                result["retry"] = retried
                result["checks"].append("fresh_quote_retry_accepted_after_cancel")
    
                sell = selected["sell"]
                server.command(
                    f"execute as @a[hasitem={{item={sell['name']},quantity={sell['count']}..}}] "
                    "run setblock 5 100 0 gold_block"
                )
                start = server.command("testforblock 5 100 0 gold_block")
                wait_server("successfully found", start)
                result["checks"].append("independent_bds_retried_trade_output_oracle")
            else:
                exchanged = json.loads(until("trade_exchange_result=", timeout=30))
                result["exchange"] = exchanged
                assert exchanged["result"] == "SUCCESS", exchanged
                assert exchanged["request_id"] < staged["request_id"], exchanged
                assert exchanged["output_slot"] == 9, exchanged
                assert exchanged["output_id"] is not None and exchanged["output_id"] != 0, exchanged
                assert exchanged["output_count"] == int(selected["sell"]["count"]), exchanged
                assert any(
                    c["name"] in ("HOTBAR_CONTAINER", "INVENTORY_CONTAINER",
                                  "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER")
                    and any(s["slot"] == 9 and s["amount"] == int(selected["sell"]["count"])
                            for s in c["slots"])
                    for c in exchanged["containers"]
                ), exchanged
                result["checks"].append("server_accepted_exchange_and_output_slot_reconciled")
    
                # Independent BDS inventory oracle; SDK response alone is insufficient.
                sell = selected["sell"]
                server.command(
                    f"execute as @a[hasitem={{item={sell['name']},quantity={sell['count']}..}}] "
                    "run setblock 6 100 0 gold_block"
                )
                start = server.command("testforblock 6 100 0 gold_block")
                wait_server("successfully found", start)
                result["checks"].append("independent_bds_traded_output_inventory_oracle")
    
                closed = int(until("trade_window_closed=", timeout=15))
                assert closed == offers["window_id"], (closed, offers)
                result["checks"].append("trade_close_ack_matches_window")
    

        if not args.server_interrupt or args.reconnect:
            send("close")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0, client.returncode
        server.wait_player(False)
        result["checks"].append("bds_players_zero_after_leave")
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
                result["checks"].append("bds_clean_stop")
            except Exception as error:
                result["status"] = "FAIL"
                result["stop_error"] = f"{type(error).__name__}: {error}"
                traceback.print_exc()
        (work / "villager_trade_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        )
        print("TRADE_RESULT=" + json.dumps(result), flush=True)
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
