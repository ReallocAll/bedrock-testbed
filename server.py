"""Linux Endstone/BDS process and server command lifecycle."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAYER = "EndPilotTest"
VERSION_LINE = "This server is running Endstone version: 0.11.14 (Minecraft: 26.52)"
PROPERTIES = {
    "transport": "raknet",
    "enable-lan-visibility": "false",
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


def configure(path, transport):
    properties = {**PROPERTIES, "transport": transport}
    if not path.is_file():
        raise FileNotFoundError(f"Endstone did not create {path}")
    updated = set()
    lines = []
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in properties:
                line = key + "=" + properties[key]
                updated.add(key)
        lines.append(line)
    lines.extend(k + "=" + v for k, v in properties.items() if k not in updated)
    path.write_text("\n".join(lines) + "\n")
