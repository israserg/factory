"""Stand-in for the claude CLI used by tests. Behaviour is chosen with FAKE_CLAUDE_MODE."""
import json
import os
import sys

prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
with open(os.environ["FAKE_CLAUDE_LOG"], "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}, f)

if mode == "garbage":
    print("not json at all")
elif mode == "error":
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "terminal_reason": "max_turns", "result": "ran out of turns"}))
elif mode == "intake_ambiguous":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                      "total_cost_usd": 0.01, "permission_denials": [],
                      "structured_output": {
                          "outcome": "ready", "issue_key": "KAN-9", "title": "Make it nicer",
                          "instructions_md": "## Task\n" + "Make the header bigger. " * 8 + "\n## Assumptions\n- 'nicer' = larger title",
                          "assumptions": ["'nicer' interpreted as a larger header title", "  "], "reason": ""}}))
elif mode == "no_structured":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "free text"}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
                      "total_cost_usd": 0.0123, "permission_denials": [],
                      "structured_output": {"outcome": "ready", "issue_key": "KAN-9"}}))
