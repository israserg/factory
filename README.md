# factory

Orchestration for the "task to production" training project: Jira task -> Claude agent -> patch -> deploy via MCP -> test -> commit -> Jira Done.

- `jira-mcp/`: stdio MCP server wrapping Jira REST
- `mcp-deployer/`: long-running HTTP MCP server that patches and restarts `mynews`
- `orchestrator/`: the pipeline driving `claude -p`
- `docs/PLAN.md`: the full plan

The app itself lives in the separate `mynews` repo.
