# pyBEEP Potentiostat REST API

A lightweight FastAPI service that exposes a **REST API** to control one or more USB-connected potentiostats via **[pyBEEP]** (Aurelien Blanc). It is optimized for **Linux** (especially for SMB/NAS syncing) and also runs on **Windows** for the core measurement features.

> **Highlights**
>
> - Device discovery and slot mapping (`slot01`, `slot02`, …)
> - Query available modes and mode parameters from the hardware
> - **Parameter validation** helper endpoint
> - Start **multi-mode** experiments across multiple devices in parallel
> - Live **progress** estimates and detailed job status per slot
> - CSV output (plus optional PNG plots) with a clean, reproducible **folder layout**
> - Optional **SMB/NAS** upload and automatic local retention (Linux)
> - Simple API-key auth via `X-API-Key` header
>
> The server auto-detects devices at startup and exposes interactive API docs at **`/docs`**.

---

## Requirements

- **Python**: 3.10 or newer (uses modern `|` type hints)
- **OS**:
  - **Linux** (recommended): fully supported, including NAS/SMB syncing.
  - **Windows**: core API works; NAS/SMB helpers (Linux `mount`/`rsync`) are not available out of the box.
- **Hardware Driver**: [pyBEEP] installed (see below).
- **System tools (Linux / NAS)**: `mount` (CIFS), `rsync`, and `cifs-utils` (package names vary by distro).
- **Permissions**: Running as **administrator/root** is recommended (needed for serial access and CIFS mount).

## Install

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -U fastapi uvicorn pyserial
```

Install **pyBEEP** (package exposes `pyBEEP.controller` and `pyBEEP.plotter`). Either from PyPI (if available for you) or directly from the GitHub repo:

```bash
# Option A: PyPI (if published for your platform)
pip install pyBEEP

# Option B: from the upstream GitHub repository
pip install "git+https://github.com/aurelienblanc2/Potentiostat-driver-pyBEEP"
```

> Tip (Linux): add your user to the `dialout`/`uucp` group depending on your distro, or run the server with `sudo` to access `/dev/ttyACM*` devices.

## Configuration

Set environment variables (optional unless noted):

- `BOX_API_KEY` — if set, requests **must** include header `X-API-Key: <value>`.
- `BOX_ID` — free-form identifier of this box instance (reported by `/health`).
- `RUNS_ROOT` — directory where run folders are created. Default: `/opt/box/runs`.
- `NAS_CONFIG_PATH` — path to persistent NAS/SMB config JSON. Default: `/opt/box/nas_smb.json`.

The service creates `/opt/box` and `RUNS_ROOT` if needed.

## Run the server

```bash
# Linux (recommended: keep env vars with -E)
sudo -E uvicorn app:app --host 0.0.0.0 --port 8000

# Windows (run terminal as Administrator)
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000/docs** for interactive Swagger UI.

---

## API Overview

### Auth
If `BOX_API_KEY` is set, pass it in every request:
```
X-API-Key: <your-key>
```

### System & Devices
| Method | Path                            | Description |
|-------:|---------------------------------|-------------|
| GET    | `/version`                      | Returns API build info, Python and pyBEEP versions. |
| GET    | `/health`                       | Box health, number of detected devices, `box_id`. |
| GET    | `/devices`                      | List detected devices as `{slot, port, sn}`. |
| GET    | `/modes`                        | List available measurement modes from the first device. |
| GET    | `/modes/{mode}/params`          | Parameter schema for a given `mode` from the device. |
| POST   | `/modes/{mode}/validate`        | Offline parameter validation for common modes (CV, LSV, EIS, …). |

### Jobs & Progress
| Method | Path                            | Description |
|-------:|---------------------------------|-------------|
| POST   | `/jobs`                         | Start a **multi‑mode** run across one or more `slotXX` devices. |
| GET    | `/jobs`                         | List jobs (filter by `state=incomplete|completed` or by `group_id`). |
| GET    | `/jobs/{run_id}`                | Snapshot with `progress_pct`, `remaining_s`, slot states, files. |
| POST   | `/jobs/status`                  | Bulk status for many `run_id`s in one call. |
| POST   | `/jobs/{run_id}/cancel`         | Best‑effort cancel (queued slots cancel immediately; running slots request device abort). |

### Run Files
| Method | Path                                 | Description |
|-------:|--------------------------------------|-------------|
| GET    | `/runs/{run_id}/files`               | List all files (relative paths) inside a run directory. |
| GET    | `/runs/{run_id}/file?path=...`       | Download a single file by relative path. |
| GET    | `/runs/{run_id}/zip`                 | Download the entire run as a ZIP archive. |

### NAS / SMB (Linux)
| Method | Path                            | Description |
|-------:|---------------------------------|-------------|
| POST   | `/nas/setup`                    | Configure SMB target (host/share/credentials, base subdir, retention). |
| GET    | `/nas/health`                   | Quick health probe of the configured SMB target. |
| POST   | `/runs/{run_id}/upload`         | Enqueue an upload of a completed run to the NAS. |

### Admin
| Method | Path                            | Description |
|-------:|---------------------------------|-------------|
| POST   | `/admin/rescan`                 | Re-scan serial devices and refresh the slot registry. |

---

## Request & Response Details

### `JobRequest` (payload for `POST /jobs`)

```jsonc
{
  "devices": "all",                       // or ["slot01","slot02"]
  "modes": ["CV", "EIS"],                 // executed sequentially per slot
  "params_by_mode": {
    "CV": {
      "start": -0.2, "vertex1": 0.8, "vertex2": -0.2, "end": 0.0,
      "scan_rate": 0.1, "cycles": 3
    },
    "EIS": {
      "start_freq": 1.0e5, "end_freq": 1.0, "points_per_decade": 10, "cycles_per_freq": 3, "spacing": "log"
    }
  },
  "tia_gain": 0,
  "sampling_interval": 0.01,
  "experiment_name": "MyExperiment",
  "subdir": "BatchA",                     // optional (aka folder_name)
  "client_datetime": "2025-11-03T10:15:00",
  "run_name": null,                        // optional custom run_id
  "make_plot": true                        // save PNG plots next to CSV
}
```

**Responses**
- `201/200`: `JobStatus` with per-slot states (`queued|running|done|failed|cancelled`), `progress_pct`, `remaining_s`, `current_mode`, `remaining_modes`, and collected file paths.
- Errors carry a compact `{code, message, hint}` structure for easier debugging.

### Validation helper (`POST /modes/{mode}/validate`)

Returns a `ValidationResult` with `ok`, `errors`, and `warnings`. For example, CV validation enforces voltage ranges and warns on high scan rates or very large cycle counts. Other modes include basic “required field” checks and may show a “not yet implemented” warning. Use `/modes/{mode}/params` to discover the device‑specific keys and combine it with this helper where available.

---

## Data Layout & File Naming

All outputs go under `RUNS_ROOT` (default `/opt/box/runs`) in an experiment‑centric structure:

```
<RUNS_ROOT>/<experiment>/<optional-subdir>/<YYYY-MM-DDTHH_MM_SS>/
└── Wells/
    ├── slot01/
    │   ├── CV/
    │   │   ├── <experiment>_<subdir>_<timestamp>_slot01_CV.csv
    │   │   └── <experiment>_<subdir>_<timestamp>_slot01_CV.png
    │   └── EIS/
    │       └── <experiment>_<subdir>_<timestamp>_slot01_EIS.csv
    └── slot02/
        └── ...
```

- The `<timestamp>` is derived from the **client** timestamp you provide (`client_datetime`) and sanitized (colons/spaces replaced).  
- Each mode gets its own subfolder and files. PNG plots are saved when `make_plot=true` (CV uses cycle plots; other modes use a time‑series plot).

---

## NAS / SMB Upload (Linux)

Configure once, then upload runs as they finish:

```bash
# Configure (values are examples)
curl -X POST http://localhost:8000/nas/setup \
  -H "Content-Type: application/json" -H "X-API-Key: $BOX_API_KEY" \
  -d '{
        "host":"nas.local", "share":"experiments",
        "username":"lab", "password":"***",
        "base_subdir":"projectA/line2", "retention_days": 14
      }'

# Check health
curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/nas/health

# Enqueue upload for a finished run
curl -X POST -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/runs/<run_id>/upload
```

- Credentials are written to `/opt/box/.smbcredentials_nas` (mode `0600`).  
- A CIFS mount is created under `/mnt/nas_box` during health checks and uploads.  
- Uploads use `rsync -a` and create an `UPLOAD_DONE` marker on success.  
- A background retention loop deletes local runs older than `retention_days` **after** they were successfully uploaded.  
> These features require Linux with CIFS support and admin privileges.

---

## Examples

List devices (requires API key if configured):
```bash
curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/devices
```

Discover modes and start a simple CV on all slots:
```bash
# What modes are available?
curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/modes

# Start a job
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" -H "X-API-Key: $BOX_API_KEY" \
  -d '{
        "devices":"all",
        "modes":["CV"],
        "params_by_mode":{
          "CV":{"start":-0.2,"vertex1":0.8,"vertex2":-0.2,"end":0.0,"scan_rate":0.1,"cycles":3}
        },
        "experiment_name":"Demo",
        "client_datetime":"2025-11-03T12:00:00",
        "make_plot":true
      }'
```

Check progress:
```bash
curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/jobs/<run_id>
```

Download all output as a ZIP:
```bash
curl -H "X-API-Key: $BOX_API_KEY" -L -o run.zip http://localhost:8000/runs/<run_id>/zip
```

Cancel a run:
```bash
curl -X POST -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/jobs/<run_id>/cancel
```

---

## Notes & Design

- Device discovery assigns **slot names** (`slot01`, `slot02`, …) and tracks per-slot state. On cancellation, queued slots are cancelled immediately; running slots receive a best‑effort abort request.  
- Modes run **sequentially** per slot according to the `modes` list; different slots execute in parallel threads.  
- Progress estimates combine elapsed time since slot start with a rough per‑mode duration model (CV, LSV, EIS, CA/CP/OCP, etc.).  
- Error responses are uniform: `{ "code": "...", "message": "...", "hint": "..." }`.

### Windows
Core functionality works on Windows (serial I/O, jobs, file layout, plotting). NAS/SMB helpers are Linux‑specific because they rely on `mount`/CIFS and `rsync`.

---

## Development & Deployment

### Run with `systemd` (Linux, example)
```ini
# /etc/systemd/system/pybeep-box.service
[Unit]
Description=pyBEEP Potentiostat REST API
After=network-online.target

[Service]
Environment=BOX_API_KEY=change-me
Environment=BOX_ID=lab-box-01
Environment=RUNS_ROOT=/opt/box/runs
Environment=NAS_CONFIG_PATH=/opt/box/nas_smb.json
User=root
ExecStart=/usr/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pybeep-box.service
```

---

## Acknowledgements

- **pyBEEP** by Aurelien Blanc — driver and plotting utilities.
- Built with **FastAPI** and **Uvicorn**.

[pyBEEP]: https://github.com/aurelienblanc2/Potentiostat-driver-pyBEEP
