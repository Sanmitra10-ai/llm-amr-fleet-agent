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
- [ ] **Phase 3: MCP server.** Fleet tools exposed over MCP.
- [ ] **Phase 4: LLM agent.** Observe, act and verify loop, with tool-call logging.
- [ ] **Phase 5: Factory scenario.** Custom shop-floor map and an RCGA material-flow planner as an MCP tool.
- [ ] **Phase 6: Evaluation and demo.** Test set, metrics and demo video.

## Tech stack

Ubuntu 24.04 · ROS 2 Jazzy · Gazebo Harmonic · Open-RMF · rmf-web API server (Docker) · Python (httpx) · MCP

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

# 3. Python client check
source .venv/bin/activate
python scripts/phase2_check.py --cancel-test
```

## Repository structure

```
mcp_server/
  rmf_client.py     HTTP client for the rmf-web API (fleet state, dispatch, track, cancel)
scripts/
  phase2_check.py   end-to-end check of the API bridge
docs/
  setup.md          environment setup, verification steps and known issues
agent/              LLM agent loop (Phase 4)
eval/               test commands and results (Phase 6)
```
