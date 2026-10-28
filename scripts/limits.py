"""Claude plan limits (the 5-hour and the 7-day window), shared by every project on this account.

Every Claude Code run reports them as `rate_limit_event` lines in its stream. All runs of all projects draw from
the same pool, so the newest reading lives in one file next to the project list, not in a mission's .mission/.
The scheduler uses it to stop starting runs before the window fills up (executors earlier than the supervisor,
so cheap workers can't use up the window the supervisor needs to review their work).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

MC_HOME = Path(os.environ.get("MC_HOME", str(Path.home() / ".mission-control")))
PATH = MC_HOME / "limits.json"

WINDOWS = ("five_hour", "seven_day")
LABEL = {"five_hour": "5h", "seven_day": "7d"}
DEFAULT_CONFIG = {"enabled": True, "executor_pct": 85, "supervisor_pct": 95}
FALLBACK_WAIT = 30 * 60  # a limit hit whose reset time we couldn't read: look again after this long


def _read() -> dict:
    try:
        d = json.loads(PATH.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    MC_HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(MC_HOME), prefix=".limits-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, PATH)  # atomic: several runs write at once
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in (_read().get("config") or {}).items() if k in DEFAULT_CONFIG})
    return cfg


def set_config(**kw) -> dict:
    d = _read()
    cfg = dict(d.get("config") or {})
    for k, v in kw.items():
        if k in DEFAULT_CONFIG and v is not None:
            cfg[k] = v
    d["config"] = cfg
    _write(d)
    return config()


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def record(info: dict, now: float | None = None) -> dict | None:
    """Merge one `rate_limit_info` object from a stream into the shared snapshot.
    Utilization only grows inside a window, so a late event from an older run can't pull it back down; a window
    with a later reset time is a new window and replaces the old one. Returns the rejection ({window, resets_at})
    when this event says the limit was hit, else None."""
    if not isinstance(info, dict):
        return None
    now = now or time.time()
    d = _read()
    wins = d.setdefault("windows", {})
    seen = {}
    for name, w in (info.get("unifiedWindows") or {}).items():
        if name in WINDOWS and isinstance(w, dict):
            seen[name] = w
    top = info.get("rateLimitType")
    if top in WINDOWS and top not in seen and _num(info.get("utilization")) is not None:
        seen[top] = {"utilization": info["utilization"], "resetsAt": info.get("resetsAt")}
    for name, w in seen.items():
        u, r = _num(w.get("utilization")), _num(w.get("resetsAt"))
        if u is None or r is None:
            continue
        old = wins.get(name) or {}
        if _num(old.get("resets_at")) == r:
            u = max(u, _num(old.get("utilization")) or 0.0)
        elif _num(old.get("resets_at")) is not None and r < old["resets_at"]:
            continue  # older than what we have
        wins[name] = {"utilization": u, "resets_at": r, "seen": now}
    rejected = None
    if info.get("status") == "rejected" and not info.get("isUsingOverage"):
        name = top if top in WINDOWS else "five_hour"
        r = _num(info.get("resetsAt")) or _num((wins.get(name) or {}).get("resets_at"))
        rejected = {"window": name, "resets_at": r, "seen": now}
        d["rejected"] = rejected
    elif info.get("status") in ("allowed", "allowed_warning") and (d.get("rejected") or {}).get("seen", 0) < now:
        d.pop("rejected", None)  # a call went through after the rejection: the limit is open again
    d["captured_at"] = now
    d["status"] = info.get("status") or d.get("status")
    _write(d)
    return rejected


def record_rejection(resets_at: float | None, window: str = "five_hour", now: float | None = None) -> None:
    """A run printed 'You've hit your session limit' but we saw no rejected event: remember it anyway."""
    d = _read()
    d["rejected"] = {"window": window, "resets_at": resets_at or ((now or time.time()) + FALLBACK_WAIT), "seen": now or time.time()}
    _write(d)


_RESET_TEXT = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", re.I)


def parse_reset_text(text: str, now: float | None = None) -> float | None:
    """'resets 7:40pm (Europe/Oslo)' -> the next time that is, as a Unix time."""
    m = _RESET_TEXT.search(text or "")
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(m.group(4).strip())
    except Exception:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    cur = datetime.fromtimestamp(now or time.time(), tz)
    at = cur.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if at <= cur:
        at += timedelta(days=1)
    return at.timestamp()


def snapshot(now: float | None = None) -> dict:
    """What the dashboard shows: each window with whether its reading is still current."""
    now = now or time.time()
    d = _read()
    out = {"config": config(), "windows": {}, "rejected": None, "captured_at": d.get("captured_at")}
    for name in WINDOWS:
        w = (d.get("windows") or {}).get(name)
        if not w:
            continue
        live = (_num(w.get("resets_at")) or 0) > now  # past its reset time = unknown, not full
        out["windows"][name] = {"utilization": w.get("utilization") if live else None, "resets_at": w.get("resets_at"),
                                "live": live, "seen": w.get("seen")}
    rj = d.get("rejected")
    if rj and (_num(rj.get("resets_at")) or 0) > now:
        out["rejected"] = rj
    return out


def clock(ts: float) -> str:
    t = datetime.fromtimestamp(ts)
    day = "" if t.date() == datetime.now().date() else t.strftime("%a ")
    return day + t.strftime("%H:%M")


def hold(kind: str, now: float | None = None) -> tuple[bool, str, float | None]:
    """Should a run of this kind ('executor' or 'supervisor') be held back? -> (hold, reason, resets_at).
    A rejected limit holds every kind until it resets; otherwise a window at or past the kind's threshold does."""
    cfg = config()
    now = now or time.time()
    snap = snapshot(now)
    rj = snap["rejected"]
    if rj:
        return True, f"plan limit reached ({LABEL.get(rj['window'], rj['window'])} window), resets {clock(rj['resets_at'])}", rj["resets_at"]
    if not cfg["enabled"] or kind == "user":
        return False, "", None
    pct = cfg["supervisor_pct" if kind == "supervisor" else "executor_pct"] / 100.0
    worst = None
    for name, w in snap["windows"].items():
        if w["live"] and w["utilization"] is not None and w["utilization"] >= pct:
            if worst is None or w["resets_at"] > worst[1]:
                worst = (name, w["resets_at"], w["utilization"])
    if worst:
        name, resets, u = worst
        return True, f"holding: {LABEL[name]} window at {u * 100:.0f}% (limit for {kind}s {pct * 100:.0f}%), resets {clock(resets)}", resets
    return False, "", None
