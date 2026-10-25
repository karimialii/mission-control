"""Git mode and GitHub handoff for Mission Control.

Git mode (automatic when the project is a git repo):
  - the mission works on its own branch `mission/<name>`, checked out in .mission/worktrees/_mission,
    so the user's own checkout and branches are never touched;
  - every AI task runs in its own worktree on branch `mission/<name>-t-00x`, created from the mission
    branch's latest tip; approving a task merges it into the mission branch;
  - the finished mission is pushed and opened as one pull request.

Human tasks: handed to a person as a GitHub issue (with the full brief) and a pushed task branch.
Their pull request into the mission branch is what gets reviewed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import compat

MARK = "<!-- mission-control -->"


class GitError(RuntimeError):
    pass


class BaseMissing(GitError):
    """The branch the mission was built on isn't on the remote, so a pull request against it can't exist."""
    def __init__(self, message: str, suggest: str = "main"):
        super().__init__(message)
        self.suggest = suggest


# Runtime folders that agent tools drop into the working directory. They must never end up in a commit.
TOOL_DIRS = [".claude-flow", ".swarm", ".hive-mind", ".ruflo", ".claude/memory", ".mission"]


def git(cwd, *args, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and cp.returncode != 0:
        raise GitError((cp.stderr or cp.stdout).strip() or f"git {' '.join(args)} failed")
    return cp.stdout.strip()


def ok(cwd, *args) -> bool:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True).returncode == 0


def is_repo(root) -> bool:
    return shutil.which("git") is not None and ok(root, "rev-parse", "--is-inside-work-tree") and \
        Path(git(root, "rev-parse", "--show-toplevel")).resolve() == Path(root).resolve()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "mission"


def cfg(m) -> dict:
    return m.meta().get("git") or {}


def enabled(m) -> bool:
    return bool(cfg(m).get("enabled"))


def mission_wt(m) -> Path:
    return m.dir / "worktrees" / "_mission"


def task_wt(m, tid: str) -> Path:
    return m.dir / "worktrees" / tid


def task_branch(m, tid: str) -> str:
    return f"{cfg(m)['branch']}-{tid.lower()}"


def github_slug(url: str) -> str | None:
    mm = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return f"{mm.group(1)}/{mm.group(2)}" if mm else None


def _identity_args(cwd) -> list:
    """Commit even when the user has no git identity configured."""
    if ok(cwd, "config", "user.email"):
        return []
    return ["-c", "user.name=Mission Control", "-c", "user.email=mission-control@localhost"]


def setup(m, save) -> dict | None:
    """Create the mission branch + worktree once. `save(dict)` stores the git config in mission.json."""
    c = cfg(m)
    if c.get("disabled"):
        return None
    if c.get("enabled"):
        if not mission_wt(m).exists():  # worktree folder deleted: recreate it from the branch
            git(m.root, "worktree", "prune")
            git(m.root, "worktree", "add", str(mission_wt(m)), c["branch"])
        return c
    if not is_repo(m.root):
        return None
    if not ok(m.root, "rev-parse", "--verify", "HEAD"):
        raise GitError("the repository has no commits yet; make a first commit so a mission branch can be created")
    # keep .mission/ out of git for this clone only (doesn't touch the shared .gitignore)
    excl = Path(m.root) / git(m.root, "rev-parse", "--git-path", "info/exclude")
    excl.parent.mkdir(parents=True, exist_ok=True)
    if ".mission/" not in (excl.read_text() if excl.exists() else ""):
        with open(excl, "a") as f:
            f.write("\n# Mission Control (local state)\n.mission/\n")
    base = git(m.root, "rev-parse", "--abbrev-ref", "HEAD")
    if base == "HEAD":
        base = "main"
    remote = "origin" if "origin" in git(m.root, "remote").split() else ""
    start = base
    if remote:
        ok(m.root, "fetch", remote, base)
        if ok(m.root, "rev-parse", "--verify", f"{remote}/{base}"):
            start = f"{remote}/{base}"
    name = slug(m.meta().get("name") or Path(m.root).name)
    branch, n = f"mission/{name}", 2
    while ok(m.root, "rev-parse", "--verify", branch):
        branch, n = f"mission/{name}-{n}", n + 1
    mission_wt(m).parent.mkdir(parents=True, exist_ok=True)
    git(m.root, "worktree", "add", "--no-track", "-b", branch, str(mission_wt(m)), start)
    url = git(m.root, "remote", "get-url", remote, check=False) if remote else ""
    c = {"enabled": True, "base": base, "branch": branch, "start": start, "remote": remote,
         "github": github_slug(url), "pushed": False}
    save(c)
    return c


def prepare_task(m, tid: str, merge_from: str | None = None) -> tuple:
    """Worktree for an AI task, up to date with the mission branch. Returns (path, conflicted_files).
    merge_from: a ref (e.g. origin/main) to merge into the new worktree, leaving any conflicts for the executor."""
    c = cfg(m)
    wt, br = task_wt(m, tid), task_branch(m, tid)
    if not wt.exists():
        git(m.root, "worktree", "prune")
        if ok(m.root, "rev-parse", "--verify", br):
            git(m.root, "worktree", "add", str(wt), br)
        else:
            git(m.root, "worktree", "add", "-b", br, str(wt), c["branch"])
        if merge_from and ok(wt, "rev-parse", "--verify", merge_from):
            cp = subprocess.run(["git", *_identity_args(wt), "merge", "--no-commit", "--no-ff", merge_from], cwd=str(wt),
                                capture_output=True, text=True)
            if cp.returncode != 0:
                return wt, git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
        return wt, []
    if _merging(wt):  # an unfinished merge from last time: don't start another, just point at what's left
        return wt, git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
    # retry: bring in what other tasks merged meanwhile; leave conflict markers for the executor
    cp = subprocess.run(["git", *_identity_args(wt), "merge", "--no-edit", c["branch"]], cwd=str(wt),
                        capture_output=True, text=True)
    if cp.returncode != 0:
        files = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
        return wt, files
    return wt, []


def _tracked_in(cwd, ref: str, path: str) -> bool:
    return bool(git(cwd, "ls-tree", "-r", "--name-only", ref, "--", path, check=False))


def unstage_tool_files(wt) -> None:
    """Keep agent tool folders out of the commit unless the repository already tracks them."""
    for d in TOOL_DIRS:
        if not _tracked_in(wt, "HEAD", d):
            git(wt, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", d, check=False)


def commit_task(m, tid: str, title: str) -> str | None:
    wt = task_wt(m, tid)
    if not wt.exists():
        return None
    if _merging(wt):
        unresolved = [f for f in git(wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
                      if (wt / f).is_file() and "<<<<<<<" in (wt / f).read_text(errors="ignore")]
        if unresolved:
            raise GitError("unresolved merge conflict markers in " + ", ".join(unresolved))
    git(wt, "add", "-A")
    unstage_tool_files(wt)
    if ok(wt, "diff", "--cached", "--quiet") and not _merging(wt):
        return None
    git(wt, *_identity_args(wt), "commit", "-q", "-m", f"{tid}: {title}", "-m", "Executed by Mission Control.")
    return git(wt, "rev-parse", "--short", "HEAD")


def _merging(wt) -> bool:
    return ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")


def diff(m, tid: str, full: bool = False, human: bool = False) -> str:
    c = cfg(m)
    br = task_branch(m, tid)
    ref = br
    if human and c.get("remote"):
        ok(m.root, "fetch", c["remote"], br)
        ref = f"{c['remote']}/{br}"
    if not ok(m.root, "rev-parse", "--verify", ref):
        return "(no commits on this task's branch yet)"
    rng = f"{c['branch']}...{ref}"
    out = git(m.root, "diff", "--stat", rng, check=False)
    if full:
        body = git(m.root, "diff", rng, check=False)
        out += "\n\n" + (body if len(body) < 60000 else body[:60000] + "\n… (diff truncated)")
    return out or "(no changes)"


def integrate(m, tid: str, title: str, human: bool = False) -> tuple:
    """Merge a task branch into the mission branch. Returns (ok, message).
    Tasks often finish at the same moment, and two merges into one worktree collide, so they take turns."""
    with open(m.dir / ".merge.lock", "a") as lf:
        compat.lock(lf)
        try:
            res = _integrate(m, tid, title, human)
            if res[0] and follows(m):
                update_folder(m)  # approved work shows up in the user's own folder
            return res
        finally:
            compat.unlock(lf)


# ───────── the user's own folder follows the mission ─────────
# Agents work on the mission branch in a hidden worktree, so the user's checkout is never changed
# while they work. Approved work is then brought into the user's folder: automatically when that is a
# plain fast-forward (nothing of theirs can be overwritten), or on request with a normal merge.

def follows(m) -> bool:
    return enabled(m) and cfg(m).get("follow", True) is not False


def folder_status(m) -> dict:
    """How the user's checkout compares with the mission branch."""
    c = cfg(m)
    if not enabled(m):
        return {"enabled": False}
    root, mission = m.root, c["branch"]
    cur = git(root, "branch", "--show-current", check=False)
    out = {"enabled": True, "follow": follows(m), "branch": cur, "base": c.get("base"), "mission": mission}
    if not ok(root, "rev-parse", "--verify", mission):
        return {**out, "behind": 0, "state": "up to date"}
    behind = int(git(root, "rev-list", "--count", f"{cur or 'HEAD'}..{mission}", check=False) or 0)
    files = [l for l in git(root, "diff", "--name-only", f"{cur or 'HEAD'}...{mission}", check=False).splitlines() if l][:200]
    dirty = [l for l in git(root, "diff", "--name-only", "HEAD", check=False).splitlines() if l.strip()]  # staged or not
    out.update(behind=behind, files=files)
    if not behind:
        return {**out, "state": "up to date"}
    if cur != c.get("base"):
        return {**out, "state": "waiting", "reason": f"your folder is on branch '{cur or '(detached)'}', and the mission builds on '{c.get('base')}'"}
    if dirty:
        return {**out, "state": "waiting", "reason": "you have uncommitted changes in " + ", ".join(dirty[:4]) + (" …" if len(dirty) > 4 else ""), "dirty": dirty}
    if ok(root, "merge-base", "--is-ancestor", cur, mission):
        return {**out, "state": "fast-forward"}
    return {**out, "state": "waiting", "reason": f"'{cur}' has commits of its own, so bringing the work in needs a merge commit", "needs_merge": True}


def update_folder(m, merge: bool = False) -> tuple:
    """Bring approved mission work into the user's folder. Fast-forward only, unless merge=True
    (the user pressed the button). Never touches uncommitted changes. Returns (ok, message)."""
    st = folder_status(m)
    if not st.get("enabled"):
        return False, "git mode is off"
    if st["state"] == "up to date":
        return True, "your folder is up to date"
    if st["state"] == "waiting" and not (merge and st.get("needs_merge")):
        return False, st["reason"]
    args = ["merge", "--ff-only", st["mission"]] if st["state"] == "fast-forward" else \
           [*_identity_args(m.root), "merge", "--no-edit", "-m", f"Bring in the mission's approved work ({st['mission']})", st["mission"]]
    cp = subprocess.run(["git", *args], cwd=str(m.root), capture_output=True, text=True)
    if cp.returncode != 0:
        if _merging(m.root):
            ok(m.root, "merge", "--abort")  # leave the folder exactly as it was
        msg = (cp.stderr or cp.stdout).strip()
        if "would be overwritten" in msg:
            names = [l.strip() for l in msg.splitlines() if l.startswith(("\t", "    "))][:4]
            return False, "files in your folder that git doesn't track would be overwritten: " + ", ".join(names)
        return False, "git couldn't bring it in: " + (msg.splitlines() or ["unknown error"])[-1][:160]
    return True, f"brought {st['behind']} commit{'s' if st['behind'] != 1 else ''} into your folder ({st['branch']})"


def _integrate(m, tid: str, title: str, human: bool = False) -> tuple:
    c = cfg(m)
    mwt, br = mission_wt(m), task_branch(m, tid)
    ref = br
    if human:
        if c.get("remote"):
            ok(m.root, "fetch", c["remote"], br)
            ref = f"{c['remote']}/{br}"
        if not ok(m.root, "rev-parse", "--verify", ref):
            return True, "no commits to merge"
    elif not ok(m.root, "rev-parse", "--verify", br):
        return True, "no branch to merge"
    if human and c.get("remote"):  # someone may have merged their PR on GitHub already
        ok(mwt, "pull", "--no-edit", "--no-rebase", c["remote"], c["branch"])
    cp = subprocess.run(["git", *_identity_args(mwt), "merge", "--no-ff", "--no-edit", "-m", f"Merge {tid}: {title}", ref],
                        cwd=str(mwt), capture_output=True, text=True)
    if cp.returncode != 0:
        files = git(mwt, "diff", "--name-only", "--diff-filter=U", check=False).split()
        ok(mwt, "merge", "--abort")
        if not files:  # not a content conflict: say what git actually said
            return False, "git could not merge it: " + ((cp.stderr or cp.stdout).strip().splitlines() or ["unknown error"])[-1][:160]
        return False, "merge conflict in " + ", ".join(files)
    if not human:
        wt = task_wt(m, tid)
        if wt.exists():
            ok(m.root, "worktree", "remove", "--force", str(wt))
        ok(m.root, "branch", "-D", br)
    if c.get("pushed") and c.get("remote"):
        ok(mwt, "push", c["remote"], c["branch"])
    return True, git(mwt, "rev-parse", "--short", "HEAD")


def push(m, branch: str, cwd=None) -> None:
    c = cfg(m)
    if not c.get("remote"):
        raise GitError("this repository has no 'origin' remote")
    git(cwd or m.root, "push", "-u", c["remote"], branch)


def drop_task(m, tid: str) -> None:
    wt = task_wt(m, tid)
    if wt.exists():
        ok(m.root, "worktree", "remove", "--force", str(wt))


# ───────────────────────────── GitHub (gh CLI) ─────────────────────────────

def gh_ready(m) -> bool:
    return bool(cfg(m).get("github")) and shutil.which("gh") is not None


def gh(m, *args) -> str:
    cp = subprocess.run(["gh", *args], cwd=str(m.root), capture_output=True, text=True)
    if cp.returncode != 0:
        raise GitError((cp.stderr or cp.stdout).strip() or f"gh {' '.join(args)} failed")
    return cp.stdout.strip()


def gh_me(m) -> str:
    try:
        return gh(m, "api", "user", "-q", ".login")
    except GitError:
        return ""


def handoff(m, tid: str, title: str, assignee: str, body: str, mark_pushed) -> dict:
    """Create + push the task branch and open an issue assigned to the person."""
    c = cfg(m)
    br = task_branch(m, tid)
    if not ok(m.root, "rev-parse", "--verify", br):
        git(m.root, "branch", br, c["branch"])
    out = {"branch": br}
    if c.get("remote"):
        push(m, c["branch"], cwd=mission_wt(m))
        mark_pushed()
        push(m, br)
        out["pushed"] = True
    if gh_ready(m):
        repo = c["github"]
        args = ["issue", "create", "-R", repo, "--title", f"[{m.meta().get('name', 'Mission')}] {tid}: {title}",
                "--body", body + "\n\n" + MARK]
        try:
            url = gh(m, *args, "--assignee", assignee)
        except GitError:  # not a collaborator / unknown login: open it unassigned and mention them
            url = gh(m, *args[:-1], f"@{assignee}\n\n" + body + "\n\n" + MARK)
        out.update(issue=int(url.rstrip("/").split("/")[-1]), issue_url=url)
    return out


def issue_comment(m, number: int, text: str) -> None:
    gh(m, "issue", "comment", str(number), "-R", cfg(m)["github"], "--body", text + "\n\n" + MARK)


def close_issue(m, number: int, text: str = "") -> None:
    args = ["issue", "close", str(number), "-R", cfg(m)["github"]]
    if text:
        args += ["--comment", text + "\n\n" + MARK]
    gh(m, *args)


def pr_comment(m, number: int, text: str) -> None:
    gh(m, "pr", "comment", str(number), "-R", cfg(m)["github"], "--body", text + "\n\n" + MARK)


def sync_task(m, tid: str, issue: int | None, seen_comments: int) -> dict:
    """What changed on GitHub for a human task: new comments, its PR, issue state."""
    repo = cfg(m)["github"]
    out = {"comments": [], "seen": seen_comments, "pr": None, "issue_state": None}
    if issue:
        data = json.loads(gh(m, "issue", "view", str(issue), "-R", repo, "--json", "comments,state"))
        out["issue_state"] = data.get("state")
        comments = data.get("comments") or []
        out["comments"] = [{"author": (cm.get("author") or {}).get("login", "?"), "body": cm.get("body", ""),
                            "url": cm.get("url", "")}
                           for cm in comments[seen_comments:] if MARK not in (cm.get("body") or "")]
        out["seen"] = len(comments)
    prs = json.loads(gh(m, "pr", "list", "-R", repo, "--head", task_branch(m, tid), "--state", "all",
                        "--json", "number,url,state,author,title,headRefOid"))
    if prs:
        p = prs[0]
        out["pr"] = {"number": p["number"], "url": p["url"], "state": p["state"], "sha": p.get("headRefOid", ""),
                     "author": (p.get("author") or {}).get("login", "")}
    return out


def fork_point(m) -> str:
    """The commit the mission's own work starts from. Recorded as a branch NAME at setup, but a name like origin/main
    moves when the remote does, so always use where the mission branch actually forked from it."""
    c, mwt = cfg(m), mission_wt(m)
    for ref in (c.get("start"), c.get("base")):
        if ref and ok(mwt, "rev-parse", "--verify", ref):
            mb = git(mwt, "merge-base", c["branch"], ref, check=False)
            if mb:
                return mb
    return c.get("start") or c["base"]


def predict_conflicts(m, ref: str) -> list:
    """Files that would conflict if `ref` were merged into the mission branch now (nothing is changed)."""
    c = cfg(m)
    cp = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", c["branch"], ref], cwd=str(m.root),
                        capture_output=True, text=True)
    lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    return lines[1:] if cp.returncode != 0 and len(lines) > 1 else []


def remote_has_branch(m, name: str) -> bool:
    c = cfg(m)
    return subprocess.run(["git", "ls-remote", "--exit-code", "--heads", c["remote"], name], cwd=str(m.root),
                          capture_output=True, text=True).returncode == 0


def remote_default_branch(m) -> str:
    c = cfg(m)
    out = git(m.root, "ls-remote", "--symref", c["remote"], "HEAD", check=False)
    mm = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
    return mm.group(1) if mm else "main"


def strip_tool_files(m) -> list:
    """If tool folders slipped into the mission branch (and aren't in its base), remove them in a clean commit."""
    c, mwt = cfg(m), mission_wt(m)
    base_ref = fork_point(m)
    removed = []
    for d in TOOL_DIRS:
        if _tracked_in(mwt, "HEAD", d) and not _tracked_in(mwt, base_ref, d):
            git(mwt, "rm", "-r", "-q", "-f", "--ignore-unmatch", "--", d, check=False)
            removed.append(d)
    if removed and not ok(mwt, "diff", "--cached", "--quiet"):
        git(mwt, *_identity_args(mwt), "commit", "-q", "-m", "Remove agent tool files from the mission branch",
            "-m", "Runtime folders written by agent tools: " + ", ".join(removed))
    return removed


def _replay_onto(m, target: str, title: str) -> str:
    """Build a branch from origin/<target> holding ONLY the mission's own changes (the net diff between the mission's
    starting point and its tip, minus tool files), so a PR doesn't drag in commits that exist only on the user's machine."""
    c, mwt = cfg(m), mission_wt(m)
    remote = c["remote"]
    git(m.root, "fetch", "-q", remote, target)
    start = fork_point(m)
    excl = [f":(exclude){d}" for d in TOOL_DIRS]
    patch = subprocess.run(["git", "diff", "--binary", start, c["branch"], "--", ".", *excl], cwd=str(mwt),
                           capture_output=True).stdout
    if not patch.strip():
        raise GitError("the mission has no changes of its own to propose")
    pr_branch = f"{c['branch']}-pr"
    pwt = m.dir / "worktrees" / "_pr"
    if pwt.exists():
        ok(m.root, "worktree", "remove", "--force", str(pwt))
    git(m.root, "worktree", "prune")
    git(m.root, "worktree", "add", "-q", "-B", pr_branch, str(pwt), f"{remote}/{target}")
    try:
        ap = subprocess.run(["git", "apply", "--3way", "--index", "-"], cwd=str(pwt), input=patch, capture_output=True)
        if ap.returncode != 0:
            files = git(pwt, "diff", "--name-only", "--diff-filter=U", check=False).split()
            detail = (ap.stderr.decode(errors="ignore").strip().splitlines() or ["the changes don't apply"])[-1]
            raise GitError(f"The mission's changes don't apply cleanly on top of {target}"
                           + (f" (conflicts in {', '.join(files)})" if files else f" ({detail})")
                           + ". They probably depend on work that isn't in that branch yet.")
        git(pwt, *_identity_args(pwt), "commit", "-q", "-m", title, "-m", f"Changes from the {c['branch']} mission, applied on {target}.")
        # This branch is generated here, so re-running replaces it. The lease still refuses if someone else pushed to it.
        git(pwt, "push", "-q", "-u", "--force-with-lease", remote, pr_branch)
    except GitError:
        ok(m.root, "worktree", "remove", "--force", str(pwt))
        ok(m.root, "branch", "-D", pr_branch)
        raise
    ok(m.root, "worktree", "remove", "--force", str(pwt))
    return pr_branch


def make_clean_branch(m, title: str, body: str) -> dict:
    """A local branch with ONE commit: the mission's final files (agent tool folders removed) on top of the commit the
    mission was built from. Same content as the mission branch, none of its history. Nothing is published.
    Reused as long as the mission branch hasn't changed, so what you preview is exactly what gets pushed."""
    c, mwt = cfg(m), mission_wt(m)
    fork = fork_point(m)
    name = f"{c['branch']}-clean"
    tip = git(mwt, "rev-parse", c["branch"])
    if c.get("clean_for") == tip and ok(m.root, "rev-parse", "--verify", name):
        return {"branch": name, "sha": git(m.root, "rev-parse", name), "fork": fork, "reused": True, "tip": tip}
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
        def run(*args):
            return subprocess.run(["git", *args], cwd=str(mwt), env=env, capture_output=True, text=True)
        if run("read-tree", c["branch"]).returncode != 0:
            raise GitError("couldn't read the mission branch")
        for d in TOOL_DIRS:
            if not _tracked_in(mwt, fork, d):
                run("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", d)
        tree = run("write-tree").stdout.strip()
    sha = git(mwt, *_identity_args(mwt), "commit-tree", tree, "-p", fork, "-m", title, "-m", body)
    git(m.root, "branch", "-f", name, sha)
    return {"branch": name, "sha": sha, "fork": fork, "reused": False, "tip": tip}


def clean_preview(m, name: str, target: str) -> dict:
    c = cfg(m)
    remote = c["remote"]
    git(m.root, "fetch", "-q", remote, target)
    fork = git(m.root, "merge-base", name, f"{remote}/{target}", check=False)
    excl = [f":(exclude){d}" for d in TOOL_DIRS]
    tool_in_tree = [d for d in TOOL_DIRS if d != ".mission" and git(m.root, "ls-tree", "-r", "--name-only", name, "--", d, check=False)]
    tool_in_history = [d for d in TOOL_DIRS if d != ".mission" and
                       git(m.root, "log", "--oneline", f"{fork}..{name}", "--", d, check=False)] if fork else []
    return {
        "branch": name, "sha": git(m.root, "rev-parse", "--short", name), "target": target, "repo": c.get("github") or remote,
        "parent": git(m.root, "rev-parse", "--short", f"{name}^"), "parent_in_target": bool(fork) and git(m.root, "rev-parse", f"{name}^") == fork,
        "commits": git(m.root, "log", "--format=%h %s", f"{fork}..{name}", check=False).splitlines() if fork else [],
        "files": git(m.root, "diff", "--numstat", f"{name}^", name, "--", ".", *excl, check=False).splitlines(),
        "stat": git(m.root, "diff", "--shortstat", f"{name}^", name, check=False),
        "tool_in_tree": tool_in_tree, "tool_in_history": tool_in_history,
        "behind": len(git(m.root, "rev-list", f"{name}..{remote}/{target}", check=False).split()),
        "conflicts": predict_conflicts_for(m, name, f"{remote}/{target}"),
        "message": git(m.root, "log", "-1", "--format=%s", name),
    }


def predict_conflicts_for(m, a: str, b: str) -> list:
    cp = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", a, b], cwd=str(m.root), capture_output=True, text=True)
    lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    return lines[1:] if cp.returncode != 0 and len(lines) > 1 else []


def preview_pr(m, target: str | None = None, as_is: bool = False) -> dict:
    """Everything a pull request would publish, without publishing anything: no push, no PR, no change to the
    mission branch. Builds the result in a temporary checkout and throws it away."""
    c, mwt = cfg(m), mission_wt(m)
    remote = c.get("remote")
    if not remote:
        raise GitError("no 'origin' remote")
    out = {"remote": remote, "repo": c.get("github"), "mission_branch": c["branch"], "base": c["base"],
           "target": target or c["base"], "base_on_remote": remote_has_branch(m, c["base"])}
    tool_in_branch = [d for d in TOOL_DIRS if _tracked_in(mwt, "HEAD", d) and d != ".mission"]
    out["tool_dirs_in_branch"] = tool_in_branch
    tgt = target or c["base"]
    if not remote_has_branch(m, tgt):
        out["error"] = f"'{tgt}' isn't on {remote}"
        return out
    out["as_is"] = as_is
    out["will_push_branch"] = c["branch"] if as_is else (f"{c['branch']}-pr" if (target and target != c["base"]) else c["branch"])
    git(m.root, "fetch", "-q", remote, tgt)
    start = fork_point(m)
    out["built_on"] = git(m.root, "rev-parse", "--short", start, check=False) or start
    out["unpublished_not_included"] = [ln for ln in git(m.root, "log", "--format=%h %s", f"{remote}/{tgt}..{start}", check=False).splitlines()][:20]
    out["mission_commits"] = git(m.root, "log", "--format=%h %s", f"{start}..{c['branch']}", check=False).splitlines()[:30]
    excl = [f":(exclude){d}" for d in TOOL_DIRS]
    if as_is:
        # What GitHub will show under "Files changed": the branch compared with where it forked from the target.
        fork = git(mwt, "merge-base", c["branch"], f"{remote}/{tgt}", check=False) or start
        out["built_on"] = git(m.root, "rev-parse", "--short", fork, check=False) or fork
        out["behind"] = len(git(m.root, "rev-list", f"{c['branch']}..{remote}/{tgt}", check=False).split())
        out["unpublished_not_included"] = []
        out["mission_commits"] = git(m.root, "log", "--format=%h %s", f"{fork}..{c['branch']}", check=False).splitlines()[:30]
        out["files"] = git(mwt, "diff", "--numstat", fork, c["branch"], "--", ".", *excl, check=False).splitlines()
        out["stat"] = git(mwt, "diff", "--shortstat", fork, c["branch"], "--", ".", *excl, check=False)
        out["empty"] = not out["files"]
        out["applies_cleanly"] = None
        out["conflicts"] = predict_conflicts(m, f"{remote}/{tgt}")
        return out
    patch = subprocess.run(["git", "diff", "--binary", start, c["branch"], "--", ".", *excl], cwd=str(mwt), capture_output=True).stdout
    out["empty"] = not patch.strip()
    if out["empty"]:
        return out
    tmp = m.dir / "worktrees" / "_preview"
    if tmp.exists():
        ok(m.root, "worktree", "remove", "--force", str(tmp))
    git(m.root, "worktree", "prune")
    git(m.root, "worktree", "add", "-q", "--detach", str(tmp), f"{remote}/{tgt}")
    try:
        ap = subprocess.run(["git", "apply", "--3way", "--index", "-"], cwd=str(tmp), input=patch, capture_output=True)
        out["applies_cleanly"] = ap.returncode == 0
        if ap.returncode == 0:
            out["files"] = git(tmp, "diff", "--cached", "--numstat").splitlines()
            out["stat"] = git(tmp, "diff", "--cached", "--shortstat")
        else:
            out["conflicts"] = git(tmp, "diff", "--name-only", "--diff-filter=U", check=False).split()
            out["apply_error"] = (ap.stderr.decode(errors="ignore").strip().splitlines() or [""])[-1]
    finally:
        ok(m.root, "worktree", "remove", "--force", str(tmp))
    return out


def create_pr(m, title: str, body: str, target: str | None = None, as_is: bool = False, head_branch: str | None = None) -> str:
    """Open the pull request. Nothing is pushed until we know the pull request can exist:
    the branch it targets must be on the remote. With target='main' (or any branch that is), the PR carries only the
    mission's own changes. Returns the PR URL ('' if gh isn't available)."""
    c = cfg(m)
    mwt = mission_wt(m)
    if not c.get("remote"):
        raise GitError("no 'origin' remote: push the mission branch yourself, it's " + c["branch"])
    removed = [] if head_branch else strip_tool_files(m)
    base = target or c["base"]
    if not remote_has_branch(m, base):
        dflt = remote_default_branch(m)
        raise BaseMissing(f"The mission was built on '{c['base']}', which isn't on {c['remote']}, so a pull request against it can't exist. "
                          f"Nothing was pushed. Open it against '{dflt}' instead, with only this mission's changes?", dflt)
    if as_is:
        # Publish the mission branch exactly as it is. No merging of the target into it: GitHub shows the redesign's own
        # changes, and flags conflicts if the target moved on, which is for the repository owner to judge.
        head = head_branch or c["branch"]
        push(m, head, cwd=mwt)
        behind = len(git(m.root, "rev-list", f"{head}..{c['remote']}/{base}", check=False).split())
        if behind:
            body += (f"\n\n_This was built on an earlier version of `{base}`; `{base}` has {behind} newer commit(s), so GitHub may report "
                     "conflicts. The proposed changes are shown as they were made. Happy to rebase if you want to merge it._")
    elif target and target != c["base"]:
        head = _replay_onto(m, target, title)
        body += f"\n\n_Contains only this mission's own changes, applied on `{target}`._"
    else:
        ok(mwt, "fetch", c["remote"], base)
        upstream = f"{c['remote']}/{base}"
        if ok(mwt, "rev-parse", "--verify", upstream):
            cp = subprocess.run(["git", *_identity_args(mwt), "merge", "--no-edit", upstream], cwd=str(mwt),
                                capture_output=True, text=True)
            if cp.returncode != 0:
                files = git(mwt, "diff", "--name-only", "--diff-filter=U", check=False).split()
                ok(mwt, "merge", "--abort")
                raise GitError(f"{base} has changed and conflicts with the mission in: {', '.join(files)}. "
                               "Resolve by adding a task, then try again.")
        push(m, c["branch"], cwd=mwt)
        head = c["branch"]
    if removed:
        body += f"\n\n_Agent tool folders were left out: {', '.join(removed)}._"
    if not gh_ready(m):
        return ""
    existing = json.loads(gh(m, "pr", "list", "-R", c["github"], "--head", head, "--state", "open", "--json", "url"))
    if existing:
        return existing[0]["url"]
    return gh(m, "pr", "create", "-R", c["github"], "--base", base, "--head", head,
              "--title", title, "--body", body + "\n\n" + MARK)
