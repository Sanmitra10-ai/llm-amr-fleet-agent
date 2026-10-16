"""Small Python client for the Open-RMF web API server (rmf-web).

The MCP server (Phase 3) talks to Open-RMF only through this module, so it
never needs ROS 2 itself. Everything here is plain HTTP + JSON.

Endpoints used (all under the API server, default http://localhost:8000):
    GET  /fleets                    fleet and robot states
    GET  /building_map              levels, nav graphs and named waypoints
    GET  /tasks                     task states, optionally filtered by status
    GET  /tasks/{task_id}/state     state of one task
    POST /tasks/dispatch_task       submit a new task
    POST /tasks/cancel_task         cancel a task
"""

from __future__ import annotations

import time
import warnings
from typing import Any, Callable

import httpx
import jwt

DEFAULT_URL = "http://localhost:8000"

# Demo-only credentials. rmf-web's default config verifies bearer tokens with
# this shared secret, audience and issuer. Never use these outside a local sim.
DEMO_JWT_SECRET = "rmfisawesome"
DEMO_JWT_AUDIENCE = "rmf_api_server"
DEMO_JWT_ISSUER = "stub"

TERMINAL_STATUSES = {"completed", "failed", "canceled", "killed", "skipped"}
ACTIVE_STATUSES = ["queued", "standby", "underway", "delayed", "blocked"]


class RMFError(RuntimeError):
    """Raised when the API server rejects a request."""


def summarize_task(state: dict) -> dict:
    """Compact view of an RMF task state."""
    assigned = state.get("assigned_to") or {}
    return {
        "task_id": (state.get("booking") or {}).get("id"),
        "category": state.get("category"),
        "status": state.get("status"),
        "robot": assigned.get("name"),
        "fleet": assigned.get("group"),
        "detail": state.get("detail"),
    }


def make_demo_token(username: str = "admin") -> str:
    """Create a bearer token accepted by rmf-web's default (demo) config."""
    claims = {
        "preferred_username": username,
        "aud": DEMO_JWT_AUDIENCE,
        "iss": DEMO_JWT_ISSUER,
    }
    with warnings.catch_warnings():
        # The demo secret is shorter than PyJWT recommends; fine for local sim.
        warnings.simplefilter("ignore")
        return jwt.encode(claims, DEMO_JWT_SECRET, algorithm="HS256")


class RMFClient:
    """Thin wrapper around the rmf-web REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        token: str | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token or make_demo_token()}",
                "Accept": "application/json",
            },
        )

    # -- plumbing -------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RMFClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise RMFError(f"Authentication rejected by API server ({resp.status_code})")
        if resp.status_code == 400:
            # rmf-web returns RMF's own error payload with a 400.
            raise RMFError(f"Request rejected by RMF: {resp.text}")
        if resp.status_code >= 500:
            # e.g. {"detail": "rmf service timed out"} when RMF never replies over ROS 2.
            raise RMFError(f"API server error {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._http.get(path, params=params)
        self._check(resp)
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        resp = self._http.post(path, json=body)
        self._check(resp)
        return resp.json()

    # -- fleets ---------------------------------------------------------------

    def get_fleets(self) -> list[dict]:
        """Raw fleet states, as published by the fleet adapters."""
        return self._get("/fleets")

    def get_robots(self) -> list[dict]:
        """Flattened, compact view of every robot in every fleet."""
        robots = []
        for fleet in self.get_fleets():
            for name, r in (fleet.get("robots") or {}).items():
                loc = r.get("location") or {}
                battery = r.get("battery")
                robots.append(
                    {
                        "fleet": fleet.get("name"),
                        "name": r.get("name", name),
                        "status": r.get("status"),
                        "task_id": r.get("task_id") or None,
                        "battery_pct": round(battery * 100) if battery is not None else None,
                        "map": loc.get("map"),
                        "x": round(loc["x"], 2) if "x" in loc else None,
                        "y": round(loc["y"], 2) if "y" in loc else None,
                    }
                )
        return robots

    # -- building map ---------------------------------------------------------

    def get_building_map(self) -> dict | None:
        """Building map with levels, nav graphs and named waypoints.

        Returns None if the API server hasn't received the map from RMF yet.
        """
        try:
            return self._get("/building_map")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    # -- tasks ----------------------------------------------------------------

    def list_tasks(self, statuses: list[str] | None = None, limit: int = 100) -> list[dict]:
        """Task states known to the API server, optionally filtered by status."""
        params: dict[str, Any] = {"limit": limit}
        if statuses:
            params["status"] = ",".join(statuses)
        return self._get("/tasks", params=params)

    def dispatch_task(
        self,
        category: str,
        description: dict,
        fleet_name: str | None = None,
        earliest_start_ms: int = 0,
        requester: str = "llm-amr-fleet-agent",
    ) -> str:
        """Submit a task and return its task ID.

        earliest_start_ms=0 means "as soon as possible". In simulation RMF runs
        on sim time, which starts at 0, so wall-clock timestamps would make the
        task wait for a very long time.
        """
        request: dict[str, Any] = {
            "category": category,
            "description": description,
            "unix_millis_earliest_start_time": earliest_start_ms,
            "requester": requester,
        }
        if fleet_name:
            request["fleet_name"] = fleet_name

        result = self._post(
            "/tasks/dispatch_task",
            {"type": "dispatch_task_request", "request": request},
        )
        if not result.get("success"):
            raise RMFError(f"Dispatch failed: {result.get('errors') or result}")
        return result["state"]["booking"]["id"]

    def dispatch_patrol(self, places: list[str], rounds: int = 1) -> str:
        return self.dispatch_task("patrol", {"places": places, "rounds": rounds})

    def dispatch_delivery(
        self,
        pickup: str,
        pickup_handler: str,
        dropoff: str,
        dropoff_handler: str,
    ) -> str:
        return self.dispatch_task(
            "delivery",
            {
                "pickup": {"place": pickup, "handler": pickup_handler, "payload": []},
                "dropoff": {"place": dropoff, "handler": dropoff_handler, "payload": []},
            },
        )

    def get_task_state(self, task_id: str) -> dict | None:
        """Full task state, or None if the server doesn't know the task yet."""
        try:
            return self._get(f"/tasks/{task_id}/state")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_task_summary(self, task_id: str) -> dict | None:
        """Compact task view: status, assigned robot, category, detail."""
        state = self.get_task_state(task_id)
        if state is None:
            return None
        summary = summarize_task(state)
        summary["task_id"] = summary["task_id"] or task_id
        return summary

    def cancel_task(self, task_id: str) -> dict:
        return self._post(
            "/tasks/cancel_task",
            {"type": "cancel_task_request", "task_id": task_id},
        )

    def wait_for_task(
        self,
        task_id: str,
        timeout_s: float = 900,
        poll_s: float = 5,
        on_update: Callable[[dict], None] | None = None,
    ) -> dict | None:
        """Poll until the task reaches a terminal status or the timeout passes.

        Calls on_update(summary) whenever the status or assigned robot changes.
        Returns the last summary seen.
        """
        deadline = time.monotonic() + timeout_s
        last_key = None
        summary = None
        while time.monotonic() < deadline:
            summary = self.get_task_summary(task_id)
            if summary is not None:
                key = (summary["status"], summary["robot"])
                if key != last_key and on_update:
                    on_update(summary)
                last_key = key
                if summary["status"] in TERMINAL_STATUSES:
                    return summary
            time.sleep(poll_s)
        return summary
