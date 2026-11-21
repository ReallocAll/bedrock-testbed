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
    parser.add_argument("--interrupt", action="store_true", help="remove the trader before exchange")
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
            + (["--interrupt"] if args.interrupt else []),
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
        server.command("setblock 4 100 0 composter")
        server.command("tp EndPilotTest 0.5 100 0.5")
        server.command("gamemode survival EndPilotTest")
        time.sleep(0.5)

        send("watch")
        until("villager_watch_ready")
        start = server.command("summon villager 2 100 0 0 0 minecraft:become_farmer")
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
        assert "farmer" in offers["display_name"].lower(), offers
        assert offers["offers"], offers
        assert any(
            quote["buy_a"] is not None and quote["sell"] is not None
            and quote["buy_a"]["count"] > 0 and quote["sell"]["count"] > 0
            for quote in offers["offers"]
        ), offers
        result["trade"] = offers
        result["checks"].append("server_trade_quote_with_item_pricing")

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

        if args.interrupt:
            until("trade_interrupt_watch_ready")
            # Removing the observed trader forces BDS to terminate the trade UI.
            server.command("kill @e[type=villager_v2]")
            interrupted = json.loads(until("trade_interrupted=", timeout=30))
            assert interrupted["window_id"] == offers["window_id"], interrupted
            assert interrupted["server_initiated"] is True, interrupted
            assert interrupted["open_window"] is False, interrupted
            assert interrupted["stale_exchange_blocked"] is True, interrupted
            assert interrupted["expected_count"] == staged["before"], interrupted
            assert interrupted["restored_count"] >= staged["before"], interrupted
            result["interrupted"] = interrupted
            result["checks"].append("server_close_and_sdk_recovery_state")

            # Check the returned ingredient independently in BDS, not just SDK state.
            server.command(
                f"execute as @a[hasitem={{item={material['name']},quantity={count}..}}] "
                "run setblock 6 100 0 gold_block"
            )
            start = server.command("testforblock 6 100 0 gold_block")
            wait_server("successfully found", start)
            result["checks"].append("independent_bds_returned_trade_ingredient")
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
