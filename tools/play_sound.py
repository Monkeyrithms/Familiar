"""
Play sound tool — lets the LLM play MP3/WAV files from the sounds/ directory.

Sound is deferred: it plays when the response is delivered to the user,
not during the tool execution loop. This ensures the sound accompanies
the visible response rather than firing mid-processing.
"""

import json
from core.sounds import list_sounds, queue_deferred, SOUNDS_DIR
from tools.registry import registry


def play_sound(name: str = "", list: bool = False) -> str:
    """Queue a sound for playback or list available sounds."""
    if list or not name:
        available = list_sounds()
        if not available:
            return json.dumps({"error": "No sound files found in sounds/ directory."})
        return json.dumps({"available_sounds": available, "directory": str(SOUNDS_DIR)})

    # Verify the file exists before queuing
    path = SOUNDS_DIR / name
    if not path.exists():
        # Try without extension
        found = False
        for ext in ('.mp3', '.wav', '.ogg', '.wma'):
            if (SOUNDS_DIR / name).with_suffix(ext).exists():
                name = path.with_suffix(ext).name
                found = True
                break
        if not found:
            available = list_sounds()
            return json.dumps({
                "error": f"Sound '{name}' not found.",
                "available_sounds": available,
            })

    queue_deferred(name)
    return json.dumps({
        "status": "done",
        "message": f"Sound '{name}' is queued and will play when your reply is delivered. "
                   f"No further tool calls — respond to the user in text now.",
        "queued": name,
    })


registry.register(
    name="play_sound",
    description=(
        "Play sound FX from sounds/ dir. Plays when response shown (✗ immediate). "
        "list=true → list sounds. ✓ alerts, notifications, emphasis."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Filename (e.g. 'snapshot.mp3'). Omit → list.",
            },
            "list": {
                "type": "boolean",
                "description": "List sounds ✗ play.",
            },
        },
    },
    execute=play_sound,
)
