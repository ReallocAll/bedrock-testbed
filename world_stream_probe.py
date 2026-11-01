"""Bounded real-BDS observation of natural world streaming and SubChunk requests.

This is a Testbed-only experiment. It reuses testbed.main() for the pinned
EndPilot check and BDS lifecycle, and uses Navigator's existing single input
writer integration only for a known, flat, bounded walk.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone

import scenarios
import testbed
from server import PLAYER, ROOT

TESTBED_COMMIT = "6e97e231f499ef2147e486b8105744370f3789ee"
ENDPILOT_COMMIT = "1d530f0efbb3b0ac03f0945f2f5d89efa81eee3a"
PROTOCOL_VERSION = 2193
DIMENSION_OVERWORLD = 0
WALK_FEET_Y = 100
WALK_SUBCHUNK_Y = WALK_FEET_Y // 16
MAX_EXPERIMENT_SECONDS = 120.0
RESERVED_SERVER_CLEANUP_SECONDS = 8.0
P1_MAX_SECONDS = 30.0
P2_MAX_WAIT_SECONDS = 10.0
MAX_P2_TARGETS = 8
MAX_WALK_BLOCKS = 64
MAX_PACKET_EVENTS = 2000
MAX_EVENT_EVIDENCE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_TRACKED_COORDINATES = 4096
MAX_QUERY_SNAPSHOTS = 256
MAX_SUBCHUNK_ENTRIES_PER_PACKET = 64
TRACKED_PACKET_IDS = {11, 21, 58, 61, 69, 70, 110, 121, 172, 174}
SUBCHUNK_RESULTS = {
    0: "UNDEFINED",
    1: "SUCCESS",
    2: "LEVEL_CHUNK_DOESNT_EXIST",
    3: "WRONG_DIMENSION",
    4: "PLAYER_DOESNT_EXIST",
    5: "INDEX_OUT_OF_BOUNDS",
    6: "SUCCESS_ALL_AIR",
}


def _publisher_contains(publisher: dict | None, chunk_x: int, chunk_z: int) -> bool:
    if publisher is None:
        return False
    px, pz = publisher["center_block"]
    radius = publisher["radius_blocks"]
    dx = max(chunk_x * 16 - px, px - (chunk_x * 16 + 15), 0)
    dz = max(chunk_z * 16 - pz, pz - (chunk_z * 16 + 15), 0)
    return dx * dx + dz * dz <= radius * radius


def _block_snapshot(world, x: int, y: int, z: int) -> dict:
    block = world.block(x, y, z)
    if block is None:
        value = None
    else:
        value = {
            "runtime_id": int(block.runtime_id),
            "name": block.name,
            "air": bool(block.air),
            "solid": block.solid,
        }
    return {
        "coordinate_blocks": [x, y, z],
        "block_known": block is not None,
        "block": value,
        "can_stand": world.can_stand(x, y, z),
    }


async def exercise(server, results, transport):
    from endpilot import (
        BotOptions,
        EventKind,
        PacketDecodedEvent,
        PositionEvent,
        Runtime,
        event_mask,
    )
    from endpilot.navigation import GoalNear, NavigationError, Navigator
    from endpilot.world import WorldQuery

    loop = asyncio.get_running_loop()
    started = loop.time()
    operation_deadline = started + MAX_EXPERIMENT_SECONDS - RESERVED_SERVER_CLEANUP_SECONDS
    endpoint = (
        "http://127.0.0.1:19132"
        if transport == "nethernet"
        else "raknet://127.0.0.1:19132"
    )
    stream = {
        "schema_version": 1,
        "experiment_id": "endpilot-stage-3.2-pr-0-world-stream",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "testbed_commit": os.environ.get("GITHUB_SHA"),
        "testbed_expected_commit": TESTBED_COMMIT,
        "endpilot_expected_commit": results.get("endpilot_commit", ENDPILOT_COMMIT),
        "endpilot_installed_commit": results.get("installed_endpilot_commit"),
        "transport": transport,
        "bds_version": results.get("bds", "1.26.52"),
        "endstone_version": results.get("endstone", "0.11.14"),
        "protocol_version": PROTOCOL_VERSION,
        "dimension": DIMENSION_OVERWORLD,
        "limits": {
            "experiment_seconds_including_reserved_shutdown": MAX_EXPERIMENT_SECONDS,
            "p1_passive_seconds_max": P1_MAX_SECONDS,
            "p2_wait_seconds_per_batch_max": P2_MAX_WAIT_SECONDS,
            "p2_targets_max": MAX_P2_TARGETS,
            "p2_batches_max": 2,
            "walk_blocks_max": MAX_WALK_BLOCKS,
            "packet_events_max": MAX_PACKET_EVENTS,
            "tracked_coordinates_max": MAX_TRACKED_COORDINATES,
            "worldquery_snapshots_max": MAX_QUERY_SNAPSHOTS,
            "world_stream_json_bytes_max": MAX_JSON_BYTES,
            "p1_subchunk_requests_max": 0,
        },
        "status": "RUNNING",
        "experiment_conclusion": "INCONCLUSIVE",
        "phase": "initialization",
        "phase_times_ms": {"initialization_started": 0.0},
        "packet_events": [],
        "requests": [],
        "observer_errors": [],
        "limitations": [
            "EndPilot WorldQuery observes SubChunk and incremental block packets but does not parse LevelChunk serialized_chunk.",
            "SubChunk responses have no request identifier; P2 correlation uses target coordinates and the isolated request phase.",
            "WorldQuery can_stand is a conservative full-cube test and does not model complex collision shapes or fluids.",
        ],
    }
    active_phase = {"value": "initialization"}
    budget = {"event_count": 0, "event_bytes": 0, "exhausted": False}
    state = {
        "all_level_chunks": set(),
        "all_subchunks": set(),
        "initial_level_chunks": set(),
        "initial_subchunks": set(),
        "initial_request_targets": set(),
        "initial_request_chunks": set(),
        "initial_request_times": {},
        "initial_responses": {},
        "p1_new_level_chunks": set(),
        "p1_new_subchunks": set(),
        "latest_publisher": None,
        "last_world_change": started,
        "p1_position_events": [],
        "p2_request_times": {},
        "p2_request_chunks": set(),
        "p2_responses": {},
        "p2_late_responses": [],
        "request_calls_by_phase": {
            "initialization": 0,
            "p1_passive": 0,
            "p2_bounded_request": 0,
        },
        "coordinate_overflow": False,
        "p1_safety_violation": False,
        "observer_failure": False,
    }
    event_ready = asyncio.Event()
    runtime = None
    bot = None
    world = None
    navigator = None
    unsubscribe = None

    def remaining() -> float:
        return max(0.0, operation_deadline - loop.time())

    def require_time(stage: str) -> None:
        if remaining() <= 0:
            raise TimeoutError(f"120-second experiment budget exhausted during {stage}")

    def note_world_change() -> None:
        state["last_world_change"] = loop.time()

    def add_coordinate(collection: set, value: tuple) -> bool:
        was_new = value not in collection
        if was_new:
            is_primary_inventory = (
                collection is state["all_level_chunks"]
                or collection is state["all_subchunks"]
            )
            total_coordinates = (
                len(state["all_level_chunks"]) + len(state["all_subchunks"])
            )
            if is_primary_inventory and total_coordinates >= MAX_TRACKED_COORDINATES:
                state["coordinate_overflow"] = True
                budget["exhausted"] = True
                return False
            collection.add(value)
        return was_new

    def add_packet_event(row: dict) -> None:
        encoded_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if (
            budget["event_count"] >= MAX_PACKET_EVENTS
            or budget["event_bytes"] + encoded_size > MAX_EVENT_EVIDENCE_BYTES
        ):
            budget["exhausted"] = True
            return
        stream["packet_events"].append(row)
        budget["event_count"] += 1
        budget["event_bytes"] += encoded_size

    def payload_evidence(raw) -> dict:
        if raw is None:
            return {"length": 0, "sha256": None, "sample_hex": ""}
        view = memoryview(raw)
        return {
            "length": len(view),
            "sha256": hashlib.sha256(view).hexdigest(),
            "sample_hex": bytes(view[:32]).hex(),
        }

    def source_for_chunk(key: tuple) -> str:
        phase = active_phase["value"]
        dim, cx, cz = key
        if phase == "initialization" or (dim, cx, cz) in state["initial_request_chunks"]:
            return "initialization"
        return "passive_stream"

    def record_packet(event) -> None:
        packet = event.packet
        packet_id = int(packet.header.packet_id)
        if packet_id not in TRACKED_PACKET_IDS:
            return
        payload = packet.payload
        phase = active_phase["value"]
        status = bot.get_status()
        row = {
            "t_ms": round((loop.time() - started) * 1000, 3),
            "phase": phase,
            "session_id": int(status.session_id),
            "runtime_dimension": int(status.dimension),
            "packet_id": packet_id,
        }
        changed = False
        if packet_id == 58:
            dim = int(payload.dimension_id)
            cx, cz = int(payload.pos.x), int(payload.pos.z)
            key = (dim, cx, cz)
            previously_observed = key in state["all_level_chunks"]
            if not previously_observed:
                changed = add_coordinate(state["all_level_chunks"], key)
            source = source_for_chunk(key)
            if phase == "initialization":
                add_coordinate(state["initial_level_chunks"], key)
            elif phase == "p1_passive" and source == "passive_stream" and not previously_observed:
                add_coordinate(state["p1_new_level_chunks"], key)
            count = int(payload.sub_chunks_count)
            limit = payload.client_request_sub_chunk_limit
            cache_enabled = bool(payload.cache_enabled)
            if cache_enabled:
                payload_kind = "cache_metadata_enabled_payload_completeness_unverified"
            elif 0 < count <= 64:
                payload_kind = "uncached_serialized_subchunk_payload"
            elif count == 0:
                payload_kind = "zero_subchunk_count"
            elif count == 0xFFFFFFFE:
                payload_kind = "limited_subchunk_request_mode"
            elif count == 0xFFFFFFFF:
                payload_kind = "special_subchunk_count_mode_uninterpreted"
            else:
                payload_kind = "unrecognized_subchunk_count_mode"
            row.update({
                "source": source,
                "dimension": dim,
                "chunk": [cx, cz],
                "previously_observed": previously_observed,
                "sub_chunks_count": count,
                "client_request_sub_chunk_limit": None if limit is None else int(limit),
                "cache_enabled": cache_enabled,
                "cache_metadata_count": len(payload.cache_metadata),
                "serialized_chunk": payload_evidence(payload.serialized_chunk),
                "payload_kind": payload_kind,
                "payload_decoded_by_worldquery": False,
            })
        elif packet_id == 174:
            dim = int(payload.dimension_type)
            center = payload.center_pos
            entries = list(payload.sub_chunk_data)
            row.update({
                "dimension": dim,
                "center_chunk_x_subchunk_y_z": [int(center.x), int(center.y), int(center.z)],
                "cache_enabled": bool(payload.cache_enabled),
                "entry_count": len(entries),
                "entries_truncated": len(entries) > MAX_SUBCHUNK_ENTRIES_PER_PACKET,
                "entries": [],
            })
            if len(entries) > MAX_SUBCHUNK_ENTRIES_PER_PACKET:
                budget["exhausted"] = True
            for entry in entries[:MAX_SUBCHUNK_ENTRIES_PER_PACKET]:
                offset = entry.sub_chunk_pos_offset
                cx = int(center.x) + int(offset.x)
                sy = int(center.y) + int(offset.y)
                cz = int(center.z) + int(offset.z)
                key = (dim, cx, sy, cz)
                previously_observed = key in state["all_subchunks"]
                if not previously_observed:
                    changed = add_coordinate(state["all_subchunks"], key) or changed
                chunk_key = (dim, cx, cz)
                source = "initialization" if phase == "initialization" else "passive_stream"
                if phase == "initialization" or chunk_key in state["initial_request_chunks"]:
                    source = "initialization"
                    if key in state["initial_request_times"]:
                        request_time = state["initial_request_times"][key]
                        response_list = state["initial_responses"].setdefault(key, [])
                        if len(response_list) < 4:
                            response_list.append({
                                "t_ms": round((loop.time() - started) * 1000, 3),
                                "elapsed_ms": round((loop.time() - request_time) * 1000, 3),
                                "result": int(entry.result),
                                "result_name": SUBCHUNK_RESULTS.get(int(entry.result), "UNKNOWN"),
                                "serialized_subchunk": payload_evidence(entry.serialized_sub_chunk),
                                "cache_enabled": bool(payload.cache_enabled),
                            })
                elif phase == "p1_passive" and not previously_observed:
                    source = "passive_stream"
                    if chunk_key not in state["initial_request_chunks"]:
                        add_coordinate(state["p1_new_subchunks"], key)
                elif (
                    phase == "p2_bounded_request"
                    and chunk_key in state["p2_request_chunks"]
                ) or key in state["p2_request_times"]:
                    source = "bounded_request"
                    if key in state["p2_request_times"]:
                        request_time = state["p2_request_times"][key]
                        response_evidence = {
                            "t_ms": round((loop.time() - started) * 1000, 3),
                            "elapsed_ms": round((loop.time() - request_time) * 1000, 3),
                            "result": int(entry.result),
                            "result_name": SUBCHUNK_RESULTS.get(int(entry.result), "UNKNOWN"),
                            "serialized_subchunk": payload_evidence(entry.serialized_sub_chunk),
                            "cache_enabled": bool(payload.cache_enabled),
                        }
                        if phase == "p2_bounded_request":
                            response_list = state["p2_responses"].setdefault(key, [])
                            if len(response_list) < 4:
                                response_list.append(response_evidence)
                            event_ready.set()
                        else:
                            response_evidence["arrived_after_p2_wait_window"] = True
                            if len(state["p2_late_responses"]) < MAX_P2_TARGETS * 4:
                                state["p2_late_responses"].append({
                                    "target_dimension_chunk_x_subchunk_y_chunk_z": list(key),
                                    **response_evidence,
                                })
                result = int(entry.result)
                row["entries"].append({
                    "coordinate_chunk_x_subchunk_y_z": [cx, sy, cz],
                    "offset": [int(offset.x), int(offset.y), int(offset.z)],
                    "source": source,
                    "previously_observed": previously_observed,
                    "result": result,
                    "result_name": SUBCHUNK_RESULTS.get(result, "UNKNOWN"),
                    "serialized_subchunk": payload_evidence(entry.serialized_sub_chunk),
                })
            if phase == "initialization" or changed:
                note_world_change()
        elif packet_id == 121:
            position = payload.position
            publisher = {
                "center_block": [int(position.x), int(position.z)],
                "radius_blocks": int(payload.radius),
            }
            previous = state["latest_publisher"]
            state["latest_publisher"] = publisher
            if publisher != previous:
                changed = True
                note_world_change()
            row.update({
                "source": "initialization" if phase == "initialization" else "passive_stream",
                "event_kind": "server_publisher_update",
                **publisher,
                "server_built_chunk_count": len(payload.server_built_chunks),
                "server_built_chunks_sample": [
                    [int(chunk.x), int(chunk.z)] for chunk in payload.server_built_chunks[:32]
                ],
            })
        elif packet_id in (21, 110):
            position = payload.pos
            row.update({
                "source": "initialization" if phase == "initialization" else "passive_stream",
                "event_kind": "incremental_block_update",
                "position_blocks": [int(position.x), int(position.y), int(position.z)],
                "runtime_id": int(payload.runtime_id),
                "layer": int(payload.layer),
            })
            if phase == "initialization":
                note_world_change()
        elif packet_id == 172:
            entries = []
            for layer, changed_blocks in enumerate((payload.blocks_changed.standards, payload.blocks_changed.extras)):
                for item in changed_blocks[:MAX_SUBCHUNK_ENTRIES_PER_PACKET]:
                    entries.append({
                        "position_blocks": [int(item.pos.x), int(item.pos.y), int(item.pos.z)],
                        "runtime_id": int(item.runtime_id),
                        "layer": layer,
                    })
            row.update({
                "source": "initialization" if phase == "initialization" else "passive_stream",
                "event_kind": "incremental_subchunk_block_update",
                "entries": entries,
            })
            if phase == "initialization":
                note_world_change()
        elif packet_id == 11:
            row.update({
                "source": "initialization",
                "block_network_ids_are_hashes": bool(payload.block_network_ids_are_hashes),
                "block_properties_count": len(payload.block_properties),
                "server_version": payload.server_version,
            })
        elif packet_id == 70:
            row.update({
                "source": "initialization" if phase == "initialization" else "passive_stream",
                "event_kind": "server_radius_ack",
                "chunk_radius": int(payload.chunk_radius),
            })
        else:
            row.update({
                "source": "initialization" if phase == "initialization" else "passive_stream",
                "event_kind": "protocol_lifecycle_packet",
            })
        add_packet_event(row)
        if changed:
            note_world_change()

    def observer(source, event) -> None:
        if source is not bot:
            return
        if (
            active_phase["value"] == "p1_passive"
            and isinstance(event, PositionEvent)
            and (event.teleport or event.correction)
        ):
            state["p1_safety_violation"] = True
            state["p1_position_events"].append({
                "teleport": bool(event.teleport),
                "correction": bool(event.correction),
                "position": [event.position.x, event.position.y, event.position.z],
                "t_ms": round((loop.time() - started) * 1000, 3),
            })
            if navigator is not None:
                navigator.stop()
        try:
            frame = navigator.on_event(event) if navigator is not None else None
            if frame is not None:
                bot.set_input(frame)
        except Exception as error:
            state["observer_failure"] = True
            if len(stream["observer_errors"]) < 32:
                stream["observer_errors"].append(f"{type(error).__name__}: {error}")
            if navigator is not None:
                navigator.stop()
        if isinstance(event, PacketDecodedEvent):
            try:
                record_packet(event)
            except Exception as error:
                state["observer_failure"] = True
                if len(stream["observer_errors"]) < 32:
                    stream["observer_errors"].append(f"packet {event.packet.header.packet_id}: {type(error).__name__}: {error}")
                budget["exhausted"] = True
            if budget["exhausted"]:
                event_ready.set()
                if navigator is not None:
                    navigator.stop()

    async def fill_floor() -> None:
        command = "fill 0 99 -2 80 99 2 minecraft:stone"
        for attempt in range(3):
            require_time("safe floor preparation")
            offset = server.command(command)
            until = min(operation_deadline, loop.time() + 4.0)
            while loop.time() < until:
                lines = server.snapshot(offset)
                if any("blocks filled" in line.lower() or "block filled" in line.lower() for line in lines):
                    return
                if any("outside of the world" in line.lower() or "failed" in line.lower() for line in lines):
                    break
                await asyncio.sleep(0.05)
            lines = server.snapshot(offset)
            if any("outside of the world" in line.lower() for line in lines) and attempt < 2:
                await asyncio.sleep(0.5)
                continue
            raise RuntimeError(f"safe floor preparation was not acknowledged: {lines[-8:]}")

    async def authoritative_position(timeout: float = 5.0) -> dict:
        require_time("BDS querytarget")
        offset = server.command(f"querytarget {PLAYER}")
        until = min(operation_deadline, loop.time() + timeout)
        while loop.time() < until:
            lines = server.snapshot(offset)
            fields = [line.partition("]:")[2].strip() for line in lines]
            begin = next((i for i, value in enumerate(fields) if value.startswith("Target data:")), None)
            if begin is not None:
                end = next((i for i in range(begin + 1, len(fields)) if fields[i] == "]"), None)
                if end is not None:
                    raw = fields[begin].split("Target data:", 1)[1] + " ".join(fields[begin + 1:end + 1])
                    data = json.loads(raw)
                    if len(data) != 1:
                        raise RuntimeError(f"BDS querytarget returned {len(data)} entities")
                    target = data[0]
                    position = target["position"]
                    return {
                        "position": [float(position["x"]), float(position["y"]), float(position["z"])],
                        "dimension": int(target["dimension"]),
                        "name": target.get("name"),
                    }
            if any("unknown command" in value.lower() or "syntax error" in value.lower() for value in fields):
                raise RuntimeError("BDS querytarget is unavailable")
            await asyncio.sleep(0.025)
        raise TimeoutError(f"BDS querytarget timed out: {server.snapshot(offset)[-6:]}")

    async def stable_authoritative_position() -> dict:
        previous = None
        for _ in range(4):
            current = await authoritative_position(timeout=min(3.0, remaining()))
            if previous is not None:
                distance = math.dist(previous["position"], current["position"])
                if previous["dimension"] == current["dimension"] and distance <= 0.05:
                    current["stable"] = True
                    return current
            previous = current
            await asyncio.sleep(min(0.2, remaining()))
        raise RuntimeError("BDS authoritative position did not stabilize")

    def request_subchunk(stage: str, dimension: int, chunk_x: int, subchunk_y: int, chunk_z: int) -> None:
        if stage == "p1_passive":
            raise RuntimeError("P1 attempted an active SubChunk request")
        if stage not in ("initialization", "p2_bounded_request"):
            raise RuntimeError(f"SubChunk request outside an authorized phase: {stage}")
        if active_phase["value"] != stage:
            raise RuntimeError(f"SubChunk request phase mismatch: expected {stage}, observed {active_phase['value']}")
        key = (dimension, chunk_x, subchunk_y, chunk_z)
        if stage == "initialization":
            state["initial_request_targets"].add(key)
            state["initial_request_chunks"].add((dimension, chunk_x, chunk_z))
            state["initial_request_times"][key] = loop.time()
        else:
            if key in state["p2_request_times"] or len(state["p2_request_times"]) >= MAX_P2_TARGETS:
                raise RuntimeError("P2 request budget or duplicate-target guard tripped")
            current = bot.get_status()
            if (
                current.session_id != stream["session_id"]
                or current.dimension != dimension
                or current.state.value != 5
            ):
                raise RuntimeError("P2 target no longer belongs to the active Ready Session and Dimension")
            if not _publisher_contains(state["latest_publisher"], chunk_x, chunk_z):
                raise RuntimeError("P2 target is no longer inside the latest server Publisher range")
            state["p2_request_times"][key] = loop.time()
            state["p2_request_chunks"].add((dimension, chunk_x, chunk_z))
        state["request_calls_by_phase"][stage] += 1
        stream["requests"].append({
            "phase": stage,
            "source": "initialization" if stage == "initialization" else "bounded_request",
            "dimension": dimension,
            "target_chunk_x_subchunk_y_z": [chunk_x, subchunk_y, chunk_z],
            "coordinate_units": {"x": "chunk", "y": "absolute_subchunk", "z": "chunk"},
            "session_id": int(bot.get_status().session_id),
            "publisher": state["latest_publisher"],
            "publisher_contains_target": _publisher_contains(
                state["latest_publisher"], chunk_x, chunk_z
            ),
            "t_ms": round((loop.time() - started) * 1000, 3),
            "batch": 1 if stage == "p2_bounded_request" else None,
        })
        bot.request_sub_chunks(dimension, chunk_x, chunk_z, [subchunk_y])

    def target_snapshot(chunk: tuple[int, int, int]) -> dict:
        dimension, chunk_x, chunk_z = chunk
        x, z = chunk_x * 16 + 8, chunk_z * 16 + 8
        value = _block_snapshot(world, x, WALK_FEET_Y, z)
        value.update({
            "dimension": dimension,
            "chunk": [chunk_x, chunk_z],
            "subchunk_y": WALK_SUBCHUNK_Y,
            "source": "worldquery_observation",
        })
        return value

    def select_p1_targets(before: dict, baseline_chunks: set, baseline_subchunks: set) -> list[dict]:
        publisher = state["latest_publisher"]
        if publisher is None:
            return []
        start_x, _, start_z = before["position"]
        end_x = start_x + MAX_WALK_BLOCKS
        projected = {
            "center_block": [publisher["center_block"][0] + MAX_WALK_BLOCKS, publisher["center_block"][1]],
            "radius_blocks": publisher["radius_blocks"],
        }
        old_edge = math.floor((publisher["center_block"][0] + publisher["radius_blocks"]) / 16)
        new_edge = math.floor((projected["center_block"][0] + projected["radius_blocks"]) / 16)
        base_z = math.floor(start_z / 16)
        candidates = []
        for chunk_x in range(max(math.floor(start_x / 16), old_edge - 2), min(new_edge + 2, old_edge + 10)):
            if chunk_x * 16 <= end_x:
                continue
            for chunk_z in range(base_z - 1, base_z + 2):
                chunk = (DIMENSION_OVERWORLD, chunk_x, chunk_z)
                subchunk = (DIMENSION_OVERWORLD, chunk_x, WALK_SUBCHUNK_Y, chunk_z)
                if chunk in baseline_chunks or chunk in state["initial_request_chunks"]:
                    continue
                if subchunk in baseline_subchunks or subchunk in state["initial_request_targets"]:
                    continue
                if _publisher_contains(publisher, chunk_x, chunk_z):
                    continue
                if not _publisher_contains(projected, chunk_x, chunk_z):
                    continue
                x, z = chunk_x * 16 + 8, chunk_z * 16 + 8
                if world.block(x, WALK_FEET_Y, z) is not None:
                    continue
                candidates.append({
                    "dimension": DIMENSION_OVERWORLD,
                    "chunk": [chunk_x, chunk_z],
                    "sample_blocks": [x, WALK_FEET_Y, z],
                    "subchunk_y": WALK_SUBCHUNK_Y,
                    "previously_observed_levelchunk": False,
                    "previously_observed_subchunk": False,
                    "previously_requested": False,
                    "initial_worldquery": target_snapshot(chunk),
                    "publisher_before": publisher,
                    "predicted_publisher_after": projected,
                    "selection_basis": "unknown forward chunk outside the initial publisher and inside its 64-block projected publisher range",
                })
        return candidates[:8]

    def classify_result(p1: dict, p2: dict, p3: dict) -> str:
        if state["p1_safety_violation"] or budget["exhausted"] or state["coordinate_overflow"]:
            return "EXPERIMENT_EXECUTION_BLOCKED"
        if (
            p1.get("valid_authoritative_movement")
            and p1.get("target_unknown_before_movement")
            and p1.get("passive_worldquery_usable")
        ):
            return "PASSIVE_GROWTH_SUPPORTED"
        if p2.get("usable_request_result"):
            return "BOUNDED_SUBCHUNK_REQUEST_SUPPORTED"
        if p1.get("uncached_levelchunk_without_worldquery_data"):
            return "LEVELCHUNK_ADAPTATION_NEEDED"
        if p1.get("uncached_levelchunk_payload_seen_but_coordinate_coverage_unverified"):
            return "INCONCLUSIVE"
        if p1.get("valid_authoritative_movement") and p1.get("target_unknown_before_movement"):
            return "NO_USABLE_GROWTH_OBSERVED"
        return "INCONCLUSIVE"

    try:
        require_time("runtime initialization")
        from endpilot import Runtime

        runtime = Runtime()
        async with runtime:
            bot = runtime.add_bot(BotOptions(
                server_url=endpoint,
                transport=transport,
                protocol=PROTOCOL_VERSION,
                name=PLAYER,
                chunk_radius=8,
                client_cache=False,
                validate_all_packets=True,
            ))
            world = WorldQuery(bot)
            navigator = Navigator(bot, world)
            event_mask_value = 0
            for kind in (EventKind.tick, EventKind.position, EventKind.dimension,
                         EventKind.state, EventKind.packet_decoded):
                event_mask_value |= event_mask(kind)
            unsubscribe = runtime.subscribe(observer, events=event_mask_value)

            await asyncio.wait_for(bot.wait_ready(45), timeout=min(45.0, remaining()))
            await asyncio.wait_for(asyncio.to_thread(server.wait_player, True), timeout=min(7.0, remaining()))
            stream["session_id"] = int(bot.get_status().session_id)
            stream["dimension"] = int(bot.get_status().dimension)
            stream["requested_chunk_radius"] = 8
            stream["client_cache_enabled"] = False
            if stream["dimension"] != DIMENSION_OVERWORLD:
                raise RuntimeError(f"expected Overworld dimension, got {stream['dimension']}")

            await fill_floor()
            server.command(f"tp {PLAYER} 0.5 100 0.5")
            initial_authority = await stable_authoritative_position()
            if initial_authority["dimension"] != DIMENSION_OVERWORLD:
                raise RuntimeError("BDS authoritative starting position is not in the Overworld")
            if math.dist(initial_authority["position"], [0.5, 101.62001, 0.5]) > 0.25:
                raise RuntimeError(f"unexpected BDS authoritative start position: {initial_authority}")

            for chunk_x in range(5):
                request_subchunk("initialization", DIMENSION_OVERWORLD, chunk_x, WALK_SUBCHUNK_Y, 0)
            init_deadline = min(operation_deadline, loop.time() + 12.0)
            path_known = lambda: all(world.can_stand(x, WALK_FEET_Y, 0) is True for x in range(65))
            while loop.time() < init_deadline and not path_known() and not budget["exhausted"]:
                await asyncio.sleep(0.05)
            if budget["exhausted"]:
                raise RuntimeError("structured evidence budget exhausted during initialization")
            if not path_known():
                raise RuntimeError("could not establish a fully known safe 64-block corridor")
            if len(state["initial_request_targets"]) != 5:
                raise RuntimeError("initialization request set did not match the five corridor SubChunks")

            quiet_deadline = min(operation_deadline, loop.time() + 7.0)
            quiet = False
            while loop.time() < quiet_deadline:
                pending_init = state["initial_request_targets"] - set(state["initial_responses"])
                if loop.time() - state["last_world_change"] >= 2.0 and not pending_init:
                    quiet = True
                    break
                if budget["exhausted"]:
                    raise RuntimeError("structured evidence budget exhausted before P1 baseline")
                await asyncio.sleep(0.05)
            if not quiet:
                raise RuntimeError("initial packet and publisher observations did not reach a stable baseline")

            initial_level_chunks = set(state["all_level_chunks"])
            initial_subchunks = set(state["all_subchunks"])
            initial_worldquery_count = int(world.observed_subchunks)
            initial_publisher = state["latest_publisher"]
            initial_chunk_radius = int(bot.get_status().chunk_radius)
            known_air_coordinate = (0, WALK_FEET_Y, 0)
            known_air_before = _block_snapshot(world, *known_air_coordinate)
            initial_retention_probes = []
            for dim, cx, sy, cz in sorted(initial_subchunks):
                if dim != DIMENSION_OVERWORLD:
                    continue
                x, y, z = cx * 16 + 8, sy * 16 + 8, cz * 16 + 8
                before = _block_snapshot(world, x, y, z)
                if before["block_known"]:
                    initial_retention_probes.append({
                        "dimension": dim,
                        "chunk": [cx, cz],
                        "subchunk_y": sy,
                        "coordinate_blocks": [x, y, z],
                        "publisher_before": initial_publisher,
                        "worldquery_before": before,
                    })
                    if len(initial_retention_probes) >= 8:
                        break
            stream["initialization"] = {
                "authoritative_position": initial_authority,
                "session_id": int(bot.get_status().session_id),
                "dimension": int(bot.get_status().dimension),
                "levelchunk_count": len(initial_level_chunks),
                "level_chunks": [list(key) for key in sorted(initial_level_chunks)],
                "subchunk_count": len(initial_subchunks),
                "subchunks_dimension_chunk_x_subchunk_y_chunk_z": [list(key) for key in sorted(initial_subchunks)],
                "worldquery_known_subchunks": initial_worldquery_count,
                "known_air_reference": known_air_before,
                "publisher_retention_probes": initial_retention_probes,
                "publisher": initial_publisher,
                "explicit_subchunk_requests": [
                    list(key) for key in sorted(state["initial_request_targets"])
                ],
                "explicit_subchunk_responses": [
                    {
                        "target_dimension_chunk_x_subchunk_y_chunk_z": list(key),
                        "responses": state["initial_responses"].get(key, []),
                    }
                    for key in sorted(state["initial_request_targets"])
                ],
                "safe_corridor": {
                    "feet_y": WALK_FEET_Y,
                    "start_block": [0, WALK_FEET_Y, 0],
                    "goal_block": [MAX_WALK_BLOCKS, WALK_FEET_Y, 0],
                    "known_safe_cells": 65,
                    "known_safe": True,
                },
                "baseline_frozen_at_ms": round((loop.time() - started) * 1000, 3),
            }
            stream["phase_times_ms"]["initialization_completed"] = round((loop.time() - started) * 1000, 3)
            p1_targets = select_p1_targets(initial_authority, initial_level_chunks, initial_subchunks)
            stream["p1_target_selection"] = {
                "method": "select forward chunks outside the frozen publisher and observation sets, inside the publisher projected 64 blocks along the known corridor",
                "targets": p1_targets,
                "selected": bool(p1_targets),
            }

            p1_target_before = {
                tuple(target["chunk"]): target["initial_worldquery"]
                for target in p1_targets
            }
            goal_x = MAX_WALK_BLOCKS
            p1_window = min(P1_MAX_SECONDS, max(5.0, remaining() - P2_MAX_WAIT_SECONDS - 8.0))
            p1_started = loop.time()
            stream["phase_times_ms"]["p1_passive_started"] = round((p1_started - started) * 1000, 3)
            p1_deadline = min(operation_deadline, p1_started + p1_window)
            active_phase["value"] = "p1_passive"
            stream["phase"] = "p1_passive"
            stream["p1_subchunk_request_count"] = 0
            movement_result = "not_started"
            movement_error = None
            movement_task = asyncio.create_task(navigator.goto(GoalNear(goal_x, WALK_FEET_Y, 0)))
            move_timeout = min(22.0, max(1.0, p1_window - 5.0), max(0.1, p1_deadline - loop.time()))
            try:
                await asyncio.wait_for(movement_task, timeout=move_timeout)
                movement_result = "navigator_predicted_completion"
            except asyncio.TimeoutError:
                navigator.stop()
                movement_result = "bounded_movement_timeout"
                movement_error = "Navigator did not complete within the bounded movement interval"
            except NavigationError as error:
                navigator.stop()
                movement_result = "navigation_failed_closed"
                movement_error = f"{type(error).__name__}: {error}"
            except Exception as error:
                navigator.stop()
                movement_result = "movement_exception"
                movement_error = f"{type(error).__name__}: {error}"
            finally:
                if not movement_task.done():
                    movement_task.cancel()
                    try:
                        await movement_task
                    except asyncio.CancelledError:
                        pass
                if navigator.controller.state == "coasting":
                    coast_deadline = min(p1_deadline, loop.time() + 1.5)
                    while loop.time() < coast_deadline and navigator.controller.state == "coasting":
                        await asyncio.sleep(0.05)

            while loop.time() < p1_deadline and not budget["exhausted"]:
                await asyncio.sleep(min(0.05, max(0.0, p1_deadline - loop.time())))
            p1_elapsed = loop.time() - p1_started
            stream["phase_times_ms"]["p1_passive_completed"] = round((loop.time() - started) * 1000, 3)
            p1_worldquery_count_after = int(world.observed_subchunks)
            p1_publisher_after = state["latest_publisher"]
            p1_new_chunks = sorted(state["p1_new_level_chunks"])
            p1_new_subchunks = sorted(state["p1_new_subchunks"])
            target_after = {
                tuple(target["chunk"]): target_snapshot((DIMENSION_OVERWORLD, *target["chunk"]))
                for target in p1_targets
            }
            p1_new_region_snapshots = []
            for dim, cx, cz in p1_new_chunks[:MAX_QUERY_SNAPSHOTS // 2]:
                if dim == DIMENSION_OVERWORLD:
                    snapshot = target_snapshot((dim, cx, cz))
                    snapshot["stream_coordinate_source"] = "new_passive_levelchunk"
                    p1_new_region_snapshots.append(snapshot)
            for dim, cx, sy, cz in p1_new_subchunks[:MAX_QUERY_SNAPSHOTS // 2]:
                if dim == DIMENSION_OVERWORLD:
                    x, y, z = cx * 16 + 8, sy * 16 + 8, cz * 16 + 8
                    snapshot = _block_snapshot(world, x, y, z)
                    snapshot.update({
                        "dimension": dim,
                        "chunk": [cx, cz],
                        "subchunk_y": sy,
                        "stream_coordinate_source": "new_passive_subchunk",
                        "source": "worldquery_observation",
                    })
                    p1_new_region_snapshots.append(snapshot)
            stream["p1_passive_observation"] = {
                "evidence_status": "passive_window_captured_authority_check_pending",
                "window_seconds": round(p1_elapsed, 3),
                "movement_result": movement_result,
                "movement_error": movement_error,
                "authoritative_before": initial_authority,
                "authoritative_after": None,
                "initial_levelchunk_count": len(initial_level_chunks),
                "new_levelchunk_count": len(p1_new_chunks),
                "new_levelchunks_dimension_chunk_x_z": [list(key) for key in p1_new_chunks],
                "initial_subchunk_count": len(initial_subchunks),
                "new_subchunk_count": len(p1_new_subchunks),
                "new_subchunks_dimension_chunk_x_subchunk_y_chunk_z": [list(key) for key in p1_new_subchunks],
                "publisher_after": p1_publisher_after,
                "active_subchunk_request_count": state["request_calls_by_phase"]["p1_passive"],
                "target_observations_after_movement": list(target_after.values()),
                "new_region_worldquery_snapshots": p1_new_region_snapshots,
                "worldquery_snapshots_truncated": (
                    len(p1_new_chunks) + len(p1_new_subchunks) > len(p1_new_region_snapshots)
                ),
                "worldquery_known_subchunks_before": initial_worldquery_count,
                "worldquery_known_subchunks_after": p1_worldquery_count_after,
                "worldquery_known_subchunks_delta": p1_worldquery_count_after - initial_worldquery_count,
                "position_corrections_or_teleports": state["p1_position_events"],
            }
            active_phase["value"] = "post_p1_authority"
            after_authority = await stable_authoritative_position()
            stream["phase_times_ms"]["post_p1_authority_completed"] = round((loop.time() - started) * 1000, 3)
            after_status = bot.get_status()
            displacement = [
                after_authority["position"][index] - initial_authority["position"][index]
                for index in range(3)
            ]
            displacement_distance = math.dist(initial_authority["position"], after_authority["position"])
            if displacement_distance > MAX_WALK_BLOCKS + 0.5:
                raise RuntimeError(f"BDS authoritative displacement exceeded 64 blocks: {displacement_distance}")
            same_session = (
                int(after_status.session_id) == int(stream["session_id"])
                and int(after_status.dimension) == DIMENSION_OVERWORLD
                and after_authority["dimension"] == DIMENSION_OVERWORLD
            )
            if not same_session:
                state["p1_safety_violation"] = True
            if state["p1_safety_violation"]:
                raise RuntimeError("teleport, correction, dimension or Session change interrupted P1")
            if budget["exhausted"] or state["coordinate_overflow"] or state["observer_failure"]:
                raise RuntimeError("bounded packet observation failed or exhausted its evidence budget")
            if state["request_calls_by_phase"]["p1_passive"] != 0:
                raise RuntimeError("P1 issued a forbidden active SubChunk request")

            selected_target_unknown = bool(p1_targets) and all(
                not target["previously_observed_levelchunk"]
                and not target["previously_observed_subchunk"]
                and not target["previously_requested"]
                and target["initial_worldquery"]["block_known"] is False
                for target in p1_targets
            )
            actual_publisher = p1_publisher_after
            qualifying_new_chunks = {
                (dim, cx, cz) for dim, cx, cz in p1_new_chunks
                if dim == DIMENSION_OVERWORLD
                and not _publisher_contains(initial_publisher, cx, cz)
                and _publisher_contains(actual_publisher, cx, cz)
            }
            qualifying_new_subchunks = {
                (dim, cx, sy, cz) for dim, cx, sy, cz in p1_new_subchunks
                if dim == DIMENSION_OVERWORLD
                and not _publisher_contains(initial_publisher, cx, cz)
                and _publisher_contains(actual_publisher, cx, cz)
            }
            new_region_unknown = bool(qualifying_new_chunks or qualifying_new_subchunks)
            p1_target_unknown = selected_target_unknown or new_region_unknown
            p1_worldquery_usable = any(
                p1_target_before[chunk]["block_known"] is False
                and snapshot["block_known"] is True
                and snapshot["can_stand"] is not None
                and not _publisher_contains(initial_publisher, *chunk)
                and _publisher_contains(actual_publisher, *chunk)
                for chunk, snapshot in target_after.items()
            ) or any(
                snapshot.get("stream_coordinate_source") == "new_passive_subchunk"
                and (
                    snapshot["dimension"],
                    snapshot["chunk"][0],
                    snapshot["subchunk_y"],
                    snapshot["chunk"][1],
                ) in qualifying_new_subchunks
                and snapshot.get("block_known") is True
                and snapshot.get("can_stand") is not None
                for snapshot in p1_new_region_snapshots
            )
            uncached_levelchunk_without_worldquery = None
            levelchunk_coverage_unverified = any(
                row.get("packet_id") == 58
                and row.get("phase") == "p1_passive"
                and row.get("source") == "passive_stream"
                and row.get("payload_kind") == "uncached_serialized_subchunk_payload"
                for row in stream["packet_events"]
            ) and not p1_worldquery_usable
            valid_movement = displacement_distance >= 1.0 and displacement_distance <= MAX_WALK_BLOCKS + 0.5 and same_session and not state["p1_safety_violation"]
            p1 = {
                "window_seconds": round(p1_elapsed, 3),
                "movement_result": movement_result,
                "movement_error": movement_error,
                "navigator_path": [list(cell) for cell in (navigator.last_plan.path if navigator.last_plan else ())],
                "navigator_path_status": navigator.last_plan.status if navigator.last_plan else None,
                "authoritative_before": initial_authority,
                "authoritative_after": after_authority,
                "authoritative_displacement": displacement,
                "authoritative_distance": round(displacement_distance, 4),
                "bds_authoritative_position_stable": bool(after_authority.get("stable")),
                "session_id_before": int(stream["session_id"]),
                "session_id_after": int(after_status.session_id),
                "dimension_before": DIMENSION_OVERWORLD,
                "dimension_after": int(after_status.dimension),
                "chunk_radius_before": initial_chunk_radius,
                "chunk_radius_after": int(after_status.chunk_radius),
                "chunk_radius_option": 8,
                "position_corrections_or_teleports": state["p1_position_events"],
                "initial_levelchunk_count": len(initial_level_chunks),
                "new_levelchunk_count": len(p1_new_chunks),
                "new_levelchunks_dimension_chunk_x_z": [list(key) for key in p1_new_chunks],
                "initial_subchunk_count": len(initial_subchunks),
                "new_subchunk_count": len(p1_new_subchunks),
                "new_subchunks_dimension_chunk_x_subchunk_y_chunk_z": [list(key) for key in p1_new_subchunks],
                "publisher_after": p1_publisher_after,
                "active_subchunk_request_count": state["request_calls_by_phase"]["p1_passive"],
                "target_unknown_before_movement": p1_target_unknown,
                "selected_forward_target_unknown_before_movement": selected_target_unknown,
                "new_packet_coordinates_absent_from_frozen_baseline": new_region_unknown,
                "new_coordinates_outside_initial_and_inside_final_publisher": {
                    "levelchunks": [list(key) for key in sorted(qualifying_new_chunks)],
                    "subchunks": [list(key) for key in sorted(qualifying_new_subchunks)],
                },
                "target_observations_after_movement": list(target_after.values()),
                "new_region_worldquery_snapshots": p1_new_region_snapshots,
                "worldquery_snapshots_truncated": (
                    len(p1_new_chunks) + len(p1_new_subchunks) > len(p1_new_region_snapshots)
                ),
                "worldquery_known_subchunks_before": initial_worldquery_count,
                "worldquery_known_subchunks_after": p1_worldquery_count_after,
                "worldquery_known_subchunks_delta": p1_worldquery_count_after - initial_worldquery_count,
                "passive_worldquery_usable": p1_worldquery_usable,
                "uncached_levelchunk_without_worldquery_data": uncached_levelchunk_without_worldquery,
                "uncached_levelchunk_payload_seen_but_coordinate_coverage_unverified": levelchunk_coverage_unverified,
                "levelchunk_adaptation_assessment": (
                    "UNVERIFIED: serialized LevelChunk payload is not decoded by this probe"
                ),
                "valid_authoritative_movement": valid_movement,
            }
            if p1_worldquery_usable and valid_movement:
                p1["conclusion"] = "PASSIVE_GROWTH_SUPPORTED"
            elif uncached_levelchunk_without_worldquery and valid_movement:
                p1["conclusion"] = "LEVELCHUNK_ADAPTATION_NEEDED"
            elif levelchunk_coverage_unverified:
                p1["conclusion"] = "INCONCLUSIVE"
            elif valid_movement and p1_target_unknown:
                p1["conclusion"] = "NO_USABLE_GROWTH_OBSERVED"
            else:
                p1["conclusion"] = "INCONCLUSIVE"
            stream["p1_passive_observation"] = p1

            p2_before_count = int(world.observed_subchunks)
            p2_targets = []
            publisher = state["latest_publisher"]
            if same_session and publisher is not None:
                for dim, cx, cz in p1_new_chunks:
                    key = (dim, cx, WALK_SUBCHUNK_Y, cz)
                    if dim != DIMENSION_OVERWORLD or key in state["initial_request_targets"]:
                        continue
                    if not _publisher_contains(publisher, cx, cz):
                        continue
                    sample = target_snapshot((dim, cx, cz))
                    if sample["block_known"] and sample["can_stand"] is not None:
                        continue
                    p2_targets.append({
                        "dimension": dim,
                        "chunk_x": cx,
                        "chunk_z": cz,
                        "subchunk_y": WALK_SUBCHUNK_Y,
                        "sample_blocks": sample["coordinate_blocks"],
                        "worldquery_before": sample,
                        "levelchunk_observed": True,
                        "subchunk_packet_previously_observed": key in state["all_subchunks"],
                        "publisher": publisher,
                    })
                    if len(p2_targets) >= MAX_P2_TARGETS:
                        break
            p2 = {
                "eligible_target_count": len(p2_targets),
                "eligible_targets": p2_targets,
                "request_count": 0,
                "batch_count": 0,
                "responses": [],
                "timeouts": [],
                "worldquery_known_subchunks_before": p2_before_count,
                "publisher_rechecked_before_send": None,
                "illegal_range_request_count": 0,
            }
            stream["p2_bounded_requests"] = p2
            active_phase["value"] = "p2_bounded_request"
            stream["phase"] = "p2_bounded_request"
            stream["phase_times_ms"]["p2_bounded_request_started"] = round((loop.time() - started) * 1000, 3)
            if not p2_targets:
                p2["skip_reason"] = "P2_SKIPPED_NO_ELIGIBLE_TARGET"
                p2["conclusion"] = "INCONCLUSIVE"
            else:
                p2["batch_count"] = 1
                for target in p2_targets:
                    request_subchunk(
                        "p2_bounded_request",
                        target["dimension"],
                        target["chunk_x"],
                        target["subchunk_y"],
                        target["chunk_z"],
                    )
                p2["request_count"] = len(p2_targets)
                p2_deadline = min(operation_deadline, loop.time() + P2_MAX_WAIT_SECONDS)
                while loop.time() < p2_deadline and not budget["exhausted"]:
                    if all(tuple((target["dimension"], target["chunk_x"], target["subchunk_y"], target["chunk_z"])) in state["p2_responses"] for target in p2_targets):
                        break
                    event_ready.clear()
                    try:
                        await asyncio.wait_for(event_ready.wait(), timeout=min(0.1, p2_deadline - loop.time()))
                    except asyncio.TimeoutError:
                        pass
                for target in p2_targets:
                    key = (target["dimension"], target["chunk_x"], target["subchunk_y"], target["chunk_z"])
                    responses = state["p2_responses"].get(key, [])
                    requested_at = state["p2_request_times"].get(key)
                    elapsed_ms = None if not responses else responses[0]["elapsed_ms"]
                    after = target_snapshot((target["dimension"], target["chunk_x"], target["chunk_z"]))
                    response_row = {
                        **target,
                        "response_seen": bool(responses),
                        "responses": responses,
                        "elapsed_ms_to_observation": elapsed_ms,
                        "timeout": not bool(responses),
                        "worldquery_after": after,
                        "worldquery_usable": after["block_known"] and after["can_stand"] is not None,
                        "worldquery_target_grew": (
                            (not target["worldquery_before"]["block_known"] and after["block_known"])
                            or (
                                target["worldquery_before"]["can_stand"] is None
                                and after["can_stand"] is not None
                            )
                        ),
                    }
                    p2["responses"].append(response_row)
                    if not responses:
                        p2["timeouts"].append(key)
                p2["response_count"] = sum(bool(row["response_seen"]) for row in p2["responses"])
                p2["worldquery_known_subchunks_after"] = int(world.observed_subchunks)
                p2["worldquery_known_subchunks_interval_delta"] = int(world.observed_subchunks) - p2_before_count
                p2["worldquery_interval_delta_causality"] = (
                    "aggregate count change during the P2 interval; targeted request contribution is reported per target"
                )
                p2["usable_request_result"] = any(
                    row["response_seen"]
                    and any(response["result"] in (1, 6) for response in row["responses"])
                    and row["worldquery_usable"]
                    and row["worldquery_target_grew"]
                    for row in p2["responses"]
                )
                p2["correlation"] = "coordinate, dimension, and isolated post-request phase; SubChunk protocol packets have no request ID"
                p2["publisher_rechecked_before_send"] = all(
                    request["publisher_contains_target"]
                    for request in stream["requests"]
                    if request["phase"] == "p2_bounded_request"
                )
                p2["illegal_range_request_count"] = sum(
                    not request["publisher_contains_target"]
                    for request in stream["requests"]
                    if request["phase"] == "p2_bounded_request"
                )
                p2["conclusion"] = (
                    "BOUNDED_SUBCHUNK_REQUEST_SUPPORTED"
                    if p2["usable_request_result"] else "NO_USABLE_GROWTH_OBSERVED"
                )
                if budget["exhausted"] or state["coordinate_overflow"] or state["observer_failure"]:
                    raise RuntimeError("P2 packet observation failed or exhausted its evidence budget")
            stream["p2_bounded_requests"] = p2
            stream["phase_times_ms"]["p2_bounded_request_completed"] = round((loop.time() - started) * 1000, 3)

            active_phase["value"] = "worldquery_observation"
            stream["phase_times_ms"]["p3_worldquery_observation_started"] = round((loop.time() - started) * 1000, 3)
            retention_after = []
            for probe in initial_retention_probes:
                x, y, z = probe["coordinate_blocks"]
                after = _block_snapshot(world, x, y, z)
                outside_latest_publisher = (
                    state["latest_publisher"] is not None
                    and not _publisher_contains(
                        state["latest_publisher"], probe["chunk"][0], probe["chunk"][1]
                    )
                )
                retention_after.append({
                    **probe,
                    "worldquery_after": after,
                    "publisher_after": state["latest_publisher"],
                    "outside_latest_publisher": outside_latest_publisher,
                    "publisher_eviction_observed": (
                        probe["worldquery_before"]["block_known"]
                        and not after["block_known"]
                        and outside_latest_publisher
                    ),
                })
            publisher_evictions = [
                row for row in retention_after if row["publisher_eviction_observed"]
            ]
            known_air_after = _block_snapshot(world, *known_air_coordinate)
            unknown_reference_before = (
                p1_targets[0]["initial_worldquery"] if p1_targets else None
            )
            unknown_reference_after = (
                target_after[tuple(p1_targets[0]["chunk"])] if p1_targets else None
            )
            p3 = {
                "initialization_worldquery_known_subchunks": initial_worldquery_count,
                "after_p1_worldquery_known_subchunks": int(p1["worldquery_known_subchunks_after"]),
                "after_p2_worldquery_known_subchunks": int(world.observed_subchunks),
                "p1_contribution": {
                    "new_levelchunks": p1["new_levelchunk_count"],
                    "new_subchunks": p1["new_subchunk_count"],
                    "queryable_target_snapshots": p1["target_observations_after_movement"],
                },
                "p2_contribution": {
                    "requests": p2.get("request_count", 0),
                    "responses": p2.get("response_count", 0),
                    "queryable_target_growths": sum(
                        bool(row.get("worldquery_target_grew"))
                        and bool(row.get("worldquery_usable"))
                        and bool(row.get("response_seen"))
                        for row in p2.get("responses", [])
                    ),
                    "aggregate_interval_delta": p2.get("worldquery_known_subchunks_interval_delta", 0),
                    "queryable_target_snapshots": [row["worldquery_after"] for row in p2.get("responses", [])],
                },
                "same_coordinate_query_comparison": {
                    "known_air_reference_before": known_air_before,
                    "known_air_reference_after": known_air_after,
                    "unknown_reference_before": unknown_reference_before,
                    "unknown_reference_after_p1": unknown_reference_after,
                    "known_air_distinct_from_unknown_observed": (
                        known_air_before["block_known"]
                        and bool(known_air_before["block"]["air"])
                        and unknown_reference_before is not None
                        and not unknown_reference_before["block_known"]
                    ),
                },
                "publisher_retention_probes": retention_after,
                "collision_unsupported_targets": [
                    row["worldquery_after"] for row in p2.get("responses", [])
                    if row["worldquery_after"]["block_known"]
                    and row["worldquery_after"]["block"].get("solid") is None
                ],
                "session_id_after": int(bot.get_status().session_id),
                "dimension_after": int(bot.get_status().dimension),
                "publisher_after": state["latest_publisher"],
                "publisher_eviction_observed": bool(publisher_evictions),
            }
            stream["p3_worldquery_usability"] = p3
            stream["request_count"] = len(stream["requests"])
            stream["response_count"] = sum(len(values) for values in state["p2_responses"].values())
            stream["timeouts"] = p2.get("timeouts", [])
            stream["worldquery_growth"] = {
                "initialization": initial_worldquery_count,
                "p1_after": int(p1["worldquery_known_subchunks_after"]),
                "p2_after": int(world.observed_subchunks),
                "p1_delta": int(p1["worldquery_known_subchunks_delta"]),
                "p2_interval_delta": p2.get("worldquery_known_subchunks_interval_delta", 0),
            }
            stream["experiment_conclusion"] = classify_result(p1, p2, p3)
            stream["status"] = "PASS"
            stream["phase"] = "complete"
            stream["cleanup_status"] = "Runtime context owns Bot shutdown; Testbed main owns graceful BDS stop and process-group cleanup"
            results["world_stream_conclusion"] = stream["experiment_conclusion"]
            results["world_stream_status"] = stream["status"]
    except BaseException as error:
        stream["status"] = "FAIL"
        stream["phase"] = active_phase["value"]
        stream["experiment_conclusion"] = "EXPERIMENT_EXECUTION_BLOCKED"
        stream["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if navigator is not None:
            try:
                navigator.stop()
                coast_deadline = min(operation_deadline, loop.time() + 1.5)
                while navigator.controller.state == "coasting" and loop.time() < coast_deadline:
                    await asyncio.sleep(0.05)
                stream["movement_controller_final_state"] = navigator.controller.state
            except Exception as error:
                stream["cleanup_error"] = f"navigator: {type(error).__name__}: {error}"
                stream["status"] = "FAIL"
                stream["experiment_conclusion"] = "EXPERIMENT_EXECUTION_BLOCKED"
        if bot is not None:
            try:
                stream["runtime_final_bot_state"] = str(bot.get_status().state)
                stream["runtime_context_closed_session"] = True
            except Exception as error:
                stream["cleanup_error"] = f"bot: {type(error).__name__}: {error}"
                stream["status"] = "FAIL"
                stream["experiment_conclusion"] = "EXPERIMENT_EXECUTION_BLOCKED"
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception as error:
                stream["cleanup_error"] = f"unsubscribe: {type(error).__name__}: {error}"
                stream["status"] = "FAIL"
                stream["experiment_conclusion"] = "EXPERIMENT_EXECUTION_BLOCKED"
        stream["request_calls_by_phase"] = state["request_calls_by_phase"]
        stream["p2_late_responses"] = state["p2_late_responses"]
        stream["packet_event_count"] = budget["event_count"]
        stream["packet_event_evidence_bytes"] = budget["event_bytes"]
        stream["evidence_budget_exhausted"] = budget["exhausted"]
        stream["coordinate_budget_exhausted"] = state["coordinate_overflow"]
        stream["observer_failure"] = state["observer_failure"]
        stream["elapsed_seconds"] = round(loop.time() - started, 3)
        stream["phase_times_ms"]["experiment_completed"] = round((loop.time() - started) * 1000, 3)
        stream["finished_at"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(stream, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_JSON_BYTES:
            stream["status"] = "FAIL"
            stream["experiment_conclusion"] = "EXPERIMENT_EXECUTION_BLOCKED"
            stream["artifact_size_over_budget"] = True
            stream["observer_errors"] = stream["observer_errors"][:8]
            initialization = stream.get("initialization", {})
            for key in (
                "level_chunks",
                "subchunks_dimension_chunk_x_subchunk_y_chunk_z",
                "explicit_subchunk_responses",
            ):
                values = initialization.get(key)
                if isinstance(values, list) and len(values) > 256:
                    initialization[key] = values[:256]
                    initialization["coordinate_or_response_lists_truncated"] = True
            p1_evidence = stream.get("p1_passive_observation", {})
            for key in (
                "new_levelchunks_dimension_chunk_x_z",
                "new_subchunks_dimension_chunk_x_subchunk_y_chunk_z",
                "new_region_worldquery_snapshots",
            ):
                values = p1_evidence.get(key)
                if isinstance(values, list) and len(values) > 64:
                    p1_evidence[key] = values[:64]
                    p1_evidence["coordinate_or_snapshot_lists_truncated"] = True
            encoded = json.dumps(stream, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_JSON_BYTES:
            compact = {
                "schema_version": 1,
                "experiment_id": stream["experiment_id"],
                "testbed_commit": stream.get("testbed_commit"),
                "endpilot_expected_commit": stream.get("endpilot_expected_commit"),
                "endpilot_installed_commit": stream.get("endpilot_installed_commit"),
                "transport": transport,
                "bds_version": stream.get("bds_version"),
                "endstone_version": stream.get("endstone_version"),
                "protocol_version": PROTOCOL_VERSION,
                "status": "FAIL",
                "experiment_conclusion": "EXPERIMENT_EXECUTION_BLOCKED",
                "artifact_size_over_budget": True,
                "phase": stream.get("phase"),
                "phase_times_ms": stream.get("phase_times_ms"),
                "initialization": {
                    "levelchunk_count": stream.get("initialization", {}).get("levelchunk_count"),
                    "subchunk_count": stream.get("initialization", {}).get("subchunk_count"),
                    "publisher": stream.get("initialization", {}).get("publisher"),
                    "authoritative_position": stream.get("initialization", {}).get("authoritative_position"),
                },
                "p1_passive_observation": stream.get("p1_passive_observation"),
                "p2_bounded_requests": stream.get("p2_bounded_requests"),
                "p3_worldquery_usability": stream.get("p3_worldquery_usability"),
                "requests": stream.get("requests", [])[:16],
                "packet_events": stream.get("packet_events", []),
                "observer_errors": stream.get("observer_errors", [])[:8],
                "request_calls_by_phase": stream.get("request_calls_by_phase"),
                "timeouts": stream.get("timeouts", []),
                "worldquery_growth": stream.get("worldquery_growth"),
                "packet_event_count": budget["event_count"],
                "packet_event_evidence_bytes": budget["event_bytes"],
                "elapsed_seconds": round(loop.time() - started, 3),
                "cleanup_status": stream.get("cleanup_status"),
                "cleanup_error": stream.get("cleanup_error"),
                "limitations": stream.get("limitations", []),
            }
            encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        stream_path = ROOT / "work" / "world_stream.json"
        stream_path.write_bytes(encoded)
        print("WORLD_STREAM " + json.dumps({
            "status": stream["status"],
            "conclusion": stream["experiment_conclusion"],
            "transport": transport,
            "packet_events": stream["packet_event_count"],
            "artifact_bytes": len(encoded),
        }, ensure_ascii=False), flush=True)
        results["world_stream_status"] = stream["status"]
        results["world_stream_conclusion"] = stream["experiment_conclusion"]
    if stream["status"] != "PASS":
        raise RuntimeError("World Stream probe failed a bounded evidence or cleanup check")


def main() -> int:
    scenarios.exercise = exercise
    return testbed.main()


if __name__ == "__main__":
    sys.exit(main())
