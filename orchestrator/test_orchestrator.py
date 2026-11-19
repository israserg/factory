"""Offline tests: the claude CLI is replaced by fake_claude.py, no network, no Jira, no deployer."""
import json
import sys
from pathlib import Path

import pytest

import claude_stage
import run_pipeline
import stages
from claude_stage import StageFailure, run_claude, write_mcp_config
from config import Config

HERE = Path(__file__).parent
SCHEMA = {"type": "object", "properties": {"outcome": {"type": "string"}}, "required": ["outcome"]}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "call.json"))
    c = Config(state_dir=tmp_path / "state", deployer_token="s3cret")
    c.claude_cmd = [sys.executable, str(HERE / "fake_claude.py")]
    return c


def test_run_claude_parses_structured_output_and_builds_safe_command(cfg, tmp_path):
    res = run_claude(cfg, "intake", "hello prompt", cwd=tmp_path, schema=SCHEMA,
                     allowed_tools=["Read", "mcp__jira__get_issue"], run_dir=tmp_path / "run",
                     mcp_servers=["jira"])
    assert res["output"]["issue_key"] == "KAN-9" and res["cost_usd"] == 0.0123 and res["turns"] == 3
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert call["prompt"] == "hello prompt"                       # prompt goes via stdin, not argv
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"  # user plugins kept out
    assert argv[argv.index("--allowedTools") + 1] == "Read,mcp__jira__get_issue"
    assert "--max-budget-usd" in argv and "--max-turns" in argv
    assert (tmp_path / "run" / "intake" / "prompt.md").read_text() == "hello prompt"
    assert (tmp_path / "run" / "intake" / "result.json").exists()


@pytest.mark.parametrize("mode,fragment", [("garbage", "unparsable"), ("error", "agent failed"),
                                           ("no_structured", "agent failed")])
def test_run_claude_failures_become_stage_failures(cfg, tmp_path, monkeypatch, mode, fragment):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(StageFailure, match=fragment):
        run_claude(cfg, "intake", "p", cwd=tmp_path, schema=SCHEMA, allowed_tools=["Read"], run_dir=tmp_path / "r")


def test_mcp_config_has_both_servers_and_forward_slashes(cfg, tmp_path):
    p = write_mcp_config(tmp_path / "m.json", cfg, ["jira", "deployer"])
    conf = json.loads(p.read_text())["mcpServers"]
    assert conf["jira"]["type"] == "stdio" and "\\" not in conf["jira"]["command"]
    assert conf["deployer"]["headers"]["Authorization"] == "Bearer s3cret"


def test_parse_pytest_extracts_failures():
    out = ("FAILED tests/test_stats.py::test_count - assert 404 == 200\n"
           "FAILED tests/test_stats.py::test_other - boom\n2 failed, 4 passed in 0.5s\n")
    r = stages.parse_pytest(out, 1)
    assert r["failed"] == 2 and r["passed"] == 4
    assert r["failed_files"] == ["tests/test_stats.py"]
    assert r["failed_tests"][0] == "tests/test_stats.py::test_count"


def test_render_fills_and_rejects_leftovers(cfg, tmp_path):
    cfg.prompts_dir = tmp_path
    (tmp_path / "t.md").write_text("hi {{name}} {{other}}")
    assert stages.render(cfg, "t", name="x", other="y") == "hi x y"
    with pytest.raises(RuntimeError, match="unfilled"):
        stages.render(cfg, "t", name="x")


def test_real_prompts_have_all_placeholders_filled(cfg):
    stages.render(cfg, "intake", project="KAN", mode_note="m", unclear_action="u", ready_action="r")
    stages.render(cfg, "implement", branch="b", python="p", instructions="i")
    stages.render(cfg, "deploy", package="x.zip")
    stages.render(cfg, "close", issue_key="KAN-1", comment="c")


def _run(monkeypatch, cfg, fake_stages, argv):
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(run_pipeline, "STAGES", fake_stages)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    return run_pipeline.main()


def test_pipeline_stops_gracefully_when_a_stage_says_stop(cfg, monkeypatch):
    def intake(ctx, cfg, run_dir, log):
        ctx["stop_reason"] = "no tasks in 'To Do'"
        return "stop"

    def never(ctx, cfg, run_dir, log):
        raise AssertionError("must not run after stop")

    rc = _run(monkeypatch, cfg, [(1, "intake", intake), (2, "implement", never)], ["--dry-run"])
    assert rc == 0
    run = next(cfg.state_dir.iterdir())
    ctx = json.loads((run / "run.json").read_text())
    assert ctx["status"] == "stopped" and "implement" not in ctx["stages"]


def test_pipeline_failure_runs_cleanup_and_returns_1(cfg, monkeypatch):
    cleaned = []

    def boom(ctx, cfg, run_dir, log):
        raise StageFailure("tests still fail")

    monkeypatch.setattr(run_pipeline, "cleanup_after_failure",
                        lambda ctx, cfg, no, reason, log: cleaned.append((no, reason)))
    rc = _run(monkeypatch, cfg, [(1, "intake", lambda *a: None), (2, "implement", boom)], [])
    assert rc == 1 and cleaned == [(2, "tests still fail")]
    ctx = json.loads((next(cfg.state_dir.iterdir()) / "run.json").read_text())
    assert ctx["status"] == "failed" and ctx["stages"]["intake"]["status"] == "ok"


def test_single_stage_rerun_loads_saved_state(cfg, monkeypatch):
    seen = {}

    def one(ctx, cfg, run_dir, log):
        ctx["issue_key"] = "KAN-5"

    def two(ctx, cfg, run_dir, log):
        seen["key"] = ctx.get("issue_key")

    fake = [(1, "intake", one), (2, "implement", two)]
    assert _run(monkeypatch, cfg, fake, ["--only", "1"]) == 0
    run_id = next(cfg.state_dir.iterdir()).name
    assert _run(monkeypatch, cfg, fake, ["--run-id", run_id, "--only", "2"]) == 0
    assert seen["key"] == "KAN-5"
