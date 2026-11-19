"""Read-only lifetime trace for OnHit vectors in isolated Linux BDS 1.26.65."""
from __future__ import annotations

import argparse
import hashlib
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
ENDSTONE = "0.11.16"
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
        "  if $nonempty_cleanup <= 160\n"
        '   printf "CLEANUP_NONEMPTY record=0x%lx first=0x%lx count=%lu\\n", $rdi, *(unsigned long long*)$start, ($end-$start)/8\n'
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
        ' printf "IMPACT_DELETING_DTOR object=0x%lx\\n", $rdi\n'
        " continue\n"
        "end\n"
        "continue\n",
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
                if "Breakpoint 3 at" in line:
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
        result["checks"].append("three_native_entry_breakpoints_armed")
        for x,z in ((8,11),(10,12),(6,12)):
            command_confirm(server, f"summon shulker {x} 221 {z}", "successfully summoned", 15)
        result["checks"].append("three_shulkers_summoned")
        result["shulker_bullet_observed"] = observe_bullet(server)
        if not result["shulker_bullet_observed"]:
            raise RuntimeError("No real shulker bullet observed")
        result["checks"].append("real_shulker_bullet_observed")
        for _ in range(3):
            time.sleep(8)
        server.stop()
        server = None
        if gdb.poll() is None:
            gdb.wait(timeout=20)
        if thread is not None:
            thread.join(timeout=5)
        trace = log.read_text() if log else ""
        result["copy_nonempty"] = trace.count("COPY_NONEMPTY ")
        result["copy_empty_observed"] = trace.count("COPY_EMPTY ")
        result["cleanup_nonempty_logged"] = trace.count("CLEANUP_NONEMPTY ")
        result["impact_deleting_dtor_observed"] = trace.count("IMPACT_DELETING_DTOR ")
        result["checks"].append("native_lifetime_trace_collected")
        if result["cleanup_nonempty_logged"] == 0:
            raise RuntimeError("No nonempty OnHit cleanup record observed")
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
            for name, marker in (("cleanup_four_logged","CLEANUP_FOUR "), ("copy_default_observed","COPY_DEFAULT "), ("copy_nonempty","COPY_NONEMPTY "), ("copy_empty_observed","COPY_EMPTY "),
                                 ("cleanup_nonempty_logged","CLEANUP_NONEMPTY "), ("impact_deleting_dtor_observed","IMPACT_DELETING_DTOR ")):
                result[name] = trace.count(marker)
        (WORK / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
