"""One clean Endstone/BDS lifecycle and one real EndPilot public-API smoke test.

Ubuntu Linux only. Endstone and EndPilot are installed in separate venvs by CI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAYER = "EndPilotTest"
VERSION_LINE = "This server is running Endstone version: 0.11.14 (Minecraft: 26.52)"
PROPERTIES = {
    "online-mode": "false",
    "allow-list": "false",
    "server-port": "19132",
    "server-portv6": "19133",
    "gamemode": "creative",
    "difficulty": "peaceful",
    "allow-cheats": "true",
    "default-player-permission-level": "operator",
    "player-idle-timeout": "0",
    "view-distance": "12",
    "tick-distance": "4",
    "client-side-chunk-generation-enabled": "false",
}
PLAYER_COUNT = re.compile(r"there are\s+(\d+)\s*(?:/|of a max of)\s*\d+\s+players?\s+online", re.I)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Server:
    def __init__(self, python: Path, folder: Path, logfile: Path):
        self.folder = folder
        self.logfile = logfile
        self.process = None
        self.lines = []
        self.lock = threading.Lock()
        self.thread = None
        self.stream = None
        self.python = python

    def start(self):
        self.lines = []
        self.stream = self.logfile.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(self.python), "-m", "endstone", "--yes", "--server-folder", str(self.folder)],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        assert self.process and self.process.stdout and self.stream
        for raw in self.process.stdout:
            line = ANSI.sub("", raw).strip()
            with self.lock:
                self.lines.append(line)
            self.stream.write(raw)
            self.stream.flush()
            print("[bds] " + line, flush=True)

    def snapshot(self, start=0):
        with self.lock:
            return self.lines[start:].copy()

    def wait_ready(self, timeout=420):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.snapshot()
            if any("server started" in line.lower() for line in lines):
                if VERSION_LINE not in "\n".join(lines):
                    raise RuntimeError("BDS Ready but Endstone/version line does not match 0.11.14/26.52")
                version_path = self.folder / "version.txt"
                if not version_path.is_file() or version_path.read_text().strip() != "26.52":
                    raise RuntimeError("BDS version.txt is missing or does not equal 26.52")
                return
            if self.process.poll() is not None:
                raise RuntimeError(f"BDS exited before Ready (code={self.process.returncode})")
            time.sleep(0.25)
        raise TimeoutError("BDS did not report Ready and expected versions")

    def command(self, value):
        if self.process is None or self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("BDS is not running")
        with self.lock:
            offset = len(self.lines)
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()
        return offset

    def player_online(self):
        """Require a fresh BDS 'list' response, not only a client-side Ready."""
        offset = self.command("list")
        deadline = time.monotonic() + 6
        count = None
        observed_at = None
        while time.monotonic() < deadline:
            lines = self.snapshot(offset)
            for line in lines:
                match = PLAYER_COUNT.search(line)
                if match:
                    count = int(match.group(1))
                    observed_at = observed_at or time.monotonic()
            if count is not None and (PLAYER.lower() in "\n".join(lines).lower() or count == 0):
                return count > 0 and PLAYER.lower() in "\n".join(lines).lower(), lines
            if count is not None and time.monotonic() - observed_at > 1:
                return False, lines
            if self.process.poll() is not None:
                raise RuntimeError("BDS exited during player list check")
            time.sleep(0.1)
        raise TimeoutError("BDS did not answer 'list' with a recognizable player count")

    def wait_player(self, expected, timeout=25):
        deadline = time.monotonic() + timeout
        last = []
        while time.monotonic() < deadline:
            online, last = self.player_online()
            if online == expected:
                return last
            time.sleep(0.5)
        raise AssertionError(f"BDS player list did not converge to online={expected}: {last[-8:]}")

    @staticmethod
    def group_members(pgid):
        """Linux /proc group members, excluding exited zombies."""
        active = []
        for stat_file in Path("/proc").glob("[0-9]*/stat"):
            try:
                stat = stat_file.read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                    active.append(int(stat_file.parent.name))
            except (ValueError, IndexError, OSError):
                continue
        return active

    def stop(self):
        if self.process is None:
            return
        proc = self.process
        graceful = False
        try:
            if proc.poll() is None:
                self.command("stop")
            proc.wait(timeout=45)
            graceful = proc.returncode == 0
        except (BrokenPipeError, RuntimeError, subprocess.TimeoutExpired):
            pass
        finally:
            members = self.group_members(proc.pid)
            if members:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(0.5)
                members = self.group_members(proc.pid)
                if members:
                    os.killpg(proc.pid, signal.SIGKILL)
            if self.thread:
                self.thread.join(timeout=5)
            if self.stream:
                self.stream.close()
            self.process = None
        leftover = self.group_members(proc.pid)
        if not graceful or leftover:
            raise RuntimeError(
                f"BDS shutdown was not clean: returncode={proc.returncode}, "
                f"graceful={graceful}, residual_pids={leftover}"
            )


def configure(path):
    if not path.is_file():
        raise FileNotFoundError(f"Endstone did not create {path}")
    updated = set()
    lines = []
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in PROPERTIES:
                line = key + "=" + PROPERTIES[key]
                updated.add(key)
        lines.append(line)
    lines.extend(k + "=" + v for k, v in PROPERTIES.items() if k not in updated)
    path.write_text("\n".join(lines) + "\n")


async def exercise(server, results):
    from bedrock_protocol import v2193
    from endpilot import BotOptions, BotState, EventKind, PacketDecodedEvent, Runtime, event_mask

    radius_events = []

    def callback(_bot, event):
        if isinstance(event, PacketDecodedEvent) and event.packet.header.packet_id == 70:
            radius_events.append(int(event.packet.payload.chunk_radius))

    async with Runtime(callback, events=event_mask(EventKind.packet_decoded)) as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=2193, name=PLAYER, chunk_radius=16,
        ))
        await bot.wait_ready(65)
        status = bot.get_status()
        assert status.state == BotState.ready
        assert status.actor_runtime_id > 0
        assert all(math.isfinite(v) for v in (status.position.x, status.position.y, status.position.z))
        results["world_state"] = {"actor_runtime_id": status.actor_runtime_id, "dimension": status.dimension,
                                  "position": [status.position.x, status.position.y, status.position.z]}
        results["bds_online_list"] = await asyncio.to_thread(server.wait_player, True)
        results["checks"].append("raknet_login_server_confirmed")

        async def request_and_observe(requested):
            before = len(radius_events)
            bot.send_packet(v2193.RequestChunkRadiusPacket(chunk_radius=requested, max_chunk_radius=requested))
            deadline = asyncio.get_running_loop().time() + 12
            while asyncio.get_running_loop().time() < deadline:
                new = radius_events[before:]
                if new and bot.get_status().chunk_radius == new[-1]:
                    approved = new[-1]
                    assert 1 <= approved <= 12, f"BDS granted unexpected chunk radius: {approved}"
                    return {"requested": requested, "approved": approved, "new_responses": len(new)}
                if bot.get_status().state != BotState.ready:
                    raise AssertionError("Session left Ready while waiting for chunk-radius response")
                await asyncio.sleep(0.1)
            raise TimeoutError(f"No fresh server ChunkRadiusUpdated response to request {requested}")

        initial = bot.get_status().chunk_radius
        first = await request_and_observe(6)
        assert first["approved"] != initial, f"radius did not change from initial {initial}"
        results["initial_radius"] = initial
        results["radius_request"] = first
        results["checks"].append("typed_packet_server_response")

        try:
            bot.send_packet(object())
        except (TypeError, ValueError, RuntimeError) as error:
            results["rejected_packet"] = type(error).__name__
        else:
            raise AssertionError("Invalid typed packet was admitted")
        assert bot.get_status().state == BotState.ready, "Invalid public packet poisoned the session"
        second = await request_and_observe(8)
        assert second["approved"] != first["approved"], "Session did not process valid packet after rejection"
        results["radius_after_rejection"] = second
        results["checks"].append("invalid_packet_isolation")

        await bot.disconnect()
        results["bds_offline_list"] = await asyncio.to_thread(server.wait_player, False)
        results["checks"].append("client_disconnect_server_confirmed")

    pending = [
        task.get_name() for task in asyncio.all_tasks()
        if not task.done() and task is not asyncio.current_task()
        and task.get_name().startswith(("bedrock-", "endpilot-"))
    ]
    assert not pending, f"EndPilot tasks survived Runtime shutdown: {pending}"
    results["checks"].append("runtime_tasks_closed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", type=Path, required=True)
    args = parser.parse_args()
    python = args.server_python.resolve()
    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    folder = work / "server"
    logfile = work / "bds.log"
    results = {
        "status": "FAIL",
        "endstone": "0.11.14",
        "bds": "1.26.52",
        "protocol": 2193,
        "python": sys.version.split()[0],
        "endpilot_commit": "b5a74d12497ddcc44eddf61ee6813138889ff40f",
        "checks": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    server = None
    try:
        server = Server(python, folder, logfile)
        server.start()
        server.wait_ready()
        results["checks"].append("official_bds_bootstrap_version_verified")
        server.stop()
        server = None

        configure(folder / "server.properties")
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(python, folder, logfile)
        server.start()
        server.wait_ready()
        results["checks"].append("clean_configured_bds_ready")
        asyncio.run(exercise(server, results))
        server.stop()
        server = None
        results["checks"].append("bds_graceful_stop_no_processes")
        results["status"] = "PASS"
    except BaseException as error:
        results["error"] = f"{type(error).__name__}: {error}"
        print(results["error"], file=sys.stderr, flush=True)
    finally:
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                results["cleanup_error"] = f"{type(error).__name__}: {error}"
                results["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(results, ensure_ascii=False), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
