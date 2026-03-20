#!/usr/bin/env python3
"""
3CX Server Health Monitor — daily cron check + email notifications.

Fetches stats from the syshealth API on the 3CX server and sends an alert
email if overall_status is not "ok" (service down, kernel issue, etc.) or
if the server is unreachable. Sends a "resolved" email when it recovers.

State is persisted in syshealth_state.json so a resolved notification fires
exactly once on recovery.

Setup (run once):
    crontab -e
    # Add:
    0 7 * * * /usr/bin/python3 /home/casper/Documents/Code/SysHealth/cron_check.py >> /home/casper/Documents/Code/SysHealth/cron.log 2>&1

Requires: network access to 192.168.1.7:8082
"""

import json
import os
import smtplib
import sys
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
STATS_URL        = "http://192.168.1.7:8082/api/stats"
EMAIL_CONFIG_FILE = Path(SCRIPT_DIR).parent / "email_config.json"
STATE_FILE       = Path(SCRIPT_DIR) / "syshealth_state.json"
SENDER_NAME      = "CC: 3CX Server Monitor"
REQUEST_TIMEOUT  = 15  # seconds

_EMAIL_DEFAULTS = {
    "host": "", "port": 25, "from_email": "", "admin_email": "",
}

_STATE_DEFAULTS = {
    "last_status": "ok",
    "last_check":  None,
}


def _load_json(path, defaults):
    try:
        with open(path) as f:
            return {**defaults, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(defaults)


def _save_state(state):
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, str(STATE_FILE))


def _smtp_send(cfg, msg):
    with smtplib.SMTP(cfg["host"], int(cfg["port"])) as s:
        s.send_message(msg)


def _fetch_stats():
    """Fetch stats from the API. Returns (data_dict, error_string)."""
    try:
        with urllib.request.urlopen(STATS_URL, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)


def _issues_summary(data):
    """Return a short plain-text summary of what's wrong."""
    parts = []
    if (data.get("kernel_issues_count") or 0) > 0:
        parts.append(f"{data['kernel_issues_count']} kernel issue(s)")
    if data.get("services_all_ok") is False:
        down = [k for k, v in (data.get("services") or {}).items() if v != "active"]
        parts.append(f"{len(down)} service(s) down: {', '.join(down)}")
    mem = data.get("memory") or {}
    if mem.get("used_pct", 0) > 85:
        parts.append(f"Memory {mem['used_pct']:.1f}%")
    if mem.get("swap_used_mb", 0) > 0:
        parts.append(f"Swap in use: {mem['swap_used_mb']} MB")
    sys_info = data.get("system") or {}
    vcpus = sys_info.get("vcpus", 4)
    if data.get("system", {}).get("load_1m", 0) > vcpus:
        parts.append(f"Load {data['system']['load_1m']:.1f} > {vcpus} vCPUs")
    boot_history = data.get("boot_history") or []
    if len(boot_history) > 1:
        prev = boot_history[-2]  # previous boot (oldest-first array)
        if prev.get("end_type") == "crash":
            dur = prev.get("duration_human") or ""
            parts.append(f"previous boot crashed ({dur})".strip())
    return parts


def _send_alert_email(email_cfg, data, status):
    """Send an alert email when the server is non-ok or unreachable."""
    summary_parts = [] if data is None else _issues_summary(data)
    summary_brief = ", ".join(summary_parts) if summary_parts else status.capitalize()

    subject = f"3CX Server Alert — {status.capitalize()}: {summary_brief}"
    hostname = (data or {}).get("hostname", "3CX server")

    # ── Plain-text ────────────────────────────────────────────────────────────
    text_lines = [f"3CX Server Health Alert\n{'='*40}\n"]
    text_lines.append(f"Status:  {status.upper()}")
    text_lines.append(f"Host:    {hostname}")
    text_lines.append(f"Time:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if data is None:
        text_lines.append("The server is UNREACHABLE — the stats API did not respond.")
        text_lines.append("This may indicate the server has crashed or lost network.\n")
    else:
        if summary_parts:
            text_lines.append("Issues detected:")
            for p in summary_parts:
                text_lines.append(f"  • {p}")
            text_lines.append("")

        # Services
        services = data.get("services") or {}
        down = [(k, v) for k, v in services.items() if v != "active"]
        if down:
            text_lines.append("Services not active:")
            for name, state in down:
                text_lines.append(f"  • {name}: {state}")
            text_lines.append("")

        # Kernel issues
        if data.get("kernel_issues_count", 0) > 0:
            text_lines.append("Kernel issues (raw log lines):")
            for line in data.get("kernel_issues", []):
                text_lines.append(f"  {line}")
            text_lines.append("")

        # Boot history — flag crashes or suspiciously short previous boots
        boot_history = data.get("boot_history") or []
        if len(boot_history) > 1:
            prev = boot_history[-2]  # previous boot (array is oldest-first; -1 = current)
            end_type = prev.get("end_type", "unknown")
            dur_human = prev.get("duration_human") or f"{int((prev.get('duration_seconds') or 0) // 60)}m"
            if end_type == "crash":
                gap_sec = prev.get("gap_to_next_seconds")
                gap_note = f", gap to next boot: {int(gap_sec // 60)}m" if gap_sec is not None else ""
                text_lines.append(
                    f"Previous boot ended in a CRASH (duration: {dur_human}, "
                    f"started {prev.get('first','?')}, ended {prev.get('last','?')}{gap_note}).\n"
                )
            elif end_type not in ("reboot", "halt", "current") and (prev.get("duration_seconds") or 0) < 3600:
                text_lines.append(
                    f"Previous boot was short: {dur_human} "
                    f"(started {prev.get('first','?')}, ended {prev.get('last','?')}) — possible crash.\n"
                )

        # Memory / load summary
        mem = data.get("memory") or {}
        sys_info = data.get("system") or {}
        text_lines.append(
            f"System:  {sys_info.get('vcpus','?')} vCPUs  |  "
            f"Load {sys_info.get('load_1m','?'):.2g} / {sys_info.get('load_5m','?'):.2g} / {sys_info.get('load_15m','?'):.2g}  |  "
            f"Memory {mem.get('used_pct',0):.1f}%  |  "
            f"Uptime {sys_info.get('uptime_human','?')}"
        )

    text_lines.append("\n---\nCommand Central — 3CX Server Monitor")
    text_body = "\n".join(text_lines)

    # ── HTML ──────────────────────────────────────────────────────────────────
    STATUS_COLORS = {
        "warning":     {"bg": "#fef3c7", "border": "#fcd34d", "text": "#92400e"},
        "critical":    {"bg": "#fee2e2", "border": "#fca5a5", "text": "#991b1b"},
        "unreachable": {"bg": "#f3f4f6", "border": "#d1d5db", "text": "#374151"},
    }
    colors = STATUS_COLORS.get(status, STATUS_COLORS["critical"])

    issues_html = ""
    if data is None:
        issues_html = """
        <tr><td colspan="2" style="padding:12px 16px;color:#991b1b;font-weight:600">
          Server is UNREACHABLE — stats API did not respond.<br>
          <span style="font-weight:400;font-size:.85rem">The server may have crashed or lost network connectivity.</span>
        </td></tr>"""
    else:
        services = data.get("services") or {}
        down_svcs = [(k, v) for k, v in sorted(services.items()) if v != "active"]
        for name, state in down_svcs:
            issues_html += f"""
        <tr>
          <td style="padding:8px 16px;border-bottom:1px solid #f3f4f6;font-family:monospace;font-size:.82rem">{name}</td>
          <td style="padding:8px 16px;border-bottom:1px solid #f3f4f6;color:#dc2626;font-weight:700">{state}</td>
        </tr>"""

        kernel_issues = data.get("kernel_issues") or []
        if kernel_issues:
            issues_html += """
        <tr><td colspan="2" style="padding:10px 16px 4px;background:#fef2f2;font-weight:700;
             font-size:.82rem;color:#991b1b;border-top:2px solid #fecaca">
          Kernel Issues</td></tr>"""
            for line in kernel_issues:
                issues_html += f"""
        <tr><td colspan="2" style="padding:6px 16px;border-bottom:1px solid #f3f4f6;
             font-family:monospace;font-size:.72rem;color:#991b1b;word-break:break-all">{line}</td></tr>"""

    mem = (data or {}).get("memory") or {}
    sys_info = (data or {}).get("system") or {}
    mem_pct  = mem.get("used_pct", 0)
    load_1m  = sys_info.get("load_1m", "?")
    vcpus    = sys_info.get("vcpus", "?")
    uptime   = sys_info.get("uptime_human", "?")
    load_color = "#dc2626" if isinstance(load_1m, (int, float)) and isinstance(vcpus, int) and load_1m > vcpus else "#1a1a2e"
    mem_color  = "#dc2626" if mem_pct > 85 else "#1a1a2e"
    load_str   = f"{load_1m:.2g}" if isinstance(load_1m, (int, float)) else str(load_1m)

    stats_row = "" if data is None else f"""
      <div style="display:flex;gap:24px;flex-wrap:wrap;padding:12px 16px;
                  background:#f8f9fa;border-top:1px solid #e0e4ea;font-size:.8rem;color:#6b7280">
        <span>vCPUs: <strong style="color:#1a1a2e">{vcpus}</strong></span>
        <span>Load: <strong style="color:{load_color}">{load_str}</strong></span>
        <span>Memory: <strong style="color:{mem_color}">{mem_pct:.1f}%</strong></span>
        <span>Swap: <strong style="color:{'#dc2626' if mem.get('swap_used_mb',0)>0 else '#1a1a2e'}">{mem.get('swap_used_mb',0)} MB</strong></span>
        <span>Uptime: <strong style="color:#1a1a2e">{uptime}</strong></span>
      </div>"""

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#f0f2f5;margin:0;padding:24px;color:#1a1a2e">
  <div style="max-width:620px;margin:0 auto">
    <div style="background:#1a1a2e;color:#fff;padding:16px 20px;border-radius:10px 10px 0 0">
      <div style="font-size:1rem;font-weight:700">3CX Server Alert</div>
      <div style="font-size:.78rem;opacity:.6;margin-top:2px">{hostname} — {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
    </div>
    <div style="background:{colors['bg']};border:1px solid {colors['border']};
                border-top:none;padding:14px 16px">
      <div style="font-size:1rem;font-weight:700;color:{colors['text']}">{status.upper()}: {summary_brief}</div>
    </div>
    <div style="background:#fff;border:1px solid #e0e4ea;border-top:none;
                border-radius:0 0 10px 10px;overflow:hidden">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:.85rem">
        <tbody>{issues_html}</tbody>
      </table>
      {stats_row}
      <div style="padding:12px 16px;font-size:.75rem;color:#6b7280;
                  border-top:1px solid #f3f4f6;background:#f8f9fa">
        Command Central — 3CX Server Monitor
      </div>
    </div>
  </div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((SENDER_NAME, email_cfg["from_email"]))
    msg["To"]      = email_cfg["admin_email"]
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    _smtp_send(email_cfg, msg)


def _send_resolved_email(email_cfg, data):
    """Send an all-clear email when the server recovers."""
    sys_info = (data or {}).get("system") or {}
    hostname = (data or {}).get("hostname", "3CX server")
    uptime   = sys_info.get("uptime_human", "?")

    subject = f"3CX Server — All Clear ({hostname})"
    text_body = (
        f"3CX Server is back to normal.\n\n"
        f"Host:   {hostname}\n"
        f"Uptime: {uptime}\n"
        f"Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"All services active, no kernel issues detected.\n\n"
        f"---\nCommand Central — 3CX Server Monitor"
    )
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#f0f2f5;margin:0;padding:24px;color:#1a1a2e">
  <div style="max-width:480px;margin:0 auto">
    <div style="background:#1a1a2e;color:#fff;padding:16px 20px;border-radius:10px 10px 0 0">
      <div style="font-size:1rem;font-weight:700">3CX Server — All Clear</div>
      <div style="font-size:.78rem;opacity:.6;margin-top:2px">{hostname}</div>
    </div>
    <div style="background:#d1fae5;border:1px solid #6ee7b7;border-top:none;padding:14px 16px">
      <div style="font-size:1rem;font-weight:700;color:#065f46">All systems operational</div>
    </div>
    <div style="background:#fff;border:1px solid #e0e4ea;border-top:none;
                border-radius:0 0 10px 10px;padding:16px;font-size:.85rem">
      <p>All services are active and no kernel issues were detected.</p>
      <p style="margin-top:8px;color:#6b7280">Uptime: {uptime} · Checked {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
      <p style="margin-top:16px;font-size:.75rem;color:#9ca3af">Command Central — 3CX Server Monitor</p>
    </div>
  </div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((SENDER_NAME, email_cfg["from_email"]))
    msg["To"]      = email_cfg["admin_email"]
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    _smtp_send(email_cfg, msg)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    email_cfg = _load_json(EMAIL_CONFIG_FILE, _EMAIL_DEFAULTS)
    if not email_cfg["host"] or not email_cfg["admin_email"]:
        print(f"[{ts}] SKIP  SMTP not configured", file=sys.stderr)
        return 0

    state = _load_json(STATE_FILE, _STATE_DEFAULTS)

    data, fetch_error = _fetch_stats()

    if fetch_error:
        current_status = "unreachable"
        print(f"[{ts}] ERROR  API unreachable: {fetch_error}", file=sys.stderr)
    else:
        current_status = data.get("overall_status", "unknown")
        issues = _issues_summary(data)
        print(f"[{ts}] OK     status={current_status} hostname={data.get('hostname','')} "
              f"uptime={data.get('system',{}).get('uptime_human','?')}"
              + (f" | issues: {'; '.join(issues)}" if issues else ""))

    last_status = state.get("last_status", "ok")
    sent_type   = None

    if current_status != "ok":
        # Send alert every day the server is not ok
        try:
            _send_alert_email(email_cfg, data, current_status)
            sent_type = "alert"
            print(f"[{ts}] NOTIFY  Alert email sent (status={current_status})")
        except Exception as e:
            print(f"[{ts}] NOTIFY  Failed to send alert: {e}", file=sys.stderr)
    elif current_status == "ok" and last_status != "ok":
        # Resolved — send once on recovery
        try:
            _send_resolved_email(email_cfg, data)
            sent_type = "resolved"
            print(f"[{ts}] NOTIFY  Resolved email sent")
        except Exception as e:
            print(f"[{ts}] NOTIFY  Failed to send resolved email: {e}", file=sys.stderr)
    else:
        print(f"[{ts}] NOTIFY  No email needed (status={current_status}, last={last_status})")

    new_state = {
        "last_status": current_status,
        "last_check":  ts,
    }
    _save_state(new_state)
    return 0 if current_status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
