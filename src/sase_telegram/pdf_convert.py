"""Convert markdown files to PDF via pandoc."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_CSS_PATH = Path(__file__).parent / "pdf_style.css"

# Ordered list of PDF engines to try.  wkhtmltopdf goes through HTML so it
# handles Unicode/emoji natively; the LaTeX engines are fallbacks.
_PDF_ENGINES: list[str] = ["wkhtmltopdf", "xelatex", "pdflatex"]


def _find_available_engines() -> list[str]:
    """Return the subset of _PDF_ENGINES that are installed."""
    return [e for e in _PDF_ENGINES if shutil.which(e)]


def _pandoc_cmd(src: Path, dst: Path, engine: str) -> list[str]:
    """Build the pandoc command for a given engine."""
    cmd = [
        "pandoc",
        str(src),
        "-o",
        str(dst),
        f"--pdf-engine={engine}",
        "--highlight-style=tango",
    ]
    # HTML-based engines (wkhtmltopdf) support --css for styling.
    if engine == "wkhtmltopdf":
        if _CSS_PATH.exists():
            cmd += [f"--css={_CSS_PATH}"]
        # Suppress the "no <title>" warning.
        cmd += ["--metadata", f"title={src.stem}"]
    else:
        # LaTeX engines: set reasonable margins.
        cmd += ["-V", "geometry:margin=1in"]
    return cmd


def md_to_pdf(md_path: str) -> str | None:
    """Convert a markdown file to PDF using pandoc.

    Tries multiple PDF engines in order of preference and returns the PDF
    file path on success, or ``None`` if the file is not markdown or every
    engine fails.
    """
    p = Path(md_path)
    if p.suffix.lower() != ".md":
        return None

    pdf_path = p.with_suffix(".pdf")
    engines = _find_available_engines()

    if not engines:
        log.warning("No PDF engine found; cannot convert %s", md_path)
        return None

    last_err: Exception | None = None
    for engine in engines:
        cmd = _pandoc_cmd(p, pdf_path, engine)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            return str(pdf_path)
        except subprocess.TimeoutExpired:
            log.warning("pandoc timed out with engine %s for %s", engine, md_path)
            last_err = None
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.debug("Engine %s failed for %s: %s", engine, md_path, e)
            last_err = e
            # Clean up partial output before trying next engine.
            pdf_path.unlink(missing_ok=True)

    log.warning("All PDF engines failed for %s (last: %s)", md_path, last_err)
    return None
