# mcp-deployer

Long-running MCP server (streamable HTTP) that patches and restarts the local `mynews` app.

## Run
```
powershell scripts\start-deployer.ps1     # background, http://127.0.0.1:8765/mcp, logs in C:\mywork\runtime
powershell scripts\stop-deployer.ps1      # stops only the deployer; mynews keeps running
```
Needs `DEPLOYER_TOKEN` in `factory/.env` (git-ignored). Clients send `Authorization: Bearer <token>`.

## Tools
| Tool | What it does |
|---|---|
| `get_status` | running?, pid, running/deployed version, backups |
| `apply_patch(package_path)` | validate zip, install deps (before downtime), stop app, back up `current/`, swap code, start, check `/health` + `/version`; auto-rollback on failure |
| `restart` | restart the deployed version |
| `rollback` | restore the most recent backup and restart |

Packages are accepted only from `C:\mywork\mynews\dist` (override `DEPLOYER_PACKAGE_DIR`), must contain `app/main.py`, `VERSION`, `requirements.txt`, and may not contain absolute or `..` paths.

## Runtime layout (`C:\mywork\runtime\mynews`, override `DEPLOYER_RUNTIME`)
`current/` live code, `backups/` previous versions (`failed-*`, `rolledback-*` are kept for inspection), `staging/`, `run/` (pid, log), `venv/`.

## Test
```
.venv\Scripts\python -m pytest mcp-deployer      # uses a temp runtime and port 18000
```

## Use from Claude
```
claude -p "..." --mcp-config <file> --strict-mcp-config --allowedTools "mcp__deployer__get_status"
```
where the config file has `{"mcpServers":{"deployer":{"type":"http","url":"http://127.0.0.1:8765/mcp","headers":{"Authorization":"Bearer <token>"}}}}`.
Generate that file at run time (the orchestrator will), never commit it.
