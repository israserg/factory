"""Orchestrator configuration: paths, endpoints, secrets (from factory/.env), per-stage limits."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

FACTORY = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _find_claude() -> list[str]:
    if os.environ.get("CLAUDE_BIN"):
        return [os.environ["CLAUDE_BIN"]]
    found = shutil.which("claude")
    if found:
        return [found]
    local = Path.home() / ".local" / "bin" / "claude.exe"
    if local.exists():
        return [str(local)]
    raise RuntimeError("claude CLI not found; set CLAUDE_BIN in factory/.env")


@dataclass
class Config:
    factory: Path = FACTORY
    mynews_repo: Path = Path(r"C:\mywork\mynews")          # dev checkout (source of the clone URL, venv, dist/)
    python: str = r"C:\mywork\mynews\.venv\Scripts\python.exe"  # has pytest + httpx + app deps
    package_dir: Path = Path(r"C:\mywork\mynews\dist")     # the only dir the deployer accepts packages from
    app_url: str = "http://127.0.0.1:8000"
    deployer_url: str = "http://127.0.0.1:8765/mcp"
    deployer_token: str = ""
    state_dir: Path = FACTORY / "orchestrator" / "state"
    prompts_dir: Path = FACTORY / "orchestrator" / "prompts"
    claude_cmd: list[str] = field(default_factory=list)
    project: str = "KAN"
    # per-stage safety limits: (max_turns, max_budget_usd, timeout_seconds)
    limits: dict = field(default_factory=lambda: {
        "intake": (20, 1.0, 600),
        "implement": (60, 5.0, 1500),
        "deploy": (10, 0.5, 300),
        "close": (10, 0.5, 300),
    })

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(FACTORY / ".env")
        cfg = cls(deployer_token=os.environ.get("DEPLOYER_TOKEN", ""))
        cfg.claude_cmd = _find_claude()
        return cfg
