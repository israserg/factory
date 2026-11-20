"""Run console: one big button that runs the task-to-production pipeline and shows friendly progress.

Run:  python console/app.py      ->  http://127.0.0.1:8800
The pipeline runs as a subprocess (orchestrator/run_pipeline.py); we read its log lines and translate them.
Only one run at a time. Bound to localhost; state-changing requests need a custom header and a local Host header
(so a random web page in your browser cannot trigger a run).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

HERE = Path(__file__).resolve().parent
FACTORY = HERE.parent
sys.path.insert(0, str(FACTORY / "orchestrator"))
from config import load_dotenv  # noqa: E402

load_dotenv(FACTORY / ".env")

PORT = int(os.environ.get("CONSOLE_PORT", "8800"))
APP_URL = os.environ.get("MYNEWS_URL", "http://127.0.0.1:8000")
DEPLOYER_URL = os.environ.get("DEPLOYER_URL", "http://127.0.0.1:8765/mcp")

STEP_TEXT = {
    "intake": "Picking the ticket from Jira and analysing it",
    "implement": "Implementing the change and writing tests",
    "test_before": "Running tests against the current app (they should fail)",
    "deploy": "Deploying the new version",
    "test_after": "Running the tests again against the new version",
    "commit": "Committing the change and pushing to git",
    "close": "Updating Jira: adding a comment and moving the ticket to Done",
}
STEP_ORDER = list(STEP_TEXT)
LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")


def translate(raw: str) -> dict:
    """Turn one raw pipeline log line into {ts, level, text} for the UI."""
    m = LINE_RE.match(raw)
    ts, msg = (m.group(1), m.group(2)) if m else (time.strftime("%H:%M:%S"), raw)
    level, text = "info", msg
    if (s := re.match(r"--- stage (\d): (\w+)", msg)):
        name = s.group(2)
        level, text = "step", f"[{s.group(1)}/7] {STEP_TEXT.get(name, name)}…"
    elif msg.startswith("run "):
        level, text = "dim", "Run started: " + msg
    elif msg.startswith("preflight:"):
        level, text = "ok", "Checks passed: deployer, mynews and Jira are ready"
    elif "Build Broken" in msg and ": commented and moved to" in msg:
        level, text = "error", f"Ticket {msg.split(':')[0]} was commented and moved to Build Broken"
    elif " is In Progress and assigned to " in msg:
        level, text = "ok", msg.replace(" is In Progress and assigned to ", " is In Progress, owner: ")
    elif msg.startswith("FAILED"):
        level, text = "error", msg
    elif msg.startswith("stopped:"):
        level, text = "warn", "Stopped: " + msg[len("stopped:"):].strip()
    elif (t := re.match(r"task (\S+): (.*)", msg)):
        level, text = "ok", f"Picked ticket {t.group(1)}: {t.group(2)}"
    elif (t := re.match(r"implemented v(\S+), tests: (.*), package: (.*)", msg)):
        level, text = "ok", f"Implemented version {t.group(1)}; tests: {t.group(2)}; package {t.group(3)}"
    elif (t := re.match(r"tests before: exit (\d+), (\d+) failed, (\d+) passed", msg)):
        level, text = "ok", f"Tests on the current app: {t.group(2)} failed (expected), {t.group(3)} passed"
    elif (t := re.match(r"tests after: exit (\d+), (\d+) failed, (\d+) passed", msg)):
        level = "ok" if t.group(1) == "0" else "error"
        text = f"Tests on the new version: {t.group(2)} failed, {t.group(3)} passed"
    elif msg.startswith("deployed:"):
        level, text = "ok", "Deployed: " + msg[len("deployed:"):].strip()
    elif msg.startswith("committed") or msg.startswith("pushed"):
        level = "ok"
    elif msg.endswith("is Done"):
        level, text = "ok", f"Ticket {msg.split()[0]} is Done in Jira"
    elif msg.startswith("warning:"):
        level = "warn"
    elif msg.startswith("result:"):
        level, text = "dim", msg
    return {"ts": ts, "level": level, "text": text}


# The boxes of the progress row: preparation, the 7 pipeline stages, finish.
PROGRESS_KEYS = ["prep"] + [str(i) for i in range(1, 8)] + ["finish"]


def fresh_progress() -> dict:
    """status: pending | running | done | failed | stopped; t0/t1: epoch seconds (None until known)."""
    return {k: {"status": "pending", "t0": None, "t1": None} for k in PROGRESS_KEYS}


class Runner:
    """Starts the pipeline subprocess, collects its output, exposes a snapshot for polling."""

    def __init__(self, command: Callable[[bool], list[str]], cwd: Path):
        self.command, self.cwd = command, cwd
        self.lock = threading.Lock()
        self.lines: list[dict] = []
        self.status = "idle"          # idle | running | done | stopped | failed
        self.gen = 0                  # bumped on every new run so clients know to clear their log
        self.proc: subprocess.Popen | None = None
        self.final_word: str | None = None
        self.progress = fresh_progress()

    # --- progress tracking: derived from the pipeline's own log lines (call with the lock held)
    def _mark(self, key: str, status: str) -> None:
        p = self.progress[key]
        now = time.time()
        if status == "running" and p["t0"] is None:
            p["t0"] = now
        if status in ("done", "failed", "stopped"):
            p["t1"] = now
            if p["t0"] is None:
                p["t0"] = now
        p["status"] = status

    def _close_running(self, status: str) -> None:
        for k in PROGRESS_KEYS[:-1]:
            if self.progress[k]["status"] == "running":
                self._mark(k, status)

    def _track(self, raw: str) -> None:
        m = LINE_RE.match(raw)
        msg = m.group(2) if m else raw
        if msg.startswith("preflight:"):
            self._mark("prep", "done")
        elif msg.startswith("FAILED preflight"):
            self._mark("prep", "failed")
        elif (s := re.match(r"--- stage (\d): ", msg)):
            self._close_running("done")
            if self.progress["prep"]["status"] != "done":
                self._mark("prep", "done")
            self._mark(s.group(1), "running")
        elif (s := re.match(r"FAILED at stage (\d)", msg)):
            self._mark(s.group(1), "failed")

    def start(self, dry_run: bool) -> bool:
        with self.lock:
            if self.status == "running":
                return False
            self.gen += 1
            self.lines, self.status, self.final_word = [], "running", None
            self.progress = fresh_progress()
            self._mark("prep", "running")
            self._add("dim", "Starting the pipeline" + (" (dry run: no Jira changes, no push)" if dry_run else ""))
            self.proc = subprocess.Popen(
                self.command(dry_run), cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            )
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            return True

    def _add(self, level: str, text: str) -> None:
        self.lines.append({"ts": time.strftime("%H:%M:%S"), "level": level, "text": text})

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            raw = raw.rstrip("\r\n")
            if not raw.strip():
                continue
            entry = translate(raw)
            with self.lock:
                self._track(raw)
                self.lines.append(entry)
                if (m := re.match(r"result: (\w+)", raw.split("] ", 1)[-1])):
                    self.final_word = m.group(1)
        rc = proc.wait()
        with self.lock:
            word = self.final_word
            self.status = ("done" if word == "done" else "stopped" if word == "stopped"
                           else "failed" if word == "failed" or rc != 0 else "done")
            if rc != 0 and word is None:
                self._add("error", f"The pipeline exited unexpectedly (code {rc}).")
            # close the progress row: whatever was running finished (ok) or broke (failed)
            self._close_running("failed" if self.status == "failed" else "done")
            self._mark("finish", self.status)
            self._add({"done": "ok", "stopped": "warn", "failed": "error"}[self.status],
                      {"done": "Finished successfully.", "stopped": "Finished: nothing to do.",
                       "failed": "Finished with errors."}[self.status])

    def snapshot(self, since: int, gen: int) -> dict:
        with self.lock:
            reset = gen != self.gen
            start = 0 if reset else max(0, since)
            return {"status": self.status, "gen": self.gen, "reset": reset,
                    "lines": self.lines[start:], "next": len(self.lines),
                    "progress": {k: dict(v) for k, v in self.progress.items()}, "now": time.time()}


def pipeline_command(dry_run: bool) -> list[str]:
    cmd = [sys.executable, "-u", str(FACTORY / "orchestrator" / "run_pipeline.py")]
    return cmd + (["--dry-run"] if dry_run else [])


def check_env() -> dict:
    out = {"mynews": None, "deployer": False}
    try:
        out["mynews"] = httpx.get(APP_URL + "/version", timeout=2).json().get("version")
    except Exception:
        pass
    try:
        r = httpx.post(DEPLOYER_URL, timeout=3, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_status", "arguments": {}}},
                       headers={"Authorization": f"Bearer {os.environ.get('DEPLOYER_TOKEN', '')}",
                                "Accept": "application/json, text/event-stream"})
        out["deployer"] = r.status_code == 200
    except Exception:
        pass
    return out


RUNTIME = Path(os.environ.get("RUNTIME_DIR", r"C:\mywork\runtime"))
_start_lock = threading.Lock()


def deployer_tool(name: str, timeout: float = 60) -> dict:
    """Call a deployer MCP tool (JSON-RPC over HTTP, stateless) and return its parsed result."""
    r = httpx.post(DEPLOYER_URL, timeout=timeout, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}},
        headers={"Authorization": f"Bearer {os.environ.get('DEPLOYER_TOKEN', '')}",
                 "Accept": "application/json, text/event-stream"})
    r.raise_for_status()
    return json.loads(r.json()["result"]["content"][0]["text"])


def start_deployer() -> dict:
    """Launch the deployer MCP server detached in the background (same as scripts/start-deployer.ps1)."""
    if check_env()["deployer"]:
        return {"ok": True, "message": "Deployer MCP is already running."}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [sys.executable, str(FACTORY / "mcp-deployer" / "server.py")], cwd=FACTORY / "mcp-deployer",
        stdout=open(RUNTIME / "deployer.log", "ab"), stderr=open(RUNTIME / "deployer.err.log", "ab"),
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    for _ in range(40):
        time.sleep(0.5)
        if check_env()["deployer"]:
            return {"ok": True, "message": "Deployer MCP started."}
    return {"ok": False, "message": f"Deployer did not come up in 20 s; see {RUNTIME / 'deployer.err.log'}"}


def start_mynews() -> dict:
    """Ask the deployer to start the deployed mynews version (the deployer owns the app's lifecycle)."""
    env = check_env()
    if env["mynews"]:
        return {"ok": True, "message": f"mynews is already running (v{env['mynews']})."}
    if not env["deployer"]:
        return {"ok": False, "message": "The deployer is not running. Click 'Start Deployer MCP' first."}
    try:
        res = deployer_tool("restart")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Deployer call failed: {e}"}
    if res.get("started"):
        return {"ok": True, "message": f"mynews started (v{res.get('version')})."}
    return {"ok": False, "message": f"mynews did not start: {res.get('error', res)}"}


def create_app(runner: Runner) -> FastAPI:
    app = FastAPI(title="mynews run console")
    allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def guard(request: Request) -> None:
        if request.headers.get("host", "") not in allowed_hosts:
            raise HTTPException(403, "bad host")
        if request.method == "POST" and request.headers.get("x-console") != "1":
            raise HTTPException(403, "missing X-Console header")

    @app.get("/", dependencies=[Depends(guard)])
    def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/api/state", dependencies=[Depends(guard)])
    def state(since: int = 0, gen: int = -1):
        return runner.snapshot(since, gen)

    @app.get("/api/env", dependencies=[Depends(guard)])
    def env():
        return check_env()

    @app.post("/api/run", status_code=202, dependencies=[Depends(guard)])
    async def run(request: Request):
        try:
            dry = bool((await request.json()).get("dry_run"))
        except Exception:
            dry = False
        if not runner.start(dry):
            raise HTTPException(409, "a run is already in progress")
        return {"started": True, "dry_run": dry}

    @app.post("/api/start/{target}", dependencies=[Depends(guard)])
    def start_service(target: str):
        actions = {"mynews": start_mynews, "deployer": start_deployer}
        if target not in actions:
            raise HTTPException(404, "unknown target")
        if runner.status == "running":
            raise HTTPException(409, "a pipeline run is in progress; wait until it finishes")
        if not _start_lock.acquire(blocking=False):
            raise HTTPException(409, "another start is already in progress")
        try:
            return actions[target]()
        finally:
            _start_lock.release()

    return app


if __name__ == "__main__":
    uvicorn.run(create_app(Runner(pipeline_command, FACTORY)), host="127.0.0.1", port=PORT)
