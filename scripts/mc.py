#!/usr/bin/env python3
"""Mission Control: a file-based supervisor/executor protocol for AI agents.

All state lives in <project>/.mission/. Any agent that can read and write files
(or run this script) can take part. Stdlib only; Python 3.9+.

Run `python3 mc.py -h` for commands.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compat  # noqa: E402
compat.augment_path()
import gitops  # noqa: E402
import notify  # noqa: E402
import snapshots  # noqa: E402

if __name__ == "__main__":
    compat.ensure_utf8()
SKILL_DIR = HERE.parent
MC_HOME = Path(os.environ.get("MC_HOME", str(Path.home() / ".mission-control")))

STATUSES = ["todo", "running", "review", "needs_changes", "blocked", "done", "cancelled"]
RUNNABLE = {"todo", "needs_changes"}
FINAL = {"done", "cancelled"}
SPAWNED: list = []
# Session/nesting variables that would make a child `claude` attach to the parent session.
# Auth/provider variables (CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_OAUTH_TOKEN, ...) are kept.
STRIP_ENV = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
             "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_CODE_HOST_SESSION_ID",
             "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_TERMINAL_MCP_TOOLS", "CLAUDE_CODE_EXECPATH"}
REPORT_RE = re.compile(r"<<<REPORT\s*(\{.*?\})\s*REPORT>>>", re.S)
PROGRESS_RE = re.compile(r"PROGRESS:\s*(\d{1,3})\s*%?\s*(.*)")


# ───────────────────────────── helpers ─────────────────────────────

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p, data) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    compat.replace_file(tmp, p)


def die(msg: str, code: int = 1):
    print(f"mc: {msg}", file=sys.stderr)
    sys.exit(code)


def pid_alive(pid) -> bool:
    try:
        return compat.pid_alive(pid)
    except ValueError:
        return False


def short(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def stop_tree(proc) -> None:
    """Stop a headless CLI and anything it left running in the background (its helper scripts, sleeps)."""
    compat.stop_tree(proc)


def watch_for_exit_after_result(proc, grace: float | None = None) -> threading.Thread:
    """The CLI has printed its final result. It normally exits at once; if it is still alive after `grace`
    seconds it is only waiting on background tasks nobody will read, so shut it down instead of hanging."""
    grace = float(os.environ.get("MC_RESULT_GRACE", 15)) if grace is None else grace

    def run():
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            stop_tree(proc)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    if compat.IS_WIN:
        env.setdefault("PYTHONUTF8", "1")
    return env


# ───────────────────────────── registry ─────────────────────────────

def registry() -> list:
    return read_json(MC_HOME / "projects.json", [])


def register(root: Path, name: str) -> None:
    projects = [p for p in registry() if p.get("path") != str(root)]
    projects.append({"path": str(root), "name": name, "added": now()})
    write_json(MC_HOME / "projects.json", projects)


def load_runners() -> dict:
    runners = read_json(SKILL_DIR / "runners.default.json", {})
    user = read_json(MC_HOME / "runners.json", {})
    for k, v in user.items():
        if v is None:
            runners.pop(k, None)
        else:
            runners[k] = {**runners.get(k, {}), **v}
    return runners


# AI CLIs Mission Control can drive: runner name -> (executable, how to get it, how to sign in)
PROVIDERS = {
    "claude": ("claude", "npm i -g @anthropic-ai/claude-code", "run `claude` once and sign in"),
    "codex": ("codex", "npm i -g @openai/codex", "run `codex` once and sign in"),
    "gemini": ("gemini", "npm i -g @google/gemini-cli", "run `gemini` once and sign in, or set GEMINI_API_KEY"),
}


def detect_providers() -> dict:
    """{name: path or None} for every supported AI CLI."""
    return {k: shutil.which(v[0]) for k, v in PROVIDERS.items()}


def default_provider() -> str:
    """The provider new projects should use: Claude if installed, else the first installed one, else Claude."""
    found = detect_providers()
    return "claude" if found.get("claude") else next((k for k, v in found.items() if v), "claude")


def default_supervisor_runner() -> str:
    return default_provider() + "-supervisor"


# Older names kept working: name -> (runner, model)
RUNNER_ALIASES = {"claude-haiku": ("claude", "haiku"), "claude-sonnet": ("claude", "sonnet"), "claude-opus": ("claude", "opus"),
                  "supervisor-opus": ("claude-supervisor", "opus"), "supervisor-sonnet": ("claude-supervisor", "sonnet")}


def resolve_runner(name: str | None, model: str | None = None, runners: dict | None = None):
    """Returns (runner_name, runner_dict, model) or (name, None, model) if unknown."""
    runners = runners or load_runners()
    name = name or ""
    if name not in runners and name in RUNNER_ALIASES:
        name, alias_model = RUNNER_ALIASES[name]
        model = model or alias_model
    r = runners.get(name)
    return name, r, (model or (r or {}).get("model") or "")


def build_cmd(runner: dict, subs: dict, model: str, effort: str = "", budget: str = "") -> list:
    subs = {**subs, "model": model, "effort": effort, "budget": budget}
    out = []
    for a in runner["cmd"]:
        if a == "{model_args}":
            if model:
                out += [str(x).format(**subs) for x in runner.get("model_args", [])]
        elif a == "{budget_args}":
            if budget and runner.get("budget_args"):
                out += [str(x).format(**subs) for x in runner["budget_args"]]
        elif a == "{effort_args}":
            if effort and runner.get("effort_args") and (not runner.get("efforts") or effort in runner["efforts"]):
                out += [str(x).format(**subs) for x in runner["effort_args"]]
        else:
            out.append(str(a).format(**subs))
    return out


def model_label(runner_name: str, model: str) -> str:
    return f"{runner_name.replace('-supervisor', '')}:{model}" if model else runner_name.replace("-supervisor", "")


# ───────────────────────────── mission ─────────────────────────────

def find_root(start=None) -> Path | None:
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / ".mission" / "mission.json").exists():
            return d
    return None


class Mission:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.dir = self.root / ".mission"

    # ---- low level
    @contextlib.contextmanager
    def lock(self):
        self.dir.mkdir(exist_ok=True)
        with open(self.dir / ".lock", "a") as f:
            compat.lock(f)
            try:
                yield
            finally:
                compat.unlock(f)

    def meta(self) -> dict:
        return read_json(self.dir / "mission.json", {})

    def save_meta(self, m: dict) -> None:
        write_json(self.dir / "mission.json", m)

    def event(self, kind: str, actor: str, msg: str, task: str | None = None, **extra) -> None:
        ev = {"ts": now(), "kind": kind, "actor": actor, "msg": msg}
        if task:
            ev["task"] = task
        ev.update(extra)
        with open(self.dir / "events.jsonl", "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        notify.on_event(self, ev)

    def events(self, since: int = 0) -> list:
        try:
            lines = (self.dir / "events.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[since:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    # ---- tasks
    def tasks(self) -> list:
        out = []
        for p in sorted((self.dir / "tasks").glob("T-*.json")):
            t = read_json(p)
            if t:
                out.append(t)
        return out

    def task(self, tid: str) -> dict:
        t = read_json(self.dir / "tasks" / f"{tid}.json")
        if not t:
            die(f"no task {tid}")
        return t

    def save_task(self, t: dict) -> None:
        t["updated"] = now()
        write_json(self.dir / "tasks" / f"{t['id']}.json", t)

    def update_task(self, tid: str, fn) -> dict:
        with self.lock():
            t = self.task(tid)
            fn(t)
            self.save_task(t)
            return t

    def next_id(self, prefix: str, folder: str) -> str:
        nums = [int(p.stem.split("-")[1]) for p in (self.dir / folder).glob(f"{prefix}-*.json")]
        return f"{prefix}-{(max(nums) + 1) if nums else 1:03d}"

    # ---- questions
    def questions(self) -> list:
        return [q for q in (read_json(p) for p in sorted((self.dir / "questions").glob("Q-*.json"))) if q]

    def save_question(self, q: dict) -> None:
        write_json(self.dir / "questions" / f"{q['id']}.json", q)

    def notes(self) -> list:
        try:
            return [json.loads(l) for l in (self.dir / "notes.jsonl").read_text().splitlines() if l.strip()]
        except FileNotFoundError:
            return []

    def report_text(self, tid: str) -> str:
        p = self.dir / "reports" / f"{tid}.md"
        return p.read_text() if p.exists() else ""

    def feedback_text(self, tid: str) -> str:
        p = self.dir / "feedback" / f"{tid}.md"
        return p.read_text() if p.exists() else ""


def need_mission(path=None) -> Mission:
    root = find_root(path)
    if not root:
        die("no .mission/ here or in any parent. Run `mc.py init` first.")
    return Mission(root)


def model_price(runner_name: str, model: str):
    """List price (input $/M tokens) used only to tell cheaper from more expensive. None = unknown."""
    r = load_runners().get(runner_name) or {}
    return (r.get("model_price") or {}).get(model)


def check_model_choice(m: Mission, executor: str | None, model: str | None) -> tuple:
    """May the supervisor pick this executor/model for a task on its own? Cheaper than the mission's
    executor model: yes. Anything else: only after the user approves (`mc request-model`)."""
    if os.environ.get("MC_ROLE") != "supervisor" or m.meta().get("allow_model_overrides"):
        return True, ""
    meta = m.meta()
    base_name, _, base_model = resolve_runner(meta.get("default_executor", "claude"), meta.get("executor_model"))
    tgt_name, _, tgt_model = resolve_runner(executor or meta.get("default_executor", "claude"),
                                            model or (meta.get("executor_model") if not executor else ""))
    if tgt_name != base_name:
        return False, f"a different provider ({tgt_name}) than the one the user chose ({base_name})"
    pb, pt = model_price(base_name, base_model), model_price(tgt_name, tgt_model)
    if pb is None or pt is None:
        return False, f"{tgt_name}:{tgt_model} has no known price to compare with {base_model}"
    if pt <= pb:
        return True, ""
    return False, f"{tgt_model} costs about {pt / pb:.0f}x the executor model the user chose ({base_model})"


MODEL_NOTE = ("Cheaper models are fine to choose yourself. For a more expensive one, ask the user first: "
              "mc request-model <model> T-00x --why \"<why this task needs it>\"")


def new_task(m: Mission, spec: dict, tid: str) -> dict:
    meta = m.meta()
    if (spec.get("model") or spec.get("executor")) and not spec.get("assignee"):
        ok_, why = check_model_choice(m, spec.get("executor"), spec.get("model"))
        if not ok_:
            print(f"note: ignored the model/executor on '{spec.get('title', tid)}': {why}. {MODEL_NOTE}", file=sys.stderr)
            spec = {k: v for k, v in spec.items() if k not in ("model", "executor")}
    return {
        "id": tid,
        "title": spec.get("title") or tid,
        "goal": spec.get("goal", ""),
        "accept": list(spec.get("accept") or []),
        "files": list(spec.get("files") or []),
        "depends_on": list(spec.get("depends_on") or spec.get("deps") or []),
        "executor": "human" if spec.get("assignee") else (spec.get("executor") or meta.get("default_executor", "claude")),
        "assignee": str(spec.get("assignee") or "").lstrip("@"),
        "model": spec.get("model") or "",
        "estimate": int(spec.get("estimate") or 2),
        "check": spec.get("check") or "",
        "status": "todo",
        "progress": 0,
        "step": "",
        "paused": False,
        "attempts": [],
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "source": "none"},
        "created": now(),
        "updated": now(),
    }


DEFAULT_MAX_ATTEMPTS = 3


def attempt_cap(m: Mission, t: dict) -> int:
    """Attempts a task may use before someone has to decide: the mission limit plus any the user granted."""
    return int(m.meta().get("max_attempts") or DEFAULT_MAX_ATTEMPTS) + int(t.get("attempt_bonus") or 0)


def open_questions_for(m: Mission, tid: str, kind: str | None = None) -> list:
    return [q for q in m.questions() if q.get("task") == tid and q.get("status") == "open"
            and (kind is None or (kind == "permission" and q.get("permission")) or q.get("kind") == kind)]


def blocked_reason(m: Mission, t: dict) -> str:
    """Why a task must not be started automatically ('' = free to run)."""
    used, cap = len(t.get("attempts", [])), attempt_cap(m, t)
    if used >= cap:
        return f"{t['id']} has used {used} of {cap} attempts. Change something (model, brief, split it) and ask the user to allow more."
    perm = open_questions_for(m, t["id"], "permission")
    if perm:
        return f"{t['id']} is waiting for the user's decision on {perm[0]['id']} (a permission request)."
    return ""


def ensure_attempt_limit_cards(m: Mission) -> list:
    """A task that is held back by the attempt limit must always have a visible decision for the user, otherwise it
    just looks stuck. Covers tasks that reached the limit before limits existed and tasks the supervisor sent back."""
    made = []
    for t in m.tasks():
        if t["status"] not in ("todo", "needs_changes") or t.get("paused"):
            continue
        used, cap = len(t.get("attempts", [])), attempt_cap(m, t)
        if used < cap or open_questions_for(m, t["id"], "attempt_limit"):
            continue
        qid = m.next_id("Q", "questions")
        m.save_question({"id": qid, "task": t["id"], "from": "system", "to": "user", "kind": "attempt_limit", "extra": 2,
                         "question": f"{t['id']} ({short(t.get('title', ''), 50)}) has used {used} attempts without finishing, and the limit is {cap}. "
                                     "Nothing will retry it automatically. Allow 2 more attempts, or stop here?",
                         "options": ["Allow 2 more attempts", "Stop this task"], "recommendation": "", "status": "open",
                         "answer": "", "created": now()})
        m.update_task(t["id"], lambda x: x.update(step=f"attempt limit reached ({used}): waiting for your decision"))
        m.event("question", "system", f"{t['id']} is held at its attempt limit ({used} of {cap})", task=t["id"], question=qid, to="user")
        made.append(qid)
    return made


def ready_tasks(m: Mission, tasks: list | None = None) -> list:
    tasks = tasks if tasks is not None else m.tasks()
    by_id = {t["id"]: t for t in tasks}
    out = []
    for t in tasks:
        if t["status"] not in RUNNABLE or t.get("paused"):
            continue
        if blocked_reason(m, t):
            continue
        if all(by_id.get(d, {}).get("status") == "done" for d in t.get("depends_on", [])):
            out.append(t)
    return out


# ───────────────────────────── permissions ─────────────────────────────

def perm_list(m: Mission) -> list:
    return list((m.meta().get("permissions") or {}).get("allow") or [])


def perm_valid(pat: str) -> bool:
    """A Claude Code permission rule: `Tool` or `Tool(specifier)`. No commas (they separate rules)."""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\([^,\n]{1,300}\))?", pat.strip()))


def perm_add(m: Mission, patterns: list, by: str) -> list:
    added = []
    with m.lock():
        meta = m.meta()
        allow = meta.setdefault("permissions", {}).setdefault("allow", [])
        for pt in patterns:
            pt = pt.strip()
            if perm_valid(pt) and pt not in allow:
                allow.append(pt)
                added.append(pt)
        m.save_meta(meta)
    if added:
        m.event("permission", by, "allowed for this project: " + ", ".join(added))
    return added


def perm_remove(m: Mission, pattern: str, by: str) -> bool:
    with m.lock():
        meta = m.meta()
        allow = (meta.get("permissions") or {}).get("allow") or []
        if pattern not in allow:
            return False
        allow.remove(pattern)
        m.save_meta(meta)
    m.event("permission", by, f"no longer allowed: {pattern}")
    return True


# Sandboxed full access: every shell command runs without asking, but inside Claude Code's OS sandbox,
# which can only write to the working folder and reach the internet domains listed here.
DEFAULT_DOMAINS = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com", "*.githubusercontent.com"]
SANDBOX_DENY_READ = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/.docker", "~/.kube",
                     # only the private parts of Mission Control's own folder: `mc` must stay runnable
                     str(MC_HOME / "today"), str(MC_HOME / "notify.json"), str(MC_HOME / "notify.log"), str(MC_HOME / ".sandbox-probe")]
SANDBOX_TMP = ["/tmp", "/private/tmp"]  # test servers often put Unix sockets here (macOS caps socket paths at 104 bytes)
SANDBOX_TOOLS = "Read,Edit,Write,Glob,Grep,NotebookEdit,TodoWrite,Bash"


def sandbox_supported() -> bool:
    if sys.platform == "darwin":
        return True
    return sys.platform.startswith("linux") and bool(shutil.which("bwrap")) and bool(shutil.which("socat"))


def snapshots_on(m: Mission) -> bool:
    return m.meta().get("snapshots", True) is not False and snapshots.available()


def access_mode(m: Mission) -> str:
    """'sandbox' (default where the OS supports it) or 'allowlist' (the older per-command rules)."""
    want = m.meta().get("access") or "sandbox"
    return "sandbox" if want == "sandbox" and sandbox_supported() else "allowlist"


def net_domains(m: Mission) -> list:
    meta = m.meta()
    doms = meta["net_domains"] if isinstance(meta.get("net_domains"), list) else list(DEFAULT_DOMAINS)
    return doms + [r[7:-1] for r in perm_list(m) if r.startswith("Domain(") and r.endswith(")")]


def sandbox_settings(m: Mission) -> str:
    probe = MC_HOME / ".sandbox-probe"
    if not probe.exists():
        MC_HOME.mkdir(parents=True, exist_ok=True)
        probe.write_text("Mission Control hides this file from sandboxed agents, so `mc` can tell where it runs.\n")
    return json.dumps({"sandbox": {
        "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
        "excludedCommands": ["mc", "mc *"],  # Mission Control's own CLI launches executors, so it runs outside
        "network": {"allowedDomains": sorted(set(net_domains(m))), "allowUnixSockets": SANDBOX_TMP},
        "filesystem": {"denyRead": SANDBOX_DENY_READ, "allowWrite": SANDBOX_TMP},
    }})


def apply_permissions(m: Mission, cmd: list) -> list:
    """Add the project's access to a headless Claude command (its own settings.json is ignored in folders
    Claude hasn't been told to trust, so everything must travel on the command line)."""
    extra = [x for x in perm_list(m) if perm_valid(x) and not x.startswith("Domain(")]
    if "--allowedTools" not in cmd:
        return cmd
    i = cmd.index("--allowedTools") + 1
    if access_mode(m) == "sandbox" and Path(cmd[0]).name in ("claude", "claude.exe"):
        mine = [x for x in cmd[i].split(",") if x.startswith("Bash(mc")]
        cmd[i] = ",".join([SANDBOX_TOOLS, *mine, *[x for x in extra if not x.startswith("Bash(")]])
        if "--disallowedTools" in cmd:  # installs are fine: the sandbox decides where they can write
            j = cmd.index("--disallowedTools")
            del cmd[j:j + 2]
        cmd += ["--settings", sandbox_settings(m)]
    elif extra and i < len(cmd):
        cmd[i] += "," + ",".join(extra)
    return cmd


# Tools where the first word alone is far too broad: suggest `git fetch *`, not `git *`.
SUBCOMMAND_TOOLS = {"git", "gh", "npm", "npx", "pnpm", "yarn", "docker", "kubectl", "brew", "pip", "pip3", "cargo", "go", "make", "uv", "poetry"}
SAFE_FIRST = {"cd", "export", "cat", "ls", "echo", "test", "true", "false", "mkdir", "pwd", "printf", "sleep", "wc", "head", "tail", "grep", "sed", "awk", "sort", "uniq", "tr", "cut", "diff", "["}
RISKY = {  # what the user is agreeing to, shown on the card
    "git push": "publishes to a remote", "git fetch": "downloads from a remote (changes nothing locally)",
    "git worktree": "creates or removes extra checkouts of the repo", "git checkout": "switches files or branches",
    "git reset": "can discard work", "git clean": "deletes untracked files", "git branch": "creates or deletes branches",
    "git commit": "records changes", "git merge": "merges branches", "git rebase": "rewrites history",
    "gh pr": "creates or changes pull requests", "gh repo": "changes repositories", "gh issue": "creates or changes issues",
    "gh api": "calls the GitHub API", "rm": "deletes files", "sudo": "runs as administrator", "curl": "fetches from the internet",
    "pip": "installs packages", "npm install": "installs packages", "docker": "runs containers",
}


# Commands that publish or change things on other people's systems: allow exactly the command asked for, nothing wider.
EXACT_ONLY = {"git push", "gh pr", "gh api", "gh repo", "gh issue", "git reset", "git clean", "git rebase"}


def risk_note(rule: str) -> str:
    body = rule[5:-1] if rule.startswith("Bash(") and rule.endswith(")") else rule
    body = body.removesuffix(" *").removesuffix("*").strip()
    if body.startswith("git push") and re.search(r"(^| )(--force|--force-with-lease|-f)( |$)", body):
        return "FORCE-pushes: overwrites history on the remote"
    for key in sorted(RISKY, key=len, reverse=True):
        if body == key or body.startswith(key + " ") or body.split(" ")[0] == key:
            return RISKY[key]
    return ""


TOOL_RISK = {
    "WebSearch": ("Search the web", "low", "Sends your search words to a search engine. Nothing on your computer changes."),
    "WebFetch": ("Open web pages", "medium", "Downloads pages from the internet. A page can contain text that tries to steer the agent."),
    "Read": ("Read files", "low", "Reads files. Changes nothing."), "Glob": ("Find files", "low", "Lists file names. Changes nothing."),
    "Grep": ("Search inside files", "low", "Reads files. Changes nothing."),
    "Edit": ("Edit files", "low", "Changes files inside the project."), "Write": ("Write files", "low", "Creates or overwrites files."),
    "NotebookEdit": ("Edit notebooks", "low", "Changes notebook files."),
}
HIGH_RISK = {"git push", "gh pr", "gh api", "gh repo", "gh issue", "git reset", "git clean", "git rebase", "rm", "sudo", "curl", "docker", "kubectl"}
LOW_RISK = {"git fetch", "git commit", "git branch", "git worktree", "git checkout", "git merge", "git log", "git show"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh", "php", "deno", "bun"}
# Commands the executor already may run, so a plan never needs to ask for them.
DEFAULT_ALLOWED_PREFIXES = ("ls", "cat", "git status", "git diff", "npm test", "npm run", "pytest", "python3 -m")
DEFAULT_ALLOWED_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep"}


def covered_by_defaults(rule: str) -> bool:
    if rule in DEFAULT_ALLOWED_TOOLS:
        return True
    if rule.startswith("Bash(") and rule.endswith(")"):
        body = rule[5:-1].removesuffix("*").strip()
        return any(body == p or body.startswith(p + " ") for p in DEFAULT_ALLOWED_PREFIXES)
    return False


def perm_info(rule: str) -> dict:
    """Plain-language label, risk level (low/medium/high) and one-line risk for a permission rule."""
    if rule in TOOL_RISK:
        label, level, text = TOOL_RISK[rule]
        return {"rule": rule, "label": label, "level": level, "risk": text}
    body = rule[5:-1] if rule.startswith("Bash(") and rule.endswith(")") else rule
    body = body.removesuffix(" *").removesuffix("*").strip()
    words = body.split()
    first = os.path.basename(words[0]) if words else body
    sub2 = " ".join(words[:2])
    label = "Run `" + (body or rule) + "`"
    note = risk_note(rule)
    if note:
        if body.startswith("git push") and re.search(r"(^| )(--force|--force-with-lease|-f)( |$)", body):
            return {"rule": rule, "label": label, "level": "high", "risk": "Overwrites history on the remote. Other people's work can be lost."}
        level = "high" if (sub2 in HIGH_RISK or first in HIGH_RISK) else "low" if sub2 in LOW_RISK else "medium"
        return {"rule": rule, "label": label, "level": level, "risk": note[0].upper() + note[1:] + "."}
    if re.match(r"python\d?(\.\d+)?$", first) or first in INTERPRETERS or first.endswith((".py", ".sh")):
        return {"rule": rule, "label": label, "level": "medium",
                "risk": "Runs scripts. A script can do anything you can do on this computer, so only allow it for code you trust."}
    return {"rule": rule, "label": label, "level": "medium",
            "risk": f"Runs `{first or rule}` on your computer. Check that you trust it."}


def perm_infos(rules: list) -> list:
    return [perm_info(r) for r in rules or []]


def plan_permission_rules(m: Mission) -> list:
    """What a plan will need that isn't allowed yet: what the supervisor declared plus the tasks' own check commands."""
    have = set(perm_list(m))
    found = {}
    for d in (m.meta().get("plan_permissions") or []):
        r = d.get("rule", "") if isinstance(d, dict) else str(d)
        if perm_valid(r):
            found.setdefault(r, (d.get("why", "") if isinstance(d, dict) else ""))
    for t in m.tasks():
        if t.get("status") == "cancelled" or not t.get("check"):
            continue
        for r in suggest_permissions([{"tool": "Bash", "input": {"command": t["check"]}}]):
            found.setdefault(r, f"{t['id']} runs its check with it")
    out, seen = [], set()
    for r, w in found.items():  # `~/x` and its expanded form are the same permission: show one
        key = r.replace("(~", "(" + str(Path.home()))
        if r in have or covered_by_defaults(r) or key in seen:
            continue
        seen.add(key)
        out.append({"rule": r, "why": w, **perm_info(r)})
    return out


def _split_commands(cmd: str) -> list:
    """Split a shell line into its commands on && || ; | but never inside quotes (a quoted Python or sed
    program is not a list of commands)."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return [cmd]
    parts, cur = [], []
    for t in toks:
        if t and set(t) <= set("&|;()") and t.strip():
            if cur:
                parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return parts


def suggest_permissions(raw: list) -> list:
    """Turn denied tool calls into the smallest sensible allow rules."""
    out = []
    for d in raw or []:
        tool, inp = d.get("tool", ""), d.get("input") or {}
        if tool == "Bash":
            for words in _split_commands(str(inp.get("command", ""))):
                words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)] or words  # drop VAR=value prefixes
                if not words:
                    continue
                first = words[0]
                if first in SAFE_FIRST or not re.match(r"^[~/A-Za-z0-9_.\-]+$", first):
                    continue  # not a command name we can sensibly allow (e.g. code fragments)
                rest = list(words[1:])
                if first in SUBCOMMAND_TOOLS:
                    while rest and rest[0] in ("-C", "-c") and len(rest) > 1:  # git -C <path> <subcommand>
                        rest = rest[2:]
                    sub = next((w for w in rest if re.match(r"^[a-z][a-z0-9-]*$", w)), "")
                    variants = [f"{first} {sub}"] if sub else [first]
                    if sub and f"{first} {sub}" in EXACT_ONLY:
                        full = " ".join(words)
                        if all(re.match(r"^[\w./:@=+-]+$", w) for w in words) and len(full) < 200 and perm_valid(f"Bash({full})"):
                            out.append(f"Bash({full})")  # exactly this command, no wildcard
                        continue  # publishing commands are never allowed with a wildcard
                else:
                    variants = list({first, os.path.expanduser(first)})
                for v in variants:
                    rule = f"Bash({v} *)"
                    if perm_valid(rule):
                        out.append(rule)
        elif tool:
            out.append(tool)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:8]


# ───────────────────────────── executor brief ─────────────────────────────

def is_human(t: dict) -> bool:
    return t.get("executor") == "human"


def build_brief(m: Mission, t: dict, human: bool = False, workdir=None, conflicts=None) -> str:
    meta = m.meta()
    by_id = {x["id"]: x for x in m.tasks()}
    g = meta.get("git") or {}
    if human:
        lines = [f"# {t['id']}: {t['title']}", "",
                 f"You've been assigned this task by the Mission Control supervisor for **{meta.get('name', 'this mission')}**.", ""]
    else:
        proto = (SKILL_DIR / "EXECUTOR.md").read_text() if (SKILL_DIR / "EXECUTOR.md").exists() else ""
        lines = [proto.strip(), "", "---", "", f"# Your task: {t['id']} — {t['title']}", ""]
    if meta.get("goal"):
        lines += [f"Mission (context only): {meta['goal']}", ""]
    if not human:
        lines += [f"Project root: {workdir or m.root}", ""]
        if g.get("enabled"):
            lines += ["You are working in a git worktree of the project. Do not run git commands (no commit, branch, "
                      "checkout or push); your changes are committed for you when you finish.", ""]
        if conflicts:
            lines += ["## Merge conflicts to resolve first",
                      "Other tasks changed the same code. Resolve the conflict markers (<<<<<<< / >>>>>>>) in: "
                      + ", ".join(conflicts) + ". Keep both sides' intent.", ""]
    lines += ["## Goal", t.get("goal") or t["title"], ""]
    if t.get("accept"):
        lines += ["## Acceptance criteria"] + [f"- {a}" for a in t["accept"]] + [""]
    if t.get("files"):
        lines += ["## Files you should focus on"] + [f"- {f}" for f in t["files"]] + [""]
    if t.get("check"):
        lines += ["## Automatic check", f"After you finish, `{t['check']}` will be run. Make it pass. "
                  "To verify, run exactly that command, character for character (it is pre-approved; variations may be denied).", ""]
    deps = [by_id[d] for d in t.get("depends_on", []) if d in by_id]
    if deps:
        lines.append("## Already done by other executors (you can rely on these)")
        for d in deps:
            lines.append(f"- {d['id']} {d['title']}: {short(d.get('summary', ''), 200)}")
        lines.append("")
    if t.get("attempts"):
        prev = m.report_text(t["id"])
        if prev:
            lines += ["## Your previous attempt's report", prev.strip(), ""]
    fb = m.feedback_text(t["id"])
    if fb:
        lines += ["## Supervisor feedback (address all of it)", fb.strip(), ""]
    answered = [q for q in m.questions() if q.get("task") == t["id"] and q.get("status") == "answered"]
    if answered and not human:
        lines.append("## Answers to your earlier questions")
        for q in answered:
            lines.append(f"- Q: {q['question']}\n  A: {q['answer']}")
        lines.append("")
    if human:
        lines.append("## How to deliver")
        if g.get("enabled"):
            br = gitops.task_branch(m, t["id"])
            lines += [f"1. `git fetch && git checkout {br}` (this branch already contains the work this task builds on).",
                      f"2. Commit your work and push the branch.",
                      f"3. Open a pull request from `{br}` into **`{g['branch']}`** (the mission branch, not `{g.get('base', 'main')}`).",
                      "4. Questions? Comment on this issue; the supervisor answers here.", "",
                      "The supervisor reviews your pull request against the acceptance criteria above."]
        else:
            lines += ["When you're done, tell the person running the mission so they can mark it done."]
    return "\n".join(lines)


# ───────────────────────────── launching ─────────────────────────────

def git_save(m: Mission):
    def save(c: dict) -> None:
        with m.lock():
            meta = m.meta()
            meta["git"] = {**(meta.get("git") or {}), **c}
            m.save_meta(meta)
    return save


def ensure_git(m: Mission) -> bool:
    """Turn on git mode the first time work starts in a git repo."""
    try:
        return gitops.setup(m, git_save(m)) is not None
    except gitops.GitError as e:
        git_save(m)({"enabled": False, "disabled": True, "reason": str(e)})
        m.event("error", "system", f"git mode is off: {short(str(e), 200)}")
        return False


def launch_human(m: Mission, tid: str) -> int:
    with m.lock():
        t = m.task(tid)
        if t["status"] == "running":
            raise RuntimeError(f"{tid} is already with @{t.get('assignee')}")
        if t["status"] in FINAL:
            raise RuntimeError(f"{tid} is {t['status']}")
        if m.meta().get("phase") in GATED_PHASES:
            raise RuntimeError("the plan isn't approved yet")
        who = t.get("assignee") or "someone"
        attempt = len(t.get("attempts", [])) + 1
        t.update(status="running", progress=0, paused=False, step=f"handing off to @{who}",
                 run={"attempt": attempt, "started": now(), "human": True, "executor": f"@{who}"})
        m.save_task(t)
    t = m.task(tid)
    g = t.get("github") or {}
    info = {}
    try:
        if g.get("issue") and gitops.gh_ready(m):  # already handed over once: just ping the existing issue
            fb = m.feedback_text(tid).strip().split("\n### ")[-1]
            gitops.issue_comment(m, g["issue"], f"**Supervisor:** please pick this up again.\n\n{fb}")
        elif gitops.enabled(m):
            info = gitops.handoff(m, tid, t["title"], who, build_brief(m, t, human=True),
                                  mark_pushed=lambda: git_save(m)({"pushed": True}))
    except gitops.GitError as e:
        m.update_task(tid, lambda x: x.update(status="todo", paused=True, step=f"handoff failed: {short(str(e), 120)}"))
        m.event("error", "system", f"{tid} handoff to @{who} failed: {short(str(e), 200)}", task=tid)
        raise RuntimeError(str(e))

    def f(x):
        x["github"] = {**(x.get("github") or {}), **info}
        x["step"] = f"with @{who}" + (f" · issue #{x['github']['issue']}" if x["github"].get("issue") else " · share the brief")
    m.update_task(tid, f)
    m.event("run_started", f"@{who}", f"{tid} handed to @{who}" + (f" (issue #{info['issue']})" if info.get("issue") else ""), task=tid)
    return 0


def defer_launch(m: Mission, tid: str, force: bool) -> int:
    """Inside the agent sandbox an executor couldn't reach its model, so the dashboard starts it instead."""
    with open(m.dir / "launch-requests.jsonl", "a") as f:
        f.write(json.dumps({"task": tid, "force": force, "ts": now()}) + "\n")
    m.update_task(tid, lambda x: x.update(step="starting…"))
    return 0


def drain_launch_requests(m: Mission) -> list:
    """Dashboard loop: start the runs a sandboxed supervisor asked for."""
    p = m.dir / "launch-requests.jsonl"
    if not p.exists():
        return []
    with m.lock():
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        p.unlink()
    started, seen = [], set()
    for r in rows:
        if r.get("task") in seen:
            continue
        seen.add(r.get("task"))
        try:
            launch(m, r["task"], force=bool(r.get("force")))
            started.append(r["task"])
        except (RuntimeError, SystemExit) as e:
            m.event("error", "system", f"couldn't start {r.get('task')}: {short(str(e), 160)}", task=r.get("task"))
    return started


def launch(m: Mission, tid: str, force: bool = False) -> int:
    if compat.in_agent_sandbox():
        return defer_launch(m, tid, force)
    """Start one task attempt as a detached background process. Returns pid.
    force=True is only for a person pressing Run: it skips the attempt limit and pending permission checks."""
    if os.environ.get("MC_ROLE") == "supervisor":
        force = False
    if not force:
        why = blocked_reason(m, m.task(tid))
        if why:
            ensure_attempt_limit_cards(m)
            raise RuntimeError(why)
    if not (m.meta().get("git") or {}).get("disabled"):
        ensure_git(m)
    if is_human(m.task(tid)):
        return launch_human(m, tid)
    with m.lock():
        t = m.task(tid)
        run = t.get("run") or {}
        if t["status"] == "running":
            raise RuntimeError(f"{tid} is already running")
        if t["status"] in FINAL:
            raise RuntimeError(f"{tid} is {t['status']}")
        if m.meta().get("phase") in GATED_PHASES:
            raise RuntimeError("the plan isn't approved yet (press Approve & launch, or `mc approve`)")
        attempt = len(t.get("attempts", [])) + 1
        t.update(status="running", progress=0, step="starting", paused=False,
                 run={"attempt": attempt, "started": now(), "pid": None, "executor": t["executor"]})
        m.save_task(t)
    logdir = m.dir / "runs" / tid
    logdir.mkdir(parents=True, exist_ok=True)
    err = open(logdir / f"attempt-{attempt}.runner.txt", "w")
    p = subprocess.Popen(
        [sys.executable, str(HERE / "mc.py"), "_exec", str(m.root), tid, str(attempt)],
        cwd=str(m.root), stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT,
        env=clean_env(), **compat.spawn_kwargs(),
    )
    err.close()

    def set_pid(t):
        if (t.get("run") or {}).get("attempt") == attempt:
            t["run"]["pid"] = p.pid
    m.update_task(tid, set_pid)
    m.event("run_started", t["executor"], f"{tid} attempt {attempt} started on {t['executor']}", task=tid)
    SPAWNED.append(p)  # long-lived callers (the dashboard) reap these
    return p.pid


def schedule(m: Mission) -> list:
    """Launch ready tasks up to the parallel limit. Returns launched ids.
    Serialised with its own lock file so concurrent schedulers can't overshoot max_parallel."""
    m.dir.mkdir(exist_ok=True)
    with open(m.dir / ".sched.lock", "a") as lf:
        compat.lock(lf)
        try:
            meta = m.meta()
            if meta.get("phase") in GATED_PHASES:
                return []
            tasks = m.tasks()
            running = sum(1 for t in tasks if t["status"] == "running" and not is_human(t))
            room = max(0, int(meta.get("max_parallel", 3)) - running)
            launched = []
            for t in ready_tasks(m, tasks):  # ready_tasks already skips tasks at their attempt limit or awaiting permission
                if not is_human(t):
                    if room <= 0:
                        continue
                    room -= 1
                try:
                    launch(m, t["id"])
                    launched.append(t["id"])
                except RuntimeError:
                    pass
            return launched
        finally:
            compat.unlock(lf)


def stop_task(m: Mission, tid: str, final_status: str = "todo") -> None:
    pid = None

    def f(t):
        nonlocal pid
        pid = (t.get("run") or {}).get("pid")
        t["status"] = final_status
        t["step"] = "stopped" if final_status != "cancelled" else "cancelled"
        if final_status == "todo":
            t["paused"] = True
    t = m.update_task(tid, f)
    if pid and pid_alive(pid):
        compat.kill_group(pid)
    if final_status == "cancelled":
        try:
            if is_human(t) and (t.get("github") or {}).get("issue") and gitops.gh_ready(m):
                gitops.close_issue(m, t["github"]["issue"], "This task was cancelled by the mission owner.")
            elif gitops.enabled(m):
                gitops.drop_task(m, tid)
        except gitops.GitError:
            pass
    m.event("cancelled" if final_status == "cancelled" else "stopped", "user", f"{tid} {final_status if final_status == 'cancelled' else 'stopped'}", task=tid)


def reap_dead(m: Mission) -> None:
    """Running tasks whose runner process vanished go to review."""
    for t in m.tasks():
        run = t.get("run") or {}
        if t["status"] != "running" or run.get("human"):
            continue
        age = time.time() - (parse_ts(run.get("started")) or 0)
        if (run.get("pid") and age > 10 and not pid_alive(run["pid"])) or (not run.get("pid") and age > 30):
            def f(x):
                if x["status"] == "running":
                    x["status"] = "review"
                    x["step"] = "runner exited unexpectedly"
            m.update_task(t["id"], f)
            m.event("error", "system", f"{t['id']} runner died unexpectedly", task=t["id"])


# ───────────────────────────── stream parsing ─────────────────────────────

def describe_tool(name: str, inp: dict) -> str:
    inp = inp or {}
    if name in ("Edit", "Write", "Read", "MultiEdit", "NotebookEdit"):
        return f"{name} {inp.get('file_path') or inp.get('notebook_path', '')}"
    if name == "Bash":
        return f"$ {short(inp.get('command', ''), 100)}"
    if name in ("Glob", "Grep"):
        return f"{name} {short(inp.get('pattern', ''), 60)}"
    return name


class StreamState:
    def __init__(self, parser: str):
        self.parser = parser
        self.texts: list = []
        self.raw: list = []
        self.steps = 0
        self.progress = None
        self.step = ""
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.cost = None
        self.exact = False
        self.error = None
        self.denied: list = []
        self.denied_raw: list = []
        self.on_result = None  # called the moment the CLI's final result event is parsed
        self.calls: list = []
        self._call_by_id: dict = {}
        self._t0 = time.time()

    def _note_call(self, msg: dict) -> None:
        """One model call = one assistant message id (it arrives split over several events)."""
        u, mid = msg.get("usage") or {}, msg.get("id")
        if not mid or not u:
            return
        c = self._call_by_id.get(mid)
        if c is None:
            c = {"n": len(self.calls) + 1, "t": round(time.time() - self._t0, 1), "in": 0, "read": 0, "write": 0, "out": 0, "tool": ""}
            self._call_by_id[mid] = c
            self.calls.append(c)
        c["in"] = u.get("input_tokens", 0)
        c["read"] = u.get("cache_read_input_tokens", 0)
        c["write"] = u.get("cache_creation_input_tokens", 0)
        c["out"] = max(c["out"], u.get("output_tokens", 0))
        for blk in msg.get("content", []):
            if blk.get("type") == "tool_use" and not c["tool"]:
                c["tool"] = describe_tool(blk.get("name", ""), blk.get("input", {}))

    def feed(self, line: str) -> list:
        """Returns a list of (kind, text) items for the readable transcript."""
        line = line.rstrip("\n")
        if not line.strip():
            return []
        if self.parser in ("text", "gemini"):
            self.raw.append(line)
            if self.parser == "text":
                self._scan_text(line)
                return [("text", line)]
            return []
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return [("text", line)]
        if self.parser == "claude":
            return self._claude(ev)
        if self.parser == "codex":
            return self._codex(ev)
        return []

    def _scan_text(self, text: str) -> None:
        for m in PROGRESS_RE.finditer(text):
            self.progress = min(99, int(m.group(1)))
            if m.group(2).strip():
                self.step = short(m.group(2).strip(), 80)

    def _tool(self, desc: str) -> list:
        self.steps += 1
        self.step = short(desc, 80)
        return [("tool", desc)]

    def _claude(self, ev: dict) -> list:
        t = ev.get("type")
        out = []
        if t == "assistant":
            self._note_call(ev.get("message", {}))
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    self.texts.append(c["text"])
                    self._scan_text(c["text"])
                    out.append(("text", c["text"]))
                elif c.get("type") == "tool_use":
                    out += self._tool(describe_tool(c.get("name", ""), c.get("input", {})))
        elif t == "result":
            u = ev.get("usage", {})
            self.usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                          "cache_read": u.get("cache_read_input_tokens", 0),
                          "cache_write": u.get("cache_creation_input_tokens", 0)}
            self.cost = ev.get("total_cost_usd")
            self.exact = True
            if self.on_result:
                self.on_result()
            if ev.get("result"):
                self.texts.append(ev["result"])
            if ev.get("is_error"):
                self.error = ev.get("subtype") or "error"
            for d in ev.get("permission_denials") or []:
                self.denied.append(describe_tool(d.get("tool_name", ""), d.get("tool_input", {})))
                self.denied_raw.append({"tool": d.get("tool_name", ""), "input": d.get("tool_input", {})})
            for d in self.denied:
                out.append(("error", f"permission denied: {d}"))
            out.append(("system", f"finished · {ev.get('num_turns', '?')} turns · {round((ev.get('duration_ms') or 0) / 1000)}s"))
        return out

    def _codex(self, ev: dict) -> list:
        t = ev.get("type", "")
        item = ev.get("item") or {}
        if t == "item.completed":
            it = item.get("type")
            if it in ("agent_message", "assistant_message") and item.get("text"):
                self.texts.append(item["text"])
                self._scan_text(item["text"])
                return [("text", item["text"])]
            if it == "command_execution":
                return self._tool(f"$ {short(item.get('command', ''), 100)}")
            if it == "file_change":
                return self._tool("edit " + ", ".join(c.get("path", "") for c in item.get("changes", [])))
        if t == "turn.completed":
            u = ev.get("usage", {})
            self.usage["input"] += u.get("input_tokens", 0)
            self.usage["cache_read"] += u.get("cached_input_tokens", 0)
            self.usage["output"] += u.get("output_tokens", 0)
            self.exact = True
        return []

    def finish(self) -> list:
        if self.parser == "gemini":
            blob = "\n".join(self.raw)
            try:
                data = json.loads(blob[blob.find("{"):])
                self.texts.append(data.get("response", ""))
                for mdl in (data.get("stats", {}).get("models") or {}).values():
                    tk = mdl.get("tokens", {})
                    self.usage["input"] += tk.get("prompt", 0)
                    self.usage["output"] += tk.get("candidates", 0)
                    self.usage["cache_read"] += tk.get("cached", 0)
                    self.exact = True
                return [("text", data.get("response", ""))]
            except (json.JSONDecodeError, AttributeError):
                self.texts.append(blob)
                return [("text", blob)]
        if self.parser == "text":
            self.texts.append("\n".join(self.raw))
        return []

    def report(self) -> dict | None:
        for text in reversed(self.texts):
            found = REPORT_RE.findall(text or "")
            if found:
                try:
                    return json.loads(found[-1])
                except json.JSONDecodeError:
                    continue
        return None


# ───────────────────────────── watches ─────────────────────────────
# A watch lets the supervisor wait for something outside Mission Control (a long simulation, a download)
# without polling: the dashboard checks a few SAFE, read-only conditions for free and wakes the supervisor
# when they're met. Conditions are deliberately not shell commands, so a watch can't bypass the
# supervisor's permissions.

WATCH_MIN_EVERY = 15
MAX_ACTIVE_WATCHES = 10


def watches_dir(m: Mission) -> Path:
    return m.dir / "watches"


def watch_list(m: Mission) -> list:
    out = []
    for p in sorted(watches_dir(m).glob("W-*.json")):
        w = read_json(p)
        if w:
            out.append(w)
    return out


def watch_save(m: Mission, w: dict) -> None:
    write_json(watches_dir(m) / f"{w['id']}.json", w)


def _inside(m: Mission, raw: str) -> Path:
    p = (m.root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    if not str(p).startswith(str(m.root.resolve())):
        raise ValueError(f"{raw} is outside the project folder")
    return p


def process_lines() -> list:
    """Command lines of running processes, minus Mission Control's own and the Claude CLIs (their prompts
    mention everything, so they would always look like a match)."""
    lines = compat.process_commands()
    if lines is None:
        return None  # this system can't list processes
    skip = re.compile(r"(^|[/\\])claude(\.exe)?( |$)|mc\.py|dashboard\.py")
    return [ln for ln in lines if not skip.search(ln)]


def cond_text(c: dict) -> str:
    if "file" in c:
        return f"{c['file']} exists"
    if "contains" in c:
        return f"{c['contains'][0]} contains '{c['contains'][1]}'"
    return f"no process matching '{c['gone']}' is running"


def cond_met(m: Mission, c: dict, procs: list) -> bool:
    if "file" in c:
        return _inside(m, c["file"]).exists()
    if "contains" in c:
        f = _inside(m, c["contains"][0])
        try:
            with open(f, "r", errors="ignore") as fh:
                fh.seek(max(0, f.stat().st_size - 2_000_000))  # only the last ~2 MB of a log
                return c["contains"][1] in fh.read()
        except OSError:
            return False
    if procs is None:
        return False  # can't tell, so never fire on a guess
    return not any(c["gone"] in ln for ln in procs)


def watch_add(m: Mission, spec: dict) -> dict:
    active = [w for w in watch_list(m) if w.get("status") == "active"]
    if len(active) >= MAX_ACTIVE_WATCHES:
        raise ValueError(f"already {len(active)} active watches; cancel some first")
    until = ([{"file": x} for x in spec.get("file", [])] + [{"contains": x} for x in spec.get("contains", [])]
             + [{"gone": x} for x in spec.get("gone", [])])
    fail = [{"contains": x} for x in spec.get("fail_contains", [])] + [{"file": x} for x in spec.get("fail_file", [])]
    fail_gone = list(spec.get("fail_if_gone", []))
    if not until:
        raise ValueError("give at least one thing to wait for: --file, --contains or --gone")
    for c in until + fail:
        if "file" in c:
            _inside(m, c["file"])
        elif "contains" in c:
            _inside(m, c["contains"][0])
    wid = m.next_id("W", "watches") if watches_dir(m).exists() else "W-001"
    watches_dir(m).mkdir(exist_ok=True)
    every = max(WATCH_MIN_EVERY, int(spec.get("every") or 60))
    w = {"id": wid, "name": spec.get("name") or wid, "note": spec.get("note", ""), "task": spec.get("task"),
         "until": until, "fail": fail, "fail_if_gone": fail_gone, "every": every, "status": "active",
         "created": now(), "expires": datetime.fromtimestamp(time.time() + float(spec.get("hours") or 24) * 3600, timezone.utc)
         .isoformat(timespec="seconds"), "last_check": None, "result": ""}
    watch_save(m, w)
    m.event("watch_added", spec.get("by", "supervisor"), f"waiting for: {w['name']}", task=w["task"], watch=wid)
    return w


def watches_tick(m: Mission, force: bool = False) -> list:
    """Check due watches (cheap, no model). Returns the watches that fired."""
    if not watches_dir(m).exists():
        return []
    fired, procs = [], None
    for w in watch_list(m):
        if w.get("status") != "active":
            continue
        last = parse_ts(w.get("last_check")) or 0
        if not force and time.time() - last < w["every"]:
            continue
        procs = process_lines() if procs is None else procs
        try:
            done = all(cond_met(m, c, procs) for c in w["until"])
            failed = (not done) and (any(cond_met(m, c, procs) for c in w["fail"])
                                     or any(cond_met(m, {"gone": g}, procs) for g in w.get("fail_if_gone", [])))
        except ValueError as e:
            done, failed = False, False
            w["result"] = str(e)
        expired = (not done and not failed) and time.time() > (parse_ts(w["expires"]) or 1e18)
        w["last_check"] = now()
        if done or failed or expired:
            reason = "done" if done else "failed" if failed else "expired"
            w.update(status="fired", reason=reason, fired_at=now(), seen=False)
            what = {"done": "is ready", "failed": "failed or its process disappeared", "expired": "timed out without finishing"}[reason]
            msg = f"watch '{w['name']}' {what}." + (f" You planned to: {w['note']}" if w.get("note") else "")
            watch_save(m, w)
            m.event("watch", "system", msg, task=w.get("task"), watch=w["id"], reason=reason)
            chat_add(m, "system", ("✓ " if done else "⚠ ") + msg)
            fired.append(w)
        else:
            watch_save(m, w)
    return fired


# ───────────────────────────── usage ledger ─────────────────────────────

def allocate_calls(calls: list, usage: dict, cost) -> list:
    """Per-call tokens are exact for input/cache; output in the stream is partial, so the exact total is spread
    in proportion. Each call's cost is the exact total split by a token-price weighting (so it sums to the total)."""
    calls = [dict(c) for c in calls]
    if not calls:
        return []
    tot_out = usage.get("output", 0)
    seen_out = sum(max(c.get("out", 0), 1) for c in calls)
    for c in calls:
        c["out"] = round(tot_out * max(c.get("out", 0), 1) / seen_out) if tot_out else c.get("out", 0)
    weights = [c["in"] + 2.0 * c["write"] + 0.1 * c["read"] + 5.0 * c["out"] for c in calls]
    wsum = sum(weights) or 1.0
    for c, w in zip(calls, weights):
        c["cost"] = round(float(cost) * w / wsum, 6) if cost else 0.0
    return calls


def usage_path(m: Mission) -> Path:
    return m.dir / "usage.jsonl"


def usage_add(m: Mission, row: dict) -> None:
    row = {"id": f"{time.time():.6f}", "ts": now(), **row}
    with m.lock():
        with open(usage_path(m), "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def usage_rows(m: Mission, with_calls: bool = False) -> list:
    if not usage_path(m).exists():
        usage_backfill(m)
    rows = []
    try:
        for line in usage_path(m).read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not with_calls:
                r["n_calls"] = len(r.get("calls") or [])
                r.pop("calls", None)
            rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def usage_backfill(m: Mission) -> None:
    """Build the ledger for missions that predate it, from task attempts and supervisor chat costs.
    Per-call detail doesn't exist for those runs."""
    rows = []
    for t in m.tasks():
        for a in t.get("attempts", []):
            u = a.get("usage") or {}
            rows.append({"ts": a.get("finished") or t.get("updated"), "kind": "executor", "task": t["id"], "title": t.get("title", ""),
                         "attempt": a.get("n"), "model": a.get("executor"), "input": u.get("input", 0), "cache_read": u.get("cache_read", 0),
                         "cache_write": u.get("cache_write", 0), "output": u.get("output", 0), "cost_usd": a.get("cost_usd") or 0.0,
                         "exact": bool(a.get("exact")), "status": a.get("status"), "denied": len(a.get("denied") or []), "backfilled": True})
    n = 0
    for c in chat_log(m):
        if c.get("role") == "supervisor" and c.get("cost") is not None:
            n += 1
            rows.append({"ts": c.get("ts"), "kind": "supervisor", "turn": n, "cost_usd": float(c["cost"]), "exact": True, "backfilled": True,
                         "model": (m.meta().get("supervisor") or {}).get("last_model", ""), "trigger": "(before per-turn tracking)"})
    rows.sort(key=lambda r: r.get("ts") or "")
    with m.lock():
        with open(usage_path(m), "w") as f:
            for i, r in enumerate(rows):
                f.write(json.dumps({"id": f"b{i:05d}", **r}, ensure_ascii=False) + "\n")


# ───────────────────────────── finishing an attempt ─────────────────────────────

def add_tokens(t: dict, usage: dict, cost, exact: bool) -> None:
    tok = t.setdefault("tokens", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "source": "none"})
    for k in ("input", "output", "cache_read", "cache_write"):
        tok[k] = tok.get(k, 0) + int(usage.get(k, 0) or 0)
    if cost:
        tok["cost_usd"] = round(tok.get("cost_usd", 0.0) + float(cost), 6)
    if exact:
        tok["source"] = "exact" if tok.get("source") in ("none", "exact") else "mixed"
    elif any(usage.values()):
        tok["source"] = "reported" if tok.get("source") in ("none", "reported") else "mixed"


def estimate_cost(runner: dict, usage: dict):
    price = runner.get("price")  # USD per million tokens: {"in":..,"out":..,"cache_read":..}
    if not price:
        return None
    return (usage.get("input", 0) * price.get("in", 0) + usage.get("output", 0) * price.get("out", 0)
            + usage.get("cache_read", 0) * price.get("cache_read", 0)
            + usage.get("cache_write", 0) * price.get("cache_write", price.get("in", 0))) / 1e6


def finalize(m: Mission, tid: str, attempt: int, rep: dict | None, usage: dict, cost, exact: bool,
             fallback_text: str = "", error: str | None = None, executor: str = "", denied: list | None = None,
             workdir=None, commit: str | None = None, denied_raw: list | None = None, calls: list | None = None,
             duration_s: float | None = None, commit_error: str | None = None) -> None:
    """Shared by the runner and by manual `mc.py report`: store report, run check, set status."""
    t = m.task(tid)
    if t["status"] != "running" or (t.get("run") or {}).get("attempt") != attempt:
        # stopped or cancelled meanwhile: just keep the token accounting
        m.update_task(tid, lambda x: add_tokens(x, usage, cost, exact))
        return
    rep = dict(rep or {})
    no_report = not rep
    if no_report:
        rep = {"status": "failed" if error else "done", "summary": short(fallback_text or error or "(no output)", 600),
               "confidence": "low"}
    status = str(rep.get("status", "done")).lower()
    summary = rep.get("summary", "")
    files = rep.get("files") or []
    if isinstance(files, str):
        files = [f.strip() for f in files.split(",") if f.strip()]

    md = [f"# {tid} report · attempt {attempt}", "",
          f"**Status:** {status}" + ("  ⚠ executor gave no REPORT block" if no_report else ""),
          f"**Confidence:** {rep.get('confidence', '?')}", "", summary.strip(), ""]
    if files:
        md += ["**Files:** " + ", ".join(f"`{f}`" for f in files), ""]
    if rep.get("notes"):
        md += [f"**Notes:** {rep['notes']}", ""]
    if error:
        md += [f"**Runner error:** {error}", ""]
    if gitops.enabled(m) and not is_human(t):
        md += [f"**Commit:** {commit or 'no changes'} on `{gitops.task_branch(m, tid)}`", "```",
               gitops.diff(m, tid)[:3000], "```", ""]
    if denied:
        md += ["**⚠ Permission denied (executor could not run these, so don't trust claims that depend on them):**"]
        md += [f"- `{d}`" for d in denied] + [""]
        sug = suggest_permissions(denied_raw or [])
        if sug:
            md += ["**To allow these, ask the user:** `mc request-permission " + tid + " " + " ".join(f"'{x}'" for x in sug)
                   + " --why \"...\"`", ""]

    new_status, step = "review", "awaiting supervisor review"
    check_result = None
    if status == "blocked" and rep.get("question"):
        qid = m.next_id("Q", "questions")
        m.save_question({"id": qid, "task": tid, "from": tid, "to": "supervisor", "question": rep["question"],
                         "options": rep.get("options") or [], "recommendation": rep.get("recommendation", ""),
                         "status": "open", "answer": "", "created": now()})
        md += [f"**Question {qid} for supervisor:** {rep['question']}", ""]
        new_status, step = "blocked", f"waiting for answer to {qid}"
        m.event("question", tid, f"{tid} asks supervisor: {short(rep['question'], 140)}", task=tid, question=qid, to="supervisor")
    elif status == "done" and t.get("check"):
        try:
            if compat.IS_WIN:
                sh = compat.bash()
                if not sh:
                    raise FileNotFoundError("checks are written for bash; install Git for Windows (Git Bash) or use WSL")
                cp = subprocess.run([sh, "-c", t["check"]], cwd=str(workdir or m.root), capture_output=True, text=True, timeout=900)
            else:
                cp = subprocess.run(t["check"], shell=True, cwd=str(workdir or m.root), capture_output=True, text=True, timeout=900)
            tail = "\n".join((cp.stdout + cp.stderr).strip().splitlines()[-25:])
            check_result = {"ok": cp.returncode == 0, "code": cp.returncode, "tail": tail}
        except subprocess.TimeoutExpired:
            check_result = {"ok": False, "code": -1, "tail": "check timed out after 900s"}
        except FileNotFoundError as e:
            check_result = {"ok": False, "code": -1, "tail": str(e)}
        md += [f"**Check** `{t['check']}`: {'PASSED' if check_result['ok'] else 'FAILED (exit %s)' % check_result['code']}", ""]
        if not check_result["ok"]:
            md += ["```", check_result["tail"], "```", ""]
        if check_result["ok"] and not commit_error:
            new_status, step = "done", "auto-approved (check passed)"
            if gitops.enabled(m):
                merged, msg = gitops.integrate(m, tid, t["title"])
                if not merged:
                    new_status, step = "needs_changes", f"check passed but {msg}"
                    with open(m.dir / "feedback" / f"{tid}.md", "a") as fbf:
                        fbf.write(f"\n### Merge conflict ({now()})\nYour work passed its check, but merging into the mission "
                                  f"branch failed ({msg}). Other tasks changed the same code; reconcile with their changes.\n")
                    md += [f"**Merge into mission branch failed:** {msg}", ""]
        m.event("check", "system", f"{tid} check {'passed' if check_result['ok'] else 'FAILED'}", task=tid, ok=check_result["ok"])

    if commit_error and new_status in ("done", "review"):
        # The work couldn't be committed (for example conflict markers are still in the files), so it is NOT done.
        new_status, step = "needs_changes", f"could not commit: {short(commit_error, 90)}"
        with open(m.dir / "feedback" / f"{tid}.md", "a") as fbf:
            fbf.write(f"\n### Not committed ({now()})\nYour changes could not be committed: {commit_error}. "
                      "Fix that (resolve every conflict marker, <<<<<<< ======= >>>>>>>) and finish the task.\n")
        md += [f"**Not committed:** {commit_error}", ""]

    # A run that ended blocked/failed because commands were denied: ask the user straight away (no model tokens
    # spent) and hold the task, instead of letting it retry into the same wall.
    perm_q = None
    sug = [x for x in suggest_permissions(denied_raw or []) if x not in perm_list(m)]
    if sug and new_status != "done" and (no_report or status in ("blocked", "failed")) and not open_questions_for(m, tid, "permission"):
        shown = "; ".join(short(d, 70) for d in (denied or [])[:4])
        qid = m.next_id("Q", "questions")
        perm_q = {"id": qid, "task": tid, "from": "system", "to": "user", "auto": True,
                  "question": f"{tid} was blocked. It couldn't run: {shown}. Allow these for this project?",
                  "why": f"It tried to run: {shown}", "permission": sug, "options": ["Allow", "Don't allow"], "recommendation": "Allow", "status": "open",
                  "answer": "", "created": now()}
        m.save_question(perm_q)
        new_status, step = "blocked", f"waiting for your permission decision ({qid})"
        md += [f"**Permission request {qid} sent to the user:** " + ", ".join(f"`{x}`" for x in sug), ""]
        m.event("question", "system", f"{tid} needs permission: {', '.join(sug)}", task=tid, question=qid, to="user")

    (m.dir / "reports").mkdir(exist_ok=True)
    (m.dir / "reports" / f"{tid}.md").write_text("\n".join(md))

    def f(x):
        add_tokens(x, usage, cost, exact)
        x["status"] = new_status
        x["step"] = step
        x["progress"] = 100 if new_status in ("review", "done") else x.get("progress", 0)
        x["summary"] = short(summary, 400)
        x["confidence"] = rep.get("confidence", "")
        x["files_touched"] = files
        x.setdefault("attempts", []).append({
            "n": attempt, "executor": executor or x.get("executor"), "started": (x.get("run") or {}).get("started"),
            "finished": now(), "status": status, "usage": usage, "cost_usd": cost, "exact": exact,
            "check": check_result, "error": error, "denied": denied or [],
            "suggest": suggest_permissions(denied_raw or []),
        })
        x["run"] = {**(x.get("run") or {}), "pid": None, "finished": now()}
        if new_status == "done":
            x["finished"] = now()
    m.update_task(tid, f)
    run_info = m.task(tid).get("run") or {}
    usage_add(m, {"kind": "executor", "task": tid, "title": t.get("title", ""), "attempt": attempt, "model": executor or t.get("executor"),
                  "effort": run_info.get("effort", ""), "duration_s": round(duration_s, 1) if duration_s is not None else None,
                  "input": usage.get("input", 0), "cache_read": usage.get("cache_read", 0), "cache_write": usage.get("cache_write", 0),
                  "output": usage.get("output", 0), "cost_usd": float(cost or 0), "exact": bool(exact), "status": status,
                  "outcome": new_status, "denied": len(denied or []), "calls": allocate_calls(calls or [], usage, cost)})
    tok_txt = f"{usage.get('input', 0) + usage.get('cache_read', 0) + usage.get('cache_write', 0):,} in / {usage.get('output', 0):,} out"
    m.event("report", tid, f"{tid} reported '{status}' · {short(summary, 120)} · {tok_txt}", task=tid, status=status)
    t_after = m.task(tid)
    if new_status != "done" and len(t_after.get("attempts", [])) >= attempt_cap(m, t_after) and not perm_q \
            and not open_questions_for(m, tid, "attempt_limit"):
        used = len(t_after["attempts"])
        qid = m.next_id("Q", "questions")
        m.save_question({"id": qid, "task": tid, "from": "system", "to": "user", "kind": "attempt_limit", "extra": 2,
                         "question": f"{tid} has used all {used} attempts without finishing. Nothing will retry it automatically. "
                                     "Allow 2 more attempts, or stop here?",
                         "options": ["Allow 2 more attempts", "Stop this task"], "recommendation": "", "status": "open",
                         "answer": "", "created": now()})
        m.update_task(tid, lambda x: x.update(step=f"attempt limit reached ({used}): waiting for your decision"))
        m.event("question", "system", f"{tid} hit its attempt limit ({used})", task=tid, question=qid, to="user")
    if new_status == "done":
        m.event("approved", "system", f"{tid} auto-approved: check passed" + (" · merged into mission branch" if gitops.enabled(m) else ""), task=tid)
    if m.meta().get("autopilot"):
        schedule(m)


def cmd_exec(args) -> None:
    """Internal: run one attempt of a task with its configured runner (one fresh chat)."""
    m = Mission(args.root)
    tid, attempt = args.task, int(args.attempt)
    t = m.task(tid)
    meta = m.meta()
    default_model = meta.get("executor_model") if t["executor"] == meta.get("default_executor") else ""
    rname, runner, model = resolve_runner(t["executor"], t.get("model") or default_model)
    logdir = m.dir / "runs" / tid
    logdir.mkdir(parents=True, exist_ok=True)
    log_path = logdir / f"attempt-{attempt}.jsonl"
    if not runner:
        finalize(m, tid, attempt, None, {}, None, False, error=f"unknown executor '{t['executor']}' (see runners.json)")
        return
    workdir, conflicts = m.root, []
    if gitops.enabled(m):
        try:
            workdir, conflicts = gitops.prepare_task(m, tid, t.get("merge_from"))
        except gitops.GitError as e:
            finalize(m, tid, attempt, None, {}, None, False, error=f"git worktree: {e}")
            return
    prompt = build_brief(m, t, workdir=workdir, conflicts=conflicts)
    prompt_file = logdir / f"attempt-{attempt}.prompt.md"
    prompt_file.write_text(prompt)
    subs = {"prompt": prompt, "prompt_file": str(prompt_file), "task": tid, "root": str(workdir),
            "mc": str(HERE / "mc.py"), "python": sys.executable}
    effort = (t.get("effort") or meta.get("executor_effort") or "") if rname == meta.get("default_executor") or t.get("effort") else ""
    budget = meta.get("executor_budget_usd")
    turn_cap = int(meta.get("executor_max_turns") or 0)
    cmd = apply_permissions(m, build_cmd(runner, subs, model, effort, f"{float(budget):g}" if budget else ""))
    if compat.cmdline_too_long(cmd):  # Windows caps the command line: hand over the brief as a file instead
        subs["prompt"] = f"Your full task brief is in the file {prompt_file}. Read it first, then follow it exactly."
        cmd = apply_permissions(m, build_cmd(runner, subs, model, effort, f"{float(budget):g}" if budget else ""))
    m.update_task(tid, lambda x: x.setdefault("run", {}).update(model=model_label(rname, model), effort=effort,
                                                                  budget=budget or None, turn_cap=turn_cap or None))
    if t.get("check") and "--allowedTools" in cmd:
        # Let the executor run its own acceptance check. Claude's matcher doesn't match quoted
        # literals, so pre-approve the command's prefix up to the first quote.
        i = cmd.index("--allowedTools") + 1
        prefix = re.split(r"['\"]", t["check"], 1)[0].strip()
        if i < len(cmd) and prefix and "," not in prefix:
            cmd[i] += f",Bash({prefix})" + f",Bash({prefix} *)"
    if not shutil.which(cmd[0]) and not Path(cmd[0]).exists():
        finalize(m, tid, attempt, None, {}, None, False, error=f"executable '{cmd[0]}' not found on PATH")
        return
    try:
        cmd = compat.resolve_exe(cmd)
    except RuntimeError as e:
        finalize(m, tid, attempt, None, {}, None, False, error=str(e))
        return
    st = StreamState(runner.get("parser", "text"))
    t_started = time.time()
    timeout = int(runner.get("timeout", 3600))
    log = open(log_path, "w")

    def write(kind, text):
        log.write(json.dumps({"ts": now(), "kind": kind, "text": text}, ensure_ascii=False) + "\n")
        log.flush()

    if snapshots_on(m):  # agents have full access to the folder, so keep a way back
        try:
            snap = snapshots.take(m.root, f"before {tid} attempt {attempt}")
            write("system", "snapshot of the project taken" if snap else "snapshot: nothing changed since the last one")
        except Exception as e:
            write("system", f"snapshot failed: {short(str(e), 200)}")
    write("system", f"attempt {attempt} · {runner.get('label', rname)} · model {model or 'default'} · fresh session")
    proc = subprocess.Popen(cmd, cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1,
                            env={**clean_env(), "MC_TASK": tid, "MC_ROOT": str(m.root)})

    def on_term(*_):
        proc.terminate()
    signal.signal(signal.SIGTERM, on_term)
    timer = threading.Timer(timeout, compat.kill_hard, [proc])
    timer.start()
    last_update = 0.0
    capped = ""
    st.on_result = lambda: watch_for_exit_after_result(proc)
    with open(logdir / f"attempt-{attempt}.raw.jsonl", "w") as raw:
        for line in proc.stdout:
            raw.write(line)
            for kind, text in st.feed(line):
                write(kind, text)
            if turn_cap and len(st.calls) > turn_cap and not capped:
                capped = f"Stopped after {turn_cap} model calls (the turn cap for each run)."
                write("error", capped)
                proc.terminate()
            if time.time() - last_update > 1.0:
                last_update = time.time()
                prog = st.progress if st.progress is not None else min(90, 5 + st.steps * 7)
                step = st.step

                def upd(x):
                    if x["status"] == "running" and (x.get("run") or {}).get("attempt") == attempt:
                        x["progress"] = max(x.get("progress", 0), prog)
                        x["step"] = step or x.get("step", "")
                m.update_task(tid, upd)
    code = proc.wait()
    timer.cancel()
    for kind, text in st.finish():
        write(kind, text)
    if code != 0:
        write("error", f"executor exited with code {code}")
    log.close()
    if not st.exact and st.calls:  # killed before the CLI could report totals: rebuild from the per-call tokens we saw
        st.usage = {"input": sum(c["in"] for c in st.calls), "cache_read": sum(c["read"] for c in st.calls),
                    "cache_write": sum(c["write"] for c in st.calls), "output": sum(c["out"] for c in st.calls)}
        pr = model_price(rname, model)
        if pr and st.cost is None:
            st.cost = pr * (st.usage["input"] + 2.0 * st.usage["cache_write"] + 0.1 * st.usage["cache_read"] + 5.0 * st.usage["output"]) / 1e6
    cost = st.cost if st.cost is not None else estimate_cost(runner, st.usage)
    error = st.error or (f"exit code {code}" if code != 0 and not st.report() else None)
    if st.error == "error_max_budget_usd":
        error = f"Stopped at the budget cap of ${float(budget or 0):g} for this run."
    if capped:
        error = capped
    sha, commit_err = None, None
    if gitops.enabled(m) and m.task(tid)["status"] == "running":
        try:
            sha = gitops.commit_task(m, tid, t["title"])
        except gitops.GitError as e:
            commit_err = str(e)
            error = (error + "; " if error else "") + f"commit failed: {e}"
    finalize(m, tid, attempt, st.report(), st.usage, cost, st.exact,
             fallback_text=(st.texts[-1] if st.texts else ""), error=error, executor=model_label(rname, model), denied=st.denied,
             workdir=workdir, commit=sha, denied_raw=st.denied_raw, calls=st.calls, duration_s=time.time() - t_started,
             commit_error=commit_err)


# ───────────────────────────── simulator executor ─────────────────────────────

def cmd_demo(args) -> None:
    """Fake executor that speaks Claude's stream-json. Used for demos and tests (no tokens spent)."""
    m = Mission(os.environ.get("MC_ROOT", os.getcwd()))
    t = m.task(args.task)
    speed = float(os.environ.get("MC_DEMO_SPEED", "1"))
    rnd = random.Random(hash(t["id"]) + len(t.get("attempts", [])))

    def emit(obj):
        print(json.dumps(obj), flush=True)

    def say(text):
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})

    def tool(name, inp):
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})

    files = t.get("files") or [f"src/{t['id'].lower()}.py"]
    steps = [("Read", {"file_path": files[0]}), ("Grep", {"pattern": "def "}),
             ("Edit", {"file_path": files[0]}), ("Bash", {"command": "pytest -q"})]
    n = max(3, t.get("estimate", 2) * 2)
    for i in range(n):
        pct = int((i + 1) / (n + 1) * 100)
        say(f"PROGRESS: {pct} working on {t['title'].lower()} (step {i + 1}/{n})")
        name, inp = steps[i % len(steps)]
        tool(name, inp)
        time.sleep(rnd.uniform(0.8, 2.0) / speed)
    if "/.mission/worktrees/" in os.getcwd():  # git mode demo: produce a real change to commit and merge
        target = Path(os.getcwd()) / files[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as fh:
            fh.write(f"# {t['id']} {t['title']} (attempt {len(t.get('attempts', [])) + 1})\n")
        if os.environ.get("MC_DEMO_JUNK"):  # test aid: emulate an agent tool writing its runtime folder into the worktree
            (Path(os.getcwd()) / ".claude-flow").mkdir(exist_ok=True)
            (Path(os.getcwd()) / ".claude-flow" / "state.json").write_text("{}")
    answered = any(q.get("task") == t["id"] and q.get("status") == "answered" for q in m.questions())
    if "[ask]" in t["title"].lower() and not answered:
        rep = {"status": "blocked", "summary": "Need a decision before continuing.",
               "question": "Should sessions be stored in Redis or in the SQL database?",
               "options": ["Redis", "SQL database"], "recommendation": "Redis", "confidence": "medium"}
    else:
        rep = {"status": "done", "summary": f"Completed: {t['title']}. All acceptance criteria addressed.",
               "files": files, "confidence": rnd.choice(["high", "high", "medium"])}
    say("<<<REPORT\n" + json.dumps(rep) + "\nREPORT>>>")
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": n, "duration_ms": 1000 * n,
          "total_cost_usd": round(rnd.uniform(0.004, 0.03), 5), "result": "<<<REPORT\n" + json.dumps(rep) + "\nREPORT>>>",
          "usage": {"input_tokens": rnd.randint(20, 400), "output_tokens": rnd.randint(800, 4000),
                    "cache_read_input_tokens": rnd.randint(10000, 60000), "cache_creation_input_tokens": rnd.randint(2000, 9000)}})


# ───────────────────────────── state summary ─────────────────────────────

def phase_levels(tasks: list) -> dict:
    by_id = {t["id"]: t for t in tasks}
    memo: dict = {}

    def lvl(tid, seen=()):
        if tid in memo:
            return memo[tid]
        if tid in seen or tid not in by_id:
            return 0
        deps = [d for d in by_id[tid].get("depends_on", []) if d in by_id]
        memo[tid] = 0 if not deps else 1 + max(lvl(d, seen + (tid,)) for d in deps)
        return memo[tid]
    return {t["id"]: lvl(t["id"]) for t in tasks}


def state_text(m: Mission) -> str:
    meta = m.meta()
    tasks = m.tasks()
    counts = {s: sum(1 for t in tasks if t["status"] == s) for s in STATUSES}
    cost = sum(t.get("tokens", {}).get("cost_usd", 0) for t in tasks)
    levels = phase_levels(tasks)
    lines = [f"# Mission: {meta.get('name', m.root.name)}", "", f"Goal: {meta.get('goal', '-')}", "",
             "Progress: " + " · ".join(f"{s} {n}" for s, n in counts.items() if n) + f" · executor cost ${cost:.2f}",
             f"Autopilot: {'on' if meta.get('autopilot') else 'off'} · max parallel {meta.get('max_parallel', 3)}",
             (f"Git: mission branch `{(meta.get('git') or {}).get('branch')}` (code: .mission/worktrees/_mission; base `{(meta.get('git') or {}).get('base')}`)"
              + (f" · PR {meta['git']['pr']}" if (meta.get('git') or {}).get('pr') else "")) if (meta.get("git") or {}).get("enabled")
             else "Git: off (work happens directly in the project folder)", "",
             "## Tasks", "| id | phase | status | executor | title | summary |", "|---|---|---|---|---|---|"]
    for t in tasks:
        who = f"@{t.get('assignee')} (human)" if is_human(t) else t["executor"]
        lines.append(f"| {t['id']} | {levels[t['id']] + 1} | {t['status']}{' (paused)' if t.get('paused') else ''} | "
                     f"{who} | {t['title']} | {short(t.get('summary', ''), 80)} |")
    dec = m.dir / "decisions.md"
    if dec.exists() and dec.read_text().strip():
        lines += ["", "## Decisions", dec.read_text().strip()]
    open_q = [q for q in m.questions() if q.get("status") == "open"]
    if open_q:
        lines += ["", "## Open questions"]
        lines += [f"- {q['id']} ({q['from']} → {q['to']}): {q['question']}" for q in open_q]
    ready = [t["id"] for t in ready_tasks(m, tasks)]
    review = [t["id"] for t in tasks if t["status"] == "review"]
    lines += ["", "## Next", f"- Ready to run: {', '.join(ready) or 'none'}", f"- Awaiting your review: {', '.join(review) or 'none'}"]
    return "\n".join(lines) + "\n"


# ───────────────────────────── dashboard supervisor ─────────────────────────────
# When a mission is ignited from the dashboard, the supervisor is a headless Claude session
# (resumed turn after turn). It is woken by user chat messages and by executor events.

GATED_PHASES = {"briefing", "awaiting_approval"}
SUPERVISOR_INTERESTING = {"report", "question", "answer", "error", "approved", "note", "watch"}
DASHBOARD_ADDENDUM = """

## You are running inside the Mission Control dashboard
- The user reads the final message of each of your turns in the dashboard chat. Write it for them: short, clear and friendly. Markdown is fine.
- The CLI is on your PATH as `mc` (e.g. `mc status`, `mc review T-001 approve`). To create tasks, Write the plan JSON to `.mission/plan.json` and run `mc plan .mission/plan.json`. Do not use `mc wait` or `mc dashboard`; you are woken automatically when the user writes or when executors report or ask.
- Mission phases:
  1. **briefing**: understand what the user wants. Look at the project cheaply, once (README, top-level layout, package files). Then ask focused questions (at most 3 at a time) until you can state the goal, scope, constraints and definition of done. Restate them in a few lines and get the user's agreement. Do not create tasks before the goal is agreed.
  2. When agreed, run `mc goal "<one-line goal>"`, then write the plan. The mission moves to **awaiting_approval** and nothing runs yet. Summarise the plan (phases, what runs in parallel, executors, rough size) and ask the user to approve with the **Approve & launch** button or by saying so. If they want changes, edit the tasks (`mc set`, `mc add`, `mc cancel`) and summarise again.
  3. If the user approves in chat, run `mc approve`; that launches the work.
  4. **executing**: run the supervisor loop. Review reports, answer executor questions (`mc answer`), react to user messages. Keep messages to the user brief, since they see the details in the dashboard. Only ask the user for real decisions.
  5. When every task is done, verify the whole, then write a final summary.
- **Nobody sees an approval prompt for you.** You run headless: a command outside your allow-list is simply denied. Never tell the user to "approve a prompt" or to run commands for you in their terminal. When you're denied, Mission Control shows the user an Allow / Don't allow card automatically (or you can send one with `mc request-permission`), and you're woken with their answer. Ask for narrow rules (`git fetch *`, not `git *`); say what the command will do, especially anything that publishes (`git push`, `gh pr create`).
- **Permissions belong to the user.** When a report shows a denied command or tool (PERMISSION DENIED in your inbox), do NOT edit `.claude/settings.json` (headless sessions ignore it) and do not try to grant anything yourself. Ask the user: `mc request-permission T-00x '<rule>' '<rule>' --why "<what it is for>"`, preferably with the suggested rules, kept as narrow as possible. Then end your turn. You're woken when they answer; if they allowed it, send the blocked task back with `mc review T-00x changes "permission granted, retry"`.
- **Limits protect the user's money.** Each task has an attempt limit (3 by default). When it is reached the user is asked and nothing retries it automatically; you cannot start it again yourself. Don't work around it by re-adding the same task. Before the limit, change something with each retry (a sharper brief, a smaller task) instead of sending the same instructions again. Blocked-by-permission tasks are held until the user answers.
- **Models: cheaper is yours, pricier is the user's.** The user picked the executor model for the mission. For simple tasks you may choose a cheaper one yourself (`"model":"claude-haiku-5-5"` in the plan, or `mc set T-00x model=...`). Anything more expensive is refused unless the user approves it: `mc request-model <model> T-00x [T-00y ...] --why "<why the chosen model isn't enough for this task>"`. Be specific (what failed or what makes it hard), ask for as few tasks as possible, and keep working on other tasks while you wait. Never ask for a stronger model just because a task is long.
- **Never start watcher or polling scripts, `sleep` loops or background waits.** They keep your turn open and block the user's messages. If something outside Mission Control has to finish first (a long simulation, a download), register a watch and end your turn: `mc watch add "<name>" --file <path/that/appears> --fail-contains <log> Traceback --fail-if-gone "<text in the process's command line>" --task T-00x --note "<what you'll do next>"`. Mission Control checks it for free and wakes you when it's done, failed, or timed out. Use `mc watch list` to see them.
- Act, then end your turn. Never sleep or poll.

## Git and teammates
- In a git repo, the mission works on its own branch (see "Git:" in `mc status`). Each AI task runs in its own worktree and is merged into the mission branch when approved, so the user's checkout is never touched. Read the mission's current code in `.mission/worktrees/_mission`, and use `mc diff T-00x` (add `--full` for the patch) to review a task's changes.
- Tasks can go to people: `"assignee": "<github-login>"` in the plan, or `mc assign T-00x --to <login>`. Use this when the user asks, or for work that needs human access, judgment or credentials. Their brief becomes a GitHub issue. They open a PR into the mission branch, which reaches you as a REVIEW. Their questions reach you as QUESTIONs; your `mc answer` is posted back on the issue. Write human briefs a little more fully than AI ones.
- When everything is done and verified, ask the user before opening the pull request (it's visible to their team). Then run `mc pr`.
"""


def chat_log(m: Mission) -> list:
    try:
        return [json.loads(l) for l in (m.dir / "chat.jsonl").read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def chat_add(m: Mission, role: str, text: str, **extra) -> dict:
    msg = {"id": f"{time.time():.6f}", "ts": now(), "role": role, "text": text, **extra}
    with m.lock():
        with open(m.dir / "chat.jsonl", "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return msg


def chat_mark_delivered(m: Mission, ids: set) -> None:
    with m.lock():
        msgs = chat_log(m)
        for c in msgs:
            if c["id"] in ids:
                c["delivered"] = True
        write_lines = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in msgs)
        (m.dir / "chat.jsonl").write_text(write_lines)


def sup_update(m: Mission, **kw) -> dict:
    with m.lock():
        meta = m.meta()
        sup = meta.setdefault("supervisor", {})
        sup.update(kw)
        m.save_meta(meta)
        return sup


def supervisor_events(m: Mission, advance: bool = True) -> list:
    """New events the supervisor hasn't seen (same cursor as `mc wait`)."""
    cur_file = m.dir / ".supervisor_cursor"
    try:
        cursor = int(cur_file.read_text())
    except (FileNotFoundError, ValueError):
        cursor = 0
    evs = m.events(cursor)
    hits = [e for e in evs if e["kind"] in SUPERVISOR_INTERESTING
            and not (e["kind"] == "question" and e.get("to") == "user")
            and not (e["kind"] in ("answer", "approved", "note") and e.get("actor") == "supervisor")]
    if advance:
        cur_file.write_text(str(cursor + len(evs)))
    return hits


def events_total(m: Mission) -> int:
    try:
        return len((m.dir / "events.jsonl").read_text().splitlines())
    except FileNotFoundError:
        return 0


def supervisor_cursor_commit(m: Mission, value: int) -> None:
    """Mark events as handled. Only called after a turn succeeded, so a turn that is killed or fails can't lose them."""
    (m.dir / ".supervisor_cursor").write_text(str(value))


def needs_decision(m: Mission, events: list) -> bool:
    """Only wake the (expensive) supervisor when something needs judgment. Routine events such as
    auto-approved checks ride along in the next turn's updates instead of costing a turn."""
    if not events:
        return False
    tasks = {t["id"]: t for t in m.tasks()}
    for e in events:
        k = e["kind"]
        if k in ("question", "error", "note", "watch") or (k == "answer" and e.get("actor") == "user"):
            return True
        if k == "report" and tasks.get(e.get("task"), {}).get("status") in ("review", "blocked"):
            return True
    sup = m.meta().get("supervisor") or {}
    if tasks and all(t["status"] in FINAL for t in tasks.values()) and not sup.get("completion_seen"):
        return True  # one final wake to verify the whole and summarise
    return False


def pending_user_messages(m: Mission) -> list:
    return [c for c in chat_log(m) if c["role"] == "user" and not c.get("delivered")]


def wake_supervisor(m: Mission, kickoff: bool = False) -> bool:
    sup = m.meta().get("supervisor") or {}
    if sup.get("mode") != "dashboard":
        return False
    if sup.get("pid") and pid_alive(sup["pid"]):
        return False  # the running supervisor picks up queued messages before it exits
    p = subprocess.Popen([sys.executable, str(HERE / "mc.py"), "_supervise", str(m.root)] + (["--kickoff"] if kickoff else []),
                         cwd=str(m.root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=open(m.dir / "supervisor.err.txt", "a"), env=clean_env(), **compat.spawn_kwargs())
    SPAWNED.append(p)
    return True


def ensure_mc_shim() -> Path:
    bindir = MC_HOME / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "mc"
    body = f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{(HERE / "mc.py").as_posix()}" "$@"\n'
    if not shim.exists() or shim.read_text() != body:
        shim.write_text(body)
        shim.chmod(0o755)
    if compat.IS_WIN:  # cmd.exe and PowerShell look for mc.cmd; Git Bash uses the sh shim above
        cmdbody = f'@echo off\r\n"{sys.executable}" "{HERE / "mc.py"}" %*\r\n'
        cmdshim = bindir / "mc.cmd"
        if not cmdshim.exists() or cmdshim.read_text() != cmdbody:
            cmdshim.write_bytes(cmdbody.encode())
    return bindir


KICKOFF = ("The user just ignited Mission Control on this project from the dashboard. Take a quick, cheap look at "
           "the project, then greet them in one line, say in 2-3 lines what you see, and ask what they want to "
           "achieve (goal, must-haves, constraints, what 'done' looks like). Keep it to a few focused questions.")


def compose_turn(m: Mission, pending: list, events: list, kickoff: bool, fresh: bool) -> str:
    meta = m.meta()
    parts = [f"[Mission Control · phase: {meta.get('phase', 'executing')}]"]
    if fresh and not kickoff:
        parts += ["You are resuming this mission in a fresh session. Current state:", state_text(m)]
        recent = [c for c in chat_log(m) if c["role"] in ("user", "supervisor")][-12:]
        if recent:
            parts.append("Recent conversation:")
            parts += [f"{c['role'].upper()}: {short(c['text'], 600)}" for c in recent if c not in pending]
    if kickoff:
        parts.append(KICKOFF)
    if pending:
        parts.append("Message(s) from the user (reply to these in your final message):")
        parts += [f"> {c['text']}" for c in pending]
    if events:
        parts.append("Updates since your last turn:")
        parts += [f"- [{e['kind']}] {e['msg']}" for e in events]
        items = inbox_items(m, mark_read=True)
        if items:
            parts += ["Inbox:"] + items
    return "\n\n".join(parts)


def session_belongs_here(root: Path, sid: str) -> bool:
    """Claude keeps each session under the project folder it was started in. Only resume one that lives under
    THIS project's folder, never an id copied from somewhere else (that would load and append to another
    project's conversation, and bill for its whole context)."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))
    return (base / enc / f"{sid}.jsonl").exists()


def run_supervisor_turn(m: Mission, message: str, trigger: str = "") -> dict:
    meta = m.meta()
    sup = meta.get("supervisor") or {}
    rname, runner, model = resolve_runner(sup.get("runner") or "claude-supervisor", sup.get("model"))
    if not runner:
        return {"error": f"unknown supervisor runner {sup.get('runner')}"}
    system = (SKILL_DIR / "SKILL.md").read_text() + DASHBOARD_ADDENDUM
    if "{system}" not in runner["cmd"]:
        # CLIs without a system-prompt flag. Drop the YAML frontmatter: a prompt starting with "---" is parsed as an option.
        body = re.sub(r"\A---\n.*?\n---\n", "", system, count=1, flags=re.S).lstrip()
        message = body + "\n\n=====\n\n" + message
    subs = {"prompt": message, "system": system, "root": str(m.root), "mc": str(HERE / "mc.py"), "python": sys.executable,
            "task": "", "prompt_file": ""}
    cmd = apply_permissions(m, build_cmd(runner, subs, model, sup.get("effort") or ""))
    resumable = bool(runner.get("resume"))
    if resumable and sup.get("session_id") and not session_belongs_here(m.root, sup["session_id"]):
        sup_update(m, session_id=None)  # unknown to this folder: start fresh instead of touching someone else's session
        sup = {**sup, "session_id": None}
    if resumable and sup.get("session_id"):
        cmd += ["--resume", sup["session_id"]]
    if not shutil.which(cmd[0]):
        return {"error": f"'{cmd[0]}' not found on PATH"}
    try:
        cmd = compat.resolve_exe(cmd)
        if compat.cmdline_too_long(cmd):
            return {"error": "the supervisor prompt is too long for a Windows command line; run Mission Control inside WSL"}
    except RuntimeError as e:
        return {"error": str(e)}
    env = {**clean_env(), "PATH": f"{ensure_mc_shim()}{os.pathsep}{os.environ.get('PATH', '')}", "MC_ROLE": "supervisor",
           "MC_HOME": str(MC_HOME)}
    logdir = m.dir / "supervisor"
    logdir.mkdir(exist_ok=True)
    n = int(sup.get("turns", 0)) + 1
    st = StreamState(runner.get("parser", "claude"))
    t_begin = time.time()
    session_id = None
    if snapshots_on(m):
        try:
            snapshots.take(m.root, "before a supervisor turn")
        except Exception as e:
            m.event("error", "system", f"snapshot failed: {short(str(e), 200)}")
    proc = subprocess.Popen(cmd, cwd=str(m.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, env=env)
    timer = threading.Timer(int(runner.get("timeout", 1800)), compat.kill_hard, [proc])
    timer.start()
    last_ctx = 0
    st.on_result = lambda: watch_for_exit_after_result(proc)
    with open(logdir / f"turn-{n}.jsonl", "w") as log:
        log.write(json.dumps({"ts": now(), "kind": "system", "text": message}) + "\n")
        for line in proc.stdout:
            try:
                ev = json.loads(line)
                session_id = ev.get("session_id") or session_id
                if ev.get("type") == "result":
                    its = (ev.get("usage") or {}).get("iterations") or []
                    if its:
                        u = its[-1]
                        last_ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            except json.JSONDecodeError:
                pass
            for kind, text in st.feed(line):
                log.write(json.dumps({"ts": now(), "kind": kind, "text": text}, ensure_ascii=False) + "\n")
                log.flush()
                if kind == "tool":
                    sup_update(m, step=short(text, 80))
    code = proc.wait()
    timer.cancel()
    st.finish()
    t_took = time.time() - t_begin
    if not resumable:
        session_id = None
    reply = st.texts[-1] if st.texts else ""
    tok = sup.get("tokens") or {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}
    for k in ("input", "output", "cache_read", "cache_write"):
        tok[k] = tok.get(k, 0) + st.usage.get(k, 0)
    # A resumed session reports its running TOTAL cost, so charge only what this turn added.
    total = float(st.cost or 0)
    same_session = bool(resumable and sup.get("session_id") and session_id == sup.get("session_id"))
    prev = float(sup.get("session_cost") or 0) if same_session else 0.0
    delta = round(total - prev, 6) if total >= prev else round(total, 6)
    tok["cost_usd"] = round(tok.get("cost_usd", 0) + delta, 6)
    sup_update(m, turns=n, tokens=tok, session_id=(session_id or sup.get("session_id")) if resumable else None,
               context=last_ctx, step="", last_model=model_label(rname, model),
               session_cost=total if resumable else 0.0, cost_model=2)
    usage_add(m, {"kind": "supervisor", "turn": n, "model": model_label(rname, model), "effort": sup.get("effort") or "",
                  "trigger": trigger, "duration_s": round(t_took, 1), "input": st.usage.get("input", 0),
                  "cache_read": st.usage.get("cache_read", 0), "cache_write": st.usage.get("cache_write", 0),
                  "output": st.usage.get("output", 0), "cost_usd": delta, "exact": bool(st.exact), "context": last_ctx,
                  "fresh_session": not same_session, "calls": allocate_calls(st.calls, st.usage, delta)})
    return {"reply": reply, "code": code, "error": st.error if (st.error or code) else None, "context": last_ctx,
            "denied": st.denied, "denied_raw": st.denied_raw, "cost": delta}


def fix_supervisor_costs(m: Mission) -> bool:
    """One-time repair: older versions added up a running total as if it were per-turn, overstating the
    supervisor's cost. Rebuild per-turn costs from the chat log (a drop means a fresh session started)."""
    meta = m.meta()
    sup = meta.get("supervisor") or {}
    if sup.get("cost_model") == 2 or "tokens" not in sup:
        return False
    msgs = chat_log(m)
    prev, total = 0.0, 0.0
    for c in msgs:
        if c.get("role") == "supervisor" and c.get("cost") is not None:
            cum = float(c["cost"])
            delta = cum - prev if cum >= prev else cum
            prev = cum
            c["cost"] = round(delta, 6)
            total += delta
    with m.lock():
        if msgs:
            (m.dir / "chat.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in msgs))
        meta = m.meta()
        sp = meta.setdefault("supervisor", {})
        old = (sp.get("tokens") or {}).get("cost_usd", 0)
        sp.setdefault("tokens", {})["cost_usd"] = round(total, 6)
        sp["session_cost"] = prev if sp.get("session_id") else 0.0
        sp["cost_model"] = 2
        m.save_meta(meta)
    m.event("note", "system", f"supervisor cost corrected from ${old:.2f} to ${total:.2f} (older versions double-counted running totals)")
    return True


def request_supervisor_permission(m: Mission, denied_raw: list, denied: list) -> str:
    """The supervisor runs headless, so a command outside its allow-list is denied outright: nobody gets a
    prompt. Turn the denial into an Allow card the user can actually see, and wake the supervisor on the answer."""
    sug = [x for x in suggest_permissions(denied_raw) if x not in perm_list(m)]
    if not sug:
        return ""
    for q in m.questions():
        if q.get("status") == "open" and q.get("permission") and set(sug) <= set(q["permission"]):
            return ""  # already asked
    qid = m.next_id("Q", "questions")
    m.save_question({"id": qid, "task": None, "from": "system", "to": "user", "auto": True, "wake": True,
                     "question": "Your supervisor was blocked: it isn't allowed to run " + "; ".join(short(d, 70) for d in denied[:3])
                                 + ". Allow these for this project?",
                     "why": "The supervisor tried to run: " + "; ".join(short(d, 70) for d in denied[:3]),
                     "permission": sug, "options": ["Allow", "Don't allow"], "recommendation": "", "status": "open",
                     "answer": "", "created": now()})
    m.event("question", "system", f"supervisor needs permission: {', '.join(sug)}", question=qid, to="user")
    return qid


def cmd_supervise(a) -> None:
    """Internal: run supervisor turns until nothing new is waiting. One instance per mission."""
    m = Mission(a.root)
    fix_supervisor_costs(m)
    lf = open(m.dir / ".supervisor.lock", "a")
    try:
        compat.lock(lf, blocking=False)
    except BlockingIOError:
        return
    sup_update(m, pid=os.getpid(), running=True, started=now(), step="thinking")
    kickoff = a.kickoff
    try:
        for _ in range(12):
            meta = m.meta()
            sup = meta.get("supervisor") or {}
            pending = pending_user_messages(m)
            gated = meta.get("phase") in GATED_PHASES
            peek = [] if gated else supervisor_events(m, advance=False)
            if not pending and not kickoff and not needs_decision(m, peek):
                break
            events = [] if gated else supervisor_events(m, advance=False)
            cursor_target = None if gated else events_total(m)
            tasks_now = m.tasks()
            if tasks_now and all(t["status"] in FINAL for t in tasks_now):
                sup_update(m, completion_seen=True)
            if pending:
                sup_update(m, auto_turns=0)
            else:
                auto = int(sup.get("auto_turns", 0)) + 1
                cap = int(sup.get("max_auto_turns", 40))
                if auto > cap:
                    chat_add(m, "system", f"Auto-supervision paused after {cap} turns without you. Send a message to continue.")
                    break
                sup_update(m, auto_turns=auto)
            fresh = not sup.get("session_id")
            msg = compose_turn(m, pending, events, kickoff, fresh)
            trig = ("kickoff" if kickoff else "your message: " + short(pending[0]["text"], 90) if pending else
                    f"{len(events)} update(s): " + ", ".join(sorted({e['kind'] for e in events})))
            res = run_supervisor_turn(m, msg, trig)
            if res.get("error") and sup.get("session_id") and not res.get("reply"):
                sup_update(m, session_id=None)  # resume failed: start a fresh session from STATE
                res = run_supervisor_turn(m, compose_turn(m, pending, events, kickoff, True), trig + " (fresh session)")
            chat_mark_delivered(m, {c["id"] for c in pending})
            if cursor_target is not None and (res.get("reply") or not res.get("error")):
                supervisor_cursor_commit(m, cursor_target)
            kickoff = False
            if res.get("reply"):
                chat_add(m, "supervisor", REPORT_RE.sub("", res["reply"]).strip(), cost=res.get("cost"))
            if res.get("error"):
                chat_add(m, "system", f"Supervisor turn had a problem: {res['error']}")
                if not res.get("reply"):
                    break
            if res.get("denied"):
                made = request_supervisor_permission(m, res.get("denied_raw") or [], res["denied"])
                chat_add(m, "system", "Supervisor was denied: " + "; ".join(res["denied"][:3])
                         + (f". I've asked you under Decisions waiting on you ({made}): Allow or Don't allow." if made else ""))
            if res.get("context", 0) > int(sup.get("max_context", 120000)):
                (m.dir / "STATE.md").write_text(state_text(m))
                sup_update(m, session_id=None)
                chat_add(m, "system", "Supervisor context got large, so the next turn starts a fresh session from STATE.md (saves tokens).")
    finally:
        sup_update(m, pid=None, running=False, step="")
        compat.unlock(lf)


def set_models(m: Mission, sup_runner=None, sup_model=None, exec_runner=None, exec_model=None,
               sup_effort=None, exec_effort=None) -> None:
    """Choose the expensive (supervisor) and cheap (executor) provider+model for a mission."""
    with m.lock():
        meta = m.meta()
        sup = meta.setdefault("supervisor", {})
        if sup_runner is not None:
            if sup_runner != sup.get("runner"):
                sup["session_id"] = None  # a different CLI can't resume the old session
            sup["runner"] = sup_runner
        if sup_model is not None:
            if sup_model != sup.get("model"):
                sup["session_id"] = None
            sup["model"] = sup_model
        if sup_effort is not None:
            sup["effort"] = sup_effort
        if exec_effort is not None:
            meta["executor_effort"] = exec_effort
        old = meta.get("default_executor")
        if exec_runner is not None:
            meta["default_executor"] = exec_runner
        if exec_model is not None:
            meta["executor_model"] = exec_model
        m.save_meta(meta)
        if exec_runner is not None and exec_runner != old:
            for t in m.tasks():  # move tasks that haven't run yet to the new provider
                if t["status"] in RUNNABLE | {"blocked"} and t["executor"] == old:
                    t["executor"] = exec_runner
                    m.save_task(t)
    s = m.meta()
    m.event("models", "user", f"supervisor {model_label(s['supervisor'].get('runner', 'claude-supervisor'), s['supervisor'].get('model', ''))}"
                              f" · executors {model_label(s.get('default_executor', 'claude'), s.get('executor_model', ''))}")


def ignite(root: Path, name: str | None = None, models: dict | None = None) -> Mission:
    m = Mission(root)
    for d in ("tasks", "reports", "feedback", "questions", "runs"):
        (m.dir / d).mkdir(parents=True, exist_ok=True)
    meta = m.meta() or {"created": now()}
    meta.update(name=name or meta.get("name") or root.name, goal=meta.get("goal", ""), max_parallel=meta.get("max_parallel", 3),
                default_executor=meta.get("default_executor") or default_provider(), autopilot=False,
                phase="briefing" if not m.tasks() else meta.get("phase", "executing"))
    meta["supervisor"] = {**meta.get("supervisor", {}), "mode": "dashboard",
                          "runner": (meta.get("supervisor") or {}).get("runner") or default_supervisor_runner()}
    m.save_meta(meta)
    if models:
        set_models(m, models.get("sup_runner"), models.get("sup_model"), models.get("exec_runner"), models.get("exec_model"),
                   models.get("sup_effort"), models.get("exec_effort"))
    (m.dir / ".gitignore").write_text("runs/\nsupervisor/\n.lock\n.*.lock\n.*.tmp\n.supervisor_cursor\n*.err.txt\n")
    register(root, meta["name"])
    m.event("init", "user", f"mission '{meta['name']}' ignited from the dashboard")
    wake_supervisor(m, kickoff=True)
    return m


def approve_plan(m: Mission, by: str, allow: list | None = None) -> list:
    """Approve the plan. `allow` (user only) is the list of permissions from the plan's preflight card that the
    user ticked: they're granted in the same step, so the executors don't stop to ask later."""
    granted = []
    if by == "user" and allow:
        wanted = {x["rule"] for x in plan_permission_rules(m)}
        granted = perm_add(m, [r for r in allow if r in wanted], "user")
    meta = m.meta()
    meta.update(phase="executing", autopilot=True)
    meta.pop("plan_permissions", None)
    m.save_meta(meta)
    m.event("approved", by, "plan approved: launching" + (f" (allowed: {', '.join(granted)})" if granted else ""))
    if by == "user":
        chat_add(m, "system", "✓ Plan approved." + (f" Allowed: {', '.join(granted)}." if granted else "") + " Executors are launching.")
    return schedule(m)


# ───────────────────────────── CLI commands ─────────────────────────────

def cmd_init(a):
    root = Path(a.path or os.getcwd()).resolve()
    m = Mission(root)
    for d in ("tasks", "reports", "feedback", "questions", "runs"):
        (m.dir / d).mkdir(parents=True, exist_ok=True)
    meta = m.meta() or {"created": now()}
    meta.update(name=a.name or meta.get("name") or root.name, goal=a.goal or meta.get("goal", ""),
                max_parallel=a.parallel or meta.get("max_parallel", 3),
                default_executor=a.executor or meta.get("default_executor") or default_provider(),
                executor_model=a.executor_model or meta.get("executor_model", ""),
                autopilot=meta.get("autopilot", False), phase=meta.get("phase", "executing"))
    meta.setdefault("supervisor", {"mode": "external"})
    m.save_meta(meta)
    (m.dir / ".gitignore").write_text("runs/\nsupervisor/\n.lock\n.*.lock\n.*.tmp\n.supervisor_cursor\n*.err.txt\n")
    register(root, meta["name"])
    m.event("init", "supervisor", f"mission '{meta['name']}' initialised")
    print(f"mission ready at {m.dir}")


def cmd_plan(a):
    m = need_mission()
    data = json.loads(sys.stdin.read() if a.file == "-" else Path(a.file).read_text())
    specs = data.get("tasks", data) if isinstance(data, dict) else data
    if isinstance(data, dict) and data.get("goal"):
        meta = m.meta()
        meta["goal"] = data["goal"]
        m.save_meta(meta)
    declared = [{"rule": (x.get("rule") if isinstance(x, dict) else x) or "", "why": (x.get("why", "") if isinstance(x, dict) else "")}
                for x in ((data.get("permissions") or []) if isinstance(data, dict) else [])]
    bad = [d["rule"] for d in declared if not perm_valid(d["rule"])]
    if bad:
        die(f"not valid permission rules: {bad}. Use e.g. 'Bash(/path/to/python *)' or 'WebSearch'")
    keymap = {t["id"]: t["id"] for t in m.tasks()}
    created = []
    with m.lock():
        for spec in specs:
            tid = m.next_id("T", "tasks")
            keymap[spec.get("key") or tid] = tid
            spec["_id"] = tid
            m.save_task(new_task(m, spec, tid))
            created.append(spec)
        for spec in created:
            t = m.task(spec["_id"])
            deps = spec.get("depends_on") or spec.get("deps") or []
            missing = [d for d in deps if d not in keymap]
            if missing:
                die(f"{t['id']} depends on unknown {missing}")
            t["depends_on"] = [keymap[d] for d in deps]
            m.save_task(t)
    m.event("plan", "supervisor", f"planned {len(created)} tasks")
    meta = m.meta()
    if declared:
        meta["plan_permissions"] = declared + [d for d in (meta.get("plan_permissions") or []) if d.get("rule") not in {x["rule"] for x in declared}]
        m.save_meta(meta)
    if meta.get("phase") == "briefing":
        meta["phase"] = "awaiting_approval"
        m.save_meta(meta)
        print("plan saved. Mission is awaiting the user's approval; nothing runs until then.")
    for spec in created:
        t = m.task(spec["_id"])
        print(f"{t['id']}  {t['title']}" + (f"  ← {', '.join(t['depends_on'])}" if t["depends_on"] else ""))


def cmd_add(a):
    m = need_mission()
    with m.lock():
        tid = m.next_id("T", "tasks")
        m.save_task(new_task(m, {"title": a.title, "goal": a.goal, "accept": a.accept, "files": a.files,
                                 "depends_on": a.deps, "executor": a.executor, "estimate": a.estimate,
                                 "check": a.check, "model": a.model}, tid))
    m.event("task_added", "supervisor", f"{tid} added: {a.title}", task=tid)
    print(tid)


def cmd_set(a):
    m = need_mission()

    def f(t):
        for kv in a.pairs:
            k, _, v = kv.partition("=")
            if k in ("model", "executor"):
                ok_, why = check_model_choice(m, v if k == "executor" else t.get("executor"), v if k == "model" else t.get("model"))
                if not ok_:
                    print(f"note: ignored {k}={v}: {why}. {MODEL_NOTE}", file=sys.stderr)
                    continue
            try:
                t[k] = json.loads(v)
            except json.JSONDecodeError:
                t[k] = v
    m.update_task(a.task, f)
    m.event("task_updated", "supervisor", f"{a.task} updated: {', '.join(a.pairs)}", task=a.task)


def cmd_status(a):
    m = need_mission()
    if a.json:
        print(json.dumps({"mission": m.meta(), "tasks": m.tasks()}, indent=2))
    else:
        print(state_text(m))


def cmd_checkpoint(a):
    m = need_mission()
    (m.dir / "STATE.md").write_text(state_text(m))
    m.event("checkpoint", "supervisor", "STATE.md refreshed")
    print(f"wrote {m.dir / 'STATE.md'}  (a fresh supervisor can resume from it)")


def cmd_brief(a):
    m = need_mission()
    print(build_brief(m, m.task(a.task)))


def cmd_run(a):
    m = need_mission()
    if a.ready or not a.tasks:
        launched = schedule(m)
        print("launched: " + (", ".join(launched) or "nothing ready"))
        return
    for tid in a.tasks:
        try:
            pid = launch(m, tid, force=os.environ.get("MC_ROLE") != "supervisor")
            t = m.task(tid)
            print(f"{tid} handed to @{t.get('assignee')} ({t.get('step')})" if is_human(t)
                  else f"{tid} queued: the dashboard starts it within a few seconds" if not pid else f"{tid} started (pid {pid})")
        except RuntimeError as e:
            print(f"{tid}: {e}")


def cmd_stop(a):
    m = need_mission()
    stop_task(m, a.task, "cancelled" if a.cmd == "cancel" else "todo")


def cmd_progress(a):
    m = need_mission()

    def f(t):
        t["progress"] = max(0, min(100, a.pct))
        if a.step:
            t["step"] = " ".join(a.step)
    m.update_task(a.task, f)


def cmd_report(a):
    """Manual mode: an executor without a runner reports by command."""
    m = need_mission()
    t = m.task(a.task)
    if t["status"] != "running":
        m.update_task(a.task, lambda x: x.update(status="running", run={"attempt": len(x.get("attempts", [])) + 1, "started": now()}))
    attempt = m.task(a.task)["run"]["attempt"]
    rep = {"status": a.status, "summary": a.summary, "files": a.files, "confidence": a.confidence,
           "question": a.question, "options": a.options}
    usage = {"input": a.tokens_in, "output": a.tokens_out, "cache_read": 0, "cache_write": 0}
    finalize(m, a.task, attempt, rep, usage, None, False, executor=t["executor"] + " (manual)")
    print(f"{a.task} → {m.task(a.task)['status']}")


def cmd_request_permission(a):
    """Supervisor asks the USER to allow tool rules for this project. Nothing is granted until they say yes."""
    m = need_mission()
    bad = [x for x in a.rules if not perm_valid(x)]
    if bad:
        die(f"not valid permission rules: {bad}. Use e.g. 'Bash(/path/to/python *)' or 'WebSearch'")
    qid = m.next_id("Q", "questions")
    why = " ".join(a.why or []) or "An executor was blocked by a missing permission."
    m.save_question({"id": qid, "task": a.task, "from": "supervisor", "to": "user",
                     "question": f"Allow these for this project? {why}", "why": why, "permission": a.rules,
                     "options": ["Allow", "Don't allow"], "recommendation": "Allow", "status": "open", "answer": "",
                     "created": now()})
    m.event("question", "supervisor", f"supervisor asks permission: {', '.join(a.rules)}", task=a.task, question=qid, to="user")
    print(f"{qid} sent to the user. Don't retry the blocked task until they've answered; you'll be woken.")


def cmd_request_model(a):
    """Supervisor asks the USER to approve a more expensive model for some tasks."""
    m = need_mission()
    meta = m.meta()
    head, _, rest = a.model.partition(":")
    prov, mdl = (head, rest) if rest and head in load_runners() else (meta.get("default_executor", "claude"), a.model)
    base_name, _, base_model = resolve_runner(meta.get("default_executor", "claude"), meta.get("executor_model"))
    pb, pt = model_price(base_name, base_model), model_price(prov, mdl)
    cost = f" (about {pt / pb:.0f}x the {base_model} the user chose)" if pb and pt else ""
    for tid in a.tasks:
        m.task(tid)
    qid = m.next_id("Q", "questions")
    why = " ".join(a.why or []) or "These tasks are hard for the chosen model."
    m.save_question({"id": qid, "task": a.tasks[0], "from": "supervisor", "to": "user",
                     "question": f"Use {prov}:{mdl}{cost} for {', '.join(a.tasks)}? {why}",
                     "model_request": {"tasks": a.tasks, "executor": prov, "model": mdl},
                     "options": ["Allow", "Don't allow"], "recommendation": "", "status": "open", "answer": "", "created": now()})
    m.event("question", "supervisor", f"supervisor asks to use {prov}:{mdl} for {', '.join(a.tasks)}", task=a.tasks[0], question=qid, to="user")
    print(f"{qid} sent to the user. Keep working on other tasks; you'll be woken when they answer.")


def cmd_allow(a):
    """The user's own control over project permissions (the supervisor may not use this)."""
    m = need_mission()
    if os.environ.get("MC_ROLE") == "supervisor":
        die("you can't grant permissions yourself. Ask the user: mc request-permission <task> '<rule>' --why \"...\"")
    if a.remove:
        print("removed" if perm_remove(m, a.remove, "user") else "not in the list")
    elif a.rules:
        bad = [x for x in a.rules if not perm_valid(x)]
        if bad:
            die(f"not valid permission rules: {bad}")
        print("added: " + (", ".join(perm_add(m, a.rules, "user")) or "nothing new"))
    print("project allows: " + (", ".join(perm_list(m)) or "(only the defaults)"))


def cmd_ask(a):
    m = need_mission()
    qid = m.next_id("Q", "questions")
    m.save_question({"id": qid, "task": a.task, "from": a.sender, "to": a.to, "question": a.question,
                     "options": a.options, "recommendation": a.recommend or "", "status": "open", "answer": "",
                     "created": now()})
    m.event("question", a.sender, f"{a.sender} asks {a.to}: {short(a.question, 140)}", task=a.task, question=qid, to=a.to)
    print(qid)


def answer_question(m: Mission, qid: str, text: str, by: str) -> dict:
    q = read_json(m.dir / "questions" / f"{qid}.json")
    if not q:
        die(f"no question {qid}")
    if (q.get("permission") or q.get("model_request")) and by != "user":  # only the person can answer these
        die("this request can only be answered by the user")
    q.update(status="answered", answer=text, answered_by=by, answered_at=now())
    if q.get("model_request"):
        mr = q["model_request"]
        if text.strip().lower().startswith("allow"):
            for tid in mr["tasks"]:
                m.update_task(tid, lambda t: t.update(executor=mr["executor"], model=mr["model"]))
            q["answer"] = f"Allowed {mr['executor']}:{mr['model']} for {', '.join(mr['tasks'])}"
            m.event("task_updated", "user", f"{', '.join(mr['tasks'])} → {mr['executor']}:{mr['model']} (approved)", task=mr["tasks"][0])
        else:
            q["answer"] = "Not allowed. " + text
    elif q.get("permission"):
        if text.strip().lower().startswith("allow"):
            granted = perm_add(m, q["permission"], "user")
            q["answer"] = "Allowed: " + ", ".join(q["permission"]) + ("" if text.strip().lower() == "allow" else f" (user said: {text})")
            q["granted"] = granted or q["permission"]
        else:
            q["answer"] = "Not allowed. " + text
    m.save_question(q)
    tid_q = q.get("task")
    if q.get("kind") == "attempt_limit" and by == "user" and tid_q:
        if text.strip().lower().startswith("allow"):
            extra = int(q.get("extra") or 2)
            m.update_task(tid_q, lambda t: t.update(attempt_bonus=int(t.get("attempt_bonus") or 0) + extra, paused=False,
                                                    step=f"{extra} more attempts allowed by you"))
            m.event("task_updated", "user", f"{tid_q}: {extra} more attempts allowed", task=tid_q)
        else:
            m.update_task(tid_q, lambda t: t.update(paused=True, step="stopped by you at the attempt limit"))
            m.event("stopped", "user", f"{tid_q} stopped at the attempt limit", task=tid_q)
    if q.get("auto") and q.get("permission") and by == "user" and tid_q and q["answer"].startswith("Allowed"):
        with open(m.dir / "feedback" / f"{tid_q}.md", "a") as fbf:
            fbf.write(f"\n### Permission granted ({now()})\nThe user allowed: {', '.join(q['permission'])}. Retry the task; "
                      "those commands now work.\n")
        for oq in open_questions_for(m, tid_q):  # the executor's own question about the same wall is now moot
            if oq["id"] != q["id"] and oq.get("to") == "supervisor" and oq.get("from") == tid_q:
                oq.update(status="answered", answered_by="system", answered_at=now(),
                          answer="The user allowed it (" + ", ".join(q["permission"]) + "). Retry the task.")
                m.save_question(oq)
        m.update_task(tid_q, lambda t: t.update(status="needs_changes", step="permission granted, ready to retry")
                      if t["status"] == "blocked" else None)
        if m.meta().get("autopilot"):
            schedule(m)
    if q.get("github_issue"):
        try:
            gitops.issue_comment(m, q["github_issue"], f"**Supervisor:** {text}")
        except gitops.GitError as e:
            m.event("error", "system", f"couldn't post the answer to issue #{q['github_issue']}: {short(str(e), 120)}")
    # When the system asked and the user said yes, the retry is automatic: no supervisor turn (no tokens) needed.
    # A "no" still goes to the supervisor, which has to decide what to do instead.
    handled = bool((q.get("auto") or q.get("kind") == "attempt_limit") and not q.get("wake")) and q["answer"].lower().startswith("allow")
    m.event("permission" if handled else "answer", by, f"{qid} answered: {short(q['answer'], 140)}",
            task=q.get("task"), question=qid, to=q.get("from"))
    tid = q.get("task")
    if tid and q.get("to") == "supervisor":
        still_open = [x for x in m.questions() if x.get("task") == tid and x["status"] == "open"]
        if not still_open:
            def f(t):
                if t["status"] == "blocked":
                    t["status"] = "todo"
                    t["step"] = "answer received, ready to resume"
            m.update_task(tid, f)
            if m.meta().get("autopilot"):
                schedule(m)
    return q


def cmd_answer(a):
    m = need_mission()
    answer_question(m, a.qid, " ".join(a.text), a.by)
    print(f"{a.qid} answered")


def review_task(m: Mission, tid: str, verdict: str, msg: str = "", by: str = "supervisor") -> str:
    """Approve (merging into the mission branch in git mode) or request changes. Returns a status line."""
    t = m.task(tid)
    human = is_human(t)
    g = t.get("github") or {}
    if verdict == "approve":
        if gitops.enabled(m):
            merged, info = gitops.integrate(m, tid, t["title"], human=human)
            if not merged:
                return review_task(m, tid, "changes", f"Approved, but merging into the mission branch failed ({info}). "
                                   "Other tasks changed the same code: update your branch from the mission branch, resolve, and resubmit.", by)
        m.update_task(tid, lambda x: x.update(status="done", step="approved" + (" · merged" if gitops.enabled(m) else ""),
                                              progress=100, finished=now()))
        m.event("approved", by, f"{tid} approved" + (f": {short(msg, 120)}" if msg else ""), task=tid)
        if human and g.get("issue") and gitops.gh_ready(m):
            try:
                gitops.close_issue(m, g["issue"], "Approved and merged into the mission branch. Thank you!" + (f"\n\n{msg}" if msg else ""))
            except gitops.GitError:
                pass
        out = f"{tid} approved"
    else:
        if not msg:
            die("changes need a message: what exactly to fix")
        rnd = len(t.get("attempts", []))
        with open(m.dir / "feedback" / f"{tid}.md", "a") as f:
            f.write(f"\n### Round {rnd} feedback ({now()})\n{msg}\n")
        if human:
            target = (g.get("pr") or {}).get("number")
            try:
                if gitops.gh_ready(m) and target:
                    gitops.pr_comment(m, target, f"**Supervisor review: changes requested**\n\n{msg}")
                elif gitops.gh_ready(m) and g.get("issue"):
                    gitops.issue_comment(m, g["issue"], f"**Supervisor review: changes requested**\n\n{msg}")
            except gitops.GitError as e:
                m.event("error", "system", f"couldn't post review to GitHub: {short(str(e), 120)}", task=tid)

            def f(x):
                x["status"] = "running"
                x["step"] = f"changes requested from @{x.get('assignee')}"
                x.setdefault("github", {})["reviewed_sha"] = (x.get("github", {}).get("pr") or {}).get("sha")
            m.update_task(tid, f)
        else:
            m.update_task(tid, lambda x: x.update(status="needs_changes", step="changes requested"))
        m.event("changes", by, f"{tid} needs changes: {short(msg, 140)}", task=tid)
        if rnd >= int(m.meta().get("max_attempts", 3)) and not human:
            out = f"warning: {tid} has had {rnd} attempts. Consider doing it yourself or splitting it."
        else:
            out = f"{tid} sent back with your feedback"
    if m.meta().get("autopilot"):
        schedule(m)
    return out


def cmd_review(a):
    m = need_mission()
    print(review_task(m, a.task, a.verdict, " ".join(a.message or [])))


def sync_humans(m: Mission) -> int:
    """Pull news for human tasks from GitHub: comments become questions, PRs become reviews."""
    if not gitops.gh_ready(m):
        return 0
    changes = 0
    for t in m.tasks():
        if not is_human(t) or t["status"] not in ("running", "review", "blocked"):
            continue
        tid, g = t["id"], t.get("github") or {}
        try:
            d = gitops.sync_task(m, tid, g.get("issue"), int(g.get("seen_comments", 0)))
        except gitops.GitError as e:
            m.event("error", "system", f"GitHub sync for {tid} failed: {short(str(e), 150)}", task=tid)
            continue
        for c in d["comments"]:
            qid = m.next_id("Q", "questions")
            m.save_question({"id": qid, "task": tid, "from": f"@{c['author']}", "to": "supervisor", "question": c["body"],
                             "options": [], "recommendation": "", "status": "open", "answer": "", "created": now(),
                             "github_issue": g.get("issue"), "url": c.get("url")})
            m.event("question", f"@{c['author']}", f"@{c['author']} on {tid}: {short(c['body'], 140)}", task=tid, question=qid, to="supervisor")
            changes += 1
        pr = d.get("pr")
        new_status = None
        if pr and pr["state"] == "MERGED" and t["status"] != "done":
            new_status = "merged"
        elif pr and pr["state"] == "OPEN" and t["status"] == "running" and pr.get("sha") != g.get("reviewed_sha"):
            new_status = "review"
        elif d.get("issue_state") == "CLOSED" and not pr and t["status"] == "running":
            new_status = "closed"

        def f(x):
            x.setdefault("github", {}).update(seen_comments=d["seen"])
            if pr:
                x["github"]["pr"] = pr
            if new_status == "review":
                x.update(status="review", step=f"PR #{pr['number']} by @{pr['author']} awaiting review", progress=100,
                         summary=f"@{pr['author']} opened PR #{pr['number']}: {pr['url']}")
            elif new_status == "merged":
                x.update(status="done", step=f"PR #{pr['number']} merged", progress=100, finished=now())
            elif new_status == "closed":
                x.update(status="review", step="issue closed without a PR", progress=100,
                         summary=f"@{x.get('assignee')} closed the issue without opening a pull request.")
        m.update_task(tid, f)
        if new_status == "review":
            (m.dir / "reports").mkdir(exist_ok=True)
            (m.dir / "reports" / f"{tid}.md").write_text(
                f"# {tid} · pull request #{pr['number']} by @{pr['author']}\n\n{pr['url']}\n\n```\n"
                + gitops.diff(m, tid, human=True)[:3000] + "\n```\n\nReview with `mc diff " + tid + " --full`, then approve or request changes.\n")
            m.event("report", f"@{pr['author']}", f"{tid}: @{pr['author']} opened PR #{pr['number']} for review", task=tid, status="done")
        elif new_status == "merged":
            try:
                gitops.integrate(m, tid, t["title"], human=True)
            except gitops.GitError:
                pass
            m.event("approved", f"@{pr['author']}", f"{tid}: PR #{pr['number']} was merged into the mission branch", task=tid)
        elif new_status == "closed":
            m.event("report", f"@{t.get('assignee')}", f"{tid}: issue closed without a PR", task=tid, status="done")
        changes += 1 if new_status else 0
    if changes and m.meta().get("autopilot"):
        schedule(m)
    return changes


def cmd_sync(a):
    m = need_mission()
    print(f"{sync_humans(m)} update(s) from GitHub")


def cmd_assign(a):
    m = need_mission()
    who = a.to.lstrip("@")
    runners = load_runners()
    name = who.split(":")[0]
    if name in runners or name in RUNNER_ALIASES:
        ok_, why = check_model_choice(m, name, who.partition(":")[2])
        if not ok_:
            die(f"{why}. {MODEL_NOTE}")
    if name in runners or name in RUNNER_ALIASES:
        def f(t):
            t["executor"], _, model = who.partition(":")
            t["model"] = model
            t["assignee"] = ""
    else:
        def f(t):
            t["executor"], t["assignee"] = "human", who
    t = m.update_task(a.task, f)
    m.event("task_updated", "supervisor", f"{a.task} assigned to {'@' + who if is_human(t) else who}", task=a.task)
    print(f"{a.task} → {'@' + who if is_human(t) else who}")


def mark_done(m: Mission, tid: str, note: str = "", by: str = "user") -> None:
    t = m.task(tid)
    if t["status"] != "running":
        m.update_task(tid, lambda x: x.update(status="running", run={"attempt": len(x.get("attempts", [])) + 1, "started": now(),
                                                                      "human": is_human(x)}))
    attempt = m.task(tid)["run"]["attempt"]
    finalize(m, tid, attempt, {"status": "done", "summary": note or f"Marked done by {by}.", "confidence": "medium"},
             {}, None, False, executor=f"@{t.get('assignee')}" if is_human(t) else by)


def cmd_done(a):
    m = need_mission()
    mark_done(m, a.task, " ".join(a.note or []))
    print(f"{a.task} → {m.task(a.task)['status']}")


def cmd_diff(a):
    m = need_mission()
    if not gitops.enabled(m):
        die("git mode is off for this mission")
    print(gitops.diff(m, a.task, full=a.full, human=is_human(m.task(a.task))))


def pr_body(m: Mission) -> str:
    meta = m.meta()
    lines = [f"## {meta.get('name', 'Mission')}", "", meta.get("goal", ""), "", "### Tasks"]
    for t in m.tasks():
        if t["status"] == "cancelled":
            continue
        who = f"@{t['assignee']}" if is_human(t) else (t.get("run") or {}).get("model") or t["executor"]
        lines.append(f"- [{'x' if t['status'] == 'done' else ' '}] **{t['id']} {t['title']}** ({who}): {short(t.get('summary', ''), 160)}")
    dec = m.dir / "decisions.md"
    if dec.exists() and dec.read_text().strip():
        lines += ["", "### Decisions", dec.read_text().strip()]
    lines += ["", "_Planned, delegated and reviewed with Mission Control: a supervisor model with cheaper executor models and teammates._"]
    return "\n".join(lines)


def pr_text(m: Mission) -> tuple:
    """Title and description for the pull request: the ones the user wrote (mc clean-branch --title/--body-file),
    otherwise a generated summary."""
    meta, g = m.meta(), gitops.cfg(m)
    title = g.get("pr_title") or f"{meta.get('name', 'Mission')}: {short(meta.get('goal', ''), 80)}".rstrip(": ")
    return title, (g.get("pr_body") or pr_body(m))


def clean_branch(m: Mission) -> dict:
    title, body = pr_text(m)
    info = gitops.make_clean_branch(m, title, body)
    git_save(m)({"clean_for": info["tip"], "clean_branch": info["branch"]})
    return info


def open_pr(m: Mission, by: str = "user", target: str | None = None, as_is: bool = False, clean: bool = False) -> str:
    if not gitops.enabled(m):
        raise gitops.GitError("git mode is off for this mission")
    meta = m.meta()
    head = None
    if clean:
        head, as_is = clean_branch(m)["branch"], True
    title, body = pr_text(m)
    url = gitops.create_pr(m, title, body, target, as_is, head)
    git_save(m)({"pushed": True, "pr": url, "pr_target": target or gitops.cfg(m)["base"]})
    msg = f"Pull request ready: {url}" if url else f"Pushed {gitops.cfg(m)['branch']}. Open the pull request on your git host."
    m.event("pr", by, msg)
    chat_add(m, "system", msg)
    return url


def print_clean_preview(pv: dict) -> None:
    print(f"Branch:       {pv['branch']}  (ONE commit {pv['sha']}, parent {pv['parent']}"
          + (", which is already in " + pv["target"] + ")" if pv["parent_in_target"] else ", NOT in " + pv["target"] + ")"))
    print(f"Commit:       {pv['message']}")
    print(f"Pull request: {pv['branch']}  →  {pv['target']}  on  {pv['repo']}")
    print(f"\nFiles it changes ({pv['stat'].strip()}):")
    for ln in pv["files"]:
        add, dele, name = (ln.split("\t") + ["", "", ""])[:3]
        print(f"  +{add:<5} -{dele:<5} {name}")
    print(f"\nAgent tool folders in the branch's files:   {', '.join(pv['tool_in_tree']) or 'none'}")
    print(f"Agent tool folders anywhere in its history: {', '.join(pv['tool_in_history']) or 'none'}")
    print(f"\n{pv['target']} has {pv['behind']} newer commit(s)" + (f"; GitHub would flag conflicts in: {', '.join(pv['conflicts'])}" if pv["conflicts"] else "; no conflicts"))


def cmd_clean_branch(a):
    m = need_mission()
    if not gitops.enabled(m):
        die("git mode is off for this mission")
    if a.title or a.body_file:
        upd = {}
        if a.title:
            upd["pr_title"] = a.title.strip()
        if a.body_file:
            upd["pr_body"] = Path(a.body_file).read_text().strip()
        if upd != {k: gitops.cfg(m).get(k) for k in upd}:
            git_save(m)({**upd, "clean_for": None})  # new wording: rebuild the branch so the commit carries it
    try:
        info = clean_branch(m)
        target = a.target or gitops.remote_default_branch(m)
        pv = gitops.clean_preview(m, info["branch"], target)
    except gitops.GitError as e:
        die(str(e))
    print(("Reusing" if info["reused"] else "Created") + f" the local branch {info['branch']}. NOTHING is published.\n")
    print_clean_preview(pv)
    print(f"\nTo publish exactly this: mc pr --target {target} --clean")


def cmd_pr(a):
    m = need_mission()
    if a.dry_run and a.clean:
        die("use `mc clean-branch` to preview the clean branch")
    if a.dry_run:
        try:
            pv = gitops.preview_pr(m, a.target, a.as_is)
        except gitops.GitError as e:
            die(str(e))
        print(f"DRY RUN: nothing is pushed and no pull request is opened.\n")
        print(f"Repository:        {pv.get('repo') or pv['remote']}")
        print(f"Pull request:      {pv.get('will_push_branch', '?')}  →  {pv['target']}")
        if pv.get("error"):
            print(f"Problem:           {pv['error']}")
            return
        print(f"Mission built on:  {pv['built_on']}" + ("" if pv["base_on_remote"] else f"  (the branch '{pv['base']}' is NOT on {pv['remote']})"))
        if pv.get("empty"):
            print("The mission has no changes of its own to propose.")
            return
        if pv.get("as_is"):
            print(f"Published as is: the branch is pushed unchanged and GitHub compares it with where it forked from {pv['target']}.")
            print(f"{pv['target']} has {pv.get('behind', 0)} newer commit(s)" + (f", and these files would show as conflicting on GitHub: {', '.join(pv['conflicts'])}" if pv.get("conflicts") else ", with no conflicts"))
        else:
            print(f"Applies on {pv['target']}: " + ("yes, cleanly" if pv.get("applies_cleanly") else f"NO: {pv.get('apply_error', '')} {', '.join(pv.get('conflicts', []))}"))
        if pv.get("files"):
            print(f"\nFiles that would be published ({pv.get('stat', '').strip()}):")
            for ln in pv["files"]:
                add, dele, name = (ln.split("\t") + ["", "", ""])[:3]
                print(f"  +{add:<5} -{dele:<5} {name}")
        if pv.get("tool_dirs_in_branch"):
            print(f"\nLeft out (agent tool folders on the mission branch): {', '.join(pv['tool_dirs_in_branch'])}")
        if pv.get("unpublished_not_included"):
            print(f"\nNOT included: {len(pv['unpublished_not_included'])} commit(s) the mission is built on that exist only on your machine:")
            for ln in pv["unpublished_not_included"][:8]:
                print(f"  {ln}")
        if pv.get("mission_commits"):
            print(f"\nMission commits the changes come from ({len(pv['mission_commits'])}):")
            for ln in pv["mission_commits"][:10]:
                print(f"  {ln}")
        return
    try:
        print(open_pr(m, os.environ.get("MC_ROLE") or "user", a.target, a.as_is, a.clean) or "pushed (no GitHub CLI or GitHub remote, so open the PR on your host)")
    except gitops.BaseMissing as e:
        die(f"{e}\nRun: mc pr --target {e.suggest}")
    except gitops.GitError as e:
        die(str(e))


def cmd_sync_base(a):
    """Bring the mission up to date with the latest remote branch (e.g. main): create a task that merges it in and
    resolves the conflicts, so the pull request is main plus the mission's own work."""
    m = need_mission()
    if not gitops.enabled(m):
        die("git mode is off for this mission")
    c = gitops.cfg(m)
    target = a.target or gitops.remote_default_branch(m)
    try:
        git_run = lambda *args: gitops.git(m.root, *args)
        git_run("fetch", "-q", c["remote"], target)
    except gitops.GitError as e:
        die(f"couldn't fetch {target} from {c['remote']}: {e}")
    ref = f"{c['remote']}/{target}"
    if gitops.ok(m.root, "merge-base", "--is-ancestor", ref, c["branch"]):
        print(f"already up to date: the mission branch contains everything on {ref}")
        return
    files = gitops.predict_conflicts(m, ref)
    behind = len(gitops.git(m.root, "rev-list", f"{c['branch']}..{ref}").split())
    with m.lock():
        tid = m.next_id("T", "tasks")
        goal = (f"The mission branch is {behind} commit(s) behind {ref}, and {ref} changed some of the same files. "
                f"{ref} has ALREADY been merged into your working copy, and the merge is unfinished.\n"
                + (f"Conflicts to resolve: {', '.join(files)}.\n" if files else "")
                + "Resolve every conflict so that BOTH sides' intent survives: keep the owner's newer changes from the base branch, and re-apply "
                  "the mission's own work on top of them (the mission's changes are what this project's tasks built; see .mission/reports/ for what each did). "
                  "Do not discard either side. Then run the project's tests and make them pass.")
        spec = {"title": f"Bring the mission up to date with {target}", "goal": goal, "files": files, "estimate": 3,
                "accept": [f"no conflict markers remain in any file", f"the merge of {ref} is complete", "the project's full test suite passes"]
                + ([f"`{a.check}` passes"] if a.check else []),
                "check": a.check or "", "model": a.model or ""}
        t = new_task(m, spec, tid)
        t["merge_from"] = ref
        m.save_task(t)
    git_save(m)({"base": target, "start": ref, "synced_with": ref})
    m.event("task_added", os.environ.get("MC_ROLE") or "user", f"{tid} added: bring the mission up to date with {target}", task=tid)
    print(f"{tid} created: merge {ref} into the mission ({behind} commits behind)" + (f"; expected conflicts: {', '.join(files)}" if files else "; no conflicts expected"))
    print(f"The pull request will target {target}. Run it with: mc run {tid}")


def cmd_git(a):
    m = need_mission()
    if a.state == "off":
        git_save(m)({"disabled": True})
    elif a.state == "on":
        git_save(m)({"disabled": False})
        ensure_git(m)
    print(json.dumps(m.meta().get("git") or {"enabled": False}, indent=2))


def cmd_decide(a):
    m = need_mission()
    with open(m.dir / "decisions.md", "a") as f:
        f.write(f"- {datetime.now().strftime('%Y-%m-%d')}: {' '.join(a.text)}\n")
    m.event("decision", "supervisor", short(" ".join(a.text), 160))


def add_note(m: Mission, text: str, by: str = "user") -> None:
    with open(m.dir / "notes.jsonl", "a") as f:
        f.write(json.dumps({"ts": now(), "by": by, "text": text, "read": False}, ensure_ascii=False) + "\n")
    m.event("note", by, f"note to supervisor: {short(text, 140)}")


def cmd_note(a):
    add_note(need_mission(), " ".join(a.text), a.by)


def inbox_items(m: Mission, mark_read: bool) -> list:
    items = []
    for q in m.questions():
        if q["to"] == "supervisor" and q["status"] == "open":
            opts = f" options: {' | '.join(q['options'])}" if q.get("options") else ""
            rec = f" (executor suggests: {q['recommendation']})" if q.get("recommendation") else ""
            items.append(f"QUESTION {q['id']} from {q['from']}: {q['question']}{opts}{rec}")
        if q["to"] == "user" and q["status"] == "answered" and not q.get("seen"):
            items.append(f"USER ANSWERED {q['id']}: {q['question']} → {q['answer']}")
            if mark_read:
                q["seen"] = True
                m.save_question(q)
    notes = m.notes()
    unread = [n for n in notes if not n.get("read")]
    for n in unread:
        items.append(f"USER NOTE: {n['text']}")
    if mark_read and unread:
        for n in notes:
            n["read"] = True
        (m.dir / "notes.jsonl").write_text("".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes))
    if watches_dir(m).exists():
        for w in watch_list(m):
            if w.get("status") == "fired" and not w.get("seen"):
                items.append(f"WATCH {w['id']} '{w['name']}' {w.get('reason', 'fired').upper()}" + (f" (task {w['task']})" if w.get("task") else "")
                             + (f". You planned to: {w['note']}" if w.get("note") else ""))
                if mark_read:
                    w["seen"] = True
                    watch_save(m, w)
    for t in m.tasks():
        if t["status"] == "review":
            last = (t.get("attempts") or [{}])[-1]
            flag = " CHECK FAILED" if (last.get("check") or {}).get("ok") is False else ""
            flag += " RUNNER ERROR" if last.get("error") else ""
            flag += " PERMISSION DENIED" if last.get("denied") else ""
            if last.get("suggest"):
                flag += " (suggested rules: " + " ".join(last["suggest"][:4]) + ")"
            items.append(f"REVIEW {t['id']} ({t['title']}):{flag} {short(t.get('summary', ''), 200)} [confidence {t.get('confidence', '?')}] → .mission/reports/{t['id']}.md")
    return items


def cmd_inbox(a):
    m = need_mission()
    items = inbox_items(m, mark_read=not a.peek)
    print("\n".join(items) if items else "inbox empty")


def cmd_wait(a):
    """Block (spending zero model tokens) until something needs the supervisor."""
    m = need_mission()
    if os.environ.get("MC_ROLE") == "supervisor":
        print("You're the dashboard supervisor: end your turn. You'll be woken when something happens.")
        return
    cur_file = m.dir / ".supervisor_cursor"
    try:
        cursor = int(cur_file.read_text())
    except (FileNotFoundError, ValueError):
        cursor = 0
    interesting = {"report", "question", "answer", "note", "error", "approved", "watch"}
    deadline = time.time() + a.timeout
    while True:
        watches_tick(m)
        evs = m.events(cursor)
        hits = [e for e in evs if e["kind"] in interesting and not (e["kind"] == "question" and e.get("to") == "user")
                and not (e["kind"] in ("answer", "approved") and e.get("actor") == "supervisor")]
        tasks = m.tasks()
        running = [t for t in tasks if t["status"] == "running"]
        if hits and (not a.batch or not running or len(hits) >= a.batch):
            cursor += len(evs)
            cur_file.write_text(str(cursor))
            print(f"{len(hits)} update(s):")
            for e in hits:
                print(f"- [{e['kind']}] {e['msg']}")
            print("\nInbox:")
            print("\n".join(inbox_items(m, mark_read=True)) or "empty")
            return
        waiting_user = any(q["to"] == "user" and q["status"] == "open" for q in m.questions())
        waiting_watch = any(w.get("status") == "active" for w in watch_list(m)) if watches_dir(m).exists() else False
        if not running and not hits and not waiting_user and not waiting_watch:
            reap_dead(m)
            cursor += len(evs)
            cur_file.write_text(str(cursor))
            ready = [t["id"] for t in ready_tasks(m, tasks)]
            print("idle: nothing is running." + (f" Ready to run: {', '.join(ready)}" if ready else ""))
            items = inbox_items(m, mark_read=True)
            if items:
                print("\n".join(items))
            return
        if time.time() > deadline:
            what = ", ".join(t["id"] + " " + str(t.get("progress", 0)) + "%" for t in running)
            print(f"still working: {what or 'waiting for the user to answer or a watch to fire'}. Call wait again.")
            return
        time.sleep(2)


def cmd_autopilot(a):
    m = need_mission()
    meta = m.meta()
    meta["autopilot"] = a.state == "on"
    m.save_meta(meta)
    m.event("autopilot", "supervisor", f"autopilot {a.state}")
    if meta["autopilot"]:
        print("launched: " + (", ".join(schedule(m)) or "nothing ready"))


def cmd_watch(a):
    m = need_mission()
    if a.action == "list" or a.action is None:
        ws = watch_list(m)
        for w in ws:
            conds = " AND ".join(cond_text(c) for c in w["until"])
            print(f"{w['id']} [{w['status']}{'/' + w['reason'] if w.get('reason') else ''}] {w['name']}: waiting until {conds}"
                  + (f" · fails if {', '.join(cond_text(c) for c in w['fail'])}" if w["fail"] else "")
                  + (f" · fails if process gone: {', '.join(w['fail_if_gone'])}" if w.get("fail_if_gone") else "")
                  + f" · checked every {w['every']}s · expires {w['expires']}")
        if not ws:
            print("no watches")
    elif a.action == "cancel":
        w = next((x for x in watch_list(m) if x["id"] == a.name), None)
        if not w:
            die(f"no watch {a.name}")
        w["status"] = "cancelled"
        watch_save(m, w)
        m.event("watch_cancelled", os.environ.get("MC_ROLE") or "user", f"stopped waiting for: {w['name']}", watch=w["id"])
        print(f"{w['id']} cancelled")
    else:
        try:
            w = watch_add(m, {"name": a.name, "note": " ".join(a.note or []), "task": a.task, "file": a.file or [], "contains": a.contains or [],
                              "gone": a.gone or [], "fail_contains": a.fail_contains or [], "fail_file": a.fail_file or [],
                              "fail_if_gone": a.fail_if_gone or [], "every": a.every, "hours": a.hours,
                              "by": os.environ.get("MC_ROLE") or "user"})
        except ValueError as e:
            die(str(e))
        procs = process_lines()
        for g in w["fail_if_gone"] + [c["gone"] for c in w["until"] if "gone" in c]:
            if procs is not None and not any(g in ln for ln in procs):
                print(f"warning: no running process matches '{g}' right now. If that's a typo the watch fires immediately.", file=sys.stderr)
        print(f"{w['id']} watching: " + " AND ".join(cond_text(c) for c in w["until"]) + f" (checked every {w['every']}s, no tokens used). "
              "End your turn now; you'll be woken when it happens.")


def cmd_projects(a):
    if a.remove:
        target = str(Path(a.remove).expanduser().resolve())
        before = registry()
        keep = [p for p in before if p.get("path") != target and p.get("name") != a.remove]
        write_json(MC_HOME / "projects.json", keep)
        print("removed from the dashboard (files untouched)" if len(keep) < len(before) else "not in the list")
    for p in registry():
        ok = (Path(p["path"]) / ".mission" / "mission.json").exists()
        print(f"{p['name']:<24} {p['path']}{'' if ok else '  (missing)'}")


def cmd_goal(a):
    m = need_mission()
    meta = m.meta()
    meta["goal"] = " ".join(a.text)
    m.save_meta(meta)
    m.event("decision", "supervisor", f"goal: {short(meta['goal'], 150)}")


def cmd_approve(a):
    m = need_mission()
    print("launched: " + (", ".join(approve_plan(m, "supervisor")) or "nothing ready"))


def cmd_chat(a):
    m = need_mission()
    chat_add(m, "user", " ".join(a.text))
    print("queued" if not wake_supervisor(m) else "supervisor woken")


def cmd_supervisor(a):
    """Switch who supervises: the dashboard (headless) or an external chat."""
    m = need_mission()
    kw = {"mode": a.mode}
    if a.runner:
        kw["runner"] = a.runner
    sup_update(m, **kw)
    print(f"supervisor mode: {a.mode}")


def set_limits(m: Mission, max_attempts=None, budget=None, max_turns=None, sup_turns=None) -> None:
    """Safety limits. budget/max_turns/sup_turns: 0 or "" clears the limit."""
    with m.lock():
        meta = m.meta()
        if max_attempts is not None:
            meta["max_attempts"] = max(1, min(10, int(max_attempts)))
        if budget is not None:
            meta["executor_budget_usd"] = float(budget) if float(budget or 0) > 0 else None
        if max_turns is not None:
            meta["executor_max_turns"] = int(max_turns) if int(max_turns or 0) > 0 else None
        if sup_turns is not None:
            meta.setdefault("supervisor", {})["max_auto_turns"] = max(1, min(500, int(sup_turns))) if int(sup_turns or 0) > 0 else 40
        m.save_meta(meta)
    m.event("limits", "user", f"limits: attempts {m.meta().get('max_attempts') or DEFAULT_MAX_ATTEMPTS}, "
            f"budget/run {m.meta().get('executor_budget_usd') or 'none'}, model calls/run {m.meta().get('executor_max_turns') or 'none'}")


def cmd_limits(a):
    m = need_mission()
    if os.environ.get("MC_ROLE") == "supervisor" and any(x is not None for x in (a.max_attempts, a.executor_budget, a.executor_calls, a.supervisor_turns)):
        die("limits are the user's to change. Ask them in chat.")
    if any(x is not None for x in (a.max_attempts, a.executor_budget, a.executor_calls, a.supervisor_turns)):
        set_limits(m, a.max_attempts, a.executor_budget, a.executor_calls, a.supervisor_turns)
    meta = m.meta()
    print(f"max attempts per task:    {meta.get('max_attempts') or DEFAULT_MAX_ATTEMPTS}")
    print(f"budget per executor run:  {('$' + format(meta['executor_budget_usd'], 'g')) if meta.get('executor_budget_usd') else 'no cap'}")
    print(f"model calls per run:      {meta.get('executor_max_turns') or 'no cap'}")
    print(f"supervisor turns w/o you: {(meta.get('supervisor') or {}).get('max_auto_turns', 40)}")


def cmd_notify(a):
    """Where and when Mission Control pings you. Global, not per project (the supervisor may not change it)."""
    cfg = notify.load()
    act = a.action or "status"
    if act != "status" and os.environ.get("MC_ROLE") == "supervisor":
        die("notification settings are the user's to change. Ask them in chat.")
    if act in ("on", "off"):
        cfg = notify.update({"enabled": act == "on"})
    elif act == "test":
        if not cfg["enabled"]:
            die("notifications are off. Turn them on with: mc notify on")
        res = notify.send(cfg, "Mission Control test", "If you can read this, notifications work.", "test", wait=True)
        if not res:
            die("no channel is switched on. Try: mc notify set desktop")
        for ch, r in res.items():
            print(f"{ch:8} {'sent' if r == 'ok' else 'FAILED: ' + r}")
        return
    elif act == "set":
        if a.channel not in notify.CHANNELS or not a.channel:
            die("channel must be one of: " + ", ".join(notify.CHANNELS))
        patch = {"on": not a.off}
        if a.value:
            patch["topic" if a.channel == "ntfy" else "url"] = a.value
        if a.server:
            patch["server"] = a.server
        try:
            cfg = notify.update({"channels": {a.channel: patch}})
        except ValueError as e:
            die(str(e))
    elif act in ("mute", "unmute"):
        if a.channel not in notify.KINDS:
            die("kind must be one of: " + ", ".join(notify.KINDS))
        cfg = notify.update({"kinds": {a.channel: act == "unmute"}})
    shown = notify.masked(cfg)
    print(f"notifications: {'on' if shown['enabled'] else 'OFF'}")
    for name, ch in shown["channels"].items():
        state = "on " if ch["on"] else "off"
        extra = f"  ({ch['hint']})" if ch.get("hint") else ("  (not configured)" if name != "desktop" and not ch.get("set") else "")
        if name == "desktop" and not shown["available"]["desktop"]:
            extra = "  (not supported on this system)"
        print(f"  {name:8} {state}{extra}")
    print("  tell me about:")
    for k, label in notify.KINDS.items():
        print(f"    {'✓' if shown['kinds'][k] else '·'} {k:9} {label}")


def cmd_models(a):
    """Show or set the supervisor (expensive) and executor (cheap) provider:model."""
    m = need_mission()
    def split(v):
        if v is None:
            return None, None
        r, _, mdl = v.partition(":")
        return r, mdl
    sr, sm = split(a.supervisor)
    er, em = split(a.executor)
    if a.supervisor_effort is not None or a.executor_effort is not None:
        for lvl in (a.supervisor_effort, a.executor_effort):
            if lvl not in (None, "", "low", "medium", "high", "xhigh", "max"):
                die("effort must be one of: low, medium, high, xhigh, max (or empty for the default)")
        set_models(m, None, None, None, None, a.supervisor_effort, a.executor_effort)
    runners = load_runners()
    for r in (sr, er):
        if r and r not in runners:
            die(f"unknown runner '{r}'. Known: {', '.join(runners)}")
    if any(x is not None for x in (sr, er)):
        set_models(m, sr, sm, er, em)
    meta = m.meta()
    sup = meta.get("supervisor") or {}
    print(f"supervisor: {model_label(sup.get('runner', 'claude-supervisor'), resolve_runner(sup.get('runner', 'claude-supervisor'), sup.get('model'))[2])}"
          f"  ({sup.get('mode', 'external')})")
    print(f"executors:  {model_label(meta.get('default_executor', 'claude'), resolve_runner(meta.get('default_executor', 'claude'), meta.get('executor_model'))[2])}")
    print(f"effort:     supervisor {sup.get('effort') or 'default'} · executors {meta.get('executor_effort') or 'default'}")
    print("available:  " + ", ".join(f"{k} [{r.get('role', 'executor')}] {'/'.join(r.get('models', []))}" for k, r in runners.items()))


def cmd_dashboard(a):
    os.execv(sys.executable, [sys.executable, str(HERE / "dashboard.py"), "--port", str(a.port)] + (["--open"] if a.open else []))


def main(argv=None):
    p = argparse.ArgumentParser(prog="mc.py", description="Mission Control: supervisor/executor protocol")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create .mission/ here and register the project")
    s.add_argument("--name"); s.add_argument("--goal"); s.add_argument("--path")
    s.add_argument("--parallel", type=int); s.add_argument("--executor", help="executor provider, e.g. claude, codex, gemini")
    s.add_argument("--executor-model", help="e.g. haiku, sonnet, a Gemini/OpenAI model id")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("plan", help="create many tasks from JSON ({tasks:[...]}); '-' reads stdin")
    s.add_argument("file"); s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("add", help="add one task")
    s.add_argument("--title", required=True); s.add_argument("--goal", default="")
    s.add_argument("--accept", nargs="*", default=[]); s.add_argument("--files", nargs="*", default=[])
    s.add_argument("--deps", nargs="*", default=[]); s.add_argument("--executor")
    s.add_argument("--estimate", type=int, default=2); s.add_argument("--check", default="")
    s.add_argument("--model", default="", help="override the executor model for this task")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("set", help="edit task fields: set T-001 executor=claude-sonnet estimate=3")
    s.add_argument("task"); s.add_argument("pairs", nargs="+"); s.set_defaults(fn=cmd_set)

    s = sub.add_parser("status", help="compact mission summary"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("checkpoint", help="write STATE.md for resuming in a fresh chat").set_defaults(fn=cmd_checkpoint)

    s = sub.add_parser("brief", help="print the executor prompt for a task (paste into any AI)")
    s.add_argument("task"); s.set_defaults(fn=cmd_brief)

    s = sub.add_parser("run", help="run tasks (each in a fresh executor session); no args = all ready")
    s.add_argument("tasks", nargs="*"); s.add_argument("--ready", action="store_true"); s.set_defaults(fn=cmd_run)
    for name in ("stop", "cancel"):
        s = sub.add_parser(name, help=f"{name} a task"); s.add_argument("task"); s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("progress", help="executor: report progress")
    s.add_argument("task"); s.add_argument("pct", type=int); s.add_argument("step", nargs="*")
    s.set_defaults(fn=cmd_progress)

    s = sub.add_parser("report", help="executor (manual mode): submit report")
    s.add_argument("task"); s.add_argument("--status", choices=["done", "blocked", "failed"], default="done")
    s.add_argument("--summary", required=True); s.add_argument("--files", nargs="*", default=[])
    s.add_argument("--confidence", default="medium"); s.add_argument("--question", default="")
    s.add_argument("--options", nargs="*", default=[]); s.add_argument("--tokens-in", type=int, default=0)
    s.add_argument("--tokens-out", type=int, default=0); s.set_defaults(fn=cmd_report)

    s = sub.add_parser("ask", help="ask a question (supervisor → user, or executor → supervisor)")
    s.add_argument("question"); s.add_argument("--to", choices=["user", "supervisor"], default="user")
    s.add_argument("--from", dest="sender", default="supervisor"); s.add_argument("--task")
    s.add_argument("--options", nargs="*", default=[]); s.add_argument("--recommend")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("request-permission", help="ask the user to allow tool rules for the project (supervisor)")
    s.add_argument("task"); s.add_argument("rules", nargs="+"); s.add_argument("--why", nargs="*"); s.set_defaults(fn=cmd_request_permission)
    s = sub.add_parser("request-model", help="ask the user to approve a more expensive model for tasks (supervisor)")
    s.add_argument("model"); s.add_argument("tasks", nargs="+"); s.add_argument("--why", nargs="*"); s.set_defaults(fn=cmd_request_model)
    s = sub.add_parser("allow", help="user: add/remove project permission rules"); s.add_argument("rules", nargs="*")
    s.add_argument("--remove"); s.set_defaults(fn=cmd_allow)
    s = sub.add_parser("answer", help="answer a question"); s.add_argument("qid"); s.add_argument("text", nargs="+")
    s.add_argument("--by", default="supervisor"); s.set_defaults(fn=cmd_answer)

    s = sub.add_parser("review", help="supervisor verdict on a task")
    s.add_argument("task"); s.add_argument("verdict", choices=["approve", "changes"]); s.add_argument("message", nargs="*")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("decide", help="log a decision (kept in STATE.md)"); s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_decide)
    s = sub.add_parser("note", help="leave a note for the supervisor"); s.add_argument("text", nargs="+")
    s.add_argument("--by", default="user"); s.set_defaults(fn=cmd_note)
    s = sub.add_parser("inbox", help="what needs the supervisor now"); s.add_argument("--peek", action="store_true")
    s.set_defaults(fn=cmd_inbox)
    s = sub.add_parser("wait", help="block until something needs the supervisor (no tokens while waiting)")
    s.add_argument("--timeout", type=int, default=540); s.add_argument("--batch", type=int, default=0)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("autopilot", help="auto-launch ready tasks"); s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_autopilot)
    s = sub.add_parser("projects", help="list registered projects"); s.add_argument("--remove", help="remove a project (path or name) from the dashboard; files are untouched")
    s.set_defaults(fn=cmd_projects)
    s = sub.add_parser("dashboard", help="start the dashboard"); s.add_argument("--port", type=int, default=7777)
    s.add_argument("--open", action="store_true"); s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("goal", help="set the agreed mission goal"); s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_goal)
    sub.add_parser("approve", help="approve the plan and launch (dashboard missions)").set_defaults(fn=cmd_approve)
    s = sub.add_parser("chat", help="send a chat message to the dashboard supervisor"); s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("supervisor", help="who supervises: dashboard (headless) or external chat")
    s.add_argument("mode", choices=["dashboard", "external"]); s.add_argument("--runner"); s.set_defaults(fn=cmd_supervisor)
    s = sub.add_parser("_supervise"); s.add_argument("root"); s.add_argument("--kickoff", action="store_true")
    s.set_defaults(fn=cmd_supervise)
    s = sub.add_parser("watch", help="wait for something outside Mission Control (a file, a log line, a process ending) without polling")
    s.add_argument("action", nargs="?", choices=["add", "list", "cancel"]); s.add_argument("name", nargs="?", help="a short name (add) or the watch id (cancel)")
    s.add_argument("--file", action="append", help="fire when this path exists (repeatable; all must exist)")
    s.add_argument("--contains", action="append", nargs=2, metavar=("PATH", "TEXT"), help="fire when the file contains the text")
    s.add_argument("--gone", action="append", metavar="PATTERN", help="fire when no running process's command line contains PATTERN")
    s.add_argument("--fail-contains", action="append", nargs=2, metavar=("PATH", "TEXT"), help="fire as FAILED when the file contains the text (e.g. run.log Traceback)")
    s.add_argument("--fail-file", action="append", help="fire as FAILED when this path exists")
    s.add_argument("--fail-if-gone", action="append", metavar="PATTERN", help="fire as FAILED if the process disappears before the main condition is met")
    s.add_argument("--every", type=int, default=60, help="seconds between checks (min 15)"); s.add_argument("--hours", type=float, default=24, help="give up after this long")
    s.add_argument("--task"); s.add_argument("--note", nargs="*", help="what you plan to do when it fires"); s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("limits", help="safety limits: attempts per task, budget and model calls per executor run")
    s.add_argument("--max-attempts", type=int); s.add_argument("--executor-budget", type=float, help="USD per executor run (0 = no cap)")
    s.add_argument("--executor-calls", type=int, help="max model calls per executor run (0 = no cap)")
    s.add_argument("--supervisor-turns", type=int, help="supervisor turns in a row without a message from you"); s.set_defaults(fn=cmd_limits)
    s = sub.add_parser("notify", help="get pinged when a decision is waiting, something fails or a mission finishes")
    s.add_argument("action", nargs="?", choices=["status", "on", "off", "test", "set", "mute", "unmute"])
    s.add_argument("channel", nargs="?", help="set: desktop | slack | webhook | ntfy; mute/unmute: decision | failure | complete | watch")
    s.add_argument("value", nargs="?", help="set: the webhook URL (slack, webhook) or topic (ntfy)")
    s.add_argument("--off", action="store_true", help="with set: switch that channel off"); s.add_argument("--server", help="ntfy server (default https://ntfy.sh)")
    s.set_defaults(fn=cmd_notify)
    s = sub.add_parser("models", help="show/set models: --supervisor claude-supervisor:opus --executor gemini:gemini-2.5-flash")
    s.add_argument("--supervisor"); s.add_argument("--executor")
    s.add_argument("--supervisor-effort"); s.add_argument("--executor-effort"); s.set_defaults(fn=cmd_models)
    s = sub.add_parser("assign", help="assign a task: --to sara (a person, GitHub login) or --to claude:sonnet")
    s.add_argument("task"); s.add_argument("--to", required=True); s.set_defaults(fn=cmd_assign)
    s = sub.add_parser("done", help="mark a human task done (no GitHub)"); s.add_argument("task"); s.add_argument("note", nargs="*")
    s.set_defaults(fn=cmd_done)
    s = sub.add_parser("diff", help="show a task's changes vs the mission branch"); s.add_argument("task")
    s.add_argument("--full", action="store_true"); s.set_defaults(fn=cmd_diff)
    sub.add_parser("sync", help="pull comments and PRs for human tasks from GitHub").set_defaults(fn=cmd_sync)
    s = sub.add_parser("pr", help="open the pull request (pushes only once it can exist)")
    s.add_argument("--target", help="open it against this branch with only the mission's own changes (use when the mission's base branch isn't on the remote)")
    s.add_argument("--dry-run", action="store_true", help="show exactly what would be published, without pushing or opening anything")
    s.add_argument("--clean", action="store_true", help="publish the one-commit clean branch made by `mc clean-branch` (implies --as-is)")
    s.add_argument("--as-is", action="store_true", help="publish the mission branch unchanged and let the repository owner judge conflicts (no merging, no rewriting)")
    s.set_defaults(fn=cmd_pr)
    s = sub.add_parser("clean-branch", help="make a local one-commit branch of the mission's final changes (no tool files, no history); publishes nothing")
    s.add_argument("--target", help="branch it would be proposed against (default: the remote's default branch)")
    s.add_argument("--title", help="pull request title (also the commit message's first line)")
    s.add_argument("--body-file", help="a file with the pull request description (also the commit message body)"); s.set_defaults(fn=cmd_clean_branch)
    s = sub.add_parser("sync-base", help="bring the mission up to date with the latest main (merge + resolve conflicts as a task)")
    s.add_argument("--target", help="branch to sync with (default: the remote's default branch)")
    s.add_argument("--check", help="command that must pass afterwards, e.g. \"python3 -m unittest discover -s tests\"")
    s.add_argument("--model", help="model for the task (resolving conflicts is delicate)"); s.set_defaults(fn=cmd_sync_base)
    s = sub.add_parser("git", help="git mode: status / on / off"); s.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    s.set_defaults(fn=cmd_git)
    s = sub.add_parser("_exec"); s.add_argument("root"); s.add_argument("task"); s.add_argument("attempt")
    s.set_defaults(fn=cmd_exec)
    s = sub.add_parser("_demo"); s.add_argument("task"); s.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
