"""
Archive tool — zip/unzip/tar operations.
"""

import json
import zipfile
import tarfile
import shutil
from pathlib import Path
from tools.registry import registry


def archive(action: str, path: str, dest: str = "", files: list = None) -> str:
    """Create or extract archives."""

    if action == "zip":
        if not files:
            return json.dumps({"error": "files list required for zip"})
        try:
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    fp = Path(f)
                    if fp.is_file():
                        zf.write(f, fp.name)
                    elif fp.is_dir():
                        for child in fp.rglob("*"):
                            if child.is_file():
                                zf.write(str(child), str(child.relative_to(fp.parent)))
            size = Path(path).stat().st_size
            return json.dumps({"created": path, "size": size})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "unzip":
        dest = dest or str(Path(path).parent / Path(path).stem)
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                zf.extractall(dest)
                names = zf.namelist()
            return json.dumps({"extracted_to": dest, "files": len(names)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "tar":
        if not files:
            return json.dumps({"error": "files list required for tar"})
        mode = "w:gz" if path.endswith(".gz") or path.endswith(".tgz") else "w"
        try:
            with tarfile.open(path, mode) as tf:
                for f in files:
                    tf.add(f, arcname=Path(f).name)
            size = Path(path).stat().st_size
            return json.dumps({"created": path, "size": size})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "untar":
        dest = dest or str(Path(path).parent / Path(path).stem.replace(".tar", ""))
        try:
            with tarfile.open(path, 'r:*') as tf:
                tf.extractall(dest)
                names = tf.getnames()
            return json.dumps({"extracted_to": dest, "files": len(names)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "list":
        try:
            if path.endswith(".zip"):
                with zipfile.ZipFile(path, 'r') as zf:
                    entries = [{"name": i.filename, "size": i.file_size} for i in zf.infolist()[:50]]
            else:
                with tarfile.open(path, 'r:*') as tf:
                    entries = [{"name": m.name, "size": m.size} for m in tf.getmembers()[:50]]
            return json.dumps({"entries": entries, "count": len(entries)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({"error": "action must be: zip, unzip, tar, untar, list"})


registry.register(
    name="archive",
    description="Create|extract zip/tar. Actions: zip, unzip, tar, untar, list.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["zip", "unzip", "tar", "untar", "list"]},
            "path": {"type": "string", "description": "Archive path."},
            "dest": {"type": "string", "description": "Extract dest (opt)."},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files/dirs for zip/tar."},
        },
        "required": ["action", "path"],
    },
    execute=archive,
)
