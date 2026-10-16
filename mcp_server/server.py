"""MCP server exposing Open-RMF fleet tools to an LLM.

Run from the repo root (stdio transport):

    python -m mcp_server.server

Configuration (environment variables):
    RMF_API_URL         rmf-web API server URL (default http://localhost:8000)
    RMF_MCP_LOG_LEVEL   server log level on stderr (default INFO; the agent CLI sets WARNING)

Never print() in this module: over stdio, stdout carries the MCP protocol.
Logging goes to stderr.
"""

from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from typing import Annotated, Any, Callable, TypeVar

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_server.locations import Location, LocationIndex, parse_locations
from mcp_server.rmf_client import (
    ACTIVE_STATUSES,
    DEFAULT_URL,
    TERMINAL_STATUSES,
    RMFClient,
    RMFError,
    summarize_task,
)

LOG_LEVEL = os.environ.get("RMF_MCP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(stream=sys.stderr, level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rmf-fleet-mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)  # don't log every HTTP request

# -- guardrails (enforced here, not just in the agent's prompt) ---------------
MAX_PATROL_PLACES = 6
MAX_ROUNDS = 5
MAX_ACTIVE_TASKS = 6

API_URL = os.environ.get("RMF_API_URL", DEFAULT_URL)

INSTRUCTIONS = """\
Tools for commanding a simulated fleet of autonomous mobile robots (AMRs) managed by Open-RMF.
You decide WHAT should be done; Open-RMF decides WHICH robot does it, plans paths and avoids collisions.

Guidelines:
- Call list_locations before using any place name, and use the names exactly as listed.
- Call get_fleet_state before dispatching to see which robots are free and their battery.
- To send a robot to one place, use dispatch_patrol with a single place and rounds=1.
- Deliveries can only start at pickup points and end at drop-off points (see list_locations).
- Dispatch tools return a task_id. Use get_task_status to follow it; tasks take minutes in simulation.
- Never assume a task finished: check its status.
"""

mcp = MCPServer("rmf-fleet", instructions=INSTRUCTIONS, log_level=LOG_LEVEL)
_client = RMFClient(API_URL)
_index: LocationIndex | None = None

T = TypeVar("T")


# -- helpers -------------------------------------------------------------------

def _rmf(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call the RMF client and turn failures into errors the LLM can read."""
    try:
        return fn(*args, **kwargs)
    except RMFError as e:
        raise ToolError(str(e)) from e
    except httpx.ConnectError as e:
        raise ToolError(f"Cannot reach the RMF API server at {API_URL}. Is it running?") from e
    except httpx.HTTPError as e:
        raise ToolError(f"Error talking to the RMF API server: {e}") from e


def _get_index(refresh: bool = False) -> LocationIndex | None:
    """Location index from the building map, cached after the first success."""
    global _index
    if _index is None or refresh:
        building_map = _rmf(_client.get_building_map)
        if building_map:
            index = LocationIndex(parse_locations(building_map))
            if len(index):
                _index = index
                log.info("Loaded %d named locations from the building map", len(index))
    return _index


def _require_index() -> LocationIndex:
    index = _get_index()
    if index is None:
        raise ToolError(
            "The building map isn't available from the API server yet, so locations can't be checked. "
            "Make sure the simulation is running, then try again."
        )
    return index


def _unknown_location(name: str, index: LocationIndex) -> ToolError:
    hint = index.suggest(name)
    did_you_mean = f" Did you mean: {', '.join(hint)}?" if hint else ""
    return ToolError(f"Unknown location '{name}'.{did_you_mean} Call list_locations for valid names.")


def _resolve(name: str, index: LocationIndex) -> Location:
    loc = index.resolve(name)
    if loc is None:
        raise _unknown_location(name, index)
    return loc


def _check_capacity() -> None:
    active = _rmf(_client.list_tasks, statuses=ACTIVE_STATUSES)
    if len(active) >= MAX_ACTIVE_TASKS:
        raise ToolError(
            f"{len(active)} tasks are already active (limit {MAX_ACTIVE_TASKS}). "
            "Wait for some to finish or cancel one before dispatching more."
        )


# -- read-only tools ------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(title="Get fleet state", read_only_hint=True))
def get_fleet_state() -> dict:
    """Current state of every robot: status (idle, working, charging, error, ...),
    battery percentage, position (map, x, y in metres) and the task it is working on."""
    robots = _rmf(_client.get_robots)
    result: dict[str, Any] = {
        "robot_count": len(robots),
        "status_counts": dict(Counter(r["status"] for r in robots)),
        "robots": robots,
    }
    if not robots:
        result["note"] = (
            "No robots reported. The simulation may not be running, or it was not "
            "launched with server_uri pointing at the API server."
        )
    return result


@mcp.tool(annotations=ToolAnnotations(title="List locations", read_only_hint=True))
def list_locations() -> dict:
    """Named locations robots can go to, read from the building map. Use these exact
    names in dispatch_patrol and dispatch_delivery. can_pickup / can_dropoff mark the
    places where deliveries can start and end; is_charger marks charging stations."""
    index = _get_index(refresh=True)
    if index is None:
        raise ToolError(
            "The building map isn't available from the API server yet. "
            "Make sure the simulation is running, then try again."
        )
    return {
        "count": len(index),
        "pickup_points": index.pickups(),
        "dropoff_points": index.dropoffs(),
        "chargers": index.chargers(),
        "locations": [loc.to_dict() for loc in sorted(index.locations.values(), key=lambda l: l.name)],
    }


@mcp.tool(annotations=ToolAnnotations(title="Get task status", read_only_hint=True))
def get_task_status(
    task_id: Annotated[str, Field(description="Task ID returned by a dispatch tool or list_tasks")],
) -> dict:
    """Status of one task: queued, underway, completed, failed, canceled, ...,
    plus the robot it was assigned to. 'finished' is true once the task has ended."""
    summary = _rmf(_client.get_task_summary, task_id)
    if summary is None:
        raise ToolError(f"Unknown task ID '{task_id}'. Use list_tasks to see known tasks.")
    summary["finished"] = summary["status"] in TERMINAL_STATUSES
    return summary


@mcp.tool(annotations=ToolAnnotations(title="List tasks", read_only_hint=True))
def list_tasks(
    active_only: Annotated[bool, Field(description="Only queued or running tasks (default). False includes finished ones.")] = True,
    limit: Annotated[int, Field(ge=1, le=50, description="Maximum number of tasks to return")] = 20,
) -> dict:
    """Tasks known to the fleet manager, with status and assigned robot.
    Use it to see what the fleet is already doing before dispatching more work."""
    states = _rmf(_client.list_tasks, statuses=ACTIVE_STATUSES if active_only else None)
    tasks = [summarize_task(s) for s in states][-limit:]
    return {"count": len(tasks), "active_only": active_only, "tasks": tasks}


# -- action tools -----------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(title="Dispatch patrol", read_only_hint=False, destructive_hint=False))
def dispatch_patrol(
    places: Annotated[
        list[str],
        Field(min_length=1, max_length=MAX_PATROL_PLACES, description="Location names to visit, in order"),
    ],
    rounds: Annotated[
        int, Field(ge=1, le=MAX_ROUNDS, description="How many times to repeat the route")
    ] = 1,
) -> dict:
    """Send a robot to visit places in order, repeating the route `rounds` times.
    To send a robot to a single location, pass one place and rounds=1.
    Open-RMF chooses the robot. Returns the task_id."""
    index = _require_index()
    names = [_resolve(p, index).name for p in places]
    _check_capacity()
    task_id = _rmf(_client.dispatch_patrol, names, rounds)
    log.info("Dispatched patrol %s x%d as %s", names, rounds, task_id)
    return {
        "task_id": task_id,
        "category": "patrol",
        "places": names,
        "rounds": rounds,
        "status": "queued",
        "next_step": "Open-RMF will assign a robot. Use get_task_status to follow progress.",
    }


@mcp.tool(annotations=ToolAnnotations(title="Dispatch delivery", read_only_hint=False, destructive_hint=False))
def dispatch_delivery(
    pickup: Annotated[str, Field(description="Pickup location (must be a pickup point)")],
    dropoff: Annotated[str, Field(description="Drop-off location (must be a drop-off point)")],
) -> dict:
    """Have a robot collect an item at a pickup point and deliver it to a drop-off point.
    Valid pickup and drop-off points are listed by list_locations. Returns the task_id."""
    index = _require_index()
    src = _resolve(pickup, index)
    dst = _resolve(dropoff, index)
    if not src.pickup_handler:
        raise ToolError(f"'{src.name}' is not a pickup point. Pickup points: {', '.join(index.pickups()) or 'none'}.")
    if not dst.dropoff_handler:
        raise ToolError(f"'{dst.name}' is not a drop-off point. Drop-off points: {', '.join(index.dropoffs()) or 'none'}.")
    _check_capacity()
    task_id = _rmf(_client.dispatch_delivery, src.name, src.pickup_handler, dst.name, dst.dropoff_handler)
    log.info("Dispatched delivery %s -> %s as %s", src.name, dst.name, task_id)
    return {
        "task_id": task_id,
        "category": "delivery",
        "pickup": src.name,
        "dropoff": dst.name,
        "status": "queued",
        "next_step": "Open-RMF will assign a robot. Use get_task_status to follow progress.",
    }


@mcp.tool(annotations=ToolAnnotations(title="Cancel task", read_only_hint=False, destructive_hint=True))
def cancel_task(
    task_id: Annotated[str, Field(description="ID of the task to cancel")],
) -> dict:
    """Cancel a queued or running task. Fails if the task has already finished.
    The robot stops the task and becomes available for other work."""
    summary = _rmf(_client.get_task_summary, task_id)
    if summary is None:
        raise ToolError(f"Unknown task ID '{task_id}'. Use list_tasks to see known tasks.")
    if summary["status"] in TERMINAL_STATUSES:
        raise ToolError(f"Task '{task_id}' already ended with status '{summary['status']}'; nothing to cancel.")
    response = _rmf(_client.cancel_task, task_id)
    log.info("Cancel requested for %s: %s", task_id, response)
    return {
        "task_id": task_id,
        "cancel_requested": bool(response.get("success")),
        "previous_status": summary["status"],
        "robot": summary["robot"],
        "next_step": "Use get_task_status to confirm the task shows 'canceled'.",
    }


def main() -> None:
    log.info("Starting rmf-fleet MCP server (RMF API: %s)", API_URL)
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
