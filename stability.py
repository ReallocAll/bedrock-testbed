"""Existing 600-second two-Bot BDS stability and SIGKILL recovery."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from pathlib import Path

from server import PLAYER, PLAYER_COUNT, Server

async def exercise_stability(server, results, transport):
    """Bounded real-BDS two-Bot soak plus process-kill recovery check."""
    from bedrock_protocol import v2193
    from endpilot import BotOptions, BotState, Runtime

    endpoint = "http://127.0.0.1:19132" if transport == "nethernet" else "raknet://127.0.0.1:19132"
    names = (PLAYER, "EndPilotPeer")
    loop = asyncio.get_running_loop()
    errors = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(str(context.get("exception") or context.get("message"))))

    def resources():
        def process_usage(pid):
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                rss = re.search(r"^VmRSS:\s+(\d+) kB", status, re.M)
                return {"pid": pid, "rss_kb": int(rss.group(1)) if rss else 0,
                        "fds": len(list(Path(f"/proc/{pid}/fd").iterdir()))}
            except OSError:
                return {"pid": pid, "missing": True}
        pgid = server.process.pid if server.process is not None else None
        return {
            "client": process_usage(os.getpid()),
            "server_processes": [process_usage(pid) for pid in Server.group_members(pgid)] if pgid else [],
            "tasks": len(asyncio.all_tasks()),
            "sdk_tasks": sorted(t.get_name() for t in asyncio.all_tasks()
                                if not t.done() and t.get_name().startswith(("bedrock-", "endpilot-"))),
        }

    async def expect_players(count):
        _, lines = await asyncio.to_thread(server.player_online)
        values = [int(match.group(1)) for line in lines if (match := PLAYER_COUNT.search(line))]
        joined = "\n".join(lines).lower()
        if not values or values[-1] != count or (count and not all(n.lower() in joined for n in names)):
            raise AssertionError(f"BDS expected {count} named players, observed: {lines[-8:]}")

    async def await_closed(bots, timeout=120):
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            states = [b.get_status().state for b in bots]
            if all(s in (BotState.failed, BotState.closed) for s in states):
                assert all(s == BotState.failed for s in states), f"Unexpected SIGKILL session states: {states}"
                return [s.name for s in states]
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Sessions did not close after BDS SIGKILL: {states}")

    try:
        results["baseline_resources"] = resources()
        async with Runtime() as runtime:
            bots = []
            for name in names:
                bot = runtime.add_bot(BotOptions(server_url=endpoint,
                            transport=transport, protocol=2193, name=name, chunk_radius=8))
                bots.append(bot)
                await bot.wait_ready(65)
                if transport == "raknet" and name != names[-1]:
                    await asyncio.sleep(8)  # BDS rejects simultaneous new connections from one IP.
            await expect_players(2)
            original_ids = [bot.get_status().session_id for bot in bots]
            old_slot = bots[0].player_slot(0)
            samples = []
            started = loop.time()
            for index in range(21):
                assert all(bot.get_status().state == BotState.ready for bot in bots), "Bot exited during soak"
                await expect_players(2)
                record = resources()
                record["elapsed_s"] = round(loop.time() - started, 2)
                record["states"] = [bot.get_status().state.name for bot in bots]
                samples.append(record)
                results["soak_samples"] = samples
                print("[stability] " + json.dumps({"transport": transport, **record}), flush=True)
                if index < 20:
                    await asyncio.sleep(max(0, started + 30 * (index + 1) - loop.time()))
            duration = loop.time() - started
            assert duration >= 600, f"Soak unexpectedly shorter than 600s: {duration:.2f}"
            results["soak_seconds"] = round(duration, 2)
            results["checks"].append("two_bots_online_for_600_seconds")

            proc = server.process
            assert proc is not None and proc.poll() is None
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=15)
            if server.thread:
                server.thread.join(timeout=5)
            if server.stream:
                server.stream.close()
            if Server.group_members(proc.pid):
                raise AssertionError("BDS process group survived SIGKILL")
            server.process = None
            results["fault"] = {"signal": "SIGKILL", "returncode": proc.returncode}
            results["fault"]["closed_states"] = await await_closed(bots)
            results["fault"]["resources"] = resources()
            assert [bot.get_status().session_id for bot in bots] == original_ids
            for bot in bots:
                try:
                    bot.send_packet(v2193.RequestChunkRadiusPacket(chunk_radius=8, max_chunk_radius=8))
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Failed Session accepted a public packet")
            results["checks"].append("abrupt_server_exit_sessions_failed")

            server.start()
            await asyncio.to_thread(server.wait_ready)
            await asyncio.sleep(3)
            assert all(bot.get_status().state != BotState.ready for bot in bots), "Undeclared automatic reconnect"
            for index, bot in enumerate(bots):
                await bot.reconnect()
                await bot.wait_ready(65)
                if transport == "raknet" and index == 0:
                    await asyncio.sleep(8)
            await expect_players(2)
            assert all(bot.get_status().session_id > old for bot, old in zip(bots, original_ids))
            try:
                bots[0].get_item(old_slot)
            except RuntimeError as error:
                assert "stale" in str(error).lower(), str(error)
            else:
                raise AssertionError("Old inventory reference survived reconnect")
            results["recovered_resources"] = resources()
            results["checks"].append("explicit_reconnect_and_stale_ref_rejection")
            await asyncio.gather(*(bot.disconnect() for bot in bots))
            await asyncio.to_thread(server.wait_player, False)
        results["post_runtime_resources"] = resources()
        pending = [t.get_name() for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()
                   and t.get_name().startswith(("bedrock-", "endpilot-"))]
        assert not pending, f"SDK tasks remained after Runtime shutdown: {pending}"
        assert not errors, f"Asyncio loop exception handler observed: {errors}"
        results["checks"].append("no_sdk_tasks_after_runtime_close")
    finally:
        results["loop_exceptions"] = errors
        loop.set_exception_handler(old_handler)
