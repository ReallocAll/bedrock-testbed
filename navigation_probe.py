"""Real-BDS known-flat A* navigation acceptance (independent server oracle).

The observer is the only Bot.set_input writer. The planner receives no hard-coded
route coordinates; walls are built using actual BDS fill commands.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys

import testbed
import scenarios
from server import PLAYER, ROOT


async def exercise(server, results, transport):
    from endpilot import (
        BotOptions, EventKind, PositionEvent, Runtime, StateEvent,
        TickEvent, event_mask,
    )
    from endpilot.navigation import GoalNear, NavigationError, Navigator
    from endpilot.world import WorldQuery

    endpoint = ("http://127.0.0.1:19132" if transport == "nethernet"
                else "raknet://127.0.0.1:19132")
    loop = asyncio.get_running_loop()
    records = []
    results["navigation"] = {"cases": records}
    forwarded = 0
    corrections = 0
    controller_starts = 0
    exceptions = []
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=endpoint, transport=transport, protocol=2193,
            name=PLAYER, chunk_radius=16, validate_all_packets=True,
            client_cache=False,
        ))
        world = WorldQuery(bot)
        navigator = Navigator(bot, world)
        original_start = navigator.controller.start

        def track_start(*args, **kwargs):
            nonlocal controller_starts
            controller_starts += 1
            return original_start(*args, **kwargs)

        navigator.controller.start = track_start

        def observe(source, event):
            nonlocal forwarded, corrections
            if source is not bot:
                return
            try:
                if isinstance(event, PositionEvent) and event.correction:
                    corrections += 1
                frame = navigator.on_event(event)
                if frame is not None:
                    forwarded += 1
                    source.set_input(frame)  # Exactly one application writer.
            except Exception as exc:
                exceptions.append(f"{type(exc).__name__}: {exc}")
                navigator.stop()

        mask = 0
        for kind in (EventKind.tick, EventKind.position, EventKind.dimension,
                     EventKind.state, EventKind.packet_decoded):
            mask |= event_mask(kind)
        unsubscribe = runtime.subscribe(observe, events=mask)

        async def authoritative():
            offset = server.command(f"querytarget {PLAYER}")
            deadline = loop.time() + 5
            while loop.time() < deadline:
                lines = server.snapshot(offset)
                fields = [line.partition("]:")[2].strip() for line in lines]
                begin = next((i for i, v in enumerate(fields) if v.startswith("Target data:")), None)
                if begin is not None:
                    end = next((i for i in range(begin + 1, len(fields)) if fields[i] == "]"), None)
                    if end is not None:
                        raw = fields[begin].split("Target data:", 1)[1] + " ".join(fields[begin + 1:end + 1])
                        data = json.loads(raw)
                        if len(data) != 1 or data[0]["dimension"] != 0:
                            raise AssertionError(f"Unexpected querytarget result: {data}")
                        pos = data[0]["position"]
                        return (float(pos["x"]), float(pos["y"]), float(pos["z"]))
                if any("unknown command" in v.lower() or "syntax error" in v.lower() for v in lines):
                    raise RuntimeError("BDS querytarget unavailable")
                await asyncio.sleep(.025)
            raise TimeoutError(f"querytarget timed out: {server.snapshot(offset)[-6:]}")

        async def fill(command):
            for attempt in range(5):
                offset = server.command(command)
                until = loop.time() + 5
                while loop.time() < until:
                    lines = server.snapshot(offset)
                    if any("blocks filled" in v.lower() or "block filled" in v.lower() for v in lines):
                        return
                    if any("outside of the world" in v.lower() or "failed" in v.lower() for v in lines):
                        break
                    await asyncio.sleep(.05)
                else:
                    raise TimeoutError(f"BDS did not acknowledge: {command}")
                if not any("outside of the world" in v.lower() for v in lines):
                    raise AssertionError(f"BDS fill failed: {command}: {lines}")
                await asyncio.sleep(.7)
            raise AssertionError(f"BDS chunks not editable: {command}")

        async def reset():
            server.command(f"tp {PLAYER} 0.5 100 0.5")
            until = loop.time() + 8
            while loop.time() < until:
                actual = await authoritative()
                status = bot.get_status()
                if (math.hypot(actual[0] - .5, actual[2] - .5) < .1
                        and abs(actual[1] - 101.62001) < .2
                        and math.hypot(status.position.x - .5, status.position.z - .5) < .2):
                    await asyncio.sleep(.3)
                    return actual
                await asyncio.sleep(.08)
            raise AssertionError(f"Cannot reset grounded player: {actual}")

        async def query_world():
            for cx in (-1, 0, 1):
                for cz in (-1, 0, 1):
                    bot.request_sub_chunks(0, cx, cz, [6])

        async def known(*points):
            until = loop.time() + 20
            request_at = 0
            while loop.time() < until:
                if all(world.can_stand(x, 100, z) is not None for x, z in points):
                    return
                if loop.time() >= request_at:
                    await query_world()
                    request_at = loop.time() + 2
                await asyncio.sleep(.1)
            raise AssertionError(f"WorldQuery still unknown at {points}")

        async def wait_stand(x, z, expected):
            # BDS command acknowledgement can precede inbound block updates;
            # require the *observed* WorldQuery state, not merely the command ack.
            until = loop.time() + 12
            request_at = 0
            while loop.time() < until:
                if world.can_stand(x, 100, z) is expected:
                    return
                if loop.time() >= request_at:
                    await query_world()
                    request_at = loop.time() + 1.5
                await asyncio.sleep(.08)
            raise AssertionError(
                f"WorldQuery did not observe ({x},100,{z}) as {expected}: "
                f"actual={world.can_stand(x, 100, z)}"
            )

        async def moving_to(goal):
            task = asyncio.create_task(navigator.goto(goal))
            deadline = loop.time() + 6
            while loop.time() < deadline:
                await asyncio.sleep(.025)
                if task.done():
                    await task  # Surface a premature planning/movement failure.
                    raise AssertionError("navigation finished before movement cancellation case")
                if (navigator._future is not None and navigator.controller.state == "moving"
                        and navigator.controller.frames >= 8):
                    return task
            task.cancel()
            raise AssertionError("navigation did not enter moving state")

        async def run_goal(name, destination, timeout=30):
            before = await reset()
            await known((0, 0), (destination.x, destination.z))
            record = {"name": name, "origin": before}
            records.append(record)
            start = loop.time()
            sent = forwarded
            starts = controller_starts
            corrected = corrections
            await asyncio.wait_for(navigator.goto(destination), timeout)
            record["navigation_ms"] = round((loop.time() - start) * 1000, 3)
            plan = navigator.last_plan
            record["plan"] = {
                "status": plan.status, "path": plan.path, "cost": plan.cost,
                "expanded": plan.expanded, "peak_open": plan.peak_open,
                "stand_queries": plan.stand_queries, "elapsed_ms": plan.elapsed_ms,
                "max_batch_ms": plan.max_batch_ms,
            }
            record["frames"] = forwarded - sent
            record["execution_segments"] = controller_starts - starts
            record["corrections"] = corrections - corrected
            assert record["corrections"] == 0, record
            await asyncio.sleep(.3)
            p1 = await authoritative()
            await asyncio.sleep(.3)
            p2 = await authoritative()
            record["authoritative"] = p2
            record["final_error"] = math.hypot(
                p2[0] - (destination.x + .5), p2[2] - (destination.z + .5))
            record["stop_drift"] = math.dist((p1[0], p1[2]), (p2[0], p2[2]))
            assert plan.status == "complete" and record["frames"] > 0, record
            assert record["final_error"] <= .12 and record["stop_drift"] <= .05, record
            return record

        try:
            await bot.wait_ready(65)
            await authoritative()
            server.command(f"tp {PLAYER} 0.5 100 0.5")
            await asyncio.sleep(.8)
            await fill("fill -12 100 -12 12 105 28 air")
            await fill("fill -12 99 -12 12 99 28 stone")
            await query_world()
            await known((0, 0), (0, 8), (3, 4))

            # Frontier-only planning on real decoded BDS cells, with a test-
            # imposed unknown boundary. This is NOT natural distant loading.
            from endpilot.navigation import plan_frontier

            class LimitedObservedView:
                def can_stand(self, x, y, z):
                    if -1 <= x <= 1 and 0 <= z <= 8:
                        return world.can_stand(x, y, z)
                    return None

            await known(*((x, z) for x in range(-1, 2) for z in range(9)))
            limited = LimitedObservedView()
            partial = await plan_frontier(limited, (0, 100, 0), GoalNear(0, 100, 12))
            assert partial.status == "frontier" and partial.frontier == (0, 100, 8)
            assert partial.search_truncated is False
            assert partial.progressed and partial.path[-1] == partial.frontier
            assert all(world.can_stand(*node) is True for node in partial.path)
            assert all(limited.can_stand(*node) is True for node in partial.path)
            assert all(max(abs(b[0] - a[0]), abs(b[2] - a[2])) == 1
                       for a, b in zip(partial.path, partial.path[1:]))
            assert partial.cost == 8.0
            records.append({
                "name": "controlled_safe_frontier",
                "world_source": "real_bds_decoded_worldquery",
                "unknown_source": "test_imposed_view_not_natural_unloaded_subchunks",
                "status": partial.status, "frontier": partial.frontier,
                "search_truncated": partial.search_truncated,
                "progressed": partial.progressed,
                "path": partial.path, "cost": partial.cost,
                "expanded": partial.expanded, "stand_queries": partial.stand_queries,
                "max_batch_ms": partial.max_batch_ms,
            })
            # Use the same BDS-decoded cells and test-imposed boundary, but
            # stop before exhausting the 27 known nodes. No movement or
            # SubChunk requests are issued by plan_frontier.
            truncated = await plan_frontier(
                limited, (0, 100, 0), GoalNear(0, 100, 12),
                max_expansions=3, batch_size=1)
            assert truncated.status == "frontier" and truncated.search_truncated is True
            assert truncated.expanded == 3 and truncated.path
            assert truncated.frontier == truncated.path[-1]
            assert truncated.progressed == (len(truncated.path) > 1)
            assert all(world.can_stand(*node) is True for node in truncated.path)
            assert all(limited.can_stand(*node) is True for node in truncated.path)
            cost = 0.0
            for a, b in zip(truncated.path, truncated.path[1:]):
                dx, dz = b[0] - a[0], b[2] - a[2]
                assert a[1] == b[1] and max(abs(dx), abs(dz)) == 1
                if dx and dz:
                    assert limited.can_stand(a[0] + dx, a[1], a[2]) is True
                    assert limited.can_stand(a[0], a[1], a[2] + dz) is True
                cost += math.hypot(dx, dz)
            assert math.isclose(truncated.cost, cost, abs_tol=1e-9)
            assert any(limited.can_stand(truncated.frontier[0] + dx, 100,
                                         truncated.frontier[2] + dz) is None
                       for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            records.append({
                "name": "controlled_truncated_safe_frontier",
                "world_source": "real_bds_decoded_worldquery",
                "unknown_source": "test_imposed_view_not_natural_unloaded_subchunks",
                "status": truncated.status, "search_truncated": truncated.search_truncated,
                "frontier": truncated.frontier, "path": truncated.path,
                "cost": truncated.cost, "expanded": truncated.expanded,
                "progressed": truncated.progressed, "stand_queries": truncated.stand_queries,
                "elapsed_ms": truncated.elapsed_ms, "max_batch_ms": truncated.max_batch_ms,
            })
            complete = await plan_frontier(world, (0, 100, 0), GoalNear(0, 100, 8))
            assert complete.status == "complete" and complete.frontier is None
            assert complete.search_truncated is False
            assert all(world.can_stand(*node) is True for node in complete.path)
            records.append({"name": "known_safe_frontier_complete",
                            "status": complete.status, "path": complete.path})

            # Strict A/B metrics: same scenario, oracle, transport and code for
            # both pinned SDK SHAs. The reset/known-world waits are not timed.
            for length in (8, 16):
                for repeat in range(3):
                    straight = await run_goal(
                        f"straight_{length}_r{repeat + 1}", GoalNear(0, 100, length))
                    assert all(node[0] == 0 for node in straight["plan"]["path"])
                    assert len(straight["plan"]["path"]) == length + 1

            # Narrow corridor with known solid walls on both sides.
            await fill("fill -1 100 1 -1 101 8 stone")
            await fill("fill 1 100 1 1 101 8 stone")
            corridor = await run_goal("one_wide_corridor", GoalNear(0, 100, 8))
            assert all(node[0] == 0 for node in corridor["plan"]["path"])
            await fill("fill -1 100 1 -1 101 8 air")
            await fill("fill 1 100 1 1 101 8 air")
            await wait_stand(-1, 4, True)
            await wait_stand(1, 4, True)

            diagonal = await run_goal("diagonal", GoalNear(8, 100, 8))
            assert any(a[0] != b[0] and a[2] != b[2]
                       for a, b in zip(diagonal["plan"]["path"],
                                       diagonal["plan"]["path"][1:]))

            await fill("fill 0 100 2 0 101 6 stone")
            await known((0, 3), (-1, 3), (1, 3))
            await wait_stand(0, 3, False)
            for repeat in range(3):
                detour = await run_goal(f"wall_detour_r{repeat + 1}", GoalNear(0, 100, 8))
                assert all(not (x == 0 and 2 <= z <= 6) for x, _, z in detour["plan"]["path"])
                assert any(x != 0 for x, _, _ in detour["plan"]["path"])
            await fill("fill 0 100 2 0 101 6 air")
            await wait_stand(0, 3, True)

            await fill("fill 0 100 8 0 101 8 stone")
            await reset()
            await wait_stand(0, 8, False)
            try:
                await asyncio.wait_for(navigator.goto(GoalNear(0, 100, 8)), 5)
            except NavigationError as exc:
                assert str(exc) == "unreachable", str(exc)
                records.append({"name": "unreachable", "status": str(exc)})
            else:
                raise AssertionError("planned through solid goal")
            await fill("fill 0 100 8 0 101 8 air")
            await wait_stand(0, 8, True)

            await reset()
            try:
                await asyncio.wait_for(navigator.goto(GoalNear(2000, 100, 2000)), 5)
            except NavigationError as exc:
                assert str(exc) == "need_world", str(exc)
                records.append({"name": "unknown_world", "status": str(exc)})
            else:
                raise AssertionError("accepted unknown destination")

            await reset()
            task = await moving_to(GoalNear(0, 100, 8))
            navigator.stop()
            try:
                await task
            except NavigationError as exc:
                assert str(exc) == "navigation stopped"
            else:
                raise AssertionError("stop did not interrupt goto")
            await asyncio.sleep(.4)
            p1 = await authoritative()
            await asyncio.sleep(.4)
            p2 = await authoritative()
            assert math.dist((p1[0], p1[2]), (p2[0], p2[2])) <= .05
            records.append({"name": "stop", "state": navigator.controller.state,
                            "drift": math.dist((p1[0], p1[2]), (p2[0], p2[2]))})

            await reset()
            task = await moving_to(GoalNear(0, 100, 8))
            server.command(f"tp {PLAYER} 2.5 100 2.5")
            try:
                await asyncio.wait_for(task, 6)
            except NavigationError:
                pass
            else:
                raise AssertionError("teleport did not invalidate route")
            assert navigator.controller.state == "failed"
            records.append({"name": "teleport", "state": navigator.controller.state})

            await reset()
            task = await moving_to(GoalNear(0, 100, 8))
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Task.cancel did not propagate")
            assert navigator.controller.state in ("coasting", "cancelled")
            records.append({"name": "task_cancel", "state": navigator.controller.state})

            await reset()
            task = await moving_to(GoalNear(0, 100, 8))
            # Update a future route cell while motion is active. The packet
            # observer must invalidate it before a controller frame crosses it.
            await fill("fill 0 100 5 0 101 5 stone")
            await wait_stand(0, 5, False)
            try:
                await asyncio.wait_for(task, 8)
            except NavigationError as exc:
                assert "path invalidated" in str(exc), str(exc)
                records.append({"name": "path_block_changed", "reason": str(exc)})
            else:
                raise AssertionError("active route survived a new solid obstacle")
            assert not navigator._path
            await fill("fill 0 100 5 0 101 5 air")
            await wait_stand(0, 5, True)

            await reset()
            task = await moving_to(GoalNear(0, 100, 8))
            previous = bot.get_status().session_id
            await bot.disconnect()
            try:
                await task
            except NavigationError:
                pass
            else:
                raise AssertionError("disconnect failed to stop navigation")
            await bot.reconnect()
            await bot.wait_ready(65)
            assert bot.get_status().session_id != previous
            assert not navigator._path
            records.append({"name": "disconnect_reconnect", "old_session": previous,
                            "new_session": bot.get_status().session_id})
            await bot.disconnect()
            assert not exceptions, exceptions
            results["checks"].append("navigation_real_bds_authoritative")
        finally:
            navigator.stop()
            unsubscribe()
            (ROOT / "work" / "navigation_trajectory.json").write_text(
                json.dumps(records, indent=2, ensure_ascii=False) + "\n"
            )


if __name__ == "__main__":
    scenarios.exercise = exercise
    sys.exit(testbed.main())
