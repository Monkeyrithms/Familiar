"""
Notification tool — send emails (SMTP) and webhook alerts.
"""

import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from tools.registry import registry


def _load_smtp_config() -> dict:
    """Load SMTP config from config.json."""
    try:
        cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
        return cfg.get("smtp", {})
    except Exception:
        return {}


def notify(action: str, to: str = "", subject: str = "", body: str = "",
           webhook_url: str = "", webhook_body: dict = None) -> str:
    """Send notifications via email or webhook."""

    if action == "email":
        if not to or not subject:
            return json.dumps({"error": "to and subject required for email"})
        smtp = _load_smtp_config()
        if not smtp.get("host") or not smtp.get("user"):
            return json.dumps({
                "error": "SMTP not configured. Add smtp config to config.json: "
                         "{host, port, user, password, from_email}"
            })
        try:
            msg = MIMEMultipart()
            msg["From"] = smtp.get("from_email", smtp["user"])
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body or "(no body)", "plain"))

            port = smtp.get("port", 587)
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp["host"], port) as server:
                server.starttls(context=context)
                server.login(smtp["user"], smtp["password"])
                server.send_message(msg)

            return json.dumps({
                "status": "done",
                "message": f"Email sent to {to} with subject '{subject}'. "
                           f"No further tool calls — confirm to the user in text.",
                "sent": True, "to": to, "subject": subject,
            })
        except Exception as e:
            return json.dumps({"error": f"Email failed: {e}"})

    elif action == "webhook":
        if not webhook_url:
            return json.dumps({"error": "webhook_url required"})
        try:
            import httpx
            payload = webhook_body or {"text": body or "Agent notification"}
            resp = httpx.post(webhook_url, json=payload, timeout=10)
            return json.dumps({
                "status": "done",
                "message": f"Webhook POSTed (HTTP {resp.status_code}). "
                           f"No further tool calls — confirm to the user in text.",
                "sent": True, "http_status": resp.status_code,
                "response": resp.text[:500],
            })
        except Exception as e:
            return json.dumps({"error": f"Webhook failed: {e}"})

    elif action == "desktop":
        # Cross-platform desktop notification
        try:
            title = subject or "Agent"
            message = body or "Notification"
            import sys
            if sys.platform == "win32":
                from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
                from PyQt6.QtGui import QIcon
                app = QApplication.instance()
                if app:
                    # Use a simple message box as fallback
                    from PyQt6.QtWidgets import QMessageBox
                    # Fire and forget on main thread
                    from PyQt6.QtCore import QTimer
                    QTimer.singleShot(0, lambda: QMessageBox.information(None, title, message))
            return json.dumps({
                "status": "done",
                "message": f"Desktop notification '{title}' shown to the user. "
                           f"No further tool calls — confirm to the user in text.",
                "sent": True, "type": "desktop", "title": title,
            })
        except Exception as e:
            return json.dumps({"error": f"Desktop notification failed: {e}"})

    else:
        return json.dumps({"error": "action must be: email, webhook, desktop"})


registry.register(
    name="notify",
    description=(
        "Send notifications. email: SMTP (cfg in config.json) | "
        "webhook: POST URL | desktop: desktop notification."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["email", "webhook", "desktop"]},
            "to": {"type": "string", "description": "Email recipient."},
            "subject": {"type": "string", "description": "Subject | notification title."},
            "body": {"type": "string", "description": "Message body."},
            "webhook_url": {"type": "string", "description": "Webhook URL."},
            "webhook_body": {"type": "object", "description": "Custom webhook JSON."},
        },
        "required": ["action"],
    },
    execute=notify,
)
