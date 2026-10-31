"""Independent real-BDS packet/data probe. No EndPilot private internals."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from testbed import ROOT, PLAYER, Server, configure
from endpilot import BotOptions, EventKind, PacketDecodedEvent, Runtime, event_mask
from endpilot.world import WorldQuery

def sample(raw):
    data = bytes(raw)
    return {
        "length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "head_hex": data[:192].hex(),
        "base64": base64.b64encode(data[:65536]).decode(),
        "truncated": len(data) > 65536,
    }

async def observe(server, results, transport):
    endpoint = "http://127.0.0.1:19132" if transport == "nethernet" else "raknet://127.0.0.1:19132"
    counts = Counter()
    chunks, subchunks, updates = [], [], []
    errors = []
    start_game = {}
    def callback(bot, event):
        if not isinstance(event, PacketDecodedEvent):
            return
        envelope = event.packet
        pid = envelope.header.packet_id
        counts[pid] += 1
        p = envelope.payload
        try:
            if pid == 11:
                start_game.update({
                    "network_ids_are_hashes": bool(p.block_network_ids_are_hashes),
                    "block_properties_count": len(p.block_properties),
                    "server_version": p.server_version,
                })
            elif pid == 58 and (len(chunks) < 100 or (p.pos.x, p.pos.z) in ((0, 0), (1, 0), (-1, 0))):
                item = {
                    "x": p.pos.x, "z": p.pos.z, "dim": int(p.dimension_id),
                    "sub_chunks_count": int(p.sub_chunks_count),
                    "request_limit": p.client_request_sub_chunk_limit,
                    "cache_enabled": bool(p.cache_enabled),
                    "blob_count": len(p.cache_metadata),
                    "size": len(p.serialized_chunk),
                }
                if (p.pos.x, p.pos.z) in ((0, 0), (1, 0), (-1, 0)) and not any(
                    c.get("raw") is not None and (c["x"], c["z"]) == (p.pos.x, p.pos.z) for c in chunks
                ):
                    item["raw"] = sample(p.serialized_chunk)
                chunks.append(item)
            elif pid == 174 and len(subchunks) < 70:
                item = {
                    "center": [p.center_pos.x, p.center_pos.y, p.center_pos.z],
                    "dimension": int(p.dimension_type), "cache_enabled": bool(p.cache_enabled),
                    "entries": [],
                }
                for entry in p.sub_chunk_data:
                    row = {
                        "offset": [entry.sub_chunk_pos_offset.x, entry.sub_chunk_pos_offset.y, entry.sub_chunk_pos_offset.z],
                        "result": int(entry.result),
                        "blob_id": entry.blob_id,
                    }
                    if entry.serialized_sub_chunk is not None:
                        row["raw"] = sample(entry.serialized_sub_chunk)
                    item["entries"].append(row)
                subchunks.append(item)
            elif pid in (21, 110):
                if len(updates) < 150 or (p.pos.x in (-1, 0, 1, 16) and p.pos.y in (220, 222)):
                    updates.append({
                        "packet": pid, "pos": [p.pos.x, p.pos.y, p.pos.z],
                        "runtime_id": int(p.runtime_id), "layer": int(p.layer),
                    })
            elif pid == 172:
                if len(updates) < 150:
                    updates.append({
                        "packet": pid, "pos": [
                            p.sub_chunk_block_position.x, p.sub_chunk_block_position.y, p.sub_chunk_block_position.z,
                        ],
                        "standard_count": len(p.blocks_changed.standards),
                        "extra_count": len(p.blocks_changed.extras),
                    })
        except Exception as e:
            errors.append(f"packet {pid}: {type(e).__name__}: {e}")
    async with Runtime(callback, events=event_mask(EventKind.packet_decoded)) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=endpoint, transport=transport, protocol=2193, name=PLAYER,
            chunk_radius=8, client_cache=False, validate_all_packets=True,
        ))
        world = WorldQuery(bot)
        decode_samples = []
        def observe_world(bot, event):
            start_ns = time.perf_counter_ns()
            world.observe(bot, event)
            if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id == 174:
                raw_bytes = sum(
                    len(e.serialized_sub_chunk or b"") for e in event.packet.payload.sub_chunk_data
                )
                decode_samples.append({
                    "elapsed_ns": time.perf_counter_ns() - start_ns,
                    "serialized_bytes": raw_bytes,
                    "entry_count": len(event.packet.payload.sub_chunk_data),
                })
        unsubscribe_world = runtime.subscribe(
            observe_world,
            events=event_mask(EventKind.packet_decoded) | event_mask(EventKind.dimension) | event_mask(EventKind.state),
        )
        await bot.wait_ready(70)
        await asyncio.to_thread(server.wait_player, True)
        server.command(f"tp {PLAYER} 0.5 221 0.5")
        await asyncio.sleep(4)
        for cx in (0, 1, -1):
            bot.request_sub_chunks(0, cx, 0, [13, 14])
        await asyncio.sleep(5)
        server.command("setblock 0 220 0 diamond_block")
        await asyncio.sleep(1)
        server.command("setblock 0 220 0 stone")
        server.command("setblock 1 220 0 glass")
        server.command("setblock 16 220 0 stone")
        server.command("setblock -1 220 0 stone")
        server.command("setblock 0 222 0 stone")
        await asyncio.sleep(0.3)
        server.command("setblock 0 222 0 air")
        await asyncio.sleep(3)
        for cx in (0, 1, -1):
            bot.request_sub_chunks(0, cx, 0, [13, 14])
        await asyncio.sleep(3)
        probes = [
            ("stone", "0 220 0", "WORLD_ORACLE_STONE"),
            ("glass", "1 220 0", "WORLD_ORACLE_GLASS"),
            ("stone", "16 220 0", "WORLD_ORACLE_EDGE"),
            ("air", "0 222 0", "WORLD_ORACLE_AIR"),
        ]
        oracle = {}
        for block, pos, marker in probes:
            offset = server.command(f"execute if block {pos} minecraft:{block} run say {marker}")
            await asyncio.sleep(0.6)
            oracle[marker] = any(marker in x for x in server.snapshot(offset))
        await asyncio.sleep(1)
        expected = {
            (0, 220, 0): "minecraft:stone",
            (1, 220, 0): "minecraft:glass",
            (16, 220, 0): "minecraft:stone",
            (-1, 220, 0): "minecraft:stone",
            (0, 222, 0): "minecraft:air",
        }
        observed = {}
        for pos, name in expected.items():
            block = world.block(*pos)
            observed[str(pos)] = None if block is None else {
                "name": block.name, "runtime_id": block.runtime_id, "solid": block.solid,
            }
        unknown = world.block(500, 220, 500)
        stand = world.can_stand(0, 221, 0)
        world_valid = (
            all(observed[str(pos)] is not None
                and observed[str(pos)]["name"] == name for pos, name in expected.items())
            and unknown is None and stand is True
        )
        results.update({
            "world_blocks": observed,
            "world_unknown": unknown is None,
            "world_stand": stand,
            "observed_subchunks": world.observed_subchunks,
            "world_query_verified": world_valid,
            "subchunk_decode_timings": decode_samples,

            "start_game": start_game,
            "packet_counts": dict(counts),
            "chunks": chunks,
            "subchunks": subchunks,
            "block_updates": updates,
            "oracle": oracle,
            "observer_errors": errors,
            "status": "PASS" if (
                chunks and subchunks and any(
                    e["result"] == 1 for item in subchunks for e in item["entries"]
                ) and all(oracle.values()) and
                any(u.get("pos") == [0, 220, 0] for u in updates)
                and world_valid and not errors
            ) else "INCOMPLETE",
        })
        print("WORLD_SUMMARY " + json.dumps({
            "status": results["status"], "start_game": start_game, "counts": dict(counts),
            "chunk_examples": [{k: v for k, v in c.items() if k != "raw"} for c in chunks[:10]],
            "subchunk_examples": [
                {"center": c["center"], "entries": [
                    {**{k: v for k, v in e.items() if k != "raw"},
                     "head_hex": e["raw"]["head_hex"] if "raw" in e else None} for e in c["entries"]
                ]} for c in subchunks[:5]
            ],
            "updates": updates[-15:], "oracle": oracle, "observer_errors": errors,
            "world_blocks": observed, "world_stand": stand, "world_valid": world_valid,
            "observed_subchunks": world.observed_subchunks,
        }, ensure_ascii=False), flush=True)
        # Verify actual BDS dimension changes, not fabricated DimensionEvent.
        before_session = bot.get_status().session_id
        server.command(f"execute as {PLAYER} in nether run tp @s 0 110 0")
        deadline = loop.time() + 25
        while loop.time() < deadline and bot.get_status().dimension != 1:
            await asyncio.sleep(0.1)
        assert bot.get_status().dimension == 1, "BDS did not change dimension to Nether"
        assert world.observed_subchunks == 0, "Overworld subchunks retained after BDS Nether transfer"
        assert world.block(0, 220, 0) is None
        results["dimension_invalidated"] = True

        server.command(f"execute as {PLAYER} in overworld run tp @s 0 221 0")
        deadline = loop.time() + 25
        while loop.time() < deadline and bot.get_status().dimension != 0:
            await asyncio.sleep(0.1)
        assert bot.get_status().dimension == 0, "BDS did not restore Overworld"
        for cx in (0, 1, -1):
            bot.request_sub_chunks(0, cx, 0, [13, 14])
        deadline = loop.time() + 15
        while loop.time() < deadline and (
            world.block(0, 220, 0) is None or world.block(0, 220, 0).name != "minecraft:stone"
        ):
            await asyncio.sleep(0.1)
        assert world.block(0, 220, 0) is not None, "Reload did not reconstruct world view"
        assert world.block(0, 220, 0).name == "minecraft:stone"
        results["reloaded_world_after_dimension_return"] = True

        # Packet 121 explicitly publishes the view center and radius in blocks.
        server.command(f"tp {PLAYER} 600.5 221 0.5")
        deadline = loop.time() + 15
        while loop.time() < deadline and world.block(0, 220, 0) is not None:
            await asyncio.sleep(0.1)
        assert world.block(0, 220, 0) is None, "Old publisher view still queryable"
        results["publisher_invalidated"] = True

        await bot.disconnect()
        assert world.observed_subchunks == 0, "world retained data after disconnect"
        await bot.reconnect()
        await bot.wait_ready(70)
        assert bot.get_status().session_id != before_session
        assert world.block(0, 220, 0) is None, "new Session reused old world view"
        results["reconnect_invalidated"] = True
        await bot.disconnect()
        unsubscribe_world()
        print("WORLD_LIFECYCLE " + json.dumps({
            "dimension_invalidated": results["dimension_invalidated"],
            "reloaded_after_return": results["reloaded_world_after_dimension_return"],
            "publisher_invalidated": results["publisher_invalidated"],
            "reconnect_invalidated": results["reconnect_invalidated"],
            "decode_samples": decode_samples,
        }), flush=True)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--transport", choices=("raknet", "nethernet"), required=True)
    args = parser.parse_args()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    folder = work / "server"
    server = Server(args.server_python.absolute(), folder, work / "bds.log")
    results = {"status": "FAIL", "transport": args.transport, "bds": "26.52", "protocol": 2193}
    try:
        server.start()
        server.wait_ready()
        server.stop()
        configure(folder / "server.properties", args.transport)
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server.start()
        server.wait_ready()
        asyncio.run(observe(server, results, args.transport))
    except BaseException as e:
        results["error"] = f"{type(e).__name__}: {e}"
        print(results["error"], flush=True)
    finally:
        try:
            server.stop()
        except BaseException as e:
            results["cleanup_error"] = f"{type(e).__name__}: {e}"
            results["status"] = "FAIL"
        (work / "world_probe.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if results["status"] == "PASS" else 1

if __name__ == "__main__":
    sys.exit(main())
