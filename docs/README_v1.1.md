# SG200 / Netgear Collector for Forescout Connect

This project provides a lightweight **HTTP collector service** that Forescout Connect apps can call to inventory network devices.

Primary use cases:
- **Cisco SG200**: Collect the dynamic MAC address table (MAC → VLAN → Interface), plus system identity details (serial, model, firmware).
- **Netgear** (optional): Collect access-control/device list data (where supported by the bundled scraper).

The collector is designed to run **headlessly** on Windows or Linux and is commonly deployed as a **Windows Service**.

## Key capabilities

Cisco SG200:
- Dynamic MAC table discovery via Playwright/Chromium.
- **Interface names** are returned as the switch UI labels (e.g., `GE1`, `GE2`, …), not raw port indices.
- System summary includes:
  - `host_name`
  - `serial_number`
  - `firmware_version` (or `"N/A"` if unavailable)
  - `model_description` (or `"N/A"` if unavailable)
- Uptime is intentionally not collected or reported.
- No debug directories or page-dumps are written to disk during scraping.

Security controls (collector-side):
- Optional **IP allowlist**
- Optional **shared token** (cleartext token or SHA-256 hash in a local config file)

## Components

- **Collector service**: Flask application exposing REST endpoints (see “API”).
- **Scrapers**:
  - `sg200_client.py` (Playwright-based SG200 scraper)
  - `netgear_client.py` (HTTP-based Netgear scraper, if applicable)
- **Forescout Connect apps** (separate packages): Configure the app to call the collector on an interval.

---

## System requirements

Windows (recommended deployment target):
- Windows 10/11 or Windows Server 2019+ (Server 2022+ preferred)
- Python 3.10+ (3.11 is fine)
- Network reachability from collector host to:
  - SG200 management UI (HTTP/HTTPS per your environment)
  - Netgear UI/API (if used)

Linux/macOS (supported for development/testing):
- Python 3.10+
- Ability to install Playwright Chromium dependencies

---

## Installation

### 1) Obtain the collector files

Extract the collector release zip to a stable directory. Recommended layout:

- `C:\SG200Collector\current\`
- `C:\SG200Collector\logs\`

Ensure the extracted directory contains:
- `collector.py`
- `scrapers\` (directory)
- optional: `requirements.txt`

### 2) Create a Python virtual environment and install dependencies

Open PowerShell:

```powershell
cd C:\SG200Collector\current
py -m venv venv
.\venv\Scripts\activate
python -m pip install --upgrade pip
```

Install dependencies:

If the release includes `requirements.txt`:
```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

Otherwise install the minimum set:
```powershell
pip install flask waitress playwright requests beautifulsoup4
python -m playwright install chromium
```

Notes:
- `waitress` is used as the production WSGI server on Windows.
- `python -m playwright install chromium` is required for the SG200 scraper.

---

## Configuration (security and runtime)

### File-based security configuration (recommended)

Create `collector_security.json` next to `collector.py`:

Example (cleartext token):
```json
{
  "allowed_ips": ["192.168.0.50", "192.168.0.60", "127.0.0.1"],
  "token": "replace-with-a-long-random-string"
}
```

Example (recommended: store only token hash):
```json
{
  "allowed_ips": ["192.168.0.50", "192.168.0.60", "127.0.0.1"],
  "token_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

Generate the SHA-256 token hash:
```powershell
python -c "import hashlib; print(hashlib.sha256(b'your-token-here').hexdigest())"
```

Important:
- The allowlist uses **exact IP matches** (no CIDR ranges unless you extend the code).
- The token is validated against the `X-Collector-Token` HTTP request header.

### Environment variable fallback (optional)

If `collector_security.json` is not present, the collector may read:
- `SG200_COLLECTOR_ALLOWED_IPS="ip1,ip2"`
- `SG200_COLLECTOR_TOKEN="shared-token"`
- `SG200_COLLECTOR_HOST="0.0.0.0"`
- `SG200_COLLECTOR_PORT="8081"`

---

## Run modes

### A) Interactive (development / first-run validation)

```powershell
cd C:\SG200Collector\current
.\venv\Scripts\activate
waitress-serve --host=127.0.0.1 --port=8081 collector:app
```

Test:
```powershell
curl http://127.0.0.1:8081/health
```

### B) Windows Service (recommended): NSSM + Waitress

This provides:
- Start on boot
- Manual start/stop
- Restart on failure
- Headless operation

#### 1) Install NSSM

Download `nssm.exe` and place it at:
- `C:\Tools\nssm.exe`

#### 2) Create a dedicated service account (recommended)

Playwright/Chromium is most reliable under a real user profile rather than `LocalSystem`.

- Create a local user, e.g. `sg200svc`
- Grant “Log on as a service”:
  - Local Security Policy → Local Policies → User Rights Assignment → Log on as a service
- NTFS permissions:
  - Read/Execute: `C:\SG200Collector\current\`
  - Modify: `C:\SG200Collector\logs\`
- Ensure the account can write to `%TEMP%` (normal for user accounts)

#### 3) Install the service

Run PowerShell as Administrator:

```powershell
C:\Tools\nssm.exe install SG200Collector
```

In the NSSM GUI, set:

**Application**
- Path: `C:\SG200Collector\current\venv\Scripts\python.exe`
- Startup directory: `C:\SG200Collector\current`
- Arguments:
  - `-m waitress --listen=0.0.0.0:8081 collector:app`

**I/O**
- Stdout: `C:\SG200Collector\logs\stdout.log`
- Stderr: `C:\SG200Collector\logs\stderr.log`

**Process**
- Enable “Kill process tree” (prevents orphaned Chromium processes)

Then set the service Log On account:
- Services → SG200Collector → Properties → Log On → `.\sg200svc`

Startup + Recovery:
- Startup type: Automatic
- Recovery tab:
  - First failure: Restart the Service
  - Second failure: Restart the Service
  - Subsequent failures: Restart the Service

Start the service:
```powershell
sc start SG200Collector
```

Stop the service:
```powershell
sc stop SG200Collector
```

#### 4) Windows Firewall

If the collector is accessed remotely, open inbound TCP 8081 and scope it to only the Forescout appliance IP(s).

---

## Monitoring and debugging (service mode)

### Live log tail (terminal-like)

```powershell
Get-Content C:\SG200Collector\logs\stdout.log -Tail 200 -Wait
```

Also check:
- `C:\SG200Collector\logs\stderr.log`

### Log retention (≤ 24 hours)

Recommended approach: Scheduled Task that deletes log files older than 24 hours.

Create a Scheduled Task that runs hourly (or daily) with this action:

```powershell
powershell.exe -NoProfile -Command ^
  "Get-ChildItem 'C:\SG200Collector\logs\*.log' -File | Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-24) } | Remove-Item -Force"
```

If you prefer keeping a longer retention window, adjust `AddHours(-24)`.

---

## API

Base URL: `http://<collector-host>:8081`

### `GET /health`

Returns:
```json
{"status":"ok"}
```

### `POST /sg200/mac-table`

Body:
```json
{
  "ip": "192.168.0.221",
  "user": "cisco",
  "pass": "cisco"
}
```

Example response:
```json
{
  "switch_ip": "192.168.0.221",
  "entries": [
    {
      "switch_ip": "192.168.0.221",
      "vlan": 1,
      "mac": "00:11:22:33:44:55",
      "port_index": "GE1"
    }
  ]
}
```

### `POST /sg200/system-summary`

Body:
```json
{
  "ip": "192.168.0.221",
  "user": "cisco",
  "pass": "cisco"
}
```

Example response:
```json
{
  "switch_ip": "192.168.0.221",
  "host_name": "GARAGE-SG200",
  "serial_number": "DNI161702F3",
  "firmware_version": "1.1.2.0",
  "model_description": "26-port Gigabit Smart Switch"
}
```

### `POST /netgear/access-control` (if enabled)

Body:
```json
{
  "ip": "192.168.1.7",
  "user": "admin",
  "pass": "password"
}
```

Example response:
```json
{
  "router_ip": "192.168.1.7",
  "entries": [
    {
      "router_ip": "192.168.1.7",
      "ip": "192.168.1.199",
      "mac": "00:0c:29:b2:94:c0",
      "status": "Allowed",
      "conn_type": "wired",
      "name": "DESKTOP-EXAMPLE"
    }
  ]
}
```

### Token usage (optional)

If you configured a token, include it as:

```bash
-H "X-Collector-Token: <your-token>"
```

Example:
```bash
curl -fsS -X POST "http://<collector-host>:8081/sg200/mac-table" \
  -H "Content-Type: application/json" \
  -H "X-Collector-Token: <your-token>" \
  -d '{"ip":"192.168.0.221","user":"cisco","pass":"cisco"}'
```

---

## Troubleshooting

### 1) Service starts but endpoints fail
- Check `stderr.log` for Playwright/Chromium errors.
- Ensure the service runs under a real user account (not LocalSystem).
- Confirm the service account can write to `%TEMP%` and `C:\SG200Collector\logs\`.

### 2) Requests are blocked unexpectedly
- If IP allowlist is enabled, verify the source IP the collector sees matches your allowlist.
- If token is enabled, verify `X-Collector-Token` is present and correct.

### 3) SG200 scraping fails intermittently
- Ensure stable management reachability and no interactive-login prompts.
- Reduce polling frequency if you are scraping multiple switches concurrently.

---

## Security notes

- The collector endpoints may receive switch credentials in request bodies. Treat the collector host as sensitive.
- If traffic crosses an untrusted network segment, protect the collector with:
  - network ACLs/firewall, and
  - TLS termination (reverse proxy) if required by your threat model.
- Restrict read access to `collector_security.json` to Administrators and the service account only.
