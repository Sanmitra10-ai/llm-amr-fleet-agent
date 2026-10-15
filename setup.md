# Environment setup and Phase 1 verification

## Environment

| Component | Version |
|---|---|
| OS | Ubuntu 24.04 LTS (VirtualBox VM) |
| ROS 2 | Jazzy Jalisco |
| Simulator | Gazebo Harmonic (`ros-jazzy-ros-gz`) |
| Fleet management | Open-RMF binaries (`ros-jazzy-rmf-dev`) |
| Demos | `rmf_demos` 2.3.0, built from source (matches the Jazzy binary release) |
| API server | `ghcr.io/open-rmf/rmf-web/api-server:jazzy-nightly` (Docker) |
| Python | 3.12 in a venv, with `httpx` and `PyJWT` |

## 1. Install ROS 2 Jazzy

```bash
# Locale
sudo apt update && sudo apt install locales -y
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export LANG=en_US.UTF-8

# ROS 2 apt source
sudo apt install software-properties-common -y
sudo add-apt-repository universe
sudo apt update && sudo apt install curl -y
export ROS_APT_SOURCE_VERSION=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F "tag_name" | awk -F'"' '{print $4}')
curl -L -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo ${UBUNTU_CODENAME:-${VERSION_CODENAME}})_all.deb"
sudo dpkg -i /tmp/ros2-apt-source.deb

# ROS 2 Jazzy
sudo apt update && sudo apt upgrade -y
sudo apt install ros-jazzy-desktop ros-dev-tools -y
echo "source /opt/ros/jazzy/setup.bash" >> ~/.bashrc
```

## 2. Install Gazebo Harmonic

```bash
sudo apt install ros-jazzy-ros-gz -y
```

## 3. Install Open-RMF

```bash
sudo rosdep init
rosdep update
colcon mixin add default https://raw.githubusercontent.com/colcon/colcon-mixin-repository/master/index.yaml
colcon mixin update default
sudo apt install ros-jazzy-rmf-dev -y
```

## 4. Build rmf_demos

The Open-RMF binaries do not include `rmf_demos`, so it is built from source.

```bash
mkdir -p ~/rmf_ws/src && cd ~/rmf_ws/src
git clone https://github.com/open-rmf/rmf_demos.git -b 2.3.0
cd ~/rmf_ws
rosdep install --from-paths src --ignore-src -y
colcon build
echo "source ~/rmf_ws/install/setup.bash" >> ~/.bashrc
```

## 5. Run the office demo (headless)

Inside a VM, the Gazebo GUI is too heavy, so the demo runs headless. `headless:=1` turns off **both** the Gazebo GUI and RViz; the simulation still runs in the background.

```bash
# Terminal 1: keep running
ros2 launch rmf_demos_gz office.launch.xml headless:=1
```

Optional RViz view (slow in a VM):

```bash
rviz2 -d $(ros2 pkg prefix rmf_visualization_schedule)/share/rmf_visualization_schedule/config/rmf.rviz
```

## 6. Phase 1 verification

```bash
# Simulation clock is advancing
ros2 topic echo /clock --once

# Fleet state: robot names, positions, battery
ros2 topic echo /fleet_states --once

# Dispatch a patrol
ros2 run rmf_demos_tasks dispatch_patrol -p coe lounge -n 2 --use_sim_time

# Dispatch a delivery
ros2 run rmf_demos_tasks dispatch_delivery -p pantry -ph coke_dispenser -d hardware_2 -dh coke_ingestor --use_sim_time

# Watch robots (refreshes every 10 s)
watch -n 10 "ros2 topic echo /fleet_states --once | grep -E 'name: tinyRobot|task_id|    x:|    y:'"

# Check task events in the launch log
grep -iE "<task_id>" ~/.ros/log/latest/launch.log | tail -20
```

## Phase 1 results

- Fleet `tinyRobot` with 2 robots (`tinyRobot1`, `tinyRobot2`) on level `L1`, battery 99%.
- **Patrol** (`coe` ↔ `lounge`, 2 rounds) was bid on and awarded to `tinyRobot1`.
- **Delivery** (`pantry` → `hardware_2`) was bid on and awarded to `tinyRobot2` while the patrol was still running, so both tasks ran in parallel.
- Both robots moved several metres, both `task_id` fields cleared on completion, and no errors were logged.

### Dispatch payloads (reference for the Phase 3 MCP tools)

Patrol:

```json
{
  "type": "dispatch_task_request",
  "request": {
    "unix_millis_earliest_start_time": 0,
    "category": "patrol",
    "description": { "places": ["coe", "lounge"], "rounds": 2 }
  }
}
```

Delivery:

```json
{
  "type": "dispatch_task_request",
  "request": {
    "unix_millis_earliest_start_time": 0,
    "category": "delivery",
    "description": {
      "pickup":  { "place": "pantry",     "handler": "coke_dispenser", "payload": [] },
      "dropoff": { "place": "hardware_2", "handler": "coke_ingestor",  "payload": [] }
    }
  }
}
```

## 7. Phase 2: RMF API bridge

### Install Docker

```bash
sudo apt install docker.io -y
sudo usermod -aG docker $USER
# Log out and back in (or reboot) so every terminal gets the docker group
docker run hello-world
```

### Use UDP transport for ROS 2

The API server runs in a container as root. Fast DDS's default shared-memory transport doesn't carry messages between it and the host user, so both sides use UDP only.

```bash
echo "export FASTDDS_BUILTIN_TRANSPORTS=UDPv4" >> ~/.bashrc
source ~/.bashrc
ros2 daemon stop
```

### Start everything (in this order)

```bash
# Terminal A: API server (http://localhost:8000, docs at /docs)
docker run --network host --ipc=host -it --rm \
  -e ROS_DOMAIN_ID=0 \
  -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  -e FASTDDS_BUILTIN_TRANSPORTS=UDPv4 \
  ghcr.io/open-rmf/rmf-web/api-server:jazzy-nightly

# Terminal B: simulation, sending robot and task updates to the API server
ros2 launch rmf_demos_gz office.launch.xml headless:=1 server_uri:="ws://localhost:8000/_internal"

# Optional: web dashboard at http://localhost:3000
docker run --network host -it --rm ghcr.io/open-rmf/rmf-web/demo-dashboard:jazzy-nightly
```

How the pieces talk:

- **Dispatch and cancel:** Python client → HTTP → API server → ROS 2 (`task_api_requests` / `task_api_responses`) → Open-RMF.
- **Robot and task state:** fleet adapter → websocket (`server_uri`) → API server → HTTP → Python client.

### Python client

```bash
sudo apt install python3.12-venv -y
cd ~/llm-amr-fleet-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python scripts/phase2_check.py
python scripts/phase2_check.py --cancel-test
```

The client authenticates with a token signed by rmf-web's built-in demo secret. This only works with the API server's default config and is for local simulation only.

### Phase 2 results

| Check | Result |
|---|---|
| Read fleet state over HTTP | 2 robots with status, battery and position |
| Dispatch patrol (`coe` ↔ `lounge`, 1 round) | Assigned to `tinyRobot1` in ~5 s; `underway` → `completed` in ~2 min |
| Task queued behind another task | Stayed `queued` until the earlier patrol finished, then completed |
| Cancel a running 3-round patrol | `cancel_task` returned `success: True`; task ended `canceled` |

## Known issues and notes

| Symptom | Cause and fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | User not in the `docker` group yet. Run `sudo usermod -aG docker $USER`, then log out and back in. |
| `ensurepip is not available` when creating the venv | Install `python3.12-venv` with `sudo`, delete the broken `.venv`, recreate it. |
| Robot table works but dispatch returns `500` ("rmf service timed out") | ROS 2 messages aren't crossing between the container and the host. Use `FASTDDS_BUILTIN_TRANSPORTS=UDPv4` on both sides (see above). |
| `/fleets` returns an empty list | The sim wasn't launched with `server_uri`, or was started before the API server. |
| Task stays `queued` with a robot assigned | RMF queued it behind another task on that robot. It starts when the earlier task finishes. |
| Robot still shows `working` right after a task completes or is cancelled | Robot state lags task state by a few seconds while the robot finishes its move or parks. |
| `package 'rmf_demos_gz' not found` | The workspace isn't sourced in that terminal. Run `source ~/rmf_ws/install/setup.bash`. |
| Gazebo GUI "not responding" | Weak 3D graphics in VirtualBox. Run with `headless:=1`. |
| No RViz window in headless mode | Expected: `headless:=1` disables RViz. Launch `rviz2` separately if needed. |
| `A message was lost!!!` when echoing `/clock` | Harmless; the terminal can't keep up with the high-rate clock topic. |
| `mode` stays `0` and `path` stays empty while a robot moves | In this demo these fields don't update during motion. Use `location` x/y and `task_id` instead. |
| `Aborted (core dumped)` when closing RViz with Ctrl+C | Harmless crash on shutdown. |
