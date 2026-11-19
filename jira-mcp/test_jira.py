"""Unit tests use a mocked HTTP transport (no network). The live test hits real Jira only with JIRA_LIVE=1."""
import json
import os

import httpx
import pytest

from jira_client import Jira, JiraError, adf_to_text, text_to_adf


def make_jira(handler) -> Jira:
    return Jira("https://x.atlassian.net", "me@example.com", "tok", transport=httpx.MockTransport(handler))


def test_adf_to_text():
    adf = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Add a footer"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
        ]},
    ]}
    assert adf_to_text(adf).strip() == "Add a footer\n- one\n- two"
    assert adf_to_text(None) == ""


def test_text_to_adf_paragraphs_and_breaks():
    adf = text_to_adf("line1\nline2\n\nsecond paragraph")
    assert len(adf["content"]) == 2
    assert adf["content"][0]["content"][1] == {"type": "hardBreak"}
    assert adf_to_text(adf).strip() == "line1\nline2\n\nsecond paragraph".replace("\n\n", "\n")


def test_key_validation():
    j = make_jira(lambda r: httpx.Response(500))
    for bad in ("KAN4", "kan-x", "KAN-4; DROP", ""):
        with pytest.raises(JiraError):
            j.get_issue(bad)


def test_list_issues_builds_safe_jql_and_maps_fields():
    seen = {}

    def handler(req):
        seen["jql"] = req.url.params["jql"]
        return httpx.Response(200, json={"issues": [{"key": "KAN-4", "fields": {
            "summary": "First Task", "status": {"name": "To Do"},
            "issuetype": {"name": "Task"}, "created": "2026-11-19T10:00:00.000+0000"}}]})

    j = make_jira(handler)
    out = j.list_issues("kan", 'To Do" OR 1=1 --')
    assert seen["jql"].startswith('project = KAN AND status = "To Do\\" OR 1=1 --"')
    assert out[0]["key"] == "KAN-4" and out[0]["status"] == "To Do"
    with pytest.raises(JiraError):
        j.list_issues("KAN OR project = X")


def test_transition_resolves_by_name_case_insensitive_and_reports_states():
    state = {"status": "To Do", "posted": None}

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": [
                {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}}]})
        if req.method == "POST":
            state["posted"] = json.loads(req.content)["transition"]["id"]
            state["status"] = {"21": "In Progress", "31": "Done"}[state["posted"]]
            return httpx.Response(204)
        return httpx.Response(200, json={"fields": {"status": {"name": state["status"]}}})

    j = make_jira(handler)
    r = j.transition("kan-4", "in progress")
    assert state["posted"] == "21" and r == {"key": "KAN-4", "from": "To Do", "to": "In Progress"}


def test_transition_unknown_status_lists_options():
    def handler(req):
        if req.url.path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": [
                {"id": "31", "name": "Done", "to": {"name": "Done"}}]})
        return httpx.Response(200, json={"fields": {"status": {"name": "To Do"}}})

    with pytest.raises(JiraError, match=r"Available: \['Done'\]"):
        make_jira(handler).transition("KAN-4", "Shipped")


def test_http_errors_become_jira_errors():
    j = make_jira(lambda r: httpx.Response(404, json={"errorMessages": ["Issue does not exist"], "errors": {}}))
    with pytest.raises(JiraError, match="404.*Issue does not exist"):
        j.get_issue("KAN-999")


def test_comment_rejects_empty():
    with pytest.raises(JiraError):
        make_jira(lambda r: httpx.Response(500)).add_comment("KAN-4", "   ")


@pytest.mark.skipif(os.environ.get("JIRA_LIVE") != "1", reason="set JIRA_LIVE=1 to run against real Jira")
def test_live_roundtrip_on_kan4():
    """Moves KAN-4 To Do -> In Progress -> To Do, adds a comment; always restores the original status."""
    j = Jira(os.environ["JIRA_SITE"], os.environ["JIRA_EMAIL"], os.environ["JIRA_TOKEN"])
    original = j.get_issue("KAN-4")["status"]
    try:
        assert any(i["key"] == "KAN-4" for i in j.list_issues("KAN", original))
        assert j.transition("KAN-4", "In Progress")["to"] == "In Progress"
        assert j.get_issue("KAN-4")["status"] == "In Progress"
        c = j.add_comment("KAN-4", "jira-mcp live test\n\nsecond paragraph")
        assert c["comment_id"]
        assert "jira-mcp live test" in j.get_issue("KAN-4")["comments"][-1]["text"]
    finally:
        j.transition("KAN-4", original)
    assert j.get_issue("KAN-4")["status"] == original
