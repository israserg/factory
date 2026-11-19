"""Jira MCP server (stdio, started on demand by the Claude CLI).

Credentials come from the environment or factory/.env: JIRA_SITE, JIRA_EMAIL, JIRA_TOKEN.
IMPORTANT: stdio transport uses stdout for the protocol, so never print() here; log to stderr.
"""
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from jira_client import Jira


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

mcp = MCPServer(
    "jira",
    instructions=("Jira access for one Jira Cloud site. Statuses on the board: 'To Do', 'In Progress', 'Done'. "
                  "Typical flow: list_todo -> get_issue -> transition_issue(key, 'In Progress') -> ... -> "
                  "add_comment -> transition_issue(key, 'Done')."),
)

_jira: Jira | None = None


def jira() -> Jira:
    global _jira
    if _jira is None:
        missing = [k for k in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_TOKEN") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)} (set them in factory/.env)")
        _jira = Jira(os.environ["JIRA_SITE"], os.environ["JIRA_EMAIL"], os.environ["JIRA_TOKEN"])
    return _jira


@mcp.tool()
def list_todo(project: str = "KAN", status: str = "To Do", limit: int = 20) -> list[dict]:
    """List issues of a project in the given status (default: project KAN, status 'To Do'), oldest first."""
    return jira().list_issues(project, status, limit)


@mcp.tool()
def get_issue(key: str) -> dict:
    """Get one issue: summary, status, type, labels, description (plain text), comments and the
    transitions currently available. Example key: KAN-4."""
    return jira().get_issue(key)


@mcp.tool()
def transition_issue(key: str, to_status: str) -> dict:
    """Move an issue to another status by name, e.g. 'In Progress' or 'Done'. Returns the old and new status.
    Fails with the list of available transitions if the move is not possible."""
    return jira().transition(key, to_status)


@mcp.tool()
def add_comment(key: str, text: str) -> dict:
    """Add a plain-text comment to an issue (blank line = new paragraph)."""
    return jira().add_comment(key, text)


if __name__ == "__main__":
    print("jira MCP server starting (stdio)", file=sys.stderr)
    mcp.run("stdio")
