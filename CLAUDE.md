# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Health monitor for the 3CX PBX server — a Debian 12 virtual machine (Hyper-V, 4 vCPUs, 4 GB RAM) running the 3CX phone system. The server crashed 4+ times due to a Hyper-V NO_HZ timer bug (RCU preempt kthread starvation). Fix applied 2026-03-18: `nohz=off` kernel boot parameter. This module exists to confirm stability and catch any recurrence early.

Files:
- `index.html` — browser dashboard; served at `/syshealth/` by the top-level server. Auto-refreshes every 30s. Shows status banner, services grid, kernel issues, boot history, resource bars (CPU/memory/disk).
- `cron_check.py` — daily cron script (07:00); fetches stats from the API, sends an alert email if `overall_status != "ok"` or the server is unreachable. Sends a "resolved" email once on recovery. State tracked in `syshealth_state.json`.
- `syshealth_state.json` — persists `last_status` and `last_check` between cron runs (auto-managed).
- `cron.log` — stdout/stderr from `cron_check.py` (auto-created).

## Running

This project is served as a sub-project of [command-central](https://github.com/casperbudtz/command-central). Start the top-level server:

```bash
python3 /path/to/command-central/server.py
# Dashboard at: http://localhost:8080/syshealth/
```

The `server.py` proxy route (`GET /syshealth/api/stats`) fetches from the remote API:
```
http://192.168.1.7:8082/api/stats
```

The cron script can also be run manually:
```bash
python3 cron_check.py   # fetch current status and send email if needed
```

## Stats API

The remote agent at `192.168.1.7:8082` exposes `GET /api/stats` — see `syshealth-ai-context.md` in the parent repo for the full JSON field reference.

Key fields:

| Field | Meaning |
|---|---|
| `overall_status` | `"ok"` / `"warning"` / `"critical"` |
| `services` | map of service name → systemd state; expected `"active"` for all |
| `services_all_ok` | `true` if all services are active |
| `kernel_issues` | list of raw kernel log lines matching RCU stall / OOM / BUG patterns |
| `kernel_issues_count` | length of `kernel_issues` (0 = clean) |
| `system.vcpus` | should be 4 after the fix |
| `boot_history` | up to 10 boots, oldest-first; key fields: `end_type`, `duration_seconds`, `duration_human`, `gap_to_next_seconds`, `clean_shutdown` |
| `memory.swap_used_mb` | should be 0; non-zero = RAM pressure |

## Network

| IP | Port | Description |
|----|------|-------------|
| `192.168.1.7` | `8082` | 3CX server stats API |

## Key Services

| Service | Role |
|---|---|
| `3CXPhoneSystem01` | Core SIP server — handles all calls |
| `3CXMediaServer` | Audio/RTP relay |
| `postgresql@15-main` | Database — all services depend on this |
| `nginx` | HTTPS reverse proxy (web client, management) |
| `3CXCallFlow01` | Call routing / dial plan |
| `3CXIVR01` | IVR / auto-attendant |
| `3CXQueueManager01` | Call queue management |
| `3CXGatewayService` | SIP trunk gateway |

If `postgresql` or `3CXPhoneSystem01` are down, the entire phone system is non-functional.

## Email Notifications

`cron_check.py` reads SMTP config from `../email_config.json` (shared with the rest of Command Central). The `SENDER_NAME` constant at the top of the script sets the From display name (`"CC: 3CX Server Monitor"`).

- Sends an **alert** email every day `overall_status != "ok"` (including unreachable).
- Sends a **resolved** email once when status returns to `"ok"`.
- No email sent when status is `"ok"` and was `"ok"` on the previous run.

## Cron Job

Installed at `0 7 * * *` (07:00 daily). To re-install:

```bash
crontab -e
# Add:
0 7 * * * /usr/bin/python3 /home/casper/Documents/Code/SysHealth/cron_check.py >> /home/casper/Documents/Code/SysHealth/cron.log 2>&1
```

## What to Watch For

| Condition | Meaning |
|---|---|
| `overall_status = "critical"` | Service down or kernel issue — phone system may be impaired |
| `kernel_issues` contains "rcu.*stall" or "RCU.*starved" | Same issue that caused the crashes — escalate |
| `system.vcpus < 4` | vCPU count dropped — Hyper-V config change |
| `boot_history[-2].end_type = "crash"` | Previous boot crashed (array is oldest-first; -1 = current, -2 = previous) |
| `memory.swap_used_mb > 0` | RAM pressure |
| API unreachable | Server may be down / crashed |
