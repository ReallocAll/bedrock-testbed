"""One clean Endstone/BDS lifecycle and one real EndPilot public-API smoke test.

Ubuntu Linux only. Endstone and EndPilot are installed in separate venvs by CI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from importlib.metadata import distribution
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAYER = "EndPilotTest"
VERSION_LINE = "This server is running Endstone version: 0.11.14 (Minecraft: 26.52)"
PROPERTIES = {
    "transport": "raknet",
    "enable-lan-visibility": "false",
    "online-mode": "false",
    "allow-list": "false",
    "server-port": "19132",
    "server-portv6": "19133",
    "gamemode": "creative",
    "difficulty": "peaceful",
    "allow-cheats": "true",
    "default-player-permission-level": "operator",
    "player-idle-timeout": "0",
    "view-distance": "12",
    "tick-distance": "4",
    "client-side-chunk-generation-enabled": "false",
}
PLAYER_COUNT = re.compile(r"there are\s+(\d+)\s*(?:/|of a max of)\s*\d+\s+players?\s+online", re.I)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Server:
    def __init__(self, python: Path, folder: Path, logfile: Path):
        self.folder = folder
        self.logfile = logfile
        self.process = None
        self.lines = []
        self.lock = threading.Lock()
        self.thread = None
        self.stream = None
        self.python = python

    def start(self):
        self.lines = []
        self.stream = self.logfile.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(self.python), "-m", "endstone", "--yes", "--server-folder", str(self.folder)],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        assert self.process and self.process.stdout and self.stream
        for raw in self.process.stdout:
            line = ANSI.sub("", raw).strip()
            with self.lock:
                self.lines.append(line)
            self.stream.write(raw)
            self.stream.flush()
            print("[bds] " + line, flush=True)

    def snapshot(self, start=0):
        with self.lock:
            return self.lines[start:].copy()

    def wait_ready(self, timeout=420):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.snapshot()
            if any("server started" in line.lower() for line in lines):
                if VERSION_LINE not in "\n".join(lines):
                    raise RuntimeError("BDS Ready but Endstone/version line does not match 0.11.14/26.52")
                version_path = self.folder / "version.txt"
                if not version_path.is_file() or version_path.read_text().strip() != "26.52":
                    raise RuntimeError("BDS version.txt is missing or does not equal 26.52")
                return
            if self.process.poll() is not None:
                raise RuntimeError(f"BDS exited before Ready (code={self.process.returncode})")
            time.sleep(0.25)
        raise TimeoutError("BDS did not report Ready and expected versions")

    def command(self, value):
        if self.process is None or self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("BDS is not running")
        with self.lock:
            offset = len(self.lines)
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()
        return offset

    def player_online(self):
        """Require a fresh BDS 'list' response, not only a client-side Ready."""
        offset = self.command("list")
        deadline = time.monotonic() + 6
        count = None
        observed_at = None
        while time.monotonic() < deadline:
            lines = self.snapshot(offset)
            for line in lines:
                match = PLAYER_COUNT.search(line)
                if match:
                    count = int(match.group(1))
                    observed_at = observed_at or time.monotonic()
            if count is not None and (PLAYER.lower() in "\n".join(lines).lower() or count == 0):
                return count > 0 and PLAYER.lower() in "\n".join(lines).lower(), lines
            if count is not None and time.monotonic() - observed_at > 1:
                return False, lines
            if self.process.poll() is not None:
                raise RuntimeError("BDS exited during player list check")
            time.sleep(0.1)
        raise TimeoutError("BDS did not answer 'list' with a recognizable player count")

    def wait_player(self, expected, timeout=25):
        deadline = time.monotonic() + timeout
        last = []
        while time.monotonic() < deadline:
            online, last = self.player_online()
            if online == expected:
                return last
            time.sleep(0.5)
        raise AssertionError(f"BDS player list did not converge to online={expected}: {last[-8:]}")

    @staticmethod
    def group_members(pgid):
        """Linux /proc group members, excluding exited zombies."""
        active = []
        for stat_file in Path("/proc").glob("[0-9]*/stat"):
            try:
                stat = stat_file.read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                    active.append(int(stat_file.parent.name))
            except (ValueError, IndexError, OSError):
                continue
        return active

    def stop(self):
        if self.process is None:
            return
        proc = self.process
        graceful = False
        try:
            if proc.poll() is None:
                self.command("stop")
            proc.wait(timeout=45)
            graceful = proc.returncode == 0
        except (BrokenPipeError, RuntimeError, subprocess.TimeoutExpired):
            pass
        finally:
            members = self.group_members(proc.pid)
            if members:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(0.5)
                members = self.group_members(proc.pid)
                if members:
                    os.killpg(proc.pid, signal.SIGKILL)
            if self.thread:
                self.thread.join(timeout=5)
            if self.stream:
                self.stream.close()
            self.process = None
        leftover = self.group_members(proc.pid)
        if not graceful or leftover:
            raise RuntimeError(
                f"BDS shutdown was not clean: returncode={proc.returncode}, "
                f"graceful={graceful}, residual_pids={leftover}"
            )


def configure(path):
    if not path.is_file():
        raise FileNotFoundError(f"Endstone did not create {path}")
    updated = set()
    lines = []
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in PROPERTIES:
                line = key + "=" + PROPERTIES[key]
                updated.add(key)
        lines.append(line)
    lines.extend(k + "=" + v for k, v in PROPERTIES.items() if k not in updated)
    path.write_text("\n".join(lines) + "\n")


async def exercise(server, results):
    from bedrock_protocol import v2193
    from endpilot import (
        BotOptions, BotState, EventKind, Input, ItemRequestOutcome, PacketDecodedEvent,
        Runtime, SlotState, TickEvent, Vec2, Vec3, event_mask,
    )

    radius_events = []
    observer_radii = []
    observer_errors = []

    def observe_radius(_bot, event):
        if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id == 70:
            observer_radii.append(int(event.packet.payload.chunk_radius))

    def broken_observer(_bot, event):
        if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id == 70:
            raise ValueError("independent observer sentinel")

    def callback(_bot, event):
        if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id == 70:
            radius_events.append(int(event.packet.payload.chunk_radius))

    async with Runtime(callback, events=event_mask(EventKind.packet_decoded)) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=2193, name=PLAYER, chunk_radius=16,
        ))
        await bot.wait_ready(65)
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: observer_errors.append(context))
        unsubscribe_good = runtime.subscribe(observe_radius, events=event_mask(EventKind.packet_decoded))
        unsubscribe_bad = runtime.subscribe(broken_observer, events=event_mask(EventKind.packet_decoded))
        status = bot.get_status()
        assert status.state == BotState.ready
        assert status.actor_runtime_id > 0
        assert all(math.isfinite(v) for v in (status.position.x, status.position.y, status.position.z))
        results["world_state"] = {"actor_runtime_id": status.actor_runtime_id, "dimension": status.dimension,
                                  "position": [status.position.x, status.position.y, status.position.z]}
        results["bds_online_list"] = await asyncio.to_thread(server.wait_player, True)
        results["checks"].append("raknet_login_server_confirmed")

        async def request_and_observe(requested):
            before = len(radius_events)
            bot.send_packet(v2193.RequestChunkRadiusPacket(chunk_radius=requested, max_chunk_radius=requested))
            deadline = asyncio.get_running_loop().time() + 12
            while asyncio.get_running_loop().time() < deadline:
                new = radius_events[before:]
                if new and bot.get_status().chunk_radius == new[-1]:
                    approved = new[-1]
                    assert 1 <= approved <= 12, f"BDS granted unexpected chunk radius: {approved}"
                    return {"requested": requested, "approved": approved, "new_responses": len(new)}
                if bot.get_status().state != BotState.ready:
                    raise AssertionError("Session left Ready while waiting for chunk-radius response")
                await asyncio.sleep(0.1)
            raise TimeoutError(f"No fresh server ChunkRadiusUpdated response to request {requested}")

        initial = bot.get_status().chunk_radius
        first = await request_and_observe(6)
        assert observer_radii[-1:] == [first["approved"]], "Independent subscriber missed server response"
        assert observer_errors and all(
            entry.get("message") == "EndPilot event observer failed"
            and isinstance(entry.get("exception"), ValueError) for entry in observer_errors
        ), f"Unexpected observer error reports: {observer_errors}"
        failures_before = len(observer_errors)
        unsubscribe_bad()
        unsubscribe_bad()
        assert first["approved"] != initial, f"radius did not change from initial {initial}"
        results["initial_radius"] = initial
        results["radius_request"] = first
        results["checks"].append("typed_packet_server_response")

        try:
            bot.send_packet(object())
        except Exception as error:
            results["rejected_packet"] = type(error).__name__
        else:
            raise AssertionError("Invalid typed packet was admitted")
        assert bot.get_status().state == BotState.ready, "Invalid public packet poisoned the session"
        second = await request_and_observe(8)
        assert second["approved"] != first["approved"], "Session did not process valid packet after rejection"
        assert observer_radii[-1:] == [second["approved"]], "Observer stopped receiving after another observer failed"
        assert len(observer_errors) == failures_before, "Unsubscribed observer still failed"
        unsubscribe_good()
        loop.set_exception_handler(previous_handler)
        results["observer_failures_isolated"] = failures_before
        results["checks"].append("independent_observers_fault_isolated")
        results["radius_after_rejection"] = second
        results["checks"].append("invalid_packet_isolation")

        # A separate, server-authoritative oracle: BDS querytarget reports the
        # entity's actual coordinates, unlike the client's predicted position.
        server.command(f"tp {PLAYER} 0.5 240 0.5")
        deadline = loop.time() + 12
        while loop.time() < deadline and bot.get_status().position.y > 255:
            await asyncio.sleep(0.1)
        if bot.get_status().position.y > 255:
            raise AssertionError("Client never observed BDS teleport")

        async def authoritative_position():
            offset = server.command(f"querytarget {PLAYER}")
            deadline = loop.time() + 5
            while loop.time() < deadline:
                lines = server.snapshot(offset)
                fields = [line.partition("]:")[2].strip() for line in lines]
                start = next((i for i, value in enumerate(fields) if value.startswith("Target data:")), None)
                if start is not None:
                    stop = next((i for i in range(start + 1, len(fields)) if fields[i] == "]"), None)
                    if stop is not None:
                        document = fields[start].split("Target data:", 1)[1] + " ".join(
                            fields[start + 1:stop + 1]
                        )
                        targets = json.loads(document)
                        if len(targets) != 1 or targets[0]["dimension"] != 0:
                            raise AssertionError(f"Unexpected BDS target response: {targets}")
                        return targets[0]["position"]
                if any("unknown command" in line.lower() or "syntax error" in line.lower() for line in lines):
                    raise AssertionError(f"BDS querytarget unavailable: {lines}")
                await asyncio.sleep(0.1)
            raise TimeoutError(f"No complete BDS querytarget response: {server.snapshot(offset)}")

        origin = await authoritative_position()
        results["start_server_position"] = origin
        assert abs(origin["x"] - 0.5) < 1, f"BDS did not teleport to the initial x: {origin}"
        results["checks"].append("server_position_origin_confirmed")

        # Newly generated worlds may not simulate the target chunk immediately.
        # Prove that server player physics advances before measuring inputs.
        physics_deadline = loop.time() + 12
        while loop.time() < physics_deadline:
            falling = await authoritative_position()
            if falling["y"] < origin["y"] - 0.25:
                origin = falling
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("BDS player physics did not advance after teleport; movement oracle unavailable")
        results["start_server_position"] = origin

        # Independent third-party behavior using only public event and input APIs.
        submitted = 0

        def walk(_bot, event):
            nonlocal submitted
            if not isinstance(event, TickEvent) or submitted >= 34:
                return
            position = _bot.get_status().position
            _bot.set_input(Input(
                position=Vec3(position.x + 0.09, position.y, position.z),
                delta=Vec3(0.09, 0, 0),
                move=Vec2(1, 0),
                sprinting=True,
                flags=("START_SPRINTING",) if submitted == 0 else (),
            ))
            submitted += 1

        stop_walking = runtime.subscribe(walk, events=event_mask(EventKind.tick))
        deadline = loop.time() + 6
        while submitted < 34 and loop.time() < deadline:
            await asyncio.sleep(0.1)
        stop_walking()
        results["input_frames"] = submitted
        assert submitted == 34 and bot.get_status().state == BotState.ready
        destination = await authoritative_position()
        results["end_server_position"] = destination
        displacement = math.hypot(destination["x"] - origin["x"], destination["z"] - origin["z"])
        results["server_horizontal_displacement"] = displacement
        assert displacement >= 0.5, f"BDS did not accept significant horizontal movement: {origin} -> {destination}"
        results["checks"].append("input_movement_server_confirmed")

        # Exercise the public jump input against a stable server-side platform.
        # Player coordinates are sampled from BDS, never from local prediction.
        server.command("setblock 0 220 0 stone")
        server.command(f"tp {PLAYER} 0.5 221 0.5")
        deadline = loop.time() + 12
        while loop.time() < deadline and abs(bot.get_status().position.y - 221) > 2:
            await asyncio.sleep(0.1)
        if abs(bot.get_status().position.y - 221) > 2:
            raise AssertionError("Jump setup teleport was not observed")

        await asyncio.sleep(0.4)
        ground = await authoritative_position()
        await asyncio.sleep(0.3)
        stable = await authoritative_position()
        assert abs(stable["y"] - ground["y"]) < 0.1, (
            f"No stable BDS platform for jump test: {ground} -> {stable}"
        )
        jump_sent = 0

        def jump(source, event):
            nonlocal jump_sent
            if source is not bot or not isinstance(event, TickEvent) or jump_sent >= 14:
                return
            position = source.get_status().position
            source.set_input(Input(
                position=position, jumping=True,
                flags=("START_JUMPING",) if jump_sent == 0 else (),
            ))
            jump_sent += 1

        stop_jumping = runtime.subscribe(jump, events=event_mask(EventKind.tick))
        jump_heights = []
        try:
            deadline = loop.time() + 3
            while loop.time() < deadline and jump_sent < 14:
                jump_heights.append((await authoritative_position())["y"])
                await asyncio.sleep(0.04)
        finally:
            stop_jumping()
        assert jump_sent == 14, f"Only sent {jump_sent} of 14 jump frames"
        jump_rise = max(jump_heights, default=stable["y"]) - stable["y"]
        results["jump"] = {
            "ground": stable["y"], "sampled_heights": jump_heights,
            "rise": jump_rise, "frames": jump_sent,
        }
        assert jump_rise > 0.25, f"BDS did not confirm vertical jump: {results['jump']}"
        results["checks"].append("input_jump_server_confirmed")

        # Real inventory: let BDS grant the item, then request one native
        # server-side inventory transfer using only the public Bot API.
        server.command(f"gamemode survival {PLAYER}")
        item_id = "minecraft:emerald"
        deadline = loop.time() + 10
        initial_items = bot.count_player_item(item_id)
        while not initial_items.complete and loop.time() < deadline:
            await asyncio.sleep(0.1)
            initial_items = bot.count_player_item(item_id)
        assert initial_items.complete and initial_items.observed_count == 0, (
            f"Fresh BDS inventory is not fully known and empty: {initial_items}"
        )
        server.command(f"give {PLAYER} emerald 4")
        deadline = loop.time() + 10
        granted = bot.count_player_item(item_id)
        while loop.time() < deadline and (
            not granted.complete or granted.observed_count != 4
        ):
            await asyncio.sleep(0.1)
            granted = bot.count_player_item(item_id)
        assert granted.complete and granted.observed_count == 4, (
            f"BDS item grant was not observed via Item Registry: {granted}"
        )
        results["checks"].append("item_registry_inventory_server_confirmed")

        # The server must acknowledge opening a player's inventory UI before
        # processing main-inventory item requests (Interact / ContainerOpen).
        opened_inventories = []

        def observe_inventory_open(source, event):
            if (
                source is bot and isinstance(event, PacketDecodedEvent)
                and event.packet.header.packet_id == 46
            ):
                opened_inventories.append(event.packet.payload)

        stop_inventory_observer = runtime.subscribe(
            observe_inventory_open, events=event_mask(EventKind.packet_decoded)
        )
        try:
            bot.send_packet(v2193.InteractPacket(
                action=v2193.InteractPacket.Action.OPEN_INVENTORY,
                target_id=bot.get_status().actor_runtime_id,
                pos=None,
            ))
            deadline = loop.time() + 8
            while not opened_inventories and loop.time() < deadline:
                await asyncio.sleep(0.1)
            assert opened_inventories, "BDS did not acknowledge opening player inventory"
        finally:
            stop_inventory_observer()
        results["checks"].append("player_inventory_screen_server_confirmed")

        slots = bot.get_player_inventory()
        source = next(
            (slot for slot in slots if slot.identifier == item_id and slot.count == 4), None
        )
        target = next(
            (slot for slot in slots if slot.state == SlotState.empty and slot.slot.index >= 9), None
        )
        assert source is not None and target is not None, "No known item source and empty destination"
        operation = bot.begin_move_item(source.slot, target.slot, 1)
        settled = await operation.wait(12)
        assert settled.outcome == ItemRequestOutcome.completed and settled.server_result == 0, (
            f"BDS rejected inventory move: {settled}"
        )
        moved = bot.get_item(target.slot)
        remaining = bot.get_item(source.slot)
        assert (
            moved.identifier == item_id and moved.count == 1
            and remaining.identifier == item_id and remaining.count == 3
            and bot.count_player_item(item_id).observed_count == 4
        ), f"Server-authoritative inventory transfer did not conserve items: {remaining} -> {moved}"
        results["inventory"] = {
            "item": item_id, "granted": 4, "moved": 1,
            "server_result": int(settled.server_result),
        }
        results["checks"].append("item_move_transaction_server_confirmed")

        # Villager integration: BDS owns the farmer, generated offers and
        # trade window. Only use the Bot's public entity/window APIs.
        screen = opened_inventories[-1]
        bot.send_packet(v2193.ContainerClosePacket(
            container_id=screen.container_id,
            container_type=screen.type,
            server_initiated_close=False,
        ))
        server.command("fill -2 220 -2 6 220 3 stone")
        server.command("setblock 4 221 0 composter")
        server.command(f"tp {PLAYER} 0.5 221 0.5")
        server.command("summon villager 2.5 221 0.5 0 0 minecraft:spawn_farmer")
        deadline = loop.time() + 12
        target = None
        while loop.time() < deadline:
            villagers = [
                actor for actor in bot.get_observed_entities()
                if actor.type in ("minecraft:villager", "minecraft:villager_v2")
                and math.dist(
                    (actor.position.x, actor.position.y, actor.position.z),
                    (2.5, 221, 0.5),
                ) < 5
            ]
            if len(villagers) == 1 and abs(bot.get_status().position.y - 221) < 3:
                target = villagers[0]
                break
            await asyncio.sleep(0.1)
        assert target is not None, f"BDS farmer was not visible: {bot.get_observed_entities()}"
        results["villager"] = {
            "type": target.type, "unique_id": target.ref.unique_id,
            "runtime_id": target.ref.runtime_id,
        }
        current = bot.get_status()
        dx = target.position.x - current.position.x
        dy = target.position.y + 1 - current.position.y
        dz = target.position.z - current.position.z
        bot.set_look(-math.degrees(math.atan2(dy, math.hypot(dx, dz))), math.degrees(math.atan2(-dx, dz)))
        await asyncio.sleep(0.3)
        bot.interact_actor(target.ref, Vec3(0, 1, 0))
        deadline = loop.time() + 8
        while bot.get_trade_window() is None and loop.time() < deadline:
            await asyncio.sleep(0.1)
        window = bot.get_trade_window()
        assert window is not None, "BDS farmer did not open a trade window"
        deadline = loop.time() + 8
        offers = bot.get_trade_offers(window.ref)
        while (offers is None or not offers.offers) and loop.time() < deadline:
            await asyncio.sleep(0.1)
            offers = bot.get_trade_offers(window.ref)
        assert offers is not None and offers.offers, "BDS farmer gave no trade offers"
        results["villager"]["offers"] = [
            {
                "supported": item.supported,
                "input": None if item.input is None else item.input.identifier,
                "input_count": None if item.input is None else item.input.count,
                "output": None if item.output is None else item.output.identifier,
                "reason": item.unsupported_reason,
            } for item in offers.offers
        ]
        results["checks"].append("villager_trade_window_server_confirmed")

        # A single real Trade2 exchange: use a server-generated quote, and
        # let BDS supply the ingredient. Every item request requires status 0.
        quote = next((
            offer for offer in offers.offers
            if offer.supported and offer.ref is not None
            and offer.input is not None and offer.second_input is None
            and re.fullmatch(r"minecraft:[a-z0-9_]+", offer.input.identifier)
            and offer.input.count < 64 and offer.output is not None
            and offer.output.identifier == "minecraft:emerald"
            and offer.uses < offer.max_uses
        ), None)
        assert quote is not None, f"No server-offered, single-ingredient trade: {results['villager']}"
        ingredient = quote.input
        output = quote.output
        amount = ingredient.count + 1
        player_source_count = bot.count_player_item(ingredient.identifier).observed_count
        player_output_count = bot.count_player_item(output.identifier).observed_count
        server.command(f"give {PLAYER} {ingredient.identifier} {amount}")
        deadline = loop.time() + 10
        source = None
        while loop.time() < deadline:
            source = next((
                slot for slot in bot.get_player_inventory()
                if slot.identifier == ingredient.identifier and slot.count >= amount
            ), None)
            if source is not None:
                break
            await asyncio.sleep(0.1)
        assert source is not None, "BDS-granted trade ingredient was not observed"
        input_slot = bot.trade_input(window.ref, 0)
        deadline = loop.time() + 8
        while bot.get_item(input_slot).state == SlotState.unknown and loop.time() < deadline:
            await asyncio.sleep(0.1)
        assert bot.get_item(input_slot).state == SlotState.empty, "BDS trade input not known empty"

        async def confirm_trade_op(operation, step):
            result = await operation.wait(12)
            assert result.outcome == ItemRequestOutcome.completed and result.server_result == 0, (
                f"BDS rejected {step}: {result}"
            )
            return result.server_result

        stage_status = await confirm_trade_op(
            bot.begin_move_item(source.slot, input_slot, amount), "stage ingredient"
        )
        assert bot.get_item(input_slot).identifier == ingredient.identifier
        assert bot.get_item(input_slot).count == amount, "Staged ingredient amount differs"
        fresh = next((
            candidate for candidate in bot.get_trade_offers(window.ref).offers
            if candidate.ref is not None and candidate.ref.recipe_net_id == quote.ref.recipe_net_id
        ), None)
        assert fresh is not None and fresh.supported, "Trade quote changed during staging"
        destination = next((
            slot.slot for slot in bot.get_player_inventory()
            if slot.slot.index >= 9 and slot.state == SlotState.empty
        ), None)
        assert destination is not None, "No known empty output inventory slot"
        trade_status = await confirm_trade_op(bot.begin_trade(fresh.ref, destination), "trade")
        assert bot.get_item(destination).identifier == output.identifier
        assert bot.get_item(destination).count == output.count, "No server-confirmed trade output"
        assert bot.get_item(input_slot).count == 1, "Trade did not leave staged input residual"
        return_status = await confirm_trade_op(
            bot.begin_move_item(input_slot, source.slot, 1), "return residual"
        )
        assert bot.get_item(input_slot).state == SlotState.empty, "Residual was not returned"
        assert bot.count_player_item(ingredient.identifier).observed_count == (
            player_source_count + amount - ingredient.count
        ), "Input conservation failed"
        assert bot.count_player_item(output.identifier).observed_count == (
            player_output_count + output.count
        ), "Output conservation failed"
        results["villager"]["trade"] = {
            "input": ingredient.identifier, "price": ingredient.count,
            "output": output.identifier, "output_count": output.count,
            "stage_status": stage_status, "trade_status": trade_status,
            "return_status": return_status,
        }
        bot.close_container(window.ref)
        deadline = loop.time() + 8
        while bot.get_trade_window() is not None and loop.time() < deadline:
            await asyncio.sleep(0.1)
        assert bot.get_trade_window() is None, "BDS trade window was not closed"
        results["checks"].append("villager_trade_exchange_server_confirmed")

        # Two real RakNet players sharing one Runtime: closing one Session must
        # not cancel the other Bot or corrupt its inbound packet state.
        async def expect_players(count, *names):
            recent = []
            for _ in range(25):
                _, recent = await asyncio.to_thread(server.player_online)
                counts = [
                    int(match.group(1))
                    for line in recent if (match := PLAYER_COUNT.search(line))
                ]
                names_text = "\\n".join(recent).lower()
                if counts and counts[-1] == count and all(name.lower() in names_text for name in names):
                    return recent
                await asyncio.sleep(0.2)
            raise AssertionError(f"Expected BDS count={count} with {names}: {recent}")

        other_name = "EndPilotPeer"
        other = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=2193, name=other_name, chunk_radius=16,
        ))
        await other.wait_ready(65)
        results["two_bot_list"] = await expect_players(2, PLAYER, other_name)
        results["checks"].append("two_bots_online_server_confirmed")

        await bot.disconnect()
        results["single_survivor_list"] = await expect_players(1, other_name)
        assert other.get_status().state == BotState.ready, "Peer disconnect stopped the surviving Bot"
        observed_other = []

        def observe_other(source, event):
            if source is other and isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id == 70:
                observed_other.append(int(event.packet.payload.chunk_radius))

        unsubscribe_other = runtime.subscribe(observe_other, events=event_mask(EventKind.packet_decoded))
        other.send_packet(v2193.RequestChunkRadiusPacket(chunk_radius=7, max_chunk_radius=7))
        deadline = loop.time() + 12
        while loop.time() < deadline and (
            not observed_other or other.get_status().chunk_radius != observed_other[-1]
        ):
            await asyncio.sleep(0.1)
        unsubscribe_other()
        assert observed_other and observed_other[-1] == 7, f"Surviving Bot missed server reply: {observed_other}"
        assert other.get_status().state == BotState.ready
        results["survivor_radius"] = observed_other[-1]
        results["checks"].append("other_bot_survives_disconnect_and_packets")

        await bot.reconnect()
        await bot.wait_ready(65)
        results["reconnected_two_bot_list"] = await expect_players(2, PLAYER, other_name)
        assert other.get_status().state == BotState.ready
        results["checks"].append("original_bot_reconnect_server_confirmed")

        await bot.disconnect()
        await other.disconnect()
        results["bds_offline_list"] = await expect_players(0)
        results["checks"].append("client_disconnect_server_confirmed")

    pending = [
        task.get_name() for task in asyncio.all_tasks()
        if not task.done() and task is not asyncio.current_task()
        and task.get_name().startswith(("bedrock-", "endpilot-"))
    ]
    assert not pending, f"EndPilot tasks survived Runtime shutdown: {pending}"
    results["checks"].append("runtime_tasks_closed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True, help="Full SHA expected from the EndPilot VCS install")
    args = parser.parse_args()
    # Preserve the venv executable path; resolve() follows its symlink to base Python.
    python = args.server_python.absolute()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    folder = work / "server"
    logfile = work / "bds.log"
    results = {
        "status": "FAIL",
        "endstone": "0.11.14",
        "bds": "1.26.52",
        "protocol": 2193,
        "python": sys.version.split()[0],
        "endpilot_commit": args.endpilot_commit,
        "installed_endpilot_commit": None,
        "checks": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    server = None
    try:
        direct_url = distribution("endpilot").read_text("direct_url.json")
        if direct_url is None:
            raise RuntimeError("EndPilot has no PEP 610 VCS installation metadata")
        vcs = json.loads(direct_url).get("vcs_info", {})
        installed_commit = vcs.get("commit_id")
        results["installed_endpilot_commit"] = installed_commit
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("--endpilot-commit must be a full lowercase 40-character SHA")
        if installed_commit != args.endpilot_commit:
            raise RuntimeError(
                f"Installed EndPilot commit {installed_commit!r} does not match requested {args.endpilot_commit}"
            )
        results["checks"].append("endpilot_installed_commit_verified")
        server = Server(python, folder, logfile)
        server.start()
        server.wait_ready()
        results["checks"].append("official_bds_bootstrap_version_verified")
        server.stop()
        server = None

        configure(folder / "server.properties")
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(python, folder, logfile)
        server.start()
        server.wait_ready()
        results["checks"].append("clean_configured_bds_ready")
        asyncio.run(exercise(server, results))
        server.stop()
        server = None
        results["checks"].append("bds_graceful_stop_no_processes")
        results["status"] = "PASS"
    except BaseException as error:
        results["error"] = f"{type(error).__name__}: {error}"
        print(results["error"], file=sys.stderr, flush=True)
    finally:
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                results["cleanup_error"] = f"{type(error).__name__}: {error}"
                results["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(results, ensure_ascii=False), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
