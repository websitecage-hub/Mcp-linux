import os
import re
import json
import uuid
import base64
import signal
import shutil
import hashlib
import mimetypes
import subprocess
import threading
import urllib.request
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime

from mcp.server import MCPServer
from mcp.types import Resource

# Context import for the current MCP Python SDK line.
try:
    from mcp.server.fastmcp import Context
except ImportError:
    try:
        from mcp.server.mcpserver import Context
    except ImportError:
        Context = object

from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
)


# ============================================================
# WORKSPACE
# ============================================================

WORKDIR = Path(
    os.environ.get(
        "MCP_WORKDIR",
        "/tmp/workspace",
    )
).resolve()

WORKDIR.mkdir(
    parents=True,
    exist_ok=True,
)

JOBS_DIR = WORKDIR / ".jobs"

JOBS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# MCP SERVER
# ============================================================

mcp = MCPServer(
    name="personal-vps",
    instructions=(
        "You are an autonomous Linux coding/workstation backend. "
        "Use run_command for quick commands. "
        "Use start_job for long-running commands. "
        "Use job_status and job_output for background jobs. "
        "Uploaded chat files are provided as MCP Resources; use "
        "upload_resource to copy them into the VPS workspace. "
        "Use download_file to obtain a direct URL for generated artifacts. "
        "Use read_file for text/data files. "
        "Use download_url for internet downloads."
    ),
)


# ============================================================
# PATH HELPERS
# ============================================================

def safe_path(path: str) -> Path:
    target = (
        WORKDIR / path
    ).resolve()

    try:
        target.relative_to(WORKDIR)
    except ValueError:
        raise ValueError(
            "Path escapes workspace"
        )

    return target


def relative_path(path: Path) -> str:
    return str(
        path.resolve().relative_to(WORKDIR)
    )


# ============================================================
# SYSTEM
# ============================================================

@mcp.tool()
def system_info() -> str:
    """
    Return useful VPS system information.
    """

    commands = {
        "kernel": "uname -a",
        "cpu": "nproc",
        "memory": "free -h",
        "disk": "df -h /",
        "python": "python3 --version",
        "node": "node --version 2>/dev/null || true",
        "npm": "npm --version 2>/dev/null || true",
        "git": "git --version 2>/dev/null || true",
        "ffmpeg": "ffmpeg -version 2>/dev/null | head -1 || true",
    }

    result = {}

    for name, command in commands.items():

        try:

            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(WORKDIR),
                capture_output=True,
                text=True,
                timeout=10,
            )

            result[name] = (
                completed.stdout.strip()
                or completed.stderr.strip()
            )

        except Exception as exc:

            result[name] = str(exc)

    return json.dumps(
        result,
        indent=2,
    )


# ============================================================
# QUICK COMMAND
# ============================================================

@mcp.tool()
def run_command(
    command: str,
    timeout_seconds: int = 60,
) -> str:
    """
    Run a shell command synchronously.

    Use start_job for installs, builds, renders, downloads,
    model downloads, servers, or anything long-running.
    """

    timeout_seconds = max(
        1,
        min(
            int(timeout_seconds),
            300,
        ),
    )

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
            f"ERROR: timed out after "
            f"{timeout_seconds}s. "
            f"Use start_job."
        )


# ============================================================
# BACKGROUND JOBS
# ============================================================

def job_paths(job_id: str):

    if not re.fullmatch(
        r"[0-9a-f]{12}",
        job_id,
    ):
        raise ValueError(
            "Invalid job id"
        )

    directory = JOBS_DIR / job_id

    return (
        directory,
        directory / "stdout.log",
        directory / "stderr.log",
        directory / "meta.json",
    )


@mcp.tool()
def start_job(
    command: str,
) -> str:
    """
    Start a long-running shell command.
    """

    job_id = uuid.uuid4().hex[:12]

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = job_paths(job_id)

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    meta = {
        "job_id": job_id,
        "command": command,
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "pid": None,
    }

    meta_file.write_text(
        json.dumps(meta, indent=2)
    )

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

            meta_file.write_text(
                json.dumps(meta, indent=2)
            )

            code = process.wait()

            meta["status"] = "finished"
            meta["exit_code"] = code
            meta["finished_at"] = (
                datetime.utcnow().isoformat()
            )

            meta_file.write_text(
                json.dumps(meta, indent=2)
            )

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()

    return (
        f"Started job {job_id}. "
        f"Use job_status('{job_id}')."
    )


@mcp.tool()
def job_status(
    job_id: str,
) -> str:
    """
    Get status and recent output of a job.
    """

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = job_paths(job_id)

    if not meta_file.exists():

        return (
            f"ERROR: no such job {job_id}"
        )

    meta = json.loads(
        meta_file.read_text()
    )

    stdout = (
        stdout_log.read_text(
            errors="replace"
        )[-5000:]
        if stdout_log.exists()
        else ""
    )

    stderr = (
        stderr_log.read_text(
            errors="replace"
        )[-5000:]
        if stderr_log.exists()
        else ""
    )

    return (
        f"status: {meta.get('status')}\n"
        f"pid: {meta.get('pid')}\n"
        f"exit_code: {meta.get('exit_code')}\n"
        f"command: {meta.get('command')}\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )


@mcp.tool()
def job_output(
    job_id: str,
) -> str:
    """
    Get complete stdout and stderr.
    """

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = job_paths(job_id)

    if not meta_file.exists():

        return (
            f"ERROR: no such job {job_id}"
        )

    stdout = (
        stdout_log.read_text(
            errors="replace"
        )
        if stdout_log.exists()
        else ""
    )

    stderr = (
        stderr_log.read_text(
            errors="replace"
        )
        if stderr_log.exists()
        else ""
    )

    return (
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )


@mcp.tool()
def list_jobs() -> str:
    """
    List all background jobs.
    """

    rows = []

    for directory in sorted(
        JOBS_DIR.iterdir()
    ):

        meta_file = directory / "meta.json"

        if not meta_file.exists():
            continue

        try:

            meta = json.loads(
                meta_file.read_text()
            )

            rows.append(
                f"{directory.name}\t"
                f"{meta.get('status')}\t"
                f"{str(meta.get('command', ''))[:120]}"
            )

        except Exception:
            continue

    return (
        "\n".join(rows)
        if rows
        else "(no jobs)"
    )


@mcp.tool()
def kill_job(
    job_id: str,
) -> str:
    """
    Kill a running background job.
    """

    (
        directory,
        stdout_log,
        stderr_log,
        meta_file,
    ) = job_paths(job_id)

    if not meta_file.exists():

        return (
            f"ERROR: no such job {job_id}"
        )

    meta = json.loads(
        meta_file.read_text()
    )

    pid = meta.get("pid")

    if not pid:
        return (
            f"Job {job_id} has no PID"
        )

    try:

        os.killpg(
            os.getpgid(pid),
            signal.SIGKILL,
        )

    except ProcessLookupError:

        return (
            f"Job {job_id} already finished"
        )

    meta["status"] = "killed"

    meta_file.write_text(
        json.dumps(meta, indent=2)
    )

    return (
        f"Killed job {job_id}"
    )


# ============================================================
# CHAT → VPS FILE UPLOAD
# ============================================================

@mcp.tool()
async def upload_resource(
    file_info: Resource,
    destination: str,
    ctx: Context,
) -> str:
    """
    Copy a binary/text file supplied by the MCP client/chat into the VPS workspace.

    The MCP host should pass an uploaded attachment as a Resource.  This is the
    preferred ChatGPT/MCP upload path because binary attachments remain binary
    instead of being forced through a text-only argument.

    Example:
        destination="audio/testing.wav"
    """
    if not file_info:
        return "ERROR: no uploaded resource supplied"

    destination_path = safe_path(destination)

    # Prevent accidental writes to an enormous single file.  Adjust through env.
    max_upload_mb = max(1, int(os.environ.get("MCP_MAX_UPLOAD_MB", "512")))
    max_upload_bytes = max_upload_mb * 1024 * 1024

    destination_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        contents = await ctx.read_resource(file_info.uri)
    except Exception as exc:
        return (
            "ERROR: could not read uploaded MCP resource "
            f"{file_info.uri}: {exc}"
        )

    if not contents:
        return "ERROR: uploaded resource contained no data"

    first = contents[0]
    data = None

    # Binary MCP resource.
    if hasattr(first, "blob") and first.blob:
        try:
            data = base64.b64decode(first.blob, validate=True)
        except Exception as exc:
            return f"ERROR: invalid base64 resource data: {exc}"

    # Text MCP resource.
    elif hasattr(first, "text") and first.text is not None:
        data = first.text.encode("utf-8")

    if data is None:
        return "ERROR: unsupported MCP resource content type"

    if len(data) > max_upload_bytes:
        return (
            f"ERROR: upload is {len(data)} bytes, exceeding the "
            f"MCP_MAX_UPLOAD_MB limit of {max_upload_mb} MB"
        )

    # Atomic write: never leave a half-written upload if the process is
    # interrupted while writing.
    temp_path = destination_path.with_name(
        f".{destination_path.name}.{uuid.uuid4().hex}.upload"
    )
    try:
        temp_path.write_bytes(data)
        temp_path.replace(destination_path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return json.dumps(
        {
            "ok": True,
            "source_uri": str(file_info.uri),
            "destination": relative_path(destination_path),
            "bytes": len(data),
            "name": getattr(file_info, "name", None),
            "mime_type": getattr(file_info, "mimeType", None),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        indent=2,
    )


@mcp.tool()
def upload_base64(
    filename: str,
    content_base64: str,
    destination: str = ".",
) -> str:
    """
    Upload binary data supplied directly as base64.

    This is a compatibility fallback for MCP clients that cannot expose an
    attachment as a Resource.  The model/client can send base64 data and the
    VPS writes it atomically into the workspace.

    Example:
        filename="testing.wav"
        destination="audio"
    """
    if not filename or filename in {".", ".."}:
        return "ERROR: invalid filename"

    # Never allow a client-supplied filename to escape the requested directory.
    clean_name = Path(filename).name
    if clean_name != filename:
        return "ERROR: filename must be a simple file name"

    destination_dir = safe_path(destination)
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = safe_path(str(Path(destination) / clean_name))

    max_upload_mb = max(1, int(os.environ.get("MCP_MAX_UPLOAD_MB", "512")))
    max_upload_bytes = max_upload_mb * 1024 * 1024

    try:
        data = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        return f"ERROR: invalid base64 data: {exc}"

    if len(data) > max_upload_bytes:
        return (
            f"ERROR: upload is {len(data)} bytes, exceeding the "
            f"MCP_MAX_UPLOAD_MB limit of {max_upload_mb} MB"
        )

    temp_path = target.with_name(
        f".{target.name}.{uuid.uuid4().hex}.upload"
    )
    try:
        temp_path.write_bytes(data)
        temp_path.replace(target)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return json.dumps(
        {
            "ok": True,
            "destination": relative_path(target),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
        },
        indent=2,
    )


# ============================================================
# FILE OPERATIONS
# ============================================================

@mcp.tool()
def write_file(
    path: str,
    content: str,
    base64_encoded: bool = False,
) -> str:
    """
    Create or overwrite a workspace file.
    """

    target = safe_path(path)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if base64_encoded:

        target.write_bytes(
            base64.b64decode(content)
        )

    else:

        target.write_text(
            content
        )

    return (
        f"Wrote {target.stat().st_size} "
        f"bytes to {path}"
    )


@mcp.tool()
def read_file(
    path: str,
    base64_encoded: bool = False,
) -> str:
    """
    Read a workspace file.
    """

    target = safe_path(path)

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if target.is_dir():

        return (
            f"ERROR: {path} is a directory"
        )

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
    """
    Replace one exact unique string.
    """

    target = safe_path(path)

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    text = target.read_text()

    count = text.count(
        old_str
    )

    if count == 0:
        return "ERROR: old_str not found"

    if count > 1:

        return (
            f"ERROR: old_str occurs "
            f"{count} times"
        )

    target.write_text(
        text.replace(
            old_str,
            new_str,
            1,
        )
    )

    return f"Edited {path}"


@mcp.tool()
def delete_file(
    path: str,
) -> str:
    """
    Delete a workspace file or directory.
    """

    target = safe_path(path)

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if target.is_dir():

        shutil.rmtree(target)

    else:

        target.unlink()

    return (
        f"Deleted {path}"
    )


@mcp.tool()
def move_file(
    source: str,
    destination: str,
) -> str:
    """
    Move/rename a workspace file or directory.
    """

    src = safe_path(source)
    dst = safe_path(destination)

    if not src.exists():

        return (
            f"ERROR: {source} does not exist"
        )

    dst.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.move(
        str(src),
        str(dst),
    )

    return (
        f"Moved {source} -> {destination}"
    )


@mcp.tool()
def copy_file(
    source: str,
    destination: str,
) -> str:
    """
    Copy a workspace file or directory.
    """

    src = safe_path(source)
    dst = safe_path(destination)

    if not src.exists():

        return (
            f"ERROR: {source} does not exist"
        )

    dst.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if src.is_dir():

        shutil.copytree(
            src,
            dst,
            dirs_exist_ok=True,
        )

    else:

        shutil.copy2(
            src,
            dst,
        )

    return (
        f"Copied {source} -> {destination}"
    )


@mcp.tool()
def list_files(
    path: str = ".",
) -> str:
    """
    List files and directories.
    """

    target = safe_path(path)

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if not target.is_dir():

        return (
            f"ERROR: {path} is not a directory"
        )

    rows = []

    for item in sorted(
        target.iterdir()
    ):

        if item.is_dir():

            rows.append(
                f"DIR\t\t{item.name}"
            )

        else:

            rows.append(
                f"FILE\t"
                f"{item.stat().st_size}\t"
                f"{item.name}"
            )

    return (
        "\n".join(rows)
        if rows
        else "(empty)"
    )


@mcp.tool()
def search_files(
    pattern: str,
    path: str = ".",
    max_results: int = 100,
) -> str:
    """
    Search workspace file contents with regex.
    """

    target = safe_path(path)

    regex = re.compile(pattern)

    hits = []

    for root, dirs, files in os.walk(
        target
    ):

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

            file_path = (
                Path(root) / filename
            )

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

                            hits.append(
                                f"{file_path.relative_to(WORKDIR)}:"
                                f"{line_number}:"
                                f"{line.strip()[:300]}"
                            )

                            if len(hits) >= max_results:

                                return "\n".join(
                                    hits
                                )

            except (
                PermissionError,
                OSError,
            ):

                pass

    return (
        "\n".join(hits)
        if hits
        else "(no matches)"
    )


# ============================================================
# FILE METADATA
# ============================================================

@mcp.tool()
def file_info(
    path: str,
) -> str:
    """
    Return file metadata.
    """

    target = safe_path(path)

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if not target.is_file():

        return (
            f"ERROR: {path} is not a file"
        )

    stat = target.stat()

    mime = (
        mimetypes.guess_type(
            target.name
        )[0]
        or "application/octet-stream"
    )

    return json.dumps(
        {
            "path": path,
            "name": target.name,
            "bytes": stat.st_size,
            "mime_type": mime,
            "modified": datetime.fromtimestamp(
                stat.st_mtime
            ).isoformat(),
        },
        indent=2,
    )


# ============================================================
# INTERNET DOWNLOAD
# ============================================================

@mcp.tool()
def download_url(
    url: str,
    save_as: str,
) -> str:
    """
    Download a public URL into the workspace.
    """

    target = safe_path(
        save_as
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "personal-vps-agent/1.0"
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=120,
    ) as response:

        data = response.read()

    target.write_bytes(
        data
    )

    return (
        f"Downloaded {len(data)} "
        f"bytes -> {save_as}"
    )


# ============================================================
# VPS → CHAT / BROWSER
# ============================================================

@mcp.tool()
def download_file(
    path: str,
) -> str:
    """
    Return a direct public URL for a workspace artifact.

    The file endpoint is intentionally unauthenticated,
    matching the user's existing VPS design.
    """

    target = safe_path(path)

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if not target.is_file():

        return (
            f"ERROR: {path} is not a file"
        )

    return (
        "https://mcp-linux-production-8dfb.up.railway.app"
        "/files/"
        + path.lstrip("/")
    )


# ============================================================
# ZIP DIRECTORY
# ============================================================

@mcp.tool()
def zip_directory(
    path: str,
    output: str,
) -> str:
    """
    Create a ZIP archive of a workspace directory.
    """

    source = safe_path(path)
    destination = safe_path(output)

    if not source.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if not source.is_dir():

        return (
            f"ERROR: {path} is not a directory"
        )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with zipfile.ZipFile(
        destination,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as archive:

        for file in source.rglob("*"):

            if file.is_file():

                archive.write(
                    file,
                    file.relative_to(
                        source.parent
                    ),
                )

    return (
        f"Created {output} "
        f"({destination.stat().st_size} bytes)"
    )


# ============================================================
# CHAT / HTTP MULTIPART UPLOAD COMPATIBILITY
# ============================================================

async def upload_endpoint(request):
    """
    POST /upload as multipart/form-data.

    Form fields:
      - file: uploaded binary
      - destination: optional workspace-relative directory

    This exists for MCP/client integrations that can make HTTP requests but
    cannot expose an MCP Resource attachment.
    """
    try:
        form = await request.form()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"invalid multipart request: {exc}"},
            status_code=400,
        )

    upload = form.get("file")
    destination = str(form.get("destination", "."))

    if upload is None or not hasattr(upload, "read") or not hasattr(upload, "filename"):
        return JSONResponse(
            {"ok": False, "error": "multipart field 'file' is required"},
            status_code=400,
        )

    filename = Path(upload.filename or "upload.bin").name
    if not filename or filename in {".", ".."}:
        return JSONResponse(
            {"ok": False, "error": "invalid filename"},
            status_code=400,
        )

    try:
        destination_dir = safe_path(destination)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "invalid destination"},
            status_code=400,
        )

    destination_dir.mkdir(parents=True, exist_ok=True)
    target = safe_path(str(Path(destination) / filename))

    max_upload_mb = max(1, int(os.environ.get("MCP_MAX_UPLOAD_MB", "512")))
    max_upload_bytes = max_upload_mb * 1024 * 1024

    # Stream to disk in chunks so uploads do not require holding the whole file
    # in RAM.
    temp_path = target.with_name(
        f".{target.name}.{uuid.uuid4().hex}.upload"
    )
    total = 0

    try:
        with open(temp_path, "wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_upload_bytes:
                    raise ValueError(
                        f"upload exceeds {max_upload_mb} MB limit"
                    )
                output.write(chunk)

        temp_path.replace(target)

        return JSONResponse(
            {
                "ok": True,
                "destination": relative_path(target),
                "bytes": total,
                "name": filename,
                "mime_type": mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    finally:
        try:
            await upload.close()
        except Exception:
            pass


# ============================================================
# PUBLIC FILE DOWNLOAD
# ============================================================

async def download_endpoint(
    request,
):

    path = request.path_params.get(
        "path",
        "",
    )

    if not path:

        return PlainTextResponse(
            "File path required",
            status_code=400,
        )

    try:

        target = safe_path(path)

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

    mime = (
        mimetypes.guess_type(
            target.name
        )[0]
        or "application/octet-stream"
    )

    return FileResponse(
        target,
        media_type=mime,
        filename=target.name,
    )


# ============================================================
# HEALTH
# ============================================================

async def health_endpoint(
    request,
):

    return JSONResponse(
        {
            "ok": True,
            "service": "personal-vps",
            "mcp": "/mcp",
            "files": "/files/",
            "workspace": str(WORKDIR),
        }
    )


# ============================================================
# REGISTER HTTP ROUTES
# ============================================================

# These routes are registered on MCP itself.
#
# IMPORTANT:
# Do NOT Mount("/mcp", ...).
#
# streamable_http_app() owns /mcp.

mcp.custom_route(
    "/upload",
    methods=["POST"],
)(upload_endpoint)

mcp.custom_route(
    "/files/{path:path}",
    methods=["GET"],
)(download_endpoint)

mcp.custom_route(
    "/health",
    methods=["GET"],
)(health_endpoint)


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

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    app = mcp.streamable_http_app(
        transport_security=security,
        stateless_http=True,
    )

    print("==========================================")
    print("PERSONAL VPS AGENT")
    print("==========================================")
    print("MCP:    /mcp")
    print("Upload: /upload (POST multipart)\n    Files:  /files/<path>")
    print("Health: /health")
    print("==========================================")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
