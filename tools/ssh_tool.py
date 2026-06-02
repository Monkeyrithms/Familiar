"""
SSH tool — execute commands on remote machines.
Uses paramiko if available, falls back to system ssh command.
"""

import json
import subprocess
from tools.registry import registry


def ssh_exec(host: str, command: str, user: str = "", port: int = 22,
             key_path: str = "", password: str = "", timeout: int = 30) -> str:
    """Execute a command on a remote machine via SSH."""

    # Try paramiko first (proper SSH library)
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {"hostname": host, "port": port, "timeout": timeout}
        if user:
            connect_kwargs["username"] = user
        if key_path:
            connect_kwargs["key_filename"] = key_path
        elif password:
            connect_kwargs["password"] = password

        client.connect(**connect_kwargs)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        client.close()

        from core.redact import redact
        return redact(json.dumps({
            "stdout": out[:10000],
            "stderr": err[:5000] if err else None,
            "exit_code": exit_code,
            "host": host,
        }, ensure_ascii=False))

    except ImportError:
        pass
    except Exception as e:
        return json.dumps({"error": f"SSH error: {e}"})

    # Fallback: system ssh command
    try:
        ssh_cmd = ["ssh"]
        if port != 22:
            ssh_cmd.extend(["-p", str(port)])
        if key_path:
            ssh_cmd.extend(["-i", key_path])
        if user:
            ssh_cmd.append(f"{user}@{host}")
        else:
            ssh_cmd.append(host)
        ssh_cmd.append(command)

        proc = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")

        from core.redact import redact
        return redact(json.dumps({
            "stdout": proc.stdout[:10000],
            "stderr": proc.stderr[:5000] if proc.stderr else None,
            "exit_code": proc.returncode,
            "host": host,
        }, ensure_ascii=False))
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"SSH timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": f"SSH fallback error: {e}"})


registry.register(
    name="ssh",
    description=(
        "Exec remote cmd via SSH. paramiko → fallback system ssh. Key | password auth."
    ),
    parameters={
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Hostname | IP."},
            "command": {"type": "string", "description": "Remote cmd."},
            "user": {"type": "string", "description": "Username."},
            "port": {"type": "integer", "description": "Port (default 22)."},
            "key_path": {"type": "string", "description": "Private key path."},
            "password": {"type": "string", "description": "Password (key preferred)."},
            "timeout": {"type": "integer", "description": "Seconds (default 30)."},
        },
        "required": ["host", "command"],
    },
    execute=ssh_exec,
)
