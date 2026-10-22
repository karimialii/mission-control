#!/usr/bin/env python3
"""One-step launcher: checks your setup, detects installed AI CLIs, and opens the dashboard.

    python3 start.py            (Windows: python start.py, or double-click start.bat)

Stdlib only. Safe to run again any time; it never changes your projects.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))

if sys.version_info < (3, 9):
    sys.exit(f"Mission Control needs Python 3.9 or newer (this is {sys.version.split()[0]}). Get it at https://www.python.org/downloads/")

import compat  # noqa: E402

compat.ensure_utf8()
import dashboard  # noqa: E402
import mc  # noqa: E402

PORT = int(os.environ.get("MC_PORT", "7777"))


def report() -> None:
    print("Mission Control · setup check")
    print(f"  Python   {sys.version.split()[0]}  ✓")
    for tool, why in (("git", "needed for task branches and PRs"), ("gh", "optional: clone private repos, open PRs")):
        path = mc.shutil.which(tool)
        print(f"  {tool:<8} {'✓ ' + path if path else '– not found (' + why + ')'}")
    found = mc.detect_providers()
    for name, path in found.items():
        _, install, signin = mc.PROVIDERS[name]
        print(f"  {name:<8} {'✓ ' + path if path else '– not found. Install: ' + install}")
        if path and compat.IS_WIN and path.lower().endswith((".cmd", ".bat")):
            print(f"           ! {name} is a .cmd wrapper, which Mission Control refuses on native Windows. Use WSL, or a native .exe build.")
    usable = [k for k, v in found.items() if v]
    if usable:
        print(f"\n  New projects will use: {mc.default_provider()}  (change per project in the dashboard)")
        print("  Not signed in yet? " + "; ".join(f"{k}: {mc.PROVIDERS[k][2]}" for k in usable))
    else:
        print("\n  No AI CLI found. The dashboard still opens (try the Simulator runner), but to do real work install one,")
        print("  e.g. `npm i -g @openai/codex`, then run this script again.")


def link_skill() -> None:
    """If Claude Code is present, make Mission Control available as a skill (so a Claude chat can be the supervisor)."""
    skills = Path.home() / ".claude" / "skills"
    dest = skills / "mission-control"
    if not skills.parent.is_dir() or dest.exists() or dest.is_symlink():
        return
    try:
        skills.mkdir(exist_ok=True)
        if compat.IS_WIN:
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dest), str(HERE)], check=True, capture_output=True)
        else:
            dest.symlink_to(HERE)
        print(f"  Linked as a Claude Code skill: {dest}")
    except Exception as e:  # optional nicety, never block startup
        print(f"  (could not link the Claude Code skill: {e})")


def pick_port(start: int) -> int:
    """Our own dashboard on the port is replaced by bind(); any other program means: try the next port."""
    for port in range(start, start + 20):
        try:
            dashboard.Server(("127.0.0.1", port), dashboard.Handler).server_close()
            return port
        except OSError:
            pid = dashboard.holder_pid(port)
            if pid and dashboard.is_dashboard(pid):
                return port
    sys.exit(f"No free port found between {start} and {start + 19}. Set MC_PORT to another number.")


def main() -> None:
    report()
    mc.MC_HOME.mkdir(parents=True, exist_ok=True)
    link_skill()
    sys.argv = [sys.argv[0], "--port", str(pick_port(PORT))] + (["--open"] if "--no-open" not in sys.argv else [])
    print()
    dashboard.main()


if __name__ == "__main__":
    main()
