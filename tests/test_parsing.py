import pytest

from app.pdf_parser import PDFParseError, parse_pdf, table_to_markdown
from conftest import SAMPLES


def test_parse_text_pdf():
    doc = parse_pdf((SAMPLES / "sample_invoice.pdf").read_bytes(), ocr_enabled=False)
    assert doc.total_pages == 1 and not doc.truncated
    text = doc.full_text()
    assert "INV-2026-0419" in text
    assert "Hydraulic pump HX-200" in text
    assert text.startswith("--- Page 1 ---")


def test_rejects_non_pdf():
    with pytest.raises(PDFParseError):
        parse_pdf(b"hello world")


def test_scanned_pdf_without_ocr_has_no_text():
    doc = parse_pdf((SAMPLES / "scanned_memo.pdf").read_bytes(), ocr_enabled=False)
    assert doc.pages[0].text.strip() == ""


def test_max_pages_truncates():
    doc = parse_pdf((SAMPLES / "sample_invoice.pdf").read_bytes(), max_pages=0, ocr_enabled=False)
    assert doc.truncated and doc.pages == []


def test_table_to_markdown_pads_and_escapes():
    md = table_to_markdown([["a", "b|c"], ["1"]])
    assert md.splitlines() == ["| a | b\\|c |", "| --- | --- |", "| 1 |  |"]
