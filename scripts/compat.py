"""Platform differences in one place. On macOS and Linux every helper does what the old inline code did."""
from __future__ import annotations

import contextlib
import errno
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"

if IS_WIN:
    import msvcrt
else:
    import fcntl


def ensure_utf8() -> None:
    """Windows opens files and decodes pipes in the locale code page unless UTF-8 mode is on. Re-run once in UTF-8 mode."""
    if IS_WIN and not sys.flags.utf8_mode:
        env = {**os.environ, "PYTHONUTF8": "1"}
        try:
            sys.exit(subprocess.call([sys.executable, *sys.argv], env=env))
        except KeyboardInterrupt:
            sys.exit(130)


# ── file locks ──────────────────────────────────────────────────────────────

def lock(f, blocking: bool = True) -> None:
    """Exclusive lock on an open file. With blocking=False raises BlockingIOError if someone else holds it."""
    if not IS_WIN:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    while True:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as e:
            if not blocking:
                raise BlockingIOError(e.errno, "locked") from e
            time.sleep(0.05)


def unlock(f) -> None:
    if not IS_WIN:
        fcntl.flock(f, fcntl.LOCK_UN)
        return
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def file_lock(path):
    with open(path, "a") as f:
        lock(f)
        try:
            yield f
        finally:
            unlock(f)


# ── processes ───────────────────────────────────────────────────────────────

def pid_alive(pid) -> bool:
    if not pid:
        return False
    pid = int(pid)
    if not IS_WIN:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5 or k32.GetLastError() == 5  # access denied: it exists
    try:
        code = ctypes.c_ulong()
        return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def spawn_kwargs() -> dict:
    """Popen options that detach a child so stopping its parent leaves it (and lets us stop it as a group)."""
    if IS_WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _taskkill(pid) -> None:
    subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True)


def kill_group(pid) -> None:
    """Stop a process we started with spawn_kwargs() and everything it started."""
    import signal
    if IS_WIN:
        _taskkill(pid)
        return
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def descendants(pid: int) -> list:
    if IS_WIN:
        return []
    out, stack = [], [pid]
    while stack:
        cur = stack.pop()
        try:
            cp = subprocess.run(["pgrep", "-P", str(cur)], capture_output=True, text=True)
        except OSError:
            break
        for tok in cp.stdout.split():
            if tok.isdigit():
                out.append(int(tok))
                stack.append(int(tok))
    return out


def stop_tree(proc) -> None:
    """Stop a headless CLI and anything it left running in the background (its helper scripts, sleeps)."""
    import signal
    if IS_WIN:
        _taskkill(proc.pid)
        return
    kids = descendants(proc.pid)
    try:
        proc.terminate()
    except OSError:
        pass
    for k in kids:
        try:
            os.kill(k, signal.SIGTERM)
        except OSError:
            pass


def kill_hard(proc) -> None:
    if IS_WIN:
        _taskkill(proc.pid)
    else:
        proc.kill()


def terminate_pid(pid, force: bool = False) -> None:
    import signal
    if IS_WIN:
        _taskkill(pid)
    else:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def _powershell(script: str) -> str:
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return ""
    try:
        return subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def process_commands():
    """Command lines of all running processes, or None where this system can't list them."""
    if IS_WIN:
        out = _powershell("Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }")
        return out.splitlines() if out.strip() else None
    try:
        return subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout.splitlines()
    except OSError:
        return None


def process_command(pid: int) -> str:
    if IS_WIN:
        return _powershell(f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine")
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return ""


def port_holder(port: int):
    """PID listening on a TCP port, or None."""
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
            for ln in out.splitlines():
                p = ln.split()
                if len(p) >= 5 and p[3] == "LISTENING" and p[1].rsplit(":", 1)[-1] == str(port):
                    return int(p[4])
            return None
        out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None
    except (OSError, ValueError):
        return None


ADDR_IN_USE = {errno.EADDRINUSE, 48, 98, 10048}  # macOS, Linux, Windows (WSAEADDRINUSE)


# ── files, folders, executables ─────────────────────────────────────────────

def replace_file(src, dst) -> None:
    """os.replace, retrying on Windows where a reader holding the target open makes it fail briefly."""
    for i in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WIN or i == 19:
                raise
            time.sleep(0.05)


def reveal(target: Path) -> bool:
    """Show a file or folder in the system file manager. False if this system has no way to."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(target)] if target.is_file() else ["open", str(target)])
    elif IS_WIN:
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
    else:
        return False
    return True


def resolve_exe(cmd: list) -> list:
    """Find the program on PATH. On Windows, refuse .cmd/.bat wrappers: cmd.exe would re-parse the prompt in the arguments."""
    if not IS_WIN:
        return cmd
    exe = shutil.which(cmd[0]) or cmd[0]
    if exe.lower().endswith((".cmd", ".bat")):
        raise RuntimeError(f"'{cmd[0]}' is a {Path(exe).suffix} wrapper ({exe}). Windows would let cmd.exe re-interpret the prompt, "
                           "so Mission Control won't run it. Use a native .exe build of the CLI, or run Mission Control inside WSL.")
    return [exe, *cmd[1:]]


def bash() -> str | None:
    """A bash to run `check` commands (they are written in bash syntax). On Windows: Git Bash, never the WSL launcher."""
    if not IS_WIN:
        return shutil.which("bash") or "/bin/sh"
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LocalAppData")):
        if base:
            for sub in ("Git/bin/bash.exe", "Programs/Git/bin/bash.exe"):
                p = Path(base) / sub
                if p.exists():
                    return str(p)
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


MAX_CMDLINE = 30000  # CreateProcess caps a whole command line at 32,767 characters


def cmdline_too_long(cmd: list) -> bool:
    return IS_WIN and sum(len(str(a)) + 3 for a in cmd) > MAX_CMDLINE


def augment_path() -> list:
    """Add the folders where AI CLIs usually live to PATH (apps started from a GUI or a fresh shell often miss them).
    Returns the folders that were added."""
    home = Path.home()
    cands = [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin", home / ".bun" / "bin",
             home / ".cargo" / "bin", home / ".volta" / "bin"]
    if IS_WIN:
        cands += [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm",
                  Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs" / "claude",
                  home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links"]
    else:
        cands += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/home/linuxbrew/.linuxbrew/bin")]
        nvm = home / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            cands += sorted(nvm.glob("*/bin"))
    cur = os.environ.get("PATH", "").split(os.pathsep)
    added = [str(c) for c in cands if c.is_dir() and str(c) not in cur]
    if added:
        os.environ["PATH"] = os.pathsep.join(cur + added)
    return added


def in_agent_sandbox() -> bool:
    """True inside the OS sandbox Mission Control gives its agents. The sandbox hides one marker file
    ($MC_HOME/.sandbox-probe) from them, so failing to read it means we're inside."""
    probe = Path(os.environ.get("MC_HOME", str(Path.home() / ".mission-control"))) / ".sandbox-probe"
    try:
        probe.read_bytes()
        return False
    except PermissionError:
        return True
    except OSError:
        return False
