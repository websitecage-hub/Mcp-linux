import os
import subprocess
import base64
import urllib.request
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


WORKDIR = Path(os.environ.get("MCP_WORKDIR", "/tmp/workspace"))
WORKDIR.mkdir(parents=True, exist_ok=True)

mcp = MCPServer(
    name="personal-vps",
    instructions="Remote machine with shell, file, and URL-download access.",
)


def _safe_path(rel_path: str) -> Path:
    workspace = WORKDIR.resolve()
    target = (workspace / rel_path).resolve()

    if target != workspace and workspace not in target.parents:
        raise ValueError("Path escapes workspace directory")

    return target


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
def write_file(
    path: str,
    content: str,
    base64_encoded: bool = False,
) -> str:
    """Write text or base64-encoded binary content to the workspace."""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if base64_encoded:
        target.write_bytes(base64.b64decode(content))
    else:
        target.write_text(content)

    return f"Wrote {target.stat().st_size} bytes to {path}"


@mcp.tool()
def read_file(path: str, base64_encoded: bool = False) -> str:
    """Read a file from the workspace."""
    target = _safe_path(path)

    if not target.exists():
        return f"ERROR: {path} does not exist"

    if base64_encoded:
        return base64.b64encode(target.read_bytes()).decode()

    return target.read_text(errors="replace")


@mcp.tool()
def list_files(path: str = ".") -> str:
    """List files and directories under a workspace path."""
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
    """Download a file from the internet into the workspace."""
    target = _safe_path(save_as)
    target.parent.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()

    target.write_bytes(data)

    return f"Downloaded {len(data)} bytes from {url} -> {save_as}"


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))

    # MCP Streamable HTTP.
    #
    # "/" makes the Render URL itself the MCP endpoint:
    # https://your-service.onrender.com/
    #
    # If you prefer the standard MCP endpoint, change "/" to "/mcp".
    app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        host="0.0.0.0",
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
