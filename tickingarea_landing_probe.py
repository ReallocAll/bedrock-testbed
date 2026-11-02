"""Two fresh-world, two-lifecycle, single-Fill-per-world BDS TickingArea preflight.

No teleport, SetBlock, extra Fill, pre-Fill get_block_at, or SDK changes.
Experiment status and CI infrastructure status are intentionally independent.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import distribution, version
from pathlib import Path

SEED = "0"
FILL = "fill 0 99 0 0 99 0 minecraft:stone"
AREA_ADD = "tickingarea add 0 99 0 15 99 15 endpilot_landing false"
AREA_LIST = "tickingarea list"
AREA_REMOVE = "tickingarea remove endpilot_landing"
NAME = "endpilot_landing"
SNAPSHOT_COMMAND = "talobs snapshot"
POSTBLOCK_COMMAND = "talobs postblock"
TIME_SLOTS = (6.0, 12.0, 18.0)
FILL_AT = 20.0
MAX_CONDITION_SECONDS = 110.0
MAX_LIFECYCLES = 2
FILL_OK = re.compile(r"\b(\d+)\s+blocks?\s+filled\b", re.I)
ERROR = re.compile(r"outside of the world|\bfailed\b|unknown command|syntax error|permission denied|not allowed|too many blocks|no targets matched|could not", re.I)
ADD_OK = re.compile(r"(?:added|created)\s+(?:a\s+)?ticking\s+area|ticking\s+area\s+(?:added|created)", re.I)
REMOVE_OK = re.compile(r"(?:removed|deleted)\s+(?:a\s+)?ticking\s+area|ticking\s+area\s+(?:removed|deleted)", re.I)
LOG_RE = re.compile(r"^\[[^]]+\s+(INFO|ERROR|WARNING)\]:\s*(.*)$")
OBS_MARK = "TAL_OBSERVER "


class Blocked(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def emit(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    if len(data) > 2_000_000:
        raise Blocked("evidence_exceeds_2MB")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def payload(line: str):
    match = LOG_RE.search(line)
    return (match.group(1), match.group(2)) if match else (None, "")


def terminal(line: str, kind: str):
    level, content = payload(line)
    if level is None:
        return None
    if kind == "fill":
        if ERROR.search(content) or level == "ERROR":
            return ("failure", None)
        match = FILL_OK.search(content)
        return ("success" if int(match.group(1)) == 1 else "no_change_or_wrong_count",
                int(match.group(1))) if match else None
    if kind == "add":
        if ERROR.search(content) or level == "ERROR":
            return ("failure", None)
        return ("success", None) if ADD_OK.search(content) else None
    if kind == "remove":
        if ERROR.search(content) or level == "ERROR":
            return ("failure", None)
        return ("success", None) if REMOVE_OK.search(content) else None
    return None


class GuardedServer:
    """Fail closed at the console boundary, including duplicate submissions."""
    def __init__(self, wrapped, condition):
        self.wrapped = wrapped
        self.condition = condition
        self.commands = []
        self.fill_count = 0
        self.add_count = 0
        self.remove_count = 0

    def send(self, cmd):
        if cmd.startswith("fill "):
            if cmd != FILL or self.fill_count or len([r for r in self.commands if r == FILL]):
                raise Blocked("fill_command_budget_or_whitelist_violation")
            self.fill_count += 1
        elif cmd.startswith("tickingarea "):
            if self.condition != "B":
                raise Blocked("tickingarea_forbidden_in_control")
            if cmd == AREA_ADD:
                if self.add_count:
                    raise Blocked("duplicate_tickingarea_add")
                self.add_count += 1
            elif cmd == AREA_REMOVE:
                if self.remove_count:
                    raise Blocked("duplicate_tickingarea_remove")
                self.remove_count += 1
            elif cmd != AREA_LIST:
                raise Blocked("unauthorized_tickingarea_command")
        elif cmd not in ("list", "querytarget EndPilotTest") and not re.fullmatch(
                r"talobs (?:snapshot|postblock) [0-9a-f]{32}", cmd):
            raise Blocked("console_command_not_allowlisted:" + cmd[:80])
        if cmd.startswith("talobs postblock") and self.fill_count != 1:
            raise Blocked("intrusive_block_read_before_fill")
        self.commands.append(cmd)
        return self.wrapped.command(cmd)

    def snapshot(self, offset=0):
        return self.wrapped.snapshot(offset)

    @property
    def process(self):
        return self.wrapped.process


def new_row(condition: str):
    return {
        "condition": condition, "transport": "raknet", "seed": SEED,
        "tick_distance": 4, "tickingarea_enabled": condition == "B",
        "tickingarea_add_status": None, "tickingarea_remove_status": None,
        "chunk_loaded_before": None, "chunk_loaded_after": None,
        "fill_status": None, "reported_block_count": None,
        "block_postcondition": None, "time_from_ready_ms": None,
        "error": None, "cleanup_status": None,
        "command_events": [], "chunk_snapshots": [], "time_slots_seconds": list(TIME_SLOTS),
        "world_isolation": "fresh_same_seed_independent_world_not_byte_identical",
        "worlds_absent_before_start": None, "actual_properties": None,
        "position": None, "ready_monotonic": None,
        "process": {"start_count": 0, "stop_attempted": False, "stop_success": None},
        "runtime_cleanup": {"entered": False, "exited": False, "exit_error": None},
        "tickingarea_list": None, "tickingarea_final_list": None,
        "observer_source": "endstone.dimension.loaded_chunks",
        "load_state_verified": False,
    }


async def pause_until(when):
    delay = when - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


async def console_reply(s: GuardedServer, cmd: str, kind: str, timeout=6.0):
    offset = s.send(cmd)
    start = time.monotonic()
    deadline = start + timeout
    terminals = []
    seen = set()
    while time.monotonic() < deadline:
        for i, line in enumerate(s.snapshot(offset)):
            if i in seen:
                continue
            seen.add(i)
            result = terminal(line, kind)
            if result is not None:
                terminals.append((i, result))
        if terminals:
            await asyncio.sleep(.35)  # Late/duplicate terminal outputs invalidate success.
            break
        if s.process is None or s.process.poll() is not None:
            break
        await asyncio.sleep(.05)
    lines = s.snapshot(offset)
    outcomes = [terminal(line, kind) for line in lines]
    outcomes = [x for x in outcomes if x is not None]
    # A single-flight console stream gives ordering, not an on-wire request ID.
    status = outcomes[0][0] if len(outcomes) == 1 else ("timeout" if not outcomes else "ambiguous")
    record = {"command": cmd, "kind": kind, "log_offset": offset,
              "sent_monotonic": start, "duration_ms": round((time.monotonic()-start)*1000, 2),
              "status": status, "raw_lines": lines[-35:],
              "reported_block_count": outcomes[0][1] if len(outcomes) == 1 else None,
              "unique_terminal_count": len(outcomes)}
    return record


async def observer(s: GuardedServer, operation: str, timeout=5.0):
    rid = uuid.uuid4().hex
    cmd = f"talobs {operation} {rid}"
    offset = s.send(cmd)
    until = time.monotonic() + timeout
    matches = []
    while time.monotonic() < until:
        matches = []
        for line in s.snapshot(offset):
            if OBS_MARK not in line:
                continue
            try:
                payload_obj = json.loads(line.split(OBS_MARK, 1)[1])
            except ValueError:
                continue
            if payload_obj.get("request_id") == rid and payload_obj.get("operation") == operation:
                matches.append(payload_obj)
        if matches:
            await asyncio.sleep(.15)
            break
        await asyncio.sleep(.05)
    if len(matches) != 1 or matches[0].get("status") != "ok":
        return {"status": "unverified", "request_id": rid,
                "records": matches[:3], "raw_lines": s.snapshot(offset)[-12:]}
    return matches[0]


async def query_position(s: GuardedServer):
    offset = s.send("querytarget EndPilotTest")
    until = time.monotonic() + 6
    while time.monotonic() < until:
        lines = s.snapshot(offset)
        cleaned = [line.partition("]:")[2].strip() for line in lines]
        start = next((i for i,v in enumerate(cleaned) if v.startswith("Target data:")), None)
        if start is not None:
            end = next((i for i in range(start+1,len(cleaned)) if cleaned[i] == "]"), None)
            if end is not None:
                raw = cleaned[start].split("Target data:",1)[1] + " ".join(cleaned[start+1:end+1])
                targets = json.loads(raw)
                if len(targets) != 1 or targets[0].get("dimension") != 0:
                    raise Blocked("authoritative_dimension_or_player_mismatch")
                return {"dimension":0,"position":targets[0].get("position"),"raw":lines[-25:]}
        await asyncio.sleep(.05)
    raise Blocked("querytarget_not_correlated")


async def area_list(s: GuardedServer):
    offset = s.send(AREA_LIST)
    await asyncio.sleep(.65)
    lines = s.snapshot(offset)
    # Preserve original output. Name alone is evidence of registration, not loading.
    return {"command": AREA_LIST, "raw_lines": lines[-35:],
            "contains_name": any(NAME.lower() in payload(line)[1].lower() for line in lines),
            "error": any(ERROR.search(payload(line)[1]) for line in lines)}


async def condition_run(s: GuardedServer, row: dict):
    from endpilot import BotOptions, Runtime
    # 'Ready' = public SDK Ready plus BDS list and server-authoritative position.
    async with Runtime() as runtime:
        row["runtime_cleanup"]["entered"] = True
        bot = runtime.add_bot(BotOptions(
            server_url="raknet://127.0.0.1:19132", transport="raknet",
            protocol=2193, name="EndPilotTest", chunk_radius=8,
            client_cache=False, validate_all_packets=True,
        ))
        try:
            await bot.wait_ready(70)
            await asyncio.to_thread(s.wrapped.wait_player, True, 15)
            row["position"] = await query_position(s)
            if bot.get_status().dimension != 0:
                raise Blocked("bot_not_in_overworld")
            ready = time.monotonic()
            row["ready_monotonic"] = ready
            if s.condition == "B":
                cmd = await console_reply(s, AREA_ADD, "add")
                row["command_events"].append(cmd)
                listed = await area_list(s)
                row["tickingarea_list"] = listed
                row["tickingarea_add_status"] = "accepted" if (cmd["status"] == "success"
                    and listed["contains_name"] and not listed["error"]) else "unverified"
                if row["tickingarea_add_status"] != "accepted":
                    raise Blocked("tickingarea_add_or_list_not_proven")
            for slot in TIME_SLOTS:
                await pause_until(ready + slot)
                snap = await observer(s, "snapshot")
                row["chunk_snapshots"].append({"slot": slot, "actual_from_ready_ms":
                    round((time.monotonic()-ready)*1000,2), "record": snap})
            before = row["chunk_snapshots"][-1]["record"]
            if before.get("status") == "ok" and type(before.get("loaded")) is bool:
                row["chunk_loaded_before"] = before["loaded"]
                row["load_state_verified"] = True
            await pause_until(ready + FILL_AT)
            row["time_from_ready_ms"] = round((time.monotonic()-ready)*1000,2)
            command = await console_reply(s, FILL, "fill", timeout=7)
            row["command_events"].append(command)
            row["fill_status"] = command["status"]
            row["reported_block_count"] = command["reported_block_count"]
            if command["status"] in ("timeout", "ambiguous"):
                raise Blocked("fill_terminal_response_not_unique")
            # Capture loaded state *before* potentially intrusive post-block read.
            after = await observer(s, "snapshot")
            row["chunk_snapshot_after"] = after
            if after.get("status") == "ok" and type(after.get("loaded")) is bool:
                row["chunk_loaded_after"] = after["loaded"]
            post = await observer(s, "postblock")
            row["block_postcondition"] = {"status": post.get("status"),
                  "block_type": post.get("block_type"), "read_intrusive": True,
                  "readback_request": post.get("request_id"), "record": post}
        finally:
            # Any exception, cancellation, or timeout exits the Runtime context.
            row["runtime_cleanup"]["exit_started"] = True
    row["runtime_cleanup"]["exited"] = True


def summarize(rows, complete):
    if not complete or len(rows) != 2:
        return "EXPERIMENT_BLOCKED"
    if any(x["error"] is not None for x in rows):
        return "EXPERIMENT_INCONCLUSIVE" if all(x["fill_status"] is not None for x in rows) else "EXPERIMENT_BLOCKED"
    if any(not x["load_state_verified"] for x in rows):
        return "LOAD_STATE_UNVERIFIED"
    if any(x["fill_status"] not in ("success", "failure", "no_change_or_wrong_count") for x in rows):
        return "EXPERIMENT_INCONCLUSIVE"
    passed = [x["fill_status"] == "success" and x["reported_block_count"] == 1
              and (x["block_postcondition"] or {}).get("block_type") == "minecraft:stone"
              for x in rows]
    if any(x["fill_status"] == "success" and not p for x,p in zip(rows,passed)):
        return "EXPERIMENT_INCONCLUSIVE"
    if not all(x["cleanup_status"] == "complete" for x in rows):
        return "EXPERIMENT_INCONCLUSIVE"
    a,b = rows
    if b["tickingarea_add_status"] != "accepted" or b["tickingarea_remove_status"] != "verified_removed":
        return "EXPERIMENT_INCONCLUSIVE"
    if abs(a["time_from_ready_ms"] - b["time_from_ready_ms"]) > 1000:
        return "EXPERIMENT_INCONCLUSIVE"
    if a["actual_properties"] != b["actual_properties"] or a["position"]["position"] != b["position"]["position"]:
        return "EXPERIMENT_INCONCLUSIVE"
    if not passed[0] and passed[1]:
        return "CONTROL_FAIL_TICKINGAREA_PASS"
    if passed[0] and passed[1]:
        return "BOTH_PASS"
    if not passed[0] and not passed[1]:
        return "BOTH_FAIL"
    return "CONTROL_PASS_TICKINGAREA_FAIL"


def write_properties(folder: Path, base: dict):
    folder.mkdir(parents=True, exist_ok=False)
    config = dict(base)
    config.update({"level-seed": SEED, "level-name": "EndPilot TickingArea",
                   "transport": "raknet", "tick-distance": "4", "view-distance": "12"})
    path = folder / "server.properties"
    path.write_text("".join(f"{k}={v}\n" for k,v in config.items()),encoding="utf-8")
    return config


def read_properties(path: Path):
    data = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key,value = raw.split("=",1)
        if key in data:
            raise Blocked("duplicate_effective_property:"+key)
        data[key] = value
    return data


def preflight(args):
    if args.transport != "raknet" or sys.version_info[:2] != (3,12):
        raise Blocked("client_python_or_transport_mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}",args.endpilot_commit):
        raise Blocked("invalid_endpilot_commit")
    direct = distribution("endpilot").read_text("direct_url.json")
    if not direct or json.loads(direct).get("vcs_info",{}).get("commit_id") != args.endpilot_commit:
        raise Blocked("installed_endpilot_commit_mismatch")
    if version("bedrock-protocol-native") != "0.5.0":
        raise Blocked("native_protocol_version_mismatch")
    check = subprocess.check_output([str(args.server_python.absolute()), "-c",
        'import json,sys;from importlib.metadata import version;print(json.dumps([list(sys.version_info[:2]),version("endstone")]))'],
        text=True,timeout=10)
    if json.loads(check) != [[3,13],"0.11.14"]:
        raise Blocked("server_python_or_endstone_mismatch")
    if not args.plugin_wheel.is_file() or args.plugin_wheel.suffix != ".whl":
        raise Blocked("observer_wheel_unavailable")


def self_test():
    assert terminal("[02:00:00 INFO]: 1 blocks filled", "fill") == ("success",1)
    assert terminal("[02:00:00 ERROR]: Cannot place blocks outside of the world", "fill") == ("failure",None)
    assert terminal("[02:00:00 INFO]: 0 blocks filled", "fill") == ("no_change_or_wrong_count",0)
    assert terminal("[02:00:00 INFO]: Added ticking area 'endpilot_landing'", "add") == ("success",None)
    assert terminal("[02:00:00 INFO]: Removed ticking area 'endpilot_landing'", "remove") == ("success",None)
    class Fake:
        def __init__(self):
            self.sent=[]
        def command(self,c):
            self.sent.append(c);return len(self.sent)
    a=GuardedServer(Fake(),"A")
    for command in (AREA_ADD, AREA_LIST, AREA_REMOVE,"tp EndPilotTest 0 0 0", "setblock 0 99 0 stone"):
        try: a.send(command)
        except Blocked: pass
        else: raise AssertionError("A unexpectedly admitted "+command)
    assert not a.commands
    a.send(FILL)
    try: a.send(FILL)
    except Blocked: pass
    else: raise AssertionError("double Fill admitted")
    b=GuardedServer(Fake(),"B");b.send(AREA_ADD);b.send(AREA_LIST);b.send(FILL);b.send(AREA_REMOVE)
    for cmd in (AREA_ADD, AREA_REMOVE):
        try: b.send(cmd)
        except Blocked: pass
        else: raise AssertionError("duplicate tickingarea admitted")
    assert b.add_count==b.remove_count==b.fill_count==1
    assert not any("tp " in c or "setblock" in c for c in b.commands)
    print("SELF_TEST_PASS: command ACL, singleton Fill, singleton area, oracle",flush=True)
    return 0


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--self-test",action="store_true")
    parser.add_argument("--server-python",type=Path)
    parser.add_argument("--plugin-wheel",type=Path)
    parser.add_argument("--endpilot-commit")
    parser.add_argument("--transport",choices=("raknet",),default="raknet")
    args=parser.parse_args()
    if args.self_test:
        return self_test()
    from server import ROOT, Server, PROPERTIES
    work=ROOT/"work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    results={"experiment_status":"EXPERIMENT_BLOCKED", "ci_status":"FAIL",
             "started_at_utc":utc(),"conditions":[],"bds_lifecycles":0,
             "endpilot_commit":args.endpilot_commit,"seed":SEED,
             "baseline_type":"two_fresh_generated_worlds_same_seed_not_byte_identical",
             "confounders": ["fresh world generation may diverge despite same seed",
                             "postblock read is intrusive after Fill, not a prestate measurement"],
             "error":None}
    try:
        preflight(args)
        for label in ("A","B"):
            row=new_row(label)
            results["conditions"].append(row)
            folder=work/("server-"+label)
            assert not folder.exists()
            desired=write_properties(folder,PROPERTIES)
            row["worlds_absent_before_start"] = not (folder/"worlds").exists()
            (folder/"plugins").mkdir()
            shutil.copy2(args.plugin_wheel,folder/"plugins"/args.plugin_wheel.name)
            row["prestart_properties"]=desired
            row["prestart_sha256"]=hashlib.sha256((folder/"server.properties").read_bytes()).hexdigest()
            # All file and preflight steps happen before beginning a BDS lifecycle.
            wrapped=Server(args.server_python.absolute(),folder,work/"bds.log")
            guard=GuardedServer(wrapped,label)
            try:
                if results["bds_lifecycles"] >= MAX_LIFECYCLES:
                    raise Blocked("BDS_lifecycle_budget_exceeded")
                results["bds_lifecycles"]+=1
                row["process"]["start_count"]+=1
                row["process"]["start_utc"]=utc()
                wrapped.start()
                wrapped.wait_ready(420)
                if not any("Version: 1.26.52.3" in line for line in wrapped.snapshot()):
                    raise Blocked("BDS_patch_version_unverified")
                observer_deadline = time.monotonic() + 15.0
                while (not any("TAL_OBSERVER_READY" in line for line in wrapped.snapshot())
                       and time.monotonic() < observer_deadline and wrapped.process.poll() is None):
                    time.sleep(.1)
                if not any("TAL_OBSERVER_READY" in line for line in wrapped.snapshot()):
                    raise Blocked("observer_not_ready")
                row["actual_properties"]=read_properties(folder/"server.properties")
                if any(row["actual_properties"].get(k)!=v for k,v in desired.items()):
                    raise Blocked("effective_properties_mismatch")
                row["version_file"]=(folder/"version.txt").read_text().strip()
                row["generated_world_evidence"] = {"worlds_dir_exists":(folder/"worlds").is_dir(),
                    "created_vanilla_world_in_log":any("CREATING VANILLA WORLD" in line for line in wrapped.snapshot())}
                if not row["generated_world_evidence"]["worlds_dir_exists"]:
                    raise Blocked("world_missing")
                asyncio.run(asyncio.wait_for(condition_run(guard,row),timeout=MAX_CONDITION_SECONDS))
            except BaseException as error:
                row["error"]=f"{type(error).__name__}: {error}"[:500]
            finally:
                if label == "B" and wrapped.process is not None and wrapped.process.poll() is None:
                    # Removal is mandatory even after add/list or Fill fails.
                    try:
                        if guard.add_count:
                            cleanup=asyncio.run(console_reply(guard,AREA_REMOVE,"remove",timeout=5))
                            row["command_events"].append(cleanup)
                            listed=asyncio.run(area_list(guard))
                            row["tickingarea_final_list"]=listed
                            row["tickingarea_remove_status"] = (
                                "verified_removed" if cleanup["status"]=="success"
                                and not listed["contains_name"] and not listed["error"] else "unverified")
                    except BaseException as error:
                        row["tickingarea_remove_status"]="cleanup_error:"+str(error)[:200]
                row["process"]["stop_attempted"]=True
                try:
                    wrapped.stop()
                    row["process"]["stop_success"]=True
                except BaseException as error:
                    row["process"]["stop_success"]=False
                    row["process"]["stop_error"]=str(error)[:300]
                row["process"]["stop_utc"]=utc()
                row["process"]["observer_disable_observed"] = any(
                    "TAL_OBSERVER_DISABLED" in line for line in wrapped.snapshot())
                row["cleanup_status"] = "complete" if (row["process"]["stop_success"] and
                      row["runtime_cleanup"]["exited"] and row["process"]["observer_disable_observed"] and
                      (label=="A" or (row["tickingarea_remove_status"] == "verified_removed"))) else "incomplete"
                row["allowlisted_commands"]=guard.commands
                for src,dest in ((folder/"server.properties", work/(label+"-server.properties")),
                                 (folder/"version.txt",work/(label+"-version.txt"))):
                    if src.exists(): shutil.copy2(src,dest)
                emit(work/"tickingarea_landing.json",results)
                if row["error"] or row["cleanup_status"]!="complete":
                    break
        results["experiment_status"]=summarize(results["conditions"], len(results["conditions"])==2)
        results["ci_status"]="PASS" if (len(results["conditions"])==2
            and all(r["cleanup_status"]=="complete" and r["error"] is None for r in results["conditions"])
            and results["bds_lifecycles"] == 2) else "FAIL"
    except BaseException as error:
        results["error"]=f"{type(error).__name__}: {error}"[:500]
    finally:
        results["finished_at_utc"]=utc()
        emit(work/"tickingarea_landing.json",results)
        emit(work/"result.json",results)
        print(json.dumps({k:results[k] for k in ("experiment_status","ci_status","bds_lifecycles","error")},ensure_ascii=False),flush=True)
    return 0 if results["ci_status"]=="PASS" else 1


if __name__=="__main__":
    sys.exit(main())
