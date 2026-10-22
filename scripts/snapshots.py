"""Snapshots: restorable copies of a project folder, so agents with full access can't lose your work.

A snapshot is taken before each agent run and every 30 minutes while agents work, and kept 7 days.
They live in a private git store (.mission/snapshots.git), separate from the project's own git, so it
works in any folder and never touches your history. Unchanged files are stored once, so a snapshot
of a folder that barely changed costs almost nothing. Files over 50 MB, online-only cloud files
(OneDrive/iCloud placeholders, so nothing is downloaded) and build/dependency folders are skipped.
Stdlib only; needs git.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import compat

KEEP_DAYS = 7
EVERY = 30 * 60
MAX_FILE = 50 * 1024 * 1024
SKIP_DIRS = {".mission", ".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
             "dist", "build", ".next", ".cache", ".DS_Store", ".claude-flow", ".swarm"}
DATALESS = 0x40000000  # macOS SF_DATALESS: a cloud placeholder whose contents aren't on disk


def available() -> bool:
    return bool(shutil.which("git"))


def _store(root: Path) -> Path:
    return root / ".mission" / "snapshots.git"


def _git(root: Path, *args, check=True, env_extra=None, input_=None) -> str:
    sd = _store(root)
    env = {**os.environ, "GIT_DIR": str(sd), "GIT_WORK_TREE": str(root), "GIT_INDEX_FILE": str(sd / "mc-index"),
           "GIT_AUTHOR_NAME": "Mission Control", "GIT_AUTHOR_EMAIL": "snapshots@mission-control.local",
           "GIT_COMMITTER_NAME": "Mission Control", "GIT_COMMITTER_EMAIL": "snapshots@mission-control.local",
           "GIT_CONFIG_NOSYSTEM": "1", **(env_extra or {})}
    p = subprocess.run(["git", *args], cwd=str(root), env=env, capture_output=True, text=input_ is None or isinstance(input_, str),
                       input=input_, timeout=600)
    if check and p.returncode:
        raise RuntimeError(f"git {args[0]}: {(p.stderr or '').strip()[:300]}")
    return p.stdout


def _init(root: Path) -> None:
    sd = _store(root)
    if (sd / "HEAD").exists():
        return
    sd.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-q", str(sd)], check=True, capture_output=True)
    for k, v in (("core.autocrlf", "false"), ("core.fileMode", "false"), ("gc.auto", "0"), ("core.quotePath", "false")):
        subprocess.run(["git", "--git-dir", str(sd), "config", k, v], check=True, capture_output=True)


def _skipped(root: Path) -> list:
    """Paths (relative, posix) git must not read: big files and online-only placeholders."""
    out = []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(dp, d))]
        for f in files:
            full = os.path.join(dp, f)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if st.st_size > MAX_FILE or (getattr(st, "st_flags", 0) & DATALESS):
                out.append(Path(full).relative_to(root).as_posix())
    return out


def _write_excludes(root: Path) -> None:
    info = _store(root) / "info"
    info.mkdir(exist_ok=True)
    lines = [f"/{d}/" for d in sorted(SKIP_DIRS)] + [d + "/" for d in ("node_modules", "__pycache__", ".venv")] + [".DS_Store"]
    lines += ["/" + p.replace("[", "\\[").replace("*", "\\*").replace("?", "\\?") for p in _skipped(root)]
    (info / "exclude").write_text("\n".join(lines) + "\n")


def _current_tree(root: Path) -> str:
    _write_excludes(root)
    _git(root, "add", "-A", "--ignore-errors", ".", check=False)
    return _git(root, "write-tree").strip()


@contextlib.contextmanager
def _lock(root: Path, blocking: bool = True):
    """Across processes: parallel executors may all snapshot as they start."""
    _store(root).parent.mkdir(exist_ok=True)
    with open(_store(root).parent / ".snapshot.lock", "a") as f:
        compat.lock(f, blocking)
        try:
            yield
        finally:
            compat.unlock(f)


def busy(root) -> bool:
    try:
        with _lock(Path(root), blocking=False):
            return False
    except BlockingIOError:
        return True


def take(root, reason: str) -> dict | None:
    """Snapshot now. Returns the snapshot, or None when nothing changed since the last one."""
    root = Path(root)
    if not available():
        return None
    with _lock(root):
        _init(root)
        tree = _current_tree(root)
        snaps = list_snapshots(root)
        if snaps and snaps[0]["tree"] == tree:
            _touch(root)
            return None
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        commit = _git(root, "commit-tree", tree, "-m", f"{reason}\n\n{ts}").strip()
        ref = f"refs/snaps/{int(time.time() * 1000)}"
        _git(root, "update-ref", ref, commit)
        _touch(root)
        prune(root)
        return {"ref": ref, "commit": commit, "tree": tree, "ts": ts, "reason": reason}


def _touch(root: Path) -> None:
    (_store(root) / "mc-last").write_text(str(time.time()))


def last_time(root) -> float:
    try:
        return float((_store(Path(root)) / "mc-last").read_text())
    except (OSError, ValueError):
        return 0.0


def maybe(root, reason: str, every: int = EVERY) -> None:
    """Take a snapshot in the background if the last one is older than `every` seconds (0 = always)."""
    root = Path(root)
    if not available() or (every and time.time() - last_time(root) < every) or busy(root):
        return
    t = threading.Thread(target=_safe_take, args=(root, reason), daemon=True)
    t.start()


def _safe_take(root: Path, reason: str) -> None:
    try:
        take(root, reason)
    except Exception as e:  # a failed snapshot must never stop an agent
        try:
            (_store(root) / "mc-error").write_text(f"{datetime.now():%Y-%m-%d %H:%M} {e}")
        except OSError:
            pass


def list_snapshots(root) -> list:
    root = Path(root)
    if not (_store(root) / "HEAD").exists():
        return []
    out = _git(root, "for-each-ref", "--sort=-refname", "--format=%(refname)%09%(objectname)%09%(tree)%09%(contents:subject)%09%(contents:body)", "refs/snaps/", check=False)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        ref, commit, tree, subject = parts[:4]
        ms = int(ref.rsplit("/", 1)[1])
        rows.append({"ref": ref, "commit": commit, "tree": tree, "reason": subject,
                     "ts": datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S"), "ms": ms})
    rows.sort(key=lambda r: -r["ms"])
    return rows


def prune(root) -> int:
    root = Path(root)
    cutoff = (time.time() - KEEP_DAYS * 86400) * 1000
    old = [s for s in list_snapshots(root) if s["ms"] < cutoff]
    for s in old:
        _git(root, "update-ref", "-d", s["ref"], check=False)
    if old:
        _git(root, "gc", "--prune=now", "-q", check=False)
    return len(old)


def changes(root, ref: str) -> list:
    """What differs between a snapshot and the folder now: [{"status": "M|A|D", "path"}].
    A = in the folder now but not in the snapshot; D = in the snapshot but gone now."""
    root = Path(root)
    snap = _resolve(root, ref)
    with _lock(root):
        now = _current_tree(root)
    out = _git(root, "diff-tree", "-r", "--name-status", "--no-renames", snap["tree"], now)
    rows = []
    for line in out.splitlines():
        st, _, path = line.partition("\t")
        rows.append({"status": st[:1], "path": path})
    return rows


def size_bytes(root) -> int:
    sd = _store(Path(root))
    return sum(f.stat().st_size for f in sd.rglob("*") if f.is_file()) if sd.exists() else 0


def _resolve(root: Path, ref: str) -> dict:
    snap = next((s for s in list_snapshots(root) if s["ref"] == ref), None)
    if not snap:
        raise ValueError("that snapshot doesn't exist (it may be older than 7 days)")
    return snap


def restore(root, ref: str, paths: list | None = None) -> dict:
    """Put files back as they were in a snapshot. With paths, only those; without, every file in it.
    Files created after the snapshot are kept. The current state is snapshotted first, so it can be undone."""
    root = Path(root)
    snap = _resolve(root, ref)
    take(root, "before restoring " + snap["ts"].replace("T", " "))
    with _lock(root):
        entries = [e for e in _git(root, "ls-tree", "-r", "-z", snap["tree"]).split("\0") if e]
        listed = [e.split("\t", 1)[1] for e in entries if e.startswith(("100644", "100755"))]  # regular files only
        wanted = listed if not paths else [p for p in paths if p in set(listed)]
        root_r = root.resolve()
        n = 0
        for rel in wanted:
            dest = (root / rel).resolve()
            if root_r not in dest.parents:
                continue  # never write outside the project
            data = subprocess.run(["git", "--git-dir", str(_store(root)), "cat-file", "blob", f"{snap['tree']}:{rel}"],
                                  capture_output=True, timeout=120).stdout
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            n += 1
        return {"restored": n, "from": snap["ts"]}
