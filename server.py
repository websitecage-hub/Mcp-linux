import os
import re
import json
import uuid
import base64
import signal
import subprocess
import threading
import urllib.request
import mimetypes
from pathlib import Path
from datetime import datetime

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
)
from starlette.routing import Mount, Route


# ============================================================
# CONFIG
# ============================================================

WORKDIR = Path(
    os.environ.get("MCP_WORKDIR", "/tmp/workspace")
).resolve()

WORKDIR.mkdir(parents=True, exist_ok=True)

JOBS_DIR = WORKDIR / ".jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# MCP
# ============================================================

mcp = MCPServer(
    name="personal-vps",
    instructions=(
        "Autonomous remote Linux machine. "
        "Use run_command for quick commands. "
        "Use start_job for long-running commands. "
        "Use job_status and job_output to monitor jobs. "
        "Use download_file to obtain a direct public URL for files. "
        "Use read_file for text/data and download_url to fetch internet files."
    ),
)


# ============================================================
# SAFE WORKSPACE PATH
# ============================================================

def _safe_path(path: str) -> Path:
    p = (WORKDIR / path).resolve()

    try:
        p.relative_to(WORKDIR)
    except ValueError:
        raise ValueError("Path escapes workspace directory")

    return p


# ============================================================
# COMMAND EXECUTION
# ============================================================

@mcp.tool()
def run_command(
    command: str,
    timeout_seconds: int = 60,
) -> str:
    """Run a quick shell command."""

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout_seconds, 300)),
        )

        return (
            f"exit_code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    except subprocess.TimeoutExpired:
        return (
            f"ERROR: command timed out after "
            f"{timeout_seconds}s. Use start_job."
        )


# ============================================================
# BACKGROUND JOBS
# ============================================================

def _job_paths(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ValueError("Invalid job id")

    directory = JOBS_DIR / job_id

    return (
        directory,
        directory / "stdout.log",
        directory / "stderr.log",
        directory / "meta.json",
    )


@mcp.tool()
def start_job(command: str) -> str:
    """Start a long-running shell command."""

    job_id = uuid.uuid4().hex[:12]

    directory, stdout_log, stderr_log, meta_file = (
        _job_paths(job_id)
    )

    directory.mkdir(parents=True, exist_ok=True)

    meta = {
        "command": command,
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "pid": None,
    }

    meta_file.write_text(json.dumps(meta))

    def worker():

        with (
            open(stdout_log, "w") as stdout,
            open(stderr_log, "w") as stderr,
        ):

            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(WORKDIR),
                stdout=stdout,
                stderr=stderr,
                preexec_fn=os.setsid,
            )

            meta["pid"] = process.pid
            meta_file.write_text(json.dumps(meta))

            exit_code = process.wait()

            meta["status"] = "finished"
            meta["exit_code"] = exit_code
            meta["finished_at"] = datetime.utcnow().isoformat()

            meta_file.write_text(json.dumps(meta))

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()

    return (
        f"Started job {job_id}. "
        f"Poll with job_status('{job_id}')."
    )


@mcp.tool()
def job_status(job_id: str) -> str:
    """Check a background job."""

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = _job_paths(job_id)

    if not meta_file.exists():
        return f"ERROR: no such job {job_id}"

    meta = json.loads(meta_file.read_text())

    stdout = (
        stdout_log.read_text(errors="replace")[-4000:]
        if stdout_log.exists()
        else ""
    )

    stderr = (
        stderr_log.read_text(errors="replace")[-4000:]
        if stderr_log.exists()
        else ""
    )

    return (
        f"status: {meta.get('status')}\n"
        f"command: {meta.get('command')}\n"
        f"pid: {meta.get('pid')}\n"
        f"exit_code: {meta.get('exit_code')}\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )


@mcp.tool()
def job_output(job_id: str) -> str:
    """Get complete output of a background job."""

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = _job_paths(job_id)

    if not meta_file.exists():
        return f"ERROR: no such job {job_id}"

    stdout = (
        stdout_log.read_text(errors="replace")
        if stdout_log.exists()
        else ""
    )

    stderr = (
        stderr_log.read_text(errors="replace")
        if stderr_log.exists()
        else ""
    )

    return (
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )


@mcp.tool()
def list_jobs() -> str:
    """List background jobs."""

    rows = []

    for directory in sorted(JOBS_DIR.iterdir()):

        meta_file = directory / "meta.json"

        if not meta_file.exists():
            continue

        meta = json.loads(meta_file.read_text())

        rows.append(
            f"{directory.name}\t"
            f"{meta.get('status')}\t"
            f"{str(meta.get('command', ''))[:100]}"
        )

    return "\n".join(rows) if rows else "(no jobs)"


@mcp.tool()
def kill_job(job_id: str) -> str:
    """Kill a running job."""

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = _job_paths(job_id)

    if not meta_file.exists():
        return f"ERROR: no such job {job_id}"

    meta = json.loads(meta_file.read_text())

    pid = meta.get("pid")

    if not pid:
        return f"Job {job_id} has no recorded pid"

    try:
        os.killpg(
            os.getpgid(pid),
            signal.SIGKILL,
        )
    except ProcessLookupError:
        return f"Job {job_id} already finished"

    meta["status"] = "killed"
    meta_file.write_text(json.dumps(meta))

    return f"Killed job {job_id} (pid {pid})"


# ============================================================
# FILES
# ============================================================

@mcp.tool()
def write_file(
    path: str,
    content: str,
    base64_encoded: bool = False,
) -> str:
    """Write text or base64 binary data."""

    target = _safe_path(path)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if base64_encoded:
        target.write_bytes(
            base64.b64decode(content)
        )
    else:
        target.write_text(content)

    return (
        f"Wrote {target.stat().st_size} "
        f"bytes to {path}"
    )


@mcp.tool()
def read_file(
    path: str,
    base64_encoded: bool = False,
) -> str:
    """Read text or binary data."""

    target = _safe_path(path)

    if not target.exists():
        return f"ERROR: {path} does not exist"

    if target.is_dir():
        return f"ERROR: {path} is a directory"

    if base64_encoded:
        return base64.b64encode(
            target.read_bytes()
        ).decode()

    return target.read_text(
        errors="replace"
    )


@mcp.tool()
def edit_file(
    path: str,
    old_str: str,
    new_str: str,
) -> str:
    """Replace one exact unique string."""

    target = _safe_path(path)

    if not target.exists():
        return f"ERROR: {path} does not exist"

    content = target.read_text()

    count = content.count(old_str)

    if count == 0:
        return "ERROR: old_str not found"

    if count > 1:
        return (
            f"ERROR: old_str is not unique "
            f"({count} occurrences)"
        )

    target.write_text(
        content.replace(
            old_str,
            new_str,
            1,
        )
    )

    return f"Edited {path}"


@mcp.tool()
def list_files(path: str = ".") -> str:
    """List files in a workspace directory."""

    target = _safe_path(path)

    if not target.exists():
        return f"ERROR: {path} does not exist"

    if not target.is_dir():
        return f"ERROR: {path} is not a directory"

    rows = []

    for item in sorted(target.iterdir()):

        if item.is_dir():
            rows.append(
                f"DIR\t\t{item.name}"
            )
        else:
            rows.append(
                f"FILE\t{item.stat().st_size}\t{item.name}"
            )

    return "\n".join(rows) if rows else "(empty)"


@mcp.tool()
def search_files(
    pattern: str,
    path: str = ".",
    max_results: int = 50,
) -> str:
    """Search workspace files using regex."""

    target = _safe_path(path)

    regex = re.compile(pattern)

    results = []

    for root, dirs, files in os.walk(target):

        dirs[:] = [
            d for d in dirs
            if d not in (
                ".git",
                "__pycache__",
                "node_modules",
                ".jobs",
            )
        ]

        for filename in files:

            file_path = Path(root) / filename

            try:

                with open(
                    file_path,
                    "r",
                    errors="ignore",
                ) as file:

                    for line_number, line in enumerate(
                        file,
                        1,
                    ):

                        if regex.search(line):

                            relative = (
                                file_path.relative_to(
                                    WORKDIR
                                )
                            )

                            results.append(
                                f"{relative}:"
                                f"{line_number}:"
                                f"{line.strip()[:300]}"
                            )

                            if len(results) >= max_results:
                                return "\n".join(results)

            except (
                UnicodeDecodeError,
                PermissionError,
                OSError,
            ):
                pass

    return (
        "\n".join(results)
        if results
        else "(no matches)"
    )


# ============================================================
# INTERNET
# ============================================================

@mcp.tool()
def download_url(
    url: str,
    save_as: str,
) -> str:
    """Download an internet file into the workspace."""

    target = _safe_path(save_as)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=60,
    ) as response:

        data = response.read()

    target.write_bytes(data)

    return (
        f"Downloaded {len(data)} bytes "
        f"from {url} -> {save_as}"
    )


# ============================================================
# DIRECT FILE URL
# ============================================================

@mcp.tool()
def download_file(path: str) -> str:
    """
    Return a direct Railway URL for a workspace file.

    Example:

        download_file("renders/video.mp4")

    Returns:

        https://YOUR-RAILWAY-DOMAIN/files/renders/video.mp4
    """

    target = _safe_path(path)

    if not target.exists():
        return f"ERROR: {path} does not exist"

    if not target.is_file():
        return f"ERROR: {path} is not a file"

    # Railway public hostname is intentionally hard-coded.
    # No environment variable is required.

    return (
        "https://mcp-linux-production.up.railway.app"
        "/files/"
        + path.lstrip("/")
    )


# ============================================================
# FILE HTTP SERVER
# ============================================================

async def serve_file(request):
    """
    Directly serve a workspace file.

    /files/example.txt
    /files/renders/video.mp4
    /files/project.zip
    """

    relative_path = request.path_params["path"]

    try:
        target = _safe_path(relative_path)
    except ValueError:
        return PlainTextResponse(
            "Invalid path",
            status_code=400,
        )

    if not target.exists():
        return PlainTextResponse(
            "File not found",
            status_code=404,
        )

    if not target.is_file():
        return PlainTextResponse(
            "Not a file",
            status_code=400,
        )

    mime_type = (
        mimetypes.guess_type(
            target.name
        )[0]
        or "application/octet-stream"
    )

    return FileResponse(
        target,
        media_type=mime_type,
        filename=target.name,
    )


# ============================================================
# HEALTH
# ============================================================

async def health(request):
    return JSONResponse(
        {
            "ok": True,
            "service": "personal-vps",
            "mcp": "/mcp",
            "files": "/files/",
        }
    )


# ============================================================
# SERVER
# ============================================================

if __name__ == "__main__":

    import uvicorn

    from mcp.server.transport_security import (
        TransportSecuritySettings,
    )

    port = int(
        os.environ.get(
            "PORT",
            "8000",
        )
    )

    # EXACTLY the same MCP transport setup
    # as your original working server.

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    mcp_app = mcp.streamable_http_app(
        transport_security=security,
        stateless_http=True,
    )

    # /mcp       -> MCP
    # /files/*   -> direct file downloads
    # /health    -> health check

    app = Starlette(
        routes=[
            Route(
                "/health",
                health,
                methods=["GET"],
            ),

            Mount(
                "/files",
                app=Starlette(
                    routes=[
                        Route(
                            "/{path:path}",
                            serve_file,
                            methods=["GET"],
                        ),
                    ],
                ),
            ),

            Mount(
                "/mcp",
                app=mcp_app,
            ),
        ],
    )

    print("========================================")
    print("Personal VPS MCP")
    print("MCP:     /mcp")
    print("Files:   /files/<path>")
    print("Health:  /health")
    print("========================================")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
