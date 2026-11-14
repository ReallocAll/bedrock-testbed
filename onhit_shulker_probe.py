"""Run preserved-world endstone-bot trigger after passive BDS acceptance.

Optional one-hook funchook variant runs only in this ephemeral isolated runner.
No production access. The passive and active binaries are built separately.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from server import ROOT, Server

BOT_COMMIT = "4bd0691f1ef0f551e5ba258d4747b35a3b748177"
BOT_NAME = "OnHitTarget"
BULLET_MARKER = "ONHIT_BULLET_OBSERVED"


def bds_memory_kib(server: Server, folder: Path) -> dict[str, int]:
    """Unadjusted per-process Linux resident memory; NOT retained OnHit bytes."""
    if server.process is None:
        raise RuntimeError("No running BDS launcher")
    parent = server.process.pid
    pids = [int(x) for x in Path(
        f"/proc/{parent}/task/{parent}/children").read_text().split()]
    expected = (folder / "bedrock_server").resolve()
    matches = [p for p in pids
               if Path(f"/proc/{p}/exe").resolve() == expected]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one BDS process, got {matches}")
    fields = {}
    for line in Path(f"/proc/{matches[0]}/smaps_rollup").read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.strip().split()
        if key in {"Rss", "Pss", "Private_Dirty"} and parts:
            fields[key] = int(parts[0])
    if len(fields) != 3:
        raise RuntimeError(f"Incomplete smaps_rollup fields: {fields}")
    return fields


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


def start_native_entry_probe(server: Server, folder: Path, work: Path):
    """Single bounded external breakpoint, independent of C++ function ABI.

    GDB detaches itself at the first parse entry. This never changes any BDS
    file and never installs or calls a detour/trampoline.
    """
    if server.process is None:
        raise RuntimeError("BDS bootstrap process is missing")
    parent = server.process.pid
    children_file = Path(f"/proc/{parent}/task/{parent}/children")
    children = [int(x) for x in children_file.read_text().split()]
    expected = (folder / "bedrock_server").resolve()
    bds = [pid for pid in children
           if Path(f"/proc/{pid}/exe").resolve() == expected]
    if len(bds) != 1:
        raise RuntimeError(f"Expected a single BDS child of {parent}: {children}")
    pid = bds[0]
    loads = set()
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split()
        # PT_LOAD segments have distinct page-aligned mappings; only the
        # offset-zero map anchors the actual PIE image base.
        if (len(fields) >= 6 and fields[-1] == str(expected)
                and int(fields[2], 16) == 0):
            loads.add(int(fields[0].split("-", 1)[0], 16))
    if len(loads) != 1:
        raise RuntimeError(f"Could not determine BDS ELF offset-zero map: {loads}")
    load_bias = loads.pop()
    if f"load bias=0x{load_bias:x}" not in "\n".join(server.snapshot()):
        raise RuntimeError("GDB load bias disagrees with native identity gate")
    entry = load_bias + 0x7143660

    commands = work / "native-entry.gdb"
    commands.write_text(
        "set pagination off\n"
        "set confirm off\n"
        "set print thread-events off\n"
        f"break *0x{entry:x}\n"
        "commands 1\n"
        "  silent\n"
        '  printf "ONHIT_GDB_PARSE rdi=0x%lx rsi=0x%lx retpc=0x%lx\\n", '
        '$rdi, $rsi, *(unsigned long*)$rsp\n'
        "  detach\n"
        "  quit\n"
        "end\n"
        "continue\n"
    )
    proc = subprocess.Popen(
        ["sudo", "-n", "gdb", "-q", "-nx", "--batch",
         "-p", str(pid), "-x", str(commands)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    ready = threading.Event()
    trace_log = work / "native-entry-gdb.log"

    def capture() -> None:
        with trace_log.open("w") as output:
            assert proc.stdout is not None
            for line in proc.stdout:
                output.write(line)
                output.flush()
                if "Breakpoint 1 at" in line:
                    ready.set()

    threading.Thread(target=capture, daemon=True).start()
    if not ready.wait(timeout=20):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        raise RuntimeError("GDB breakpoint was not ready: " +
                           trace_log.read_text()[-1200:])
    return proc, trace_log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    parser.add_argument("--native-gdb", action="store_true")
    parser.add_argument("--stress-rounds", type=int, default=0,
                        help="Repeat fixed-size Shulker spawn/cleanup workload")
    parser.add_argument("--fix-candidate", action="store_true",
                        help="Enable isolated parse-reclaim candidate for A/B job")
    parser.add_argument("--hook-plugin", type=Path)
    parser.add_argument("--hook-scope", choices=("parse", "all"), default="parse")
    args = parser.parse_args()
    if args.native_gdb and args.hook_plugin:
        parser.error("--native-gdb and --hook-plugin are mutually exclusive")
    if args.hook_scope != "parse" and args.hook_plugin is None:
        parser.error("--hook-scope all requires --hook-plugin")
    python = args.server_python.absolute()
    os.environ["BDS_ONHIT_CONTACT_PROBE"] = "I_CONFIRM_ISOLATED_BDS"
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
    gdb_process: subprocess.Popen | None = None
    gdb_log: Path | None = None

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

        if args.hook_plugin is not None:
            # Only replace the plugin after the passive/Beta boot has stopped.
            # No live binary replacement, dynamic module reload or production use.
            hook_plugin = args.hook_plugin.absolute()
            if not hook_plugin.is_file() or not hook_plugin.stat().st_size:
                raise FileNotFoundError(f"Isolated hook plugin unavailable: {hook_plugin}")
            shutil.copyfile(hook_plugin,
                            folder / "plugins" / "endstone_onhit_lifecycle.so")
            os.environ["BDS_ONHIT_ISOLATED_TEST"] = "I_CONFIRM_ISOLATED_BDS"
            os.environ["BDS_ONHIT_HOOK_SCOPE"] = args.hook_scope
            result["hook_scope"] = args.hook_scope
            result["fix_candidate"] = args.fix_candidate
            if args.fix_candidate:
                os.environ["BDS_ONHIT_FIX_CANDIDATE"] = "CLEANUP_RECLAIM"
            else:
                os.environ.pop("BDS_ONHIT_FIX_CANDIDATE", None)
            result["checks"].append("isolated_parse_only_funchook_binary_selected")

        current = start()
        if args.hook_plugin is not None:
            wait_for_log(current,
                         "EXPERIMENTAL OnHit " +
                         ("lifecycle" if args.hook_scope == "all" else "parse-only") +
                         " hook active", timeout=15)
            result["onhit_hooks_enabled"] = True
            if args.fix_candidate:
                wait_for_log(current, "ONHIT_FIX_CANDIDATE_ENABLED", timeout=15)
                result["checks"].append("parse_reclaim_candidate_enabled")
            result["checks"].append("native_funchook_parse_installed")
        wait_for_log(current, "ONHIT_CONTACT_PROBE_READY", timeout=20)
        result["checks"].append("native_passive_contact_oracle_registered")
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
        result["memory_before_combat_kib"] = bds_memory_kib(current, folder)

        if args.native_gdb:
            gdb_process, gdb_log = start_native_entry_probe(current, folder, work)
            result["checks"].append("external_native_entry_breakpoint_installed")

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

        # Bullet existence is not a hit. Require an independent Endstone
        # ActorDamageEvent from the simulated player whose direct source is a
        # shulker bullet, rather than inferring contact from entity presence.
        contact_deadline = time.monotonic() + 65
        contact_line = ""
        while time.monotonic() < contact_deadline:
            for line in current.snapshot():
                if ("ONHIT_CONTACT receiver=" + BOT_NAME) in line and (
                    "direct=minecraft:shulker_bullet" in line or
                    "direct=shulker_bullet" in line
                ):
                    contact_line = line
                    break
            if contact_line:
                break
            if current.process is None or current.process.poll() is not None:
                raise RuntimeError("BDS exited before a confirmed projectile hit")
            time.sleep(0.25)
        result["contact_observed"] = bool(contact_line)
        if not contact_line:
            raise AssertionError(
                "A real shulker_bullet existed, but no target damage from a "
                "shulker_bullet was independently observed within 65 seconds"
            )
        result["contact_evidence"] = contact_line
        result["checks"].append("simulated_player_shulker_projectile_damage_observed")
        result["memory_after_contact_kib"] = bds_memory_kib(current, folder)
        if args.stress_rounds:
            if not 1 <= args.stress_rounds <= 300:
                raise ValueError("stress-rounds must be between 1 and 300")
            snapshots = []
            for round_no in range(1, args.stress_rounds + 1):
                for x, z in ((8, 11), (10, 12), (6, 12)):
                    current.command(f"summon shulker {x} 221 {z}")
                time.sleep(0.3)
                current.command("kill @e[type=shulker]")
                current.command("kill @e[type=shulker_bullet]")
                if round_no % 10 == 0:
                    time.sleep(0.4)
                    if current.process is None or current.process.poll() is not None:
                        raise RuntimeError(f"BDS died during stress round {round_no}")
                    snapshots.append({"round": round_no,
                                      "memory_kib": bds_memory_kib(current, folder)})
            result["stress_rounds"] = args.stress_rounds
            result["stress_summons_sent"] = args.stress_rounds * 3
            result["stress_memory_snapshots"] = snapshots
            result["checks"].append("fixed_count_shulker_spawn_cleanup_stress")
        time.sleep(5)
        result["memory_after_combat_kib"] = bds_memory_kib(current, folder)
        current.stop()
        current = None
        result["checks"].append("shulker_scenario_graceful_stop")
        if args.hook_plugin is not None:
            traces = sorted((folder / "plugins").rglob("trace.tsv"))
            if len(traces) != 1:
                raise AssertionError(f"Expected one onDisable hook trace: {traces}")
            lines = [line for line in traces[0].read_text().splitlines()
                     if line and not line.startswith("#")]
            if not lines or not lines[0].startswith("sequence\tkind\t"):
                raise AssertionError("Hook trace has no valid TSV header")
            entries = [row.split("\t") for row in lines[1:]]
            parse_enter = sum(row[1] == "1" for row in entries if len(row) >= 6)
            parse_exit = sum(row[1] == "2" for row in entries if len(row) >= 6)
            result["funchook_parse_enter"] = parse_enter
            result["funchook_parse_exit"] = parse_exit
            result["funchook_trace_rows"] = len(entries)
            if args.hook_scope == "all":
                extra = {}
                for kind, enter_id, exit_id in (
                    ("reset", "3", "4"), ("cleanup", "5", "6")
                ):
                    entered = sum(row[1] == enter_id for row in entries
                                  if len(row) >= 6)
                    exited = sum(row[1] == exit_id for row in entries
                                 if len(row) >= 6)
                    extra[kind] = {"enter": entered, "exit": exited}
                    if entered < 1 or exited != entered:
                        raise AssertionError(
                            f"{kind} detour was not fully observed: {entered}/{exited}"
                        )
                result["funchook_lifecycle"] = extra
                result["checks"].append("reset_cleanup_detours_enter_exit_traced")
            headers = traces[0].read_text().splitlines()[:3]
            result["funchook_trace_header"] = headers
            marker = next((x for x in headers if x.startswith("#fix_candidate=")), "")
            if not marker:
                raise AssertionError("Fix candidate status not recorded in trace")
            result["fix_reclaimed"] = int(marker.split("reclaimed=", 1)[1])
            if args.fix_candidate != ("#fix_candidate=1" in marker):
                raise AssertionError(f"Fix candidate mode mismatch: {marker}")
            if parse_enter < 1 or parse_exit < 1 or parse_enter != parse_exit:
                raise AssertionError(
                    f"Native parse trampoline mismatch: enter={parse_enter} "
                    f"exit={parse_exit}; rows={len(entries)}"
                )
            result["checks"].append("native_parse_detour_enter_exit_traced")
        if args.native_gdb:
            assert gdb_process is not None and gdb_log is not None
            gdb_process.wait(timeout=12)
            evidence = [line.strip() for line in gdb_log.read_text().splitlines()
                        if "ONHIT_GDB_PARSE" in line]
            result["native_parse_entries"] = evidence
            result["gdb_exit_code"] = gdb_process.returncode
            if not evidence or gdb_process.returncode != 0:
                raise AssertionError("No verified native parse entry or debugger failure; "
                                     "see work-onhit/native-entry-gdb.log")
            result["checks"].append("exact_native_parse_entry_breakpoint_hit")
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
        if gdb_process is not None and gdb_process.poll() is None:
            gdb_process.terminate()
            try:
                gdb_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gdb_process.kill()
                gdb_process.wait()
        (work / "shulker_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
