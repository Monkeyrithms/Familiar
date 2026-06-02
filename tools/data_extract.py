"""
Structured data extraction — parse messy text/HTML/CSV into clean JSON.
"""

import json
import csv
import io
import re
from pathlib import Path
from tools.registry import registry


def data_extract(action: str, source: str = "", schema: dict = None,
                 delimiter: str = ",", url: str = "") -> str:
    """Extract structured data from various sources."""

    if action == "csv_to_json":
        try:
            if Path(source).is_file():
                text = Path(source).read_text(encoding="utf-8", errors="replace")
            else:
                text = source
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            rows = [dict(r) for r in list(reader)[:500]]
            return json.dumps({"rows": rows, "count": len(rows), "columns": list(rows[0].keys()) if rows else []},
                              ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "json_to_csv":
        try:
            if Path(source).is_file():
                data = json.loads(Path(source).read_text(encoding="utf-8"))
            else:
                data = json.loads(source)
            if isinstance(data, dict):
                data = [data]
            if not data:
                return json.dumps({"error": "No data to convert"})
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data[:500])
            return json.dumps({"csv": output.getvalue(), "rows": len(data)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "html_to_text":
        try:
            if url:
                import httpx
                resp = httpx.get(url, follow_redirects=True, timeout=15)
                html = resp.text
            elif Path(source).is_file():
                html = Path(source).read_text(encoding="utf-8", errors="replace")
            else:
                html = source
            # Simple HTML tag stripping
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return json.dumps({"text": text[:20000], "chars": len(text)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "extract_tables":
        try:
            if Path(source).is_file():
                html = Path(source).read_text(encoding="utf-8", errors="replace")
            else:
                html = source
            # Extract HTML tables
            tables = []
            for table_match in re.finditer(r'<table[^>]*>(.*?)</table>', html, re.DOTALL):
                rows = []
                for tr in re.finditer(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.DOTALL):
                    cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr.group(1), re.DOTALL)
                    cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                    if cells:
                        rows.append(cells)
                if rows:
                    tables.append(rows)
            return json.dumps({"tables": tables, "count": len(tables)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "regex_extract":
        if not schema:
            return json.dumps({"error": "schema required (dict of field_name: regex_pattern)"})
        try:
            if Path(source).is_file():
                text = Path(source).read_text(encoding="utf-8", errors="replace")
            else:
                text = source
            results = {}
            for field, pattern in schema.items():
                matches = re.findall(pattern, text)
                results[field] = matches if len(matches) != 1 else matches[0]
            return json.dumps({"extracted": results}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({
            "error": "action must be: csv_to_json, json_to_csv, html_to_text, extract_tables, regex_extract"
        })


registry.register(
    name="data_extract",
    description=(
        "Structured data extraction.\n"
        "- csv_to_json | json_to_csv | html_to_text (strip tags) | extract_tables (HTML) | regex_extract (fields by regex)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["csv_to_json", "json_to_csv", "html_to_text", "extract_tables", "regex_extract"]},
            "source": {"type": "string", "description": "File path | raw text|HTML|CSV."},
            "schema": {"type": "object", "description": "field→regex map (regex_extract)."},
            "delimiter": {"type": "string", "description": "CSV delim (default ,)."},
            "url": {"type": "string", "description": "URL to fetch HTML (html_to_text)."},
        },
        "required": ["action"],
    },
    execute=data_extract,
)
