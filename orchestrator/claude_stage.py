"""Run one agent stage through the headless Claude CLI and return its structured result.

Contract: the agent must answer with JSON matching `schema` (enforced by --json-schema); we read it from the
`structured_output` field of `--output-format json`. Everything is saved under <run_dir>/<stage>/ for inspection.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from config import Config


class StageFailure(Exception):
    """A stage could not complete; the message is human readable and goes to logs/Jira."""


def write_mcp_config(path: Path, cfg: Config, servers: list[str]) -> Path:
    """Generate an MCP config for the given servers ('jira', 'deployer'). May contain a secret -> lives in state/."""
    conf: dict = {}
    if "jira" in servers:
        conf["jira"] = {
            "type": "stdio",
            "command": Path(cfg.factory / ".venv" / "Scripts" / "python.exe").as_posix(),
            "args": [(cfg.factory / "jira-mcp" / "server.py").as_posix()],
        }
    if "deployer" in servers:
        conf["deployer"] = {
            "type": "http",
            "url": cfg.deployer_url,
            "headers": {"Authorization": f"Bearer {cfg.deployer_token}"},
        }
    path.write_text(json.dumps({"mcpServers": conf}, indent=2))
    return path


def run_claude(cfg: Config, stage: str, prompt: str, *, cwd: Path, schema: dict, allowed_tools: list[str],
               run_dir: Path, mcp_servers: list[str] | None = None, limits_key: str | None = None,
               permission_mode: str | None = None, disallowed_tools: list[str] | None = None) -> dict:
    max_turns, budget, timeout = cfg.limits[limits_key or stage]
    stage_dir = run_dir / stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    cmd = [*cfg.claude_cmd, "-p", "--output-format", "json",
           "--json-schema", json.dumps(schema),
           "--no-session-persistence",
           "--setting-sources", "project",       # keep user-level plugins/hooks out of pipeline agents
           "--strict-mcp-config",                # only the MCP servers we hand over
           "--max-turns", str(max_turns),
           "--max-budget-usd", str(budget),
           "--allowedTools", ",".join(allowed_tools)]
    if disallowed_tools:                         # removed from the agent's toolbox, so it cannot even try them
        cmd += ["--disallowedTools", ",".join(disallowed_tools)]
    if mcp_servers:
        cmd += ["--mcp-config", str(write_mcp_config(stage_dir / "mcp.json", cfg, mcp_servers))]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]

    try:
        proc = subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StageFailure(f"{stage}: claude timed out after {timeout}s")
    (stage_dir / "claude.stdout.json").write_text(proc.stdout or "", encoding="utf-8")
    (stage_dir / "claude.stderr.txt").write_text(proc.stderr or "", encoding="utf-8")

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        raise StageFailure(f"{stage}: claude exit {proc.returncode}, unparsable output: "
                           f"{(proc.stdout or proc.stderr or '')[:300]}")
    out = data.get("structured_output")
    if data.get("is_error") or out is None:
        raise StageFailure(f"{stage}: agent failed ({data.get('subtype')}, stop={data.get('terminal_reason')}): "
                           f"{str(data.get('result'))[:300]}")
    result = {
        "output": out,
        "cost_usd": data.get("total_cost_usd", 0.0),
        "turns": data.get("num_turns"),
        "permission_denials": data.get("permission_denials", []),
    }
    (stage_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
