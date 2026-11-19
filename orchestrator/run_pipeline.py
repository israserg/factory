"""Task-to-production pipeline: Jira task -> Claude agent -> tests -> deploy via MCP -> tests -> commit -> Jira Done.

Examples:
  python orchestrator/run_pipeline.py --dry-run                       # all stages, no Jira writes, no push
  python orchestrator/run_pipeline.py                                  # full run (takes the oldest To Do issue)
  python orchestrator/run_pipeline.py --run-id 20261119-1500 --only 3  # re-run one stage of an existing run
  python orchestrator/run_pipeline.py --run-id X --from 3              # continue from stage 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_stage import StageFailure  # noqa: E402
from config import Config  # noqa: E402
from stages import STAGES, cleanup_after_failure  # noqa: E402


def load_ctx(run_dir: Path, run_id: str, args) -> dict:
    f = run_dir / "run.json"
    if f.exists():
        ctx = json.loads(f.read_text(encoding="utf-8"))
    else:
        ctx = {"run_id": run_id, "started": datetime.now().isoformat(timespec="seconds"), "stages": {}}
    ctx["dry_run"] = args.dry_run
    ctx["no_push"] = args.no_push
    ctx.pop("status", None)
    ctx.pop("error", None)
    ctx.pop("stop_reason", None)
    return ctx


def save_ctx(run_dir: Path, ctx: dict) -> None:
    (run_dir / "run.json").write_text(json.dumps(ctx, indent=2, default=str), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", help="existing run to continue (default: start a new run)")
    ap.add_argument("--only", type=int, choices=range(1, 8), metavar="N", help="run only stage N (1-7)")
    ap.add_argument("--from", dest="from_", type=int, choices=range(1, 8), metavar="N", default=1)
    ap.add_argument("--to", type=int, choices=range(1, 8), metavar="N", default=7)
    ap.add_argument("--dry-run", action="store_true", help="no Jira writes, no git push, no Jira close")
    ap.add_argument("--no-push", action="store_true", help="commit locally but do not push to GitHub")
    args = ap.parse_args()

    first, last = (args.only, args.only) if args.only else (args.from_, args.to)
    if first > 1 and not args.run_id:
        ap.error("starting after stage 1 needs --run-id of an existing run")

    cfg = Config.load()
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.state_dir / run_id
    if first > 1 and not run_dir.exists():
        ap.error(f"no such run: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = load_ctx(run_dir, run_id, args)
    logf = open(run_dir / "pipeline.log", "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    log(f"run {run_id}  stages {first}-{last}  dry_run={args.dry_run} no_push={args.no_push}")
    total_cost = 0.0
    rc = 0
    for no, name, fn in STAGES:
        if not first <= no <= last:
            continue
        log(f"--- stage {no}: {name}")
        t0 = time.time()
        ctx["_cost"] = 0.0
        try:
            outcome = fn(ctx, cfg, run_dir, log)
            rec = {"status": "ok", "seconds": round(time.time() - t0, 1), "cost_usd": ctx.pop("_cost", 0.0)}
            ctx["stages"][name] = rec
            total_cost += rec["cost_usd"]
            if outcome == "stop":
                ctx["status"] = "stopped"
                log(f"stopped: {ctx['stop_reason']}")
                save_ctx(run_dir, ctx)
                break
        except StageFailure as e:
            ctx["stages"][name] = {"status": "failed", "seconds": round(time.time() - t0, 1),
                                   "cost_usd": ctx.pop("_cost", 0.0)}
            ctx["status"], ctx["error"] = "failed", str(e)
            log(f"FAILED at stage {no} ({name}): {e}")
            cleanup_after_failure(ctx, cfg, no, str(e), log)
            rc = 1
            save_ctx(run_dir, ctx)
            break
        save_ctx(run_dir, ctx)
    else:
        if last == 7 and rc == 0:
            ctx["status"] = "done"
    save_ctx(run_dir, ctx)
    log(f"result: {ctx.get('status', 'partial')}   agent cost: ${total_cost:.3f}   state: {run_dir}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
