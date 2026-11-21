"""Verify obstacle-aware public SDK walking against real BDS querytarget."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
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


async def exercise_close_walk(bot: Bot, server: Server, results: dict, origin: dict):
    """Close the public Bot while a real BDS walk is still running."""
    movement = bot._movement
    ticker = movement._ticker
    walk = asyncio.create_task(bot.walk_forward(160))
    await asyncio.sleep(0.65)
    if walk.done():
        await walk
        raise AssertionError("Walk completed before Bot.close()")
    moving = await asyncio.to_thread(server_target, server)
    if moving["position"]["z"] <= origin["position"]["z"] + 0.4:
        raise AssertionError(f"BDS did not observe active walk: {origin} -> {moving}")

    await asyncio.wait_for(bot.close(), 5)
    frames = await asyncio.wait_for(walk, 5)
    if not 5 <= frames < 160:
        raise AssertionError(f"Bot.close() did not interrupt active walk: {frames}")
    if movement._active_done is not None:
        raise AssertionError("Closed walk retained an active movement action")
    if not ticker.done():
        raise AssertionError("Idle ticker survived Bot.close()")
    if bot._movement is not None or bot._session is not None:
        raise AssertionError("Bot retained movement or session after close")
    results["close_walk"] = {
        "submitted_frames": frames, "server_active_position": moving,
    }
    results["checks"].extend((
        "bds_querytarget_confirms_active_walk",
        "bot_close_interrupts_walk_and_drains_action_and_ticker",
    ))


async def exercise_cancel_walk(bot: Bot, server: Server, results: dict, origin: dict):
    """Cancel a live public walk, then reuse the still-connected player."""
    movement = bot._movement
    ticker = movement._ticker
    walk = asyncio.create_task(bot.walk_forward(160))
    await asyncio.sleep(0.65)
    if walk.done():
        await walk
        raise AssertionError("Walk completed before active cancellation")
    moving = await asyncio.to_thread(server_target, server)
    if moving["position"]["z"] <= origin["position"]["z"] + 0.4:
        raise AssertionError(f"BDS did not observe active walk: {origin} -> {moving}")

    walk.cancel()
    try:
        await asyncio.wait_for(walk, 5)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("Public walk did not propagate CancelledError")
    if movement._active_done is not None:
        raise AssertionError("Cancelled walk retained an active movement action")
    if ticker.done():
        raise AssertionError("Cancelled walk stopped the idle input ticker")

    await asyncio.sleep(0.4)
    settled = await asyncio.to_thread(server_target, server)
    await asyncio.sleep(0.4)
    rested = await asyncio.to_thread(server_target, server)
    drift = math.hypot(
        rested["position"]["x"] - settled["position"]["x"],
        rested["position"]["z"] - settled["position"]["z"],
    )
    if drift > 0.2:
        raise AssertionError(f"BDS continued walking after cancel: {settled} -> {rested}")

    x, y, z = bot.position
    await bot.look_at((x - 6, y, z))
    resumed = await bot.walk_forward(8)
    await asyncio.sleep(0.3)
    after = await asyncio.to_thread(server_target, server)
    if resumed != 8 or after["position"]["x"] >= rested["position"]["x"] - 0.2:
        raise AssertionError(f"BDS did not accept the next walk: {rested} -> {after}")
    if movement._active_done is not None:
        raise AssertionError("Follow-up walk retained an active movement action")
    results["cancel_walk"] = {
        "action": "walk_forward", "outcome": "CancelledError",
        "active": moving, "settled": settled, "rested": rested,
        "stop_drift": drift, "followup_frames": resumed,
        "followup_server_position": after,
    }
    results["checks"].extend((
        "bds_querytarget_confirms_active_walk",
        "cancelled_walk_releases_input_and_ticker_survives",
        "bds_querytarget_confirms_stop_and_same_bot_reuse",
    ))


async def exercise(server: Server, transport: str, results: dict,
                   kick_active: bool = False, close_active: bool = False,
                   cancel_active: bool = False, cancel_walk: bool = False,
                   close_walk: bool = False):
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

        if close_walk:
            await exercise_close_walk(bot, server, results, origin)
        elif cancel_walk:
            await exercise_cancel_walk(bot, server, results, origin)
        elif kick_active or close_active or cancel_active:
            # A longer goal keeps navigation active while BDS observes a detour.
            movement = bot._movement
            ticker = movement._ticker
            if kick_active:
                disconnected = asyncio.create_task(bot.wait_for_server_disconnect(timeout=10))
                await asyncio.sleep(0)
            task = asyncio.create_task(bot.navigate_to((7, 100, 0), ticks=160))
            await asyncio.sleep(0.65)
            if task.done():
                await task
                raise AssertionError("Navigation finished before the requested shutdown")
            detour = await asyncio.to_thread(server_target, server)
            results["detour"] = detour
            if detour["position"]["z"] < 0.75:
                raise AssertionError(f"BDS did not observe active navigation detour: {detour}")
            results["checks"].append("bds_querytarget_confirms_active_navigation")
            if close_active:
                await asyncio.wait_for(bot.close(), 5)
                frames = await asyncio.wait_for(task, 5)
                if not 5 <= frames < 160:
                    raise AssertionError(f"Close did not interrupt active navigation: {frames}")
                results["close"] = {"submitted_frames": frames}
                results["checks"].append("bot_close_interrupts_active_navigation")
            elif cancel_active:
                task.cancel()
                try:
                    await asyncio.wait_for(task, 5)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("Task.cancel did not propagate from public navigate_to")
                if movement._active_done is not None:
                    raise AssertionError("Cancelled navigation retained an active action")
                before = await asyncio.to_thread(server_target, server)
                await asyncio.sleep(0.35)
                stopped = await asyncio.to_thread(server_target, server)
                drift = ((stopped["position"]["x"] - before["position"]["x"]) ** 2
                         + (stopped["position"]["z"] - before["position"]["z"]) ** 2) ** 0.5
                if drift > 0.15:
                    raise AssertionError(f"BDS still moving after cancellation: {before} -> {stopped}")
                if ticker.done():
                    raise AssertionError("Cancel unexpectedly stopped the idle ticker")
                x, y, z = bot.position
                await bot.look_at((x, y, z - 6))
                resumed = await bot.walk_forward(8)
                await asyncio.sleep(0.2)
                after = await asyncio.to_thread(server_target, server)
                if resumed != 8 or after["position"]["z"] >= stopped["position"]["z"] - 0.2:
                    raise AssertionError(f"BDS did not accept follow-up walk: {resumed}, {stopped} -> {after}")
                results["cancel"] = {
                    "action": "navigate_to", "outcome": "CancelledError",
                    "stop_drift": drift, "followup_frames": resumed,
                    "followup_server_position": after,
                }
                results["checks"].extend((
                    "task_cancel_releases_navigation_without_closing_bot",
                    "bds_querytarget_confirms_stop_and_followup_walk",
                ))
            else:
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
                results["checks"].append("bds_kick_interrupts_navigation")
            if movement._active_done is not None:
                raise AssertionError("Navigation left an active action behind")
            await asyncio.sleep(0)
            if cancel_active:
                if ticker.done():
                    raise AssertionError("Idle ticker stopped after cancellation")
                results["checks"].append("navigation_ticker_survives_cancel")
            else:
                if not ticker.done():
                    raise AssertionError("Movement ticker survived shutdown")
                results["checks"].append("navigation_ticker_drained")
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
    parser.add_argument("--close-active", action="store_true")
    parser.add_argument("--cancel-active", action="store_true")
    parser.add_argument("--cancel-walk", action="store_true")
    parser.add_argument("--close-walk", action="store_true")
    args = parser.parse_args()
    if sum((args.kick_active, args.close_active, args.cancel_active,
            args.cancel_walk, args.close_walk)) > 1:
        parser.error("active shutdown/cancellation modes are mutually exclusive")
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
        asyncio.run(exercise(server, args.transport, results,
                             kick_active=args.kick_active, close_active=args.close_active,
                             cancel_active=args.cancel_active, cancel_walk=args.cancel_walk,
                             close_walk=args.close_walk))
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
