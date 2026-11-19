"""Core logic of the mynews deployer: stop / swap code / start / health-check / rollback.

Kept free of MCP code so it can be tested directly. Windows-oriented (taskkill, detached processes).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

REQUIRED_FILES = ("app/main.py", "VERSION", "requirements.txt")


@dataclass
class Config:
    runtime_dir: Path          # holds current/, backups/, run/, venv/
    package_dir: Path          # the only folder packages may come from
    app_port: int = 8000
    health_timeout: float = 25.0

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            runtime_dir=Path(os.environ.get("DEPLOYER_RUNTIME", r"C:\mywork\runtime\mynews")),
            package_dir=Path(os.environ.get("DEPLOYER_PACKAGE_DIR", r"C:\mywork\mynews\dist")),
            app_port=int(os.environ.get("MYNEWS_PORT", "8000")),
        )


class DeployError(Exception):
    pass


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _move(src: Path, dst: Path, retries: int = 15) -> None:
    """Rename with retries: Windows keeps folders locked for a moment after a process dies."""
    for i in range(retries):
        try:
            src.rename(dst)
            return
        except OSError:
            if i == retries - 1:
                raise
            time.sleep(0.5)


class Deployer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.current = cfg.runtime_dir / "current"
        self.backups = cfg.runtime_dir / "backups"
        self.staging_root = cfg.runtime_dir / "staging"
        self.run_dir = cfg.runtime_dir / "run"
        self.venv = cfg.runtime_dir / "venv"
        self.pid_file = self.run_dir / "mynews.pid"
        self.log_file = self.run_dir / "mynews.log"
        self.req_hash_file = self.run_dir / "requirements.sha256"
        self.base_url = f"http://127.0.0.1:{cfg.app_port}"
        self._lock = threading.Lock()
        for d in (self.backups, self.staging_root, self.run_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- helpers ----------
    @property
    def venv_python(self) -> Path:
        return self.venv / "Scripts" / "python.exe"

    def _read_pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out

    def _health(self) -> tuple[bool, str | None]:
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=2)
            if r.status_code != 200:
                return False, None
            v = httpx.get(f"{self.base_url}/version", timeout=2).json().get("version")
            return True, v
        except Exception:
            return False, None

    def _current_version(self) -> str | None:
        try:
            return (self.current / "VERSION").read_text().strip()
        except OSError:
            return None

    # ---------- public operations ----------
    def status(self) -> dict:
        pid = self._read_pid()
        healthy, running_version = self._health()
        return {
            "running": healthy,
            "pid": pid if pid and self._pid_alive(pid) else None,
            "running_version": running_version,
            "deployed_version": self._current_version(),
            "backups": sorted(p.name for p in self.backups.iterdir() if p.is_dir()),
            "url": self.base_url,
        }

    def stop(self) -> dict:
        with self._lock:
            return self._stop()

    def restart(self) -> dict:
        with self._lock:
            self._stop()
            return self._start(expect_version=self._current_version())

    def apply_patch(self, package_path: str) -> dict:
        with self._lock:
            return self._apply_patch(package_path)

    def rollback(self) -> dict:
        with self._lock:
            return self._rollback_to_latest_backup()

    # ---------- internals ----------
    def _stop(self) -> dict:
        pid = self._read_pid()
        stopped = False
        if pid and self._pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            for _ in range(20):
                if not self._pid_alive(pid):
                    break
                time.sleep(0.25)
            stopped = True
        self.pid_file.unlink(missing_ok=True)
        return {"stopped": stopped, "pid": pid}

    def _start(self, expect_version: str | None) -> dict:
        if not self.venv_python.exists() or not (self.current / "app" / "main.py").exists():
            raise DeployError("nothing deployed yet (no venv or code in runtime/current)")
        if self._health()[0]:
            raise DeployError(f"port {self.cfg.app_port} already answers /health; "
                              "something else (not managed by the deployer) is running there")
        log = open(self.log_file, "ab")
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
        proc = subprocess.Popen(
            [str(self.venv_python), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.cfg.app_port)],
            cwd=self.current, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        self.pid_file.write_text(str(proc.pid))
        deadline = time.time() + self.cfg.health_timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise DeployError(f"app exited during startup (code {proc.returncode}); see {self.log_file}")
            healthy, version = self._health()
            if healthy:
                if expect_version and version != expect_version:
                    raise DeployError(f"app is healthy but reports version {version}, expected {expect_version}")
                return {"started": True, "pid": proc.pid, "version": version}
            time.sleep(0.5)
        raise DeployError(f"app did not become healthy within {self.cfg.health_timeout:.0f}s; see {self.log_file}")

    def _validate_and_extract(self, package_path: str) -> tuple[Path, str]:
        pkg = Path(package_path).resolve()
        allowed = self.cfg.package_dir.resolve()
        if allowed not in pkg.parents:
            raise DeployError(f"package must be inside {allowed}")
        if pkg.suffix != ".zip" or not pkg.is_file():
            raise DeployError(f"not a .zip file: {pkg}")
        try:
            zf = zipfile.ZipFile(pkg)
        except zipfile.BadZipFile:
            raise DeployError("package is not a valid zip file")
        with zf:
            names = zf.namelist()
            for n in names:
                p = Path(n)
                if p.is_absolute() or ".." in p.parts or ":" in n:
                    raise DeployError(f"unsafe path in package: {n}")
            for req in REQUIRED_FILES:
                if req not in names:
                    raise DeployError(f"package is missing {req}")
            staging = self.staging_root / _stamp()
            if staging.exists():
                shutil.rmtree(staging)
            zf.extractall(staging)
        version = (staging / "VERSION").read_text().strip()
        return staging, version

    def _ensure_deps(self, staging: Path) -> None:
        if not self.venv_python.exists():
            r = subprocess.run([sys.executable, "-m", "venv", str(self.venv)], capture_output=True, text=True)
            if r.returncode != 0:
                raise DeployError(f"venv creation failed: {r.stderr[-500:]}")
            self.req_hash_file.unlink(missing_ok=True)
        req = staging / "requirements.txt"
        digest = hashlib.sha256(req.read_bytes()).hexdigest()
        if self.req_hash_file.exists() and self.req_hash_file.read_text().strip() == digest:
            return
        r = subprocess.run([str(self.venv_python), "-m", "pip", "install", "-q", "-r", str(req)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise DeployError(f"pip install failed: {r.stderr[-500:]}")
        self.req_hash_file.write_text(digest)

    def _apply_patch(self, package_path: str) -> dict:
        old_version = self._current_version()
        try:
            staging, new_version = self._validate_and_extract(package_path)
            self._ensure_deps(staging)  # before touching the running app: failures here cost no downtime
        except DeployError as e:
            return {"ok": False, "stage": "validate", "error": str(e), "rolled_back": False}

        self._stop()
        backup = None
        if self.current.exists():
            backup = self.backups / f"{_stamp()}-v{old_version or 'unknown'}"
            _move(self.current, backup)
        _move(staging, self.current)
        try:
            started = self._start(expect_version=new_version)
        except DeployError as e:
            err = str(e)
            self._stop()
            if backup is not None:
                _move(self.current, self.backups / f"failed-{_stamp()}-v{new_version}")
                _move(backup, self.current)
                try:
                    self._start(expect_version=old_version)
                    return {"ok": False, "stage": "start", "error": err, "rolled_back": True,
                            "running_version": old_version}
                except DeployError as e2:
                    return {"ok": False, "stage": "rollback", "rolled_back": True,
                            "error": f"{err}; rollback also failed: {e2}"}
            return {"ok": False, "stage": "start", "error": err, "rolled_back": False}
        return {"ok": True, "old_version": old_version, "new_version": new_version,
                "pid": started["pid"], "backup": backup.name if backup else None}

    def _rollback_to_latest_backup(self) -> dict:
        candidates = sorted(p for p in self.backups.iterdir()
                            if p.is_dir() and not p.name.startswith(("failed-", "rolledback-")))
        if not candidates:
            return {"ok": False, "error": "no backups available"}
        latest = candidates[-1]
        from_version = self._current_version()
        to_version = (latest / "VERSION").read_text().strip() if (latest / "VERSION").exists() else None
        self._stop()
        if self.current.exists():
            _move(self.current, self.backups / f"rolledback-{_stamp()}-v{from_version}")
        _move(latest, self.current)
        try:
            started = self._start(expect_version=to_version)
        except DeployError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "from_version": from_version, "to_version": to_version, "pid": started["pid"]}
