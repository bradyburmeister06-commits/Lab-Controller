# Machine Research Backend

Python FastAPI backend for a machine interval research project. It stores machine activation events and Arduino temperature/RH readings in SQLite, exposes dashboard-ready API endpoints for your web app, and includes a safe mock machine controller by default.

## What this backend does

- Tracks when the machine last turned on and when it will turn on next.
- Turns the machine on at a configured interval using a swappable controller.
- Reads temperature and relative humidity from two Arduino Uno R3 boards.
- Stores all machine and sensor data in SQLite for Grafana or Python visualization.
- Exposes REST API endpoints for a Tailscale-hosted or public read-only web app.
- Serves a public read-only dashboard at `/` and `/public`.
- Serves a protected sysadmin dashboard at `/admin` for controls, logs, raw data, and configuration.
- Includes simulator mode so you can develop before connecting Arduinos or actuator hardware.

## Project structure

```text
app/
  api/routes.py                 FastAPI REST endpoints
  config.py                     Environment-based settings
  db/models.py                  SQLite table definitions
  db/init_db.py                 Database initialization
  services/machine_controller.py Safe mock/WOL/command actuator abstraction
  services/machine_service.py   Machine activation/status logic
  services/scheduler.py         Interval scheduler
  services/sensor_service.py    Arduino serial ingestion and parser
  static/public.html            Public read-only status dashboard
  static/index.html             Protected sysadmin dashboard web app
scripts/
  init_db.py                    Create SQLite tables and default machine
  insert_sample_readings.py     Seed chart-ready sample sensor readings
tests/
  test_sensor_parsing.py
  test_machine_service.py
```

## Quick start

### Docker quick start

Use Docker if you want to avoid installing Python packages on the host:

```bash
docker compose up -d --build
```

This default `docker-compose.yml` runs the backend in **all-in-one** mode
(dashboards + simulated sensors + mock relays) on a single machine and is
unchanged. If you instead want to run the website on one computer and the
hardware automation on another, see
[Two-machine Docker deployment](#two-machine-docker-deployment) below — it
uses `docker-compose.hub.yml` and `docker-compose.collector.yml`.

Open:

- Public read-only dashboard: `http://localhost:8000/` or `http://localhost:8000/public`
- Protected sysadmin dashboard: `http://localhost:8000/admin`
- API docs: `http://localhost:8000/docs`
- Public dashboard JSON: `http://localhost:8000/api/public/dashboard`

The default Docker setup uses simulator mode and stores SQLite data in a persistent Docker volume named `machine_research_data`.

The default sysadmin login is:

```text
Username: admin
Password: change-me-now
```

Change `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `docker-compose.yml` before exposing the machine to anyone else.

### Windows Docker setup

You can run this on Windows without changing operating systems:

1. Install Docker Desktop for Windows.
2. Enable the WSL 2 backend during Docker Desktop setup.
3. Open PowerShell in the project folder.
4. Run:

```powershell
docker compose up -d --build
```

Then open:

```text
http://localhost:8000/public
http://localhost:8000/admin
```

If you are using real Arduino Uno R3 boards on Windows, Docker Desktop needs the devices to appear inside WSL before the Linux container can read them. The common approach is `usbipd-win`:

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
usbipd bind --busid <BUSID>
usbipd attach --busid <BUSID> --wsl
```

After attaching both Arduinos, verify their Linux names from a WSL shell:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

Then update `docker-compose.yml` so `ARDUINO_1_PORT`, `ARDUINO_2_PORT`, and the `devices:` mappings match those names.

To stop the container:

```bash
docker compose down
```

To stop the container and delete all stored SQLite data:

```bash
docker compose down -v
```

### Windows Collector Quick Start (one-command native start)

This is the path you want when the lab Windows computer drives a real
**MCC USB-1208FS-Plus** board and three SSRs. Docker on Windows cannot use
the `mcculw` driver (it is Windows-only and the Linux container cannot see
the USB device), so the collector must run **natively on Windows** —
**not** in Docker, **not** in WSL.

The four scripts in the repo root drive the entire lifecycle:

| Script                          | What it does |
|---------------------------------|--------------|
| `setup-collector-windows.bat`   | First-time setup: creates `.venv`, installs `requirements.txt` + `requirements-windows.txt`, copies `.env.collector.example` to `.env`. |
| `start-collector.bat`           | Preflight checks (`.env`, `.venv`, `APP_MODE=collector`, hub URL placeholder, token placeholder, `mcculw` import), then runs `uvicorn app.main:app --host 0.0.0.0 --port 8001` and writes a PID file + `logs\collector.log`. |
| `stop-collector.bat`            | Stops the collector using the PID file, falls back to whatever process is listening on TCP 8001. |
| `status-collector.bat`          | Reports running/not-running, prints `COLLECTOR_ID` / `HUB_BASE_URL` from `.env`, hits `/api/health` locally and on the hub. |

> **Hardware topology assumed by the defaults:** ONE Windows collector +
> ONE MCC USB-1208FS-Plus + THREE SSR-controlled machines. Each machine
> maps to one DIO bit on the same MCC board:
>
> | Hub admin label | Local relay id | MCC pin             |
> |-----------------|----------------|---------------------|
> | machine 1       | `relay-1`      | Port B bit 0 (`B0`) |
> | machine 2       | `relay-2`      | Port B bit 1 (`B1`) |
> | machine 3       | `relay-3`      | Port B bit 2 (`B2`) |
>
> Per-machine ON/OFF intervals are still edited from the hub `/admin` UI
> and apply to those three relays on this collector. The hub still
> supports N collectors if you ever add more hardware nodes — see
> [Multi-collector deployment (one Mac hub + three Windows collectors)](#multi-collector-deployment-one-mac-hub--three-windows-collectors).

#### First-time setup on the lab Windows computer

1. Install **Python 3.11, 3.12, or 3.13 64-bit** from
   <https://www.python.org/downloads/windows/>. Tick "Add python.exe to
   PATH". (The dependency stack — Pydantic 2.13, FastAPI 0.136 — is
   tested on 3.10–3.13. Python 3.14 should also work but is not yet
   validated against `mcculw`.)
2. Install **MCC Universal Library / InstaCal** from
   <https://www.mccdaq.com/Software-Downloads> and configure the
   USB-1208FS-Plus as **board 0**.
3. Install **Tailscale** and join the same tailnet as the hub.
4. Open **Command Prompt** and clone the repo:

   ```bat
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   setup-collector-windows.bat
   ```

5. Open the freshly created `.env` in Notepad and edit at minimum:

   ```dotenv
   APP_MODE=collector
   HUB_BASE_URL=http://100.x.y.z:8000          REM hub's Tailscale IP or URL
   COLLECTOR_API_TOKEN=<long random secret>     REM must match the hub
   COLLECTOR_ID=lab-mcc-controller
   COLLECTOR_NAME=Lab MCC Controller
   RELAY_CONTROLLER=mcc_usb1208fs_plus
   MCC_BOARD_NUM=0
   MCC_DIGITAL_PORT=FIRSTPORTA
   RELAY_1_BIT=0
   RELAY_2_BIT=1
   RELAY_3_BIT=2
   RELAY_ACTIVE_HIGH=true
   MACHINE_CONTROLLER=mock
   SENSOR_SIMULATOR=false
   ARDUINO_1_PORT=COM3
   ARDUINO_2_PORT=COM4
   ```

6. Start the collector:

   ```bat
   start-collector.bat
   ```

   The script aborts with a clear error if any preflight check fails
   (placeholder hub URL, default token, `APP_MODE` not `collector`,
   `mcculw` not importable, …).

7. Confirm it registered with the hub: open the hub's `/admin` page and
   look under **Collector status** for `lab-mcc-controller`.

#### Daily startup

```bat
cd C:\path\to\Lab-Controller
start-collector.bat
```

That single command runs preflight, starts uvicorn, starts the
`CollectorAgent` background loop (which handles **data collection** from
Arduino sensors, the **relay timing/schedule loops** for relay-1/2/3,
and **hub sync / heartbeat / register / poll**), and writes
`logs\collector.log`. There is nothing else to launch.

#### Status / restart / stop

```bat
status-collector.bat       REM Is it running? Is the hub reachable?
stop-collector.bat         REM Clean stop using the PID file.
start-collector.bat        REM Restart = stop, then start.
```

To restart in one go:

```bat
stop-collector.bat && start-collector.bat
```

### Hub + collector split deployment (important)

- **Hub (Mac/Docker)** hosts dashboard/API and queues commands.
- **Collector (Windows/native Python)** physically controls MCC relay outputs.
- For remote relay control via hub, requests must target the collector machine key:

```http
POST /api/relays/relay-1/on?machine_key=lab-mcc-controller
POST /api/relays/relay-1/off?machine_key=lab-mcc-controller
```

The admin dashboard now includes the selected collector automatically in relay and schedule calls.

#### Make the collector start automatically on boot/login (Task Scheduler)

You want the collector to come back up after a power cut without anyone
logging in.

1. Open **Task Scheduler** (`taskschd.msc`).
2. **Action -> Create Task...** (NOT "Create Basic Task" — you need the
   advanced options).
3. **General** tab:
   - Name: `Lab Controller collector`
   - Select **Run whether user is logged on or not**.
   - Tick **Run with highest privileges**.
   - Configure for: Windows 10 / Windows 11.
4. **Triggers** tab -> **New...**:
   - Begin the task: **At startup**.
   - (Optional second trigger: **At log on** of the lab user, if you also
     want it to relaunch after an interactive logon.)
   - Tick **Enabled**.
5. **Actions** tab -> **New...**:
   - Action: **Start a program**.
   - Program/script: `C:\path\to\Lab-Controller\start-collector.bat`
   - **Start in (optional):** `C:\path\to\Lab-Controller`
     (this is required — `start-collector.bat` resolves `.env`/`.venv`
     relative to its working directory).
6. **Conditions** tab: untick **Start the task only if the computer is
   on AC power** (so it survives on a UPS).
7. **Settings** tab:
   - Tick **Allow task to be run on demand**.
   - **If the task fails, restart every:** 1 minute, up to 3 times.
   - **Stop the task if it runs longer than:** uncheck (this is a
     long-running service).
8. Click **OK**, enter the Windows password for the account when prompted.

To verify, right-click the task -> **Run**, then run
`status-collector.bat` from any Command Prompt — it should report
RUNNING with the configured `COLLECTOR_ID` and `HUB_BASE_URL`.

> **Why not Docker on the lab machine?** The MCC `mcculw` driver is a
> Windows-only DLL. Docker Desktop on Windows runs Linux containers in
> WSL and cannot reach `mcculw`. USB passthrough into a Linux container
> on Windows is also not reliably supported. Run the **hub** in Docker
> on the home server if you like, but run the **collector** natively on
> Windows for real MCC + SSR control.
>
> **Tailscale must already be up.** `start-collector.bat` will succeed
> on preflight as long as `HUB_BASE_URL` is non-placeholder, but the
> `CollectorAgent` won't be able to reach the hub if the tailnet isn't
> connected. `status-collector.bat` shows hub reachability so you can
> tell which side is broken.

### Docker with real Arduinos

Edit `docker-compose.yml`:

```yaml
environment:
  SENSOR_SIMULATOR: "false"
  ARDUINO_1_PORT: /dev/ttyACM0
  ARDUINO_2_PORT: /dev/ttyACM1
devices:
  - "/dev/ttyACM0:/dev/ttyACM0"
  - "/dev/ttyACM1:/dev/ttyACM1"
```

Then restart:

```bash
docker compose up -d --build
```

If your Arduinos appear as `/dev/ttyUSB0` and `/dev/ttyUSB1`, change both the environment variables and `devices` mappings.

### Docker environment settings

Important settings in `docker-compose.yml`:

```yaml
ADMIN_USERNAME: admin
ADMIN_PASSWORD: change-me-now
SENSOR_SIMULATOR: "true"
MACHINE_CONTROLLER: mock
DATABASE_URL: sqlite:////data/machine_research.db
```

- `ADMIN_USERNAME` and `ADMIN_PASSWORD`: required for `/admin` and protected admin API endpoints.
- `SENSOR_SIMULATOR`: set to `"false"` when the Arduinos are connected and passed into Docker.
- `MACHINE_CONTROLLER`: leave as `mock` until you have tested the schedule and sensor pipeline.
- `DATABASE_URL`: points SQLite to the persistent Docker volume.

### Build and upload the Docker image

Build a local image:

```bash
docker build -t machine-research-backend:latest .
```

Save it as a file that you can upload to another machine:

```bash
docker save machine-research-backend:latest | gzip > machine-research-backend.tar.gz
```

On the target machine:

```bash
gunzip -c machine-research-backend.tar.gz | docker load
docker compose up -d
```

### Python quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/init_db.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

- Public read-only dashboard: `http://localhost:8000/` or `http://localhost:8000/public`
- Protected sysadmin dashboard: `http://localhost:8000/admin`
- API docs: `http://localhost:8000/docs`
- Public dashboard JSON: `http://localhost:8000/api/public/dashboard`
- Public sensor history for charts: `http://localhost:8000/api/public/sensors/readings?hours=24`

## Arduino data format

Each Arduino should print one reading per line over serial. Either format is supported:

```text
temp=72.4,rh=48.1
```

or:

```json
{"temp":72.4,"rh":48.1}
```

Example Arduino loop:

```cpp
void loop() {
  float temp = 72.4; // replace with your sensor library reading
  float rh = 48.1;
  Serial.print("temp=");
  Serial.print(temp);
  Serial.print(",rh=");
  Serial.println(rh);
  delay(10000);
}
```

Set the serial ports in `.env`:

```text
SENSOR_SIMULATOR=false
ARDUINO_1_PORT=/dev/ttyACM0
ARDUINO_1_NAME=arduino-1
ARDUINO_2_PORT=/dev/ttyACM1
ARDUINO_2_NAME=arduino-2
ARDUINO_BAUDRATE=9600
```

On Linux, find ports with:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

You may need to add your user to the `dialout` group:

```bash
sudo usermod -aG dialout $USER
```

Then log out and back in.

## Web app endpoints

The container includes two built-in dashboards:

- Public dashboard: `/` and `/public`
- Sysadmin dashboard: `/admin`

Use the public endpoints for pages that other people can view. Use the admin endpoints only from the protected sysadmin dashboard or from scripts that provide Basic Auth credentials.

### Public dashboard

`GET /api/public/dashboard`

Returns last machine activation, next activation, countdown, and latest room sensor summary. This is safe for the public read-only page because it does not expose controls.

### Public sensor history

`GET /api/public/sensors/readings?hours=24&limit=1000`

Use this for public temperature/RH line charts. Add `sensor_name=arduino-1` to filter one Arduino.

### Admin dashboard

`GET /api/dashboard`

Requires Basic Auth. Returns the same dashboard summary used by `/admin`.

```json
{
  "machine": {
    "id": "machine-1",
    "name": "Research Machine",
    "enabled": true,
    "interval_seconds": 3600,
    "activation_duration_seconds": 5,
    "next_run_at": "2026-05-05T18:00:00Z"
  },
  "last_activation": null,
  "next_run_at": "2026-05-05T18:00:00Z",
  "seconds_until_next_run": 3120,
  "room": {
    "latest_by_sensor": [
      {
        "sensor_name": "arduino-1",
        "temperature": 72.4,
        "relative_humidity": 48.1,
        "recorded_at": "2026-05-05T17:08:00Z"
      }
    ],
    "average_temperature": 72.4,
    "average_relative_humidity": 48.1,
    "sensor_count": 1
  }
}
```

### Admin latest sensor data

`GET /api/sensors/latest`

Requires Basic Auth. Use this for current temp/RH cards in a protected admin web app.

### Admin sensor history

`GET /api/sensors/readings?hours=24&limit=1000`

Requires Basic Auth. Use this for protected line charts. Add `sensor_name=arduino-1` to filter one Arduino.

### All activations

`GET /api/activations?limit=1000`

Requires Basic Auth. Use this for the full activation-history table in the sysadmin dashboard.

### All machines

`GET /api/machines`

Requires Basic Auth. Use this to show schedule settings and next run time for every configured machine.

### Logs

`GET /api/logs?limit=1000`

Requires Basic Auth. Use this to show backend/system messages.

### Data summary

`GET /api/data/summary`

Requires Basic Auth. Returns row counts for stored tables so the sysadmin dashboard can show how much data is available.

### Manual machine trigger

`POST /api/machines/machine-1/trigger`

Requires Basic Auth. This records an activation and calls the configured machine controller.

### Update interval

`PATCH /api/machines/machine-1`

```json
{
  "interval_seconds": 1800,
  "activation_duration_seconds": 5,
  "enabled": true
}
```

## Example frontend fetch

```js
async function loadPublicDashboard() {
  const response = await fetch("http://YOUR_HOST:8000/api/public/dashboard");
  if (!response.ok) throw new Error("Backend request failed");
  return response.json();
}

async function loadPublicSensorHistory() {
  const response = await fetch("http://YOUR_HOST:8000/api/public/sensors/readings?hours=6");
  return response.json();
}
```

If your own separate web app runs on another hostname or port, add it to `CORS_ORIGINS` in `.env` or `docker-compose.yml`.

## Machine controller options

The default is safe:

```text
MACHINE_CONTROLLER=mock
```

It records the activation but does not touch hardware.

Wake-on-LAN:

```text
MACHINE_CONTROLLER=wol
WOL_MAC_ADDRESS=AA:BB:CC:DD:EE:FF
```

Shell command:

```text
MACHINE_CONTROLLER=command
COMMAND_ON=/usr/local/bin/turn-machine-on.sh
```

Use `command` for GPIO relay scripts, smart plug CLIs, lab power controllers, or vendor-specific control commands. Keep the command script small and test it manually before enabling scheduler control.

## CLI command cheat sheet

Use this section when you want to start data collection, check that readings are coming in, trigger a machine event, or export the stored SQLite data.

### Docker service commands

Start or rebuild the backend:

```bash
docker compose up -d --build
```

Stop the backend:

```bash
docker compose down
```

Restart after changing `docker-compose.yml`:

```bash
docker compose up -d --build
```

Watch backend logs:

```bash
docker compose logs -f machine-research-backend
```

Check whether the container is running:

```bash
docker compose ps
```

Open a shell inside the container:

```bash
docker compose exec machine-research-backend sh
```

### Data collection commands

The backend continuously collects Arduino data while it is running. To collect from real Arduinos, set this in `docker-compose.yml`:

```yaml
SENSOR_SIMULATOR: "false"
```

Then make sure the Arduino device mappings are enabled:

```yaml
devices:
  - "/dev/ttyACM0:/dev/ttyACM0"
  - "/dev/ttyACM1:/dev/ttyACM1"
```

Restart collection:

```bash
docker compose up -d --build
```

Seed fake sample readings for dashboard testing:

```bash
docker compose exec machine-research-backend python scripts/insert_sample_readings.py
```

Initialize or repair the database schema:

```bash
docker compose exec machine-research-backend python scripts/init_db.py
```

### Public read-only API commands

Health check:

```bash
curl http://localhost:8000/api/health
```

Public dashboard status:

```bash
curl http://localhost:8000/api/public/dashboard
```

Public sensor history for the last 24 hours:

```bash
curl "http://localhost:8000/api/public/sensors/readings?hours=24&limit=1000"
```

Public sensor history for one Arduino:

```bash
curl "http://localhost:8000/api/public/sensors/readings?sensor_name=arduino-1&hours=6&limit=500"
```

### Sysadmin API commands

Set credentials in your shell first.

Linux/macOS/Git Bash:

```bash
export ADMIN_USER=admin
export ADMIN_PASS=change-me-now
```

PowerShell:

```powershell
$env:ADMIN_USER = "admin"
$env:ADMIN_PASS = "change-me-now"
```

Protected dashboard JSON:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" http://localhost:8000/api/dashboard
```

List machines:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" http://localhost:8000/api/machines
```

Manually trigger the machine:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" -X POST http://localhost:8000/api/machines/machine-1/trigger
```

Change the machine interval to 30 minutes:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" \
  -X PATCH http://localhost:8000/api/machines/machine-1 \
  -H "Content-Type: application/json" \
  -d '{"interval_seconds":1800,"enabled":true}'
```

Disable the schedule:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" \
  -X PATCH http://localhost:8000/api/machines/machine-1 \
  -H "Content-Type: application/json" \
  -d '{"enabled":false}'
```

Re-enable the schedule:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" \
  -X PATCH http://localhost:8000/api/machines/machine-1 \
  -H "Content-Type: application/json" \
  -d '{"enabled":true}'
```

View activation history:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" "http://localhost:8000/api/activations?limit=100"
```

View system logs:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" "http://localhost:8000/api/logs?limit=100"
```

View database row counts:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" http://localhost:8000/api/data/summary
```

### Per-relay (SSR) independent schedules

Each of the three SSRs/relays (`relay-1`, `relay-2`, `relay-3`) has its own
independent ON/OFF cycle scheduler. While a schedule is enabled, the relay
turns ON for `on_duration_seconds`, then OFF for `off_duration_seconds`,
repeating on its own clock. Schedules are managed only from the protected
sysadmin UI at `/admin` and the authenticated `/api/relays/{relay_id}/schedule`
endpoints — the public `/public` dashboard can display the current state and
next-flip time but cannot edit them.

**Safety note:** Always test in mock mode first
(`RELAY_CONTROLLER=mock`) and confirm the SSR wiring matches the configured
`RELAY_*_BIT` mapping before flipping `RELAY_CONTROLLER=mcc_usb1208fs_plus`.
Disabling a schedule forces the relay to OFF, which is the fail-safe state
for most heater / pump / lamp loads driven by an SSR.

List all schedules:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" http://localhost:8000/api/relay-schedules
```

Get a single relay's schedule:

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" http://localhost:8000/api/relays/relay-1/schedule
```

Enable relay-1 to cycle 30s ON, 90s OFF (the change is applied immediately —
no app restart needed):

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" \
  -X PATCH http://localhost:8000/api/relays/relay-1/schedule \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"on_duration_seconds":30,"off_duration_seconds":90}'
```

Disable a relay's schedule (the relay is forced OFF as a safe state):

```bash
curl -u "$ADMIN_USER:$ADMIN_PASS" \
  -X PATCH http://localhost:8000/api/relays/relay-2/schedule \
  -H "Content-Type: application/json" \
  -d '{"enabled":false}'
```

Each schedule transition is recorded as a `RelayEvent` with
`trigger_source="schedule"` and is visible in `/api/relays/{relay_id}/events`
and in the admin dashboard's "Relay history" tab.

### Watch data live from the command line

Linux/macOS/Git Bash:

```bash
watch -n 5 'curl -s http://localhost:8000/api/public/dashboard'
```

PowerShell:

```powershell
while ($true) {
  Invoke-RestMethod http://localhost:8000/api/public/dashboard
  Start-Sleep -Seconds 5
}
```

### SQLite inspection commands

Open the SQLite database inside the container:

```bash
docker compose exec machine-research-backend sqlite3 /data/machine_research.db
```

List tables:

```bash
docker compose exec machine-research-backend sqlite3 /data/machine_research.db ".tables"
```

Show the latest 20 sensor readings:

```bash
docker compose exec machine-research-backend sqlite3 -header -column /data/machine_research.db \
  "SELECT recorded_at, sensor_name, temperature, relative_humidity FROM sensor_readings ORDER BY recorded_at DESC LIMIT 20;"
```

Show the latest 20 activation events:

```bash
docker compose exec machine-research-backend sqlite3 -header -column /data/machine_research.db \
  "SELECT started_at, completed_at, machine_id, status, trigger_source, message FROM activation_events ORDER BY started_at DESC LIMIT 20;"
```

Show row counts:

```bash
docker compose exec machine-research-backend sqlite3 -header -column /data/machine_research.db \
  "SELECT 'sensor_readings' AS table_name, COUNT(*) AS rows FROM sensor_readings UNION ALL SELECT 'activation_events', COUNT(*) FROM activation_events UNION ALL SELECT 'system_logs', COUNT(*) FROM system_logs;"
```

### Export data to CSV

Export sensor readings to `sensor_readings.csv`:

```bash
docker compose exec machine-research-backend sh -c \
  "sqlite3 -header -csv /data/machine_research.db 'SELECT * FROM sensor_readings ORDER BY recorded_at;'" \
  > sensor_readings.csv
```

Export activation events to `activation_events.csv`:

```bash
docker compose exec machine-research-backend sh -c \
  "sqlite3 -header -csv /data/machine_research.db 'SELECT * FROM activation_events ORDER BY started_at;'" \
  > activation_events.csv
```

Export system logs to `system_logs.csv`:

```bash
docker compose exec machine-research-backend sh -c \
  "sqlite3 -header -csv /data/machine_research.db 'SELECT * FROM system_logs ORDER BY created_at;'" \
  > system_logs.csv
```

Copy the raw SQLite database out of Docker:

```bash
docker cp machine-research-backend:/data/machine_research.db ./machine_research.db
```

### Windows Arduino USB commands

List USB devices from PowerShell:

```powershell
usbipd list
```

Bind an Arduino to USB/IP from an administrator PowerShell:

```powershell
usbipd bind --busid <BUSID>
```

Attach it to WSL:

```powershell
usbipd attach --busid <BUSID> --wsl
```

Check the Linux serial device name from WSL:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

Detach a device when finished:

```powershell
usbipd detach --busid <BUSID>
```

### Tailscale sharing commands

Private tailnet-only sharing:

```bash
tailscale serve --bg http://127.0.0.1:8000
```

Public sharing without requiring viewers to install Tailscale:

```bash
tailscale funnel --bg http://127.0.0.1:8000
```

Check Tailscale Serve/Funnel status:

```bash
tailscale serve status
```

Turn Funnel off:

```bash
tailscale funnel off
```

Reset all Serve/Funnel config:

```bash
tailscale serve reset
```

## SQLite and Grafana

The default database is:

```text
data/machine_research.db
```

In Docker, the database is stored at:

```text
/data/machine_research.db
```

That `/data` directory is backed by the `machine_research_data` Docker volume in `docker-compose.yml`.

Grafana can read SQLite using a SQLite data source plugin. Useful tables:

- `sensor_readings`: `recorded_at`, `sensor_name`, `temperature`, `relative_humidity`
- `activation_events`: `started_at`, `completed_at`, `machine_id`, `status`, `trigger_source`
- `machines`: current schedule and next run time

Example Grafana-style query:

```sql
SELECT
  recorded_at AS time,
  sensor_name,
  temperature
FROM sensor_readings
WHERE recorded_at >= datetime('now', '-24 hours')
ORDER BY recorded_at;
```

## systemd service

Create `/etc/systemd/system/machine-research-backend.service`:

```ini
[Unit]
Description=Machine Research Backend
After=network-online.target

[Service]
WorkingDirectory=/opt/machine-research-backend
EnvironmentFile=/opt/machine-research-backend/.env
ExecStart=/opt/machine-research-backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
User=YOUR_LINUX_USER

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now machine-research-backend
sudo systemctl status machine-research-backend
```

## Tailscale notes

Run this backend on the same machine that has access to the Arduinos and actuator hardware. Tailscale should expose that host privately to your tailnet. Your web app can call the backend at:

```text
http://TAILSCALE_IP_OR_HOSTNAME:8000/api/public/dashboard
```

The public read-only dashboard is available at:

```text
http://TAILSCALE_IP_OR_HOSTNAME:8000/public
```

The protected sysadmin dashboard is available at:

```text
http://TAILSCALE_IP_OR_HOSTNAME:8000/admin
```

Add the web app origin to:

```text
CORS_ORIGINS=http://your-webapp-host:3000,http://your-webapp-host:5173
```

### Do viewers need Tailscale?

It depends on how you share the site:

- Private sharing: invite viewers through Tailscale or share the machine with their Tailscale account. They need Tailscale installed and they can access `http://TAILSCALE_IP_OR_HOSTNAME:8000/public`.
- Public sharing: use Tailscale Funnel, Cloudflare Tunnel, a VPS reverse proxy, or normal router port forwarding. Viewers do not need Tailscale, but the page is reachable by anyone who has the URL.
- Recommended setup: expose only `/public` to general viewers and keep `/admin` limited to your own Tailscale network.

### Tailscale Serve for private viewers

Use Serve when viewers are in your tailnet or have accepted a shared-device invite:

```bash
tailscale serve --bg http://127.0.0.1:8000
```

Then share the generated Tailscale URL with viewers. This remains private to authorized Tailscale users.

### Tailscale Funnel for public viewers

Use Funnel when viewers should not need Tailscale:

```bash
tailscale funnel --bg http://127.0.0.1:8000
```

Only do this after changing `ADMIN_PASSWORD`. Funnel exposes the service publicly, so anyone with the URL can reach the app. The `/admin` page still requires Basic Auth, but the safest pattern is to expose this container behind a reverse proxy that only forwards `/public` and `/api/public/*`.

### Safer public reverse-proxy pattern

If you use Caddy, Nginx, Cloudflare Tunnel, or another reverse proxy, route only these paths to the internet:

```text
/public
/api/public/dashboard
/api/public/sensors/readings
```

Do not publicly route these admin/control paths:

```text
/admin
/api/dashboard
/api/machines
/api/activations
/api/logs
/api/data/summary
```

### Admin API with Basic Auth

Example:

```bash
curl -u admin:change-me-now http://localhost:8000/api/dashboard
```

Change the password first:

```yaml
environment:
  ADMIN_USERNAME: admin
  ADMIN_PASSWORD: your-long-random-password
```

## Testing

```bash
pytest
```

Simulator mode inserts fake readings every 10 seconds for both Arduino names. This lets you build and verify the web app sensor cards and charts before the hardware is connected.

## Relay control (MCC USB-1208FS-Plus)

The backend can drive 3 relays via a Measurement Computing **USB-1208FS-Plus** DAQ. Three logical relays — `relay-1`, `relay-2`, `relay-3` — are created automatically on first startup, with current state and a full event history stored in SQLite (`relays` and `relay_events` tables).

### Two controller modes

| `RELAY_CONTROLLER` | Behavior | Where it works |
| --- | --- | --- |
| `mock` (default) | Logs the bit-mask write only, no hardware I/O. | Linux, macOS, Windows, Docker |
| `mcc_usb1208fs_plus` | Drives the chosen DIO port via `mcculw`. | **Windows only** (requires MCC Universal Library + InstaCal) |

The MCC controller uses a single output **byte latch**: every change is computed by masking the latch (`latch = (latch & ~mask) | new_bit`) and written with `d_out()` on the configured port. This means toggling one relay never disturbs the other bits on the same port. We avoid `d_bit_out()` because it is documented to misbehave on some MCC ports.

### Wiring safety (READ THIS)

The USB-1208FS-Plus digital outputs are **TTL-level I/O**. They must **not** drive relay coils directly. Use one of:

- A relay board with **opto-isolated TTL inputs** (typical 5 V logic-level relay modules).
- An external relay driver IC / transistor stage.
- A solid-state relay (SSR) with a TTL-compatible control input.

`FIRSTPORTB` lines on the USB-1208FS-Plus are documented as higher-current (24 mA) than other DIO lines, which is why it is the default in `MCC_DIGITAL_PORT`. Even so, treat the DIO outputs as logic-level signal lines, not coil drivers. Always confirm your relay board's input current and voltage requirements against the [USB-1208FS-Plus datasheet](https://www.mccdaq.com/) before wiring.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `RELAY_CONTROLLER` | `mock` | `mock` or `mcc_usb1208fs_plus` |
| `MCC_BOARD_NUM` | `0` | Board number configured in InstaCal |
| `MCC_DIGITAL_PORT` | `FIRSTPORTB` | `mcculw.enums.DigitalPortType` name |
| `RELAY_1_BIT` | `0` | Bit index on the port for relay-1 |
| `RELAY_2_BIT` | `1` | Bit index on the port for relay-2 |
| `RELAY_3_BIT` | `2` | Bit index on the port for relay-3 |
| `RELAY_ACTIVE_HIGH` | `true` | Set `false` if your relay board is active-low |

### Windows-native setup

Docker on Windows talks to the Linux WSL kernel and **cannot** use `mcculw`. To control real relays, run the backend natively on Windows.

1. **Install Python 3.12 or 3.13 (64-bit)** from <https://www.python.org/downloads/windows/>. Tick "Add python.exe to PATH" during install. (The dependency stack supports 3.10–3.13.)
2. **Install MCC DAQ Software** (includes Universal Library and InstaCal): <https://www.mccdaq.com/Software-Downloads>. Reboot if prompted.
3. **Open InstaCal**, plug in the USB-1208FS-Plus, and confirm it is listed as **Board 0** (or whatever you set in `MCC_BOARD_NUM`). Click _Test_ → _Digital_ to confirm DIO works.
4. **Clone the repo and create a venv** (PowerShell):

   ```powershell
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   pip install -r requirements-windows.txt
   ```

   `requirements-windows.txt` installs `mcculw` and is intentionally **not** in the main `requirements.txt` so Linux/Docker installs do not break.
5. **Configure environment** (PowerShell, current session only):

   ```powershell
   Copy-Item .env.example .env
   $env:RELAY_CONTROLLER = "mcc_usb1208fs_plus"
   $env:MCC_BOARD_NUM = "0"
   $env:MCC_DIGITAL_PORT = "FIRSTPORTB"
   $env:RELAY_1_BIT = "0"
   $env:RELAY_2_BIT = "1"
   $env:RELAY_3_BIT = "2"
   $env:RELAY_ACTIVE_HIGH = "true"
   $env:ADMIN_USERNAME = "admin"
   $env:ADMIN_PASSWORD = "change-me-now"
   ```

   Or edit `.env` and set `RELAY_CONTROLLER=mcc_usb1208fs_plus`.
6. **Run the server**:

   ```powershell
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

   On startup the backend will configure the chosen port for output and force all three relays to OFF.

### CLI control with curl

```bash
# Public read-only relay status (no auth required)
curl http://localhost:8000/api/public/relays

# Admin: list relays
curl -u admin:change-me-now http://localhost:8000/api/relays

# Admin: get one relay
curl -u admin:change-me-now http://localhost:8000/api/relays/relay-1

# Admin: turn relays on/off
curl -u admin:change-me-now -X POST http://localhost:8000/api/relays/relay-1/on
curl -u admin:change-me-now -X POST http://localhost:8000/api/relays/relay-2/off

# Admin: explicit set (JSON body)
curl -u admin:change-me-now -X POST http://localhost:8000/api/relays/relay-3/set \
     -H "Content-Type: application/json" -d '{"on": true}'

# Admin: toggle
curl -u admin:change-me-now -X POST http://localhost:8000/api/relays/relay-1/toggle

# Admin: history
curl -u admin:change-me-now "http://localhost:8000/api/relays/relay-1/events?limit=50"
curl -u admin:change-me-now "http://localhost:8000/api/relay-events?limit=200"
```

Same calls in PowerShell:

```powershell
$cred = "admin:change-me-now"
$auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($cred))
$headers = @{ Authorization = "Basic $auth" }

Invoke-RestMethod -Headers $headers -Method Post http://localhost:8000/api/relays/relay-1/on
Invoke-RestMethod -Headers $headers -Method Post http://localhost:8000/api/relays/relay-2/toggle
Invoke-RestMethod -Headers $headers http://localhost:8000/api/relays
```

### Dashboards

- The **public** dashboard (`/`, `/public`) shows the three relay states read-only.
- The **sysadmin** dashboard (`/admin`) provides a full **Relay Control** panel:
  - Per-relay live state (ON / OFF) with a colored status indicator and last-changed timestamp.
  - Bit / channel, configured digital port, active-high vs. active-low logic, controller mode
    (`mock` or `mcc_usb1208fs_plus`), output latch, and "initialized" flag.
  - Admin-only **Turn ON / Turn OFF / Toggle** buttons per relay. Each command shows
    a status banner (in-progress, success, or error) so you know whether the command worked.
  - An **Edit metadata** form per relay (display name, description, display order, enabled/disabled).
    A relay marked disabled refuses ON / Toggle commands; OFF is always allowed for safety.
  - A **Relay history** tab showing recent relay events (action, target state, trigger source,
    success flag, message, timestamp) sourced from the `relay_events` table.
- Hardware-level relay configuration (`RELAY_CONTROLLER`, `MCC_*`, bit map, `RELAY_ACTIVE_HIGH`)
  is shown read-only on the dashboard and is changed via `.env` followed by a backend restart.
  This is intentional — live reassignment of MCC bit mappings is **not** allowed from the UI
  to avoid driving the wrong physical channel.
- Mutation endpoints (`POST /api/relays/*`, `PATCH /api/relays/{id}`) require Basic Auth with
  `ADMIN_USERNAME` / `ADMIN_PASSWORD`. Read-only state is also exposed at `GET /api/public/relays`
  and as part of `GET /api/public/dashboard` for the public page.

Additional admin API endpoints used by the dashboard:

```bash
# Edit relay metadata (name / description / enabled / display_order)
curl -u admin:change-me-now -X PATCH http://localhost:8000/api/relays/relay-1 \
  -H "Content-Type: application/json" \
  -d '{"name": "Vacuum pump", "description": "PB.0 line, opto-isolated module", "enabled": true, "display_order": 1}'

# Inspect controller mode / port / bit map / latch (read-only; reflects .env)
curl -u admin:change-me-now http://localhost:8000/api/relays-controller
```

### Notes / quirks

- `mcculw` is Windows-only and imported lazily; the backend imports cleanly and runs in `mock` mode on Linux/macOS/Docker even when `mcculw` is not installed.
- If `mcculw` import or `d_config_port` fails at startup the app still boots, logs a warning, and relay writes will return a hardware error instead of crashing the server.
- `RELAY_ACTIVE_HIGH=false` flips the on/off semantics for boards whose inputs are active-low.

## Two-machine Docker deployment

This is the easiest split-mode setup: each computer runs a single Docker
container. The **hub** container hosts the website and SQLite database.
The **collector** container drives the lab automation code and pushes
data to the hub over HTTP (typically through Tailscale).

```
+-----------------------------+              +------------------------------+
|  HOME / SERVER computer     |  HTTP push   |  LAB / AUTOMATION computer   |
|  docker-compose.hub.yml     |  <---------  |  docker-compose.collector.yml|
|  APP_MODE=hub               |   Tailscale  |  APP_MODE=collector          |
|  Web UI + SQLite at /data   |              |  Arduino + automation code   |
|  Port 8000 published        |              |  No public port published    |
+-----------------------------+              +------------------------------+
```

The collector only needs **outbound** access to the hub. You do not need
to expose the lab computer to inbound traffic.

> **Windows + MCC USB-1208FS-Plus relay hardware:** Docker on Windows runs
> Linux containers, and the MCC Universal Library / `mcculw` is
> Windows-only. USB and driver passthrough into Linux containers on
> Windows Docker Desktop is not reliably supported, so a Dockerized
> collector on Windows is only suitable for `RELAY_CONTROLLER=mock` or
> network-only collectors. **For real MCC relay hardware, run the
> collector natively on Windows** — see
> [Windows-native collector for real MCC hardware](#windows-native-collector-for-real-mcc-hardware)
> below.

### Files

| File                            | Where it runs               | Purpose                              |
|---------------------------------|-----------------------------|--------------------------------------|
| `docker-compose.yml`            | one machine                 | All-in-one (default, unchanged)      |
| `docker-compose.hub.yml`        | home/server computer        | Hub container only                   |
| `docker-compose.collector.yml`  | lab/automation computer     | Collector container only             |
| `.env.hub.example`              | home/server computer        | Template — copy to `.env` on the hub |
| `.env.collector.example`        | lab/automation computer     | Template — copy to `.env` on collector |

### A. Home / server computer (hub container)

1. Install Docker (Docker Desktop on Windows/macOS, or `docker.io` on Linux)
   and Tailscale. Note the Tailscale IP (e.g. `100.64.1.10`) or DNS name
   (e.g. `lab-hub.tailnet-name.ts.net`) of this computer.
2. Clone the repo and create the hub's `.env`:

   ```bash
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   cp .env.hub.example .env
   ```

3. Edit `.env` on the hub machine. At minimum change:

   ```dotenv
   ADMIN_PASSWORD=use-a-real-password
   COLLECTOR_API_TOKEN=generate-a-long-random-secret
   ```

   Generate a token with:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

4. Build and start:

   ```bash
   docker compose -f docker-compose.hub.yml up -d --build
   ```

5. Verify locally:

   ```bash
   curl -i http://localhost:8000/api/health
   ```

   Open `http://localhost:8000/` (public) and `http://localhost:8000/admin`
   (sysadmin login). On Tailscale, the same hub is reachable at
   `http://HOME_TAILSCALE_IP:8000/` from any tailnet device.

### B. Lab / automation computer (collector container)

Use this when the lab machine is Linux, OR when you only need a
mock/network-only collector on Windows. **For real MCC relays on
Windows, skip this and use
[Windows-native collector for real MCC hardware](#windows-native-collector-for-real-mcc-hardware).**

1. Install Docker and Tailscale on the lab machine and join the same
   tailnet as the hub.
2. Clone the repo and create the collector's `.env`:

   ```bash
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   cp .env.collector.example .env
   ```

3. Edit `.env` on the collector machine. At minimum:

   ```dotenv
   # Use the hub's Tailscale IP or DNS name. Both work:
   HUB_BASE_URL=http://100.64.1.10:8000
   # HUB_BASE_URL=http://lab-hub.tailnet-name.ts.net:8000

   # MUST match what you set on the hub.
   COLLECTOR_API_TOKEN=generate-a-long-random-secret

   COLLECTOR_ID=collector-1
   COLLECTOR_NAME="Lab Collector"
   ```

   To start in mock mode (verifies hub<->collector wiring without
   hardware), keep `SENSOR_SIMULATOR=true` and `RELAY_CONTROLLER=mock`.

4. (Linux + real Arduino) Edit `docker-compose.collector.yml` and
   uncomment the `devices:` block, e.g.:

   ```yaml
       devices:
         - "/dev/ttyACM0:/dev/ttyACM0"
         - "/dev/ttyACM1:/dev/ttyACM1"
   ```

   Confirm the host paths first with `ls /dev/ttyACM*`. Then in the
   collector's `.env` set `SENSOR_SIMULATOR=false` and adjust
   `ARDUINO_1_PORT` / `ARDUINO_2_PORT` to those `/dev/ttyACM*` names.

   On Windows Docker Desktop, mounting a host `COM3` device into a
   Linux container is not supported. Run the Windows-native collector
   instead.

5. Build and start:

   ```bash
   docker compose -f docker-compose.collector.yml up -d --build
   ```

6. Tail the collector log to confirm it is reaching the hub:

   ```bash
   docker logs -f machine-research-collector
   ```

   On startup you should see push/poll log lines and no `401` or
   connection-refused errors.

### C. Test connectivity from the collector machine to the hub

Run these from the lab/collector computer. Replace the IP, DNS name, and
token with your real values.

```bash
# Hub is reachable on Tailscale?
curl -i http://HOME_TAILSCALE_IP:8000/api/health
# or
curl -i http://lab-hub.tailnet-name.ts.net:8000/api/health

# Token works end-to-end?
curl -i \
  -H "X-Collector-Token: generate-a-long-random-secret" \
  -H "Content-Type: application/json" \
  -X POST http://HOME_TAILSCALE_IP:8000/api/collector/heartbeat \
  -d '{"collector_id":"collector-1","name":"Lab Collector","mode":"collector"}'
```

A `200 OK` from both means Tailscale, the hub, and the shared token are
working. A `401` means `COLLECTOR_API_TOKEN` doesn't match between the
two `.env` files.

### D. See the collector online in the admin UI

On the hub machine, open `http://localhost:8000/admin` (or
`http://HOME_TAILSCALE_IP:8000/admin` from another tailnet device) and
sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. The **Collector
status** panel should show `collector-1` with a recent heartbeat
timestamp. If it says `stale` or is missing, the collector has not
successfully heartbeated in the last ~60 seconds — check the collector
container's log.

### E. Updating, stopping, restarting

```bash
# On the hub machine
docker compose -f docker-compose.hub.yml restart
docker compose -f docker-compose.hub.yml down
docker compose -f docker-compose.hub.yml up -d --build

# On the collector machine
docker compose -f docker-compose.collector.yml restart
docker compose -f docker-compose.collector.yml down
docker compose -f docker-compose.collector.yml up -d --build
```

The hub's SQLite database lives in the named volume
`machine_research_hub_data` and survives `down`/rebuilds. The collector's
small local buffer lives in `machine_research_collector_data`.

### Windows-native collector for real MCC hardware

Use this path when the lab computer is Windows AND you need real MCC
USB-1208FS-Plus relay control. Docker is **not** used on the collector
side here — the hub computer can still run inside Docker.

> **TL;DR:** for a fresh lab Windows box, just follow
> [Windows Collector Quick Start (one-command native start)](#windows-collector-quick-start-one-command-native-start)
> above. The four batch scripts (`setup-collector-windows.bat`,
> `start-collector.bat`, `stop-collector.bat`, `status-collector.bat`)
> wrap everything below into one command per lifecycle step and add
> preflight checks. The detailed steps below remain accurate if you want
> to do it by hand.

1. On the lab Windows machine, install Python 3.11+, MCC InstaCal, and
   join the tailnet. From `cmd.exe`:

   ```bat
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   copy .env.collector.example .env
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -r requirements-windows.txt
   ```

2. Edit `.env` on the Windows machine:

   ```dotenv
   APP_MODE=collector
   HUB_BASE_URL=http://HOME_TAILSCALE_IP:8000
   COLLECTOR_API_TOKEN=generate-a-long-random-secret
   COLLECTOR_ID=collector-1
   COLLECTOR_NAME="Lab Collector"

   RELAY_CONTROLLER=mcc_usb1208fs_plus
   MCC_BOARD_NUM=0
   MCC_DIGITAL_PORT=FIRSTPORTB
   RELAY_1_BIT=0
   RELAY_2_BIT=1
   RELAY_3_BIT=2
   RELAY_ACTIVE_HIGH=true

   SENSOR_SIMULATOR=false
   ARDUINO_1_PORT=COM3
   ARDUINO_2_PORT=COM4
   ARDUINO_BAUDRATE=9600
   ```

3. Run it:

   ```bat
   .venv\Scripts\activate
   uvicorn app.main:app --host 127.0.0.1 --port 8001
   ```

   (Optional) install as a Windows service with NSSM — see the
   [Two-computer (hub + collector) deployment over Tailscale](#two-computer-hub--collector-deployment-over-tailscale)
   section below for the exact NSSM commands.

4. Confirm the collector appears under **Collector status** on the hub's
   `/admin` page.

### Which `.env` to edit on which machine

- On the **hub** computer: edit `.env` in the project folder. It was
  created from `.env.hub.example`. Hub-only settings live here
  (`ADMIN_PASSWORD`, `COLLECTOR_API_TOKEN`, etc.).
- On the **collector** computer: edit `.env` in the project folder there.
  It was created from `.env.collector.example`. Collector-only settings
  live here (`HUB_BASE_URL`, hardware ports, MCC settings, etc.).
- The `COLLECTOR_API_TOKEN` value in the two files MUST match exactly.
- The all-in-one `.env.example` is still used by `docker-compose.yml` for
  the single-machine default.

## Two-computer (hub + collector) deployment over Tailscale

The backend supports a split deployment so the public dashboard and the lab
hardware can live on different computers. The home computer runs the **hub**
(web service, SQLite DB, sysadmin UI). The lab Windows computer runs the
**collector**, which talks to the local Arduino sensors and the MCC USB-1208FS-Plus
relay board, then pushes data to the hub through Tailscale.

```
+---------------------+                  +---------------------------+
|  Home server (hub)  |  <-- HTTPS  --   |   Lab PC (Windows)        |
|  APP_MODE=hub       |  via Tailscale   |   APP_MODE=collector      |
|  Dashboards + DB    |                  |   Arduinos + MCC relays   |
+---------------------+                  +---------------------------+
```

Communication is one-way HTTP from the collector to the hub (push readings,
poll for commands). You do **not** need to expose the lab computer to inbound
traffic; only the hub needs to be reachable on its Tailscale IP/URL.

### A. Home/server computer (hub)

1. Install Tailscale and join your tailnet. Note the machine's Tailscale URL
   or 100.x.y.z IP — call it `https://lab-hub.ts.net` for the rest of this guide.
2. Clone this repo and copy `.env.example` to `.env`:

   ```bash
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   cp .env.example .env
   ```

3. Edit `.env` so the hub serves dashboards but never tries to talk to local
   hardware:

   ```dotenv
   APP_MODE=hub
   DATABASE_URL=sqlite:///./data/machine_research.db
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=use-a-real-password
   COLLECTOR_API_TOKEN=generate-a-long-random-secret
   COLLECTOR_ID=collector-1
   COLLECTOR_NAME="Lab Collector"
   # Hub does not run the collector loop, so HUB_BASE_URL is irrelevant here.
   # Keep MCC settings at defaults; hub mode never touches the MCC library.
   ```

4. Start the hub. Either run with Docker:

   ```bash
   docker compose up -d --build
   ```

   …or run directly with Python:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

5. Confirm the hub is reachable on Tailscale:

   - Public dashboard: `https://lab-hub.ts.net/`
   - Sysadmin dashboard: `https://lab-hub.ts.net/admin` (Basic Auth)
   - Health check: `https://lab-hub.ts.net/api/health`

### B. Lab Windows computer (collector)

1. Install Tailscale on the Windows lab machine and join the same tailnet.
2. Install Python 3.11+, MCC InstaCal, and clone the repo. From a `cmd.exe`
   prompt:

   ```bat
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   copy .env.example .env
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -r requirements-windows.txt
   ```

3. Edit `.env` for collector mode (use the same `COLLECTOR_API_TOKEN` you
   chose on the hub). Replace `lab-hub.ts.net` with your hub's actual
   Tailscale name or `100.x.y.z` IP:

   ```dotenv
   APP_MODE=collector
   HUB_BASE_URL=https://lab-hub.ts.net
   COLLECTOR_API_TOKEN=generate-a-long-random-secret
   COLLECTOR_ID=collector-1
   COLLECTOR_NAME="Lab Collector"

   # Hardware on the Windows lab machine
   RELAY_CONTROLLER=mcc_usb1208fs_plus
   MCC_BOARD_NUM=0
   MCC_DIGITAL_PORT=FIRSTPORTB
   RELAY_1_BIT=0
   RELAY_2_BIT=1
   RELAY_3_BIT=2
   RELAY_ACTIVE_HIGH=true

   # Arduinos (set SENSOR_SIMULATOR=false once the boards are wired)
   SENSOR_SIMULATOR=false
   ARDUINO_1_PORT=COM3
   ARDUINO_1_NAME=arduino-1
   ARDUINO_2_PORT=COM4
   ARDUINO_2_NAME=arduino-2
   ARDUINO_BAUDRATE=9600
   ```

4. The collector also stores a small local SQLite buffer used to track which
   readings have been shipped. Make sure the working directory is writable.

### C. Test connectivity from the Windows collector to the hub

From a `cmd.exe` prompt on the Windows lab machine, before starting the
collector loop:

```bat
:: Replace lab-hub.ts.net and the token with your real values.
curl -i https://lab-hub.ts.net/api/health
curl -i -H "X-Collector-Token: generate-a-long-random-secret" ^
  -H "Content-Type: application/json" ^
  -X POST https://lab-hub.ts.net/api/collector/heartbeat ^
  -d "{\"collector_id\":\"collector-1\",\"name\":\"Lab Collector\",\"mode\":\"collector\"}"
```

Or PowerShell:

```powershell
Invoke-RestMethod https://lab-hub.ts.net/api/health
Invoke-RestMethod -Method Post `
  -Uri https://lab-hub.ts.net/api/collector/heartbeat `
  -Headers @{ "X-Collector-Token" = "generate-a-long-random-secret" } `
  -ContentType "application/json" `
  -Body '{"collector_id":"collector-1","name":"Lab Collector","mode":"collector"}'
```

A `200 OK` response means Tailscale, the hub, and the shared token are all
working. After this you should see the collector show up under
**Collector status** on the hub's `/admin` page.

### D. Run both processes

- **Hub** (home computer): `uvicorn app.main:app --host 0.0.0.0 --port 8000`
  or `docker compose up -d`.
- **Collector** (Windows lab computer):

  ```bat
  cd Lab-Controller
  .venv\Scripts\activate
  uvicorn app.main:app --host 127.0.0.1 --port 8001
  ```

  The collector itself is a regular FastAPI app — it serves a small health
  endpoint and runs the background `CollectorAgent` thread which pushes data
  to the hub on `COLLECTOR_PUSH_INTERVAL_SECONDS` and polls for commands /
  schedules on `COLLECTOR_POLL_INTERVAL_SECONDS`. The collector's local
  `/admin` and `/` pages are not used in split mode; do all your dashboard
  work through the hub.

  To run it as a Windows service, you can use NSSM:

  ```bat
  nssm install LabCollector "C:\path\to\Lab-Controller\.venv\Scripts\uvicorn.exe" ^
    app.main:app --host 127.0.0.1 --port 8001
  nssm set LabCollector AppDirectory C:\path\to\Lab-Controller
  nssm start LabCollector
  ```

### E. Single-machine mode (default, unchanged)

Leave `APP_MODE=all_in_one` (or omit it entirely). The single backend then
runs dashboards, the SQLite DB, the sensor reader, and the relay controller
on one host. This is the original behavior and is unchanged.

### Troubleshooting

- `401` from `/api/collector/*` endpoints means `COLLECTOR_API_TOKEN`
  doesn't match between the hub and the collector. Re-set both `.env`
  files and restart both processes.
- The hub's `/admin` page shows "stale" if the collector hasn't sent a
  heartbeat in the last 60 seconds. Check the collector's Tailscale
  connection and look at the collector log for `collector loop error`
  warnings.
- The collector backs off exponentially (up to 60s) when the hub is
  unreachable. On reconnect it ships any buffered sensor readings and
  relay events that accumulated while offline.
- Schedule edits made on the hub are pulled by the collector at the
  next poll. Hub-side reads (e.g. `is_on`) reflect the last status the
  collector pushed up.

## Multi-collector deployment (one Mac hub + three Windows collectors)

The hub holds a persistent **machine registry** in its database. You no
longer hard-code the machine list in the hub's `.env` — every collector
machine identifies itself with its own `COLLECTOR_ID`/`COLLECTOR_NAME`
on its own `.env`, calls `POST /api/collector/register` once at startup,
and shows up in the admin dashboard automatically. Each collector's
SSR/relay schedules are stored independently in the hub DB, so three
collectors can run three different intervals.

### Architecture

```
                           +----------------------------+
                           |  Mac hub (APP_MODE=hub)    |
                           |  /admin /public web UI     |
                           |  SQLite registry + schedules
                           |  Port 8000 (Tailscale)     |
                           +-----------+----------------+
                                       ^
              POST /api/collector/{register,heartbeat,...}
                                       |
       +-------------------+-------------------+-------------------+
       |                   |                   |                   |
+----------------+ +----------------+ +----------------+ +----------------+
| Windows Lab A  | | Windows Lab B  | | Windows Lab C  |    ...
| collector      | | collector      | | collector      |
| ID=lab-coll-a  | | ID=lab-coll-b  | | ID=lab-coll-c  |
| ON=30 / OFF=90 | | ON=120 /OFF=600| | ON=5  / OFF=5  |
+----------------+ +----------------+ +----------------+
```

Each collector applies **only its own schedules** to its MCC USB-1208FS-Plus
relay board. Hub-side admin edits to `lab-collector-a/relay-1` never reach
`lab-collector-b`'s hardware.

### 1. Set up the Mac hub

```bash
# On the Mac
git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
cd Lab-Controller
cp .env.hub.example .env
# Edit .env: set ADMIN_PASSWORD and COLLECTOR_API_TOKEN
docker compose -f docker-compose.hub.yml up -d --build
```

The hub's `.env` only contains infrastructure/security: `APP_MODE=hub`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD`, `COLLECTOR_API_TOKEN`,
`DATABASE_URL`, `COLLECTOR_STALE_AFTER_SECONDS`. **It does not contain
the list of lab machines** — that is built dynamically as each collector
registers.

### 2. Set up each Windows collector with a unique identity

On every lab computer (three different machines in this example):

```dotenv
# Lab A — Windows native, real MCC hardware
APP_MODE=collector
HUB_BASE_URL=http://100.64.1.10:8000        # Tailscale IP of the Mac hub
COLLECTOR_API_TOKEN=<same token as the hub>
COLLECTOR_ID=lab-collector-a
COLLECTOR_NAME=Lab A (incubator)
RELAY_CONTROLLER=mcc_usb1208fs_plus
```

```dotenv
# Lab B
APP_MODE=collector
HUB_BASE_URL=http://100.64.1.10:8000
COLLECTOR_API_TOKEN=<same token>
COLLECTOR_ID=lab-collector-b
COLLECTOR_NAME=Lab B (humidity chamber)
RELAY_CONTROLLER=mcc_usb1208fs_plus
```

```dotenv
# Lab C
APP_MODE=collector
HUB_BASE_URL=http://100.64.1.10:8000
COLLECTOR_API_TOKEN=<same token>
COLLECTOR_ID=lab-collector-c
COLLECTOR_NAME=Lab C (oven)
RELAY_CONTROLLER=mcc_usb1208fs_plus
```

> Run the collector **natively on Windows** for real MCC hardware
> (see [Windows-native collector for real MCC hardware](#windows-native-collector-for-real-mcc-hardware)).
> Use the Docker collector for `RELAY_CONTROLLER=mock` integration tests
> only.

Start each collector. The first heartbeat creates the machine row in the
hub's database; subsequent heartbeats update it.

### 3. Verify each collector is online

```bash
# From the Mac (or any Tailscale node)
curl -u admin:'YOUR_ADMIN_PASSWORD' http://100.64.1.10:8000/api/admin/machines | jq .
# Expected: 3 entries with machine_key=lab-collector-{a,b,c}, online=true
```

Or open `http://100.64.1.10:8000/admin` and look at the
**Registered collectors / machines** card.

### 4. Set three different ON/OFF intervals from the Mac

You can edit each machine's relay-1/2/3 schedules from the admin UI's
**Per-machine SSR / relay schedules** card, or from the API:

```bash
ADMIN=admin:'YOUR_ADMIN_PASSWORD'
HUB=http://100.64.1.10:8000

# Lab A: relay-1 cycles 30s ON, 90s OFF
curl -u "$ADMIN" -X PATCH \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"on_duration_seconds":30,"off_duration_seconds":90}' \
  "$HUB/api/admin/machines/lab-collector-a/relay-schedules/relay-1"

# Lab B: relay-1 cycles 120s ON, 600s OFF
curl -u "$ADMIN" -X PATCH \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"on_duration_seconds":120,"off_duration_seconds":600}' \
  "$HUB/api/admin/machines/lab-collector-b/relay-schedules/relay-1"

# Lab C: relay-1 cycles 5s ON, 5s OFF (fast test)
curl -u "$ADMIN" -X PATCH \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"on_duration_seconds":5,"off_duration_seconds":5}' \
  "$HUB/api/admin/machines/lab-collector-c/relay-schedules/relay-1"
```

Lab A's relay-1 keeps cycling 30/90 even after Lab B's relay-1 is
edited. Each collector polls `GET /api/collector/poll?collector_id=...`
and only receives schedule rows scoped to its own `collector_id`.

### 5. Manual registration / connectivity smoke tests

```bash
TOKEN='your-collector-api-token'
HUB=http://100.64.1.10:8000

# Register
curl -X POST -H "X-Collector-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"collector_id":"lab-collector-a","display_name":"Lab A","mode":"collector",
       "host":"lab-a.lan","hostname":"LAB-A-PC","software_version":"0.2.0",
       "relay_controller_mode":"mcc_usb1208fs_plus","relay_controller_initialized":true}' \
  "$HUB/api/collector/register"

# Heartbeat
curl -X POST -H "X-Collector-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"collector_id":"lab-collector-a","status_message":"ok","runtime_state":"running"}' \
  "$HUB/api/collector/heartbeat"

# Poll (returns only this collector's schedules + commands)
curl -H "X-Collector-Token: $TOKEN" \
  "$HUB/api/collector/poll?collector_id=lab-collector-a"
```

### Admin endpoints (Basic Auth)

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/machines` | List all registered collectors. |
| `GET /api/admin/machines/{key}` | One machine. |
| `PATCH /api/admin/machines/{key}` | Rename / enable / disable / change role. |
| `POST /api/admin/machines/{key}/disable` | Disable. |
| `POST /api/admin/machines/{key}/enable` | Re-enable. |
| `GET /api/admin/machines/{key}/relay-schedules` | This machine's three schedules. |
| `GET /api/admin/machines/{key}/relay-schedules/{relay_id}` | One schedule. |
| `PATCH /api/admin/machines/{key}/relay-schedules/{relay_id}` | Edit ON/OFF + enable. |

### Notes

- The hub starts with **zero collectors** — `/admin` shows
  "No collectors connected yet" until at least one collector posts a
  heartbeat. This is expected.
- `COLLECTOR_STALE_AFTER_SECONDS` (default `60`) controls when a
  collector flips from `online` to `stale` in the registry.
- The legacy single-machine endpoints (`/api/relay-schedules`,
  `/api/relays/{id}/schedule`) still work in `all_in_one` mode and
  default to the local collector's machine_key for backward compat.
- The public dashboard remains read-only and shows multi-machine cards
  on `/public` and `/api/public/dashboard`.
