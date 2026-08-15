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
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, HTMLResponse
from starlette.requests import Request

# ============================================================
# CONFIGURATION
# ============================================================

WORKDIR = Path(os.environ.get("MCP_WORKDIR", "/tmp/workspace")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)

JOBS_DIR = WORKDIR / ".jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

UPLOADS_DIR = WORKDIR / ".uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

TMP_DIR = WORKDIR / ".tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "8000"))

PUBLIC_BASE_URL = os.environ.get("MCP_PUBLIC_BASE_URL", "").rstrip("/")

MAX_UPLOAD_MB = max(1, int(os.environ.get("MCP_MAX_UPLOAD_MB", "4096")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

UPLOAD_CHUNK_MB = max(1, int(os.environ.get("MCP_UPLOAD_CHUNK_MB", "8")))
UPLOAD_CHUNK_BYTES = UPLOAD_CHUNK_MB * 1024 * 1024

UPLOAD_SESSION_TTL_SECONDS = max(
    300, int(os.environ.get("MCP_UPLOAD_SESSION_TTL", str(24 * 60 * 60)))
)

REQUEST_TIMEOUT_SECONDS = max(5, int(os.environ.get("MCP_REQUEST_TIMEOUT", "120")))

# ============================================================
# MCP SERVER
# ============================================================

def _build_mcp():
    kwargs = {
        "host": "0.0.0.0",
        "port": PORT,
        "instructions": (
            "You are an autonomous Linux coding/workstation backend. "
            "Use run_command for quick commands. "
            "Use start_job for long-running commands. "
            "Use upload_base64 only for SMALL text/binary payloads. "
            "Chat attachments CANNOT be sent through MCP. For real files, "
            "tell the user to open the web upload page at the server root URL, "
            "drop the file there, then operate on it in the workspace. "
            "Alternatively use download_url if the user provides a public URL. "
            "Use download_file for generated artifacts."
        ),
    }
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
    except Exception:
        pass
    return FastMCP("personal-vps", **kwargs)

mcp = _build_mcp()

UPLOAD_LOCK = threading.RLock()

# ============================================================
# HELPERS
# ============================================================

def utc_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_path(path: str) -> Path:
    if path is None:
        raise ValueError("Path is required")
    path = str(path)
    if "\x00" in path:
        raise ValueError("Path contains null byte")
    target = (WORKDIR / path).resolve()
    try:
        target.relative_to(WORKDIR)
    except ValueError:
        raise ValueError("Path escapes workspace")
    return target

def relative_path(path: Path) -> str:
    return str(path.resolve().relative_to(WORKDIR))

def sanitize_filename(filename: str) -> str:
    if not filename:
        return "upload.bin"
    filename = str(filename)
    if "\x00" in filename:
        raise ValueError("Invalid filename")
    name = Path(filename).name
    if name in {"", ".", ".."}:
        raise ValueError("Invalid filename")
    name = "".join(ch for ch in name if ord(ch) >= 32 and ord(ch) != 127)
    if not name:
        raise ValueError("Invalid filename")
    return name

def destination_file(destination: str, filename: str) -> Path:
    filename = sanitize_filename(filename)
    directory = safe_path(destination or ".")
    directory.mkdir(parents=True, exist_ok=True)
    return safe_path(str(Path(destination or ".") / filename))

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

def atomic_write_bytes(target: Path, data: bytes):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass

# ============================================================
# SYSTEM / COMMANDS / JOBS
# ============================================================

@mcp.tool()
def system_info() -> str:
    """Return useful VPS system information."""
    commands = {
        "kernel": "uname -a",
        "cpu": "nproc",
        "memory": "free -h",
        "disk": "df -h /",
        "python": "python3 --version",
        "node": "node --version 2>/dev/null || true",
        "git": "git --version 2>/dev/null || true",
    }
    result = {}
    for name, command in commands.items():
        try:
            completed = subprocess.run(
                command, shell=True, cwd=str(WORKDIR),
                capture_output=True, text=True, timeout=10,
            )
            result[name] = completed.stdout.strip() or completed.stderr.strip()
        except Exception as exc:
            result[name] = str(exc)
    return json.dumps(result, indent=2)

@mcp.tool()
def run_command(command: str, timeout_seconds: int = 60) -> str:
    """Run a shell command and return output."""
    timeout_seconds = max(1, min(int(timeout_seconds), 300))
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(WORKDIR),
            capture_output=True, text=True, timeout=timeout_seconds,
        )
        return (
            f"exit_code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout_seconds}s. Use start_job."
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"

def job_paths(job_id: str):
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
    """Start a long-running background job."""
    job_id = uuid.uuid4().hex[:12]
    directory, stdout_log, stderr_log, meta_file = job_paths(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "job_id": job_id, "command": command,
        "status": "running", "started_at": utc_iso(), "pid": None,
    }
    meta_file.write_text(json.dumps(meta, indent=2))

    def worker():
        try:
            with open(stdout_log, "w") as so, open(stderr_log, "w") as se:
                process = subprocess.Popen(
                    command, shell=True, cwd=str(WORKDIR),
                    stdout=so, stderr=se, preexec_fn=os.setsid,
                )
                meta["pid"] = process.pid
                meta_file.write_text(json.dumps(meta, indent=2))
                code = process.wait()
                meta["status"] = "finished"
                meta["exit_code"] = code
                meta["finished_at"] = utc_iso()
                meta_file.write_text(json.dumps(meta, indent=2))
        except Exception as exc:
            meta["status"] = "failed"
            meta["error"] = str(exc)
            meta["finished_at"] = utc_iso()
            meta_file.write_text(json.dumps(meta, indent=2))

    threading.Thread(target=worker, daemon=True).start()
    return f"Started job {job_id}. Use job_status('{job_id}')."

@mcp.tool()
def job_status(job_id: str) -> str:
    """Check status of a background job."""
    directory, stdout_log, stderr_log, meta_file = job_paths(job_id)
    if not meta_file.exists():
        return f"ERROR: no such job {job_id}"
    meta = json.loads(meta_file.read_text())
    stdout = stdout_log.read_text(errors="replace")[-5000:] if stdout_log.exists() else ""
    stderr = stderr_log.read_text(errors="replace")[-5000:] if stderr_log.exists() else ""
    return (
        f"status: {meta.get('status')}\npid: {meta.get('pid')}\n"
        f"exit_code: {meta.get('exit_code')}\ncommand: {meta.get('command')}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )

@mcp.tool()
def job_output(job_id: str) -> str:
    """Get full output of a background job."""
    directory, stdout_log, stderr_log, meta_file = job_paths(job_id)
    if not meta_file.exists():
        return f"ERROR: no such job {job_id}"
    stdout = stdout_log.read_text(errors="replace") if stdout_log.exists() else ""
    stderr = stderr_log.read_text(errors="replace") if stderr_log.exists() else ""
    return f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"

@mcp.tool()
def list_jobs() -> str:
    """List all background jobs."""
    rows = []
    for directory in sorted(JOBS_DIR.iterdir()):
        meta_file = directory / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
            rows.append(
                f"{directory.name}\t{meta.get('status')}\t"
                f"{str(meta.get('command', ''))[:120]}"
            )
        except Exception:
            continue
    return "\n".join(rows) if rows else "(no jobs)"

@mcp.tool()
def kill_job(job_id: str) -> str:
    """Kill a running background job."""
    directory, stdout_log, stderr_log, meta_file = job_paths(job_id)
    if not meta_file.exists():
        return f"ERROR: no such job {job_id}"
    meta = json.loads(meta_file.read_text())
    pid = meta.get("pid")
    if not pid:
        return f"Job {job_id} has no PID"
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        return f"Job {job_id} already finished"
    meta["status"] = "killed"
    meta_file.write_text(json.dumps(meta, indent=2))
    return f"Killed job {job_id}"

# ============================================================
# UPLOAD SESSIONS
# ============================================================

def upload_session_dir(upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("Invalid upload id")
    return UPLOADS_DIR / upload_id

def upload_session_meta(upload_id: str) -> Path:
    return upload_session_dir(upload_id) / "meta.json"

def upload_session_data(upload_id: str) -> Path:
    return upload_session_dir(upload_id) / "data.part"

def save_upload_meta(meta: dict):
    path = upload_session_meta(meta["upload_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(meta, indent=2))
    os.replace(temp, path)

def load_upload_meta(upload_id: str) -> dict:
    path = upload_session_meta(upload_id)
    if not path.exists():
        raise FileNotFoundError("Upload session not found")
    return json.loads(path.read_text())

def cleanup_stale_uploads():
    now = time.time()
    for directory in UPLOADS_DIR.iterdir():
        if not directory.is_dir():
            continue
        meta_file = directory / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
            if now - float(meta.get("created_unix", 0)) > UPLOAD_SESSION_TTL_SECONDS:
                shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            continue

def upload_response(meta: dict, status: str = "success") -> dict:
    return {
        "ok": True, "status": status,
        "upload_id": meta.get("upload_id"),
        "filename": meta.get("filename"),
        "destination": meta.get("destination"),
        "bytes_received": meta.get("bytes_received", 0),
        "expected_bytes": meta.get("expected_bytes"),
        "complete": meta.get("complete", False),
        "sha256": meta.get("sha256"),
        "mime_type": meta.get("mime_type"),
    }

def create_upload_session(filename, destination=".", expected_bytes=None,
                          expected_sha256=None, mime_type=None) -> dict:
    filename = sanitize_filename(filename)
    if expected_bytes is not None:
        expected_bytes = int(expected_bytes)
        if expected_bytes < 0:
            raise ValueError("expected_bytes must be >= 0")
        if expected_bytes > MAX_UPLOAD_BYTES:
            raise ValueError(f"File exceeds {MAX_UPLOAD_MB} MB limit")
    if expected_sha256:
        expected_sha256 = expected_sha256.lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("expected_sha256 must be a valid SHA-256 hash")
    target = destination_file(destination, filename)
    upload_id = uuid.uuid4().hex
    directory = upload_session_dir(upload_id)
    directory.mkdir(parents=True, exist_ok=False)
    meta = {
        "upload_id": upload_id, "filename": filename,
        "destination": relative_path(target), "target": str(target),
        "expected_bytes": expected_bytes, "expected_sha256": expected_sha256,
        "mime_type": mime_type or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        "bytes_received": 0, "complete": False, "status": "created",
        "created_at": utc_iso(), "created_unix": time.time(),
    }
    save_upload_meta(meta)
    upload_session_data(upload_id).touch()
    return meta

def finalize_upload_session(upload_id: str) -> dict:
    meta = load_upload_meta(upload_id)
    if meta.get("complete"):
        return upload_response(meta, "already_complete")
    data_file = upload_session_data(upload_id)
    if not data_file.exists():
        raise ValueError("Upload data does not exist")
    size = data_file.stat().st_size
    expected = meta.get("expected_bytes")
    if expected is not None and size != expected:
        raise ValueError(f"Size mismatch: expected {expected} bytes, received {size}")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(f"Upload exceeds {MAX_UPLOAD_MB} MB limit")
    digest = sha256_file(data_file)
    expected_hash = meta.get("expected_sha256")
    if expected_hash and digest != expected_hash:
        raise ValueError("SHA-256 verification failed")
    target = safe_path(relative_path(Path(meta["target"])))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.finalizing")
    try:
        shutil.copyfile(data_file, temp_target)
        with open(temp_target, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_target, target)
    finally:
        try:
            temp_target.unlink(missing_ok=True)
        except Exception:
            pass
    meta["bytes_received"] = size
    meta["sha256"] = digest
    meta["complete"] = True
    meta["status"] = "complete"
    meta["completed_at"] = utc_iso()
    save_upload_meta(meta)
    try:
        data_file.unlink(missing_ok=True)
    except Exception:
        pass
    return upload_response(meta, "complete")

# ============================================================
# MCP TOOL: BASE64 UPLOAD (small payloads only)
# ============================================================

@mcp.tool()
def upload_base64(filename: str, content_base64: str, destination: str = ".") -> str:
    """Upload a SMALL file using base64. For real files use the web upload page."""
    if not content_base64:
        return "ERROR: empty base64 payload"
    filename = sanitize_filename(filename)
    try:
        content_base64 = content_base64.strip()
        if content_base64.startswith("data:"):
            content_base64 = content_base64.split(",", 1)[1]
        data = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        return f"ERROR: invalid base64 data: {exc}"
    if len(data) > MAX_UPLOAD_BYTES:
        return f"ERROR: upload is {len(data)} bytes, exceeding the {MAX_UPLOAD_MB} MB limit"
    try:
        target = destination_file(destination, filename)
        atomic_write_bytes(target, data)
        return json.dumps({
            "ok": True,
            "destination": relative_path(target),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": mimetypes.guess_type(target.name)[0]
            or "application/octet-stream",
        }, indent=2)
    except Exception as exc:
        return f"ERROR: failed to write file: {exc}"

# ============================================================
# WEB UPLOAD PAGE (drag & drop from your browser)
# ============================================================

UPLOAD_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VPS Upload</title>
<style>
body{font-family:system-ui,sans-serif;background:#0b0f14;color:#e6edf3;max-width:720px;margin:40px auto;padding:0 16px}
h1{font-size:22px}
#drop{border:2px dashed #30363d;border-radius:12px;padding:48px 16px;text-align:center;cursor:pointer;margin:16px 0}
#drop.on{border-color:#2f81f7;background:#0d1a2b}
input[type=text]{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px;width:220px}
#log div{padding:8px 10px;margin:6px 0;border-radius:6px;background:#161b22;border:1px solid #30363d;font-family:ui-monospace,monospace;font-size:13px;word-break:break-all}
#log div.ok{border-color:#238636}
#log div.err{border-color:#da3633}
</style>
</head>
<body>
<h1>Personal VPS - File Upload</h1>
<p>Drop files from your computer. They land directly in the VPS workspace, then tell the chat the filename.</p>
<div id="drop"><b>Drop files here</b><br>or click to select<input type="file" id="file" multiple hidden></div>
<p>Destination folder: <input type="text" id="dest" value="."></p>
<div id="log"></div>
<script>
var drop=document.getElementById('drop'),input=document.getElementById('file'),
    log=document.getElementById('log'),dest=document.getElementById('dest');
drop.onclick=function(){input.click()};
drop.ondragover=function(e){e.preventDefault();drop.classList.add('on')};
drop.ondragleave=function(){drop.classList.remove('on')};
drop.ondrop=function(e){e.preventDefault();drop.classList.remove('on');handle(e.dataTransfer.files)};
input.onchange=function(){handle(input.files)};
function handle(files){for(var i=0;i<files.length;i++){upload(files[i])}}
async function upload(f){
  var line=document.createElement('div');
  line.textContent='Uploading '+f.name+' ('+f.size+' bytes)...';
  log.prepend(line);
  var fd=new FormData();
  fd.append('file',f,f.name);
  fd.append('destination',dest.value||'.');
  try{
    var r=await fetch('/upload',{method:'POST',body:fd});
    var j=await r.json();
    if(j.ok){
      line.className='ok';
      line.textContent='OK '+f.name+' -> '+(j.destination||'')+' | '+(j.bytes_received||0)+' bytes | sha256 '+(j.sha256||'').slice(0,16);
    }else{
      line.className='err';
      line.textContent='FAIL '+f.name+': '+(j.error||'unknown error');
    }
  }catch(e){
    line.className='err';
    line.textContent='ERROR '+f.name+': '+e;
  }
}
</script>
</body>
</html>"""

async def upload_page_endpoint(request: Request):
    return HTMLResponse(UPLOAD_PAGE_HTML)

# ============================================================
# HTTP UPLOAD ENDPOINTS
# ============================================================

async def upload_endpoint(request: Request):
    """Multipart form upload: POST /upload with field 'file'."""
    try:
        try:
            form = await request.form(max_part_size=MAX_UPLOAD_BYTES)
        except TypeError:
            form = await request.form()
        upload = form.get("file")
        destination = str(form.get("destination", "."))
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                {"ok": False, "error": "multipart field 'file' is required"},
                status_code=400,
            )
        filename = sanitize_filename(upload.filename or "upload.bin")
        meta = create_upload_session(
            filename=filename, destination=destination,
            mime_type=getattr(upload, "content_type", None),
        )
        upload_id = meta["upload_id"]
        data_file = upload_session_data(upload_id)
        received = 0
        try:
            with open(data_file, "wb") as output:
                while True:
                    chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_UPLOAD_BYTES:
                        raise ValueError(f"Upload exceeds {MAX_UPLOAD_MB} MB limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            meta["bytes_received"] = received
            save_upload_meta(meta)
            return JSONResponse(finalize_upload_session(upload_id))
        finally:
            try:
                await upload.close()
            except Exception:
                pass
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

async def upload_raw_endpoint(request: Request):
    """Raw body upload: PUT /upload/raw?filename=x"""
    filename = (
        request.query_params.get("filename")
        or request.headers.get("x-filename")
        or "upload.bin"
    )
    destination = request.query_params.get("destination", ".")
    expected_sha256 = request.headers.get("x-sha256")
    content_length = request.headers.get("content-length")
    try:
        filename = sanitize_filename(filename)
        expected_bytes = int(content_length) if content_length else None
        if expected_bytes is not None and expected_bytes > MAX_UPLOAD_BYTES:
            raise ValueError(f"Upload exceeds {MAX_UPLOAD_MB} MB limit")
        meta = create_upload_session(
            filename=filename, destination=destination,
            expected_bytes=expected_bytes, expected_sha256=expected_sha256,
            mime_type=request.headers.get("content-type"),
        )
        upload_id = meta["upload_id"]
        data_file = upload_session_data(upload_id)
        received = 0
        with open(data_file, "wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > MAX_UPLOAD_BYTES:
                    raise ValueError(f"Upload exceeds {MAX_UPLOAD_MB} MB limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        meta["bytes_received"] = received
        save_upload_meta(meta)
        return JSONResponse(finalize_upload_session(upload_id))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

async def upload_init_endpoint(request: Request):
    try:
        payload = await request.json()
        filename = payload.get("filename")
        if not filename:
            return JSONResponse(
                {"ok": False, "error": "filename is required"}, status_code=400
            )
        meta = create_upload_session(
            filename=filename,
            destination=payload.get("destination", "."),
            expected_bytes=payload.get("expected_bytes"),
            expected_sha256=payload.get("sha256"),
            mime_type=payload.get("mime_type"),
        )
        return JSONResponse({
            "ok": True, "upload_id": meta["upload_id"],
            "chunk_size": UPLOAD_CHUNK_BYTES,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "destination": meta["destination"], "filename": meta["filename"],
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

async def upload_chunk_endpoint(request: Request):
    upload_id = request.path_params.get("upload_id")
    try:
        meta = load_upload_meta(upload_id)
        if meta.get("complete"):
            return JSONResponse(upload_response(meta, "already_complete"))
        data_file = upload_session_data(upload_id)
        with UPLOAD_LOCK:
            current = data_file.stat().st_size if data_file.exists() else 0
            offset_header = request.headers.get("x-upload-offset")
            if offset_header is not None and int(offset_header) != current:
                return JSONResponse(
                    {"ok": False, "error": "offset mismatch",
                     "expected_offset": current},
                    status_code=409,
                )
            total = current
            with open(data_file, "ab") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        output.truncate(current)
                        raise ValueError(f"Upload exceeds {MAX_UPLOAD_MB} MB limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            meta["bytes_received"] = total
            meta["status"] = "uploading"
            save_upload_meta(meta)
        return JSONResponse({
            "ok": True, "upload_id": upload_id,
            "bytes_received": total, "next_offset": total, "complete": False,
        })
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "upload session not found"}, status_code=404
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

async def upload_status_endpoint(request: Request):
    upload_id = request.path_params.get("upload_id")
    try:
        return JSONResponse(upload_response(load_upload_meta(upload_id), "status"))
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "upload session not found"}, status_code=404
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

async def upload_finalize_endpoint(request: Request):
    upload_id = request.path_params.get("upload_id")
    try:
        return JSONResponse(finalize_upload_session(upload_id))
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "upload session not found"}, status_code=404
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

# ============================================================
# FILE OPERATIONS
# ============================================================

@mcp.tool()
def write_file(path: str, content: str, base64_encoded: bool = False) -> str:
    """Write content to a file."""
    target = safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if base64_encoded:
        try:
            data = base64.b64decode(content, validate=True)
            if len(data) > MAX_UPLOAD_BYTES:
                return f"ERROR: content exceeds {MAX_UPLOAD_MB} MB limit"
            atomic_write_bytes(target, data)
        except Exception as exc:
            return f"ERROR: invalid base64 data: {exc}"
    else:
        target.write_text(content, encoding="utf-8")
    return f"Wrote {target.stat().st_size} bytes to {path}"

@mcp.tool()
def read_file(path: str, base64_encoded: bool = False) -> str:
    """Read a file's contents."""
    target = safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if target.is_dir():
        return f"ERROR: {path} is a directory"
    if base64_encoded:
        return base64.b64encode(target.read_bytes()).decode()
    return target.read_text(errors="replace")

@mcp.tool()
def edit_file(path: str, old_str: str, new_str: str) -> str:
    """Replace an exact string in a file."""
    target = safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    text = target.read_text()
    count = text.count(old_str)
    if count == 0:
        return "ERROR: old_str not found"
    if count > 1:
        return f"ERROR: old_str occurs {count} times"
    target.write_text(text.replace(old_str, new_str, 1))
    return f"Edited {path}"

@mcp.tool()
def delete_file(path: str) -> str:
    """Delete a file or directory."""
    target = safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return f"Deleted {path}"

@mcp.tool()
def move_file(source: str, destination: str) -> str:
    """Move a file or directory."""
    src = safe_path(source)
    dst = safe_path(destination)
    if not src.exists():
        return f"ERROR: {source} does not exist"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {source} -> {destination}"

@mcp.tool()
def copy_file(source: str, destination: str) -> str:
    """Copy a file or directory."""
    src = safe_path(source)
    dst = safe_path(destination)
    if not src.exists():
        return f"ERROR: {source} does not exist"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return f"Copied {source} -> {destination}"

@mcp.tool()
def list_files(path: str = ".") -> str:
    """List files in a directory."""
    target = safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if not target.is_dir():
        return f"ERROR: {path} is not a directory"
    rows = []
    for item in sorted(target.iterdir()):
        if item.is_dir():
            rows.append(f"DIR\t\t{item.name}")
        else:
            rows.append(f"FILE\t{item.stat().st_size}\t{item.name}")
    return "\n".join(rows) if rows else "(empty)"

@mcp.tool()
def search_files(pattern: str, path: str = ".", max_results: int = 100) -> str:
    """Search file contents with regex."""
    target = safe_path(path)
    regex = re.compile(pattern)
    hits = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "__pycache__", "node_modules", ".jobs", ".uploads", ".tmp"}]
        for filename in files:
            file_path = Path(root) / filename
            try:
                with open(file_path, "r", errors="ignore") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if regex.search(line):
                            hits.append(
                                f"{file_path.relative_to(WORKDIR)}:{line_number}:"
                                f"{line.strip()[:300]}"
                            )
                            if len(hits) >= max_results:
                                return "\n".join(hits)
            except (PermissionError, OSError):
                pass
    return "\n".join(hits) if hits else "(no matches)"

@mcp.tool()
def file_info(path: str) -> str:
    """Get file metadata."""
    target = safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if not target.is_file():
        return f"ERROR: {path} is not a file"
    stat = target.stat()
    return json.dumps({
        "path": path, "name": target.name, "bytes": stat.st_size,
        "mime_type": mimetypes.guess_type(target.name)[0]
        or "application/octet-stream",
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }, indent=2)

@mcp.tool()
def verify_file(path: str) -> str:
    """Verify a file and get its SHA-256."""
    target = safe_path(path)
    if not target.exists():
        return json.dumps({"ok": False, "error": "file does not exist"}, indent=2)
    if not target.is_file():
        return json.dumps({"ok": False, "error": "not a file"}, indent=2)
    stat = target.stat()
    return json.dumps({
        "ok": True, "path": relative_path(target), "name": target.name,
        "bytes": stat.st_size, "sha256": sha256_file(target),
        "mime_type": mimetypes.guess_type(target.name)[0]
        or "application/octet-stream",
    }, indent=2)

@mcp.tool()
def download_url(url: str, save_as: str) -> str:
    """Download a file from the internet into the workspace."""
    target = safe_path(save_as)
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "personal-vps-agent/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.download")
            total = 0
            try:
                with open(temp, "wb") as output:
                    while True:
                        chunk = response.read(UPLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise ValueError(
                                f"Download exceeds {MAX_UPLOAD_MB} MB limit"
                            )
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, target)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass
    except urllib.error.URLError as exc:
        return f"ERROR: download failed: {exc}"
    return f"Downloaded {total} bytes -> {save_as}"

@mcp.tool()
def download_file(path: str) -> str:
    """Get a browser download URL for a file on the VPS."""
    target = safe_path(path)
    if not target.exists():
        return f"ERROR: {path} does not exist"
    if not target.is_file():
        return f"ERROR: {path} is not a file"
    if not PUBLIC_BASE_URL:
        return "ERROR: MCP_PUBLIC_BASE_URL is not configured"
    return f"{PUBLIC_BASE_URL}/files/{quote(relative_path(target), safe='/')}"

@mcp.tool()
def zip_directory(path: str, output: str) -> str:
    """Zip a directory."""
    source = safe_path(path)
    destination = safe_path(output)
    if not source.exists():
        return f"ERROR: {path} does not exist"
    if not source.is_dir():
        return f"ERROR: {path} is not a directory"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in source.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(source.parent))
    return f"Created {output} ({destination.stat().st_size} bytes)"

# ============================================================
# HTTP: FILE DOWNLOAD / HEALTH
# ============================================================

async def download_endpoint(request: Request):
    path = request.path_params.get("path", "")
    if not path:
        return PlainTextResponse("File path required", status_code=400)
    try:
        target = safe_path(path)
    except ValueError:
        return PlainTextResponse("Invalid path", status_code=400)
    if not target.exists():
        return PlainTextResponse("File not found", status_code=404)
    if not target.is_file():
        return PlainTextResponse("Not a file", status_code=400)
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=mime, filename=target.name)

async def health_endpoint(request: Request):
    try:
        stat = shutil.disk_usage(WORKDIR)
        return JSONResponse({
            "ok": True, "service": "personal-vps",
            "mcp": "/mcp", "upload_page": "/",
            "workspace": str(WORKDIR),
            "max_upload_mb": MAX_UPLOAD_MB,
            "disk_free_bytes": stat.free,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

# ============================================================
# CLEANUP LOOP + ROUTES
# ============================================================

def cleanup_loop():
    while True:
        try:
            cleanup_stale_uploads()
        except Exception:
            pass
        time.sleep(15 * 60)

threading.Thread(target=cleanup_loop, daemon=True).start()

mcp.custom_route("/", methods=["GET"])(upload_page_endpoint)
mcp.custom_route("/upload-page", methods=["GET"])(upload_page_endpoint)
mcp.custom_route("/upload", methods=["POST"])(upload_endpoint)
mcp.custom_route("/upload/raw", methods=["PUT", "POST"])(upload_raw_endpoint)
mcp.custom_route("/upload/init", methods=["POST"])(upload_init_endpoint)
mcp.custom_route("/upload/{upload_id}/chunk", methods=["PUT", "PATCH", "POST"])(upload_chunk_endpoint)
mcp.custom_route("/upload/{upload_id}", methods=["GET"])(upload_status_endpoint)
mcp.custom_route("/upload/{upload_id}/finalize", methods=["POST"])(upload_finalize_endpoint)
mcp.custom_route("/files/{path:path}", methods=["GET"])(download_endpoint)
mcp.custom_route("/health", methods=["GET"])(health_endpoint)

# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("PERSONAL VPS AGENT")
    print(f"Workspace: {WORKDIR}")
    print(f"Port:      {PORT}")
    print("=" * 50)
    mcp.run(transport="streamable-http")
