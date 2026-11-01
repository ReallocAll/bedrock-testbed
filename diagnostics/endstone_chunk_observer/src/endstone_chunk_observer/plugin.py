"""One-shot, console-only block read and chunk snapshot calibration."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin

REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
SNAPSHOT_MARKER = "CHUNKOP_SNAPSHOT "
READ_MARKER = "CHUNKOP_READ "


class ChunkObserverPlugin(Plugin):
    api_version = "0.11"
    commands = {
        "chunkop": {
            "description": "One-shot Chunk loading state snapshot.",
            "usages": ["/chunkop snapshot <request_id: str>"],
            "permissions": ["chunk-observer.command.snapshot"],
        },
        "calread": {
            "description": "One-shot, whitelisted block read.",
            "usages": ["/calread <request_id: str> <method: str> <target: str>"],
            "permissions": ["chunk-observer.command.snapshot"],
        },
    }
    permissions = {
        "chunk-observer.command.snapshot": {
            "description": "Console-only calibration commands.", "default": "console"
        }
    }

    def on_enable(self) -> None:
        self.targets = json.loads(os.environ["CALIBRATION_TARGETS_JSON"])
        assert sorted(self.targets) == ["L", "U"]
        for coords in self.targets.values():
            assert len(coords) == 3 and all(type(n) is int for n in coords)
        self.logger.info("CHUNKOP_PLUGIN_READY api_version=0.11")

    def on_disable(self) -> None:
        self.logger.info("CHUNKOP_PLUGIN_DISABLED api_version=0.11")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name == "chunkop":
            if len(args) != 2 or args[0] != "snapshot" or not REQUEST_ID.fullmatch(args[1]):
                return False
            task = lambda: self._snapshot(args[1])
        elif command.name == "calread":
            if (len(args) != 3 or not REQUEST_ID.fullmatch(args[0])
                    or args[1] not in ("handle", "type", "data")
                    or args[2] not in ("L", "U")):
                return False
            task = lambda: self._read(args[0], args[1], args[2])
        else:
            return False
        # Run on main server tick thread; do not read BDS native data in command callback.
        self.server.scheduler.run_task(self, task, delay=1, period=0)
        return True

    def _snapshot(self, request_id: str) -> None:
        try:
            dimension = self.server.level.get_dimension("overworld")
            loaded = {(int(c.x), int(c.z)) for c in dimension.loaded_chunks}
            target_rows = {k: {"chunk": [v[0] // 16, v[2] // 16],
                               "loaded": (v[0] // 16, v[2] // 16) in loaded}
                           for k, v in self.targets.items()}
            record = {"request_id": request_id, "status": "ok", "targets": target_rows,
                      "sampled_at_utc": datetime.now(timezone.utc).isoformat(),
                      "source": "endstone.dimension.loaded_chunks"}
        except Exception as exc:
            record = {"request_id": request_id, "status": "error",
                      "error": f"{type(exc).__name__}: {exc}"[:500]}
        self.logger.info(SNAPSHOT_MARKER + json.dumps(record, separators=(",", ":")))

    def _read(self, request_id: str, method: str, target: str) -> None:
        record = {"request_id": request_id, "method": method, "target": target,
                  "coords": self.targets[target]}
        try:
            dimension = self.server.level.get_dimension("overworld")
            block = dimension.get_block_at(*self.targets[target])
            if method == "handle":
                result = "handle_created"
            elif method == "type":
                result = str(block.type)
            else:
                data = block.data
                result = type(data).__name__
            record.update(status="ok", result=result[:256])
        except Exception as exc:
            record.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
        self.logger.info(READ_MARKER + json.dumps(record, separators=(",", ":")))
