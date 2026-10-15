"""Phase 2 check: read fleet state, dispatch a task over HTTP, track it to the end.

Run from the repo root, with the API server and the sim (launched with
server_uri) both running:

    python scripts/phase2_check.py
    python scripts/phase2_check.py --cancel-test
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from mcp_server.rmf_client import DEFAULT_URL, RMFClient, RMFError  # noqa: E402


def print_robots(client: RMFClient) -> None:
    robots = client.get_robots()
    if not robots:
        print("  No robots reported. Was the sim launched with server_uri?")
        return
    print(f"  {'robot':<12} {'status':<9} {'battery':>7}  {'x':>7} {'y':>7}  task")
    for r in robots:
        print(
            f"  {r['name']:<12} {str(r['status']):<9} {str(r['battery_pct']) + '%':>7}"
            f"  {str(r['x']):>7} {str(r['y']):>7}  {r['task_id'] or '-'}"
        )


def log_update(summary: dict) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"  [{stamp}] {summary['status']:<10} robot={summary['robot'] or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--places", nargs="+", default=["coe", "lounge"])
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for the task")
    parser.add_argument("--cancel-test", action="store_true", help="also dispatch and cancel a task")
    args = parser.parse_args()

    try:
        with RMFClient(args.url) as client:
            print("1. Fleet state")
            print_robots(client)

            print(f"\n2. Dispatching patrol {args.places} x{args.rounds}")
            task_id = client.dispatch_patrol(args.places, args.rounds)
            print(f"  task_id = {task_id}")

            print("\n3. Tracking task (Ctrl+C to stop)")
            final = client.wait_for_task(task_id, timeout_s=args.timeout, on_update=log_update)
            status = final["status"] if final else "unknown"
            print(f"  Final status: {status}")

            if args.cancel_test:
                print("\n4. Cancel test")
                cancel_id = client.dispatch_patrol(args.places, rounds=3)
                print(f"  dispatched {cancel_id}, cancelling in 15 s")
                time.sleep(15)
                print(f"  cancel response: {client.cancel_task(cancel_id)}")
                final_cancel = client.wait_for_task(cancel_id, timeout_s=120, on_update=log_update)
                print(f"  Final status: {final_cancel['status'] if final_cancel else 'unknown'}")

            print("\n5. Fleet state after")
            print_robots(client)

            return 0 if status == "completed" else 1

    except httpx.ConnectError:
        print(f"Could not reach the API server at {args.url}. Is the Docker container running?")
    except RMFError as e:
        print(f"RMF error: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
