"""Tests for the public Markdown-to-PDF converter wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from sase_telegram import pdf_convert


def test_md_to_pdf_delegates_to_core_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = tmp_path / "notes.md"
    markdown.write_text("# Notes\n", encoding="utf-8")
    pdf = tmp_path / "notes.pdf"
    render_markdown_pdf = Mock(return_value=pdf)

    monkeypatch.setattr(pdf_convert, "render_markdown_pdf", render_markdown_pdf)

    assert pdf_convert.md_to_pdf(str(markdown)) == str(pdf)
    render_markdown_pdf.assert_called_once_with(
        markdown,
        pdf,
        css_path=pdf_convert._CSS_PATH,
    )


def test_md_to_pdf_returns_none_for_non_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not markdown\n", encoding="utf-8")
    render_markdown_pdf = Mock()

    monkeypatch.setattr(pdf_convert, "render_markdown_pdf", render_markdown_pdf)

    assert pdf_convert.md_to_pdf(str(text_file)) is None
    render_markdown_pdf.assert_not_called()


def test_md_to_pdf_returns_none_when_core_renderer_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = tmp_path / "notes.md"
    markdown.write_text("# Notes\n", encoding="utf-8")
    render_markdown_pdf = Mock(return_value=None)

    monkeypatch.setattr(pdf_convert, "render_markdown_pdf", render_markdown_pdf)

    assert pdf_convert.md_to_pdf(str(markdown)) is None
