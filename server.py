import os
import subprocess
import base64
import urllib.request
from pathlib import Path

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

WORKDIR = Path(os.environ.get("MCP_WORKDIR", "/tmp/workspace"))
WORKDIR.mkdir(parents=True, exist_ok=True)

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

mcp = MCPServer(
    name="personal-vps",
    instructions="Remote machine with shell, file, and URL-download access.",
)


class BearerAuthMiddleware:
    """Plain shared-secret check: requires 'Authorization: Bearer <token>'."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not AUTH_TOKEN:
            resp = PlainTextResponse("Server misconfigured: MCP_AUTH_TOKEN not set", status_code=500)
            await resp(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {AUTH_TOKEN}":
            resp = PlainTextResponse("Unauthorized", status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _safe_path(rel_path: str) -> Path:
    p = (WORKDIR / rel_path).resolve()
    if not str(p).startswith(str(WORKDIR.resolve())):
        raise ValueError("Path escapes workspace directory")
    return p


@mcp.tool()
def run_command(command: str, timeout_seconds: int = 60) -> str:
    """Run a shell command on this machine and return combined stdout/stderr."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return (
            f"exit_code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout_seconds}s"


@mcp.tool()
def write_file(path: str, content: str, base64_encoded: bool = False) -> str:
    """Write text (or base64-encoded binary) content to a file in the workspace."""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if base64_encoded:
        target.write_bytes(base64.b64decode(content))
    else:
        target.write_text(content)
    return f"Wrote {target.stat().st_size} bytes to {path}"


@mcp.tool()
def read_file(path: str, base64_encoded: bool = False) -> str:
    """Read a file from the workspace. Use base64_encoded=true for binary files."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if base64_encoded:
        return base64.b64encode(target.read_bytes()).decode()
    return target.read_text(errors="replace")


@mcp.tool()
def list_files(path: str = ".") -> str:
    """List files and directories under a path in the workspace."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    entries = []
    for item in sorted(target.iterdir()):
        kind = "DIR" if item.is_dir() else "FILE"
        size = item.stat().st_size if item.is_file() else ""
        entries.append(f"{kind}\t{size}\t{item.name}")
    return "\n".join(entries) if entries else "(empty)"


@mcp.tool()
def download_url(url: str, save_as: str) -> str:
    """Download a file from any URL on the open internet and save it into the workspace."""
    target = _safe_path(save_as)
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    target.write_bytes(data)
    return f"Downloaded {len(data)} bytes from {url} -> {save_as}"


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    app = BearerAuthMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)
