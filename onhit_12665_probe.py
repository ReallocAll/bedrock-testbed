"""Read-only lifetime trace for OnHit vectors in isolated Linux BDS 1.26.65."""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from server import ROOT, Server, configure

WORK = ROOT / "work-onhit65"
BDS = "26.65"
ENDSTONE = os.environ.get("ENDSTONE_TEST_VERSION", "0.11.16")
BOT = "OnHitTarget"


def wait_log(server: Server, needle: str, timeout: float = 25, start: int = 0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = "\n".join(server.snapshot(start))
        if needle.casefold() in lines.casefold():
            return lines
        if server.process is None or server.process.poll() is not None:
            raise RuntimeError("BDS exited before " + needle)
        time.sleep(0.25)
    raise TimeoutError("Missing BDS output " + needle + ": " + lines[-800:])


def command_confirm(server: Server, command: str, needle: str, timeout: float = 15) -> str:
    return wait_log(server, needle, timeout, server.command(command))


def binary_info(folder: Path) -> dict:
    elf = folder / "bedrock_server"
    with elf.open("rb") as handle:
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
    notes = subprocess.run(["readelf", "-n", str(elf)], capture_output=True, check=True, text=True).stdout
    matches = re.findall(r"Build ID: ([a-f0-9]+)", notes)
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one BDS Build ID: " + repr(matches))
    return {"sha256": sha, "build_id": matches[0]}


def bds_pid_and_bias(server: Server, folder: Path) -> tuple[int, int]:
    if server.process is None:
        raise RuntimeError("BDS wrapper not started")
    launcher = server.process.pid
    children = Path(f"/proc/{launcher}/task/{launcher}/children").read_text().split()
    target = (folder / "bedrock_server").resolve()
    matches = [int(p) for p in children if Path(f"/proc/{p}/exe").resolve() == target]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one BDS process, got {matches}")
    pid = matches[0]
    bases = set()
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[-1] == str(target) and int(parts[2], 16) == 0:
            bases.add(int(parts[0].split("-", 1)[0], 16))
    if len(bases) != 1:
        raise RuntimeError(f"Cannot resolve unique ELF load bias: {bases}")
    return pid, bases.pop()



def remaining_deleting_destructor_breakpoints(bias: int) -> str:
    # Breakpoint 3 already traces ImpactDamage; these cover all other
    # OnHit child RTTI classes observed in the broadened 1.26.65 sample.
    entries = (
        ("MobEffect", 0x71729F0),
        ("ParticleOnHit", 0x7172E40),
        ("RemoveOnHit", 0x7172830),
        ("ArrowEffect", 0x7172D00),
        ("StickInGround", 0x7172870),
        ("DouseFire", 0x7172950),
        ("ThrownPotionEffect", 0x71728F0),
        ("CatchFire", 0x7172890),
    )
    return "".join(
        f"break *0x{bias + ea:x}\n"
        f"commands {number}\n"
        " silent\n"
        f' printf "OTHER_DELETING_DTOR stage=%d type={name} object=0x%lx\\n", $watch_stage, $rdi\n'
        " continue\n"
        "end\n"
        for number, (name, ea) in enumerate(entries, 6)
    )

def install_reinit_pack(folder: Path) -> None:
    """Load one isolated projectile whose definition event changes its on-hit component."""
    pack_id = "9ee997e2-4e23-4cd6-a32b-4f5ca0d9bbd1"
    pack = folder / "behavior_packs" / "onhit_reinit_probe"
    entities = pack / "entities"
    entities.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": 2,
        "header": {
            "name": "Isolated OnHit reinitialization probe",
            "description": "Testbed-only projectile component-group transition",
            "uuid": pack_id,
            "version": [1, 0, 0],
            "min_engine_version": [1, 26, 0],
        },
        "modules": [{
            "type": "data", "uuid": "451b837f-e5f7-4ff3-91c0-14fe596909d2",
            "version": [1, 0, 0],
        }],
    }
    base_projectile = {
        "gravity": 0.05, "power": 1.1,
        "on_hit": {
            "impact_damage": {"damage": 1, "destroy_on_hit": True},
            "stick_in_ground": {"shake_time": 0.35},
            "arrow_effect": {},
        },
    }
    alternate_projectile = {
        "gravity": 0.05, "power": 1.6,
        "on_hit": {
            "impact_damage": {"damage": 2, "destroy_on_hit": True},
            "stick_in_ground": {"shake_time": 0.35},
            "arrow_effect": {},
        },
    }
    entity = {
        "format_version": "1.20.80",
        "minecraft:entity": {
            "description": {
                "identifier": "onhit:probe_arrow", "runtime_identifier": "minecraft:arrow",
                "is_spawnable": False, "is_summonable": True,
            },
            "components": {
                "minecraft:type_family": {"family": ["projectile", "arrow"]},
                "minecraft:collision_box": {"width": 0.25, "height": 0.25},
                "minecraft:physics": {},
                "minecraft:projectile": base_projectile,
            },
            "component_groups": {
                "onhit:alternate": {"minecraft:projectile": alternate_projectile},
            },
            "events": {
                "onhit:toggle": {"add": {"component_groups": ["onhit:alternate"]}},
                "onhit:reset": {"remove": {"component_groups": ["onhit:alternate"]}},
            },
        },
    }
    (pack / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (entities / "onhit_probe.json").write_text(json.dumps(entity, indent=2) + "\n")
    level = next((line.split("=", 1)[1].strip()
                  for line in (folder / "server.properties").read_text().splitlines()
                  if line.startswith("level-name=")), "Bedrock level")
    world = folder / "worlds" / level
    world.mkdir(parents=True, exist_ok=True)
    stack_path = world / "world_behavior_packs.json"
    stack = json.loads(stack_path.read_text()) if stack_path.is_file() else []
    if not any(item.get("pack_id") == pack_id for item in stack):
        stack.append({"pack_id": pack_id, "version": [1, 0, 0]})
    stack_path.write_text(json.dumps(stack, indent=2) + "\n")


def start_trace(server: Server, folder: Path):
    pid, bias = bds_pid_and_bias(server, folder)
    script = WORK / "ownership.gdb"
    script.write_text(
        "set pagination off\n"
        "set confirm off\n"
        "set print thread-events off\n"
        "set $copies = 0\n"
        "set $nonempty_copies = 0\n"
        "set $nonempty_cleanup = 0\n"
        "set $init_calls = 0\n"
        "set $old0 = 0\n"
        "set $old1 = 0\n"
        "set $old2 = 0\n"
        "set $oldrecord = 0\n"
        "set $watch_stage = 0\n"
        f"break *0x{bias + 0x7757090:x}\n"
        "commands 1\n"
        " silent\n"
        " if $rcx == 0\n"
        "  printf \"COPY_DEFAULT source=null\\n\"\n"
        " else\n"
        "  set $copies = $copies + 1\n"
        "  set $start = *(unsigned long long*)($rcx+0x120)\n"
        "  set $end = *(unsigned long long*)($rcx+0x128)\n"
        "  if $start != 0 && $end > $start && $end - $start <= 8192\n"
        "   set $nonempty_copies = $nonempty_copies + 1\n"
        '   printf "COPY_NONEMPTY source=0x%lx first=0x%lx count=%lu\\n", $rcx, *(unsigned long long*)$start, ($end-$start)/8\n'
        "  else\n"
        "   if $copies <= 12\n"
        '    printf "COPY_EMPTY source=0x%lx\\n", $rcx\n'
        "   end\n"
        "  end\n"
        " end\n"
        " continue\n"
        "end\n"
        f"break *0x{bias + 0x7757D20:x}\n"
        "commands 2\n"
        " silent\n"
        " set $start = *(unsigned long long*)($rdi+0x120)\n"
        " set $end = *(unsigned long long*)($rdi+0x128)\n"
        " if $start != 0 && $end > $start && $end - $start <= 8192\n"
        "  set $nonempty_cleanup = $nonempty_cleanup + 1\n"
        '  if $oldrecord != 0 && $rdi == $oldrecord\n'
        '   printf "TRACKED_RECORD_CLEANUP record=0x%lx old0=0x%lx old1=0x%lx old2=0x%lx\\n", $rdi, $old0, $old1, $old2\n'
        '   set $old0 = *(unsigned long long*)$start\n'
        '   set $old1 = *((unsigned long long*)$start+1)\n'
        '   set $old2 = *((unsigned long long*)$start+2)\n'
        '   printf "FINAL_TRIPLET record=0x%lx p0=0x%lx p1=0x%lx p2=0x%lx\\n", $rdi, $old0, $old1, $old2\n'
        '   set $watch_stage = 2\n'
        '   set $oldrecord = 0\n'
        '  end\n'
        "  if $nonempty_cleanup <= 160\n"
        '   printf "CLEANUP_NONEMPTY record=0x%lx first=0x%lx count=%lu\\n", $rdi, *(unsigned long long*)$start, ($end-$start)/8\n'
        "  if $end - $start == 24\n"
        "   set $p0 = *(unsigned long long*)$start\n"
        "   set $p1 = *((unsigned long long*)$start+1)\n"
        "   set $p2 = *((unsigned long long*)$start+2)\n"
        '   printf "CLEANUP_THREE p0=0x%lx v0=0x%lx p1=0x%lx v1=0x%lx p2=0x%lx v2=0x%lx\\n", $p0, *(unsigned long long*)$p0, $p1, *(unsigned long long*)$p1, $p2, *(unsigned long long*)$p2\n'
        "  end\n"
        "  if $end - $start == 32\n"
        "   set $p0 = *(unsigned long long*)$start\n"
        "   set $p1 = *((unsigned long long*)$start+1)\n"
        "   set $p2 = *((unsigned long long*)$start+2)\n"
        "   set $p3 = *((unsigned long long*)$start+3)\n"
        '   printf "CLEANUP_FOUR p0=0x%lx v0=0x%lx p1=0x%lx v1=0x%lx p2=0x%lx v2=0x%lx p3=0x%lx v3=0x%lx\\n", $p0, *(unsigned long long*)$p0, $p1, *(unsigned long long*)$p1, $p2, *(unsigned long long*)$p2, $p3, *(unsigned long long*)$p3\n'
        "  end\n"
        "  end\n"
        " end\n"
        " continue\n"
        "end\n"
        f"break *0x{bias + 0x71729B0:x}\n"
        "commands 3\n"
        " silent\n"
        ' printf "IMPACT_DELETING_DTOR stage=%d object=0x%lx\\n", $watch_stage, $rdi\n'
        " continue\n"
        "end\n"
        f"break *0x{bias + 0x7143B40:x}\n"
        "commands 4\n"
        " silent\n"
        " set $init_calls = $init_calls + 1\n"
        " if $init_calls <= 160\n"
        '  printf "REINIT_ENTRY record=0x%lx actor=0x%lx ecs_id=0x%x\\n", $rdi, $rsi, *(unsigned int*)($rsi+24)\n'
        " end\n"
        " set $start = *(unsigned long long*)($rdi+0x120)\n"
        " set $end = *(unsigned long long*)($rdi+0x128)\n"
        " if $start != 0 && $end > $start && $end-$start <= 8192\n"
        '  printf "REINIT_NONEMPTY record=0x%lx first=0x%lx count=%lu\\n", $rdi, *(unsigned long long*)$start, ($end-$start)/8\n'
        '  if $end-$start == 24\n'
        '   set $oldrecord = $rdi\n'
        '   set $watch_stage = 1\n'
        '   set $old0 = *(unsigned long long*)$start\n'
        '   set $old1 = *((unsigned long long*)$start+1)\n'
        '   set $old2 = *((unsigned long long*)$start+2)\n'
        '   printf "OLD_TRIPLET record=0x%lx p0=0x%lx v0=0x%lx p1=0x%lx v1=0x%lx p2=0x%lx v2=0x%lx\\n", $oldrecord, $old0, *(unsigned long long*)$old0, $old1, *(unsigned long long*)$old1, $old2, *(unsigned long long*)$old2\n'
        '   set $scan_reg = *(unsigned long long*)($rsi+16)\n'
        '   set $scan_beg = *(unsigned long long*)($scan_reg+56)\n'
        '   set $scan_end = *(unsigned long long*)($scan_reg+64)\n'
        '   set $scan_buckets = ($scan_end-$scan_beg)/8\n'
        '   set $scan_store = 0\n'
        '   if $scan_buckets > 0 && $scan_buckets < 65536\n'
        '    set $scan_idx = *(unsigned long long*)($scan_beg+8*(0x94488499 & ($scan_buckets-1)))\n'
        '    set $scan_n = 0\n'
        '    while $scan_idx != -1 && $scan_n < 256\n'
        '     set $scan_entry = *(unsigned long long*)($scan_reg+80)+32*$scan_idx\n'
        '     if *(unsigned int*)($scan_entry+8) == 0x94488499\n'
        '      set $scan_store = *(unsigned long long*)($scan_entry+16)\n'
        '      set $scan_idx = -1\n'
        '     else\n'
        '      set $scan_idx = *(unsigned long long*)$scan_entry\n'
        '     end\n'
        '     set $scan_n = $scan_n+1\n'
        '    end\n'
        '   end\n'
        '   if $scan_store != 0\n'
        '    set $scan_packed = *(unsigned long long*)($scan_store+32)\n'
        '    set $scan_count = (*(unsigned long long*)($scan_store+40)-$scan_packed)/4\n'
        '    set $scan_pages = *(unsigned long long*)($scan_store+80)\n'
        '    set $scan_i = 0\n'
        '    set $scan_hits = 0\n'
        '    set $scan_foreign = 0\n'
        '    if $scan_count < 4096\n'
        '     while $scan_i < $scan_count\n'
        '      set $scan_entity = *(unsigned int*)($scan_packed+4*$scan_i)\n'
        '      if ($scan_entity >> 18) != 0x3fff\n'
        '       set $scan_page = *(unsigned long long*)($scan_pages+8*($scan_i>>7))\n'
        '       if $scan_page != 0\n'
        '        set $scan_rec = $scan_page+760*($scan_i & 127)\n'
        '        set $scan_vec = *(unsigned long long*)($scan_rec+0x120)\n'
        '        set $scan_vecend = *(unsigned long long*)($scan_rec+0x128)\n'
        '        if $scan_vec != 0 && $scan_vecend >= $scan_vec && $scan_vecend-$scan_vec <= 8192\n'
        '         set $scan_j = 0\n'
        '         while $scan_j < ($scan_vecend-$scan_vec)/8\n'
        '          set $scan_child = *(unsigned long long*)($scan_vec+8*$scan_j)\n'
        '          if $scan_child == $old0 || $scan_child == $old1 || $scan_child == $old2\n'
        '           set $scan_hits = $scan_hits+1\n'
        '           if $scan_rec != $oldrecord\n'
        '            set $scan_foreign = $scan_foreign+1\n'
        '            printf "ALIAS_SAME_REGISTRY other_record=0x%lx child=0x%lx\\n", $scan_rec, $scan_child\n'
        '           end\n'
        '          end\n'
        '          set $scan_j = $scan_j+1\n'
        '         end\n'
        '        end\n'
        '       end\n'
        '      end\n'
        '      set $scan_i = $scan_i+1\n'
        '     end\n'
        '     printf "ALIAS_SCAN_SAME_REGISTRY records=%lu matching_slots=%lu other_record_slots=%lu\\n", $scan_count, $scan_hits, $scan_foreign\n'
        '    else\n'
        '     printf "ALIAS_SCAN_INVALID count=%lu\\n", $scan_count\n'
        '    end\n'
        '   else\n'
        '    printf "ALIAS_SCAN_NO_STORAGE\\n"\n'
        '   end\n'
        '   enable 14\n'
        '  end\n'
        " end\n"
        " continue\n"
        "end\n"
        f"break *0x{bias + 0x7758440:x}\n"
        "commands 5\n"
        " silent\n"
        ' printf "COPY_PAYLOAD destination=0x%lx source=0x%lx\\n", $rdi, $rsi\n'
        " continue\n"
        "end\n"
        + remaining_deleting_destructor_breakpoints(bias)
        + f"break *0x{bias + 0x41FB010:x} if $old0 != 0 && ($rdi == $old0 || $rdi == $old1 || $rdi == $old2)\n"
        + "commands 14\n"
        + " silent\n"
        + ' printf "WATCHED_CHILD_DEALLOC stage=%d object=0x%lx size=%lu\\n", $watch_stage, $rdi, $rsi\n'
        + " continue\n"
        + "end\n"
        + "disable 14\n"
        + "continue\n",
        encoding="ascii",
    )
    output = WORK / "ownership-gdb.log"
    process = subprocess.Popen(
        ["sudo", "-n", "gdb", "-q", "-nx", "--batch", "-p", str(pid), "-x", str(script)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    ready = threading.Event()

    def capture():
        assert process.stdout is not None
        with output.open("w") as file:
            for line in process.stdout:
                file.write(line)
                file.flush()
                if "Breakpoint 14 at" in line:
                    ready.set()

    thread = threading.Thread(target=capture, daemon=True)
    thread.start()
    if not ready.wait(timeout=25):
        if process.poll() is None:
            process.terminate()
        raise RuntimeError("GDB failed to arm: " + output.read_text()[-1800:])
    return process, thread, output, bias


def observe_bullet(server: Server, seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        output = "\n".join(server.snapshot(server.command(
            "execute if entity @e[type=shulker_bullet] run say ONHIT_BULLET_OBSERVED"
        )))
        time.sleep(0.4)
        if "ONHIT_BULLET_OBSERVED" in output or "ONHIT_BULLET_OBSERVED" in "\n".join(server.snapshot()[-16:]):
            return True
        if server.process is None or server.process.poll() is not None:
            raise RuntimeError("BDS exited during projectile workload")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    args = parser.parse_args()
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()
    folder = WORK / "server"
    result = {"status": "FAIL", "endstone": ENDSTONE, "bds": BDS,
              "standalone_onhitfix_installed": False, "checks": []}
    server = None
    gdb = None
    thread = None
    log = None

    def new_server():
        return Server(args.server_python.absolute(), folder, WORK / "bds.log",
                      expected_endstone=ENDSTONE, expected_bds=BDS)

    try:
        server = new_server()
        server.start()
        server.wait_ready()
        result["checks"].append("official_bds_start")
        server.stop()
        server = None
        result["binary"] = binary_info(folder)
        result["checks"].append("elf_sha256_and_build_id")
        configure(folder / "server.properties", "raknet")
        install_reinit_pack(folder)
        result["checks"].append("isolated_projectile_group_pack_installed")
        patch = folder / "plugins" / "bot" / "enable_beta.py"
        if not patch.is_file():
            raise FileNotFoundError("Endstone Bot beta enablement script missing")
        subprocess.run([str(args.server_python.absolute()), str(patch)],
                       cwd=ROOT, check=True, timeout=40)
        result["checks"].append("beta_api_enabled_on_isolated_world")
        prop = folder / "server.properties"
        contents = prop.read_text()
        prop.write_text(contents.replace("difficulty=peaceful", "difficulty=normal"))
        if (folder / "plugins" / "endstone_onhitfix.so").exists():
            raise RuntimeError("Standalone OnHitFix is unexpectedly installed")
        server = new_server()
        server.start()
        server.wait_ready()
        wait_log(server, "standalone SimulatedPlayer", timeout=60)
        server.command("tickingarea add circle 8 68 8 2 onhit_zone")
        command_confirm(server, f"bot createat {BOT} TestOwner 8 66 8 overworld",
                        f"创建假人 {BOT}", 25)
        server.command("time set night")
        time.sleep(4)
        for attempt in range(6):
            try:
                command_confirm(server, "fill 2 220 2 14 220 14 stone", "filled", 3)
                break
            except TimeoutError:
                if attempt == 5:
                    raise
                time.sleep(2)
        command_confirm(server, f"bot moveat {BOT} 8 221 8 overworld",
                        f"已移动 {BOT}", 20)
        time.sleep(2)
        result["checks"].append("real_simulated_player_and_arena")
        gdb, thread, log, bias = start_trace(server, folder)
        result["elf_load_bias"] = f"0x{bias:x}"
        result["checks"].append("fourteen_native_entry_breakpoints_armed")
        for x,z in ((8,11),(10,12),(6,12)):
            command_confirm(server, f"summon shulker {x} 221 {z}", "successfully summoned", 15)
        result["checks"].append("three_shulkers_summoned")
        for mob, x, z in (("skeleton", 4, 12), ("blaze", 12, 12), ("witch", 8, 13)):
            command_confirm(server, f"summon {mob} {x} 221 {z}", "successfully summoned", 15)
        result["checks"].append("skeleton_blaze_witch_summoned")
        result["shulker_bullet_observed"] = observe_bullet(server)
        if not result["shulker_bullet_observed"]:
            raise RuntimeError("No real shulker bullet observed")
        result["checks"].append("real_shulker_bullet_observed")
        for x, z in ((6, 8), (8, 8), (10, 8), (6, 10), (8, 10), (10, 10)):
            command_confirm(server, f"summon arrow {x} 225 {z}", "successfully summoned", 15)
        result["checks"].append("six_direct_arrows_summoned")
        # Mechanism-targeted: a live projectile receives a different
        # minecraft:projectile component group without despawning.
        command_confirm(server, "summon onhit:probe_arrow 8 225 8",
                        "successfully summoned", 15)
        command_confirm(server,
                        "execute if entity @e[type=onhit:probe_arrow] run say ONHIT_PROBE_ALIVE",
                        "ONHIT_PROBE_ALIVE", 15)
        result["checks"].append("probe_projectile_alive")
        event = "onhit:toggle"
        response = command_confirm(
            server, f"event entity @e[type=onhit:probe_arrow] {event}",
            "onhit:toggle successfully executed on: onhit:probe_arrow", 15,
        )
        result["group_transition_responses"] = [
            {"event": event, "response": response[-500:]}
        ]
        result["checks"].append("projectile_component_group_transition_confirmed")
        # Test an entity-bearing save/restore path, not just in-place spawns.
        # The saved volume contains the newly summoned arrows and mobs.
        command_confirm(server,
                        "structure save onhit_projectiles 2 220 2 14 230 14 true memory false",
                        "saved", 20)
        result["checks"].append("entity_structure_saved")
        command_confirm(server,
                        "structure load onhit_projectiles 24 220 2 0_degrees none true false",
                        "loaded", 20)
        result["checks"].append("entity_structure_loaded")
        # A successful load command does not prove that a projectile
        # entity was included. Only positions around the destination count.
        for label, selector in (
            ("restored_arrow", "@e[type=arrow,x=30,y=222,z=8,r=14]"),
            ("restored_shulker", "@e[type=shulker,x=30,y=221,z=11,r=14]"),
        ):
            marker = "ONHIT_" + label.upper()
            try:
                command_confirm(server,
                                f"execute if entity {selector} run say {marker}",
                                marker, 7)
                result[label] = True
            except TimeoutError:
                result[label] = False
        # Exercise the live actor/component lifecycle across leaving and
        # returning to the arena; do not infer unload from teleport alone.
        time.sleep(4)
        server.command("tickingarea remove onhit_zone")
        command_confirm(server, f"bot moveat {BOT} 1000 70 1000 overworld",
                        f"已移动 {BOT}", 25)
        time.sleep(8)
        command_confirm(server, f"bot moveat {BOT} 8 221 8 overworld",
                        f"已移动 {BOT}", 25)
        result["checks"].append("arena_departure_and_return")
        for _ in range(2):
            time.sleep(8)
        server.stop()
        server = None
        if gdb.poll() is None:
            gdb.wait(timeout=20)
        if thread is not None:
            thread.join(timeout=5)
        trace = log.read_text() if log else ""
        result["copy_payload_entries"] = trace.count("COPY_PAYLOAD ") 
        result["copy_nonempty"] = trace.count("COPY_NONEMPTY ")
        result["copy_empty_observed"] = trace.count("COPY_EMPTY ")
        result["cleanup_nonempty_logged"] = trace.count("CLEANUP_NONEMPTY ")
        result["impact_deleting_dtor_observed"] = trace.count("IMPACT_DELETING_DTOR ")
        result["other_deleting_dtor_observed"] = trace.count("OTHER_DELETING_DTOR ")
        result["reinit_nonempty_observed"] = trace.count("REINIT_NONEMPTY ")
        result["reinit_entries_logged"] = trace.count("REINIT_ENTRY ")
        result["checks"].append("native_lifetime_trace_collected")
        if result["cleanup_nonempty_logged"] == 0:
            raise RuntimeError("No nonempty OnHit cleanup record observed")
        if (trace.count("REINIT_NONEMPTY ") != 1
                or trace.count("OLD_TRIPLET ") != 1
                or trace.count("TRACKED_RECORD_CLEANUP ") != 1):
            raise RuntimeError("Targeted nonempty reinit / old-triplet / retirement chain missing")
        scans = re.findall(
            r"ALIAS_SCAN_SAME_REGISTRY records=(\d+) matching_slots=(\d+) other_record_slots=(\d+)",
            trace,
        )
        if len(scans) != 1 or int(scans[0][1]) != 3 or int(scans[0][2]) != 0:
            raise RuntimeError(f"Existing ECS storage alias scan inconclusive: {scans}")
        def triplet(pattern):
            match = re.search(pattern, trace)
            if not match:
                raise RuntimeError("Missing ownership triplet: " + pattern)
            return [int(address, 16) for address in match.groups()]

        old = triplet(r"OLD_TRIPLET record=0x[0-9a-f]+ p0=0x([0-9a-f]+) v0=.*? p1=0x([0-9a-f]+) v1=.*? p2=0x([0-9a-f]+) v2=")
        final = triplet(r"FINAL_TRIPLET record=0x[0-9a-f]+ p0=0x([0-9a-f]+) p1=0x([0-9a-f]+) p2=0x([0-9a-f]+)")
        if len(set(old)) != 3 or len(set(final)) != 3:
            raise RuntimeError("Expected three distinct owned children at each recorded site")
        deallocated = {
            stage: [int(address, 16) for address in re.findall(
                rf"WATCHED_CHILD_DEALLOC stage={stage} object=0x([0-9a-f]+)", trace
            )]
            for stage in (1, 2)
        }
        for stage, expected in ((1, old), (2, final)):
            if sorted(deallocated[stage]) != sorted(expected):
                raise RuntimeError(
                    f"Native deallocation mismatch stage={stage}: expected={expected}, observed={deallocated[stage]}"
                )
        for stage, expected in ((1, old), (2, final)):
            destroyed = [
                int(address, 16) for address in re.findall(
                    rf"(?:IMPACT_DELETING_DTOR stage={stage}|OTHER_DELETING_DTOR stage={stage} type=[A-Za-z]+) object=0x([0-9a-f]+)",
                    trace,
                )
            ]
            if sorted(destroyed) != sorted(expected):
                raise RuntimeError(
                    f"Deleting-destructor mismatch stage={stage}: expected={expected}, observed={destroyed}"
                )
        result["old_children_deallocated_once"] = len(old)
        result["replacement_children_deallocated_once"] = len(final)
        result["checks"].append("old_and_replacement_children_destroyed_and_deallocated_once")
        result["status"] = "PASS"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                result["shutdown_error"] = str(error)
        if gdb is not None and gdb.poll() is None:
            gdb.terminate()
            try:
                gdb.wait(timeout=8)
            except subprocess.TimeoutExpired:
                gdb.kill()
        if thread is not None:
            thread.join(timeout=4)
        if log and log.is_file():
            trace = log.read_text()
            for name, marker in (("cleanup_three_logged","CLEANUP_THREE "), ("cleanup_four_logged","CLEANUP_FOUR "), ("copy_payload_entries","COPY_PAYLOAD "), ("copy_default_observed","COPY_DEFAULT "), ("copy_nonempty","COPY_NONEMPTY "), ("copy_empty_observed","COPY_EMPTY "),
                                 ("cleanup_nonempty_logged","CLEANUP_NONEMPTY "), ("impact_deleting_dtor_observed","IMPACT_DELETING_DTOR "), ("other_deleting_dtor_observed","OTHER_DELETING_DTOR "), ("reinit_nonempty_observed","REINIT_NONEMPTY "), ("reinit_entries_logged","REINIT_ENTRY ")):
                result[name] = trace.count(marker)
        (WORK / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
