"""
Chatterbox backend. Spawns a persistent daemon subprocess inside the isolated
venv; the daemon loads the model once and then serves synth requests on stdin.
Subsequent calls reuse the daemon (no model-reload cost).
"""

import atexit
import json
import os
import queue as _queue
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Optional, Tuple

from . import chatterbox_installer as inst

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "audio_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
WORKER = Path(__file__).parent / "_chatterbox_worker.py"
PID_FILE = Path(__file__).parent.parent.parent / "data" / "chatterbox_env" / ".daemon.pid"

_daemon_lock = threading.Lock()
_daemon_proc: Optional[subprocess.Popen] = None
_daemon_ready: bool = False
_daemon_out_queue: Optional["_queue.Queue[str]"] = None
_daemon_err_queue: Optional["_queue.Queue[str]"] = None
_job_handle = None  # Win32 Job Object — keeps alive, auto-kills children on app death
_req_lock = threading.Lock()  # serialize requests through the single stdin pipe


def _reader_thread(stream, q: "_queue.Queue[str]"):
    """Push each line from `stream` into `q`. Runs until stream closes."""
    try:
        for line in iter(stream.readline, ""):
            if line == "":
                break
            q.put(line)
    except Exception:
        pass
    finally:
        try: q.put("")  # sentinel: EOF
        except Exception:
            traceback.print_exc()


def _create_win32_job():
    """Create a Win32 Job Object configured to kill all assigned processes when
    the job handle closes (i.e. when the Python interpreter exits, even hard)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None, None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job); return None, None
        return job, k32
    except Exception:
        return None, None


def _assign_to_job(pid: int):
    """Assign the given process to the global job object."""
    global _job_handle
    if os.name != "nt":
        return
    if _job_handle is None:
        job, _ = _create_win32_job()
        if job is None:
            return
        _job_handle = job
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        PROCESS_SET_QUOTA = 0x0100
        handle = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
        if handle:
            k32.AssignProcessToJobObject(_job_handle, handle)
            k32.CloseHandle(handle)
    except Exception:
        pass


def _kill_stale_daemon():
    """If a PID file exists from a previous (hard-killed) run, kill that process."""
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        PID_FILE.unlink(missing_ok=True); return
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 0x0001
            PROCESS_QUERY_LIMITED = 0x1000
            handle = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED, False, pid)
            if handle:
                # Confirm it's a python.exe before killing (paranoid: PIDs are reused)
                name_buf = ctypes.create_unicode_buffer(260)
                try:
                    psapi = ctypes.windll.psapi
                    psapi.GetModuleFileNameExW(handle, None, name_buf, 260)
                    if "python" in name_buf.value.lower():
                        k32.TerminateProcess(handle, 1)
                except Exception:
                    k32.TerminateProcess(handle, 1)
                k32.CloseHandle(handle)
        else:
            os.kill(pid, 15)  # SIGTERM
    except Exception:
        pass
    PID_FILE.unlink(missing_ok=True)


def is_loaded() -> bool:
    """True if the daemon is up and has finished loading the model.
    Uses a non-blocking lock so the UI's polling timer can't stall the event
    loop while the loader thread is holding the lock during model load."""
    got = _daemon_lock.acquire(blocking=False)
    if not got:
        # Loader/shutdown is mid-flight; state is transient — report not-loaded
        # rather than blocking the caller (likely the Qt main thread).
        return False
    try:
        return (_daemon_proc is not None and _daemon_proc.poll() is None
                and _daemon_ready)
    finally:
        _daemon_lock.release()


def load() -> Tuple[bool, str]:
    """Explicitly spin up the daemon (pre-warm). Returns (ok, error_or_empty)."""
    _, err = _get_daemon()
    return (err is None), (err or "")


def unload():
    """Tear down the daemon and free its model memory."""
    shutdown()


def _spawn_daemon() -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    if not inst.is_installed():
        return None, "Chatterbox is not installed. Open Settings → Voice to install it."
    # Kill any orphan from a prior hard-killed Agent session
    _kill_stale_daemon()
    env = os.environ.copy()
    env["HF_HOME"] = str(inst.MODEL_DIR)
    env["HUGGINGFACE_HUB_CACHE"] = str(inst.MODEL_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    creation_flags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW — no console flash; we'll Job-assign right after spawn
        creation_flags = 0x08000000
    try:
        p = subprocess.Popen(
            [inst.venv_python(), "-u", str(WORKER), "--daemon"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
            creationflags=creation_flags,
        )
    except Exception as e:
        return None, f"Chatterbox daemon launch failed: {e}"
    # Assign to Windows Job Object (auto-kill on parent death) + write PID file
    _assign_to_job(p.pid)
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(p.pid), encoding="utf-8")
    except Exception:
        pass
    # Wrap stdout/stderr in reader threads so synth() can use queue.get(timeout=).
    global _daemon_out_queue, _daemon_err_queue
    _daemon_out_queue = _queue.Queue()
    _daemon_err_queue = _queue.Queue()
    threading.Thread(target=_reader_thread, args=(p.stdout, _daemon_out_queue),
                     daemon=True).start()
    threading.Thread(target=_reader_thread, args=(p.stderr, _daemon_err_queue),
                     daemon=True).start()
    # Wait for {"ready": true} on stdout — first load can be slow (~15-30s)
    deadline = time.time() + 120
    while time.time() < deadline:
        if p.poll() is not None:
            err = _drain_err()
            return None, f"daemon exited before ready. stderr: {err[:400]}"
        try:
            line = _daemon_out_queue.get(timeout=0.5)
        except _queue.Empty:
            continue
        if line == "":
            err = _drain_err()
            return None, f"daemon stdout closed before ready. stderr: {err[:400]}"
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("ready"):
            return p, None
        if "error" in msg:
            return None, f"daemon failed to load: {msg.get('error','')[:300]}"
    try: p.terminate()
    except Exception:
        traceback.print_exc()
    return None, "daemon timed out during load (>120s)"


def _drain_err() -> str:
    """Pull everything currently in the stderr queue as a string."""
    if _daemon_err_queue is None:
        return ""
    lines = []
    try:
        while True:
            line = _daemon_err_queue.get_nowait()
            if line:
                lines.append(line.rstrip("\n"))
    except _queue.Empty:
        pass
    return "\n".join(lines)


def _get_daemon() -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    global _daemon_proc, _daemon_ready
    with _daemon_lock:
        if _daemon_proc is not None and _daemon_proc.poll() is None and _daemon_ready:
            return _daemon_proc, None
        # Clean up any dead instance
        if _daemon_proc is not None and _daemon_proc.poll() is not None:
            _daemon_proc = None
            _daemon_ready = False
        _daemon_proc, err = _spawn_daemon()
        if err:
            _daemon_ready = False
            return None, err
        _daemon_ready = True
        atexit.register(shutdown)
        return _daemon_proc, None


def shutdown():
    """Terminate the daemon cleanly."""
    global _daemon_proc, _daemon_ready
    with _daemon_lock:
        p = _daemon_proc
        _daemon_proc = None
        _daemon_ready = False
    if p is None:
        PID_FILE.unlink(missing_ok=True)
        return
    try:
        if p.poll() is None and p.stdin:
            try:
                p.stdin.write(json.dumps({"action": "shutdown"}) + "\n")
                p.stdin.flush()
            except Exception:
                pass
        try: p.wait(timeout=3)
        except Exception: p.terminate()
    except Exception:
        pass
    PID_FILE.unlink(missing_ok=True)


def synth(text: str, *, voice: str = "", speed: int = 0,
          output_path: str = "") -> Tuple[str, Optional[str]]:
    if not output_path:
        ts = time.strftime("%Y%m%d_%H%M%S_%f")
        output_path = str(CACHE_DIR / f"tts_{ts}.wav")

    p, err = _get_daemon()
    if err:
        return "", err
    assert p and p.stdin and p.stdout

    req = json.dumps({"text": text, "voice_ref": voice, "output_path": output_path})
    # Time budget scales with text length — long replies are chunked in the daemon
    # and each ~280-char chunk takes ~1-3s on GPU, ~10-30s on CPU. Floor at 3 min.
    TIMEOUT_SEC = max(180, len(text) // 2)
    with _req_lock:
        try:
            p.stdin.write(req + "\n")
            p.stdin.flush()
        except Exception as e:
            _mark_dead()
            return "", f"Chatterbox daemon write failed: {e}"
        if _daemon_out_queue is None:
            return "", "daemon queue not initialized"
        deadline = time.time() + TIMEOUT_SEC
        resp_line = ""
        while True:
            if p.poll() is not None:
                err_txt = _drain_err()
                _mark_dead()
                return "", f"Chatterbox daemon exited. stderr: {(err_txt or '')[:300]}"
            remaining = deadline - time.time()
            if remaining <= 0:
                err_txt = _drain_err()
                return "", (f"Chatterbox timed out after {TIMEOUT_SEC}s with no "
                            f"response from daemon. stderr tail: {err_txt[-300:]}")
            try:
                line = _daemon_out_queue.get(timeout=min(1.0, remaining))
            except _queue.Empty:
                continue
            if line == "":  # EOF sentinel
                _mark_dead()
                return "", "daemon stdout closed mid-request"
            line = line.strip()
            if not line:
                continue
            resp_line = line
            break

    try:
        result = json.loads(resp_line)
    except Exception as e:
        return "", f"bad daemon response: {e}: {resp_line[:200]}"
    if "error" in result:
        trace = result.get("trace", "")
        try:
            log = Path(__file__).parent.parent.parent / "logs" / "chatterbox_runtime.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(f"{result.get('error','')}\n\n{trace}\n", encoding="utf-8")
        except Exception:
            pass
        return "", f"Chatterbox error: {result['error']}  (trace in logs/chatterbox_runtime.log)"
    if not Path(output_path).exists():
        return "", "Chatterbox produced no file"
    return output_path, None


def _mark_dead():
    global _daemon_proc, _daemon_ready, _daemon_out_queue, _daemon_err_queue
    with _daemon_lock:
        _daemon_proc = None
        _daemon_ready = False
        _daemon_out_queue = None
        _daemon_err_queue = None
