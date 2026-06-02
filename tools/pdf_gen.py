"""
PDF generation tool — create PDFs from text, markdown, or HTML.
Uses reportlab if available, falls back to fpdf2.
"""

import json
from pathlib import Path
from tools.registry import registry


def pdf_generate(content: str, output_path: str, title: str = "",
                 format: str = "text", font_size: int = 11) -> str:
    """Generate a PDF from text, markdown, or HTML content."""
    if not content:
        return json.dumps({"error": "content required"})
    if not output_path:
        return json.dumps({"error": "output_path required"})

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Try fpdf2 first (lightweight, pure Python)
    try:
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_margins(15, 15, 15)
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        lh = font_size * 0.5  # line height

        if title:
            pdf.set_font("Helvetica", "B", 16)
            pdf.cell(w=0, h=10, text=title, new_x="LMARGIN", new_y="NEXT", align="C")
            pdf.ln(5)

        pdf.set_font("Helvetica", size=font_size)

        for line in content.split("\n"):
            stripped = line.strip()

            if format == "markdown":
                if stripped.startswith("# "):
                    pdf.set_font("Helvetica", "B", font_size + 6)
                    pdf.cell(w=0, h=lh + 3, text=stripped[2:], new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", size=font_size)
                    continue
                elif stripped.startswith("## "):
                    pdf.set_font("Helvetica", "B", font_size + 3)
                    pdf.cell(w=0, h=lh + 2, text=stripped[3:], new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", size=font_size)
                    continue
                elif stripped.startswith("### "):
                    pdf.set_font("Helvetica", "B", font_size + 1)
                    pdf.cell(w=0, h=lh + 1, text=stripped[4:], new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", size=font_size)
                    continue
                elif stripped.startswith("- ") or stripped.startswith("* "):
                    pdf.multi_cell(w=0, h=lh, text=f"    {stripped}")
                    pdf.set_x(pdf.l_margin)
                    continue
                elif stripped == "---" or stripped == "***":
                    pdf.ln(2)
                    y = pdf.get_y()
                    pdf.line(15, y, 195, y)
                    pdf.ln(2)
                    continue
                elif stripped.startswith("**") and stripped.endswith("**"):
                    pdf.set_font("Helvetica", "B", font_size)
                    pdf.multi_cell(w=0, h=lh, text=stripped.strip("*"))
                    pdf.set_x(pdf.l_margin)
                    pdf.set_font("Helvetica", size=font_size)
                    continue

            if format == "code":
                pdf.set_font("Courier", size=font_size)

            if stripped:
                pdf.multi_cell(w=0, h=lh, text=line)
                pdf.set_x(pdf.l_margin)  # Reset X after multi_cell
            else:
                pdf.ln(lh)

            if format == "code":
                pdf.set_font("Helvetica", size=font_size)

        pdf.output(str(p))
        size = p.stat().st_size
        return json.dumps({"created": str(p), "size": size, "pages": pdf.pages_count})

    except ImportError:
        pass

    # Fallback: reportlab
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet

        doc = SimpleDocTemplate(str(p), pagesize=letter)
        styles = getSampleStyleSheet()
        story = []

        if title:
            story.append(Paragraph(title, styles["Title"]))
            story.append(Spacer(1, 12))

        for line in content.split("\n"):
            if line.strip():
                story.append(Paragraph(line, styles["Normal"]))
            else:
                story.append(Spacer(1, 6))

        doc.build(story)
        size = p.stat().st_size
        return json.dumps({"created": str(p), "size": size})

    except ImportError:
        pass

    return json.dumps({
        "error": "No PDF library available. Install: pip install fpdf2 (or pip install reportlab)"
    })


registry.register(
    name="pdf",
    description=(
        "Generate PDF from text | markdown | code.\n"
        "- Titles + basic md (headers, bold, lists, hr).\n"
        "- Needs fpdf2 | reportlab (pip install fpdf2)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Content."},
            "output_path": {"type": "string", "description": "Output PDF path."},
            "title": {"type": "string", "description": "Title (optional)."},
            "format": {"type": "string", "enum": ["text", "markdown", "code"], "description": "Format (default text)."},
            "font_size": {"type": "integer", "description": "Font size (default 11)."},
        },
        "required": ["content", "output_path"],
    },
    execute=pdf_generate,
)
