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


def test_disallowed_tools_are_passed_to_the_cli_only_when_given(cfg, tmp_path):
    run_claude(cfg, "intake", "p", cwd=tmp_path, schema=SCHEMA, allowed_tools=["Read"], run_dir=tmp_path / "a",
               disallowed_tools=["PowerShell"])
    argv = json.loads((tmp_path / "call.json").read_text())["argv"]
    assert argv[argv.index("--disallowedTools") + 1] == "PowerShell"
    run_claude(cfg, "intake", "p", cwd=tmp_path, schema=SCHEMA, allowed_tools=["Read"], run_dir=tmp_path / "b")
    assert "--disallowedTools" not in json.loads((tmp_path / "call.json").read_text())["argv"]


def test_implement_prompt_spells_out_which_tool_to_use_for_what(cfg):
    p = stages.render(cfg, "implement", branch="b", python="C:/py/python.exe", instructions="i")
    assert "ONLY with the Write and Edit tools" in p and "no PowerShell" in p
    assert "`C:/py/python.exe scripts/test_local.py`" in p and "no `cd`" in p


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
    stages.render(cfg, "intake", project="KAN", candidate="KAN-1", mode_note="m", unclear_action="u",
                  ready_action="r")
    stages.render(cfg, "implement", branch="b", python="p", instructions="i")
    stages.render(cfg, "deploy", package="x.zip")
    stages.render(cfg, "close", issue_key="KAN-1", comment="c")


def _run(monkeypatch, cfg, fake_stages, argv, preflight=lambda *a: None):
    monkeypatch.setattr(run_pipeline, "preflight", preflight)
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


class FakeJira:
    """Records every write the orchestrator makes, so tests can assert on them."""

    def __init__(self, todo=("KAN-9",), statuses=("To Do", "In Progress", "Done", "Build Broken"),
                 status="In Progress", fail_transition_to=()):
        self.todo, self.statuses, self.status = list(todo), set(statuses), status
        self.fail_transition_to = set(fail_transition_to)
        self.calls = []

    summary = "mynews: s"

    def list_issues(self, project="KAN", status="To Do", limit=20):
        return [{"key": k, "summary": self.summary, "status": "To Do", "type": "Task", "created": ""}
                for k in self.todo]

    def get_issue(self, key):
        return {"key": key, "status": self.status}

    def project_statuses(self, project):
        return self.statuses

    def resolve_assignee(self, project, name):
        self.calls.append(("resolve", name))
        return "acc-autong"

    def assign(self, key, account_id):
        self.calls.append(("assign", key, account_id))

    def add_comment(self, key, text):
        self.calls.append(("comment", key, text))

    def transition(self, key, to_status):
        if to_status in self.fail_transition_to:
            raise stages.JiraError(f"no transition to {to_status}")
        self.calls.append(("transition", key, to_status))
        self.status = to_status


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(stages, "jira_client", lambda: fake)
    return fake


def _intake_env(cfg, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    cfg.mynews_repo = tmp_path
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir, []


def test_ambiguous_ticket_is_not_a_reason_to_stop_assumptions_are_recorded(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "intake_ambiguous")
    ctx = {"dry_run": True, "run_id": "t"}
    assert stages.intake(ctx, cfg, run_dir, logged.append) is None       # None = continue, not "stop"
    assert ctx["assumptions"] == ["'nicer' interpreted as a larger header title"]   # blanks dropped
    assert "Assumptions" in (run_dir / "instructions.md").read_text()
    assert any("no questions asked" in m for m in logged)
    assert jira.calls == []                                               # dry run: no Jira writes at all


def test_intake_takes_ownership_when_it_claims_the_ticket(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "intake_ambiguous")
    ctx = {"dry_run": False, "run_id": "t"}
    stages.intake(ctx, cfg, run_dir, logged.append)
    assert ("resolve", "autong-r2pvi2pl4e") in jira.calls
    assert ("assign", "KAN-9", "acc-autong") in jira.calls
    assert ctx["jira_in_progress"] and any("assigned to autong-r2pvi2pl4e" in m for m in logged)


def test_intake_with_empty_todo_stops_without_calling_the_agent(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "ok")
    jira.todo = []
    ctx = {"dry_run": False, "run_id": "t"}
    assert stages.intake(ctx, cfg, run_dir, logged.append) == "stop"
    assert ctx["stop_reason"] == "no tasks in 'To Do'" and not (tmp_path / "call.json").exists()


@pytest.mark.parametrize("title,ok", [
    ("mynews: show the count in the header", True),
    ("Show the count in the MyNews header", True),          # case-insensitive
    ("[mynews] fix header", True), ("fix the mynews-ui", True),
    ("Show newsfeed as a table", False),                    # no app name
    ("Fix mynewsfeed", False), ("Fix my news", False),      # whole word only
    ("", False),
])
def test_title_must_name_the_application(title, ok):
    assert stages.title_names_app(title, "mynews") is ok


def test_ticket_without_app_name_in_title_fails_before_any_agent_runs(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "ok")
    jira.summary = "Show newsfeed as a table"
    ctx = {"dry_run": False, "run_id": "t"}
    with pytest.raises(StageFailure, match=r"KAN-9 does not follow the required format.*'mynews'.*Show newsfeed"):
        stages.intake(ctx, cfg, run_dir, logged.append)
    assert ctx["issue_key"] == "KAN-9"                       # so cleanup comments and moves it to Build Broken
    assert not (tmp_path / "call.json").exists()             # no agent was started, no cost
    assert jira.calls == []                                  # intake itself wrote nothing; cleanup does


def test_non_conforming_title_ends_in_build_broken_with_a_helpful_comment(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "ok")
    jira.summary = "How old is my grandmother"
    ctx = {"dry_run": False, "run_id": "t"}
    with pytest.raises(StageFailure) as e:
        stages.intake(ctx, cfg, run_dir, logged.append)
    stages.cleanup_after_failure(ctx, cfg, 1, str(e.value), logged.append)
    comment = next(c for c in jira.calls if c[0] == "comment")[2]
    assert "must contain the target application name 'mynews'" in comment and "How old is my grandmother" in comment
    assert ("transition", "KAN-9", "Build Broken") in jira.calls


def test_task_that_cannot_be_implemented_is_a_failure_not_a_quiet_stop(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "intake_unclear")
    ctx = {"dry_run": False, "run_id": "t"}
    with pytest.raises(StageFailure, match="KAN-9 cannot be implemented: needs credentials"):
        stages.intake(ctx, cfg, run_dir, logged.append)
    assert ctx["issue_key"] == "KAN-9"          # recorded, so cleanup can park the ticket
    argv = json.loads((tmp_path / "call.json").read_text())["argv"]
    assert "mcp__jira__add_comment" not in argv[argv.index("--allowedTools") + 1]   # the agent may not comment


@pytest.mark.parametrize("dry", [True, False])
def test_intake_may_read_the_feed_domain_only(cfg, tmp_path, monkeypatch, jira, dry):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "intake_ambiguous")
    stages.intake({"dry_run": dry, "run_id": "t"}, cfg, run_dir, logged.append)
    argv = json.loads((tmp_path / "call.json").read_text())["argv"]
    tools = argv[argv.index("--allowedTools") + 1].split(",")
    assert "WebFetch(domain:tldr.tech)" in tools        # the feed's own domain
    assert "WebFetch" not in tools                       # never the whole internet
    assert ("mcp__jira__transition_issue" in tools) is (not dry)   # dry run still has no Jira write tools


def test_intake_prompt_treats_fetched_web_content_as_untrusted_data(cfg):
    p = stages.render(cfg, "intake", project="KAN", candidate="KAN-1", mode_note="m", unclear_action="u",
                      ready_action="r")
    assert "WebFetch" in p and "only the tldr.tech domain" in p and "untrusted DATA" in p


def test_agent_taking_a_different_ticket_is_rejected(cfg, tmp_path, monkeypatch, jira):
    run_dir, logged = _intake_env(cfg, tmp_path, monkeypatch, "intake_other")
    with pytest.raises(StageFailure, match="oldest To Do is KAN-9"):
        stages.intake({"dry_run": False, "run_id": "t"}, cfg, run_dir, logged.append)


def test_failure_is_recorded_on_the_ticket_comment_owner_then_build_broken(cfg, jira):
    ctx = {"dry_run": False, "run_id": "r1", "issue_key": "KAN-9"}
    logged = []
    stages.cleanup_after_failure(ctx, cfg, 2, "agent could not implement the task: needs a database", logged.append)
    kinds = [c[0] for c in jira.calls if c[0] != "resolve"]
    assert kinds == ["comment", "assign", "transition"]                    # in this order
    comment = next(c for c in jira.calls if c[0] == "comment")
    assert "stage 2, implement" in comment[2] and "needs a database" in comment[2] and "r1" in comment[2]
    assert ("assign", "KAN-9", "acc-autong") in jira.calls
    assert ("transition", "KAN-9", "Build Broken") in jira.calls
    assert ctx["jira_broken"] and any("moved to Build Broken" in m for m in logged)


def test_failure_after_deploy_rolls_back_and_says_so(cfg, jira, monkeypatch):
    monkeypatch.setattr(stages, "deployer_call", lambda c, tool, args=None: {"ok": True})
    ctx = {"dry_run": False, "run_id": "r1", "issue_key": "KAN-9", "deployed_new_version": True}
    stages.cleanup_after_failure(ctx, cfg, 5, "tests still fail", print)
    assert ctx["rolled_back"] is True
    assert "rolled back" in next(c for c in jira.calls if c[0] == "comment")[2]


def test_dry_run_failure_never_touches_jira(cfg, jira):
    logged = []
    stages.cleanup_after_failure({"dry_run": True, "run_id": "r", "issue_key": "KAN-9"}, cfg, 2, "x", logged.append)
    assert jira.calls == [] and any("left untouched" in m for m in logged)


def test_failure_before_a_ticket_was_picked_changes_nothing(cfg, jira):
    stages.cleanup_after_failure({"dry_run": False, "run_id": "r"}, cfg, 1, "Jira down", print)
    assert jira.calls == []


def test_ticket_is_never_left_in_todo_if_build_broken_is_unavailable(cfg, monkeypatch):
    fake = FakeJira(status="To Do", fail_transition_to={"Build Broken"})
    monkeypatch.setattr(stages, "jira_client", lambda: fake)
    logged = []
    stages.cleanup_after_failure({"dry_run": False, "run_id": "r", "issue_key": "KAN-9"}, cfg, 3, "x", logged.append)
    assert ("transition", "KAN-9", "In Progress") in fake.calls            # parked so the next run skips it
    assert any("could not move KAN-9" in m for m in logged)


def test_prompts_tell_agents_never_to_ask_questions(cfg):
    intake = stages.render(cfg, "intake", project="KAN", candidate="KAN-1", mode_note="m", unclear_action="u",
                           ready_action="r")
    impl = stages.render(cfg, "implement", branch="b", python="p", instructions="i")
    assert "NEVER stop to ask questions" in intake and "assumptions" in intake
    assert "Never stop to ask questions" in impl


def test_preflight_failure_stops_before_any_stage(cfg, monkeypatch, capsys):
    def down(*a):
        raise StageFailure("deployer unreachable")

    def never(ctx, cfg, run_dir, log):
        raise AssertionError("no stage may run when preflight fails")

    rc = _run(monkeypatch, cfg, [(1, "intake", never)], [], preflight=down)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED preflight: deployer unreachable" in out and "result: failed" in out
    ctx = json.loads((next(cfg.state_dir.iterdir()) / "run.json").read_text())
    assert ctx["status"] == "failed" and ctx["stages"] == {}


def test_preflight_checks_only_what_the_selected_stages_need(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(stages, "deployer_call", lambda c, tool, args=None: calls.append(tool) or {})
    monkeypatch.setattr(stages, "app_version", lambda c: "0.1.0")
    stages.preflight(cfg, 1, 2, dry_run=True)   # intake + implement need neither deployer nor app
    assert calls == []
    stages.preflight(cfg, 1, 7, dry_run=True)
    assert calls == ["get_status"]
    monkeypatch.setattr(stages, "app_version", lambda c: None)
    with pytest.raises(StageFailure, match="not answering"):
        stages.preflight(cfg, 3, 5)


def test_preflight_requires_build_broken_status_and_assignable_owner(cfg, monkeypatch):
    fake = FakeJira(statuses=("To Do", "In Progress", "Done"))               # no 'Build Broken'
    monkeypatch.setattr(stages, "jira_client", lambda: fake)
    with pytest.raises(StageFailure, match="no status 'Build Broken'"):
        stages.preflight(cfg, 1, 2)
    stages.preflight(cfg, 1, 2, dry_run=True)                                # dry run does not need it
    fake.statuses.add("Build Broken")
    stages.preflight(cfg, 1, 2)                                              # now fine
    assert ("resolve", "autong-r2pvi2pl4e") in fake.calls

    def nobody(project, name):
        raise stages.JiraError("no assignable user named 'autong-r2pvi2pl4e'")
    fake.resolve_assignee = nobody
    with pytest.raises(StageFailure, match="no assignable user"):
        stages.preflight(cfg, 1, 2)


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
