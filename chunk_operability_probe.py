"""Bounded A1 diagnosis of fixed-corridor BDS chunk loading and command results."""
from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import math
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from importlib.metadata import distribution
from pathlib import Path
from urllib.request import urlopen

from server import PLAYER, ROOT, Server, configure


EXPECTED_ENDPILOT_COMMIT = "1d530f0efbb3b0ac03f0945f2f5d89efa81eee3a"
EXPECTED_ENDSTONE = "0.11.14"
EXPECTED_MINECRAFT_VERSION = "26.52"
EXPECTED_PROTOCOL = 2193
EXPECTED_DIAGNOSTIC_BRANCH = "test/chunk-operability"
TARGET_COLUMNS = tuple((chunk_x, chunk_z) for chunk_z in (-1, 0) for chunk_x in range(6))
SNAPSHOT_MARKER = "CHUNKOP_SNAPSHOT "
MAX_DIAGNOSTIC_SECONDS = 120.0
MAX_SNAPSHOTS = 24
MAX_SINGLE_POINTS = 12
MAX_PACKET_EVENTS = 512
MAX_PACKET_EVIDENCE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_CHARS = 8192
SAFE_LANDING_COMMAND = "fill 0 99 0 0 99 0 minecraft:stone"
SINGLE_POINT_TARGETS = tuple(
    (chunk_x, chunk_z, 1 if chunk_x == 0 and block_z == 0 else chunk_x * 16, block_z)
    for chunk_x in range(6)
    for chunk_z, block_z in ((-1, -2), (0, 0))
)
SUCCESS_FILL = re.compile(r"\b(?P<count>\d+)\s+blocks?\s+filled\b", re.I)
SUCCESS_SETBLOCK = re.compile(
    r"\b(?:block\s+(?:placed|set)|successfully\s+(?:placed|set)\s+block)\b", re.I
)
SUCCESS_TP = re.compile(r"\bTeleported\s+EndPilotTest\s+to\b", re.I)
FAILURE_RESPONSE = re.compile(
    r"outside of the world|\bfailed\b|unknown command|syntax error|permission denied|"
    r"not allowed|too many blocks|no targets matched|(?:could not|couldn't)\s+be placed",
    re.I,
)
SNAPSHOT_COMMAND_REJECTION = re.compile(r"\bsyntax error\b|\bunknown command\b", re.I)


class DiagnosticBlocked(RuntimeError):
    def __init__(self, message: str, *, failure_class: str | None = None):
        super().__init__(message)
        self.failure_class = failure_class


def exception_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:1000]


class ObservedRuntimeContext:
    """Delegate Runtime's async context protocol while recording its observed outcome."""

    def __init__(self, context, cleanup: dict):
        self._context = context
        self._cleanup = cleanup

    async def __aenter__(self):
        try:
            value = await self._context.__aenter__()
        except BaseException as error:
            self._cleanup["runtime_context_exit_status"] = "enter_failed"
            self._cleanup["runtime_context_enter_error"] = exception_text(error)
            raise
        self._cleanup["runtime_context_entered"] = True
        self._cleanup["runtime_context_exit_status"] = "not_started"
        return value

    async def __aexit__(self, exc_type, exc, traceback):
        self._cleanup["runtime_context_exit_invoked"] = True
        self._cleanup["runtime_context_exit_status"] = "in_progress"
        try:
            result = await self._context.__aexit__(exc_type, exc, traceback)
        except BaseException as error:
            self._cleanup["runtime_context_exit_status"] = "failed"
            self._cleanup["runtime_context_closed"] = None
            self._cleanup["runtime_context_exit_error"] = exception_text(error)
            if exc is not None:
                self._cleanup["runtime_context_body_error"] = exception_text(exc)
            raise
        self._cleanup["runtime_context_exit_status"] = "completed"
        self._cleanup["runtime_context_closed"] = True
        return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise DiagnosticBlocked("diagnostic_time_budget_exhausted")
    return value


def write_json(path: Path | str, data: dict) -> bool:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    encoded = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    compacted = False
    if len(encoded) > MAX_JSON_BYTES:
        compacted = True
        data["status"] = "BLOCKED"
        data["conclusion"] = "DIAGNOSTIC_EXECUTION_BLOCKED"
        data["error"] = "structured_diagnostic_json_exceeded_2MiB_evidence_compacted"
        encoded = json.dumps(
            {
                "schema_version": data.get("schema_version", 1),
                "status": "BLOCKED",
                "conclusion": "DIAGNOSTIC_EXECUTION_BLOCKED",
                "error": "structured_diagnostic_json_exceeded_2MiB_evidence_compacted",
                "artifact_budget_exceeded": True,
                "artifact_bytes_before_budget_error": len(encoded),
                "transport": data.get("transport"),
                "testbed_commit": data.get("testbed_commit"),
                "endpilot_commit_expected": data.get("endpilot_commit_expected"),
                "endpilot_commit_installed": data.get("endpilot_commit_installed"),
                "evidence_counts": {
                    "snapshots": len(data.get("snapshots", [])),
                    "commands": len(data.get("commands", [])),
                    "authoritative_positions": len(data.get("authoritative_positions", [])),
                },
                "evidence_omitted_due_to_budget": True,
                "safety_gate": {"status": "UNVERIFIED", "reason": "evidence_compacted"},
                "safety_validation": {
                    "status": "FAIL",
                    "error": "safety_evidence_omitted_due_to_budget",
                },
                "ci_validation": {
                    "status": "FAIL",
                    "exit_code": 1,
                    "reason": "diagnostic_artifact_compacted",
                },
                "output_path": "work/chunk_operability.json",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(encoded) > MAX_JSON_BYTES:
            raise RuntimeError("bounded_diagnostic_json_fallback_exceeded_2MiB")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return compacted


def ensure_json_size(data: dict) -> None:
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) >= MAX_JSON_BYTES:
        raise DiagnosticBlocked("structured_diagnostic_json_budget_near_2MiB")


def git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def verify_plugin_wheel(wheel_path: Path) -> dict:
    if not wheel_path.is_file() or wheel_path.suffix != ".whl":
        raise DiagnosticBlocked(f"observer_wheel_missing_or_invalid: {wheel_path}")
    with zipfile.ZipFile(wheel_path) as wheel:
        entry_files = [name for name in wheel.namelist() if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_files) != 1:
            raise DiagnosticBlocked("observer_wheel_must_have_one_entry_points.txt")
        entry_points = configparser.ConfigParser()
        entry_points.read_string(wheel.read(entry_files[0]).decode("utf-8"))
        entries = dict(entry_points.items("endstone")) if entry_points.has_section("endstone") else {}
        if entries.get("chunk-observer") != "endstone_chunk_observer:ChunkObserverPlugin":
            raise DiagnosticBlocked(f"observer_entry_point_mismatch: {entries}")
        metadata_files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise DiagnosticBlocked("observer_wheel_must_have_one_METADATA")
        metadata = Parser().parsestr(wheel.read(metadata_files[0]).decode("utf-8"))
        if metadata.get("Name", "").lower().replace("_", "-") != "endstone-chunk-observer":
            raise DiagnosticBlocked("observer_wheel_distribution_name_mismatch")
        return {
            "file": wheel_path.name,
            "entry_point_group": "endstone",
            "entry_point": entries["chunk-observer"],
            "distribution": metadata.get("Name"),
            "version": metadata.get("Version"),
        }


def command_response(lines: list[str], kind: str) -> str:
    text = "\n".join(lines)
    if FAILURE_RESPONSE.search(text):
        return "failure"
    if kind == "fill":
        fill_match = SUCCESS_FILL.search(text)
        if fill_match:
            return "no_change" if int(fill_match.group("count")) == 0 else "success"
    if kind == "setblock" and SUCCESS_SETBLOCK.search(text):
        return "success"
    if kind == "tp" and SUCCESS_TP.search(text):
        return "success"
    return "pending"


def safe_landing_gate(landing: dict) -> dict:
    count = landing.get("reported_block_count")
    allowed = landing.get("status") == "success" and type(count) is int and count == 1
    if allowed:
        reason = "exactly_one_block_filled"
    elif landing.get("status") != "success":
        reason = f"landing_command_{landing.get('status', 'unknown')}_not_confirmed"
    else:
        reason = "landing_fill_count_not_exactly_one"
    return {
        "status": "ALLOW" if allowed else "BLOCKED",
        "reason": reason,
        "reported_block_count": count,
        "teleport_permitted": allowed,
    }


def validate_safety(diagnostics: dict) -> dict:
    def failed(reason: str) -> dict:
        return {"status": "FAIL", "error": reason}

    def matching_command(event: dict, phase: str, kind: str, command: str) -> dict | None:
        if not isinstance(event, dict) or type(event.get("sequence")) is not int:
            return None
        rows = [row for row in command_rows if row.get("sequence") == event["sequence"]]
        if len(rows) != 1:
            return None
        row = rows[0]
        raw_lines = row.get("raw_lines")
        if any((
            row.get("phase") != phase,
            row.get("kind") != kind,
            row.get("command") != command,
            row.get("status") != event.get("status"),
            row.get("reported_block_count") != event.get("reported_block_count"),
            not isinstance(row.get("raw_text"), str),
            row.get("raw_text") != event.get("raw_text"),
            not isinstance(raw_lines, list),
            any(not isinstance(line, str) for line in raw_lines or []),
            raw_lines != event.get("raw_lines"),
        )):
            return None
        parsed = command_response(raw_lines, kind)
        if kind == "fill":
            count_match = SUCCESS_FILL.search("\n".join(raw_lines))
            raw_count = int(count_match.group("count")) if count_match else None
            if row.get("reported_block_count") != raw_count:
                return None
            successful_count = raw_count if row["status"] == "success" else None
            if row.get("successful_block_count") != successful_count:
                return None
        if row["status"] in {"success", "failure", "no_change"}:
            if parsed != row["status"]:
                return None
        elif row["status"] in {"timeout", "server_stopped"}:
            if parsed != "pending":
                return None
        else:
            return None
        return row

    landing = diagnostics.get("safe_landing")
    recorded_gate = diagnostics.get("safety_gate")
    command_rows = diagnostics.get("commands")
    if not isinstance(landing, dict) or not isinstance(recorded_gate, dict):
        return failed("safe_landing_gate_evidence_missing")
    if not isinstance(command_rows, list) or any(not isinstance(row, dict) for row in command_rows):
        return failed("command_evidence_missing_or_invalid")

    expected_gate = safe_landing_gate(landing)
    if any(recorded_gate.get(key) != value for key, value in expected_gate.items()):
        return failed("safe_landing_gate_evidence_mismatch")
    landing_row = matching_command(landing, "safe_landing_block", "fill", SAFE_LANDING_COMMAND)
    if landing_row is None:
        return failed("safe_landing_command_evidence_missing_or_mismatched")
    if any(type(row.get("sequence")) is not int for row in command_rows):
        return failed("world_mutation_sequence_missing")
    if len({row["sequence"] for row in command_rows}) != len(command_rows):
        return failed("duplicate_world_mutation_sequence")
    if any(row["sequence"] < landing_row["sequence"] for row in command_rows):
        return failed("world_mutation_before_safe_landing_gate")
    later_commands = [row for row in command_rows if row["sequence"] > landing_row["sequence"]]

    if expected_gate["status"] == "BLOCKED":
        teleport = diagnostics.get("teleport", {})
        if not isinstance(teleport, dict):
            return failed("blocked_gate_teleport_skip_evidence_missing")
        if teleport.get("status") != "skipped_safe_landing_not_confirmed":
            return failed("teleport_not_marked_skipped_after_blocked_gate")
        if teleport.get("reason") != expected_gate["reason"] or teleport.get("bds_horizontal_movement") != 0:
            return failed("blocked_gate_teleport_skip_evidence_mismatch")
        if later_commands:
            return failed("world_mutation_after_blocked_safe_landing_gate")
        return {
            "status": "PASS",
            "checks": [
                "safe_landing_gate_blocked_without_exactly_one_block",
                "teleport_skipped",
                "no_later_world_mutations",
            ],
        }

    teleport = diagnostics.get("teleport")
    teleport_row = matching_command(
        teleport, "conditional_teleport", "tp", f"tp {PLAYER} 0.5 100 0.5"
    )
    if teleport_row is None or len(later_commands) < 2:
        return failed("teleport_or_following_command_evidence_missing")
    if teleport_row["sequence"] <= landing_row["sequence"]:
        return failed("teleport_before_safe_landing_gate")
    if teleport_row["status"] != "success":
        return failed("post_gate_command_after_unconfirmed_teleport")

    full_fill = diagnostics.get("full_fill")
    full_row = matching_command(
        full_fill,
        "full_fixed_corridor_fill",
        "fill",
        "fill 0 99 -2 80 99 2 minecraft:stone",
    )
    if full_row is None or full_row["sequence"] <= teleport_row["sequence"]:
        return failed("full_fill_command_evidence_missing_or_mismatched")

    point_rows = [row for row in later_commands if row.get("phase") == "single_point_diagnostic"]
    points = diagnostics.get("single_point_diagnostics", [])
    if not isinstance(points, list) or len(points) != len(point_rows) or len(points) > MAX_SINGLE_POINTS:
        return failed("single_point_command_evidence_count_mismatch")
    point_sequences = [point.get("sequence") for point in points if isinstance(point, dict)]
    if len(point_sequences) != len(points) or len(set(point_sequences)) != len(point_rows):
        return failed("single_point_command_sequence_mismatch")
    if set(point_sequences) != {row["sequence"] for row in point_rows}:
        return failed("single_point_command_sequence_mismatch")
    for point, (chunk_x, chunk_z, block_x, block_z) in zip(points, SINGLE_POINT_TARGETS):
        expected_command = f"setblock {block_x} 99 {block_z} minecraft:stone"
        expected_target = {"x": block_x, "y": 99, "z": block_z}
        if (
            point.get("target_chunk") != [chunk_x, chunk_z]
            or point.get("target_block") != [block_x, 99, block_z]
            or point.get("target") != expected_target
            or point.get("command") != expected_command
            or matching_command(
                point, "single_point_diagnostic", "setblock", expected_command
            ) is None
        ):
            return failed("single_point_command_evidence_missing_or_mismatched")
    if points and (
        full_row["status"] != "failure"
        or any(point["sequence"] <= full_row["sequence"] for point in points)
    ):
        return failed("single_point_diagnostics_without_explicit_full_fill_failure")
    if len(later_commands) != 2 + len(points):
        return failed("unauthorized_world_mutation_after_allowed_gate")
    return {
        "status": "PASS",
        "checks": [
            "safe_landing_gate_allowed_exactly_one_block",
            "teleport_and_fill_command_evidence_matched",
            "post_gate_world_mutations_match_fixed_workflow",
        ],
    }


def cleanup_confirmed(diagnostics: dict) -> bool:
    cleanup = diagnostics.get("cleanup", {})
    body_error = cleanup.get("runtime_context_body_error")
    expected_safe_block = (
        diagnostics.get("conclusion") == "SAFE_LANDING_NOT_CONFIRMED"
        and isinstance(body_error, str)
        and body_error.startswith("DiagnosticBlocked: safe_landing_not_confirmed;")
    )
    runtime_closed = (
        cleanup.get("runtime_context_entered") is True
        and cleanup.get("runtime_context_exit_invoked") is True
        and cleanup.get("runtime_context_closed") is True
        and cleanup.get("runtime_context_exit_status") == "completed"
        and cleanup.get("runtime_context_enter_error") is None
        and cleanup.get("runtime_context_exit_error") is None
        and (body_error is None or expected_safe_block)
    )
    observer_closed = (
        "plugin_install" in diagnostics
        and cleanup.get("observer_disable_hook_observed") is True
    )
    return cleanup.get("bds_graceful_stop") is True and runtime_closed and observer_closed


def ci_exit_code(result: dict, diagnostics: dict) -> int:
    safety = diagnostics.get("safety_validation", {})
    safe_experiment_outcome = (
        diagnostics.get("status") == "PASS" and result.get("status") == "PASS"
    ) or (
        diagnostics.get("status") == "BLOCKED"
        and diagnostics.get("conclusion") == "SAFE_LANDING_NOT_CONFIRMED"
        and diagnostics.get("failure_class") == "SAFE_LANDING_NOT_CONFIRMED"
        and result.get("status") == "BLOCKED"
    )
    return 0 if (
        safety.get("status") == "PASS"
        and safe_experiment_outcome
        and cleanup_confirmed(diagnostics)
    ) else 1


def capture_command_output(lines: list[str]) -> dict:
    text = "\n".join(lines)
    return {
        "raw_lines": lines[-40:],
        "raw_text": text[:MAX_RESPONSE_CHARS],
        "truncated": len(text) > MAX_RESPONSE_CHARS,
    }


async def wait_for_command(
    server: Server, command: str, kind: str, phase: str, deadline: float,
    pre_snapshot_id: str | None, diagnostics: dict, timeout: float = 8.0,
    target: dict | None = None,
) -> dict:
    remaining(deadline)
    submitted = utc_now()
    offset = server.command(command)
    end = min(deadline, time.monotonic() + timeout)
    status = "pending"
    lines: list[str] = []
    while time.monotonic() < end:
        lines = server.snapshot(offset)
        status = command_response(lines, kind)
        if status != "pending":
            break
        if server.process is None or server.process.poll() is not None:
            status = "server_stopped"
            break
        await asyncio.sleep(0.05)
    if status == "pending":
        status = "timeout"
    row = {
        "sequence": len(diagnostics["commands"]) + 1,
        "phase": phase,
        "kind": kind,
        "command": command,
        "submitted_at_utc": submitted,
        "observed_at_utc": utc_now(),
        "status": status,
        "target": target,
        "pre_snapshot_id": pre_snapshot_id,
        **capture_command_output(lines),
    }
    success_count = SUCCESS_FILL.search("\n".join(lines)) if kind == "fill" else None
    if kind == "fill":
        reported_count = int(success_count.group("count")) if success_count else None
        row["reported_block_count"] = reported_count
        row["successful_block_count"] = (
            reported_count if reported_count is not None and status == "success" else None
        )
    diagnostics["commands"].append(row)
    ensure_json_size(diagnostics)
    return row


def target_data(server: Server, offset: int) -> tuple[dict | None, list[str], str]:
    lines = server.snapshot(offset)
    fields = [line.partition("]:")[2].strip() for line in lines]
    begin = next((index for index, value in enumerate(fields) if value.startswith("Target data:")), None)
    if begin is None:
        return None, lines, "target_data_marker_missing"
    end = next((index for index in range(begin + 1, len(fields)) if fields[index] == "]"), None)
    if end is None:
        return None, lines, "target_data_truncated"
    raw = fields[begin].split("Target data:", 1)[1] + " ".join(fields[begin + 1 : end + 1])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, lines, "target_data_unparseable"
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return None, lines, "target_data_unexpected_shape"
    return data[0], lines, "ok"


async def querytarget(server: Server, phase: str, deadline: float, diagnostics: dict) -> dict:
    remaining(deadline)
    submitted = utc_now()
    offset = server.command(f"querytarget {PLAYER}")
    end = min(deadline, time.monotonic() + 5.0)
    entity = None
    lines: list[str] = []
    response_status = "target_data_marker_missing"
    server_stopped = False
    while time.monotonic() < end:
        entity, lines, response_status = target_data(server, offset)
        if entity is not None:
            break
        if server.process is None or server.process.poll() is not None:
            server_stopped = True
            break
        await asyncio.sleep(0.05)
    if entity is not None:
        response_status = "ok"
    elif server_stopped:
        response_status = "server_stopped"
    elif not lines:
        response_status = "timeout"
    row = {
        "sequence": len(diagnostics["authoritative_positions"]) + 1,
        "phase": phase,
        "command": f"querytarget {PLAYER}",
        "submitted_at_utc": submitted,
        "observed_at_utc": utc_now(),
        "raw_lines": lines[-40:],
        "raw_text": "\n".join(lines)[-MAX_RESPONSE_CHARS:],
        "status": response_status,
        "entity": entity,
    }
    diagnostics["authoritative_positions"].append(row)
    ensure_json_size(diagnostics)
    if entity is None:
        raise DiagnosticBlocked(f"querytarget_unavailable:{phase}")
    if entity.get("dimension") != 0:
        raise DiagnosticBlocked(f"querytarget_not_overworld:{phase}:{entity.get('dimension')}")
    if not isinstance(entity.get("position"), dict):
        raise DiagnosticBlocked(f"querytarget_missing_position:{phase}")
    return row


def position_values(row: dict) -> tuple[float, float, float]:
    pos = row["entity"]["position"]
    return float(pos["x"]), float(pos["y"]), float(pos["z"])


def annotate_snapshot_delta(command_row: dict, after_snapshot: dict, diagnostics: dict) -> None:
    before_id = command_row.get("pre_snapshot_id")
    before_snapshot = next(
        (row for row in diagnostics["snapshots"] if row.get("snapshot_id") == before_id), None
    )
    before_record = (before_snapshot or {}).get("plugin_record") or {}
    after_record = after_snapshot.get("plugin_record") or {}
    if before_record.get("status") != "ok" or after_record.get("status") != "ok":
        command_row["loaded_column_delta"] = {"status": "unavailable"}
        return
    before = {tuple(column) for column in before_record.get("loaded_target_columns", [])}
    after = {tuple(column) for column in after_record.get("loaded_target_columns", [])}
    command_row["loaded_column_delta"] = {
        "status": "observed",
        "became_loaded": [list(column) for column in sorted(after - before)],
        "became_unloaded": [list(column) for column in sorted(before - after)],
        "unchanged_loaded": [list(column) for column in sorted(before & after)],
        "unchanged_missing": [list(column) for column in sorted(set(TARGET_COLUMNS) - (before | after))],
    }


async def stable_authoritative_position(
    server: Server, phase: str, deadline: float, diagnostics: dict,
) -> dict:
    previous = await querytarget(server, phase, deadline, diagnostics)
    previous_position = position_values(previous)
    for attempt in range(2):
        await asyncio.sleep(min(0.15, remaining(deadline)))
        current = await querytarget(server, phase, deadline, diagnostics)
        current_position = position_values(current)
        if max(abs(a - b) for a, b in zip(previous_position, current_position)) <= 0.05:
            current["stable_with_previous"] = True
            current["stability_attempt"] = attempt + 1
            return current
        previous, previous_position = current, current_position
    raise DiagnosticBlocked(f"querytarget_position_not_stable:{phase}")


async def request_snapshot(
    server: Server, phase: str, deadline: float, diagnostics: dict,
) -> dict:
    if len(diagnostics["snapshots"]) >= MAX_SNAPSHOTS:
        raise DiagnosticBlocked("server_snapshot_budget_exhausted")
    remaining(deadline)
    request_id = uuid.uuid4().hex
    diagnostics["observer_cleanup"]["one_shot_snapshot_requests"] = (
        diagnostics["observer_cleanup"].get("one_shot_snapshot_requests", 0) + 1
    )
    requested_at = utc_now()
    offset = server.command(f"chunkop snapshot {request_id}")
    end = min(deadline, time.monotonic() + 6.0)
    record = None
    command_rejection = None
    lines: list[str] = []
    while time.monotonic() < end:
        lines = server.snapshot(offset)
        record = matching_snapshot_record(lines, request_id)
        if record is not None:
            break
        command_rejection = next(
            (line for line in lines if SNAPSHOT_COMMAND_REJECTION.search(line)), None
        )
        if command_rejection is not None:
            break
        if server.process is None or server.process.poll() is not None:
            break
        await asyncio.sleep(0.05)

    row = {
        "sequence": len(diagnostics["snapshots"]) + 1,
        "snapshot_id": request_id,
        "request_id": request_id,
        "phase": phase,
        "source": "Endstone 0.11.14 Dimension.loaded_chunks",
        "requested_at_utc": requested_at,
        "received_at_utc": utc_now(),
        "plugin_status": record.get("status") if record else "no_matching_response",
        "plugin_record": record,
        "console_lines": lines[-20:],
    }
    diagnostics["snapshots"].append(row)
    ensure_json_size(diagnostics)
    if record is None:
        if command_rejection is not None:
            raise DiagnosticBlocked(
                f"snapshot_command_rejected:{phase}:{command_rejection}",
                failure_class="COMMAND_REGISTRATION_FAILED",
            )
        raise DiagnosticBlocked(f"loaded_chunks_snapshot_timeout:{phase}")
    if record.get("status") != "ok":
        failure_class = {
            "scheduler_submission": "SNAPSHOT_SCHEDULER_FAILED",
            "loaded_chunks_read": "SERVER_LOAD_STATE_READ_FAILED",
        }.get(record.get("stage"), "SNAPSHOT_CALLBACK_FAILED")
        raise DiagnosticBlocked(
            f"loaded_chunks_snapshot_error:{phase}:{record.get('error')}",
            failure_class=failure_class,
        )
    expected_columns = [list(column) for column in TARGET_COLUMNS]
    if record.get("target_columns") != expected_columns:
        raise DiagnosticBlocked(f"loaded_chunks_target_set_mismatch:{phase}")
    loaded = {tuple(column) for column in record.get("loaded_target_columns", [])}
    missing = {tuple(column) for column in record.get("missing_target_columns", [])}
    if loaded & missing or loaded | missing != set(TARGET_COLUMNS):
        raise DiagnosticBlocked(f"loaded_chunks_snapshot_incomplete:{phase}")
    if record.get("loaded_target_count") != len(loaded):
        raise DiagnosticBlocked(f"loaded_chunks_count_mismatch:{phase}")
    if record.get("all_target_columns_loaded") is not (not missing):
        raise DiagnosticBlocked(f"loaded_chunks_completeness_flag_mismatch:{phase}")
    target_states = [
        {"chunk_x": chunk_x, "chunk_z": chunk_z, "loaded": (chunk_x, chunk_z) in loaded}
        for chunk_x, chunk_z in TARGET_COLUMNS
    ]
    if record.get("target_column_states") != target_states:
        raise DiagnosticBlocked(f"loaded_chunks_per_column_state_mismatch:{phase}")
    diagnostics["observer_cleanup"]["one_shot_snapshot_responses"] = (
        diagnostics["observer_cleanup"].get("one_shot_snapshot_responses", 0) + 1
    )
    row["loaded_target_count"] = record.get("loaded_target_count")
    row["all_target_columns_loaded"] = record.get("all_target_columns_loaded")
    row["missing_target_columns"] = record.get("missing_target_columns")
    row["target_column_states"] = target_states
    return row


def matching_snapshot_record(lines: list[str], request_id: str) -> dict | None:
    for line in lines:
        marker_index = line.find(SNAPSHOT_MARKER)
        if marker_index < 0:
            continue
        try:
            candidate = json.loads(line[marker_index + len(SNAPSHOT_MARKER) :].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("request_id") == request_id:
            return candidate
    return None


async def wait_plugin_ready(server: Server, deadline: float) -> None:
    end = min(deadline, time.monotonic() + 8.0)
    while time.monotonic() < end:
        if any("CHUNKOP_PLUGIN_READY api_version=0.11" in line for line in server.snapshot()):
            return
        if server.process is None or server.process.poll() is not None:
            break
        await asyncio.sleep(0.05)
    raise DiagnosticBlocked("observer_plugin_ready_marker_missing")


def client_status(bot) -> dict:
    status = bot.get_status()
    return {
        "state": getattr(status.state, "name", str(status.state)),
        "session_id": int(status.session_id),
        "dimension": int(status.dimension),
        "chunk_radius": int(status.chunk_radius),
        "position": {
            "x": float(status.position.x),
            "y": float(status.position.y),
            "z": float(status.position.z),
        },
    }


async def run_diagnostic(server: Server, diagnostics: dict, transport: str, deadline: float) -> None:
    from endpilot import BotOptions, Runtime

    endpoint = "http://127.0.0.1:19132" if transport == "nethernet" else "raknet://127.0.0.1:19132"
    async with ObservedRuntimeContext(Runtime(), diagnostics["cleanup"]) as runtime:
        bot = runtime.add_bot(
            BotOptions(
                server_url=endpoint,
                transport=transport,
                protocol=EXPECTED_PROTOCOL,
                name=PLAYER,
                chunk_radius=8,
                client_cache=False,
                validate_all_packets=True,
            )
        )
        bot_timeout = min(70.0, remaining(deadline))
        await bot.wait_ready(bot_timeout)
        diagnostics["client_observation"].update(client_status(bot))
        diagnostics["session"] = diagnostics["client_observation"]["session_id"]
        diagnostics["checks"].append("endpilot_session_ready")

        online_timeout = min(20.0, remaining(deadline))
        online_lines = await asyncio.to_thread(server.wait_player, True, online_timeout)
        diagnostics["player_online"] = {"online": True, "raw_lines": online_lines[-20:]}
        diagnostics["checks"].append("bds_player_online")

        initial = await stable_authoritative_position(server, "initial", deadline, diagnostics)
        initial_status = client_status(bot)
        if initial_status["dimension"] != 0:
            raise DiagnosticBlocked(f"client_not_in_overworld:{initial_status['dimension']}")
        if initial_status["session_id"] != diagnostics["client_observation"]["session_id"]:
            raise DiagnosticBlocked("session_changed_during_initial_observation")
        diagnostics["initial_position"] = {
            "bds_querytarget": initial["entity"],
            "client_status": initial_status,
            "stable": True,
        }
        diagnostics["checks"].append("initial_bds_authoritative_position_stable")

        await wait_plugin_ready(server, deadline)
        diagnostics["checks"].append("observer_plugin_ready")

        baseline = await request_snapshot(server, "initial", deadline, diagnostics)
        diagnostics["initial_snapshot_id"] = baseline["snapshot_id"]
        diagnostics["client_observation"].update(
            {
                "publisher": "not_collected: bounded A1 does not attach a packet decoder",
                "level_chunk_coordinates": "not_collected: bounded A1 does not attach a packet decoder",
                "subchunk_requests_sent": 0,
                "chunk_radius_changed": False,
            }
        )

        landing = await wait_for_command(
            server,
            SAFE_LANDING_COMMAND,
            "fill",
            "safe_landing_block",
            deadline,
            baseline["snapshot_id"],
            diagnostics,
            target={"x": [0, 0], "y": [99, 99], "z": [0, 0]},
        )
        diagnostics["safe_landing"] = landing
        gate = safe_landing_gate(landing)
        diagnostics["safety_gate"] = gate
        if gate["status"] == "BLOCKED":
            diagnostics["teleport"] = {
                "status": "skipped_safe_landing_not_confirmed",
                "reason": gate["reason"],
                "bds_horizontal_movement": 0,
            }
        post_landing = await request_snapshot(server, "after_safe_landing", deadline, diagnostics)
        landing["post_snapshot_id"] = post_landing["snapshot_id"]
        annotate_snapshot_delta(landing, post_landing, diagnostics)

        post_tp = post_landing
        if gate["status"] == "ALLOW":
            tp = await wait_for_command(
                server,
                f"tp {PLAYER} 0.5 100 0.5",
                "tp",
                "conditional_teleport",
                deadline,
                post_landing["snapshot_id"],
                diagnostics,
                target={"entity": PLAYER, "position": [0.5, 100, 0.5]},
            )
            diagnostics["teleport"] = tp
            post_tp = await request_snapshot(server, "after_teleport_command", deadline, diagnostics)
            tp["post_snapshot_id"] = post_tp["snapshot_id"]
            annotate_snapshot_delta(tp, post_tp, diagnostics)
            if tp["status"] != "success":
                raise DiagnosticBlocked(f"conditional_teleport_not_confirmed:{tp['status']}")
            stable_after_tp = await stable_authoritative_position(
                server, "after_teleport", deadline, diagnostics
            )
            target_position = position_values(stable_after_tp)
            bot_after_tp = client_status(bot)
            session_before = diagnostics["client_observation"]["session_id"]
            if bot_after_tp["session_id"] != session_before or bot_after_tp["dimension"] != 0:
                raise DiagnosticBlocked("session_or_dimension_changed_after_teleport")
            client_position = bot_after_tp["position"]
            if math.hypot(client_position["x"] - target_position[0], client_position["z"] - target_position[2]) > 0.1:
                raise DiagnosticBlocked(f"client_bds_horizontal_position_divergence:{client_position}:{target_position}")
            if abs(client_position["y"] - target_position[1]) > 0.5:
                raise DiagnosticBlocked(f"client_bds_vertical_position_divergence:{client_position}:{target_position}")
            if math.hypot(target_position[0] - 0.5, target_position[2] - 0.5) > 0.1:
                raise DiagnosticBlocked(f"bds_teleport_position_mismatch:{target_position}")
            if abs(target_position[1] - 101.62001) > 0.5:
                raise DiagnosticBlocked(f"bds_teleport_height_unstable:{target_position}")
            diagnostics["teleport_authority"] = {
                "bds_querytarget": stable_after_tp["entity"],
                "client_status": bot_after_tp,
                "session_unchanged": True,
                "dimension_unchanged": True,
                "position_stable": True,
                "client_position_matches_bds_authority": True,
            }
            diagnostics["checks"].append("conditional_teleport_authority_confirmed")
        else:
            raise DiagnosticBlocked(
                f"safe_landing_not_confirmed;_later_stages_skipped:{gate['reason']}",
                failure_class="SAFE_LANDING_NOT_CONFIRMED",
            )

        full_fill = await wait_for_command(
            server,
            "fill 0 99 -2 80 99 2 minecraft:stone",
            "fill",
            "full_fixed_corridor_fill",
            deadline,
            post_tp["snapshot_id"],
            diagnostics,
            target={"x": [0, 80], "y": [99, 99], "z": [-2, 2]},
        )
        diagnostics["full_fill"] = full_fill
        post_fill = await request_snapshot(server, "after_full_fill", deadline, diagnostics)
        full_fill["post_snapshot_id"] = post_fill["snapshot_id"]
        annotate_snapshot_delta(full_fill, post_fill, diagnostics)

        single_points = []
        if full_fill["status"] == "failure":
            for chunk_x, chunk_z, block_x, block_z in SINGLE_POINT_TARGETS[:MAX_SINGLE_POINTS]:
                remaining(deadline)
                pre_snapshot_id = diagnostics["snapshots"][-1]["snapshot_id"]
                command = f"setblock {block_x} 99 {block_z} minecraft:stone"
                outcome = await wait_for_command(
                    server,
                    command,
                    "setblock",
                    "single_point_diagnostic",
                    deadline,
                    pre_snapshot_id,
                    diagnostics,
                    timeout=4.0,
                    target={"x": block_x, "y": 99, "z": block_z},
                )
                outcome["target_chunk"] = [chunk_x, chunk_z]
                outcome["target_block"] = [block_x, 99, block_z]
                post_point = await request_snapshot(
                    server,
                    f"after_single_point_{chunk_x}_{chunk_z}",
                    deadline,
                    diagnostics,
                )
                outcome["post_snapshot_id"] = post_point["snapshot_id"]
                annotate_snapshot_delta(outcome, post_point, diagnostics)
                single_points.append(outcome)
                diagnostics["single_point_diagnostics"] = single_points
                write_json(diagnostics["output_path"], diagnostics)
                if outcome["status"] in ("timeout", "server_stopped"):
                    break
        diagnostics["single_point_diagnostics"] = single_points

        final_authority = await stable_authoritative_position(
            server, "final", deadline, diagnostics
        )
        final_client_status = client_status(bot)
        if final_client_status["session_id"] != diagnostics["client_observation"]["session_id"]:
            raise DiagnosticBlocked("session_changed_before_diagnostic_end")
        diagnostics["final_position"] = {
            "bds_querytarget": final_authority["entity"],
            "client_status": final_client_status,
            "stable": True,
        }
        diagnostics["checks"].append("final_bds_authoritative_position_stable")


def classify(diagnostics: dict) -> tuple[str, str, list[str]]:
    if validate_safety(diagnostics).get("status") != "PASS":
        return "BLOCKED", "SAFETY_EVIDENCE_UNVERIFIED", []
    gate = diagnostics.get("safety_gate")
    if gate["status"] != "ALLOW":
        return "BLOCKED", "SAFE_LANDING_NOT_CONFIRMED", []

    full = diagnostics.get("full_fill", {}).get("status")
    snapshots = diagnostics.get("snapshots", [])
    if not snapshots or any(row.get("plugin_status") != "ok" for row in snapshots):
        return "BLOCKED", "LOAD_STATE_UNVERIFIED", []

    labels = []
    if full == "success":
        return "PASS", "SCENE_COMMAND_OPERABLE_OBSERVED", labels
    if full in ("timeout", "server_stopped", None):
        return "BLOCKED", "DIAGNOSTIC_EXECUTION_BLOCKED", labels
    if full == "no_change":
        return "BLOCKED", "FULL_FILL_NO_CHANGE", labels
    if full != "failure":
        return "BLOCKED", "DIAGNOSTIC_EXECUTION_BLOCKED", labels

    before_fill_id = diagnostics.get("full_fill", {}).get("pre_snapshot_id")
    before_fill = next((row for row in snapshots if row["snapshot_id"] == before_fill_id), None)
    loaded_before = (before_fill or {}).get("all_target_columns_loaded")
    if loaded_before is False:
        labels.append("LOAD_INCOMPLETE_WITH_FILL_REJECTION")
    elif loaded_before is True:
        labels.append("LOADED_BUT_FILL_REJECTED")

    points = diagnostics.get("single_point_diagnostics", [])
    if any(row.get("status") == "success" for row in points):
        labels.append("SINGLE_POINT_AND_RANGE_DIVERGENCE")
    if not labels:
        return "FAIL", "INCONCLUSIVE", labels
    priority = (
        "LOAD_INCOMPLETE_WITH_FILL_REJECTION",
        "LOADED_BUT_FILL_REJECTED",
        "SINGLE_POINT_AND_RANGE_DIVERGENCE",
    )
    primary = next(label for label in priority if label in labels)
    return "PASS", primary, labels


def make_diagnostics(args, plugin_meta: dict, installed_commit: str | None) -> dict:
    return {
        "schema_version": 1,
        "status": "BLOCKED",
        "conclusion": "DIAGNOSTIC_EXECUTION_BLOCKED",
        "transport": args.transport,
        "testbed_commit": git_head(),
        "diagnostic_branch_expected": EXPECTED_DIAGNOSTIC_BRANCH,
        "endstone_version_expected": EXPECTED_ENDSTONE,
        "bds_version_expected": "1.26.52.3 (26.52 series)",
        "minecraft_version_series_expected": EXPECTED_MINECRAFT_VERSION,
        "loaded_chunks_evidence_level": "SOURCE_VERIFIED_ENDSTONE / NATIVE_IMPLEMENTATION_UNVERIFIED",
        "loaded_chunks_semantics": (
            "Endstone 0.11.14 Dimension.loaded_chunks enumerates storage entries whose native chunk state "
            "compares >= ChunkState.Loaded. The target BDS ChunkSource::getStorage implementation and "
            "command-time loaded state are not independently proven; Invalid-state filtering and TOCTOU "
            "remain limitations. Treat this as an API-reported observation, not an atomic operability guarantee."
        ),
        "protocol_version": EXPECTED_PROTOCOL,
        "endpilot_commit_expected": args.endpilot_commit,
        "endpilot_commit_installed": installed_commit,
        "plugin_wheel": plugin_meta,
        "python_client": sys.version.split()[0],
        "session": None,
        "dimension": "overworld",
        "scene": {
            "floor": {"x": [0, 80], "y": 99, "z": [-2, 2]},
            "walkway": {"x": [0, 64], "y": 100, "z": [0, 0]},
            "target_chunk_columns": [list(column) for column in TARGET_COLUMNS],
        },
        "started_at_utc": utc_now(),
        "finished_at_utc": None,
        "budget": {
            "diagnostic_seconds": MAX_DIAGNOSTIC_SECONDS,
            "snapshots_max": MAX_SNAPSHOTS,
            "snapshots_used": 0,
            "target_columns": [list(column) for column in TARGET_COLUMNS],
            "single_point_operations_max": MAX_SINGLE_POINTS,
            "packet_events_max": MAX_PACKET_EVENTS,
            "packet_events_used": 0,
            "structured_packet_evidence_bytes_max": MAX_PACKET_EVIDENCE_BYTES,
            "structured_packet_evidence_bytes_used": 0,
            "structured_json_bytes_max": MAX_JSON_BYTES,
            "horizontal_movement_intentionally_requested": 0,
            "subchunk_requests": 0,
        },
        "client_observation": {
            "status": "limited_to_session_status",
            "publisher": None,
            "level_chunk_coordinates": None,
        },
        "checks": [],
        "snapshots": [],
        "commands": [],
        "safety_gate": None,
        "safety_validation": {"status": "NOT_RUN", "checks": []},
        "ci_validation": {"status": "NOT_RUN", "exit_code": 1},
        "authoritative_positions": [],
        "single_point_diagnostics": [],
        "observer_cleanup": {
            "resources_registered": 0,
            "background_tasks": 0,
            "one_shot_snapshot_requests": 0,
            "one_shot_snapshot_responses": 0,
        },
        "cleanup": {
            "bds_graceful_stop": None,
            "observer_disable_hook_observed": None,
            "runtime_context_entered": False,
            "runtime_context_exit_invoked": False,
            "runtime_context_closed": None,
            "runtime_context_exit_status": "not_entered",
            "runtime_context_enter_error": None,
            "runtime_context_exit_error": None,
            "runtime_context_body_error": None,
        },
        "output_path": "work/chunk_operability.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--transport", choices=("raknet", "nethernet"), required=True)
    parser.add_argument("--plugin-wheel", type=Path, required=True)
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    folder = work / "server"
    logfile = work / "bds.log"
    plugin_dir = folder / "plugins"
    diagnostics_path = work / "chunk_operability.json"
    result_path = work / "result.json"
    server = None
    diagnostics = None
    result = {
        "status": "BLOCKED",
        "diagnostic_conclusion": "DIAGNOSTIC_EXECUTION_BLOCKED",
        "endstone": EXPECTED_ENDSTONE,
        "bds": f"{EXPECTED_MINECRAFT_VERSION} series",
        "protocol": EXPECTED_PROTOCOL,
        "transport": args.transport,
        "python_client": sys.version.split()[0],
        "endpilot_commit_expected": args.endpilot_commit,
        "installed_endpilot_commit": None,
        "checks": [],
        "timestamp": utc_now(),
    }
    try:
        if args.endpilot_commit != EXPECTED_ENDPILOT_COMMIT:
            raise DiagnosticBlocked("workflow_endpilot_commit_differs_from_fixed_a1_sha")
        direct_url = distribution("endpilot").read_text("direct_url.json")
        if direct_url is None:
            raise DiagnosticBlocked("EndPilot PEP 610 direct_url.json is missing")
        installed_commit = json.loads(direct_url).get("vcs_info", {}).get("commit_id")
        result["installed_endpilot_commit"] = installed_commit
        if installed_commit != args.endpilot_commit:
            raise DiagnosticBlocked(f"EndPilot commit mismatch: {installed_commit!r}")
        result["checks"].append("endpilot_pep610_sha_verified")

        plugin_meta = verify_plugin_wheel(args.plugin_wheel.resolve())
        diagnostics = make_diagnostics(args, plugin_meta, installed_commit)
        write_json(diagnostics_path, diagnostics)

        server = Server(args.server_python.absolute(), folder, logfile)
        server.start()
        server.wait_ready()
        result["checks"].append("bds_bootstrap_and_endstone_version_verified")
        server.stop()
        server = None

        configure(folder / "server.properties", args.transport)
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        installed_wheel = plugin_dir / args.plugin_wheel.name
        shutil.copy2(args.plugin_wheel, installed_wheel)
        diagnostics["plugin_install"] = {
            "wheel": args.plugin_wheel.name,
            "server_plugins_path": str(installed_wheel.relative_to(ROOT)),
            "copied_after_bootstrap": True,
        }

        server = Server(args.server_python.absolute(), folder, logfile)
        server.start()
        server.wait_ready(timeout=420.0)
        phase_started = time.monotonic()
        deadline = phase_started + MAX_DIAGNOSTIC_SECONDS
        diagnostics["started_at_utc"] = utc_now()
        diagnostics["checks"].append("configured_bds_ready")
        result["checks"].append("configured_bds_ready")
        if args.transport == "nethernet":
            with urlopen("http://127.0.0.1:19132/v1/join", timeout=min(8.0, remaining(deadline))) as response:
                signaling_status = response.status
            if not 200 <= signaling_status < 300:
                raise DiagnosticBlocked(f"nethernet_signaling_rejected:{signaling_status}")
            diagnostics["nethernet_signaling"] = {"status": signaling_status}

        asyncio.run(run_diagnostic(server, diagnostics, args.transport, deadline))
        result["checks"].extend(diagnostics["checks"])
        status, conclusion, labels = classify(diagnostics)
        diagnostics["status"] = status
        diagnostics["conclusion"] = conclusion
        diagnostics["labels"] = labels
        diagnostics["finished_at_utc"] = utc_now()
        diagnostics["budget"]["snapshots_used"] = len(diagnostics["snapshots"])
        ensure_json_size(diagnostics)
        result["status"] = status
        result["diagnostic_conclusion"] = conclusion
        result["diagnostic_labels"] = labels
    except BaseException as error:
        if diagnostics is None:
            diagnostics = make_diagnostics(args, {}, result.get("installed_endpilot_commit"))
        diagnostics["status"] = "BLOCKED"
        failure_class = getattr(error, "failure_class", None)
        if failure_class is not None:
            diagnostics["failure_class"] = failure_class
            result["failure_class"] = failure_class
        error_text = str(error).lower()
        if failure_class == "SAFE_LANDING_NOT_CONFIRMED":
            diagnostics["conclusion"] = "SAFE_LANDING_NOT_CONFIRMED"
        else:
            diagnostics["conclusion"] = (
                "LOAD_STATE_UNVERIFIED"
                if failure_class in {
                    "COMMAND_REGISTRATION_FAILED",
                    "SNAPSHOT_SCHEDULER_FAILED",
                    "SERVER_LOAD_STATE_READ_FAILED",
                    "SNAPSHOT_CALLBACK_FAILED",
                } or "loaded_chunks" in error_text or "observer_plugin_ready_marker_missing" in error_text
                else "DIAGNOSTIC_EXECUTION_BLOCKED"
            )
        diagnostics["error"] = (
            diagnostics["cleanup"].get("runtime_context_body_error") or exception_text(error)
        )
        diagnostics["finished_at_utc"] = utc_now()
        diagnostics["budget"]["snapshots_used"] = len(diagnostics["snapshots"])
        result["status"] = "BLOCKED"
        result["diagnostic_conclusion"] = diagnostics["conclusion"]
        result["error"] = diagnostics["error"]
    finally:
        if server is not None and server.process is not None:
            try:
                server.stop()
                result.setdefault("checks", []).append("bds_graceful_stop_confirmed")
                if diagnostics is not None:
                    diagnostics["cleanup"]["bds_graceful_stop"] = True
                    observer_installed = "plugin_install" in diagnostics
                    disable_observed = (
                        any("CHUNKOP_PLUGIN_DISABLED" in line for line in server.snapshot())
                        if observer_installed
                        else None
                    )
                    diagnostics["cleanup"]["observer_disable_hook_observed"] = disable_observed
                    if observer_installed and disable_observed:
                        diagnostics["checks"].append("observer_plugin_disable_hook_confirmed")
                        result["checks"].append("observer_plugin_disable_hook_confirmed")
                    elif observer_installed:
                        diagnostics["status"] = "FAIL"
                        diagnostics["conclusion"] = "DIAGNOSTIC_EXECUTION_BLOCKED"
                        cleanup_error = "observer_plugin_disable_hook_marker_missing"
                        diagnostics["cleanup"]["observer_disable_error"] = cleanup_error
                        diagnostics.setdefault("error", cleanup_error)
                        result["status"] = "FAIL"
                        result["cleanup_error"] = cleanup_error
            except BaseException as error:
                result["status"] = "FAIL"
                result["cleanup_error"] = f"{type(error).__name__}: {error}"[:1000]
                if diagnostics is not None:
                    diagnostics["status"] = "FAIL"
                    diagnostics["conclusion"] = "DIAGNOSTIC_EXECUTION_BLOCKED"
                    diagnostics["cleanup"]["bds_graceful_stop"] = False
                    diagnostics["cleanup"]["bds_stop_error"] = result["cleanup_error"]
        result["timestamp_finished"] = utc_now()
        result["checks"] = list(dict.fromkeys(result.get("checks", [])))
        result["snapshot_count"] = len(diagnostics["snapshots"]) if diagnostics is not None else 0
        result["duration_seconds"] = (
            round(time.monotonic() - phase_started, 3) if "phase_started" in locals() else None
        )
        result["bds_log"] = str(logfile.relative_to(ROOT)) if logfile.exists() else None
        result["version_file"] = str((folder / "version.txt").relative_to(ROOT)) if (folder / "version.txt").exists() else None
        result["server_properties"] = str((folder / "server.properties").relative_to(ROOT)) if (folder / "server.properties").exists() else None
        result["status"] = diagnostics.get("status", result["status"]) if diagnostics is not None else result["status"]
        result["diagnostic_conclusion"] = (
            diagnostics.get("conclusion", result["diagnostic_conclusion"])
            if diagnostics is not None else result["diagnostic_conclusion"]
        )
        if diagnostics is not None:
            diagnostics["finished_at_utc"] = diagnostics.get("finished_at_utc") or utc_now()
            diagnostics["budget"]["snapshots_used"] = len(diagnostics["snapshots"])
            diagnostics["safety_validation"] = validate_safety(diagnostics)
        result["safety_validation"] = (
            diagnostics.get("safety_validation", {"status": "NOT_RUN", "checks": []})
            if diagnostics is not None else {"status": "NOT_RUN", "checks": []}
        )
        result["safety_gate"] = diagnostics.get("safety_gate") if diagnostics is not None else None
        exit_code = ci_exit_code(result, diagnostics) if diagnostics is not None else 1
        result["ci_status"] = "PASS" if exit_code == 0 else "FAIL"
        result["ci_exit_code"] = exit_code
        if diagnostics is not None:
            diagnostics["ci_validation"] = {
                "status": result["ci_status"],
                "exit_code": exit_code,
                "cleanup_confirmed": cleanup_confirmed(diagnostics),
            }
            compacted = write_json(diagnostics_path, diagnostics)
            if compacted:
                exit_code = 1
                result["status"] = diagnostics["status"]
                result["diagnostic_conclusion"] = diagnostics["conclusion"]
                result["safety_gate"] = {"status": "UNVERIFIED", "reason": "evidence_compacted"}
                result["safety_validation"] = {
                    "status": "FAIL",
                    "error": "safety_evidence_omitted_due_to_budget",
                }
                result["ci_status"] = "FAIL"
                result["ci_exit_code"] = exit_code
        write_json(result_path, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
