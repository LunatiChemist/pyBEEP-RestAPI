pyBEEP REST API (FastAPI)

A lightweight REST API to control pyBEEP-compatible potentiostats in a headless environment.
It allows you to run electrochemical experiments, store data in structured directories, optionally generate plots, and automatically upload finished runs to a NAS (SMB/CIFS) share.

Optimized for Linux (uses mount, umount, rsync)

Also runs on Windows (NAS upload not available there)

Implemented with FastAPI and Pydantic, supporting multiple devices, experiment sequencing, progress tracking, and data management. 

app

Table of Contents

Features

Requirements

Installation & Startup

Configuration (ENV)

Devices & Modes

Starting Jobs

Job Status & Cancellation

File Access

NAS/SMB Upload

Admin

Directory Structure

Security

API Overview

Features

Automatic device discovery (slots: slot01, slot02, …) 

app

Dynamic mode and parameter validation via /modes/{mode}/validate

Multi-mode sequences per slot (e.g. CV → EIS) with parallel slot handling 

app

Optional plots (.png) generated automatically 

app

Progress tracking with estimated time remaining 

progress_utils

File API to list, download, and zip entire runs 

app

NAS/SMB integration for automatic uploads and local retention cleanup 

nas_smb

Requirements

Python 3.10+

Linux system tools: mount, umount, rsync (for NAS upload)

Python packages:

fastapi, uvicorn, pydantic

pyserial, pyBEEP, and pyBEEP.plotter 

app

On Windows, NAS/SMB upload features are disabled, but the API and local storage still work. 

nas_smb

Installation & Startup
# 1) (optional) Create virtual environment
python -m venv .venv && source .venv/bin/activate

# 2) Install dependencies
pip install fastapi uvicorn pydantic pyserial pyBEEP

# 3) Set environment variables and start
export BOX_API_KEY="my-secret-key"
uvicorn app:app --host 0.0.0.0 --port 8000


Check API version and build info:

curl http://localhost:8000/version


app

Configuration (ENV)
Variable	Description	Default
BOX_API_KEY	API key required in header X-API-Key (empty = no protection, not recommended)	""
BOX_ID	Identifier for this device (returned by /health)	""
RUNS_ROOT	Root directory for experiment runs	/opt/box/runs
NAS_CONFIG_PATH	Path to SMB config file (used by NAS manager)	/opt/box/nas_smb.json

Directories are automatically created if missing.

Devices & Modes

GET /health → { ok, devices, box_id } 

app

GET /devices → List of connected potentiostat slots 

app

GET /modes → Available measurement modes 

app

GET /modes/{mode}/params → Parameter schema for given mode 

app

POST /modes/{mode}/validate → Validate and auto-adjust parameters 

validation

Examples

curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/devices
curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/modes/CV/params

curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $BOX_API_KEY" \
  -d '{"start":0.0,"vertex1":0.5,"vertex2":-0.5,"end":0.0,"scan_rate":0.05,"cycles":3}' \
  http://localhost:8000/modes/CV/validate

Starting Jobs

Each job defines one or more modes to run per slot.
All data and plots are stored in structured folders under RUNS_ROOT. 

app

Endpoint: POST /jobs
Headers: X-API-Key: <key>
Body (JobRequest): 

app

{
  "devices": ["slot01", "slot02"],
  "modes": ["CV", "EIS"],
  "params_by_mode": {
    "CV":  { "start":0.0, "vertex1":0.5, "vertex2":-0.5, "end":0.0, "scan_rate":0.05, "cycles":3 },
    "EIS": { "start_freq":1000, "end_freq":1, "points_per_decade":10, "cycles_per_freq":3, "spacing":"log" }
  },
  "tia_gain": 0,
  "sampling_interval": null,
  "experiment_name": "MyExperiment",
  "subdir": "GroupA",
  "client_datetime": "2025-11-03T10-15-31",
  "run_name": null,
  "folder_name": null,
  "make_plot": true
}


The response includes:

run_id

slots[]

status, current_mode, remaining_modes

progress_pct and remaining_s (estimated time left) 

progress_utils

Validation rules:

modes cannot be empty

params_by_mode must include all listed modes

Devices must exist and be free (409 if in use) 

app

Job Status & Cancellation
Endpoint	Description
GET /jobs/{run_id}	Current job status and progress 

app


POST /jobs/status	Batch query multiple jobs 

app


GET /jobs	List jobs (filter by state or group_id) 

app


POST /jobs/{run_id}/cancel	Cancel a running or queued job 

app

Queued jobs are marked as cancelled immediately; running jobs attempt to abort gracefully.

File Access
Endpoint	Description
GET /runs/{run_id}/files	List all files of a run 

app


GET /runs/{run_id}/file?path=...	Download a single file (safe path validation) 

app


GET /runs/{run_id}/zip	Download a ZIP archive of the entire run 

app

NAS/SMB Upload

Integrated support for NAS uploads via SMB/CIFS. 

nas_smb

Setup

curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $BOX_API_KEY" \
  -d '{"host":"nas.local","share":"experiments","username":"lab","password":"***","base_subdir":"Box01","retention_days":14}' \
  http://localhost:8000/nas/setup


Creates credentials at /opt/box/.smbcredentials_nas (0600)

Writes NAS config to NAS_CONFIG_PATH

Mounts the share and verifies write access

Health Check

curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/nas/health


Returns { ok, last_checked, message } with read-only mount verification. 

nas_smb

Run Upload

Automatically triggered when a run finishes (done)

Manual upload: POST /runs/{run_id}/upload

Upload uses rsync; upon success, a UPLOAD_DONE marker is written.

Local Retention

Old runs are deleted locally when UPLOAD_DONE exists and age exceeds retention_days. 

nas_smb

Mount Details: SMB v3, credentials file, current UID/GID, noserverino option.
Read-only for health check, read/write for uploads. 

nas_smb

Admin

POST /admin/rescan → Rediscover connected devices 

app

Directory Structure

Each run is stored under RUNS_ROOT/<experiment>/<subdir?>/<timestamp_dir>/.
Per slot and mode:

MyExperiment/
  slot01/
    CV/
      MyExperiment_slot01_CV.csv
      MyExperiment_slot01_CV.png
    EIS/
      MyExperiment_slot01_EIS.csv
  slot02/
    ...


client_datetime and names are sanitized for file safety.

Each run has a unique run_id and corresponding path mapping persisted in _run_paths.json. 

storage

Security

API Key: All critical routes require X-API-Key if BOX_API_KEY is set. 

app

Safe Paths: File endpoints enforce strict path checks (no directory traversal). 

app

API Overview
Method & Path	Description
GET /version	API, pyBEEP, Python version, build info
GET /health	Basic system health + device count
GET /devices	List detected slots
GET /modes	Available measurement modes
GET /modes/{mode}/params	Parameter schema
POST /modes/{mode}/validate	Validate mode parameters
POST /jobs	Start a measurement job
GET /jobs	List or filter jobs
POST /jobs/status	Batch job status
GET /jobs/{run_id}	Single job status
POST /jobs/{run_id}/cancel	Cancel job
GET /runs/{run_id}/files	List files in run
GET /runs/{run_id}/file	Download a file
GET /runs/{run_id}/zip	Download run as ZIP
POST /nas/setup	Configure NAS/SMB access
GET /nas/health	NAS connection health check
POST /runs/{run_id}/upload	Trigger upload to NAS
POST /admin/rescan	Re-scan available devices

(All features and validation rules are directly derived from the source code.)

Examples

Start a job (CV → EIS on all slots):

curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $BOX_API_KEY" \
  -d '{
    "devices":"all",
    "modes":["CV","EIS"],
    "params_by_mode":{
      "CV":{"start":0.0,"vertex1":0.5,"vertex2":-0.5,"end":0.0,"scan_rate":0.05,"cycles":3},
      "EIS":{"start_freq":1000,"end_freq":1,"points_per_decade":10,"cycles_per_freq":3,"spacing":"log"}
    },
    "experiment_name":"Demo",
    "subdir":"BatchA",
    "client_datetime":"2025-11-03T10-15-31",
    "make_plot":true
  }' \
  http://localhost:8000/jobs


Check job progress:

curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/jobs/<run_id>


Download ZIP of results:

curl -H "X-API-Key: $BOX_API_KEY" -L -o run.zip http://localhost:8000/runs/<run_id>/zip


NAS Setup & Health Check:

curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $BOX_API_KEY" \
  -d '{"host":"nas.local","share":"experiments","username":"lab","password":"***","base_subdir":"Box01","retention_days":14}' \
  http://localhost:8000/nas/setup

curl -H "X-API-Key: $BOX_API_KEY" http://localhost:8000/nas/health

License

MIT License (or replace with your preferred license)
