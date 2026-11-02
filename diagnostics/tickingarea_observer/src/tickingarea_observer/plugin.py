"""Isolated Endstone 0.11 diagnostic. Block reads are post-intervention only."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin

RID = re.compile(r"[0-9a-f]{32}\Z")
MARK = "TAL_OBSERVER "


class TickingAreaObserver(Plugin):
    api_version = "0.11"
    commands = {
        "talobs": {
            "description": "One-shot passive chunk snapshot or post-Fill block read",
            "usages": ["/talobs snapshot <request_id: str>", "/talobs postblock <request_id: str>"],
            "permissions": ["talobs.command"],
        }
    }
    permissions = {"talobs.command": {"description": "Console-only diagnostic", "default": "console"}}

    def on_enable(self) -> None:
        self.logger.info("TAL_OBSERVER_READY")

    def on_disable(self) -> None:
        self.logger.info("TAL_OBSERVER_DISABLED")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "talobs":
            return False
        if (len(args) != 2 or args[0] not in ("snapshot", "postblock")
                or RID.fullmatch(args[1]) is None):
            sender.send_message("Invalid diagnostic request")
            return True
        operation, rid = args
        try:
            self.server.scheduler.run_task(self, lambda: self._observe(operation, rid), delay=1, period=0)
        except Exception as exc:
            self._emit({"request_id": rid, "operation": operation, "status": "error",
                        "error": f"submission:{type(exc).__name__}:{exc}"[:400]})
        return True

    def _emit(self, value: dict) -> None:
        value["sampled_at_utc"] = datetime.now(timezone.utc).isoformat()
        self.logger.info(MARK + json.dumps(value, separators=(",", ":"), sort_keys=True))

    def _observe(self, operation: str, rid: str) -> None:
        result = {"request_id": rid, "operation": operation, "status": "error"}
        try:
            dim = self.server.level.get_dimension("overworld")
            # loaded_chunks uses an existing collection; no get_block_at on this path.
            loaded = any(int(c.x) == 0 and int(c.z) == 0 for c in dim.loaded_chunks)
            result.update({"source": "endstone.dimension.loaded_chunks", "chunk_x": 0,
                           "chunk_z": 0, "loaded": bool(loaded), "status": "ok"})
            if operation == "postblock":
                # Intentionally after Fill: may initialize/load target; not a prestate oracle.
                result["block_type"] = str(dim.get_block_at(0, 99, 0).type)
                result["block_read_intrusive"] = True
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        self._emit(result)
