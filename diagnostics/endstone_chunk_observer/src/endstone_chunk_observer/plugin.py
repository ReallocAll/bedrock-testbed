"""Read-only, one-shot loaded-chunk snapshots for the isolated A1 probe."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin


TARGET_COLUMNS = tuple((chunk_x, chunk_z) for chunk_z in (-1, 0) for chunk_x in range(6))
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
SNAPSHOT_MARKER = "CHUNKOP_SNAPSHOT "


class ChunkObserverPlugin(Plugin):
    api_version = "0.11"

    commands = {
        "chunkop": {
            "description": "Emit one read-only A1 loaded-chunk snapshot.",
            "usages": ["/chunkop snapshot <request_id>"],
            "permissions": ["chunk-observer.command.snapshot"],
        }
    }

    permissions = {
        "chunk-observer.command.snapshot": {
            "description": "Allow the server console to request an A1 snapshot.",
            "default": "console",
        }
    }

    def on_enable(self) -> None:
        self.logger.info("CHUNKOP_PLUGIN_READY api_version=0.11")

    def on_disable(self) -> None:
        self.logger.info("CHUNKOP_PLUGIN_DISABLED api_version=0.11")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "chunkop":
            return False
        if len(args) != 2 or args[0] != "snapshot" or REQUEST_ID.fullmatch(args[1]) is None:
            sender.send_message("Usage: /chunkop snapshot <32-lowercase-hex-request-id>")
            return True

        request_id = args[1]
        try:
            # The command callback's thread is not part of the public contract.
            # Run the native snapshot synchronously on the server tick thread.
            self.server.scheduler.run_task(
                self,
                lambda: self._emit_snapshot(request_id),
                delay=1,
                period=0,
            )
        except Exception as error:
            self.logger.error(
                SNAPSHOT_MARKER
                + json.dumps(
                    {
                        "request_id": request_id,
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}"[:500],
                    },
                    separators=(",", ":"),
                )
            )
        return True

    def _emit_snapshot(self, request_id: str) -> None:
        try:
            dimension = self.server.level.get_dimension("overworld")
            loaded = {
                (int(chunk.x), int(chunk.z))
                for chunk in dimension.loaded_chunks
                if (int(chunk.x), int(chunk.z)) in TARGET_COLUMNS
            }
            target = set(TARGET_COLUMNS)
            loaded_rows = sorted(loaded)
            missing_rows = sorted(target - loaded)
            record = {
                "request_id": request_id,
                "status": "ok",
                "source": "endstone.dimension.loaded_chunks",
                "sampled_at_utc": datetime.now(timezone.utc).isoformat(),
                "dimension": "overworld",
                "target_columns": [list(column) for column in TARGET_COLUMNS],
                "target_column_states": [
                    {"chunk_x": chunk_x, "chunk_z": chunk_z, "loaded": (chunk_x, chunk_z) in loaded}
                    for chunk_x, chunk_z in TARGET_COLUMNS
                ],
                "loaded_target_columns": [list(column) for column in loaded_rows],
                "missing_target_columns": [list(column) for column in missing_rows],
                "loaded_target_count": len(loaded_rows),
                "all_target_columns_loaded": not missing_rows,
            }
        except Exception as error:
            record = {
                "request_id": request_id,
                "status": "error",
                "source": "endstone.dimension.loaded_chunks",
                "sampled_at_utc": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(error).__name__}: {error}"[:500],
            }
        self.logger.info(SNAPSHOT_MARKER + json.dumps(record, separators=(",", ":")))
