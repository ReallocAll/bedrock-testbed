"""Verify obstacle-aware public SDK walking against real BDS querytarget."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from endpilot import Bot
from nethernet_settings_probe import server_target
from nethernet_walk_probe import filled
from server import ROOT, Server, configure


async def exercise(server: Server, transport: str, results: dict, kick_active: bool = False):
    bot = Bot("EndPilotTest")
    url = ("http://127.0.0.1:19132" if transport == "nethernet"
           else "raknet://127.0.0.1:19132")
    try:
        if transport == "nethernet":
            await bot.join_nethernet(url)
        else:
            await bot.join_raknet("127.0.0.1", 19132)
        await asyncio.to_thread(server.wait_player, True)
        results["checks"].append("bds_player_spawned")
        server.command("tp EndPilotTest 0.5 100 0.5")
        await bot.wait_for_position_sync()
        await asyncio.sleep(0.7)
        await asyncio.to_thread(filled, server, "fill -8 100 -8 8 105 8 air")
        await asyncio.to_thread(filled, server, "fill -8 99 -8 8 99 8 stone")
        await asyncio.to_thread(filled, server, "fill 1 100 0 1 101 0 gold_block")
        server.command("tp EndPilotTest 0.5 100 0.5")
        await asyncio.sleep(0.8)
        origin = await asyncio.to_thread(server_target, server)
        results["origin"] = origin
        if abs(origin["position"]["x"] - 0.5) > 0.3:
            raise AssertionError(f"BDS origin differs: {origin}")
        results["checks"].append("flat_floor_two_high_gold_obstacle_bds_setup")

        await bot.request_sub_chunk((1, 100, 1))
        sampled = ((0, 99, 0), (0, 100, 0), (0, 101, 0),
                   (1, 100, 0), (1, 101, 0), (0, 100, 1),
                   (1, 100, 1), (3, 100, 0))
        for _ in range(200):
            seen = [bot.observed_block(pos) for pos in sampled]
            if all(value is not None for value in seen):
                break
            await asyncio.sleep(0.05)
        else:
            raise TimeoutError(f"BDS sub-chunk not observed: {seen}")
        states = [v.is_air for v in seen]
        if states != [False, True, True, False, False, True, True, True]:
            raise AssertionError(f"Invalid observed block classification: {states}")
        if bot.observed_block((2000, 100, 2000)) is not None:
            raise AssertionError("unloaded terrain was treated as a block")
        results["observed_blocks"] = dict(zip((str(p) for p in sampled), states))
        results["checks"].append("client_observed_air_solid_and_unknown")

        if kick_active:
            # Capture the real connection's disconnect before BDS issues the kick.
            # A longer goal keeps navigation active while the server observes a detour.
            ticker = bot._movement._ticker
            disconnected = asyncio.create_task(bot.wait_for_server_disconnect(timeout=10))
            await asyncio.sleep(0)
            task = asyncio.create_task(bot.navigate_to((7, 100, 0), ticks=160))
            await asyncio.sleep(0.65)
            if task.done():
                await task
                raise AssertionError("Navigation finished before the server kick")
            detour = await asyncio.to_thread(server_target, server)
            results["detour"] = detour
            if detour["position"]["z"] < 0.75:
                raise AssertionError(f"BDS did not observe active navigation detour: {detour}")
            results["checks"].append("bds_querytarget_confirms_active_navigation")
            server.command("kick EndPilotTest EndPilotKickNavigation")
            packet = await disconnected
            try:
                await asyncio.wait_for(task, 5)
            except ConnectionError as error:
                if "server disconnected" not in str(error):
                    raise
                results["kick"] = {
                    "packet": type(packet).__name__,
                    "action_error": type(error).__name__,
                    "reason": str(error),
                }
            else:
                raise AssertionError("Navigation returned successfully after BDS kick")
            if results["kick"]["packet"] != "DisconnectPacket":
                raise AssertionError(f"Missing typed BDS disconnect: {results['kick']}")
            if bot._movement._active_done is not None:
                raise AssertionError("Navigation left an active action behind")
            await asyncio.sleep(0)
            if not ticker.done():
                raise AssertionError("Movement ticker survived BDS kick")
            results["checks"].append("bds_kick_interrupts_navigation_and_ticker")
        else:
            task = asyncio.create_task(bot.navigate_to((3, 100, 0), ticks=120))
            await asyncio.sleep(0.65)
            detour = await asyncio.to_thread(server_target, server)
            results["detour"] = detour
            frames = await task
            await asyncio.sleep(0.4)
            arrived = await asyncio.to_thread(server_target, server)
            results["arrived"] = arrived
            results["moving_frames_submitted"] = frames
            if not 35 < frames < 120:
                raise AssertionError(f"Navigation did not finish within bound: {frames}")
            if detour["position"]["z"] < 0.75:
                raise AssertionError(f"BDS did not observe lateral detour: {detour}")
            if (abs(arrived["position"]["x"] - 3.5) > 0.6
                    or abs(arrived["position"]["z"] - 0.5) > 0.6):
                raise AssertionError(f"BDS did not reach beyond gold obstacle: {arrived}")
            results["checks"].append("bds_querytarget_detour_and_destination_confirmed")
    finally:
        await bot.close()
    await asyncio.to_thread(server.wait_player, False)
    results["checks"].append("client_left_bds_list_empty")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    parser.add_argument("--kick-active", action="store_true")
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    results = {
        "status": "FAIL", "bds": "1.26.52", "endstone": "0.11.14",
        "protocol": 2193, "transport": args.transport,
        "endpilot_commit": args.endpilot_commit, "checks": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    server = None
    try:
        if not re.fullmatch("[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("endpilot_commit must be exact")
        source = distribution("endpilot").read_text("direct_url.json")
        installed = json.loads(source or "{}").get("vcs_info", {}).get("commit_id")
        if installed != args.endpilot_commit:
            raise AssertionError(f"Installed SDK {installed} differs from {args.endpilot_commit}")
        results["checks"].append("exact_sdk_sha_confirmed")
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
        results["checks"].append("fresh_bds_ready")
        asyncio.run(exercise(server, args.transport, results, args.kick_active))
        server.stop()
        server = None
        results["checks"].append("bds_clean_shutdown")
        results["status"] = "PASS"
    except BaseException as exc:
        results["error"] = f"{type(exc).__name__}: {exc}"
        print(results["error"], file=sys.stderr, flush=True)
    finally:
        if server is not None:
            try:
                server.stop()
            except BaseException as exc:
                results["cleanup_error"] = repr(exc)
                results["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
