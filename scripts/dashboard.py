#!/usr/bin/env python3
"""Mission Control dashboard: a local web UI over every registered project's .mission/ folder.

Listens on 127.0.0.1 only. Stdlib only.  python3 dashboard.py [--port 7777] [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compat  # noqa: E402
import mc  # noqa: E402

if __name__ == "__main__":
    compat.ensure_utf8()

UI = mc.SKILL_DIR / "dashboard" / "index.html"
TOKEN = secrets.token_hex(16)
CHILD_LOCK = threading.Lock()


TASK_ID = re.compile(r"T-\d{1,6}")
REPO_SHORT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def looks_like_repo(raw: str) -> bool:
    """owner/name, https://host/owner/name(.git), git@host:owner/name.git, ssh://… (and not an existing folder)."""
    raw = raw.strip()
    if not raw or raw.startswith(("/", "~", ".", "-")) or Path(raw).expanduser().exists():
        return False
    return bool(REPO_SHORT.match(raw)) or raw.startswith(("https://", "http://", "git@", "ssh://"))


def repo_name(raw: str) -> str:
    tail = re.split(r"[/:]", raw.strip().rstrip("/"))[-1]
    return re.sub(r"\.git$", "", tail) or "repo"


def default_clone_dir() -> Path:
    for d in ("Documents/GitHub", "Projects", "code", "Developer", "src"):
        if (Path.home() / d).is_dir():
            return Path.home() / d
    return Path.home()


def repo_key(url: str) -> str:
    """owner/name from any GitHub-style spec, lowercased."""
    url = url.strip().rstrip("/")
    url = re.sub(r"\.git$", "", url)
    parts = re.split(r"[/:]", url)
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else url.lower()


def same_repo(folder: Path, raw: str) -> bool:
    if not (folder / ".git").exists():
        return False
    origin = mc.subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"], capture_output=True, text=True).stdout
    return bool(origin.strip()) and repo_key(origin) == repo_key(raw)


def clone_repo(raw: str, dest_dir: str = "") -> Path:
    """Clone with the GitHub CLI when it's available (handles private repos you can access), else git."""
    raw = raw.strip()
    base = Path(dest_dir).expanduser().resolve() if dest_dir.strip() else default_clone_dir()
    base.mkdir(parents=True, exist_ok=True)
    name = repo_name(raw)
    dest = base / name
    n = 2
    while dest.exists():
        if same_repo(dest, raw):
            return dest  # already cloned from this repo: just use it
        dest = base / f"{name}-{n}"
        n += 1
    if REPO_SHORT.match(raw) and mc.shutil.which("gh"):
        cmd = ["gh", "repo", "clone", raw, str(dest)]
    elif REPO_SHORT.match(raw):
        cmd = ["git", "clone", "--", f"https://github.com/{raw}.git", str(dest)]
    elif mc.shutil.which("gh") and "github.com" in raw and raw.startswith("http"):
        cmd = ["gh", "repo", "clone", raw, str(dest)]
    else:
        cmd = ["git", "clone", "--", raw, str(dest)]
    cp = mc.subprocess.run(cmd, capture_output=True, text=True, timeout=900, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if cp.returncode != 0:
        raise RuntimeError("Couldn't clone it: " + (cp.stderr.strip().splitlines() or ["unknown error"])[-1][:300])
    return dest


def project_paths() -> dict:
    return {p["path"]: p for p in mc.registry() if Path(p["path"]).is_dir()}


def initialized(path: str) -> bool:
    return (Path(path) / ".mission" / "mission.json").exists()


def get_mission(path: str) -> mc.Mission:
    if path not in project_paths():
        raise PermissionError("unknown project")
    if not initialized(path):
        raise PermissionError("project isn't under Mission Control yet")
    return mc.Mission(path)


def peek_files(path: Path) -> list:
    try:
        return sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not p.name.startswith("."))[:14]
    except OSError:
        return []


def runner_info() -> dict:
    return {k: {"label": v.get("label", k), "model": v.get("model", ""), "models": v.get("models", []),
                "efforts": v.get("efforts", []), "role": v.get("role", "executor"), "installed": bool(mc.shutil.which(str(v["cmd"][0]).replace("{python}", sys.executable)))}
            for k, v in mc.load_runners().items()}


def usage_summary(rows: list) -> dict:
    out = {"cost": 0.0, "sup_cost": 0.0, "exec_cost": 0.0, "in": 0, "out": 0, "runs": 0, "turns": 0, "last": "", "by_model": {}}
    for r in rows:
        c = float(r.get("cost_usd") or 0)
        out["cost"] += c
        out["sup_cost" if r.get("kind") == "supervisor" else "exec_cost"] += c
        out["in"] += (r.get("input") or 0) + (r.get("cache_read") or 0) + (r.get("cache_write") or 0)
        out["out"] += r.get("output") or 0
        out["turns" if r.get("kind") == "supervisor" else "runs"] += 1
        out["last"] = max(out["last"], r.get("ts") or "")
        mk = str(r.get("model") or "?") + ("" if r.get("kind") == "executor" else " (supervisor)")
        out["by_model"][mk] = round(out["by_model"].get(mk, 0) + c, 6)
    for k in ("cost", "sup_cost", "exec_cost"):
        out[k] = round(out[k], 4)
    return out


def usage_csv(m: mc.Mission) -> str:
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["ts", "kind", "task", "attempt", "turn", "model", "effort", "duration_s", "input", "cache_read", "cache_write",
            "output", "cost_usd", "exact", "status", "trigger"]
    w.writerow(cols)
    for r in mc.usage_rows(m):
        w.writerow([r.get(c, "") for c in cols])
    return buf.getvalue()


def summary(path: str, info: dict) -> dict:
    if not initialized(path):
        return {"path": path, "name": info.get("name") or Path(path).name, "initialized": False,
                "files": peek_files(Path(path)), "updated": info.get("added", ""), "runners": runner_info()}
    m = mc.Mission(path)
    meta = m.meta()
    tasks = m.tasks()
    counts = {s: 0 for s in mc.STATUSES}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}
    for t in tasks:
        for k in tok:
            tok[k] += t.get("tokens", {}).get(k, 0) or 0
    user_q = sum(1 for q in m.questions() if q.get("to") == "user" and q.get("status") == "open")
    updated = max([t.get("updated", "") for t in tasks] + [meta.get("created", "")])
    sup = meta.get("supervisor") or {}
    return {"path": path, "initialized": True, "phase": meta.get("phase", "executing"),
            "supervisor": {"mode": sup.get("mode", "external"), "running": bool(sup.get("running"))},
            "name": meta.get("name", info.get("name")), "goal": meta.get("goal", ""),
            "counts": counts, "total": len(tasks), "tokens": tok, "needs_you": user_q,
            "autopilot": bool(meta.get("autopilot")), "updated": updated}


def full_state(m: mc.Mission) -> dict:
    tasks = m.tasks()
    levels = mc.phase_levels(tasks)
    for t in tasks:
        t["phase"] = levels[t["id"]]
    runners = runner_info()
    meta = m.meta()
    sup = meta.get("supervisor") or {}
    if sup.get("running") and not mc.pid_alive(sup.get("pid")):
        sup = mc.sup_update(m, running=False, pid=None, step="")
    resume = (f'cd "{m.root}" && PATH="{mc.ensure_mc_shim()}:$PATH" claude --resume {sup["session_id"]}' if sup.get("session_id") else "")
    return {"mission": m.meta(), "supervisor": sup, "resume_cmd": resume, "watches": mc.watch_list(m) if mc.watches_dir(m).exists() else [], "chat": mc.chat_log(m)[-200:], "path": str(m.root), "tasks": tasks, "questions": [dict(q, perm_info=mc.perm_infos(q["permission"])) if q.get("permission") else q for q in m.questions()],
            "plan_permissions": mc.plan_permission_rules(m) if meta.get("phase") == "awaiting_approval" else [],
            "notes": m.notes()[-50:], "events": m.events()[-300:], "runners": runners,
            "ready": [t["id"] for t in mc.ready_tasks(m, tasks)], "server_time": mc.now()}


def read_log(m: mc.Mission, tid: str, attempt: int, offset: int) -> dict:
    if not TASK_ID.fullmatch(tid):
        raise PermissionError("bad task id")
    p = m.dir / "runs" / tid / f"attempt-{int(attempt)}.jsonl"
    if not p.exists():
        return {"lines": [], "offset": 0}
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read(400_000)
    cut = data.rfind(b"\n") + 1
    lines = []
    for raw in data[:cut].splitlines():
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return {"lines": lines, "offset": offset + cut}


LAST_SYNC: dict = {}
SYNC_EVERY = 90  # seconds between GitHub checks for human tasks


def supervisor_loop() -> None:
    """Reaps finished runners, marks crashed ones, syncs human tasks from GitHub, and runs autopilot."""
    while True:
        with CHILD_LOCK:
            mc.SPAWNED[:] = [p for p in mc.SPAWNED if p.poll() is None]
        for path in project_paths():
            if not initialized(path):
                continue
            try:
                m = mc.Mission(path)
                mc.reap_dead(m)
                mc.watches_tick(m)
                mc.ensure_attempt_limit_cards(m)
                if (time.time() - LAST_SYNC.get(path, 0) > SYNC_EVERY
                        and any(mc.is_human(t) and t["status"] in ("running", "review", "blocked") for t in m.tasks())):
                    LAST_SYNC[path] = time.time()
                    mc.sync_humans(m)
                sup = m.meta().get("supervisor") or {}
                if sup.get("mode") == "dashboard" and not (sup.get("pid") and mc.pid_alive(sup["pid"])):
                    if mc.pending_user_messages(m):
                        mc.wake_supervisor(m)  # a message that arrived as the last turn was ending
                    elif m.meta().get("phase") not in mc.GATED_PHASES:
                        evs = mc.supervisor_events(m, advance=False)
                        # batch: wake once the newest event is a few seconds old, and only if it needs judgment
                        if (evs and time.time() - (mc.parse_ts(evs[-1]["ts"]) or 0) > 4
                                and mc.needs_decision(m, evs)):
                            mc.wake_supervisor(m)
                if m.meta().get("autopilot"):
                    mc.schedule(m)
            except Exception as e:  # keep the loop alive no matter what
                print(f"[loop] {path}: {e}", file=sys.stderr)
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    server_version = "MissionControl/1.0"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "t" in q and not TASK_ID.fullmatch(q["t"]):
            return self._send(400, {"error": "bad task id"})
        try:
            if u.path in ("/", "/index.html"):
                html = UI.read_text().replace("__MC_TOKEN__", TOKEN)
                return self._send(200, html, "text/html")
            if u.path == "/api/usage":
                m = get_mission(q["p"])
                return self._send(200, {"rows": mc.usage_rows(m), "titles": {t["id"]: t.get("title", "") for t in m.tasks()}})
            if u.path == "/api/usage-calls":
                m = get_mission(q["p"])
                for r in mc.usage_rows(m, with_calls=True):
                    if r.get("id") == q["id"]:
                        return self._send(200, {"calls": r.get("calls") or []})
                return self._send(200, {"calls": []})
            if u.path == "/api/usage-fleet":
                fleet = []
                for path, info in project_paths().items():
                    if initialized(path):
                        m = mc.Mission(path)
                        fleet.append({"path": path, "name": m.meta().get("name", info.get("name")), **usage_summary(mc.usage_rows(m))})
                return self._send(200, fleet)
            if u.path == "/api/usage.csv":
                m = get_mission(q["p"])
                return self._send(200, usage_csv(m), "text/csv")
            if u.path == "/api/config":
                return self._send(200, {"clone_dir": str(default_clone_dir()).replace(str(Path.home()), "~", 1)})
            if u.path == "/api/projects":
                return self._send(200, [summary(p, i) for p, i in project_paths().items()])
            if u.path == "/api/state":
                return self._send(200, full_state(get_mission(q["p"])))
            if u.path == "/api/log":
                return self._send(200, read_log(get_mission(q["p"]), q["t"], int(q.get("a", 1)), int(q.get("o", 0))))
            if u.path == "/api/brief":
                m = get_mission(q["p"])
                t = m.task(q["t"])
                return self._send(200, {"brief": mc.build_brief(m, t, human=mc.is_human(t))})
            if u.path == "/api/docs":
                m = get_mission(q["p"])
                return self._send(200, {"report": m.report_text(q["t"]), "feedback": m.feedback_text(q["t"])})
            return self._send(404, {"error": "not found"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": f"bad request: {e}"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-MC-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if u.path == "/api/add-project":
                raw = str(body.get("path", "")).strip().strip("'\"")
                if not raw:
                    return self._send(400, {"error": "enter a folder path or a GitHub repo"})
                cloned = False
                if looks_like_repo(raw):
                    try:
                        raw = str(clone_repo(raw, str(body.get("into", ""))))
                        cloned = True
                    except (RuntimeError, mc.subprocess.TimeoutExpired) as e:
                        return self._send(409, {"error": str(e) if isinstance(e, RuntimeError) else "Cloning took too long"})
                root = Path(raw).expanduser().resolve()
                if not root.is_dir():
                    return self._send(400, {"error": f"not a folder: {root}"})
                found = mc.find_root(root)
                if found and found != root:
                    root = found
                name = (mc.Mission(root).meta().get("name") if initialized(str(root)) else None) or root.name
                mc.register(root, name)
                return self._send(200, {"ok": True, "path": str(root), "initialized": initialized(str(root)), "cloned": cloned})
            if u.path == "/api/ignite":
                if body.get("p") not in project_paths():
                    return self._send(403, {"error": "unknown project"})
                mc.ignite(Path(body["p"]), body.get("name"), body.get("models"))
                return self._send(200, {"ok": True})
            if u.path == "/api/remove-project":
                before = mc.registry()
                keep = [p for p in before if p.get("path") != body.get("p")]
                mc.write_json(mc.MC_HOME / "projects.json", keep)  # only the list changes; no project files are touched
                LAST_SYNC.pop(str(body.get("p")), None)
                return self._send(200, {"ok": True, "removed": len(before) - len(keep)})
            m = get_mission(body["p"])
            tid = body.get("t")
            if tid is not None and not TASK_ID.fullmatch(str(tid)):
                return self._send(400, {"error": "bad task id"})
            if u.path == "/api/run":
                mc.launch(m, tid, force=True)
            elif u.path == "/api/run-ready":
                launched = mc.schedule(m)
                return self._send(200, {"ok": True, "launched": launched})
            elif u.path == "/api/stop":
                mc.stop_task(m, tid, "todo")
            elif u.path == "/api/cancel":
                mc.stop_task(m, tid, "cancelled")
            elif u.path == "/api/resume":
                m.update_task(tid, lambda t: t.update(paused=False))
            elif u.path == "/api/executor":
                if body["executor"] not in mc.load_runners():
                    return self._send(400, {"error": "unknown executor"})
                m.update_task(tid, lambda t: t.update(executor=body["executor"]))
                m.event("task_updated", "user", f"{tid} executor → {body['executor']}", task=tid)
            elif u.path == "/api/answer":
                text = str(body.get("answer", "")).strip()
                if not text:
                    return self._send(400, {"error": "empty answer"})
                mc.answer_question(m, body["q"], text, "user")
            elif u.path == "/api/note":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._send(400, {"error": "empty note"})
                mc.add_note(m, text, "user")
            elif u.path == "/api/chat":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._send(400, {"error": "empty message"})
                if (m.meta().get("supervisor") or {}).get("mode") == "dashboard":
                    mc.chat_add(m, "user", text)
                    mc.wake_supervisor(m)
                else:
                    mc.chat_add(m, "user", text, delivered=True)
                    mc.add_note(m, text, "user")
            elif u.path == "/api/models":
                runners = mc.load_runners()
                for k in ("sup_runner", "exec_runner"):
                    if body.get(k) and body[k] not in runners:
                        return self._send(400, {"error": f"unknown runner {body[k]}"})
                mc.set_models(m, body.get("sup_runner"), body.get("sup_model"), body.get("exec_runner"), body.get("exec_model"),
                              body.get("sup_effort"), body.get("exec_effort"))
            elif u.path == "/api/assign":
                who = str(body.get("to", "")).strip().lstrip("@")
                if not who or not all(ch.isalnum() or ch in "-_:." for ch in who):
                    return self._send(400, {"error": "enter a GitHub username or an executor"})
                runners = mc.load_runners()
                if who.split(":")[0] in runners:
                    exe, _, model = who.partition(":")
                    m.update_task(tid, lambda t: t.update(executor=exe, model=model, assignee=""))
                else:
                    m.update_task(tid, lambda t: t.update(executor="human", assignee=who))
                m.event("task_updated", "user", f"{tid} assigned to {'@' + who if who.split(':')[0] not in runners else who}", task=tid)
            elif u.path == "/api/done":
                mc.mark_done(m, tid, str(body.get("note", "")).strip(), "user")
            elif u.path == "/api/sync":
                LAST_SYNC[str(m.root)] = time.time()
                return self._send(200, {"ok": True, "changes": mc.sync_humans(m)})
            elif u.path == "/api/pr":
                try:
                    url = mc.open_pr(m, "user", body.get("target") or None)
                except mc.gitops.BaseMissing as e:
                    return self._send(409, {"error": str(e), "suggest_target": e.suggest})
                except mc.gitops.GitError as e:
                    return self._send(409, {"error": str(e)})
                return self._send(200, {"ok": True, "url": url})
            elif u.path == "/api/permission":
                rule = str(body.get("rule", "")).strip()
                if body.get("action") == "remove":
                    mc.perm_remove(m, rule, "user")
                else:
                    if not mc.perm_valid(rule):
                        return self._send(400, {"error": "Use a rule like Bash(/path/to/python *) or WebSearch"})
                    mc.perm_add(m, [rule], "user")
            elif u.path == "/api/reveal":
                target = m.dir
                if body.get("what") == "report":
                    cand = m.dir / "reports" / f"{tid}.md"
                    target = cand if cand.exists() and tid and TASK_ID.fullmatch(tid) else m.dir / "reports"
                if not str(target.resolve()).startswith(str(m.dir.resolve())):
                    return self._send(400, {"error": "not inside the mission folder"})
                if not compat.reveal(target):
                    return self._send(400, {"error": "can't open folders on this system", "path": str(target)})
                return self._send(200, {"ok": True, "path": str(target)})
            elif u.path == "/api/watch-cancel":
                w = next((x for x in mc.watch_list(m) if x["id"] == str(body.get("id"))), None)
                if w and w.get("status") == "active":
                    w["status"] = "cancelled"
                    mc.watch_save(m, w)
                    m.event("watch_cancelled", "user", f"stopped waiting for: {w['name']}", watch=w["id"])
            elif u.path == "/api/limits":
                mc.set_limits(m, body.get("max_attempts"), body.get("budget"), body.get("max_turns"), body.get("sup_turns"))
            elif u.path == "/api/approve":
                launched = mc.approve_plan(m, "user", body.get("allow") if isinstance(body.get("allow"), list) else None)
                return self._send(200, {"ok": True, "launched": launched})
            elif u.path == "/api/supervisor":
                kw = {}
                if body.get("mode") in ("dashboard", "external"):
                    kw["mode"] = body["mode"]
                if body.get("runner") in mc.load_runners():
                    kw["runner"] = body["runner"]
                if body.get("reset"):
                    kw["session_id"] = None
                mc.sup_update(m, **kw)
                if body.get("reset"):
                    mc.chat_add(m, "system", "Supervisor session reset. The next turn starts fresh from the mission state.")
            elif u.path == "/api/autopilot":
                meta = m.meta()
                meta["autopilot"] = bool(body.get("on"))
                m.save_meta(meta)
                m.event("autopilot", "user", f"autopilot {'on' if meta['autopilot'] else 'off'}")
            elif u.path == "/api/parallel":
                meta = m.meta()
                meta["max_parallel"] = max(1, min(12, int(body["n"])))
                m.save_meta(meta)
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True})
        except RuntimeError as e:
            return self._send(409, {"error": str(e)})
        except SystemExit:
            return self._send(400, {"error": "invalid request"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": f"bad request: {e}"})


def holder_pid(port: int):
    """PID of the process listening on the port (pidfile first, then lsof)."""
    pf = mc.MC_HOME / f"dashboard-{port}.pid"
    try:
        pid = int(pf.read_text().strip())
        if mc.pid_alive(pid):
            return pid
    except (OSError, ValueError):
        pass
    return compat.port_holder(port)


def is_dashboard(pid: int) -> bool:
    return "dashboard.py" in compat.process_command(pid)


class Server(ThreadingHTTPServer):
    allow_reuse_address = not compat.IS_WIN  # on Windows SO_REUSEADDR would let a second dashboard share the port


def bind(port: int) -> ThreadingHTTPServer:
    """Start the server; if an older Mission Control dashboard holds the port, replace it."""
    try:
        return Server(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno not in compat.ADDR_IN_USE:
            raise
    pid = holder_pid(port)
    if not pid or pid == os.getpid() or not is_dashboard(pid):
        print(f"Port {port} is used by another program. Start on a different port: mc.py dashboard --port {port + 11}",
              file=sys.stderr)
        sys.exit(1)
    print(f"Replacing the Mission Control dashboard already running on port {port} (pid {pid})…", flush=True)
    compat.terminate_pid(pid)
    for _ in range(50):
        time.sleep(0.1)
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            continue
    compat.terminate_pid(pid, force=True)
    time.sleep(0.3)
    return Server(("127.0.0.1", port), Handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--open", action="store_true")
    a = ap.parse_args()
    for path in project_paths():  # repair costs written by older versions
        try:
            if initialized(path):
                mc.fix_supervisor_costs(mc.Mission(path))
        except Exception as e:
            print(f"[cost repair] {path}: {e}", file=sys.stderr)
    srv = bind(a.port)
    pf = mc.MC_HOME / f"dashboard-{a.port}.pid"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    threading.Thread(target=supervisor_loop, daemon=True).start()
    url = f"http://127.0.0.1:{a.port}/"
    n = len(project_paths())
    print(f"Mission Control dashboard → {url}  ({n} project{'' if n == 1 else 's'})  · Ctrl+C to stop", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if pf.read_text().strip() == str(os.getpid()):
                pf.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
