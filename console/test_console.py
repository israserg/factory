"""Offline tests for the run console: the real pipeline is replaced by fake_pipeline.py."""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as console

HERE = Path(__file__).parent
HOST = {"host": "127.0.0.1:8800"}
POST = {**HOST, "x-console": "1"}


def make(tmp_path):
    runner = console.Runner(lambda dry: [sys.executable, "-u", str(HERE / "fake_pipeline.py")]
                            + (["--dry-run"] if dry else []), HERE)
    return runner, TestClient(console.create_app(runner), base_url="http://127.0.0.1:8800")


def wait_finished(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get("/api/state", headers=HOST).json()
        if s["status"] != "running":
            return s
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_translate_gives_friendly_messages():
    t = console.translate
    assert t("[12:00:00] --- stage 1: intake") == {
        "ts": "12:00:00", "level": "step", "text": "[1/7] Picking the ticket from Jira and analysing it…"}
    assert t("[12:00:01] --- stage 3: test_before")["text"].startswith("[3/7] Running tests against the current app")
    assert t("[12:00:02] task KAN-5: Show count")["text"] == "Picked ticket KAN-5: Show count"
    assert "2 failed (expected), 4 passed" in t("[12:00:03] tests before: exit 1, 2 failed, 4 passed")["text"]
    bad = t("[12:00:04] tests after: exit 1, 1 failed, 5 passed")
    assert bad["level"] == "error"
    assert t("[12:00:05] FAILED at stage 5 (x): boom")["level"] == "error"
    assert t("plain traceback line")["level"] == "info"   # lines without a timestamp pass through


def test_full_run_streams_friendly_log_and_ends_done(tmp_path):
    runner, c = make(tmp_path)
    assert c.post("/api/run", headers=POST, json={}).status_code == 202
    s = wait_finished(c)
    assert s["status"] == "done"
    texts = [l["text"] for l in s["lines"]]
    assert any(t.startswith("[1/7] Picking the ticket") for t in texts)
    assert any(t.startswith("[7/7] Updating Jira") for t in texts)
    assert "Picked ticket KAN-7: Do the thing" in texts
    assert texts[-1] == "Finished successfully."


@pytest.mark.parametrize("mode,status,last", [("stopped", "stopped", "Finished: nothing to do."),
                                              ("failed", "failed", "Finished with errors."),
                                              ("crash", "failed", "Finished with errors.")])
def test_end_states(tmp_path, monkeypatch, mode, status, last):
    monkeypatch.setenv("FAKE_PIPELINE_MODE", mode)
    runner, c = make(tmp_path)
    c.post("/api/run", headers=POST, json={})
    s = wait_finished(c)
    assert s["status"] == status and s["lines"][-1]["text"] == last
    if mode == "crash":
        assert any("exited unexpectedly" in l["text"] for l in s["lines"])


def test_second_run_while_running_is_rejected_and_new_run_clears_log(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PIPELINE_DELAY", "0.3")
    runner, c = make(tmp_path)
    assert c.post("/api/run", headers=POST, json={}).status_code == 202
    assert c.post("/api/run", headers=POST, json={}).status_code == 409
    first_gen = wait_finished(c, 30)["gen"]
    c.post("/api/run", headers=POST, json={"dry_run": True})
    snap = c.get(f"/api/state?since=999&gen={first_gen}", headers=HOST).json()
    assert snap["reset"] is True and snap["gen"] == first_gen + 1      # client told to clear its log
    assert snap["lines"][0]["text"].startswith("Starting the pipeline (dry run")
    wait_finished(c, 30)


def test_dry_run_flag_reaches_the_pipeline(tmp_path):
    runner, c = make(tmp_path)
    c.post("/api/run", headers=POST, json={"dry_run": True})
    s = wait_finished(c)
    assert any("dry_run=True" in l["text"] for l in s["lines"])


def test_incremental_polling_returns_only_new_lines(tmp_path):
    runner, c = make(tmp_path)
    c.post("/api/run", headers=POST, json={})
    s = wait_finished(c)
    again = c.get(f"/api/state?since={s['next']}&gen={s['gen']}", headers=HOST).json()
    assert again["lines"] == [] and again["reset"] is False


def test_guards_block_foreign_pages_and_dns_rebinding(tmp_path):
    runner, c = make(tmp_path)
    assert c.post("/api/run", headers=HOST, json={}).status_code == 403                  # no X-Console header
    assert c.post("/api/run", headers={**POST, "host": "evil.example:8800"}, json={}).status_code == 403
    assert c.get("/api/state", headers={"host": "evil.example"}).status_code == 403
    assert runner.status == "idle"                                                        # nothing started
    assert c.get("/", headers=HOST).status_code == 200
