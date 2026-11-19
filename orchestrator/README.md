# orchestrator

Runs the "task to production" pipeline. The order of stages is plain code; the work inside four of them is done by Claude agents (`claude -p`) with narrow tool allow-lists.

| # | Stage | Who | What |
|---|---|---|---|
| 1 | intake | agent + Jira MCP | take the oldest To Do issue, move it to In Progress, write `instructions.md` (or comment and stop if unclear) |
| 2 | implement | agent (edit tools) | fresh clone of mynews, branch `task/KEY`, implement, add tests, bump VERSION, build the package |
| 3 | test_before | code | run tests against the running OLD app; they must fail (only the new tests) |
| 4 | deploy | agent + deployer MCP | `apply_patch`, then we verify `/version` ourselves |
| 5 | test_after | code | same tests must pass; otherwise roll back the app |
| 6 | commit | code | commit on the task branch, fast-forward `main`, push both |
| 7 | close | agent + Jira MCP | comment with commit and test results, move to Done |

On failure after stage 1: the app is rolled back if the new version is live and tests failed (stage 5), and the Jira issue gets an explanatory comment and returns to To Do.

## Prerequisites
- `factory/.env` with `JIRA_*` and `DEPLOYER_TOKEN`
- deployer running: `powershell scripts\start-deployer.ps1`
- a baseline mynews deployed and running on :8000 (stage 3 needs it)
- mynews `origin` reachable (stage 2 clones it; stage 6 pushes to it)

## Usage
```
.venv\Scripts\python orchestrator\run_pipeline.py --dry-run                 # all stages, no Jira writes, no push
.venv\Scripts\python orchestrator\run_pipeline.py                           # full run
.venv\Scripts\python orchestrator\run_pipeline.py --run-id X --only 3       # re-run one stage from saved state
.venv\Scripts\python orchestrator\run_pipeline.py --run-id X --from 4       # continue from a stage
.venv\Scripts\python orchestrator\run_pipeline.py --no-push                 # commit locally only
```
Limits per agent stage (turns, USD budget, timeout) are in `config.py`.

## Run state (`orchestrator/state/<run-id>/`, git-ignored)
`run.json` (context + per-stage status/cost), `pipeline.log`, `instructions.md`, `work/mynews/` (the clone), `test_before.txt`, `test_after.txt`, and per agent stage a folder with `prompt.md`, `mcp.json` (may contain a token), `claude.stdout.json`, `result.json`.

## Test
```
.venv\Scripts\python -m pytest orchestrator     # offline, uses fake_claude.py instead of the real CLI
```
