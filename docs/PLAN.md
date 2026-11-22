# Plan: "Task to Production" with Claude agents + MCP (learning project)

Personal education project. Local only, no production concerns. Goal: learn Claude CLI (headless), MCP servers/clients, and agent orchestration.

## 1. Tech choices (keep it simple)

| Part | Choice | Why |
|---|---|---|
| Language (everything) | **Python 3.12** | One language for app, MCP server, orchestrator. Official MCP SDK and Claude Agent SDK are both Python-friendly. |
| `mynews` backend | **FastAPI** + `uvicorn`, `feedparser`, `httpx` | Tiny, readable, auto-docs at `/docs`. |
| `mynews` frontend | **One static HTML page + vanilla JS** (served by FastAPI) | No Node, no build step, nothing to break. |
| `mynews` "engine"/storage | **No DB.** In-memory cache with a short TTL (e.g. 15 min) | Nothing to persist; keeps the patch/restart flow trivial. |
| News source | TLDR AI RSS feed (`https://tldr.tech/api/rss/ai`), fallback: any AI RSS feed | RSS is stable and easy to parse. Brief = title + first N chars of summary + link. |
| `mynews` tests | **pytest + httpx**, black-box over HTTP against `BASE_URL` | Lets the same test run against the *running* app (steps 3 and 5). |
| MCP server | Python **`mcp` SDK (FastMCP)**, streamable-HTTP or stdio transport | Fewest moving parts. |
| Orchestrator | Python script driving **`claude -p`** (headless CLI), `--output-format json`, `--mcp-config`, `--allowedTools` | Matches your goal of using the Claude CLI + MCP directly. |
| Jira access | **Custom Jira MCP server** (Python, **stdio**, on-demand), personal API token via `.env` | Atlassian's MCP rejects API tokens unless an org admin enables it. Writing our own is a good MCP exercise. Verified working: Basic auth `email:token` against `https://israserg.atlassian.net/rest/api/3/...`. |

## 2. Repositories and layout

Two separate git repos under `c:\mywork`:

```
c:\mywork\
├── PLAN.md                      (this file, not in a repo)
├── mynews\                      REPO 1: the application
│   ├── app\
│   │   ├── main.py              FastAPI app: GET /, GET /api/news, GET /health, GET /version
│   │   ├── feed.py              fetch + parse + brief
│   │   └── static\index.html    UI (vanilla JS)
│   ├── tests\                   pytest, black-box, reads BASE_URL env var
│   ├── scripts\package.py       builds dist\mynews-<version>.zip (the "deployment package")
│   ├── requirements.txt
│   └── README.md
└── factory\                     REPO 2: everything else
    ├── jira-mcp\                MCP server (stdio) wrapping Jira REST: list_todo, get_issue, transition, comment
    ├── mcp-deployer\            MCP server (HTTP, long-running) "in front of" mynews
    │   ├── server.py            tools: get_status, apply_patch, restart, rollback
    │   └── README.md
    ├── orchestrator\
    │   ├── run_pipeline.py      the 7-step pipeline
    │   ├── prompts\             one prompt template per agent stage
    │   ├── mcp\                 mcp config json files (jira, deployer)
    │   └── state\               per-run logs/artifacts (gitignored)
    ├── docs\                    notes, learnings, diagrams
    └── README.md
```

Runtime folders (not in git): `c:\mywork\runtime\mynews\current\` (live app), `...\backups\`, `...\run\` (pid, logs).

## 3. Component designs

### 3.1 `mynews` (REPO 1)
- `GET /api/news` returns `[{title, brief, link, published}]`, cached in memory.
- `GET /health` returns `{"status":"ok"}`. `GET /version` returns the version string from a `VERSION` file inside the package (lets tests and the deployer prove which build is running).
- `scripts/package.py` zips `app/`, `requirements.txt`, `VERSION` into `dist/mynews-<version>.zip`.
- Runs on `127.0.0.1:8000`.

### 3.2 MCP deployer (REPO 2, `mcp-deployer`)
Tools:
- `get_status()`: is the app running, PID, current version, health result.
- `apply_patch(package_path)`:
  1. validate the zip (exists, contains `app/main.py` and `VERSION`),
  2. stop the app (kill by PID file; Windows: `taskkill /T` or `Popen.terminate`),
  3. back up `current\` to `backups\<timestamp>\`,
  4. extract the zip into `current\`, install deps if `requirements.txt` changed,
  5. start the app (`uvicorn` via `subprocess.Popen`, detached, logs to `run\`),
  6. poll `/health` up to N seconds; on failure auto-rollback and return an error.
- `restart()` and `rollback()` as small helper tools.

Safety for a learning setup: bind to `127.0.0.1` only, only accept packages from the `mynews\dist` folder, no arbitrary paths or shell commands.

Lifecycle: the deployer is a **long-running HTTP service** started and stopped by the human (or `scripts\start-deployer.ps1` / `stop-deployer.ps1`), not by the orchestrator. It starts `mynews` as a detached child process with a PID file, so `mynews` survives a deployer restart. The orchestrator only checks that the deployer is up (preflight) and fails fast if not.

### 3.3 Orchestrator (REPO 2, `orchestrator`)
Python script that runs the steps **in order, deterministically**, and uses Claude agents (`claude -p`) for the steps that need judgement. Each run gets a folder `state\<run-id>\` with each stage's input, output JSON, and logs, so you can inspect what each agent did.

Design principle: the *pipeline order* is plain code; the *work inside a stage* is the agent. Each stage gets a minimal tool allowlist.

| Step | Who does it | Details |
|---|---|---|
| 1. Jira intake | Claude agent + Jira MCP | Allowed tools: Jira MCP only. Fetch first "To Do" issue in KAN, transition to "In Progress", read it, write `instructions.md` (what to change, acceptance criteria, test idea). Output: issue key + instructions. |
| 2. Implement | Claude agent (Read/Edit/Write/Bash/git), no MCP | Works on a branch `task/KAN-123` in `mynews`. Changes code, writes a test covering the change, runs `scripts/package.py`. Output: package path, test path. |
| 3. Test before | Orchestrator code (pytest) | Run new tests with `BASE_URL=http://127.0.0.1:8000` against the running **old** app. **Expect failure**; if it passes, stop the pipeline (test doesn't prove the change). |
| 4. Deploy | Claude agent + deployer MCP (or direct MCP client call) | Call `apply_patch(package_path)`. Start with the agent doing it, since the goal is to learn MCP from the client side. |
| 5. Test after | Orchestrator code (pytest) | Same tests, expect pass. On failure: call `rollback()`, move Jira issue back, stop. |
| 6. Commit | Orchestrator code (git) | Commit on the task branch (and merge to `main` if you want). Message includes the Jira key. |
| 7. Jira done | Claude agent + Jira MCP | Transition "In Progress" to "Done", add a comment with commit hash and test result. |

Headless call shape (for reference):
```
claude -p "<prompt>" --output-format json --mcp-config mcp\jira.json --strict-mcp-config --allowedTools "mcp__atlassian__*" --max-turns 15
```

## 4. Milestones (build in this order, each one is testable alone)

**M0: Setup (30 min)**
- Install Python 3.12, git, Claude CLI. `git init` in `mynews` and `factory`. Create venvs.
- Create 2-3 sample tasks in Jira project KAN (e.g. "Show publish date in UI", "Add /version endpoint", "Limit news to 10 items").

**M1: `mynews` v0.1**
- Feed fetch + brief, API, page, `/health`, `/version`, a couple of baseline tests, `package.py`.
- Deploy it manually into `runtime\mynews\current` and run it.
- Done when: browser shows the AI TLDR brief.

**M2: MCP deployer: DONE**
- Implemented `get_status`, `apply_patch`, `restart`, `rollback` (`factory/mcp-deployer/`), streamable HTTP on `127.0.0.1:8765/mcp`, bearer token in `factory/.env` (`DEPLOYER_TOKEN`).
- Verified: 7 integration tests (first deploy, upgrade + backup, broken package rolls back, crash-at-import rolls back, explicit rollback, rejected bad packages, restart); real deploy of mynews 0.1.0 over MCP; a headless `claude -p` agent calling `get_status` through `--mcp-config`.
- Learnings:
  - The `mcp` Python SDK is v2 here: `from mcp.server.mcpserver import MCPServer` (FastMCP was renamed).
  - Windows: a child started with DETACHED_PROCESS is still a child by parent-pid, so `taskkill /T` on the deployer also killed mynews. `stop-deployer.ps1` kills only the deployer's own processes (matched by command line), not the tree.
  - Dependencies are installed into the runtime venv from a staging copy *before* the running app is stopped, so a bad requirements.txt costs no downtime.
  - Paths in JSON passed through bash `curl` break with backslashes; use forward slashes (`C:/mywork/...`).
  - Claude CLI is at `C:\Users\isras\.local\bin\claude.exe` (not on PATH in the bash tool).

**M3: Custom Jira MCP (stdio): DONE**
- Python script using the `mcp` SDK; spawned by Claude per session (`command` + `args` in the MCP config), reads `JIRA_EMAIL`, `JIRA_TOKEN`, `JIRA_SITE` from the environment/`.env`.
- Tools: `list_todo(project)`, `get_issue(key)`, `transition_issue(key, to_status)`, `add_comment(key, text)`.
- Transition ids on the KAN board (verified): To Do = 11, In Progress = 21, Done = 31. Resolve by name at runtime via `GET /issue/{key}/transitions`.
- Test with MCP Inspector, then interactively in `claude`.
- Done when: Claude can read KAN-4 and move it between statuses.
- Result: `factory/jira-mcp/` (`jira_client.py` REST client, `server.py` MCP wrapper). 8 offline unit tests (mocked HTTP) + 1 live test (`JIRA_LIVE=1`). Verified with a headless `claude -p` agent: listed To Do, read KAN-4, moved it In Progress and back (also checked via REST).
- Learnings:
  - stdio servers must never write to stdout (it is the protocol channel); log to stderr.
  - The server loads `factory/.env` itself, so the MCP config file contains no secrets and can be committed or generated freely.
  - Transitions are resolved by name at runtime (not hard-coded ids), matching either the transition name or the target status.
  - JQL is built from validated project keys and escaped status strings; issue keys are regex-validated.
  - In MCP config JSON, use forward slashes for Windows paths (backslashes get mangled by the shell and are easy to get wrong).
  - A test comment ("jira-mcp live test") was left on KAN-4 by the live test.

**M4: Orchestrator, one stage at a time: DONE** (stages 1-7 implemented; 1-6 verified on a real run, 7 verified in M5)
- `factory/orchestrator/`: `run_pipeline.py` (CLI, state in `state/<run-id>/run.json`), `stages.py` (7 stages), `claude_stage.py` (`claude -p` helper), `config.py`, `prompts/`. Flags: `--only N`, `--from/--to`, `--run-id`, `--dry-run` (no Jira writes, no push), `--no-push`.
- Agent stages: 1 intake (Jira MCP + read-only tools), 2 implement (clone of mynews from GitHub, file edit tools + 2 whitelisted commands), 4 deploy (deployer MCP only), 7 close (Jira MCP only). Plain code: 3, 5 (pytest against the live app), 6 (git commit/merge/push).
- Every agent result is re-verified by code (Jira status, VERSION bump, package contents, HEAD unmoved, `/version` after deploy), not trusted.
- Verified: 11 offline unit tests (fake claude); real dry run on KAN-5: intake produced good instructions, implement made a 3-file change and passing tests, stage 3 correctly failed exactly the 2 new tests on the old app, stage 4 deployed v0.2.0, stage 5 passed, stage 6 committed; failure cleanup rolled the app back to 0.1.0.
- Learnings:
  - `claude -p --json-schema` returns the parsed result in `structured_output`: a clean contract between stages.
  - `--setting-sources project` keeps user-level plugins/hooks out of pipeline agents (a trivial call dropped from ~$0.17 to ~$0.01) and `--strict-mcp-config` limits MCP servers to those we pass.
  - Prompt goes in via stdin; tool allow-lists are per stage (intake dry-run does not even have the write tools).
  - The implementing agent needs a way to verify itself: `mynews/scripts/test_local.py` (start app on a free port, run pytest, stop).
  - Stage 2 clones from GitHub, so helper scripts must be pushed before a run.
  - Agents tend to write tests that hard-code the new version string; prompts now forbid it.
  - Costs reported by the CLI looked very low (~$0.01 per stage); treat as indicative.

**M5: End to end: happy path DONE, failure path still to do**
- Real run `m5-kan5` (2026-11-19): KAN-5 To Do -> In Progress -> Done in ~80 s; mynews 0.1.0 -> 0.2.0 live; commit `9885d1f` "KAN-5: Show the total number of news items in the page header" pushed to `main` and `task/KAN-5`; Jira comment with commit and test results; agent cost reported ~$0.03.
- Observed: implement agent tried the PowerShell tool for the whitelisted python command (denied, it fell back and succeeded); commit body is the agent's long first-person summary (could be shortened); author name comes from the dev repo's git config.
- Still to do: a deliberately failing run (bad patch / test still failing after deploy) to watch rollback + Jira return to To Do end to end; an ambiguous ticket (see the decision below).
- Decision (2026-11-19): agents never stop to ask questions. On ambiguity the intake agent picks the most reasonable/simplest interpretation, records it under **Assumptions** in `instructions.md` and in the `assumptions` field; the close stage writes the assumptions into the Jira comment. `unclear` (comment on the ticket and stop) is reserved for tickets that cannot be implemented at all. The implement agent likewise picks a default and mentions it in its summary.
- Decision (2026-11-20): **failure = stop + comment + status "Build Broken"** (the status was added to the KAN board; reachable from every status). Applies to every failure after a ticket is picked, including "cannot be implemented" from intake (now a failure, not a quiet stop; the intake agent no longer has comment rights, the pipeline writes the comment). The oldest To Do ticket is now identified by code before the agent runs, so any later failure can be recorded on it, and an empty To Do costs no agent call. If the move to Build Broken fails, a ticket still in To Do is parked in In Progress to avoid an endless re-pick loop. Preflight checks the status exists.
- Decision (2026-11-20): **ticket ownership** = the assignable Jira user `autong-r2pvi2pl4e` (resolved by exact display name; override with `JIRA_ASSIGNEE_NAME`), set when the ticket is picked and kept on failure. Note: this is a regular Jira user; the service account `autong` (type app) is a different identity and is not assignable (no Jira product access).
- Decision (2026-11-20): **strict ticket format**: the title must contain the application name "mynews" (whole word, case-insensitive). Checked by code in intake before any agent runs; a non-conforming ticket goes to Build Broken with a comment explaining the rule. Chosen over a prompt/skill-based relevance judgment because it is deterministic and free. Open question observed in the next test: a conforming title with an incomprehensible description (the intake agent may fall back on the title alone).

**M6: Hardening and learning notes**
- Add `--dry-run`, cost/turn limits, better logs, a per-run summary. Write what you learned in `factory\docs\`.

**M7: Run console: DONE**
- `factory/console/`: FastAPI page on `127.0.0.1:8800` with one big RUN button, a dry-run checkbox, deployer/mynews status pills and a progress log. It runs `orchestrator/run_pipeline.py` as a subprocess and translates its log lines into plain messages; one run at a time; a new run clears the log. Start/stop: `scripts/start-console.ps1`, `scripts/stop-console.ps1`.
- Added a pipeline **preflight** (deployer + mynews up) that fails before any Jira change; before, a down deployer was only noticed at stage 4 after the ticket had been moved.
- Safety: localhost only, custom `X-Console` header + Host check against cross-site triggers and DNS rebinding.
- Permission denials in the implement stage (2026-11-20, run 20261120-114121): the agent tried a `cd ... && cat > file <<EOF` Bash command and three PowerShell calls (not on the allow-list), lost ~4 of 12 turns, then recovered; the pipeline's own stage 3/5 tests confirmed the result. Fix: `--disallowedTools PowerShell` for that stage (verified with the real CLI that the tool disappears) and an explicit prompt rule (Write/Edit for files, Bash only in the exact `<python> scripts/...` form, no `cd`, no chaining). To be confirmed on the next real run: no denial warning.
- Intake web access (2026-11-22): run 20261122-103811 (KAN-14) showed the intake agent being denied `WebFetch` on the TLDR feed; it guessed the root cause, the implement stage wrote a test that already passed on the old app, and stage 3 sent the ticket to Build Broken. Fix: intake gets `WebFetch(domain:tldr.tech)` (`feed_domain` in config.py), never plain WebFetch (verified with the real CLI: the feed is readable, example.com is refused). The prompt marks fetched content as untrusted data. The implement stage deliberately has no web access. Not yet confirmed on a real run: KAN-14 must be moved back to To Do.
- Vacuous tests (2026-11-22, KAN-14 / run 20261122-103811): the new "Together With" test passed on the old app because the app returned `together_with: null` for every item and the test only asserted `if tw is not None`: zero assertions ran. Root cause of the null: the RSS feed has no body text, the sponsor block (`<strong>Together With</strong><a href><img src>`) lives only in each issue's HTML page (https://tldr.tech/ai/YYYY-MM-DD); the earlier column was built on a wrong assumption. Stage 3 (tests must fail on the old app) caught it and sent the ticket to Build Broken. Fix in prompts: tests must never pass vacuously (assert the data exists first, no `if x is None: continue` around key assertions; the implement agent must not weaken a failing existence assertion), and intake must check the real data (live feed via WebFetch) and plan the fix at the data source. Confirmed working by the user on a real rerun of KAN-14 (2026-11-22): the WebFetch grant for intake plus the anti-vacuous-test prompt rules fixed it.
- Presentation look (2026-11-20): page width 75% of the window, smaller left-aligned RUN button, and a connected pipeline row (Preparing, 7 steps, Finish) with a blinking green border + timer on the running step, green/red/orange end states. Progress state is computed server-side from log lines (4 new offline tests); checked visually with headless Edge screenshots (idle, running, done, failed; 1700/1280/1024 px) against a throwaway console on :8811 driven by the fake pipeline.
- Demo buttons (2026-11-20): **Start Mynews** (via the deployer's `restart` tool; needs the deployer up) and **Start Deployer MCP** (detached background process). Verified by hand through the API (down -> start deployer -> start mynews -> already-running messages); no automated tests by choice.
- Verified: 9 offline tests with a fake pipeline; a real dry run through the HTTP API ended "nothing to do" (empty To Do). Not shown: progress inside an agent stage (it is one `claude -p` call; streaming `stream-json` would allow it); no Stop button.

## 5. Key risks / things to decide as we go

- **Jira workflow names**: confirmed as "To Do", "In Progress", "Done". Test issue: KAN-4 ("First Task"), also KAN-1 and KAN-3 are in To Do.
- **Jira auth (resolved)**: the Atlassian MCP rejects API tokens ("ask your organization admin"), and the service account had no Jira product access. Decision: custom stdio Jira MCP using a personal classic API token (full token ends with `=XXXXXXXX`; check for truncation). Keep it in an untracked `.env`.
- **Windows process handling** (stop/start, detached process, PID file) is the fiddliest part of the deployer. Keep it simple and test it early (M2).
- **Test-fails-first (step 3)** requires the agent's test to be black-box over HTTP. Put this in the stage 2 prompt explicitly.
- **Agent permissions**: use narrow `--allowedTools` per stage instead of `--dangerously-skip-permissions`.
- **Git**: no remote is required. Add GitHub remotes later if you want.

## 6. First actions

1. Confirm the choices in section 1 (Python/FastAPI/vanilla JS/no DB/RSS).
2. Start M0 and M1 (create the `mynews` repo and a working v0.1).
