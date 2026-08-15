import os
import re
import json
import uuid
import base64
import hashlib
import hmac
import mimetypes
import secrets
import signal
import subprocess
import threading
import urllib.request
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.requests import Request

WORKDIR = Path(os.environ.get("MCP_WORKDIR", "/tmp/workspace")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)

JOBS_DIR = WORKDIR / ".jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Public HTTPS base URL of this service, e.g. https://your-service.example.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
DOWNLOAD_TOKEN_SECRET = os.environ.get("DOWNLOAD_TOKEN_SECRET", "")
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "3600"))
MAX_INLINE_FILE_BYTES = int(os.environ.get("MAX_INLINE_FILE_BYTES", str(8 * 1024 * 1024)))

if not DOWNLOAD_TOKEN_SECRET:
    # The service can still start, but public download URLs are disabled until
    # a secret is configured. This avoids accidentally exposing files.
    DOWNLOAD_TOKEN_SECRET = secrets.token_urlsafe(32)


mcp = MCPServer(
    name="personal-vps-agentic",
    instructions=(
        "Autonomous remote Linux machine. Use run_command for quick things "
        "(<60s). Use start_job for anything longer. Use job_status/job_output "
        "to monitor jobs. Use publish_file to create a temporary signed download "
        "URL for an artifact, then use download_file_base64 only for small files. "
        "Workspace paths are sandboxed to MCP_WORKDIR."
    ),
)


def _safe_path(rel_path: str) -> Path:
    # Accept relative workspace paths only.
    rel = Path(rel_path)
    if rel.is_absolute():
        raise ValueError("Use a workspace-relative path")
    p = (WORKDIR / rel).resolve()
    try:
        p.relative_to(WORKDIR)
    except ValueError:
        raise ValueError("Path escapes workspace directory")
    return p


def _job_paths(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ValueError("Invalid job id")
    d = JOBS_DIR / job_id
    return d, d / "stdout.log", d / "stderr.log", d / "meta.json"


def _utc_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _sign_download(path: str, exp: int) -> str:
    payload = f"{path}\n{exp}".encode()
    return hmac.new(
        DOWNLOAD_TOKEN_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()


def _verify_download(path: str, exp: int, sig: str) -> bool:
    if exp < _utc_ts():
        return False
    expected = _sign_download(path, exp)
    return hmac.compare_digest(expected, sig)


def _public_url(path: str, ttl_seconds: int) -> str:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL is not configured")
    exp = _utc_ts() + max(1, min(ttl_seconds, 7 * 24 * 3600))
    sig = _sign_download(path, exp)
    return (
        f"{PUBLIC_BASE_URL}/download"
        f"?path={quote(path, safe='')}&exp={exp}&sig={sig}"
    )


# ---------------------------------------------------------------------------
# Quick synchronous execution
# ---------------------------------------------------------------------------

@mcp.tool()
def run_command(command: str, timeout_seconds: int = 60) -> str:
    """Run a shell command and wait for it to finish."""
    timeout_seconds = max(1, min(int(timeout_seconds), 300))
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
        return (
            f"ERROR: command timed out after {timeout_seconds}s. "
            "Use start_job for long-running commands."
        )


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

@mcp.tool()
def start_job(command: str) -> str:
    """Start a long-running shell command and return a job id."""
    job_id = uuid.uuid4().hex[:12]
    d, out_log, err_log, meta_path = _job_paths(job_id)
    d.mkdir(parents=True, exist_ok=True)

    meta = {
        "command": command,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": None,
    }
    meta_path.write_text(json.dumps(meta))

    def _run():
        with open(out_log, "w") as out, open(err_log, "w") as err:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(WORKDIR),
                stdout=out,
                stderr=err,
                preexec_fn=os.setsid,
            )
            meta["pid"] = proc.pid
            meta_path.write_text(json.dumps(meta))
            proc.wait()
            meta["status"] = "finished"
            meta["exit_code"] = proc.returncode
            meta["finished_at"] = datetime.now(timezone.utc).isoformat()
            meta_path.write_text(json.dumps(meta))

    threading.Thread(target=_run, daemon=True).start()
    return f"Started job {job_id}. Poll with job_status('{job_id}')."


@mcp.tool()
def job_status(job_id: str) -> str:
    """Check job status and recent output."""
    d, out_log, err_log, meta_path = _job_paths(job_id)
    if not meta_path.exists():
        return f"ERROR: no such job {job_id}"
    meta = json.loads(meta_path.read_text())
    tail_out = out_log.read_text(errors="replace")[-4000:] if out_log.exists() else ""
    tail_err = err_log.read_text(errors="replace")[-4000:] if err_log.exists() else ""
    return (
        f"status: {meta.get('status')}\n"
        f"command: {meta.get('command')}\n"
        f"exit_code: {meta.get('exit_code')}\n"
        f"pid: {meta.get('pid')}\n"
        f"--- stdout (tail) ---\n{tail_out}\n"
        f"--- stderr (tail) ---\n{tail_err}"
    )


@mcp.tool()
def job_output(job_id: str) -> str:
    """Get full stdout and stderr for a job."""
    d, out_log, err_log, meta_path = _job_paths(job_id)
    if not meta_path.exists():
        return f"ERROR: no such job {job_id}"
    out = out_log.read_text(errors="replace") if out_log.exists() else ""
    err = err_log.read_text(errors="replace") if err_log.exists() else ""
    return f"--- stdout ---\n{out}\n--- stderr ---\n{err}"


@mcp.tool()
def list_jobs() -> str:
    """List background jobs."""
    rows = []
    for d in sorted(JOBS_DIR.iterdir()):
        meta_path = d / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            rows.append(
                f"{d.name}\t{meta.get('status')}\t"
                f"{str(meta.get('command', ''))[:120]}"
            )
    return "\n".join(rows) if rows else "(no jobs)"


@mcp.tool()
def kill_job(job_id: str) -> str:
    """Kill a running background job and its process group."""
    d, out_log, err_log, meta_path = _job_paths(job_id)
    if not meta_path.exists():
        return f"ERROR: no such job {job_id}"
    meta = json.loads(meta_path.read_text())
    pid = meta.get("pid")
    if not pid:
        return f"Job {job_id} has no recorded pid"
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        return f"Job {job_id} already finished"
    meta["status"] = "killed"
    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta))
    return f"Killed job {job_id} (pid {pid})"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@mcp.tool()
def write_file(path: str, content: str, base64_encoded: bool = False) -> str:
    """Write text or base64-encoded binary data to a workspace file."""
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if base64_encoded:
        target.write_bytes(base64.b64decode(content, validate=True))
    else:
        target.write_text(content)
    return f"Wrote {target.stat().st_size} bytes to {path}"


@mcp.tool()
def read_file(path: str, base64_encoded: bool = False) -> str:
    """Read a workspace file. Base64 mode is for binary/small files."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if target.is_dir():
        return f"ERROR: {path} is a directory"
    if base64_encoded:
        if target.stat().st_size > MAX_INLINE_FILE_BYTES:
            return (
                f"ERROR: file is {target.stat().st_size} bytes, exceeding "
                f"MAX_INLINE_FILE_BYTES={MAX_INLINE_FILE_BYTES}. "
                "Use publish_file instead."
            )
        return base64.b64encode(target.read_bytes()).decode()
    return target.read_text(errors="replace")


@mcp.tool()
def edit_file(path: str, old_str: str, new_str: str) -> str:
    """Replace one exact, unique string in a text file."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    content = target.read_text()
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in file"
    if count > 1:
        return f"ERROR: old_str is not unique ({count} occurrences)"
    target.write_text(content.replace(old_str, new_str, 1))
    return f"Edited {path}"


@mcp.tool()
def list_files(path: str = ".") -> str:
    """List one workspace directory."""
    target = _safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if not target.is_dir():
        return f"ERROR: {path} is not a directory"
    entries = []
    for item in sorted(target.iterdir()):
        kind = "DIR" if item.is_dir() else "FILE"
        size = item.stat().st_size if item.is_file() else ""
        entries.append(f"{kind}\t{size}\t{item.name}")
    return "\n".join(entries) if entries else "(empty)"


@mcp.tool()
def search_files(pattern: str, path: str = ".", max_results: int = 50) -> str:
    """Search workspace file contents with a regex."""
    target = _safe_path(path)
    regex = re.compile(pattern)
    hits = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [
            d for d in dirs
            if d not in (".git", "__pycache__", "node_modules", ".jobs")
        ]
        for fname in files:
            fpath = Path(root) / fname
            try:
                with open(fpath, "r", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = fpath.relative_to(WORKDIR)
                            hits.append(f"{rel}:{i}:{line.strip()[:300]}")
                            if len(hits) >= max_results:
                                return "\n".join(hits)
            except (UnicodeDecodeError, PermissionError, OSError):
                pass
    return "\n".join(hits) if hits else "(no matches)"


# ---------------------------------------------------------------------------
# Agent-friendly artifact publishing / downloading
# ---------------------------------------------------------------------------

@mcp.tool()
def artifact_info(path: str) -> str:
    """Return metadata for a workspace artifact."""
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        return f"ERROR: {path} is not a file"
    data = target.read_bytes()
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return json.dumps({
        "path": path,
        "name": target.name,
        "bytes": len(data),
        "mime_type": mime,
        "sha256": hashlib.sha256(data).hexdigest(),
        "modified_at": datetime.fromtimestamp(
            target.stat().st_mtime, timezone.utc
        ).isoformat(),
    }, indent=2)


@mcp.tool()
def publish_file(path: str, ttl_seconds: int = 3600) -> str:
    """Create a signed HTTPS download URL for a workspace artifact."""
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        return f"ERROR: {path} is not a file"
    if not PUBLIC_BASE_URL:
        return (
            "ERROR: PUBLIC_BASE_URL is not configured. Set it to the public "
            "HTTPS origin of this MCP service and restart the server."
        )
    url = _public_url(path, ttl_seconds)
    data = target.read_bytes()
    return json.dumps({
        "download_url": url,
        "name": target.name,
        "bytes": len(data),
        "mime_type": mimetypes.guess_type(target.name)[0]
                    or "application/octet-stream",
        "sha256": hashlib.sha256(data).hexdigest(),
        "expires_at": datetime.fromtimestamp(
            _utc_ts() + min(max(ttl_seconds, 1), 7 * 24 * 3600),
            timezone.utc
        ).isoformat(),
    }, indent=2)


@mcp.tool()
def download_file_base64(path: str) -> str:
    """Return a small file as base64 JSON. Use publish_file for large artifacts."""
    target = _safe_path(path)
    if not target.exists() or not target.is_file():
        return f"ERROR: {path} is not a file"
    size = target.stat().st_size
    if size > MAX_INLINE_FILE_BYTES:
        return (
            f"ERROR: {size} bytes exceeds inline limit "
            f"{MAX_INLINE_FILE_BYTES}; use publish_file."
        )
    data = target.read_bytes()
    return json.dumps({
        "name": target.name,
        "bytes": size,
        "mime_type": mimetypes.guess_type(target.name)[0]
                    or "application/octet-stream",
        "sha256": hashlib.sha256(data).hexdigest(),
        "base64": base64.b64encode(data).decode(),
    })


@mcp.tool()
def publish_directory_zip(path: str, ttl_seconds: int = 3600) -> str:
    """Zip a workspace directory and return a signed download URL."""
    target = _safe_path(path)
    if not target.exists() or not target.is_dir():
        return f"ERROR: {path} is not a directory"

    artifact_id = uuid.uuid4().hex
    rel_dir = target.relative_to(WORKDIR)
    zip_rel = Path(".artifacts") / f"{artifact_id}-{target.name}.zip"
    zip_target = _safe_path(str(zip_rel))
    zip_target.parent.mkdir(parents=True, exist_ok=True)

    with __import__("zipfile").ZipFile(
        zip_target, "w", __import__("zipfile").ZIP_DEFLATED
    ) as zf:
        for f in target.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(target.parent))

    return json.dumps({
        "download_url": _public_url(str(zip_rel), ttl_seconds),
        "path": path,
        "bytes": zip_target.stat().st_size,
        "sha256": hashlib.sha256(zip_target.read_bytes()).hexdigest(),
        "expires_at": datetime.fromtimestamp(
            _utc_ts() + min(max(ttl_seconds, 1), 7 * 24 * 3600),
            timezone.utc
        ).isoformat(),
    }, indent=2)


# ---------------------------------------------------------------------------
# Network/file helpers
# ---------------------------------------------------------------------------

@mcp.tool()
def download_url(url: str, save_as: str) -> str:
    """Download a file from an HTTPS/HTTP URL into the workspace."""
    target = _safe_path(save_as)
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "personal-vps-agentic/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    target.write_bytes(data)
    return f"Downloaded {len(data)} bytes from {url} -> {save_as}"


# ---------------------------------------------------------------------------
# Public signed download endpoint
# ---------------------------------------------------------------------------

async def download_endpoint(request: Request):
    path = request.query_params.get("path", "")
    sig = request.query_params.get("sig", "")
    try:
        exp = int(request.query_params.get("exp", "0"))
    except ValueError:
        return PlainTextResponse("invalid expiry", status_code=400)

    if not path or not sig or not _verify_download(path, exp, sig):
        return PlainTextResponse("invalid or expired download URL", status_code=403)

    try:
        target = _safe_path(path)
    except ValueError:
        return PlainTextResponse("invalid path", status_code=400)

    if not target.exists() or not target.is_file():
        return PlainTextResponse("file not found", status_code=404)

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        headers={
            "Cache-Control": "private, max-age=60",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def health_endpoint(request: Request):
    return JSONResponse({
        "ok": True,
        "service": "personal-vps-agentic",
        "workspace": str(WORKDIR),
        "public_downloads": bool(PUBLIC_BASE_URL),
        "time": datetime.now(timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    port = int(os.environ.get("PORT", "8000"))
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    # Keep MCP at /mcp, while adding normal HTTP routes for health and
    # signed artifact downloads.
    mcp_app = mcp.streamable_http_app(
        transport_security=security,
        stateless_http=True,
    )

    app = Starlette(routes=[
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/download", download_endpoint, methods=["GET"]),
        Mount("/mcp", app=mcp_app),
    ])

    print("MCP endpoint: /mcp")
    print("Health endpoint: /health")
    print("Signed downloads: /download")
    print(f"Public downloads enabled: {bool(PUBLIC_BASE_URL)}")
    uvicorn.run(app, host="0.0.0.0", port=port)
        
