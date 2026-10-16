"""Phase 3 check: start the MCP server over stdio and exercise every tool.

This connects exactly the way the Phase 4 agent will: as an MCP client that
launches `python -m mcp_server.server` as a subprocess.

Run from the repo root with the API server and the sim running:

    python scripts/phase3_check.py            # full check (waits for a delivery, ~5-10 min in a VM)
    python scripts/phase3_check.py --no-wait  # skip waiting for the delivery to finish
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, info: str = "") -> None:
    RESULTS.append((name, ok, info))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f": {info}" if info else ""))


async def call(client: Client, tool: str, args: dict | None = None) -> tuple[bool, object]:
    """Call a tool. Returns (is_error, parsed JSON or error text)."""
    result = await client.call_tool(tool, args or {})
    text = result.content[0].text if result.content else ""
    if result.is_error:
        return True, text
    try:
        return False, json.loads(text)
    except json.JSONDecodeError:
        return False, text


async def wait_for(client: Client, task_id: str, timeout: float, poll: float) -> str:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        err, status = await call(client, "get_task_status", {"task_id": task_id})
        if err:
            return f"error: {status}"
        if status["status"] != last:
            print(f"    {time.strftime('%H:%M:%S')} {status['status']:<10} robot={status['robot'] or '-'}")
            last = status["status"]
        if status["finished"]:
            return status["status"]
        await asyncio.sleep(poll)
    return f"timeout (last: {last})"


async def run(args: argparse.Namespace) -> int:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "RMF_API_URL": args.url},
    )
    async with Client(server) as client:
        print("1. Tools")
        tools = {t.name for t in (await client.list_tools()).tools}
        expected = {"get_fleet_state", "list_locations", "get_task_status", "list_tasks",
                    "dispatch_patrol", "dispatch_delivery", "cancel_task"}
        record("server lists all 7 tools", expected <= tools, ", ".join(sorted(tools)))

        print("\n2. Read-only tools")
        err, fleet = await call(client, "get_fleet_state")
        record("get_fleet_state", not err and fleet["robot_count"] > 0,
               fleet if err else f"{fleet['robot_count']} robots, {fleet['status_counts']}")

        err, locs = await call(client, "list_locations")
        ok = not err and locs["count"] > 0
        record("list_locations", ok, locs if err else
               f"{locs['count']} locations; pickup={locs['pickup_points']} dropoff={locs['dropoff_points']}")
        if not ok:
            print("\nCannot continue without locations.")
            return 1
        names = {l["name"] for l in locs["locations"]}
        pickup = args.pickup if args.pickup in locs["pickup_points"] else locs["pickup_points"][0]
        dropoff = args.dropoff if args.dropoff in locs["dropoff_points"] else locs["dropoff_points"][0]
        patrol_places = [p for p in args.patrol if p in names] or \
            [l["name"] for l in locs["locations"] if not l["is_charger"]][:2]

        err, tasks = await call(client, "list_tasks", {"active_only": False})
        record("list_tasks", not err, tasks if err else f"{tasks['count']} known tasks")

        print("\n3. Guardrails (each should be rejected)")
        err, msg = await call(client, "dispatch_patrol", {"places": ["lounje"]})
        record("unknown location rejected with suggestion", err and "Did you mean" in str(msg), str(msg)[-90:])
        err, msg = await call(client, "dispatch_patrol", {"places": [patrol_places[0]], "rounds": 50})
        record("rounds above limit rejected", err)
        err, msg = await call(client, "dispatch_delivery", {"pickup": dropoff, "dropoff": dropoff})
        record("delivery from a non-pickup point rejected", err and "not a pickup point" in str(msg), str(msg)[-90:])
        err, msg = await call(client, "get_task_status", {"task_id": "no-such-task"})
        record("unknown task ID rejected", err)

        print(f"\n4. Delivery {pickup} -> {dropoff}")
        err, d = await call(client, "dispatch_delivery", {"pickup": pickup, "dropoff": dropoff})
        record("dispatch_delivery", not err, d if err else d["task_id"])
        if not err and not args.no_wait:
            final = await wait_for(client, d["task_id"], args.timeout, args.poll)
            record("delivery completed", final == "completed", final)

        print(f"\n5. Cancel: patrol {patrol_places} x3, then cancel")
        err, p = await call(client, "dispatch_patrol", {"places": patrol_places, "rounds": 3})
        record("dispatch_patrol", not err, p if err else p["task_id"])
        if not err:
            await asyncio.sleep(args.cancel_after)
            err, active = await call(client, "list_tasks")
            record("patrol shows in active tasks", not err and any(t["task_id"] == p["task_id"] for t in active["tasks"]))
            err, c = await call(client, "cancel_task", {"task_id": p["task_id"]})
            record("cancel_task", not err and c["cancel_requested"], c if err else f"was {c['previous_status']}")
            final = await wait_for(client, p["task_id"], 120, args.poll)
            record("patrol ended canceled", final == "canceled", final)
            err, msg = await call(client, "cancel_task", {"task_id": p["task_id"]})
            record("cancelling a finished task rejected", err)

    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("RMF_API_URL", "http://localhost:8000"))
    parser.add_argument("--pickup", default="pantry", help="delivery pickup point")
    parser.add_argument("--dropoff", default="hardware_2", help="delivery drop-off point")
    parser.add_argument("--patrol", nargs="+", default=["coe", "lounge"], help="places for the cancel test")
    parser.add_argument("--no-wait", action="store_true", help="don't wait for the delivery to finish")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for the delivery")
    parser.add_argument("--poll", type=float, default=10, help="seconds between status checks")
    parser.add_argument("--cancel-after", type=float, default=15, help="seconds before cancelling the patrol")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
