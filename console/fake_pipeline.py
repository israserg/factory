"""Stand-in for orchestrator/run_pipeline.py in tests. FAKE_PIPELINE_MODE: ok | stopped | failed | crash."""
import os
import sys
import time

mode = os.environ.get("FAKE_PIPELINE_MODE", "ok")
dry = "--dry-run" in sys.argv


def log(msg):
    print(f"[12:00:00] {msg}", flush=True)
    time.sleep(float(os.environ.get("FAKE_PIPELINE_DELAY", "0.05")))


log(f"run test-run  stages 1-7  dry_run={dry} no_push=False")
if mode == "crash":
    print("Traceback (most recent call last):\nRuntimeError: boom", flush=True)
    sys.exit(2)
log("preflight: deployer and mynews are up")
log("--- stage 1: intake")
if mode == "stopped":
    log("stopped: no tasks in 'To Do'")
    log("result: stopped   agent cost: $0.010")
    sys.exit(0)
log("task KAN-7: Do the thing")
log("--- stage 2: implement")
log("implemented v0.3.0, tests: ['tests/test_x.py'], package: mynews-0.3.0.zip")
log("--- stage 3: test_before")
log("tests before: exit 1, 2 failed, 4 passed")
log("--- stage 4: deploy")
log("deployed: app now serves v0.3.0 (was 0.2.0)")
log("--- stage 5: test_after")
if mode == "failed":
    log("tests after: exit 1, 1 failed, 5 passed")
    log("FAILED at stage 5 (test_after): tests still fail after deploy")
    log("result: failed   agent cost: $0.020")
    sys.exit(1)
log("tests after: exit 0, 0 failed, 6 passed")
log("--- stage 6: commit")
log("committed abc1234 on task/KAN-7 and fast-forwarded main")
log("--- stage 7: close")
log("KAN-7 is Done")
log("result: done   agent cost: $0.030")
