# console

A one-button web page that runs the task-to-production pipeline and shows friendly progress.

```
powershell scripts\start-console.ps1     # http://127.0.0.1:8800   (background, logs in C:\mywork\runtime)
powershell scripts\stop-console.ps1
python console\app.py                    # or in the foreground
```

- **RUN** clears the previous log and starts `orchestrator/run_pipeline.py` as a subprocess. Only one run at a time (a second click while running is rejected).
- The log shows the pipeline's lines translated into plain messages (`[1/7] Picking the ticket from Jira and analysing it…`, `[3/7] Running tests against the current app (they should fail)…`, ...). Finished runs end with a green/orange/red badge: done / nothing to do / failed.
- The checkbox **Dry run** passes `--dry-run`: no Jira changes and no git push (the app is still deployed locally).
- **Start Deployer MCP** launches the deployer in the background (same as `scripts\start-deployer.ps1`; stop it with `scripts\stop-deployer.ps1`). **Start Mynews** asks the deployer to start the deployed mynews version, so the deployer must be up first (otherwise a message says so). Each button is greyed out while its service is already up, and both refuse to work while a pipeline run is in progress. For demos: click both, then RUN.
- The two pills show whether the deployer and mynews are up. Start the deployer first: `powershell scripts\start-deployer.ps1`. If either is down the run stops immediately with a clear message (pipeline preflight), before anything in Jira is touched.
- The page polls `/api/state?since=N&gen=G` about once per second while running (`gen` changes on each new run so the browser knows to clear its log).

## Safety
Bound to `127.0.0.1`. State-changing requests need an `X-Console: 1` header, and every request must carry a local `Host` header, so other web pages open in your browser cannot trigger a run on localhost (cross-site requests cannot set the custom header; DNS rebinding is blocked by the Host check).

## Not included (yet)
No Stop button, and the log is in memory (restarting the console loses it; the full log of every run is in `orchestrator/state/<run-id>/pipeline.log`). Progress inside an agent stage is not visible, only stage start/end, because the agent runs as one `claude -p` call.

## Test
```
.venv\Scripts\python -m pytest console      # offline, uses fake_pipeline.py
```
