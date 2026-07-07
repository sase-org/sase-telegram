"""Convert markdown files to PDF via the shared SASE renderer."""

from __future__ import annotations

from pathlib import Path

from sase.attachments.markdown_pdf import (
    render_launch_preview_pdf,
    render_markdown_pdf,
)

_CSS_PATH = Path(__file__).parent / "pdf_style.css"


def md_to_pdf(md_path: str) -> str | None:
    """Convert a markdown file to a sibling PDF.

    The public plugin API remains stable while the Pandoc command construction
    and engine fallback behavior live in ``sase.attachments.markdown_pdf``.
    """
    p = Path(md_path)
    if p.suffix.lower() != ".md":
        return None

    pdf_path = p.with_suffix(".pdf")
    if p.name == "launch_preview.md":
        rendered = render_launch_preview_pdf(p, pdf_path)
    else:
        rendered = render_markdown_pdf(p, pdf_path, css_path=_CSS_PATH)
    return str(rendered) if rendered is not None else None
