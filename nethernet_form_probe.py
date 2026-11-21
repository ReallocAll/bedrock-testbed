"""Verify a public EndPilot external action-form response on real BDS."""

import argparse
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from importlib.metadata import distribution
from pathlib import Path

from server import ROOT, Server, configure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-python", required=True, type=Path)
    parser.add_argument("--endpilot-commit", required=True)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--transport", choices=("nethernet", "raknet"), required=True)
    args = parser.parse_args()

    work = ROOT / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    result = {
        "status": "FAIL", "bds": "1.26.52", "endstone": "0.11.14",
        "protocol": 2193, "transport": args.transport,
        "endpilot_commit": args.endpilot_commit, "checks": [],
    }
    server = client = None
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.endpilot_commit):
            raise ValueError("expected exact EndPilot commit SHA")
        installed = json.loads(distribution("endpilot").read_text("direct_url.json") or "{}").get(
            "vcs_info", {},
        ).get("commit_id")
        assert installed == args.endpilot_commit, (installed, args.endpilot_commit)
        result["checks"].append("installed_exact_sdk")

        folder = work / "server"
        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()
        server.stop()
        server = None
        configure(folder / "server.properties", args.transport)
        shutil.rmtree(folder / "worlds", ignore_errors=True)

        wheels = work / "wheels"
        wheels.mkdir()
        subprocess.run(
            [str(args.server_python.absolute()), "-m", "pip", "wheel", "--no-deps",
             "--wheel-dir", str(wheels), str(ROOT / "form_probe")],
            check=True,
        )
        plugins = folder / "plugins"
        plugins.mkdir(exist_ok=True)
        shutil.copy2(next(wheels.glob("endstone_form_probe-*.whl")), plugins)

        server = Server(args.server_python.absolute(), folder, work / "bds.log")
        server.start()
        server.wait_ready()

        def server_has(marker, timeout=12):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                lines = server.snapshot()
                if any(marker in line for line in lines):
                    return
                if server.process.poll() is not None:
                    raise RuntimeError("BDS exited while waiting for " + marker)
                time.sleep(0.1)
            raise AssertionError(f"BDS missing {marker}: {server.snapshot()[-25:]}")

        server_has("form_probe_enabled")
        result["checks"].append("endstone_form_probe_enabled")

        url = ("http://127.0.0.1:19132" if args.transport == "nethernet"
               else "raknet://127.0.0.1:19132")
        client = subprocess.Popen(
            [sys.executable, str(args.probe.absolute()), url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        output = queue.Queue()

        def forward():
            for line in client.stdout:
                print("[client] " + line.rstrip(), flush=True)
                output.put(line)
        threading.Thread(target=forward, daemon=True).start()

        def until(marker, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    if client.poll() is not None:
                        raise AssertionError("client exited before " + marker)
                    continue
                if marker in line:
                    return line.partition(marker)[2].strip()
            raise TimeoutError("missing client marker " + marker)

        def send(value):
            assert client.stdin is not None
            client.stdin.write(value + "\n")
            client.stdin.flush()

        server.wait_player(True)
        until("spawned EndPilotTest")
        until("form_watch_ready")
        result["checks"].append("public_packet_watcher_armed_before_request")
        send("trigger")
        server_has("form_probe_sent=EndPilotTest")
        answer = json.loads(until("action_form_answer_sent="))
        assert isinstance(answer["form_id"], int) and answer["form_id"] >= 0, answer
        assert answer["title"] == {"rawtext": [{"text": "EndPilot probe"}]}, answer
        assert answer["button_index"] == 1, answer
        result["response"] = answer
        result["checks"].append("server_form_id_observed_and_typed_reply_sent")

        # The plugin callback proves that BDS accepted the form response, not
        # just that EndPilot submitted a packet to its transport.
        server_has("form_probe_submit=EndPilotTest:1")
        result["checks"].append("endstone_server_form_submit_callback")

        send("close")
        until("disconnected EndPilotTest")
        assert client.wait(timeout=10) == 0, client.returncode
        server.wait_player(False)
        result["checks"].append("no_remaining_players")
        server.stop()
        server = None
        result["checks"].append("bds_clean_stop")
        result["status"] = "PASS"
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc()
    finally:
        if client is not None and client.poll() is None:
            client.kill()
            client.communicate()
        if server is not None:
            try:
                server.stop()
            except BaseException as error:
                result["cleanup_error"] = repr(error)
                result["status"] = "FAIL"
        (work / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
