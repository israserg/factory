"""Thin Jira Cloud REST client (v3) with just what the pipeline needs. No MCP code here."""
from __future__ import annotations

import re

import httpx

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


class JiraError(Exception):
    pass


def adf_to_text(node) -> str:
    """Convert Atlassian Document Format (Jira's rich text JSON) to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    kids = node.get("content", [])
    inner = "".join(adf_to_text(k) for k in kids)
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t in ("paragraph", "heading"):
        return inner + "\n"
    if t == "codeBlock":
        return "```\n" + inner + "\n```\n"
    if t == "listItem":
        return "- " + inner.strip() + "\n"
    if t == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    if t == "mention":
        return node.get("attrs", {}).get("text", "")
    return inner  # doc, bulletList, orderedList, blockquote, panel, ...


def text_to_adf(text: str) -> dict:
    """Plain text -> ADF; blank lines separate paragraphs, single newlines become hard breaks."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        content = []
        for i, line in enumerate(block.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        if content:
            paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph", "content": []}]}


class Jira:
    def __init__(self, site: str, email: str, token: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=site.rstrip("/") + "/rest/api/3",
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    def _req(self, method: str, path: str, **kw):
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = "; ".join(body.get("errorMessages", []) + list(body.get("errors", {}).values()))
            except Exception:
                msg = r.text[:200]
            raise JiraError(f"Jira {r.status_code} on {method} {path}: {msg or 'no details'}")
        return r.json() if r.content else {}

    @staticmethod
    def _check_key(key: str) -> str:
        key = key.strip().upper()
        if not KEY_RE.match(key):
            raise JiraError(f"invalid issue key: {key!r} (expected like KAN-4)")
        return key

    def list_issues(self, project: str = "KAN", status: str = "To Do", limit: int = 20) -> list[dict]:
        project = project.strip().upper()
        if not PROJECT_RE.match(project):
            raise JiraError(f"invalid project key: {project!r}")
        status = status.replace("\\", "\\\\").replace('"', '\\"')
        jql = f'project = {project} AND status = "{status}" ORDER BY created ASC'
        data = self._req("GET", "/search/jql", params={
            "jql": jql, "maxResults": max(1, min(limit, 100)),
            "fields": "summary,status,issuetype,created",
        })
        return [
            {"key": i["key"], "summary": i["fields"]["summary"],
             "status": i["fields"]["status"]["name"], "type": i["fields"]["issuetype"]["name"],
             "created": i["fields"]["created"]}
            for i in data.get("issues", [])
        ]

    def get_issue(self, key: str) -> dict:
        key = self._check_key(key)
        d = self._req("GET", f"/issue/{key}", params={
            "fields": "summary,status,description,issuetype,labels,parent,comment"})
        f = d["fields"]
        comments = [
            {"author": c["author"]["displayName"], "created": c["created"], "text": adf_to_text(c["body"]).strip()}
            for c in f.get("comment", {}).get("comments", [])
        ]
        return {
            "key": d["key"],
            "summary": f["summary"],
            "status": f["status"]["name"],
            "type": f["issuetype"]["name"],
            "labels": f.get("labels", []),
            "parent": (f.get("parent") or {}).get("key"),
            "description": adf_to_text(f.get("description")).strip(),
            "comments": comments,
            "available_transitions": [t["name"] for t in self.transitions(key)],
        }

    def transitions(self, key: str) -> list[dict]:
        key = self._check_key(key)
        data = self._req("GET", f"/issue/{key}/transitions")
        return [{"id": t["id"], "name": t["name"], "to": t["to"]["name"]} for t in data["transitions"]]

    def transition(self, key: str, to_status: str) -> dict:
        key = self._check_key(key)
        before = self._req("GET", f"/issue/{key}", params={"fields": "status"})["fields"]["status"]["name"]
        wanted = to_status.strip().lower()
        options = self.transitions(key)
        match = next((t for t in options if t["name"].lower() == wanted or t["to"].lower() == wanted), None)
        if match is None:
            raise JiraError(f"{key} is '{before}'; no transition to '{to_status}'. "
                            f"Available: {[t['name'] for t in options]}")
        self._req("POST", f"/issue/{key}/transitions", json={"transition": {"id": match["id"]}})
        after = self._req("GET", f"/issue/{key}", params={"fields": "status"})["fields"]["status"]["name"]
        return {"key": key, "from": before, "to": after}

    def add_comment(self, key: str, text: str) -> dict:
        key = self._check_key(key)
        if not text.strip():
            raise JiraError("comment text is empty")
        d = self._req("POST", f"/issue/{key}/comment", json={"body": text_to_adf(text)})
        return {"key": key, "comment_id": d["id"]}
