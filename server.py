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
import urllib.error
import zipfile
import time
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from mcp.server import MCPServer
from mcp.types import Resource

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

from starlette.requests import Request


# ============================================================
# CONFIGURATION
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

UPLOADS_DIR = WORKDIR / ".uploads"
UPLOADS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

TMP_DIR = WORKDIR / ".tmp"
TMP_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


PORT = int(
    os.environ.get(
        "PORT",
        "8000",
    )
)

PUBLIC_BASE_URL = os.environ.get(
    "MCP_PUBLIC_BASE_URL",
    "",
).rstrip("/")


MAX_UPLOAD_MB = max(
    1,
    int(
        os.environ.get(
            "MCP_MAX_UPLOAD_MB",
            "4096",
        )
    ),
)

MAX_UPLOAD_BYTES = (
    MAX_UPLOAD_MB * 1024 * 1024
)


UPLOAD_CHUNK_MB = max(
    1,
    int(
        os.environ.get(
            "MCP_UPLOAD_CHUNK_MB",
            "8",
        )
    ),
)

UPLOAD_CHUNK_BYTES = (
    UPLOAD_CHUNK_MB * 1024 * 1024
)


UPLOAD_SESSION_TTL_SECONDS = max(
    300,
    int(
        os.environ.get(
            "MCP_UPLOAD_SESSION_TTL",
            str(24 * 60 * 60),
        )
    ),
)


REQUEST_TIMEOUT_SECONDS = max(
    5,
    int(
        os.environ.get(
            "MCP_REQUEST_TIMEOUT",
            "120",
        )
    ),
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
        "For uploaded files, never assume that a file:// URI points "
        "to the VPS filesystem. Use upload_resource only when the "
        "MCP client actually provides readable resource contents. "
        "For direct HTTP clients, use the upload API. "
        "For small binary payloads, upload_base64 may be used. "
        "Use download_file for generated artifacts."
    ),
)


# ============================================================
# GLOBAL LOCK
# ============================================================

UPLOAD_LOCK = threading.RLock()


# ============================================================
# TIME HELPERS
# ============================================================

def utc_now():
    return datetime.now(
        timezone.utc
    )


def utc_iso():
    return utc_now().isoformat()


# ============================================================
# PATH HELPERS
# ============================================================

def safe_path(path: str) -> Path:
    if path is None:
        raise ValueError(
            "Path is required"
        )

    path = str(path)

    if "\x00" in path:
        raise ValueError(
            "Path contains null byte"
        )

    target = (
        WORKDIR / path
    ).resolve()

    try:
        target.relative_to(
            WORKDIR
        )
    except ValueError:
        raise ValueError(
            "Path escapes workspace"
        )

    return target


def relative_path(path: Path) -> str:
    return str(
        path.resolve().relative_to(
            WORKDIR
        )
    )


def sanitize_filename(filename: str) -> str:
    if not filename:
        return "upload.bin"

    filename = str(filename)

    if "\x00" in filename:
        raise ValueError(
            "Invalid filename"
        )

    name = Path(filename).name

    if name in {
        "",
        ".",
        "..",
    }:
        raise ValueError(
            "Invalid filename"
        )

    # Prevent control characters.
    name = "".join(
        ch
        for ch in name
        if ord(ch) >= 32
        and ord(ch) != 127
    )

    if not name:
        raise ValueError(
            "Invalid filename"
        )

    return name


def destination_file(
    destination: str,
    filename: str,
) -> Path:

    filename = sanitize_filename(
        filename
    )

    directory = safe_path(
        destination or "."
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    target = safe_path(
        str(
            Path(destination or ".")
            / filename
        )
    )

    return target


# ============================================================
# HASHING
# ============================================================

def sha256_file(
    path: Path,
) -> str:

    digest = hashlib.sha256()

    with open(
        path,
        "rb",
    ) as handle:

        while True:

            chunk = handle.read(
                1024 * 1024
            )

            if not chunk:
                break

            digest.update(
                chunk
            )

    return digest.hexdigest()


# ============================================================
# ATOMIC WRITE
# ============================================================

def atomic_write_bytes(
    target: Path,
    data: bytes,
):

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = target.with_name(
        f".{target.name}."
        f"{uuid.uuid4().hex}.tmp"
    )

    try:

        with open(
            temp,
            "wb",
        ) as handle:

            handle.write(data)
            handle.flush()
            os.fsync(
                handle.fileno()
            )

        os.replace(
            temp,
            target,
        )

    finally:

        try:
            temp.unlink(
                missing_ok=True
            )
        except Exception:
            pass


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
        "ffmpeg": (
            "ffmpeg -version 2>/dev/null "
            "| head -1 || true"
        ),
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

    except Exception as exc:

        return (
            f"ERROR: {type(exc).__name__}: "
            f"{exc}"
        )


# ============================================================
# BACKGROUND JOBS
# ============================================================

def job_paths(
    job_id: str,
):

    if not re.fullmatch(
        r"[0-9a-f]{12}",
        job_id,
    ):
        raise ValueError(
            "Invalid job id"
        )

    directory = (
        JOBS_DIR / job_id
    )

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
        "started_at": utc_iso(),
        "pid": None,
    }

    meta_file.write_text(
        json.dumps(
            meta,
            indent=2,
        )
    )

    def worker():

        try:

            with (
                open(
                    stdout_log,
                    "w",
                ) as stdout,
                open(
                    stderr_log,
                    "w",
                ) as stderr,
            ):

                process = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=str(WORKDIR),
                    stdout=stdout,
                    stderr=stderr,
                    preexec_fn=os.setsid,
                )

                meta["pid"] = (
                    process.pid
                )

                meta_file.write_text(
                    json.dumps(
                        meta,
                        indent=2,
                    )
                )

                code = process.wait()

                meta["status"] = (
                    "finished"
                )

                meta["exit_code"] = code
                meta["finished_at"] = (
                    utc_iso()
                )

                meta_file.write_text(
                    json.dumps(
                        meta,
                        indent=2,
                    )
                )

        except Exception as exc:

            meta["status"] = "failed"
            meta["error"] = str(exc)
            meta["finished_at"] = (
                utc_iso()
            )

            meta_file.write_text(
                json.dumps(
                    meta,
                    indent=2,
                )
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

    rows = []

    for directory in sorted(
        JOBS_DIR.iterdir()
    ):

        meta_file = (
            directory / "meta.json"
        )

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
        json.dumps(
            meta,
            indent=2,
        )
    )

    return (
        f"Killed job {job_id}"
    )


# ============================================================
# UPLOAD SESSION STORAGE
# ============================================================

def upload_session_dir(
    upload_id: str,
) -> Path:

    if not re.fullmatch(
        r"[0-9a-f]{32}",
        upload_id,
    ):
        raise ValueError(
            "Invalid upload id"
        )

    return (
        UPLOADS_DIR / upload_id
    )


def upload_session_meta(
    upload_id: str,
) -> Path:

    return (
        upload_session_dir(
            upload_id
        )
        / "meta.json"
    )


def upload_session_data(
    upload_id: str,
) -> Path:

    return (
        upload_session_dir(
            upload_id
        )
        / "data.part"
    )


def save_upload_meta(
    meta: dict,
):

    path = upload_session_meta(
        meta["upload_id"]
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = path.with_suffix(
        ".tmp"
    )

    temp.write_text(
        json.dumps(
            meta,
            indent=2,
        )
    )

    os.replace(
        temp,
        path,
    )


def load_upload_meta(
    upload_id: str,
) -> dict:

    path = upload_session_meta(
        upload_id
    )

    if not path.exists():

        raise FileNotFoundError(
            "Upload session not found"
        )

    return json.loads(
        path.read_text()
    )


def cleanup_stale_uploads():

    now = time.time()

    for directory in (
        UPLOADS_DIR.iterdir()
    ):

        if not directory.is_dir():
            continue

        meta_file = (
            directory / "meta.json"
        )

        if not meta_file.exists():
            continue

        try:

            meta = json.loads(
                meta_file.read_text()
            )

            created = float(
                meta.get(
                    "created_unix",
                    0,
                )
            )

            if (
                now - created
                > UPLOAD_SESSION_TTL_SECONDS
            ):

                shutil.rmtree(
                    directory,
                    ignore_errors=True,
                )

        except Exception:

            continue


def upload_response(
    meta: dict,
    status: str = "success",
) -> dict:

    return {
        "ok": True,
        "status": status,
        "upload_id": meta.get(
            "upload_id"
        ),
        "filename": meta.get(
            "filename"
        ),
        "destination": meta.get(
            "destination"
        ),
        "bytes_received": meta.get(
            "bytes_received",
            0,
        ),
        "expected_bytes": meta.get(
            "expected_bytes"
        ),
        "complete": meta.get(
            "complete",
            False,
        ),
        "sha256": meta.get(
            "sha256"
        ),
        "mime_type": meta.get(
            "mime_type"
        ),
    }


# ============================================================
# CREATE UPLOAD SESSION
# ============================================================

def create_upload_session(
    filename: str,
    destination: str = ".",
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
    mime_type: str | None = None,
) -> dict:

    filename = sanitize_filename(
        filename
    )

    if expected_bytes is not None:

        expected_bytes = int(
            expected_bytes
        )

        if expected_bytes < 0:
            raise ValueError(
                "expected_bytes must be >= 0"
            )

        if (
            expected_bytes
            > MAX_UPLOAD_BYTES
        ):
            raise ValueError(
                f"File exceeds "
                f"{MAX_UPLOAD_MB} MB limit"
            )

    if expected_sha256:

        expected_sha256 = (
            expected_sha256.lower()
            .strip()
        )

        if not re.fullmatch(
            r"[0-9a-f]{64}",
            expected_sha256,
        ):
            raise ValueError(
                "expected_sha256 must be "
                "a valid SHA-256 hash"
            )

    target = destination_file(
        destination,
        filename,
    )

    upload_id = uuid.uuid4().hex

    directory = upload_session_dir(
        upload_id
    )

    directory.mkdir(
        parents=True,
        exist_ok=False,
    )

    meta = {
        "upload_id": upload_id,
        "filename": filename,
        "destination": relative_path(
            target
        ),
        "target": str(target),
        "expected_bytes": expected_bytes,
        "expected_sha256": expected_sha256,
        "mime_type": (
            mime_type
            or mimetypes.guess_type(
                filename
            )[0]
            or "application/octet-stream"
        ),
        "bytes_received": 0,
        "complete": False,
        "status": "created",
        "created_at": utc_iso(),
        "created_unix": time.time(),
    }

    save_upload_meta(
        meta
    )

    data_file = upload_session_data(
        upload_id
    )

    data_file.touch()

    return meta


# ============================================================
# APPEND UPLOAD CHUNK
# ============================================================

async def append_upload_stream(
    upload_id: str,
    request: Request,
    offset: int | None = None,
) -> dict:

    cleanup_stale_uploads()

    meta = load_upload_meta(
        upload_id
    )

    if meta.get("complete"):
        return upload_response(
            meta,
            "already_complete",
        )

    data_file = upload_session_data(
        upload_id
    )

    with UPLOAD_LOCK:

        current_size = (
            data_file.stat().st_size
            if data_file.exists()
            else 0
        )

        if offset is not None:

            offset = int(offset)

            if offset != current_size:
                raise ValueError(
                    f"Upload offset mismatch. "
                    f"Server expects {current_size}, "
                    f"client sent {offset}."
                )

        if (
            current_size
            >= MAX_UPLOAD_BYTES
        ):
            raise ValueError(
                "Upload already reached maximum size"
            )

        written = 0

        with open(
            data_file,
            "ab",
        ) as output:

            while True:

                chunk = await request.body()

                if chunk:
                    output.write(
                        chunk
                    )
                    written += len(
                        chunk
                    )

                break

        total = (
            current_size
            + written
        )

        if total > MAX_UPLOAD_BYTES:

            # Roll back this request.
            with open(
                data_file,
                "rb+",
            ) as output:

                output.truncate(
                    current_size
                )

            raise ValueError(
                f"Upload exceeds "
                f"{MAX_UPLOAD_MB} MB limit"
            )

        meta[
            "bytes_received"
        ] = total

        meta["status"] = (
            "uploading"
        )

        save_upload_meta(
            meta
        )

    return upload_response(
        meta,
        "chunk_received",
    )


# ============================================================
# NOTE:
# request.body() above is intentionally simple.
# For very large HTTP chunks, the route below uses a streaming
# implementation instead.
# ============================================================

async def stream_append(
    request: Request,
    output,
    max_bytes: int,
    already_received: int,
):

    received = (
        already_received
    )

    while True:

        chunk = await request.stream().__anext__()

        if not chunk:
            break

        if (
            received
            + len(chunk)
            > max_bytes
        ):
            raise ValueError(
                f"Upload exceeds "
                f"{MAX_UPLOAD_MB} MB limit"
            )

        output.write(
            chunk
        )

        received += len(
            chunk
        )

    return received


# ============================================================
# FINALIZE UPLOAD
# ============================================================

def finalize_upload_session(
    upload_id: str,
) -> dict:

    cleanup_stale_uploads()

    meta = load_upload_meta(
        upload_id
    )

    if meta.get("complete"):

        return upload_response(
            meta,
            "already_complete",
        )

    data_file = upload_session_data(
        upload_id
    )

    if not data_file.exists():

        raise ValueError(
            "Upload data does not exist"
        )

    size = data_file.stat().st_size

    expected = meta.get(
        "expected_bytes"
    )

    if (
        expected is not None
        and size != expected
    ):
        raise ValueError(
            f"Size mismatch: "
            f"expected {expected} bytes, "
            f"received {size}"
        )

    if size > MAX_UPLOAD_BYTES:

        raise ValueError(
            f"Upload exceeds "
            f"{MAX_UPLOAD_MB} MB limit"
        )

    digest = sha256_file(
        data_file
    )

    expected_hash = meta.get(
        "expected_sha256"
    )

    if (
        expected_hash
        and digest != expected_hash
    ):
        raise ValueError(
            "SHA-256 verification failed"
        )

    target = Path(
        meta["target"]
    )

    target = safe_path(
        relative_path(target)
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_target = target.with_name(
        f".{target.name}."
        f"{uuid.uuid4().hex}.finalizing"
    )

    try:

        shutil.copyfile(
            data_file,
            temp_target,
        )

        with open(
            temp_target,
            "rb",
        ) as handle:

            os.fsync(
                handle.fileno()
            )

        os.replace(
            temp_target,
            target,
        )

    finally:

        try:
            temp_target.unlink(
                missing_ok=True
            )
        except Exception:
            pass

    meta[
        "bytes_received"
    ] = size

    meta["sha256"] = digest
    meta["complete"] = True
    meta["status"] = "complete"
    meta["completed_at"] = (
        utc_iso()
    )

    save_upload_meta(
        meta
    )

    # Keep metadata for a short period,
    # but remove potentially large partial data.
    try:
        data_file.unlink(
            missing_ok=True
        )
    except Exception:
        pass

    return upload_response(
        meta,
        "complete",
    )


# ============================================================
# MCP RESOURCE UPLOAD
# ============================================================

@mcp.tool()
async def upload_resource(
    file_info: Resource,
    destination: str,
    ctx: Context,
) -> str:
    """
    Upload a binary/text MCP Resource into the workspace.

    Important:
    The URI must be readable by the MCP server/resource provider.
    A ChatGPT-local file:// URI is NOT automatically readable by a
    remote VPS.
    """

    if not file_info:

        return (
            "ERROR: no uploaded resource supplied"
        )

    try:

        contents = await ctx.read_resource(
            file_info.uri
        )

    except Exception as exc:

        return (
            "ERROR: could not read uploaded MCP "
            f"resource {file_info.uri}: {exc}. "
            "The MCP host must expose the attachment "
            "as an actual readable Resource."
        )

    if not contents:

        return (
            "ERROR: uploaded resource contained no data"
        )

    first = contents[0]

    data = None

    if (
        hasattr(first, "blob")
        and first.blob
    ):

        try:

            data = base64.b64decode(
                first.blob,
                validate=True,
            )

        except Exception as exc:

            return (
                "ERROR: invalid base64 resource data: "
                f"{exc}"
            )

    elif (
        hasattr(first, "text")
        and first.text is not None
    ):

        data = first.text.encode(
            "utf-8"
        )

    if data is None:

        return (
            "ERROR: unsupported MCP resource content type"
        )

    if len(data) > MAX_UPLOAD_BYTES:

        return (
            f"ERROR: upload is "
            f"{len(data)} bytes, exceeding "
            f"the {MAX_UPLOAD_MB} MB limit"
        )

    filename = sanitize_filename(
        getattr(
            file_info,
            "name",
            None,
        )
        or "upload.bin"
    )

    target = destination_file(
        destination,
        filename,
    )

    atomic_write_bytes(
        target,
        data,
    )

    digest = hashlib.sha256(
        data
    ).hexdigest()

    return json.dumps(
        {
            "ok": True,
            "source_uri": str(
                file_info.uri
            ),
            "destination": relative_path(
                target
            ),
            "bytes": len(data),
            "name": filename,
            "mime_type": (
                getattr(
                    file_info,
                    "mimeType",
                    None,
                )
                or mimetypes.guess_type(
                    filename
                )[0]
                or "application/octet-stream"
            ),
            "sha256": digest,
        },
        indent=2,
    )


# ============================================================
# BASE64 UPLOAD
# ============================================================

@mcp.tool()
def upload_base64(
    filename: str,
    content_base64: str,
    destination: str = ".",
) -> str:

    if not content_base64:

        return (
            "ERROR: empty base64 payload"
        )

    filename = sanitize_filename(
        filename
    )

    try:

        data = base64.b64decode(
            content_base64,
            validate=True,
        )

    except Exception as exc:

        return (
            f"ERROR: invalid base64 data: {exc}"
        )

    if len(data) > MAX_UPLOAD_BYTES:

        return (
            f"ERROR: upload is "
            f"{len(data)} bytes, exceeding "
            f"the {MAX_UPLOAD_MB} MB limit"
        )

    target = destination_file(
        destination,
        filename,
    )

    atomic_write_bytes(
        target,
        data,
    )

    return json.dumps(
        {
            "ok": True,
            "destination": relative_path(
                target
            ),
            "bytes": len(data),
            "sha256": hashlib.sha256(
                data
            ).hexdigest(),
            "mime_type": (
                mimetypes.guess_type(
                    target.name
                )[0]
                or "application/octet-stream"
            ),
        },
        indent=2,
    )


# ============================================================
# HTTP: CREATE UPLOAD SESSION
# ============================================================

async def upload_init_endpoint(
    request: Request,
):

    try:

        payload = await request.json()

        filename = payload.get(
            "filename"
        )

        destination = payload.get(
            "destination",
            ".",
        )

        expected_bytes = payload.get(
            "expected_bytes"
        )

        expected_sha256 = payload.get(
            "sha256"
        )

        mime_type = payload.get(
            "mime_type"
        )

        meta = create_upload_session(
            filename=filename,
            destination=destination,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            mime_type=mime_type,
        )

        return JSONResponse(
            {
                "ok": True,
                "upload_id": meta[
                    "upload_id"
                ],
                "chunk_size": (
                    UPLOAD_CHUNK_BYTES
                ),
                "max_upload_bytes": (
                    MAX_UPLOAD_BYTES
                ),
                "destination": meta[
                    "destination"
                ],
                "filename": meta[
                    "filename"
                ],
            }
        )

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# HTTP: UPLOAD CHUNK
# ============================================================

async def upload_chunk_endpoint(
    request: Request,
):

    upload_id = (
        request.path_params.get(
            "upload_id"
        )
    )

    try:

        meta = load_upload_meta(
            upload_id
        )

        if meta.get("complete"):

            return JSONResponse(
                upload_response(
                    meta,
                    "already_complete",
                )
            )

        data_file = (
            upload_session_data(
                upload_id
            )
        )

        with UPLOAD_LOCK:

            current = (
                data_file.stat().st_size
                if data_file.exists()
                else 0
            )

            offset_header = (
                request.headers.get(
                    "x-upload-offset"
                )
            )

            if offset_header is not None:

                offset = int(
                    offset_header
                )

                if offset != current:

                    return JSONResponse(
                        {
                            "ok": False,
                            "error": (
                                "offset mismatch"
                            ),
                            "expected_offset": (
                                current
                            ),
                        },
                        status_code=409,
                    )

            total = current

            with open(
                data_file,
                "ab",
            ) as output:

                async for chunk in (
                    request.stream()
                ):

                    if not chunk:
                        continue

                    total += len(
                        chunk
                    )

                    if (
                        total
                        > MAX_UPLOAD_BYTES
                    ):
                        raise ValueError(
                            f"Upload exceeds "
                            f"{MAX_UPLOAD_MB} MB limit"
                        )

                    output.write(
                        chunk
                    )

                output.flush()

                os.fsync(
                    output.fileno()
                )

            meta[
                "bytes_received"
            ] = total

            meta["status"] = (
                "uploading"
            )

            save_upload_meta(
                meta
            )

        return JSONResponse(
            {
                "ok": True,
                "upload_id": upload_id,
                "bytes_received": total,
                "next_offset": total,
                "complete": False,
            }
        )

    except FileNotFoundError:

        return JSONResponse(
            {
                "ok": False,
                "error": "upload session not found",
            },
            status_code=404,
        )

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# HTTP: UPLOAD STATUS
# ============================================================

async def upload_status_endpoint(
    request: Request,
):

    upload_id = (
        request.path_params.get(
            "upload_id"
        )
    )

    try:

        meta = load_upload_meta(
            upload_id
        )

        return JSONResponse(
            upload_response(
                meta,
                "status",
            )
        )

    except FileNotFoundError:

        return JSONResponse(
            {
                "ok": False,
                "error": "upload session not found",
            },
            status_code=404,
        )

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# HTTP: FINALIZE UPLOAD
# ============================================================

async def upload_finalize_endpoint(
    request: Request,
):

    upload_id = (
        request.path_params.get(
            "upload_id"
        )
    )

    try:

        result = finalize_upload_session(
            upload_id
        )

        return JSONResponse(
            result
        )

    except FileNotFoundError:

        return JSONResponse(
            {
                "ok": False,
                "error": "upload session not found",
            },
            status_code=404,
        )

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# HTTP: SIMPLE RAW UPLOAD
# ============================================================

async def upload_raw_endpoint(
    request: Request,
):

    filename = (
        request.query_params.get(
            "filename"
        )
        or request.headers.get(
            "x-filename"
        )
        or "upload.bin"
    )

    destination = (
        request.query_params.get(
            "destination",
            ".",
        )
    )

    expected_sha256 = (
        request.headers.get(
            "x-sha256"
        )
    )

    content_length = (
        request.headers.get(
            "content-length"
        )
    )

    try:

        filename = sanitize_filename(
            filename
        )

        destination = str(
            destination
        )

        expected_bytes = (
            int(content_length)
            if content_length
            else None
        )

        if (
            expected_bytes is not None
            and expected_bytes
            > MAX_UPLOAD_BYTES
        ):

            raise ValueError(
                f"Upload exceeds "
                f"{MAX_UPLOAD_MB} MB limit"
            )

        meta = create_upload_session(
            filename=filename,
            destination=destination,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            mime_type=(
                request.headers.get(
                    "content-type"
                )
            ),
        )

        upload_id = meta[
            "upload_id"
        ]

        data_file = upload_session_data(
            upload_id
        )

        received = 0

        with open(
            data_file,
            "wb",
        ) as output:

            async for chunk in (
                request.stream()
            ):

                if not chunk:
                    continue

                received += len(
                    chunk
                )

                if (
                    received
                    > MAX_UPLOAD_BYTES
                ):
                    raise ValueError(
                        f"Upload exceeds "
                        f"{MAX_UPLOAD_MB} MB limit"
                    )

                output.write(
                    chunk
                )

            output.flush()

            os.fsync(
                output.fileno()
            )

        meta[
            "bytes_received"
        ] = received

        save_upload_meta(
            meta
        )

        result = finalize_upload_session(
            upload_id
        )

        return JSONResponse(
            result
        )

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
        )


# ============================================================
# HTTP: MULTIPART COMPATIBILITY UPLOAD
# ============================================================

async def upload_endpoint(
    request: Request,
):

    try:

        max_size = (
            MAX_UPLOAD_BYTES
        )

        form = await request.form(
            max_part_size=max_size
        )

        upload = form.get(
            "file"
        )

        destination = str(
            form.get(
                "destination",
                ".",
            )
        )

        if (
            upload is None
            or not hasattr(
                upload,
                "read",
            )
            or not hasattr(
                upload,
                "filename",
            )
        ):

            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "multipart field "
                        "'file' is required"
                    ),
                },
                status_code=400,
            )

        filename = sanitize_filename(
            upload.filename
        )

        meta = create_upload_session(
            filename=filename,
            destination=destination,
            expected_bytes=None,
            expected_sha256=None,
            mime_type=getattr(
                upload,
                "content_type",
                None,
            ),
        )

        upload_id = meta[
            "upload_id"
        ]

        data_file = upload_session_data(
            upload_id
        )

        received = 0

        try:

            with open(
                data_file,
                "wb",
            ) as output:

                while True:

                    chunk = await upload.read(
                        UPLOAD_CHUNK_BYTES
                    )

                    if not chunk:
                        break

                    received += len(
                        chunk
                    )

                    if (
                        received
                        > MAX_UPLOAD_BYTES
                    ):
                        raise ValueError(
                            f"Upload exceeds "
                            f"{MAX_UPLOAD_MB} MB limit"
                        )

                    output.write(
                        chunk
                    )

                output.flush()

                os.fsync(
                    output.fileno()
                )

            meta[
                "bytes_received"
            ] = received

            save_upload_meta(
                meta
            )

            result = finalize_upload_session(
                upload_id
            )

            return JSONResponse(
                result
            )

        finally:

            try:
                await upload.close()
            except Exception:
                pass

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=400,
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

    target = safe_path(
        path
    )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if base64_encoded:

        data = base64.b64decode(
            content,
            validate=True,
        )

        if len(data) > MAX_UPLOAD_BYTES:
            return (
                "ERROR: content exceeds "
                f"{MAX_UPLOAD_MB} MB limit"
            )

        atomic_write_bytes(
            target,
            data,
        )

    else:

        target.write_text(
            content,
            encoding="utf-8",
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

    target = safe_path(
        path
    )

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

    target = safe_path(
        path
    )

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    text = target.read_text()

    count = text.count(
        old_str
    )

    if count == 0:
        return (
            "ERROR: old_str not found"
        )

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

    target = safe_path(
        path
    )

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if target.is_dir():

        shutil.rmtree(
            target
        )

    else:

        target.unlink()

    return f"Deleted {path}"


@mcp.tool()
def move_file(
    source: str,
    destination: str,
) -> str:

    src = safe_path(
        source
    )

    dst = safe_path(
        destination
    )

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

    src = safe_path(
        source
    )

    dst = safe_path(
        destination
    )

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

    target = safe_path(
        path
    )

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

    target = safe_path(
        path
    )

    regex = re.compile(
        pattern
    )

    hits = []

    for root, dirs, files in os.walk(
        target
    ):

        dirs[:] = [
            d
            for d in dirs
            if d not in {
                ".git",
                "__pycache__",
                "node_modules",
                ".jobs",
                ".uploads",
                ".tmp",
            }
        ]

        for filename in files:

            file_path = (
                Path(root)
                / filename
            )

            try:

                with open(
                    file_path,
                    "r",
                    errors="ignore",
                ) as handle:

                    for line_number, line in enumerate(
                        handle,
                        1,
                    ):

                        if regex.search(
                            line
                        ):

                            hits.append(
                                f"{file_path.relative_to(WORKDIR)}:"
                                f"{line_number}:"
                                f"{line.strip()[:300]}"
                            )

                            if (
                                len(hits)
                                >= max_results
                            ):

                                return (
                                    "\n".join(
                                        hits
                                    )
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
# FILE INFO / VERIFICATION
# ============================================================

@mcp.tool()
def file_info(
    path: str,
) -> str:

    target = safe_path(
        path
    )

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if not target.is_file():

        return (
            f"ERROR: {path} is not a file"
        )

    stat = target.stat()

    return json.dumps(
        {
            "path": path,
            "name": target.name,
            "bytes": stat.st_size,
            "mime_type": (
                mimetypes.guess_type(
                    target.name
                )[0]
                or "application/octet-stream"
            ),
            "modified": datetime.fromtimestamp(
                stat.st_mtime,
                timezone.utc,
            ).isoformat(),
        },
        indent=2,
    )


@mcp.tool()
def verify_file(
    path: str,
) -> str:

    target = safe_path(
        path
    )

    if not target.exists():

        return json.dumps(
            {
                "ok": False,
                "error": "file does not exist",
            },
            indent=2,
        )

    if not target.is_file():

        return json.dumps(
            {
                "ok": False,
                "error": "not a file",
            },
            indent=2,
        )

    stat = target.stat()

    return json.dumps(
        {
            "ok": True,
            "path": relative_path(
                target
            ),
            "name": target.name,
            "bytes": stat.st_size,
            "sha256": sha256_file(
                target
            ),
            "mime_type": (
                mimetypes.guess_type(
                    target.name
                )[0]
                or "application/octet-stream"
            ),
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
                "personal-vps-agent/2.0"
        },
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:

            temp = target.with_name(
                f".{target.name}."
                f"{uuid.uuid4().hex}.download"
            )

            total = 0

            try:

                with open(
                    temp,
                    "wb",
                ) as output:

                    while True:

                        chunk = response.read(
                            UPLOAD_CHUNK_BYTES
                        )

                        if not chunk:
                            break

                        total += len(
                            chunk
                        )

                        if (
                            total
                            > MAX_UPLOAD_BYTES
                        ):
                            raise ValueError(
                                f"Download exceeds "
                                f"{MAX_UPLOAD_MB} MB limit"
                            )

                        output.write(
                            chunk
                        )

                    output.flush()

                    os.fsync(
                        output.fileno()
                    )

                os.replace(
                    temp,
                    target,
                )

            finally:

                try:
                    temp.unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

    except urllib.error.URLError as exc:

        return (
            f"ERROR: download failed: {exc}"
        )

    return (
        f"Downloaded {total} bytes -> "
        f"{save_as}"
    )


# ============================================================
# VPS → CHAT / BROWSER
# ============================================================

@mcp.tool()
def download_file(
    path: str,
) -> str:

    target = safe_path(
        path
    )

    if not target.exists():

        return (
            f"ERROR: {path} does not exist"
        )

    if not target.is_file():

        return (
            f"ERROR: {path} is not a file"
        )

    if not PUBLIC_BASE_URL:

        return (
            "ERROR: MCP_PUBLIC_BASE_URL is not configured"
        )

    relative = relative_path(
        target
    )

    return (
        f"{PUBLIC_BASE_URL}/files/"
        f"{quote(relative, safe='/')}"
    )


# ============================================================
# ZIP
# ============================================================

@mcp.tool()
def zip_directory(
    path: str,
    output: str,
) -> str:

    source = safe_path(
        path
    )

    destination = safe_path(
        output
    )

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
# PUBLIC FILE DOWNLOAD
# ============================================================

async def download_endpoint(
    request: Request,
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

        target = safe_path(
            path
        )

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
    request: Request,
):

    try:

        stat = shutil.disk_usage(
            WORKDIR
        )

        return JSONResponse(
            {
                "ok": True,
                "service": "personal-vps",
                "mcp": "/mcp",
                "upload": "/upload",
                "upload_init": (
                    "/upload/init"
                ),
                "upload_chunk": (
                    "/upload/{upload_id}/chunk"
                ),
                "upload_status": (
                    "/upload/{upload_id}"
                ),
                "upload_finalize": (
                    "/upload/{upload_id}/finalize"
                ),
                "files": "/files/",
                "workspace": str(
                    WORKDIR
                ),
                "max_upload_mb": (
                    MAX_UPLOAD_MB
                ),
                "chunk_mb": (
                    UPLOAD_CHUNK_MB
                ),
                "disk_free_bytes": (
                    stat.free
                ),
            }
        )

    except Exception as exc:

        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
            },
            status_code=500,
        )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_loop():

    while True:

        try:

            cleanup_stale_uploads()

        except Exception:
            pass

        time.sleep(
            15 * 60
        )


threading.Thread(
    target=cleanup_loop,
    daemon=True,
).start()


# ============================================================
# ROUTES
# ============================================================

mcp.custom_route(
    "/upload",
    methods=["POST"],
)(upload_endpoint)

mcp.custom_route(
    "/upload/raw",
    methods=["PUT", "POST"],
)(upload_raw_endpoint)

mcp.custom_route(
    "/upload/init",
    methods=["POST"],
)(upload_init_endpoint)

mcp.custom_route(
    "/upload/{upload_id}/chunk",
    methods=["PUT", "PATCH", "POST"],
)(upload_chunk_endpoint)

mcp.custom_route(
    "/upload/{upload_id}",
    methods=["GET"],
)(upload_status_endpoint)

mcp.custom_route(
    "/upload/{upload_id}/finalize",
    methods=["POST"],
)(upload_finalize_endpoint)

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

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )

    app = mcp.streamable_http_app(
        transport_security=security,
        stateless_http=True,
    )

    print(
        "=========================================="
    )
    print(
        "PERSONAL VPS AGENT"
    )
    print(
        "=========================================="
    )
    print(
        f"Workspace: {WORKDIR}"
    )
    print(
        f"MCP:       /mcp"
    )
    print(
        f"Upload:    /upload"
    )
    print(
        f"Raw:       /upload/raw"
    )
    print(
        f"Resumable: /upload/init"
    )
    print(
        f"Files:     /files/<path>"
    )
    print(
        f"Health:    /health"
    )
    print(
        f"Max file:  {MAX_UPLOAD_MB} MB"
    )
    print(
        f"Chunk:     {UPLOAD_CHUNK_MB} MB"
    )
    print(
        "=========================================="
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )
