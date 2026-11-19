# jira-mcp

stdio MCP server that wraps the Jira Cloud REST API for the pipeline. It is not a service: the Claude CLI starts it on demand and it exits with the session.

## Config
`JIRA_SITE`, `JIRA_EMAIL`, `JIRA_TOKEN` in `factory/.env` (git-ignored) or the environment. Use a classic API token from https://id.atlassian.com/manage-profile/security/api-tokens (it ends with `=XXXXXXXX`).

## Tools
| Tool | What it does |
|---|---|
| `list_todo(project="KAN", status="To Do", limit=20)` | issues in a status, oldest first |
| `get_issue(key)` | summary, status, type, labels, description (plain text), comments, available transitions |
| `transition_issue(key, to_status)` | move by name (`In Progress`, `Done`, ...); errors list the available transitions |
| `add_comment(key, text)` | plain-text comment |

## Use from Claude
MCP config (no secrets needed):
```json
{"mcpServers":{"jira":{"type":"stdio","command":"C:/mywork/factory/.venv/Scripts/python.exe","args":["C:/mywork/factory/jira-mcp/server.py"]}}}
```
```
claude -p "..." --mcp-config jira.json --strict-mcp-config --allowedTools "mcp__jira__list_todo,mcp__jira__get_issue"
```

## Test
```
.venv\Scripts\python -m pytest jira-mcp            # offline unit tests
set JIRA_LIVE=1 & .venv\Scripts\python -m pytest jira-mcp   # + live KAN-4 round trip (restores its status)
```
