"""
Notification tool — send emails (SMTP), webhook alerts, desktop notifications.
"""

import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry


class _NotifyBridge(QObject):
    """Created at import time on the main thread. Worker threads emit
    show_requested; the popup is built and shown on the GUI thread."""

    show_requested = pyqtSignal(str, str)  # title, message

    def __init__(self):
        super().__init__()
        self.show_requested.connect(self._show)
        self._boxes = []  # keep refs so non-modal boxes aren't GC'd early

    def _show(self, title: str, message: str):
        try:
            from PyQt6.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Icon.Information, title, message)
            box.setModal(False)  # never block the GUI under an agent tool
            box.finished.connect(lambda *_ , b=box: self._boxes.remove(b)
                                 if b in self._boxes else None)
            self._boxes.append(box)
            box.show()
        except Exception:
            pass


_notify_bridge = _NotifyBridge()


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
        # Cross-platform desktop notification. This runs on a WORKER thread:
        # Qt widgets/timers must not be created here. Marshal to the GUI
        # thread via the bridge below (the old QTimer.singleShot-from-worker
        # never fired reliably and was thread-unsafe).
        try:
            title = subject or "Agent"
            message = body or "Notification"
            from PyQt6.QtWidgets import QApplication
            if QApplication.instance():
                _notify_bridge.show_requested.emit(title, message)
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
