"""Install / uninstall / inspect the nightly LaunchAgent (D2).

  python scripts/schedule.py install     # write ~/Library/LaunchAgents/com.jobcopilot.daily.plist + bootstrap
  python scripts/schedule.py uninstall   # bootout + remove the plist
  python scripts/schedule.py status      # installed? loaded? next run, last run (runs table)
  python scripts/schedule.py render      # print the plist that install would write

The time comes from config/schedule.yaml (`daily_run`, in `timezone`) and is
converted to this Mac's local time, which is what launchd uses. DST moves that
conversion during the year, so the plist holds EVERY local time the slot maps
to (usually one, two across a DST change) and the job is started with
`--scheduled`: jobcopilot.daily then exits early unless the current time in
the schedule tz is inside the slot and no run already happened in it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LABEL = "com.jobcopilot.daily"
TEMPLATE = REPO / "scripts" / f"{LABEL}.plist.template"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def parse_hhmm(value: str) -> tuple[int, int]:
    h, _, m = str(value).strip().partition(":")
    hour, minute = int(h), int(m or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"daily_run must be HH:MM, got {value!r}")
    return hour, minute


def local_run_time(daily_run: str, tzname: Optional[str]) -> tuple[int, int]:
    """daily_run in `tzname` -> (hour, minute) in this machine's local zone."""
    hour, minute = parse_hhmm(daily_run)
    if not tzname:
        return hour, minute
    try:
        from zoneinfo import ZoneInfo
        src = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001 — unknown zone: take the time as local
        return hour, minute
    today = dt.datetime.now(src).date()
    local = dt.datetime.combine(today, dt.time(hour, minute), src).astimezone()
    return local.hour, local.minute


def local_run_times(daily_run: str, tzname: Optional[str], year_start: Optional[dt.date] = None,
                    local_tz: Any = None) -> list[tuple[int, int]]:
    """Every local (hour, minute) the slot maps to over the next year (DST),
    sorted. Sampled daily, so any DST rule on either side is covered."""
    hour, minute = parse_hhmm(daily_run)
    if not tzname:
        return [(hour, minute)]
    try:
        from zoneinfo import ZoneInfo
        src = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001
        return [(hour, minute)]
    start = year_start or dt.datetime.now(src).date()
    seen = set()
    for d in range(0, 366):
        day = start + dt.timedelta(days=d)
        loc = dt.datetime.combine(day, dt.time(hour, minute), src).astimezone(local_tz)
        seen.add((loc.hour, loc.minute))
    return sorted(seen)


def zone_mismatch_warning(tzname: Optional[str], local_tz: Any = None,
                          year_start: Optional[dt.date] = None) -> Optional[str]:
    """A warning when this machine's zone isn't the schedule zone (offsets differ
    on any day of the coming year)."""
    if not tzname:
        return None
    try:
        from zoneinfo import ZoneInfo
        src = ZoneInfo(tzname)
    except Exception:  # noqa: BLE001
        return f"unknown schedule timezone {tzname!r}; using local time"
    start = year_start or dt.datetime.now(src).date()
    for d in range(0, 366, 1):
        t = dt.datetime.combine(start + dt.timedelta(days=d), dt.time(12), src)
        if t.utcoffset() != t.astimezone(local_tz).utcoffset():
            return (f"this machine's time zone differs from the schedule's ({tzname}); launchd fires "
                    f"in local time, so every local time the slot maps to is installed and the run "
                    f"itself checks the {tzname} slot")
    return None


def _calendar_xml(times: list[tuple[int, int]]) -> str:
    return "\n".join(f"    <dict><key>Hour</key><integer>{int(h)}</integer>"
                     f"<key>Minute</key><integer>{int(m)}</integer></dict>" for h, m in times)


def render_plist(*, hour: Optional[int] = None, minute: Optional[int] = None,
                 times: Optional[list[tuple[int, int]]] = None, repo: Path = REPO,
                 python: Optional[Path] = None, log_dir: Optional[Path] = None,
                 template: Path = TEMPLATE) -> bytes:
    """Fill the template; the result is validated by plistlib before it is returned.
    `times` = every local (hour, minute) to fire at; or one `hour` + `minute`."""
    times = list(times) if times else [(int(hour), int(minute))]
    for h, m in times:
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError(f"bad run time {h}:{m}")
    repo = Path(repo).resolve()
    python = Path(python) if python else repo / ".venv" / "bin" / "python"
    log_dir = Path(log_dir) if log_dir else repo / "data" / "logs"
    for p in (repo, python, log_dir):
        if not p.is_absolute():
            raise ValueError(f"path must be absolute: {p}")
    values = {"LABEL": LABEL, "PYTHON": str(python), "REPO": str(repo),
              "LOG_DIR": str(log_dir)}
    text = template.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", escape(v))
    text = text.replace("{{CALENDAR}}", _calendar_xml(times))
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise ValueError(f"unfilled placeholder(s) in plist template: {left}")
    data = text.encode("utf-8")
    plistlib.loads(data)   # raises on malformed output
    return data


def config_plist(**kw: Any) -> bytes:
    from jobcopilot import config
    sched = config.schedule()
    times = local_run_times(sched.get("daily_run") or "19:00", sched.get("timezone"))
    return render_plist(times=times, **kw)


def _times(p: dict) -> list[tuple[int, int]]:
    cal = p["StartCalendarInterval"]
    cal = cal if isinstance(cal, list) else [cal]
    return [(int(c["Hour"]), int(c.get("Minute", 0))) for c in cal]


def next_run(hour: int, minute: int, now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or dt.datetime.now()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return nxt if nxt > now else nxt + dt.timedelta(days=1)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> int:
    from jobcopilot import config
    warn = zone_mismatch_warning(config.schedule().get("timezone"))
    if warn:
        print(f"warning: {warn}", file=sys.stderr)
    data = config_plist()
    (REPO / "data" / "logs").mkdir(parents=True, exist_ok=True)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _launchctl("bootout", _domain(), str(target))
    target.write_bytes(data)
    r = _launchctl("bootstrap", _domain(), str(target))
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    when = ", ".join(f"{h:02d}:{m:02d}" for h, m in _times(plistlib.loads(data)))
    print(f"installed {target}; fires daily at {when} local time (runs once, inside the slot)")
    print(f"run it now with: launchctl kickstart -k {_domain()}/{LABEL}")
    return 0


def uninstall() -> int:
    target = plist_path()
    if target.exists():
        _launchctl("bootout", _domain(), str(target))
        target.unlink()
        print(f"removed {target}")
    else:
        print("not installed")
    return 0


def status() -> int:
    target = plist_path()
    installed = target.exists()
    loaded = installed and _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
    print(f"plist:  {target} ({'installed' if installed else 'not installed'})")
    print(f"loaded: {'yes' if loaded else 'no'}")
    if installed:
        nxt = min(next_run(h, m) for h, m in _times(plistlib.loads(target.read_bytes())))
        print(f"next:   {nxt:%Y-%m-%d %H:%M} local")
    try:
        from jobcopilot import store
        store.init_db()
        runs = store.list_runs(kind="daily", limit=1)
    except Exception as exc:  # noqa: BLE001
        print(f"last:   unavailable ({type(exc).__name__})")
        return 0
    if runs:
        r = runs[0]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"]))
        print(f"last:   #{r['id']} {when} {r['status']}" + (f" ({r['error'][:120]})" if r.get("error") else ""))
    else:
        print("last:   never")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="manage the job-copilot nightly LaunchAgent")
    ap.add_argument("action", choices=["install", "uninstall", "status", "render"])
    args = ap.parse_args(argv)
    if args.action == "render":
        sys.stdout.write(config_plist().decode("utf-8"))
        return 0
    return {"install": install, "uninstall": uninstall, "status": status}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
