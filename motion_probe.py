"""Temporary real-BDS movement evidence probe. Not a navigation or physics implementation."""
import asyncio
import json
import math
import sys
from pathlib import Path

import testbed
from testbed import PLAYER, ROOT


async def exercise_motion(server, results, transport):
    from endpilot import (
        BotOptions, BotState, EventKind, Input, PacketDecodedEvent, PositionEvent,
        Runtime, TickEvent, Vec2, Vec3, event_mask,
    )

    endpoint = "http://127.0.0.1:19132" if transport == "nethernet" else "raknet://127.0.0.1:19132"
    traces = {"transport": transport, "phases": [], "packets": [], "positions": [], "errors": []}
    results["motion"] = {"traces": "work/motion_trajectory.json"}
    loop = asyncio.get_running_loop()
    start = loop.time()
    active = None
    current_tick = 0
    current_session = 0

    def stamp():
        return round((loop.time() - start) * 1000, 2)

    def xyz(v):
        return [round(float(v.x), 6), round(float(v.y), 6), round(float(v.z), 6)]

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
                        raise AssertionError(f"Unexpected BDS target: {data}")
                    return xyz(type("P", (), data[0]["position"])())
            if any("unknown command" in v.lower() or "syntax error" in v.lower() for v in lines):
                raise RuntimeError("querytarget unavailable")
            await asyncio.sleep(0.025)
        raise TimeoutError(f"Missing BDS querytarget: {server.snapshot(offset)[-6:]}")

    async def sample(phase):
        p = await authoritative()
        record = {"ms": stamp(), "tick": current_tick, "session": current_session, "bds": p}
        phase["samples"].append(record)
        return p

    def event_callback(source, event):
        nonlocal current_tick, current_session, active
        if isinstance(event, PacketDecodedEvent):
            packet = event.packet
            pid = packet.header.packet_id
            if pid in (16, 19, 161, 322):
                payload = packet.payload
                rec = {"ms": stamp(), "session": source.get_status().session_id,
                       "packet_id": pid}
                for attr in ("pos", "pos_delta", "tick", "reset_position", "on_ground"):
                    if hasattr(payload, attr):
                        v = getattr(payload, attr)
                        rec[attr] = xyz(v) if hasattr(v, "z") else str(v)
                if len(traces["packets"]) < 500:
                    traces["packets"].append(rec)
            return
        if isinstance(event, PositionEvent):
            traces["positions"].append({
                "ms": stamp(), "session": source.get_status().session_id,
                "position": xyz(event.position), "teleport": bool(event.teleport),
                "correction": bool(event.correction),
            })
            return
        if not isinstance(event, TickEvent):
            return
        current_tick = int(event.tick)
        current_session = source.get_status().session_id
        phase = active
        if phase is None or phase["sent"] >= phase["count"]:
            return
        if phase["session"] != current_session:
            traces["errors"].append("stale phase unexpectedly attempted to send on a new session")
            active = None
            return
        n = phase["sent"]
        d = phase["delta"]
        if phase["name"] == "position_disagreement":
            d = (0.45, 0, 0) if n == 0 else (0, 0, 0)
        if phase["name"] == "move_only":
            d = (0, 0, 0)
        previous = phase["pred"]
        next_p = tuple(previous[i] + d[i] for i in range(3))
        phase["pred"] = next_p
        is_jump = phase["name"] == "jump" and n < 14
        flags = ()
        if is_jump and n == 0:
            flags = ("START_JUMPING",)
        if phase["sprint"]:
            flags = ("START_SPRINTING",) if n == 0 else ()
        elif phase.get("stop_sprint") and n == 0:
            flags = ("STOP_SPRINTING",)
        source.set_input(Input(
            position=Vec3(*next_p), delta=Vec3(*d), move=Vec2(*phase["move"]),
            yaw=phase["yaw"], pitch=0, jumping=is_jump, sprinting=phase["sprint"],
            flags=flags,
        ))
        phase["inputs"].append({
            "ms": stamp(), "tick": current_tick, "position": next_p,
            "delta": d, "move": phase["move"], "yaw": phase["yaw"],
            "jump": is_jump, "flags": flags,
        })
        phase["sent"] += 1

    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=endpoint, protocol=2193, name=PLAYER, chunk_radius=16,
            transport=transport,
        ))
        await bot.wait_ready(65)
        assert bot.get_status().state == BotState.ready
        results["checks"].append("movement_bot_ready")
        mask = event_mask(EventKind.tick) | event_mask(EventKind.packet_decoded) | event_mask(EventKind.position)
        unsubscribe = runtime.subscribe(event_callback, events=mask)

        async def reset():
            nonlocal active
            active = None
            server.command(f"tp {PLAYER} 0.5 100 0.5")
            deadline = loop.time() + 9
            while loop.time() < deadline:
                p = await authoritative()
                local = bot.get_status().position
                if abs(p[0] - 0.5) < 0.25 and abs(p[2] - 0.5) < 0.25 and abs(p[1] - 100) < 0.5 and abs(local.y - 100) < 1:
                    await asyncio.sleep(0.2)
                    return await authoritative()
                await asyncio.sleep(0.08)
            raise AssertionError(f"Could not reset BDS to test platform: {p}; local={xyz(bot.get_status().position)}")

        async def run_phase(name, count, delta=(0, 0, 0), move=(0, 0), yaw=0,
                            sprint=False, start=None, stop_sprint=False, sample_gap=0.09):
            nonlocal active
            phase = {
                "name": name, "count": count, "sent": 0, "session": bot.get_status().session_id,
                "delta": delta, "move": move, "yaw": yaw, "sprint": sprint,
                "stop_sprint": stop_sprint, "pred": tuple(start if start is not None else await authoritative()),
                "inputs": [], "samples": [],
            }
            traces["phases"].append(phase)
            phase["initial_bds"] = await sample(phase)
            active = phase
            deadline = loop.time() + max(5, count * 0.12)
            try:
                while phase["sent"] < count and loop.time() < deadline and bot.get_status().state == BotState.ready:
                    await sample(phase)
                    await asyncio.sleep(sample_gap)
                if phase["sent"] != count:
                    traces["errors"].append(f"{name}: only {phase['sent']} of {count} input frames")
            finally:
                active = None
            phase["last_input_bds"] = await sample(phase)
            return phase

        try:
            # Reuse the real vanilla world and server console command/query oracle.
            server.command("fill -24 99 -24 24 99 24 stone")
            await asyncio.sleep(0.4)
            origin = await reset()
            traces["platform_start"] = origin

            first = await run_phase("straight_z", 42, (0, 0, 0.09), (0, 1), start=origin)
            stopped = await run_phase("stop_z", 24, start=first["pred"])
            await asyncio.sleep(0.25)
            stopped["settled_bds"] = await sample(stopped)

            origin = await reset()
            await run_phase("yaw_90_forward", 38, (-0.09, 0, 0), (0, 1), yaw=90, start=origin)

            origin = await reset()
            await run_phase("move_only", 38, (0, 0, 0), (0, 1), start=origin)

            origin = await reset()
            jump = await run_phase("jump", 42, (0, 0, 0), (0, 0), start=origin, sample_gap=0.045)
            jump["apex_y"] = max(p["bds"][1] for p in jump["samples"])
            jump["rise"] = round(jump["apex_y"] - origin[1], 6)

            origin = await reset()
            server.command("fill -2 100 4 2 102 4 stone")
            await asyncio.sleep(0.2)
            wall = await run_phase("solid_wall_z4", 74, (0, 0, 0.09), (0, 1), start=origin)
            wall["wall_plane_z"] = 4
            server.command("fill -2 100 4 2 102 4 air")

            origin = await reset()
            await run_phase("position_disagreement", 15, (0, 0, 0), (0, 0), start=origin)

            origin = await reset()
            interrupted = await run_phase("disconnect_in_flight", 4, (0, 0, 0.09), (0, 1), start=origin)
            traces["pre_disconnect_session"] = bot.get_status().session_id
            active = None
            await bot.disconnect()
            assert bot.get_status().state in (BotState.closing, BotState.closed)
            await bot.reconnect()
            await bot.wait_ready(65)
            traces["post_reconnect_session"] = bot.get_status().session_id
            assert traces["post_reconnect_session"] != traces["pre_disconnect_session"]
            after = {"name": "reconnected_idle", "samples": []}
            traces["phases"].append(after)
            await asyncio.sleep(0.3)
            for _ in range(8):
                await sample(after)
                await asyncio.sleep(0.10)
            results["checks"].append("movement_reconnect_generation_observed")
            await bot.disconnect()
            await asyncio.to_thread(server.wait_player, False)
            results["checks"].append("movement_server_disconnect_confirmed")
        finally:
            unsubscribe()
            (ROOT / "work" / "motion_trajectory.json").write_text(
                json.dumps(traces, indent=2, ensure_ascii=False) + "\n"
            )
            results["motion"].update({
                "phases": [{"name": p["name"], "frames": len(p.get("inputs", [])),
                            "samples": len(p["samples"])} for p in traces["phases"]],
                "corrections": sum(p.get("packet_id") == 161 for p in traces["packets"]),
                "errors": traces["errors"],
            })


if __name__ == "__main__":
    testbed.exercise = exercise_motion
    sys.exit(testbed.main())
