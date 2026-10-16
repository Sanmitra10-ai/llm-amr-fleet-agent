# LLM AMR Fleet Agent

An LLM agent that commands a fleet of autonomous mobile robots (AMRs) in simulation through the **Model Context Protocol (MCP)**, built on **Open-RMF**, **ROS 2 Jazzy** and **Gazebo Harmonic**.

> 🚧 Work in progress. Target completion: 1 November 2026.

## How it works

```
You (plain-English command)
        │
        ▼
LLM agent ─────────── understands the request, decides what to do
        │
        ▼
MCP server ◄────────► RCGA optimizer (best robot-task plan)
        │   fleet tools: state, locations, dispatch, status, cancel
        ▼
rmf-web API server ── REST bridge to Open-RMF
        │
        ▼
Open-RMF ──────────── task allocation, traffic deconfliction, charging
        │
        ▼
Gazebo Harmonic ───── simulated AMR fleet
```

The LLM decides **what** should happen. Open-RMF handles **how**: path planning, keeping robots out of each other's way, and charging.

## Roadmap

- [x] **Phase 1: Simulation environment.** Open-RMF office demo running; patrol and delivery tasks dispatched and completed by two robots in parallel.
- [x] **Phase 2: RMF API bridge.** rmf-web API server in Docker and a Python client (`mcp_server/rmf_client.py`) that reads fleet state and dispatches, tracks and cancels tasks over HTTP.
- [x] **Phase 3: MCP server.** 7 fleet tools over MCP (`mcp_server/server.py`), with location names read from the building map and guardrails enforced in the server.
- [ ] **Phase 4: LLM agent.** Observe, act and verify loop (`agent/`), works with Claude or any OpenAI-compatible LLM, operator approval for risky actions, JSONL logs.
- [ ] **Phase 5: Factory scenario.** Custom shop-floor map and an RCGA material-flow planner as an MCP tool.
- [ ] **Phase 6: Evaluation and demo.** Test set, metrics and demo video.

## Tech stack

Ubuntu 24.04 · ROS 2 Jazzy · Gazebo Harmonic · Open-RMF · rmf-web API server (Docker) · Python · MCP SDK v2 · Claude / Gemini (OpenAI-compatible) APIs

## MCP tools

| Tool | Purpose |
|---|---|
| `get_fleet_state` | Robot status, battery, position and current task |
| `list_locations` | Valid place names from the building map, plus pickup points, drop-off points and chargers |
| `dispatch_patrol` | Visit places in order (one place = "go to") |
| `dispatch_delivery` | Pick up at a pickup point, deliver to a drop-off point |
| `get_task_status` | Status and assigned robot for one task |
| `list_tasks` | Active (or all) tasks |
| `cancel_task` | Cancel a queued or running task |

The agent adds a local `wait_for_tasks` tool that polls task status, so verifying an outcome costs one LLM call instead of one per status check.

**Guardrails** are enforced in the server, so the LLM can't bypass them: unknown places are rejected with suggestions, patrols are capped at 6 places and 5 rounds, at most 6 active tasks at once, deliveries must start at a pickup point, and finished tasks can't be cancelled. The agent also asks the operator before cancelling a task or making more than 2 dispatches for one request.

## Quick start

Requires the environment in [docs/setup.md](docs/setup.md). Start each in its own terminal, in this order:

```bash
# 1. API server
docker run --network host --ipc=host -it --rm \
  -e ROS_DOMAIN_ID=0 -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  -e FASTDDS_BUILTIN_TRANSPORTS=UDPv4 \
  ghcr.io/open-rmf/rmf-web/api-server:jazzy-nightly

# 2. Simulation, reporting to the API server
ros2 launch rmf_demos_gz office.launch.xml headless:=1 server_uri:="ws://localhost:8000/_internal"

# 3. Agent (needs ANTHROPIC_API_KEY or GEMINI_API_KEY)
source .venv/bin/activate
python -m agent.cli
```

Example session (illustrative; robot assignment and timing vary):

```
you> Send a robot to the pantry, then patrol coe and lounge twice
  → get_fleet_state()
  → list_locations()
  → dispatch_patrol(places=["pantry"], rounds=1)
  → dispatch_patrol(places=["coe", "lounge"], rounds=2)
  → wait_for_tasks(task_ids=[...])
agent> Done. tinyRobot1 went to the pantry and tinyRobot2 completed 2 rounds of coe ↔ lounge.
```

## Repository structure

```
mcp_server/
  server.py         MCP server: 7 fleet tools with guardrails
  locations.py      named waypoints parsed from the RMF building map
  rmf_client.py     HTTP client for the rmf-web API
agent/
  cli.py            terminal chat with the agent
  agent.py          observe → act → verify loop, approvals, wait_for_tasks
  llm.py            Claude and OpenAI-compatible (Gemini, OpenAI, Groq, Ollama) backends
  prompts.py        system prompt
  log.py            JSONL session logs (logs/)
scripts/
  phase2_check.py   end-to-end check of the API bridge
  phase3_check.py   end-to-end check of every MCP tool over stdio
docs/
  setup.md          environment setup, verification steps and known issues
eval/               test commands and results (Phase 6)
```
