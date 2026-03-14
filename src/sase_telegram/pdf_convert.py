"""Convert markdown files to PDF via pandoc."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def md_to_pdf(md_path: str) -> str | None:
    """Convert a markdown file to PDF using pandoc.

    Returns the PDF file path on success, or None if the file is not markdown
    or conversion fails.
    """
    p = Path(md_path)
    if p.suffix.lower() != ".md":
        return None

    pdf_path = p.with_suffix(".pdf")
    try:
        subprocess.run(
            ["pandoc", str(p), "-o", str(pdf_path)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.warning("Failed to convert %s to PDF: %s", md_path, e)
        return None

    return str(pdf_path)
