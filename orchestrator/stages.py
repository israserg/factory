"""The 7 pipeline stages.

Agent stages (1 intake, 2 implement, 4 deploy, 7 close) call `claude -p`; after each one we re-check the outcome
ourselves (Jira status, files, /version) instead of trusting the agent's report. Stages 3, 5, 6 are plain code.
Each stage takes (ctx, cfg, run_dir, log) and either returns normally, returns "stop" (graceful end),
or raises StageFailure.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx

from claude_stage import StageFailure, run_claude
from config import FACTORY, Config

sys.path.insert(0, str(FACTORY / "jira-mcp"))
from jira_client import Jira, JiraError  # noqa: E402

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
COAUTHOR = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


# ---------------------------------------------------------------- helpers
def render(cfg: Config, name: str, /, **values) -> str:
    text = (cfg.prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{(\w+)\}\}", text)
    if left:
        raise RuntimeError(f"prompt {name}: unfilled placeholders {left}")
    return text


def sh(cmd: list[str], cwd: Path, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, **(env or {})})
    if check and p.returncode != 0:
        raise StageFailure(f"command failed ({p.returncode}): {' '.join(cmd)}\n{(p.stderr or p.stdout)[-600:]}")
    return p


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return sh(["git", *args], cwd, check).stdout.strip()


def jira_client() -> Jira:
    return Jira(os.environ["JIRA_SITE"], os.environ["JIRA_EMAIL"], os.environ["JIRA_TOKEN"])


def deployer_call(cfg: Config, tool: str, arguments: dict | None = None) -> dict:
    """Call a deployer MCP tool directly (JSON-RPC over HTTP; the server is stateless)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}
    try:
        r = httpx.post(cfg.deployer_url, json=body, timeout=180, headers={
            "Authorization": f"Bearer {cfg.deployer_token}",
            "Accept": "application/json, text/event-stream"})
    except httpx.HTTPError as e:
        raise StageFailure(f"deployer unreachable at {cfg.deployer_url}: {e} "
                           f"(start it with scripts\\start-deployer.ps1)")
    if r.status_code != 200:
        raise StageFailure(f"deployer returned HTTP {r.status_code}")
    res = r.json().get("result", {})
    if res.get("isError"):
        raise StageFailure(f"deployer tool {tool} error: {res.get('content')}")
    return json.loads(res["content"][0]["text"])


def app_version(cfg: Config) -> str | None:
    try:
        return httpx.get(cfg.app_url + "/version", timeout=5).json().get("version")
    except Exception:
        return None


def parse_pytest(output: str, exit_code: int) -> dict:
    failed = re.findall(r"^FAILED (\S+?)::(\S+)", output, flags=re.M)
    m_pass = re.search(r"(\d+) passed", output)
    m_fail = re.search(r"(\d+) failed", output)
    return {"exit_code": exit_code,
            "passed": int(m_pass.group(1)) if m_pass else 0,
            "failed": int(m_fail.group(1)) if m_fail else 0,
            "failed_tests": [f"{f}::{n}" for f, n in failed],
            "failed_files": sorted({f.replace("\\", "/") for f, _ in failed})}


def run_tests(cfg: Config, ctx: dict, run_dir: Path, label: str) -> dict:
    p = sh([cfg.python, "-m", "pytest", "-q", "-rf", "--tb=line", "-p", "no:cacheprovider", "tests"],
           Path(ctx["work_dir"]), check=False, env={"BASE_URL": cfg.app_url})
    out = (p.stdout or "") + (p.stderr or "")
    (run_dir / f"{label}.txt").write_text(out, encoding="utf-8")
    return parse_pytest(out, p.returncode)


def instructions_text(ctx: dict, run_dir: Path) -> str:
    return (run_dir / "instructions.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. intake
INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["ready", "unclear", "no_tasks"]},
        "issue_key": {"type": "string"}, "title": {"type": "string"},
        "instructions_md": {"type": "string"}, "reason": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "issue_key", "title", "instructions_md", "assumptions", "reason"],
}


def assign_ticket(j: Jira, cfg: Config, key: str) -> None:
    """Make the pipeline's owner account the assignee of the ticket."""
    j.assign(key, j.resolve_assignee(cfg.project, cfg.assignee_name))


def intake(ctx, cfg, run_dir, log):
    dry = ctx["dry_run"]
    try:  # read-only: which ticket will the agent take? (the oldest in To Do)
        todo = jira_client().list_issues(cfg.project)
    except (JiraError, KeyError) as e:
        raise StageFailure(f"cannot read Jira: {e!r}")
    if not todo:
        ctx["stop_reason"] = "no tasks in 'To Do'"
        return "stop"
    candidate = todo[0]["key"]
    ctx["issue_key"] = candidate   # known from here on, so any later failure can be recorded on the ticket

    if dry:
        mode = ("DRY RUN: you must NOT change anything in Jira (no transition_issue, no add_comment; those tools are "
                "not available). Ignore the .venv/ and dist/ folders when reading code.")
        unclear = "do NOT comment on the issue (dry run)"
        ready = "do NOT move it (dry run)"
        tools = ["mcp__jira__list_todo", "mcp__jira__get_issue"]
    else:
        mode = "Ignore the .venv/ and dist/ folders when reading code."
        unclear = "do NOT comment on the issue and do NOT change its status (the pipeline records the failure itself)"
        ready = "move the issue to 'In Progress' with `transition_issue`"
        tools = ["mcp__jira__list_todo", "mcp__jira__get_issue", "mcp__jira__transition_issue"]
    prompt = render(cfg, "intake", project=cfg.project, candidate=candidate, mode_note=mode,
                    unclear_action=unclear, ready_action=ready)
    res = run_claude(cfg, "intake", prompt, cwd=cfg.mynews_repo, schema=INTAKE_SCHEMA,
                     allowed_tools=tools + ["Read", "Glob", "Grep"], run_dir=run_dir, mcp_servers=["jira"])
    ctx["_cost"] = res["cost_usd"]
    out = res["output"]
    if out["outcome"] == "no_tasks" or out["issue_key"] != candidate:
        raise StageFailure(f"the agent took {out['issue_key']!r} ({out['outcome']}) but the oldest To Do is {candidate}")
    ctx["title"] = out["title"]
    if out["outcome"] == "unclear":
        raise StageFailure(f"{candidate} cannot be implemented: {out['reason']}")
    if len(out["instructions_md"].strip()) < 100:
        raise StageFailure("intake returned empty/too short instructions")
    (run_dir / "instructions.md").write_text(out["instructions_md"], encoding="utf-8")
    ctx["assumptions"] = [a.strip() for a in out.get("assumptions", []) if a.strip()]
    log(f"task {out['issue_key']}: {out['title']}")
    if ctx["assumptions"]:
        log(f"assumptions (no questions asked): {'; '.join(ctx['assumptions'])}")
    if not dry:  # verify the agent really moved it, then take ownership of the ticket
        j = jira_client()
        status = j.get_issue(candidate)["status"]
        if status != "In Progress":
            raise StageFailure(f"{candidate} should be 'In Progress' but is '{status}'")
        ctx["jira_in_progress"] = True
        try:
            assign_ticket(j, cfg, candidate)
        except JiraError as e:
            raise StageFailure(f"could not assign {candidate} to {cfg.assignee_name}: {e}")
        log(f"{candidate} is In Progress and assigned to {cfg.assignee_name}")


# ---------------------------------------------------------------- 2. implement
IMPLEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["done", "failed"]},
        "summary": {"type": "string"}, "version": {"type": "string"}, "package": {"type": "string"},
        "test_files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "summary", "version", "package", "test_files"],
}


def _clone_url(cfg: Config) -> str:
    p = sh(["git", "remote", "get-url", "origin"], cfg.mynews_repo, check=False)
    return p.stdout.strip() or str(cfg.mynews_repo)


def implement(ctx, cfg, run_dir, log):
    work = run_dir / "work" / "mynews"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.parent.mkdir(parents=True, exist_ok=True)
    sh(["git", "clone", "-q", _clone_url(cfg), str(work)], run_dir)
    for key in ("user.name", "user.email"):
        sh(["git", "config", key, git(cfg.mynews_repo, "config", key)], work)
    branch = f"task/{ctx['issue_key']}"
    git(work, "checkout", "-q", "-b", branch)
    ctx.update(work_dir=str(work), branch=branch,
               base_version=(work / "VERSION").read_text().strip(), base_head=git(work, "rev-parse", "HEAD"))

    py = Path(cfg.python).as_posix()
    prompt = render(cfg, "implement", branch=branch, python=py, instructions=instructions_text(ctx, run_dir))
    tools = ["Read", "Edit", "Write", "Glob", "Grep", f"Bash({py}:*)", "Bash(git status:*)", "Bash(git diff:*)"]
    res = run_claude(cfg, "implement", prompt, cwd=work, schema=IMPLEMENT_SCHEMA, allowed_tools=tools,
                     run_dir=run_dir, permission_mode="acceptEdits")
    ctx["_cost"] = res["cost_usd"]
    out = res["output"]
    if res["permission_denials"]:
        log(f"warning: {len(res['permission_denials'])} permission denial(s), see implement/result.json")
    if out["outcome"] != "done":
        raise StageFailure(f"agent could not implement the task: {out['summary']}")

    # --- independent verification of what the agent claims
    version = (work / "VERSION").read_text().strip()
    if version != out["version"] or version == ctx["base_version"]:
        raise StageFailure(f"VERSION must be bumped (base {ctx['base_version']}, file {version}, reported {out['version']})")
    if git(work, "rev-parse", "HEAD") != ctx["base_head"]:
        raise StageFailure("agent committed or moved HEAD; only the orchestrator may commit")
    tests = [t.replace("\\", "/") for t in out["test_files"]]
    if not tests or not all(t.startswith("tests/") and (work / t).is_file() for t in tests):
        raise StageFailure(f"reported test files missing or outside tests/: {tests}")
    pkg = work / out["package"]
    if not pkg.is_file() or pkg.name != f"mynews-{version}.zip":
        raise StageFailure(f"package missing or wrong name: {pkg}")
    with zipfile.ZipFile(pkg) as z:
        if z.read("VERSION").decode().strip() != version:
            raise StageFailure("VERSION inside the package does not match")
    changed = git(work, "status", "--porcelain")
    if not changed:
        raise StageFailure("no changes in the working tree")
    (run_dir / "git-status.txt").write_text(changed, encoding="utf-8")

    cfg.package_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.package_dir / pkg.name
    shutil.copy2(pkg, target)
    ctx.update(version=version, package_path=target.as_posix(), test_files=tests, summary=out["summary"])
    log(f"implemented v{version}, tests: {tests}, package: {target.name}")


# ---------------------------------------------------------------- 3. test before
def test_before(ctx, cfg, run_dir, log):
    if app_version(cfg) is None:
        raise StageFailure(f"mynews is not answering at {cfg.app_url}; deploy a baseline first")
    r = run_tests(cfg, ctx, run_dir, "test_before")
    ctx["tests_before"] = r
    log(f"tests before: exit {r['exit_code']}, {r['failed']} failed, {r['passed']} passed")
    if r["exit_code"] == 0:
        raise StageFailure("tests PASS on the old app: they do not prove the change (expected failures)")
    if r["exit_code"] != 1:
        raise StageFailure(f"pytest exit code {r['exit_code']} (error, not a test failure); see test_before.txt")
    outside = [f for f in r["failed_files"] if f not in ctx["test_files"]]
    if outside:
        raise StageFailure(f"existing tests fail on the old app ({outside}); the baseline is not clean")


# ---------------------------------------------------------------- 4. deploy
DEPLOY_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "old_version": {"type": ["string", "null"]},
                   "new_version": {"type": ["string", "null"]}, "message": {"type": "string"}},
    "required": ["ok", "old_version", "new_version", "message"],
}


def deploy(ctx, cfg, run_dir, log):
    deployer_call(cfg, "get_status")  # preflight: deployer up and token valid
    ctx["version_before_deploy"] = app_version(cfg)
    prompt = render(cfg, "deploy", package=ctx["package_path"])
    res = run_claude(cfg, "deploy", prompt, cwd=run_dir, schema=DEPLOY_SCHEMA,
                     allowed_tools=["mcp__deployer__get_status", "mcp__deployer__apply_patch"],
                     run_dir=run_dir, mcp_servers=["deployer"])
    ctx["_cost"] = res["cost_usd"]
    out = res["output"]
    if not out["ok"]:
        raise StageFailure(f"apply_patch failed: {out['message']}")
    ctx["deployed_new_version"] = True
    live = app_version(cfg)
    if live != ctx["version"]:
        raise StageFailure(f"agent reported success but the app serves version {live}, expected {ctx['version']}")
    log(f"deployed: app now serves v{live} (was {ctx['version_before_deploy']})")


# ---------------------------------------------------------------- 5. test after
def test_after(ctx, cfg, run_dir, log):
    r = run_tests(cfg, ctx, run_dir, "test_after")
    ctx["tests_after"] = r
    log(f"tests after: exit {r['exit_code']}, {r['failed']} failed, {r['passed']} passed")
    if r["exit_code"] != 0:
        raise StageFailure(f"tests still fail after deploy: {r['failed_tests'] or 'see test_after.txt'}")


# ---------------------------------------------------------------- 6. commit
def commit(ctx, cfg, run_dir, log):
    work = Path(ctx["work_dir"])
    git(work, "add", "-A")
    if not git(work, "diff", "--cached", "--name-only"):
        raise StageFailure("nothing to commit")
    subject = f"{ctx['issue_key']}: {ctx['title']}"
    if len(subject) > 72:
        subject = subject[:71].rstrip() + "…"
    msg = (f"{subject}\n\n{ctx['summary']}\n\n"
           f"Version: {ctx['version']}\nJira: {ctx['issue_key']}\nPipeline run: {ctx['run_id']}\n\n{COAUTHOR}\n")
    git(work, "commit", "-q", "-m", msg)
    ctx["commit"] = git(work, "rev-parse", "--short", "HEAD")
    git(work, "checkout", "-q", "main")
    git(work, "merge", "-q", "--ff-only", ctx["branch"])
    log(f"committed {ctx['commit']} on {ctx['branch']} and fast-forwarded main")
    if ctx["dry_run"] or ctx["no_push"]:
        log("push skipped (dry-run / --no-push)")
        return
    git(work, "push", "-q", "origin", "main", ctx["branch"])
    ctx["pushed"] = True
    log("pushed main and the task branch to origin")
    dev = cfg.mynews_repo
    if not git(dev, "status", "--porcelain") and git(dev, "rev-parse", "--abbrev-ref", "HEAD") == "main":
        if sh(["git", "pull", "-q", "--ff-only"], dev, check=False).returncode == 0:
            log("dev checkout c:\\mywork\\mynews fast-forwarded")


# ---------------------------------------------------------------- 7. close
CLOSE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "final_status": {"type": "string"}, "message": {"type": "string"}},
    "required": ["ok", "final_status", "message"],
}


def close(ctx, cfg, run_dir, log):
    if ctx["dry_run"]:
        log("dry run: Jira not touched")
        return
    tb, ta = ctx["tests_before"], ctx["tests_after"]
    assumed = ctx.get("assumptions") or []
    assumptions = ("Assumptions made (the ticket was ambiguous, no questions were asked):\n"
                   + "\n".join(f"- {a}" for a in assumed) + "\n\n") if assumed else ""
    comment = (f"Implemented and deployed by the automated pipeline.\n\n{ctx['summary']}\n\n{assumptions}"
               f"Version: {ctx['version']}\nCommit: {ctx['commit']} (branch {ctx['branch']}, merged to main)\n"
               f"Tests before deploy: {tb['failed']} failed as expected. After deploy: {ta['passed']} passed, "
               f"{ta['failed']} failed.\nRun: {ctx['run_id']}")
    prompt = render(cfg, "close", issue_key=ctx["issue_key"], comment=comment)
    res = run_claude(cfg, "close", prompt, cwd=run_dir, schema=CLOSE_SCHEMA,
                     allowed_tools=["mcp__jira__add_comment", "mcp__jira__transition_issue", "mcp__jira__get_issue"],
                     run_dir=run_dir, mcp_servers=["jira"])
    ctx["_cost"] = res["cost_usd"]
    status = jira_client().get_issue(ctx["issue_key"])["status"]
    if status != "Done":
        raise StageFailure(f"{ctx['issue_key']} should be 'Done' but is '{status}'")
    log(f"{ctx['issue_key']} is Done")


def preflight(cfg: Config, first: int, last: int, dry_run: bool = False) -> None:
    """Fail fast, before any Jira change or agent work, if something the selected stages need is missing."""
    if any(n in range(first, last + 1) for n in (3, 4, 5)):
        deployer_call(cfg, "get_status")  # raises StageFailure if unreachable or token rejected
        if app_version(cfg) is None:
            raise StageFailure(f"mynews is not answering at {cfg.app_url}; deploy a baseline first")
    if first <= 1 <= last and not dry_run:
        # Failure handling needs the 'Build Broken' status and an assignable owner; without them a failed
        # ticket could not be parked and the next run would pick it up again.
        try:
            j = jira_client()
            if cfg.broken_status not in j.project_statuses(cfg.project):
                raise StageFailure(f"Jira project {cfg.project} has no status '{cfg.broken_status}'; add it as a board "
                                   f"column (failed tickets are moved there)")
            j.resolve_assignee(cfg.project, cfg.assignee_name)
        except (JiraError, KeyError) as e:
            raise StageFailure(f"Jira preflight failed: {e!r}")


STAGES = [
    (1, "intake", intake), (2, "implement", implement), (3, "test_before", test_before),
    (4, "deploy", deploy), (5, "test_after", test_after), (6, "commit", commit), (7, "close", close),
]


# ---------------------------------------------------------------- failure handling
def cleanup_after_failure(ctx: dict, cfg: Config, stage_no: int, reason: str, log) -> None:
    """Undo what can be undone: roll the app back if the new version is live and tests failed; then record the
    failure on the Jira ticket (comment, owner, status 'Build Broken'). Never raises."""
    try:
        if ctx.get("deployed_new_version") and stage_no == 5:
            r = deployer_call(cfg, "rollback")
            log(f"rollback: {r}")
            ctx["rolled_back"] = bool(r.get("ok"))
    except Exception as e:  # noqa: BLE001
        log(f"rollback failed: {e}")
    key = ctx.get("issue_key")
    if not key:
        return
    if ctx.get("dry_run"):
        log(f"dry run: {key} left untouched in Jira")
        return
    stage_name = {n: name for n, name, _ in STAGES}.get(stage_no, str(stage_no))
    try:
        j = jira_client()
    except KeyError as e:
        log(f"cannot update Jira, missing setting {e}")
        return
    note = " The app was rolled back to the previous version." if ctx.get("rolled_back") else ""
    try:
        j.add_comment(key, f"Automated pipeline: this task could not be completed (stage {stage_no}, {stage_name}).\n\n"
                      f"{reason[:1500]}\n\n{note.strip()}\nThe ticket was moved to '{cfg.broken_status}'. "
                      f"Run: {ctx['run_id']}")
    except Exception as e:  # noqa: BLE001
        log(f"could not comment on {key}: {e}")
    try:
        assign_ticket(j, cfg, key)
    except Exception as e:  # noqa: BLE001
        log(f"could not assign {key}: {e}")
    try:
        j.transition(key, cfg.broken_status)
        ctx["jira_broken"] = True
        log(f"{key}: commented and moved to {cfg.broken_status}")
    except Exception as e:  # noqa: BLE001
        log(f"could not move {key} to {cfg.broken_status}: {e}")
        try:  # never leave it in 'To Do': the next run would pick the same ticket up again
            if j.get_issue(key)["status"] == "To Do":
                j.transition(key, "In Progress")
                log(f"{key} parked in In Progress instead")
        except Exception as e2:  # noqa: BLE001
            log(f"could not park {key}: {e2}")
