"""
Hot-reload watcher for the tools/ folder.

Uses watchdog for instant OS-level file system notifications.
When a .py file is created, modified, or deleted, the corresponding
tool module is loaded, reloaded, or unregistered — live, no restart.

Usage:
    from tools.hot_reload import ToolWatcher
    watcher = ToolWatcher()
    watcher.start()   # background thread
    watcher.stop()    # cleanup
"""

import importlib
import sys
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent

TOOLS_DIR = Path(__file__).parent

# Files that should NOT be reloaded.
#  - Infrastructure: registry, hot_reload itself, package init.
#  - Signal-bridge modules: reloading re-runs `bridge = _XyzBridge()` at module
#    scope, creating a NEW QObject. The UI keeps holding the connection to the
#    OLD one, so signals fire into the void (file viewer stops flipping to
#    edited files, terminal stops surfacing, etc.). These must stay singletons
#    for the life of the process.
SKIP_FILES = {
    "__init__.py", "hot_reload.py", "registry.py",
    "file_viewer.py", "workspace_terminal.py", "workspace_browser.py",
    "screenshot.py", "subagent_tool.py", "chart.py",
    "workspace_sound_watch.py",
}


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[HotReload {ts}] {msg}", flush=True)


class _ToolEventHandler(FileSystemEventHandler):
    """Handles file system events for .py files in the tools directory."""

    def __init__(self):
        super().__init__()
        self._debounce: dict[str, float] = {}  # path -> last event time

    def _should_handle(self, path: str) -> bool:
        """Filter: only .py files, not infrastructure, debounce rapid saves."""
        p = Path(path)
        if p.suffix != ".py":
            return False
        if p.name in SKIP_FILES:
            return False
        if p.parent != TOOLS_DIR:
            return False  # ignore subdirectories
        # Debounce: ignore events within 1s of the last one for same file
        now = time.time()
        last = self._debounce.get(path, 0)
        if now - last < 1.0:
            return False
        self._debounce[path] = now
        return True

    def _module_name(self, path: str) -> str:
        return f"tools.{Path(path).stem}"

    def on_created(self, event):
        if event.is_directory or not self._should_handle(event.src_path):
            return
        mod_name = self._module_name(event.src_path)
        _log(f"New tool: {Path(event.src_path).name}")
        try:
            importlib.import_module(mod_name)
            _log(f"Loaded {mod_name}")
        except Exception as e:
            _log(f"Failed to load {mod_name}: {e}")

    def on_modified(self, event):
        if event.is_directory or not self._should_handle(event.src_path):
            return
        mod_name = self._module_name(event.src_path)
        mod = sys.modules.get(mod_name)
        if mod is None:
            # Not loaded yet — treat as new
            self.on_created(event)
            return
        _log(f"Modified: {Path(event.src_path).name}")
        try:
            importlib.reload(mod)
            _log(f"Reloaded {mod_name}")
        except Exception as e:
            _log(f"Failed to reload {mod_name}: {e}")

    def on_deleted(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix != ".py" or p.name in SKIP_FILES:
            return

        # Wait briefly — editors often delete then recreate on save
        import time
        time.sleep(0.5)
        if p.exists():
            return  # file came back — editor save pattern, not a real delete

        mod_name = self._module_name(event.src_path)
        _log(f"Deleted: {p.name}")

        from tools.registry import registry
        to_remove = []
        for name, tool in registry._tools.items():
            fn = tool.get("execute")
            if fn and hasattr(fn, "__module__") and fn.__module__ == mod_name:
                to_remove.append(name)
        for name in to_remove:
            registry.unregister(name)
            _log(f"Unregistered tool: {name}")

        sys.modules.pop(mod_name, None)


class ToolWatcher:
    """Watches the tools/ directory for changes using OS-level notifications."""

    def __init__(self):
        self._observer = Observer()
        self._handler = _ToolEventHandler()

    def start(self):
        """Start watching in a background daemon thread."""
        self._observer.schedule(self._handler, str(TOOLS_DIR), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        _log(f"Watching {TOOLS_DIR} (watchdog)")

    def stop(self):
        """Stop watching."""
        self._observer.stop()
        self._observer.join(timeout=5)
        _log("Stopped")