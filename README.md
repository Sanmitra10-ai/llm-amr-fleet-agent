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
- [ ] **Phase 2: RMF API bridge.** rmf-web API server and a Python client for fleet state and task dispatch.
- [ ] **Phase 3: MCP server.** Fleet tools exposed over MCP.
- [ ] **Phase 4: LLM agent.** Observe, act and verify loop, with tool-call logging.
- [ ] **Phase 5: Factory scenario.** Custom shop-floor map and an RCGA material-flow planner as an MCP tool.
- [ ] **Phase 6: Evaluation and demo.** Test set, metrics and demo video.

## Tech stack

Ubuntu 24.04 · ROS 2 Jazzy · Gazebo Harmonic · Open-RMF · Python · MCP

## Setup

See [docs/setup.md](docs/setup.md) for the full environment setup and the Phase 1 verification steps.

## Repository structure (planned)

```
mcp_server/   MCP server and RMF API client
agent/        LLM agent loop
eval/         test commands and results
docs/         setup notes and progress log
```
