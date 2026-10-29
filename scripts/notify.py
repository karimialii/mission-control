"""Notifications: tell the user when Mission Control needs them or has finished.

Hooked into Mission.event(), so every process (CLI, dashboard, executors) can notify.
Settings are global, in $MC_HOME/notify.json. Stdlib only. Delivery never raises and
never blocks the mission: a failing channel is logged to $MC_HOME/notify.log.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import compat

MC_HOME = Path(os.environ.get("MC_HOME", str(Path.home() / ".mission-control")))
CONFIG = MC_HOME / "notify.json"
LOG = MC_HOME / "notify.log"
DASHBOARD_URL = "http://127.0.0.1:7777"
TIMEOUT = 5

# what can be switched on or off, with the label shown to the user
KINDS = {
    "decision": "A decision is waiting for you",
    "failure": "A task or check failed, or something broke",
    "complete": "Every task in a mission is done",
    "watch": "A watched external job finished or failed",
    "reminder": "A reminder on your Today page is due",
    "limit": "The Claude plan limit is close, so new runs are held back",
}
DEFAULT = {
    "enabled": True,
    "kinds": {k: True for k in KINDS},
    "channels": {
        "desktop": {"on": True},
        "slack": {"on": False, "url": ""},
        "webhook": {"on": False, "url": ""},
        "ntfy": {"on": False, "topic": "", "server": "https://ntfy.sh"},
    },
}
SECRET_FIELDS = {"slack": "url", "webhook": "url", "ntfy": "topic"}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        cfg["enabled"] = bool(raw.get("enabled", True))
        for k, v in (raw.get("kinds") or {}).items():
            if k in KINDS:
                cfg["kinds"][k] = bool(v)
        for name, ch in (raw.get("channels") or {}).items():
            if name in cfg["channels"] and isinstance(ch, dict):
                cfg["channels"][name].update({k: v for k, v in ch.items() if k in cfg["channels"][name]})
    return cfg


def save(cfg: dict) -> None:
    MC_HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(tmp, 0o600)  # holds webhook URLs, which are secrets
    except OSError:
        pass
    os.replace(tmp, CONFIG)


def update(patch: dict) -> dict:
    """Merge a partial config from the CLI or dashboard. A blank secret keeps the stored one."""
    cfg = load()
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"])
    for k, v in (patch.get("kinds") or {}).items():
        if k in KINDS:
            cfg["kinds"][k] = bool(v)
    for name, ch in (patch.get("channels") or {}).items():
        if name not in cfg["channels"] or not isinstance(ch, dict):
            continue
        cur = cfg["channels"][name]
        if "on" in ch:
            cur["on"] = bool(ch["on"])
        for field in ("url", "topic", "server"):
            if field in cur and isinstance(ch.get(field), str) and ch[field].strip():
                val = ch[field].strip()
                if field in ("url", "server") and not val.startswith(("https://", "http://")):
                    raise ValueError(f"{name}: {field} must start with https:// (or http://)")
                cur[field] = val
    save(cfg)
    return cfg


def masked(cfg: dict | None = None) -> dict:
    """Config safe to show in the dashboard: secrets replaced by a hint of what is set."""
    cfg = json.loads(json.dumps(cfg or load()))
    for name, field in SECRET_FIELDS.items():
        ch = cfg["channels"][name]
        val = ch.get(field, "")
        ch[field] = ""
        ch["set"] = bool(val)
        ch["hint"] = (val[:8] + "…" + val[-4:]) if len(val) > 32 else ("saved" if val else "")
    cfg["kind_labels"] = KINDS
    cfg["available"] = {"desktop": desktop_supported()}
    return cfg


# ---- classifying events
def classify(ev: dict, m=None) -> str | None:
    kind = ev.get("kind")
    if kind == "question" and ev.get("to") == "user":
        return "decision"
    if kind == "review_suggest":
        return "decision"
    if kind == "report" and m is not None and ev.get("task"):
        try:  # a report that now waits for the user's own review
            t = m.task(ev["task"])
            if t.get("status") == "review" and t.get("reviewer") == "user" and t.get("executor") != "human":
                return "decision"
        except BaseException:
            pass
    if kind == "error" or (kind == "check" and ev.get("ok") is False):
        return "failure"
    if kind == "watch":
        return "watch"
    if kind == "limit_hold":  # once per hold, not once per task that was cut off
        return "limit"
    return None


def _all_done(m) -> bool:
    tasks = m.tasks()
    return bool(tasks) and all(t.get("status") in ("done", "cancelled") for t in tasks) and any(t.get("status") == "done" for t in tasks)


def _mark_complete(m) -> bool:
    """True once per completion. Adding work later (new tasks) re-arms it."""
    marker = m.dir / ".notified-complete"
    ids = ",".join(sorted(t["id"] for t in m.tasks()))
    try:
        if marker.read_text() == ids:
            return False
    except OSError:
        pass
    try:
        marker.write_text(ids)
    except OSError:
        return False
    return True


def titled(m, kind: str, ev: dict) -> tuple[str, str]:
    name = (m.meta().get("name") or m.root.name)
    msg = ev.get("msg", "")
    title = {
        "decision": f"{name}: your decision is needed",
        "failure": f"{name}: something failed",
        "complete": f"{name}: all tasks are done",
        "watch": f"{name}: a watched job finished",
        "limit": f"{name}: waiting for the plan limit",
    }[kind]
    return title, msg


def on_event(m, ev: dict) -> None:
    """Called after every event is written. Never raises."""
    try:
        if os.environ.get("MC_NOTIFY") == "0":
            return
        if compat.in_agent_sandbox():  # settings are hidden in here: the dashboard sends it instead
            with open(m.dir / "notify-queue.jsonl", "a") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            return
        cfg = load()
        if not cfg["enabled"]:
            return
        kind = classify(ev, m)
        if kind is None and ev.get("kind") in ("approved", "report", "task_updated") and _all_done(m) and _mark_complete(m):
            kind = "complete"
            ev = {**ev, "msg": f"All {len(m.tasks())} tasks are done. Time to verify and open the pull request."}
        if kind is None or not cfg["kinds"].get(kind):
            return
        title, body = titled(m, kind, ev)
        send(cfg, title, body, kind, {"project": m.meta().get("name") or m.root.name, "task": ev.get("task")})
    except Exception as e:  # a notification must never break a mission
        log(f"on_event: {e}")


def drain_queue(m) -> int:
    """Dashboard loop: send what sandboxed processes queued in .mission/notify-queue.jsonl."""
    p = m.dir / "notify-queue.jsonl"
    if not p.exists():
        return 0
    try:
        tmp = p.with_suffix(".sending")
        os.replace(p, tmp)
        rows = [json.loads(l) for l in tmp.read_text().splitlines() if l.strip()]
        tmp.unlink()
    except (OSError, ValueError):
        return 0
    for ev in rows:
        on_event(m, ev)
    return len(rows)


# ---- delivery
def log(line: str) -> None:
    try:
        MC_HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {line}\n")
    except OSError:
        pass


def desktop_supported() -> bool:
    return (sys.platform == "darwin" and bool(shutil.which("osascript"))) or (sys.platform.startswith("linux") and bool(shutil.which("notify-send")))


def _desktop(title: str, body: str, **_) -> None:
    # text goes in as arguments, never spliced into script source, so it can't inject commands
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv) sound name \"Glass\"", "-e", "end run"]
        subprocess.run(["osascript", *script, body[:240], title[:80]], timeout=TIMEOUT, capture_output=True)
    elif sys.platform.startswith("linux") and shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=Mission Control", "--", title[:80], body[:240]], timeout=TIMEOUT, capture_output=True)
    else:
        raise RuntimeError("desktop notifications are not supported on this system (macOS and Linux only)")


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        r.read(256)


def _slack(title: str, body: str, ch: dict, **_) -> None:  # also works for Discord via its /slack URL suffix
    _post(ch["url"], json.dumps({"text": f"*{title}*\n{body}\n<{DASHBOARD_URL}|Open the dashboard>"}).encode(), {"Content-Type": "application/json"})


def _webhook(title: str, body: str, ch: dict, kind: str = "", meta: dict | None = None) -> None:
    payload = {"event": kind, "title": title, "message": body, "dashboard": DASHBOARD_URL, **(meta or {})}
    _post(ch["url"], json.dumps(payload).encode(), {"Content-Type": "application/json"})


def _ntfy(title: str, body: str, ch: dict, kind: str = "", **_) -> None:
    prio = {"decision": "high", "failure": "high"}.get(kind, "default")
    _post(f"{ch['server'].rstrip('/')}/{ch['topic']}", body.encode(), {"Title": title.encode("ascii", "replace").decode(), "Priority": prio, "Click": DASHBOARD_URL})


CHANNELS = {"desktop": _desktop, "slack": _slack, "webhook": _webhook, "ntfy": _ntfy}


def _ready(name: str, ch: dict) -> bool:
    if not ch.get("on"):
        return False
    return {"slack": bool(ch.get("url")), "webhook": bool(ch.get("url")), "ntfy": bool(ch.get("topic")), "desktop": True}[name]


def send(cfg: dict, title: str, body: str, kind: str = "", meta: dict | None = None, wait: bool = False) -> dict:
    """Deliver to every enabled channel. Returns {channel: 'ok' | error} when wait=True."""
    results: dict = {}

    def one(name: str, ch: dict) -> None:
        try:
            CHANNELS[name](title, body, ch=ch, kind=kind, meta=meta)
            results[name] = "ok"
        except Exception as e:
            results[name] = f"{type(e).__name__}: {e}"
            log(f"{name} failed: {type(e).__name__}: {str(e)[:160]}")

    threads = [threading.Thread(target=one, args=(n, c)) for n, c in cfg["channels"].items() if _ready(n, c)]
    for t in threads:
        t.start()  # non-daemon: a short-lived CLI process waits (at most TIMEOUT) for delivery before exiting
    if wait:
        for t in threads:
            t.join(TIMEOUT + 2)
    return results
