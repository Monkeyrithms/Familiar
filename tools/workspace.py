"""
Workspace tool - create, list, and switch workspaces.
The agent can manage its own working environment.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from core.proc import NO_WINDOW
from core.workspace_paths import resolve_workspace_entry_path, to_config_workspace_path
from tools.registry import registry


def workspace(action: str, name: str = None, path: str = None,
              create_venv: bool = False) -> str:
    """Manage workspaces."""
    from core.agent import load_config, save_config  # lazy: avoid circular import
    cfg = load_config()
    workspaces = cfg.get("workspaces", {})

    if action == "list":
        if not workspaces:
            return json.dumps({"workspaces": [], "message": "No workspaces configured."})
        ws_list = []
        for ws_name, ws_data in workspaces.items():
            raw = ws_data.get("path", "")
            rp = resolve_workspace_entry_path(str(raw) if raw else "")
            ws_list.append({
                "name": ws_name,
                "path": ws_data.get("path", ""),
                "venv": ws_data.get("venv", ""),
                "exists": rp.is_dir(),
            })
        return json.dumps({"workspaces": ws_list}, ensure_ascii=False)

    if action == "create":
        if not name:
            return json.dumps({"error": "name is required for create."})
        if not path:
            return json.dumps({"error": "path is required for create."})

        full_path = str(Path(path).resolve())
        os.makedirs(full_path, exist_ok=True)

        ws_entry = {"path": to_config_workspace_path(full_path), "venv": ""}

        if create_venv:
            venv_path = os.path.join(full_path, ".venv")
            result = subprocess.run(
                [sys.executable, "-m", "venv", venv_path],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW)  # no console flash on Windows
            if result.returncode == 0:
                ws_entry["venv"] = to_config_workspace_path(venv_path)
            else:
                return json.dumps({"error": f"venv creation failed: {result.stderr}"})

        workspaces[name] = ws_entry
        cfg["workspaces"] = workspaces
        save_config(cfg)

        msg = f'Created workspace "{name}" at {full_path}'
        if create_venv:
            msg += f" with venv at {ws_entry['venv']}"
        return json.dumps({"status": msg})

    if action == "switch":
        if not name:
            return json.dumps({"error": "name is required for switch."})
        if name not in workspaces:
            available = ", ".join(workspaces.keys()) or "(none)"
            return json.dumps({"error": f'Workspace "{name}" not found. Available: {available}'})
        # The actual switch happens in the agent's tool dispatch — it reads
        # the result and updates the conversation's workspace.
        ws = workspaces[name]
        return json.dumps({
            "status": f'Switched to workspace "{name}"',
            "path": ws.get("path", ""),
            "switched_to": name,
        })

    if action == "add":
        if not name:
            return json.dumps({"error": "name is required for add."})
        if not path:
            return json.dumps({"error": "path is required for add."})
        full_path = str(Path(path).resolve())
        if not Path(full_path).is_dir():
            return json.dumps({"error": f'Path "{full_path}" does not exist.'})
        workspaces[name] = {"path": to_config_workspace_path(full_path), "venv": ""}
        cfg["workspaces"] = workspaces
        save_config(cfg)
        return json.dumps({"status": f'Added workspace "{name}" pointing to {full_path}'})

    if action == "remove":
        if not name:
            return json.dumps({"error": "name is required for remove."})
        if name not in workspaces:
            return json.dumps({"error": f'Workspace "{name}" not found.'})
        workspaces.pop(name)
        cfg["workspaces"] = workspaces
        save_config(cfg)
        return json.dumps({"status": f'Removed workspace "{name}" from registry (files not deleted).'})

    return json.dumps({"error": f'Unknown action "{action}". Use: list, create, add, switch, remove.'})


registry.register(
    name="workspace",
    description=(
        "Workspace mgmt. list|create|add|switch|remove."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "create", "add", "switch", "remove"], "description": "Op."},
            "name": {"type": "string", "description": "Workspace name."},
            "path": {"type": "string", "description": "Folder path (create/add)."},
            "create_venv": {"type": "boolean", "description": "Make .venv inside (create)."},
        },
        "required": ["action"],
    },
    execute=workspace,
)
