"""
Chatterbox installer — builds an isolated venv at data/chatterbox_env/ and
installs the heavy deps (torch, chatterbox-tts, etc.) without touching the
user's main Python environment. Emits Qt signals so the UI can show progress.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _HAS_QT = True
except Exception:
    _HAS_QT = False
    QObject = object
    def pyqtSignal(*a, **kw):
        return None

ROOT = Path(__file__).parent.parent.parent
VENV_DIR = ROOT / "data" / "chatterbox_env"
MODEL_DIR = ROOT / "data" / "chatterbox_models"
MARKER = VENV_DIR / ".ready"
WORKER = Path(__file__).parent / "_chatterbox_worker.py"

# Hide child console windows. Under pythonw (no console), a console subprocess
# otherwise allocates its OWN visible window — so venv/pip/worker each pop one.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def venv_python() -> str:
    """Path to the venv's Python executable."""
    if sys.platform == "win32":
        return str(VENV_DIR / "Scripts" / "python.exe")
    return str(VENV_DIR / "bin" / "python")


def is_installed() -> bool:
    return MARKER.exists() and Path(venv_python()).exists()


def has_gpu() -> tuple:
    """(has_cuda: bool, device_name: str). Works even before the venv exists,
    by asking the user's current Python."""
    try:
        import torch
        if torch.cuda.is_available():
            return True, torch.cuda.get_device_name(0)
    except Exception:
        pass
    # Last-resort: nvidia-smi on PATH
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=name",
                                       "--format=csv,noheader"], timeout=1, text=True,
                                      creationflags=_NO_WINDOW)
        name = out.strip().splitlines()[0] if out.strip() else ""
        return bool(name), name
    except Exception:
        return False, ""


def free_disk_gb() -> float:
    try:
        stat = shutil.disk_usage(str(ROOT))
        return stat.free / (1024 ** 3)
    except Exception:
        return -1.0


def uninstall():
    """Remove the venv and cached models."""
    shutil.rmtree(VENV_DIR, ignore_errors=True)
    shutil.rmtree(MODEL_DIR, ignore_errors=True)


class ChatterboxInstaller(QObject):
    """Runs the install in a background thread. Emits progress & finished signals."""

    progress = pyqtSignal(str, int) if _HAS_QT else None      # (phase, percent_or_-1)
    log = pyqtSignal(str) if _HAS_QT else None                # raw line for debug panel
    finished = pyqtSignal(bool, str) if _HAS_QT else None     # (success, error_msg)

    def __init__(self, parent=None):
        if _HAS_QT:
            super().__init__(parent)
        self._thread = None
        self._cancel = False
        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / "chatterbox_install.log"
        try:
            self._log_fh = open(self._log_path, "w", encoding="utf-8", buffering=1)
        except Exception:
            self._log_fh = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel = True

    def _emit_progress(self, phase: str, pct: int = -1):
        if _HAS_QT and self.progress:
            try: self.progress.emit(phase, pct)
            except Exception:
                traceback.print_exc()

    def _emit_log(self, line: str):
        if self._log_fh:
            try: self._log_fh.write(line + "\n")
            except Exception:
                traceback.print_exc()
        if _HAS_QT and self.log:
            try: self.log.emit(line)
            except Exception:
                traceback.print_exc()

    def _emit_done(self, ok: bool, msg: str = ""):
        if _HAS_QT and self.finished:
            try: self.finished.emit(ok, msg)
            except Exception:
                traceback.print_exc()

    def _run(self):
        try:
            self._emit_progress("Preparing workspace", 0)
            VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
            MODEL_DIR.mkdir(parents=True, exist_ok=True)

            if not Path(venv_python()).exists():
                r = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)],
                                   capture_output=True, text=True,
                                   creationflags=_NO_WINDOW)
                if r.returncode != 0:
                    self._emit_done(False, f"venv creation failed: {r.stderr}")
                    return
            self._emit_progress("Preparing workspace", 100)
            if self._cancel:
                self._emit_done(False, "Cancelled"); return

            self._emit_progress("Upgrading pip", 0)
            self._run_stream([venv_python(), "-m", "pip", "install", "--upgrade",
                              "pip", "wheel", "setuptools"], phase="Upgrading pip")
            if self._cancel:
                self._emit_done(False, "Cancelled"); return

            self._emit_progress("Installing components (downloads torch — can take many minutes)", -1)
            rc = self._run_stream(
                # Keep pip's progress bar ON so download % flows to the UI bar.
                [venv_python(), "-m", "pip", "install", "--progress-bar=on",
                 "chatterbox-tts"],
                phase="Installing components")
            if rc != 0:
                self._emit_done(False, "pip install failed — see log"); return
            if self._cancel:
                self._emit_done(False, "Cancelled"); return

            # GPU upgrade: chatterbox pins torch==2.6.0, but PyPI's default torch==2.6.0
            # is a CPU-only wheel. If a CUDA GPU is present, replace with CUDA wheels.
            # RTX 50-series (Blackwell) needs torch 2.7+ with CUDA 12.8.
            gpu, _ = has_gpu()
            if gpu:
                self._emit_progress("Installing GPU acceleration (~2.5 GB download)", -1)
                rc = self._run_stream(
                    [venv_python(), "-m", "pip", "install", "--progress-bar=on",
                     "--upgrade", "--force-reinstall",
                     "torch==2.7.1", "torchaudio==2.7.1",
                     "--index-url", "https://download.pytorch.org/whl/cu128"],
                    phase="Installing GPU acceleration")
                if rc != 0:
                    self._emit_log("[warn] GPU torch install failed — falling back to CPU")
            if self._cancel:
                self._emit_done(False, "Cancelled"); return

            # Pin setuptools<81 LAST — chatterbox's perth dep imports `pkg_resources`,
            # which setuptools 81+ removed. Any earlier pin gets re-bumped by
            # --force-reinstall of torch, so this must be the final dep step.
            self._emit_progress("Patching dependencies", -1)
            self._run_stream(
                [venv_python(), "-m", "pip", "install", "--progress-bar=off",
                 "setuptools<81"],
                phase="Patching dependencies")
            if self._cancel:
                self._emit_done(False, "Cancelled"); return

            self._emit_progress("Downloading voice model", 0)
            env = os.environ.copy()
            env["HF_HOME"] = str(MODEL_DIR)
            env["HUGGINGFACE_HUB_CACHE"] = str(MODEL_DIR)
            warmup_payload = json.dumps({"action": "warmup"})
            try:
                p = subprocess.Popen(
                    [venv_python(), "-u", str(WORKER)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, text=True,
                    bufsize=1, creationflags=_NO_WINDOW,
                )
                assert p.stdin and p.stdout
                p.stdin.write(warmup_payload); p.stdin.close()
                for line in p.stdout:
                    self._emit_log(line.rstrip())
                    if self._cancel:
                        p.terminate(); break
                p.wait()
                if p.returncode != 0:
                    self._emit_done(False, "model download failed — see log"); return
            except Exception as e:
                self._emit_done(False, f"model download error: {e}"); return

            MARKER.write_text("ready", encoding="utf-8")
            self._emit_progress("Done", 100)
            self._emit_done(True, "")
        except Exception as e:
            self._emit_done(False, f"Unexpected error: {e}")

    def _run_stream(self, cmd, phase: str) -> int:
        """Run a subprocess, stream stdout into log signal.
        Reads char-by-char so pip's \\r progress updates don't stall the UI."""
        import time as _time
        start_t = _time.time()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=0, env=env,
                                 creationflags=_NO_WINDOW)
        except Exception as e:
            self._emit_log(f"[launch error] {e}")
            return 1
        assert p.stdout

        buf = ""
        last_beat = _time.time()
        while True:
            if self._cancel:
                p.terminate(); break
            ch = p.stdout.read(1)
            if not ch:
                if p.poll() is not None:
                    break
                _time.sleep(0.05)
                # heartbeat so the user/terminal knows we're alive
                if _time.time() - last_beat > 5:
                    elapsed = int(_time.time() - start_t)
                    self._emit_log(f"[{phase}] still working… {elapsed}s elapsed")
                    self._emit_progress(phase, -1)
                    last_beat = _time.time()
                continue
            if ch in ("\n", "\r"):
                s = buf.strip()
                if s:
                    self._emit_log(s)
                    pct = _parse_pip_percent(s)
                    if pct >= 0:
                        self._emit_progress(phase, pct)
                    last_beat = _time.time()
                buf = ""
            else:
                buf += ch
                if len(buf) > 4096:  # runaway line guard
                    self._emit_log(buf); buf = ""
        if buf.strip():
            self._emit_log(buf.strip())
        p.wait()
        return p.returncode


def _parse_pip_percent(line: str) -> int:
    """Best-effort parse of pip's download progress lines (e.g. '45%|#### |')."""
    import re
    m = re.search(r"(\d{1,3})%", line)
    if not m:
        return -1
    try:
        v = int(m.group(1))
        return max(0, min(100, v))
    except Exception:
        return -1
