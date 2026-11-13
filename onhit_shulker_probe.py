"""Continue the passive BDS test with a preserved-world endstone-bot trigger.

No native detours. All writes are confined to work-onhit/server on an
ephemeral, isolated GitHub-hosted standard runner.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from server import ROOT, Server

BOT_COMMIT = "4bd0691f1ef0f551e5ba258d4747b35a3b748177"
BOT_NAME = "OnHitTarget"
BULLET_MARKER = "ONHIT_BULLET_OBSERVED"


def wait_for_log(server: Server, expected: str, *, timeout: float,
                 start: int = 0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = server.snapshot(start)
        if any(expected in line for line in lines):
            return lines
        if server.process is None or server.process.poll() is not None:
            raise RuntimeError(f"BDS stopped while waiting for {expected!r}")
        time.sleep(0.25)
    raise TimeoutError(f"BDS did not log {expected!r}; last={server.snapshot(start)[-10:]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    args = parser.parse_args()
    python = args.server_python.absolute()
    work = ROOT / "work-onhit"
    folder = work / "server"
    logfile = work / "bds.log"
    result = {
        "status": "FAIL",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "bds": "1.26.60.6", "endstone": "0.11.15",
        "endstone_bot_commit": BOT_COMMIT,
        "onhit_hooks_enabled": False, "checks": [],
    }
    current: Server | None = None

    def start() -> Server:
        server = Server(python, folder, logfile,
                        expected_endstone="0.11.15", expected_bds="26.60")
        server.start()
        server.wait_ready()
        return server

    try:
        if not (work / "result.json").is_file():
            raise RuntimeError("Passive OnHit acceptance must pass first")
        passive = json.loads((work / "result.json").read_text())
        if passive.get("status") != "PASS" or passive.get("hooks_enabled") is not False:
            raise RuntimeError("Passive OnHit prerequisite did not pass")
        if not (folder / "plugins" / "endstone_onhit_lifecycle.so").is_file():
            raise RuntimeError("Pinned passive native plugin is missing")

        # The first bot-enabled start installs its behavior pack and emits the
        # exact-world offline patch script. Never delete this world afterward.
        current = start()
        wait_for_log(current, "Bot 已启用：SimulatedPlayer only", timeout=15)
        initial = "\n".join(current.snapshot())
        if "Loading bot" not in initial and "Enabling bot" not in initial:
            raise RuntimeError("endstone-bot entrypoint was not loaded")
        patch = folder / "plugins" / "bot" / "enable_beta.py"
        if not patch.is_file():
            raise RuntimeError("Bot did not create plugins/bot/enable_beta.py")
        result["checks"].append("behavior_pack_and_beta_script_deployed")
        current.stop()
        current = None
        result["checks"].append("first_bot_boot_clean_stop")

        worlds = sorted((folder / "worlds").glob("*/level.dat"))
        if len(worlds) != 1:
            raise RuntimeError(f"Expected exactly one generated world: {worlds}")
        world = worlds[0]
        result["world"] = world.parent.name
        subprocess.run([str(python), str(patch)], check=True,
                       cwd=ROOT, timeout=30)
        if not world.is_file():
            raise RuntimeError("The original world was lost after the offline patch")
        result["checks"].append("beta_api_offline_patch_same_world")

        properties = folder / "server.properties"
        before = properties.read_text()
        if "difficulty=peaceful" not in before:
            raise RuntimeError("Unexpected isolated server difficulty")
        properties.write_text(before.replace("difficulty=peaceful", "difficulty=normal"))
        result["checks"].append("normal_difficulty_configured")

        current = start()
        wait_for_log(current, "standalone SimulatedPlayer 功能可用", timeout=50)
        status_offset = current.command("bot status")
        status = "\n".join(wait_for_log(current, "protocol 3", timeout=12,
                                        start=status_offset))
        if "桥接：在线" not in status:
            raise RuntimeError(f"Bridge protocol 3 is not online: {status[-900:]}")
        result["checks"].append("bridge_protocol_3_standalone_capability")

        # Keep the shulker's target and arena in a ticking chunk. Creation
        # and combat are actual vanilla BDS commands, not mocked callbacks.
        current.command("tickingarea add circle 8 68 8 2 onhit_zone")
        current.command("time set night")
        created_offset = current.command(
            f"bot createat {BOT_NAME} TestOwner 8 66 8 overworld"
        )
        wait_for_log(current, f"创建假人 {BOT_NAME}", timeout=18,
                     start=created_offset)
        result["checks"].append("survival_simulated_player_created")

        # A BDS server-side list is a stronger oracle than successful command
        # admission. The simulated player must actually join the world.
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            offset = current.command("list")
            time.sleep(0.5)
            if BOT_NAME in "\n".join(current.snapshot(offset)):
                break
        else:
            raise TimeoutError("BDS player list never contained simulated player")
        result["checks"].append("simulated_player_present_in_bds_list")

        # A fresh headless world has not generated the target chunk before the
        # simulated player joins; the earlier pre-join fill/summons correctly
        # failed with "outside of the world" / "Unable to summon object".
        # Wait for actual BDS chunk loading, then build above the terrain.
        time.sleep(3)
        fill_log = ""
        for attempt in range(6):
            offset = current.command("fill 2 220 2 14 220 14 stone")
            time.sleep(0.8)
            fill_log = "\n".join(current.snapshot(offset))
            if "Successfully filled" in fill_log or "blocks filled" in fill_log:
                break
            if "outside of the world" in fill_log:
                time.sleep(2)
                continue
            raise AssertionError(f"BDS arena fill unconfirmed: {fill_log[-600:]}")
        else:
            raise AssertionError(f"BDS never generated arena chunk: {fill_log[-600:]}")
        result["checks"].append("arena_blocks_confirmed_placed")

        move = current.command(f"bot moveat {BOT_NAME} 8 221 8 overworld")
        wait_for_log(current, f"已移动 {BOT_NAME}", timeout=12, start=move)
        time.sleep(2)
        result["checks"].append("simulated_player_relocated_to_arena")

        for x, z in ((8, 11), (10, 12), (6, 12)):
            offset = current.command(f"summon shulker {x} 221 {z}")
            time.sleep(0.5)
            response = "\n".join(current.snapshot(offset))
            if "Unable to summon" in response or "ERROR" in response:
                raise AssertionError(f"Shulker summon failed: {response[-600:]}")
            if "successfully summoned" not in response.lower():
                raise AssertionError(f"Shulker summon not confirmed: {response[-600:]}")
        result["checks"].append("shulker_entities_summoned")

        deadline = time.monotonic() + 65
        queries = 0
        seen = False
        while time.monotonic() < deadline:
            # This marker is emitted by BDS only when its entity selector
            # matches an actual vanilla shulker projectile.
            mark = current.command(
                f"execute if entity @e[type=shulker_bullet] run say {BULLET_MARKER}"
            )
            queries += 1
            time.sleep(0.30)
            if BULLET_MARKER in "\n".join(current.snapshot(mark)):
                seen = True
                break
            if current.process is None or current.process.poll() is not None:
                raise RuntimeError("BDS exited during shulker bullet observation")
        result["bullet_queries"] = queries
        result["shulker_bullet_observed"] = seen
        if not seen:
            raise AssertionError(
                "No verified shulker_bullet observed within 65s; inspect BDS "
                "command syntax, shulker AI, target visibility and bot game mode"
            )
        result["checks"].append("real_shulker_bullet_entity_observed")
        current.stop()
        current = None
        result["checks"].append("shulker_scenario_graceful_stop")
        result["status"] = "PASS"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if current is not None:
            try:
                current.stop()
            except BaseException as exc:
                result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                result["status"] = "FAIL"
        (work / "shulker_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
