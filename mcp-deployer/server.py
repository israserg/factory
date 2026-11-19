"""MCP server (streamable HTTP, long-running) that patches and restarts the local mynews app.

Run:  python server.py   (listens on 127.0.0.1:DEPLOYER_PORT, default 8765; needs DEPLOYER_TOKEN)
Clients must send:  Authorization: Bearer <DEPLOYER_TOKEN>
"""
import hmac
import os
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer

from deployer import Config, Deployer, DeployError


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

deployer = Deployer(Config.from_env())

mcp = MCPServer(
    "mynews-deployer",
    instructions=("Deploys new builds of the local 'mynews' web app. Use get_status first. "
                  "apply_patch takes the absolute path of a mynews-<version>.zip package built by "
                  "mynews/scripts/package.py; it stops the app, swaps the code, restarts it and verifies "
                  "/health and /version, and automatically rolls back on failure."),
)


@mcp.tool()
def get_status() -> dict:
    """Is mynews running, which version is running/deployed, and which backups exist."""
    return deployer.status()


@mcp.tool()
def apply_patch(package_path: str) -> dict:
    """Stop mynews, replace its code with the given zip package, start it, verify health + version.
    Rolls back to the previous version if the new one fails to start. Returns ok=true/false with details."""
    return deployer.apply_patch(package_path)


@mcp.tool()
def restart() -> dict:
    """Restart the currently deployed mynews version."""
    try:
        return deployer.restart()
    except DeployError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def rollback() -> dict:
    """Restore the most recent backup (the version before the last successful patch) and restart."""
    return deployer.rollback()


class BearerAuth:
    """Minimal ASGI middleware: reject requests without the shared secret."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    token = os.environ.get("DEPLOYER_TOKEN")
    if not token:
        raise SystemExit("DEPLOYER_TOKEN is not set (put it in factory/.env)")
    app = BearerAuth(mcp.streamable_http_app(stateless_http=True, json_response=True), token)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DEPLOYER_PORT", "8765")))
