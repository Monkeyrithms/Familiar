"""
Conversation persistence — thin wrapper around core.database.

All storage is SQLite with FTS5 indexing for fast search.
JSON files are auto-migrated on first run.
"""

import time
import uuid

from core.database import (
    list_conversations,
    save_conversation,
    load_conversation,
    get_conversation_meta,
    delete_conversation,
    rename_conversation,
    set_conversation_workspace,
    set_conversation_cwd,
    set_conversation_model,
    set_conversation_provider,
    set_conversation_streams,
    get_conversation_streams,
    search_conversations,
    save_chat_image,
    load_chat_image,
    get_chat_image_path,
)

# Re-export everything so existing imports keep working
__all__ = [
    "list_conversations",
    "save_conversation",
    "load_conversation",
    "get_conversation_meta",
    "delete_conversation",
    "rename_conversation",
    "set_conversation_workspace",
    "set_conversation_cwd",
    "set_conversation_model",
    "set_conversation_streams",
    "get_conversation_streams",
    "search_conversations",
    "save_chat_image",
    "load_chat_image",
    "get_chat_image_path",
    "new_conversation_id",
]


def new_conversation_id() -> str:
    """Generate a unique conversation ID."""
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
