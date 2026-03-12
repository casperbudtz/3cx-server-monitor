# 3CX Server Monitor

A health monitoring dashboard and daily alert cron for a **3CX PBX server** running on Debian 12 (Hyper-V virtual machine). Part of [Command Central](https://github.com/casperbudtz/command-central).

## What it monitors

- **Systemd services** — all 3CX services plus PostgreSQL and nginx; alerts on anything not `active`
- **Kernel issues** — scans the kernel log for RCU stalls, OOM kills, and BUG traces from the current boot
- **Boot history** — flags unexpectedly short previous boots (< 1 hour) that indicate a crash
- **Memory / CPU / disk** — resource bars with warning thresholds
- **Overall status** — `ok` / `warning` / `critical` / `unreachable` rolled up by the remote stats agent

## Files

| File | Description |
|---|---|
| `index.html` | Browser dashboard — auto-refreshes every 30s |
| `cron_check.py` | Daily cron script — fetches stats, sends alert/resolved emails |
| `syshealth_state.json` | Persists `last_status` between runs (auto-managed) |
| `cron.log` | Cron stdout/stderr (auto-created) |
| `CLAUDE.md` | AI context for Claude Code |

## Usage

Served as a submodule of Command Central. Start the top-level server:

```bash
python3 /path/to/command-central/server.py
# Dashboard: http://localhost:8080/syshealth/
```

Stats are proxied from the remote agent at `http://192.168.1.7:8082/api/stats`.

The cron script can be run manually:

```bash
python3 cron_check.py
```

## Cron setup

```bash
crontab -e
# Add:
0 7 * * * /usr/bin/python3 /home/casper/Documents/Code/SysHealth/cron_check.py >> /home/casper/Documents/Code/SysHealth/cron.log 2>&1
```

## Email notifications

Reads SMTP config from `../email_config.json` (shared with Command Central).

- **Alert** — sent every day `overall_status != "ok"` or the server is unreachable
- **Resolved** — sent once when status returns to `ok`

## Background

The monitored server crashed twice on 2026-03-12 due to **RCU preempt kthread starvation** — a known Linux/Hyper-V issue where the kernel's grace-period thread is starved of CPU time because Hyper-V stops delivering timer interrupts to a tickless-idle vCPU. The fix (upgrading from 2 to 4 vCPUs) was applied; this monitor confirms stability and catches any recurrence.
