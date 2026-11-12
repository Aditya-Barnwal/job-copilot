"""Install / uninstall / inspect the nightly LaunchAgent (D2).

  python scripts/schedule.py install     # write ~/Library/LaunchAgents/com.jobcopilot.daily.plist + bootstrap
  python scripts/schedule.py uninstall   # bootout + remove the plist
  python scripts/schedule.py status      # installed? loaded? next run, last run (runs table)
  python scripts/schedule.py render      # print the plist that install would write

The time comes from config/schedule.yaml (`daily_run`, in `timezone`) and is
converted to this Mac's local time, which is what launchd uses.
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


def render_plist(*, hour: int, minute: int, repo: Path = REPO, python: Optional[Path] = None,
                 log_dir: Optional[Path] = None, template: Path = TEMPLATE) -> bytes:
    """Fill the template; the result is validated by plistlib before it is returned."""
    repo = Path(repo).resolve()
    python = Path(python) if python else repo / ".venv" / "bin" / "python"
    log_dir = Path(log_dir) if log_dir else repo / "data" / "logs"
    for p in (repo, python, log_dir):
        if not p.is_absolute():
            raise ValueError(f"path must be absolute: {p}")
    values = {"LABEL": LABEL, "PYTHON": str(python), "REPO": str(repo),
              "LOG_DIR": str(log_dir), "HOUR": str(int(hour)), "MINUTE": str(int(minute))}
    text = template.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", escape(v))
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        raise ValueError(f"unfilled placeholder(s) in plist template: {left}")
    data = text.encode("utf-8")
    plistlib.loads(data)   # raises on malformed output
    return data


def config_plist(**kw: Any) -> bytes:
    from jobcopilot import config
    sched = config.schedule()
    hour, minute = local_run_time(sched.get("daily_run") or "19:00", sched.get("timezone"))
    return render_plist(hour=hour, minute=minute, **kw)


def next_run(hour: int, minute: int, now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or dt.datetime.now()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return nxt if nxt > now else nxt + dt.timedelta(days=1)


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> int:
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
    p = plistlib.loads(data)["StartCalendarInterval"]
    print(f"installed {target}; runs daily at {p['Hour']:02d}:{p['Minute']:02d} local time")
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
        p = plistlib.loads(target.read_bytes())["StartCalendarInterval"]
        print(f"next:   {next_run(p['Hour'], p['Minute']):%Y-%m-%d %H:%M} local")
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
