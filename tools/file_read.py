"""
File read tool - read files with line numbers.
Supports offset/limit for reading large files in chunks.
"""

import json
from pathlib import Path
from tools.registry import registry


def file_read(path: str, offset: int = None, limit: int = None) -> str:
    """Read a file and return contents with line numbers.
    Auto-detects and parses PDF, DOCX, XLSX, and PPTX files."""
    # Auto-detect binary document formats
    from tools.doc_parser import can_parse, parse_document
    if can_parse(path):
        result = parse_document(path)
        if "error" in result:
            return json.dumps(result)
        return json.dumps(result, ensure_ascii=False)

    p = Path(path)

    # Large-file fast path: when the caller bounds the read with offset/limit,
    # stream line-by-line and stop early instead of decoding the whole file
    # into memory. A full read of a multi-MB file is pure-Python work that holds
    # the GIL and stalls the UI thread; iterating the file object reads in
    # buffered chunks and releases the GIL on each underlying read().
    if limit is not None:
        start = max(0, (offset or 1) - 1)
        try:
            chunk: list[str] = []
            total = 0
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f):
                    total = idx + 1
                    if start <= idx < start + limit:
                        chunk.append(line.rstrip("\n"))
                    elif idx >= start + limit:
                        # Keep counting remaining lines cheaply for the meta line,
                        # but don't hold them in memory.
                        continue
        except Exception as e:
            return json.dumps({"error": f'Could not read "{path}": {e}'})

        if start >= total and total > 0:
            return json.dumps({
                "error": f'File has {total} lines but offset {offset} is beyond the end.'
            })
        end = min(start + limit, total)
        numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk))
        meta = f"\n\n(showing lines {start + 1}-{end} of {total})" if end < total else ""
        return json.dumps({"content": numbered + meta, "total_lines": total},
                          ensure_ascii=False)

    # No limit: read the whole file (caller wants all of it).
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f'Could not read "{path}": {e}'})

    lines = raw.split("\n")
    # Remove trailing empty line from trailing newline
    if lines and lines[-1] == "":
        lines.pop()

    total = len(lines)

    # Apply offset (1-based to 0-based)
    start = max(0, (offset or 1) - 1)
    if start >= total and total > 0:
        return json.dumps({
            "error": f'File has {total} lines but offset {offset} is beyond the end.'
        })

    end = total
    chunk = lines[start:end]

    numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk))

    meta = ""
    if end < total:
        meta = f"\n\n(showing lines {start + 1}-{end} of {total})"

    return json.dumps({"content": numbered + meta, "total_lines": total},
                      ensure_ascii=False)


registry.register(
    name="file_read",
    description=(
        "Read file. Text → numbered lines. PDF|DOCX|XLSX|PPTX → auto-parsed.\n"
        "- offset+limit for large files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute file path.",
            },
            "offset": {
                "type": "integer",
                "description": "1-based start line (default 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines; omit → read to end.",
            },
        },
        "required": ["path"],
    },
    execute=file_read,
)
