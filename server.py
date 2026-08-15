import os
import re
import json
import uuid
import base64
import shlex
import signal
import subprocess
import threading
import urllib.request
import time
from pathlib import Path
from datetime import datetime

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.routing import Mount

WORKDIR = Path(os.environ.get("MCP_WORKDIR", "/tmp/workspace"))
WORKDIR.mkdir(parents=True, exist_ok=True)

JOBS_DIR = WORKDIR / ".jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

mcp = MCPServer(
    name="personal-vps",
    instructions=(
        "Autonomous remote Linux machine. Use run_command for quick things "
        "(<60s). Use start_job for anything longer (video renders, model "
        "downloads, installs) so it survives past the HTTP request window, "
        "then poll with job_status / job_output."
    ),
)


def _safe_path(rel_path: str) -> Path:
    p = (WORKDIR / rel_path).resolve()
    if not str(p).startswith(str(WORKDIR.resolve())):
        raise ValueError("Path escapes workspace directory")
    return p


# ---------------------------------------------------------------------------
# Quick synchronous command execution
# ---------------------------------------------------------------------------

@mcp.tool()
def run_command(command: str, timeout_seconds: int = 60) -> str:
    """Run a shell command and wait for it to finish. Use only for commands
    that finish in well under a minute. For anything longer (video renders,
    pip/apt installs, model downloads), use start_job instead."""
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(WORKDIR),
            capture_output=True, text=True, timeout=timeout_seconds,
        )
        return f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout_seconds}s. Use start_job for long-running commands."


# ---------------------------------------------------------------------------
# Background jobs (survive past a single HTTP request)
# ---------------------------------------------------------------------------

def _job_paths(job_id: str):
    d = JOBS_DIR / job_id
    return d, d / "stdout.log", d / "stderr.log", d / "meta.json"


@mcp.tool()
def start_job(command: str) -> str:
    """Start a long-running shell command in the background and return a job_id
    immediately. Use this for video rendering, model downloads, package installs,
    or anything that might take more than ~60 seconds. Poll with job_status(job_id)."""
    job_id = uuid.uuid4().hex[:12]
    d, out_log, err_log, meta_path = _job_paths(job_id)
    d.mkdir(parents=True, exist_ok=True)

    meta = {"command": command, "status": "running", "started_at": datetime.utcnow().isoformat(), "pid": None}
    meta_path.write_text(json.dumps(meta))

    def _run():
        with open(out_log, "w") as out, open(err_log, "w") as err:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(WORKDIR),
                stdout=out, stderr=err, preexec_fn=os.setsid,
            )
            meta["pid"] = proc.pid
            meta_path.write_text(json.dumps(meta))
            proc.wait()
            meta["status"] = "finished"
            meta["exit_code"] = proc.returncode
            meta["finished_at"] = datetime.utcnow().isoformat()
            meta_path.write_text(json.dumps(meta))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return f"Started job {job_id}. Poll with job_status('{job_id}')."


@mcp.tool()
def job_status(job_id: str) -> str:
    """Check whether a background job is still running, and see the last bit
    of its output."""
    d, out_log, err_log, meta_path = _job_paths(job_id)
    if not meta_path.exists():
        return f"ERROR: no such job {job_id}"
    meta = json.loads(meta_path.read_text())
    tail_out = out_log.read_text(errors="replace")[-1500:] if out_log.exists() else ""
    tail_err = err_log.read_text(errors="replace")[-1500:] if err_log.exists() else ""
    return (
        f"status: {meta.get('status')}\n"
        f"command: {meta.get('command')}\n"
        f"exit_code: {meta.get('exit_code')}\n"
        f"--- stdout (tail) ---\n{tail_out}\n"
        f"--- stderr (tail) ---\n{tail_err}"
    )


@mcp.tool()
def job_output(job_id: str) -> str:
    """Get the FULL stdout+stderr of a job (running or finished)."""
    d, out_log, err_log, meta_path = _job_paths(job_id)
    if not meta_path.exists():
        return f"ERROR: no such job {job_id}"
    out = out_log.read_text(errors="replace") if out_log.exists() else ""
    err = err_log.read_text(errors="replace") if err_log.exists() else ""
    return f"--- stdout ---\n{out}\n--- stderr ---\n{err}"


@mcp.tool()
def list_jobs() -> str:
    """List all background jobs and their status."""
    rows = []
    for d in sorted(JOBS_DIR.iterdir()):
        meta_path = d / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            rows.append(f"{d.name}\t{meta.get('status')}\t{meta.get('command')[:60]}")
    return "\n".join(rows) if rows else "(no jobs)"


@mcp.tool()
def kill_job(job_id: str) -> str:
    """Kill a running background job."""
    d, out_log, err_log, meta_path = _job_paths(job_id)
    if not meta_path.exists():
        return f"ERROR: no such job {job_id}"
    meta = json.loads(meta_path.read_text())
    pid = meta.get("pid")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            meta["status"] = "killed"
            meta_path.write_text(json.dumps(meta))
            return f"Killed job {job_id} (pid {pid})"
        except ProcessLookupError:
            return f"Job {job_id} already finished"
    return f"Job {job_id} has no recorded pid"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@mcp.tool()
def write_file(path: str, content: str, base64_encoded: bool = False) -> str:
    """Write text (or base64-encoded binary) content to a file, overwriting it."""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if base64_encoded:
        target.write_bytes(base64.b64decode(content))
    else:
        target.write_text(content)
    return f"Wrote {target.stat().st_size} bytes to {path}"


@mcp.tool()
def read_file(path: str, base64_encoded: bool = False) -> str:
    """Read a file. Use base64_encoded=true for binary files (images, video, audio)."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if base64_encoded:
        return base64.b64encode(target.read_bytes()).decode()
    return target.read_text(errors="replace")


@mcp.tool()
def edit_file(path: str, old_str: str, new_str: str) -> str:
    """Replace an exact, unique string in a text file with another string.
    old_str must match exactly and appear exactly once - use this instead of
    rewriting whole files for small edits."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    content = target.read_text()
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in file"
    if count > 1:
        return f"ERROR: old_str is not unique ({count} occurrences) - make it more specific"
    target.write_text(content.replace(old_str, new_str, 1))
    return f"Edited {path}"


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
def search_files(pattern: str, path: str = ".", max_results: int = 50) -> str:
    """Search file contents for a regex pattern (like grep -r). Returns
    file:line:matched_text for each hit."""
    target = _safe_path(path)
    regex = re.compile(pattern)
    hits = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".jobs")]
        for fname in files:
            fpath = Path(root) / fname
            try:
                with open(fpath, "r", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = fpath.relative_to(WORKDIR)
                            hits.append(f"{rel}:{i}:{line.strip()[:200]}")
                            if len(hits) >= max_results:
                                return "\n".join(hits)
            except (UnicodeDecodeError, PermissionError):
                continue
    return "\n".join(hits) if hits else "(no matches)"


@mcp.tool()
def download_url(url: str, save_as: str) -> str:
    """Download a file from any URL on the open internet into the workspace."""
    target = _safe_path(save_as)
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    target.write_bytes(data)
    return f"Downloaded {len(data)} bytes from {url} -> {save_as}"


if __name__ == "__main__":
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    port = int(os.environ.get("PORT", "8000"))
    # Railway/Render put this behind a proxy with a real public hostname -
    # the SDK's DNS-rebinding protection rejects any Host header that isn't
    # localhost by default ("Invalid Host header"). Disabling it entirely
    # here, consistent with running this as an open, no-auth personal server.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(transport_security=security, stateless_http=True)
    print("MCP endpoint: /mcp  (open, no auth, any host allowed, stateless)")
    uvicorn.run(app, host="0.0.0.0", port=port)
    
