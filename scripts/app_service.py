"""Keep the local app running on http://127.0.0.1:8000 (macOS LaunchAgent).

  python scripts/app_service.py install     # write ~/Library/LaunchAgents/com.jobcopilot.app.plist + start
  python scripts/app_service.py uninstall   # stop + remove it
  python scripts/app_service.py status      # loaded? answering?
  python scripts/app_service.py render      # print the plist install would write

The app listens on loopback only (127.0.0.1), starts at login and is restarted
if it stops, so the evening link always works. Logs: data/logs/app.*.log.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LABEL = "com.jobcopilot.app"
PORT = int(os.environ.get("JOBCOPILOT_PORT", "8000"))


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render() -> bytes:
    py = REPO / ".venv" / "bin" / "python"
    logs = REPO / "data" / "logs"
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(py), "-m", "uvicorn", "server.app:app",
                             "--host", "127.0.0.1", "--port", str(PORT), "--workers", "1"],
        "WorkingDirectory": str(REPO),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # restart after a crash, not after a clean stop
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "app.out.log"),
        "StandardErrorPath": str(logs / "app.err.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def answering() -> bool:
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/meta", timeout=3).status == 200
    except Exception:  # noqa: BLE001
        return False


def install() -> int:
    (REPO / "data" / "logs").mkdir(parents=True, exist_ok=True)
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{_domain()}/{LABEL}")          # replace an older copy
    p.write_bytes(render())
    r = _launchctl("bootstrap", _domain(), str(p))
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"installed {p}\nthe app starts at login and stays up: http://127.0.0.1:{PORT}")
    return 0


def uninstall() -> int:
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    plist_path().unlink(missing_ok=True)
    print("removed; the app no longer starts by itself (make run still works)")
    return 0


def status() -> int:
    loaded = _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
    up = answering()
    print(f"LaunchAgent {'loaded' if loaded else 'not installed'}; "
          f"http://127.0.0.1:{PORT} {'answering' if up else 'not answering'}")
    return 0 if (loaded and up) else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "render":
        sys.stdout.write(render().decode())
        raise SystemExit(0)
    fn = {"install": install, "uninstall": uninstall, "status": status}.get(cmd)
    if fn is None:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(fn())
