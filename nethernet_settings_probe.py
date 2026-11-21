"""Run new-client NetherNet settings, join or position smoke on fresh BDS."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path

from server import ROOT, Server, configure


def server_target(server):
    """Read the player state accepted by BDS, including position and yaw."""
    offset = server.command("querytarget EndPilotTest")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lines = server.snapshot(offset)
        fields = [line.partition("]:")[2].strip() for line in lines]
        start = next((i for i, s in enumerate(fields) if s.startswith("Target data:")), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(fields)) if fields[i] == "]"), None)
            if end is not None:
                raw = fields[start].split("Target data:", 1)[1] + " ".join(fields[start + 1:end + 1])
                targets = json.loads(raw)
                if len(targets) != 1 or targets[0]["dimension"] != 0:
                    raise AssertionError(f"Unexpected querytarget result: {targets}")
                return targets[0]
        time.sleep(0.05)
    raise TimeoutError(f"No BDS querytarget response: {server.snapshot(offset)[-6:]}")


def server_position(server):
    """Read the position accepted by BDS, not a client-side prediction."""
    return server_target(server)["position"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--join", action="store_true")
    parser.add_argument("--position", action="store_true")
    parser.add_argument("--chat", action="store_true", help="Verify two BDS players exchange chat")
    parser.add_argument("--players", action="store_true", help="Observe another BDS player join, move, and leave")
    parser.add_argument("--command", action="store_true", help="Verify player command response and BDS effect")
    parser.add_argument("--refused", action="store_true", help="Require explicit full-server rejection")
    parser.add_argument("--transport", choices=("nethernet", "raknet"), default="nethernet")
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    folder = work / "server"
    results = {
        "status": "FAIL",
        "bds": "1.26.52",
        "endstone": "0.11.14",
        "protocol": 2193,
        "transport": args.transport,
        "endpilot_commit": args.endpilot_commit,
        "installed_endpilot_commit": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": [],
    }
    server = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("--endpilot-commit must be a full lowercase SHA")
        metadata = distribution("endpilot").read_text("direct_url.json")
        installed = json.loads(metadata or "{}").get("vcs_info", {}).get("commit_id")
        results["installed_endpilot_commit"] = installed
        if installed != args.endpilot_commit:
            raise RuntimeError(f"Installed EndPilot commit {installed!r} does not match requested SHA")
        results["checks"].append("endpilot_installed_commit_verified")

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        results["checks"].append("official_bds_bootstrap_version_verified")

        properties = folder / "server.properties"
        configure(properties, args.transport)
        if args.refused:
            content = properties.read_text()
            configured = re.sub(r"(?m)^max-players=\d+$", "max-players=1", content)
            if configured == content:
                raise AssertionError("BDS bootstrap server.properties has no max-players setting")
            properties.write_text(configured)
        shutil.rmtree(folder / "worlds", ignore_errors=True)
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        results["checks"].append("clean_bds_ready")

        url = ("http://127.0.0.1:19132" if args.transport == "nethernet"
               else "raknet://127.0.0.1:19132")
        command = [sys.executable, str(args.probe.absolute()), url]
        if args.refused:
            from endpilot import Bot

            async def run_full_server_probe():
                holder = Bot("EndPilotTest")
                try:
                    if args.transport == "raknet":
                        await holder.join_raknet("127.0.0.1", 19132)
                    else:
                        await holder.join_nethernet(url)
                    await asyncio.to_thread(server.wait_player, True)
                    results["checks"].append("first_player_occupied_only_server_slot")

                    completed = await asyncio.to_thread(
                        subprocess.run, command, capture_output=True, text=True, timeout=45
                    )
                    if completed.stdout:
                        print("[refused] " + completed.stdout.strip(), flush=True)
                    if completed.stderr:
                        print(completed.stderr, file=sys.stderr, flush=True)
                    if (completed.returncode or "login_refused=LoginRefused" not in completed.stdout
                            or "DisconnectFailReason.SERVER_FULL" not in completed.stdout):
                        raise AssertionError(
                            f"Expected explicit BDS login refusal, got exit={completed.returncode}: "
                            f"{completed.stdout} {completed.stderr}"
                        )
                    results["refusal"] = completed.stdout.strip()
                    results["checks"].append("server_refusal_distinguished_from_transport_loss")
                    lines = await asyncio.to_thread(server.wait_player, True)
                    assert not any("EndPilotDenied" in line for line in lines), lines
                    results["checks"].append("refused_player_not_spawned_holder_still_online")
                finally:
                    await holder.close()
                await asyncio.to_thread(server.wait_player, False)
                results["checks"].append("holder_left_bds_list_empty")

            asyncio.run(run_full_server_probe())
        elif args.command:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                server.wait_player(True, timeout=30)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if any("Player Spawned: EndPilotTest" in line for line in server.snapshot()):
                        break
                    time.sleep(0.1)
                else:
                    raise TimeoutError("BDS did not finish player spawn")
                results["checks"].append("command_player_spawned_real_bds")

                # Base the target on the actual BDS player position, not a
                # hard-coded coordinate that can be outside loaded terrain.
                position = server_position(server)
                x = math.floor(float(position["x"])) + 1
                y = math.floor(float(position["y"]))
                z = math.floor(float(position["z"]))
                results["command_target"] = [x, y, z]
                server.command(f"setblock {x} {y} {z} stone")
                time.sleep(0.4)

                def require_block(block):
                    offset = server.command(f"testforblock {x} {y} {z} {block}")
                    until = time.monotonic() + 8
                    while time.monotonic() < until:
                        if any("successfully found" in line.lower() for line in server.snapshot(offset)):
                            return
                        time.sleep(0.1)
                    raise AssertionError(f"BDS did not confirm {block}: {server.snapshot(offset)}")

                require_block("stone")
                results["checks"].append("bds_baseline_stone_confirmed")
                process.stdin.write(f"/setblock {x} {y} {z} gold_block\n")
                process.stdin.flush()

                deadline = time.monotonic() + 22
                feedback = None
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([process.stdout], [], [], 0.2)
                    if readable:
                        line = process.stdout.readline()
                        if line.startswith("command_feedback="):
                            feedback = json.loads(line.partition("=")[2])
                            break
                    if process.poll() is not None:
                        raise AssertionError("Command example exited without BDS feedback")
                if feedback is None:
                    raise TimeoutError("No command feedback from the SDK")
                from bedrock_protocol import TextPacketType
                rawtext = json.loads(feedback["message"]).get("rawtext", [])
                if feedback["type"] != int(TextPacketType.TEXT_OBJECT) or not any(
                    part.get("translate") == "commands.setblock.success" for part in rawtext
                ):
                    raise AssertionError(f"Unexpected BDS command feedback: {feedback}")
                results["command_feedback"] = feedback
                results["checks"].append("typed_bds_command_text_observed")
                require_block("gold_block")
                results["checks"].append("bds_testforblock_confirmed_command_effect")

                process.stdin.write("close\n")
                process.stdin.flush()
                stdout, stderr = process.communicate(timeout=15)
                if stdout:
                    print("[command] " + stdout.strip(), flush=True)
                if stderr:
                    print(stderr, file=sys.stderr, flush=True)
                if process.returncode or "disconnected EndPilotTest" not in stdout:
                    raise AssertionError(f"Command player did not close cleanly: {stdout} {stderr}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            server.wait_player(False, timeout=20)
            results["checks"].append("command_player_left_bds_list_empty")

        elif args.players:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                # Use the file descriptor, not TextIOWrapper.read() plus select:
                # decoded lines can stay buffered after the FD becomes unreadable.
                buffered = bytearray()

                def until(marker, timeout=30):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        newline = buffered.find(b"\n")
                        if newline >= 0:
                            line = bytes(buffered[:newline]).decode(errors="replace").strip()
                            del buffered[:newline + 1]
                            if line:
                                print("[players] " + line, flush=True)
                            if line.startswith(marker):
                                return line.removeprefix(marker)
                            continue
                        readable, _, _ = select.select([process.stdout], [], [], 0.2)
                        if readable:
                            data = os.read(process.stdout.fileno(), 4096)
                            if data:
                                buffered.extend(data)
                        if process.poll() is not None and not buffered:
                            raise AssertionError(
                                f"player observer exited before {marker}: {process.returncode}"
                            )
                    raise TimeoutError(f"client did not emit {marker}")

                until("both_spawned")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    online, lines = server.player_online()
                    if online and "EndPilotObserver" in "\n".join(lines) and "EndPilotTest" in "\n".join(lines):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError("BDS did not list both players")
                results["checks"].append("bds_independently_listed_both_players")

                # Prepare flat ground at the player's actual BDS location.
                initial = server_position(server)
                floor_x = math.floor(float(initial["x"]))
                floor_y = math.floor(float(initial["y"])) - 1
                floor_z = math.floor(float(initial["z"]))
                server.command(
                    f"fill {floor_x - 4} {floor_y} {floor_z - 4} "
                    f"{floor_x + 5} {floor_y} {floor_z + 15} stone"
                )
                server.command(
                    f"tp EndPilotTest {floor_x + 0.5} {floor_y + 1.62} {floor_z + 0.5}"
                )
                time.sleep(0.6)
                before = server_position(server)
                process.stdin.write("observe\n")
                process.stdin.flush()
                spawn = json.loads(until("player_spawn_observed="))
                assert spawn["name"] == "EndPilotTest" and spawn["runtime_id"] > 0, spawn
                # Bedrock unique actor IDs are signed; BDS commonly assigns negatives.
                assert spawn["unique_id"] != 0, spawn
                results["player_spawn"] = spawn
                results["checks"].append("sdk_observed_other_player_spawn_and_unique_id")
                until("player_ready")
                process.stdin.write("go\n")
                process.stdin.flush()

                move = json.loads(until("player_move_observed=", timeout=35))
                assert move["runtime_id"] == spawn["runtime_id"], move
                assert move["submitted_frames"] == 24, move
                until("movement_ready")
                after = server_position(server)
                delta_client = float(move["position"][2]) - float(spawn["position"][2])
                delta_bds = float(after["z"]) - float(before["z"])
                assert delta_client > 0.5 and delta_bds > 0.5, (
                    spawn, move, before, after
                )
                assert abs(float(move["position"][2]) - float(after["z"])) < 1.5, (
                    move, after
                )
                results["player_move"] = {
                    "client_delta_z": delta_client, "bds_delta_z": delta_bds,
                    "bds_before": before, "bds_after": after, "client": move,
                }
                results["checks"].append("other_player_move_observed_by_sdk_and_bds")

                process.stdin.write("close\n")
                process.stdin.flush()
                until("player_removed")
                stdout, stderr = process.communicate(timeout=15)
                tail = buffered.decode(errors="replace") + stdout
                if tail:
                    print("[players] " + tail.strip(), flush=True)
                if stderr:
                    print(stderr, file=sys.stderr, flush=True)
                assert process.returncode == 0 and "both_disconnected" in tail, (
                    process.returncode, tail, stderr
                )
                results["checks"].append("other_player_removed_from_observer_snapshot")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            server.wait_player(False, timeout=20)
            results["checks"].append("player_list_empty_after_both_disconnected")
        elif args.chat:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    online, lines = server.player_online()
                    text = "\n".join(lines)
                    counts = [int(m.group(1)) for line in lines
                              if (m := re.search(r"there are\s+(\d+)\s*(?:/|of a max of)\s*\d+\s+players?\s+online", line, re.I))]
                    if online and counts and counts[-1] == 2 and "EndPilotObserver" in text:
                        break
                    if process.poll() is not None:
                        raise AssertionError("chat example exited before two BDS players joined")
                    time.sleep(0.2)
                else:
                    raise TimeoutError("BDS did not list both chat players")
                results["checks"].append("both_chat_players_in_bds_list")
                process.stdin.write("go\n")
                process.stdin.flush()
                stdout, stderr = process.communicate(timeout=35)
                if stdout:
                    print("[chat] " + stdout.strip(), flush=True)
                if stderr:
                    print(stderr, file=sys.stderr, flush=True)
                match = re.search(r"^chat_received=(.+)$", stdout, re.M)
                if process.returncode or not match:
                    raise AssertionError(f"Chat example did not complete: {stdout} {stderr}")
                observed = json.loads(match.group(1))
                expected = {"author": "EndPilotTest", "message": "endpilot-chat-roundtrip"}
                if observed != expected or "both_disconnected" not in stdout:
                    raise AssertionError(f"Unexpected chat relay or disconnect: {stdout}")
                results["chat"] = observed
                results["checks"].append("server_relayed_player_chat_to_second_sdk_bot")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            server.wait_player(False, timeout=20)
            results["checks"].append("chat_players_left_bds_list_empty")
        elif args.join or args.position:
            command += ["--name", "EndPilotTest", "--hold", "2" if args.position else "15"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                server.wait_player(True, timeout=25)
                results["checks"].append("bds_list_confirmed_player_online")
                if args.position:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        if any("Player Spawned: EndPilotTest" in line for line in server.snapshot()):
                            break
                        if process.poll() is not None:
                            raise RuntimeError("position client exited before BDS spawn")
                        time.sleep(0.1)
                    else:
                        raise TimeoutError("BDS never confirmed player spawn")
                    server.command("tp EndPilotTest 0.5 68 0.5")
                    time.sleep(0.5)
                    authoritative = server_position(server)
                    results["checks"].append("bds_teleport_and_querytarget_confirmed")
                stdout, stderr = process.communicate(timeout=30)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            if stdout:
                print("[join] " + stdout.strip(), flush=True)
            if stderr:
                print(stderr, file=sys.stderr, flush=True)
            if process.returncode != 0 or "spawned EndPilotTest" not in stdout or "disconnected EndPilotTest" not in stdout:
                raise RuntimeError(f"EndPilot player script failed (exit={process.returncode}): {stdout} {stderr}")
            results["checks"].append("client_reached_spawn_and_closed")
            if args.position:
                match = re.search(r"^server_position_update=(.+)$", stdout, re.M)
                if not match:
                    raise AssertionError(f"Client did not observe BDS teleport: {stdout}")
                observed = json.loads(match.group(1))
                if any(abs(float(authoritative[k]) - float(observed[k])) > 0.05 for k in ("x", "y", "z")):
                    raise AssertionError(f"BDS/client positions differ: {authoritative} vs {observed}")
                results["position_sync"] = {"bds": authoritative, "client": observed}
                results["checks"].append("server_teleport_observed_by_client")
            server.wait_player(False, timeout=20)
            results["checks"].append("bds_list_confirmed_player_offline")
        else:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if completed.stdout:
                print("[probe] " + completed.stdout.strip(), flush=True)
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, flush=True)
            if completed.returncode != 0:
                raise RuntimeError(f"EndPilot probe exited with code {completed.returncode}")
            algorithm, threshold = completed.stdout.strip().split()
            results["compression"] = {"algorithm": algorithm, "threshold": int(threshold)}
            results["checks"].append("real_bds_network_settings_received")

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
        (work / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results), flush=True)
    return 0 if results["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
