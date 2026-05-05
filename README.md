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

1. **Install Python 3.12 (64-bit)** from <https://www.python.org/downloads/windows/>. Tick "Add python.exe to PATH" during install.
2. **Install MCC DAQ Software** (includes Universal Library and InstaCal): <https://www.mccdaq.com/Software-Downloads>. Reboot if prompted.
3. **Open InstaCal**, plug in the USB-1208FS-Plus, and confirm it is listed as **Board 0** (or whatever you set in `MCC_BOARD_NUM`). Click _Test_ → _Digital_ to confirm DIO works.
4. **Clone the repo and create a venv** (PowerShell):

   ```powershell
   git clone https://github.com/bradyburmeister06-commits/Lab-Controller.git
   cd Lab-Controller
   py -3.12 -m venv .venv
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

