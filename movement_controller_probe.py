"""Real BDS flat-ground movement-controller acceptance; separate from smoke."""
from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import replace

import testbed
import scenarios
from server import PLAYER, ROOT


async def exercise(server, results, transport):
    from endpilot import (
        BotOptions, BotState, DimensionEvent, EventKind, PacketDecodedEvent,
        PositionEvent, Runtime, StateEvent, TickEvent, Vec3, event_mask,
    )
    from endpilot._movement import MovementController

    endpoint = ("http://127.0.0.1:19132" if transport == "nethernet"
                else "raknet://127.0.0.1:19132")
    controller = MovementController()
    loop = asyncio.get_running_loop()
    records = []
    results["controller"] = {"cases": records}
    active_case = None

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
                    p = data[0]["position"]
                    return (float(p["x"]), float(p["y"]), float(p["z"]))
            if any("unknown command" in v.lower() or "syntax error" in v.lower() for v in lines):
                raise RuntimeError("BDS querytarget unavailable")
            await asyncio.sleep(0.025)
        raise TimeoutError(f"BDS querytarget timed out: {server.snapshot(offset)[-6:]}")

    def observe(source, event):
        nonlocal active_case
        if source is not bot:
            return
        if isinstance(event, PacketDecodedEvent):
            if active_case is not None and event.packet.header.packet_id == 161:
                active_case["correction_packets"] += 1
            return
        if isinstance(event, PositionEvent) and event.correction and active_case is not None:
            active_case["correction_events"] += 1
        frame = controller.on_event(source.get_status(), event)
        if frame is not None:
            if active_case is not None:
                active_case["frames"] += 1
                if controller.state in ("coasting", "cancelled") and (
                    frame.move.x != 0 or frame.move.y != 0
                ):
                    active_case["stale_move_frames"] += 1
            if (active_case is not None and active_case.get("inject_bad_position")
                    and 8 <= controller.frames < 24):
                # Sustain the discrepancy for multiple ticks; one isolated
                # divergent frame was accepted without correction by real BDS.
                frame = replace(frame, position=Vec3(
                    frame.position.x + 0.45, frame.position.y, frame.position.z,
                ))
                active_case["injected"] = active_case.get("injected", 0) + 1
            source.set_input(frame)

    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=endpoint, transport=transport, protocol=2193,
            name=PLAYER, chunk_radius=16, validate_all_packets=True,
        ))
        await bot.wait_ready(65)
        assert bot.get_status().state == BotState.ready
        mask = 0
        for kind in (EventKind.tick, EventKind.position, EventKind.dimension,
                     EventKind.state, EventKind.packet_decoded):
            mask |= event_mask(kind)
        unsubscribe = runtime.subscribe(observe, events=mask)

        async def reset():
            nonlocal active_case
            active_case = None
            server.command(f"tp {PLAYER} 0.5 100 0.5")
            deadline = loop.time() + 8
            while loop.time() < deadline:
                p = await authoritative()
                local = bot.get_status().position
                if (abs(p[0] - 0.5) < 0.2 and abs(p[2] - 0.5) < 0.2
                        and abs(p[1] - 101.62001) < 0.2
                        and abs(local.y - p[1]) < 0.25):
                    await asyncio.sleep(0.3)
                    assert abs((await authoritative())[2] - 0.5) < 0.2
                    return p
                await asyncio.sleep(0.07)
            raise AssertionError(f"Cannot establish flat grounded origin: {p}")

        async def wait_state(states, seconds=12):
            deadline = loop.time() + seconds
            while loop.time() < deadline and controller.state not in states:
                await asyncio.sleep(0.025)
            if controller.state not in states:
                raise AssertionError(f"controller timed out: {controller.state}, {controller.failure}")
            return controller.state

        async def begin(name):
            nonlocal active_case
            p = await reset()
            record = {"name": name, "origin": p, "frames": 0, "correction_packets": 0,
                      "correction_events": 0, "stale_move_frames": 0}
            records.append(record)
            active_case = record
            return p, record

        async def goal_case(name, dx, dz):
            p, rec = await begin(name)
            target = (p[0] + dx, p[1], p[2] + dz)
            controller.start(bot.get_status(), Vec3(*target), max_ticks=180)
            started = loop.time()
            await wait_state(("predicted_arrival", "failed"))
            rec["elapsed_ms"] = round((loop.time() - started) * 1000, 2)
            rec["state"] = controller.state
            rec["failure"] = controller.failure
            await asyncio.sleep(0.30)
            actual = await authoritative()
            await asyncio.sleep(0.30)
            settled = await authoritative()
            rec["actual"] = actual
            rec["settled"] = settled
            rec["final_error"] = round(math.hypot(settled[0] - target[0], settled[2] - target[2]), 6)
            rec["stop_drift"] = round(math.dist((settled[0], settled[2]), (actual[0], actual[2])), 6)
            assert controller.state == "predicted_arrival", rec
            assert rec["final_error"] <= 0.1 and rec["stop_drift"] <= 0.05, rec
            assert rec["stale_move_frames"] == 0, rec
            return rec

        try:
            server.command(f"tp {PLAYER} 0.5 100 0.5")
            await asyncio.sleep(0.6)
            await authoritative()  # Verify querytarget before constructing the platform.
            # Natural terrain at the spawn coordinates can displace a newly
            # teleported player (observed RakNet x=1.3). Clear it before reset.
            # Fresh BDS worlds may also acknowledge tp before chunks are editable.
            async def fill_blocks(command):
                for attempt in range(5):
                    offset = server.command(command)
                    deadline = loop.time() + 5
                    while loop.time() < deadline:
                        lines = server.snapshot(offset)
                        if any("blocks filled" in v.lower() or "block filled" in v.lower()
                               for v in lines):
                            return
                        if any("outside of the world" in v.lower() or "failed" in v.lower()
                               for v in lines):
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise TimeoutError(f"BDS did not answer {command}")
                    if not any("outside of the world" in v.lower() for v in lines):
                        raise AssertionError(f"BDS fill rejected: {lines}")
                    await asyncio.sleep(0.7)
                raise AssertionError(f"BDS chunks never became editable: {lines}")

            await fill_blocks("fill -12 100 -12 12 105 12 air")
            await fill_blocks("fill -12 99 -12 12 99 12 stone")

            await goal_case("straight_8", 0, 8)
            await goal_case("turn_minus_x_4", -4, 0)
            await goal_case("short_0_08", 0, 0.08)
            await goal_case("diagonal", 3, -2)

            p, rec = await begin("retarget")
            controller.start(bot.get_status(), Vec3(p[0], p[1], p[2] + 8))
            while controller.frames < 8:
                await asyncio.sleep(0.025)
            now = bot.get_status().position
            new_target = Vec3(p[0] + 3, now.y, p[2] + 2)
            controller.start(bot.get_status(), new_target)
            await wait_state(("predicted_arrival", "failed"))
            await asyncio.sleep(0.4)
            end = await authoritative()
            rec["state"] = controller.state
            rec["final_error"] = round(math.hypot(end[0] - new_target.x, end[2] - new_target.z), 6)
            assert controller.state == "predicted_arrival" and rec["final_error"] <= 0.1, rec

            p, rec = await begin("stop_during_motion")
            controller.start(bot.get_status(), Vec3(p[0], p[1], p[2] + 8))
            while controller.frames < 10:
                await asyncio.sleep(0.025)
            controller.stop()
            await wait_state(("cancelled", "failed"))
            await asyncio.sleep(0.3)
            before = await authoritative()
            await asyncio.sleep(0.3)
            after = await authoritative()
            rec["state"] = controller.state
            rec["stop_drift"] = round(math.dist((before[0], before[2]), (after[0], after[2])), 6)
            assert controller.state == "cancelled" and rec["stop_drift"] <= 0.05, rec
            assert rec["stale_move_frames"] == 0, rec

            p, rec = await begin("teleport_in_motion")
            controller.start(bot.get_status(), Vec3(p[0], p[1], p[2] + 8))
            while controller.frames < 6:
                await asyncio.sleep(0.025)
            server.command(f"tp {PLAYER} 2.5 100 2.5")
            await wait_state(("failed",))
            rec["failure"] = controller.failure
            assert "position" in controller.failure or "correction" in controller.failure, rec

            p, rec = await begin("forced_server_correction")
            rec["inject_bad_position"] = True
            controller.start(bot.get_status(), Vec3(p[0], p[1], p[2] + 8))
            await wait_state(("failed",), seconds=7)
            rec["failure"] = controller.failure
            rec["prediction_invalidated"] = controller.predicted_position is None
            assert rec.get("injected") and rec["correction_packets"] >= 1, rec
            assert rec["correction_events"] >= 1 and rec["prediction_invalidated"], rec
            count = controller.frames
            await asyncio.sleep(0.2)
            assert controller.frames == count, rec

            p, rec = await begin("disconnect_reconnect")
            controller.start(bot.get_status(), Vec3(p[0], p[1], p[2] + 8))
            while controller.frames < 6:
                await asyncio.sleep(0.025)
            old_session = bot.get_status().session_id
            await bot.disconnect()
            assert controller.state == "failed"
            await bot.reconnect()
            await bot.wait_ready(65)
            rec["old_session"] = old_session
            rec["new_session"] = bot.get_status().session_id
            assert rec["new_session"] != old_session
            count = controller.frames
            await asyncio.sleep(0.4)
            assert controller.frames == count
            await bot.disconnect()
            await asyncio.to_thread(server.wait_player, False)
            results["checks"].append("controller_real_bds_authoritative_cases")
        finally:
            unsubscribe()
            (ROOT / "work" / "controller_trajectory.json").write_text(
                json.dumps(records, indent=2, ensure_ascii=False) + "\n"
            )


if __name__ == "__main__":
    scenarios.exercise = exercise
    sys.exit(testbed.main())
