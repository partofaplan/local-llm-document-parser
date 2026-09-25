"""PDF text/table extraction with an OCR fallback for scanned pages."""

from __future__ import annotations

import io
import logging
from dataclasses import asdict, dataclass, field

import pdfplumber

log = logging.getLogger(__name__)


class PDFParseError(ValueError):
    pass


@dataclass
class PageContent:
    number: int
    text: str
    tables: list[list[list[str | None]]] = field(default_factory=list)
    ocr: bool = False


@dataclass
class ParsedDocument:
    pages: list[PageContent]
    metadata: dict[str, str]
    total_pages: int

    @property
    def truncated(self) -> bool:
        return len(self.pages) < self.total_pages

    def full_text(self) -> str:
        """Text of all pages, with tables rendered as Markdown, for feeding to the LLM."""
        parts = []
        for page in self.pages:
            body = page.text.strip()
            for i, table in enumerate(page.tables, 1):
                body += f"\n\n[Table {i}]\n{table_to_markdown(table)}"
            parts.append(f"--- Page {page.number} ---\n{body}")
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "metadata": self.metadata,
            "total_pages": self.total_pages,
            "parsed_pages": len(self.pages),
            "truncated": self.truncated,
            "pages": [asdict(p) for p in self.pages],
        }


def table_to_markdown(table: list[list[str | None]]) -> str:
    rows = [[(c or "").replace("\n", " ").replace("|", "\\|").strip() for c in row] for row in table if row]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _ocr_page(page, dpi: int, lang: str) -> str:
    try:
        import pytesseract

        image = page.to_image(resolution=dpi).original
        return pytesseract.image_to_string(image, lang=lang)
    except Exception as exc:  # tesseract missing, bad language pack, render failure...
        log.warning("OCR failed on page %s: %s", page.page_number, exc)
        return ""


def parse_pdf(
    data: bytes,
    *,
    max_pages: int = 200,
    ocr_enabled: bool = True,
    ocr_min_chars: int = 25,
    ocr_dpi: int = 200,
    ocr_lang: str = "eng",
) -> ParsedDocument:
    if not data.lstrip()[:5].startswith(b"%PDF-"):
        raise PDFParseError("File is not a PDF")
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        raise PDFParseError(f"Unable to open PDF: {exc}") from exc

    with pdf:
        metadata = {str(k): str(v) for k, v in (pdf.metadata or {}).items()}
        total = len(pdf.pages)
        pages: list[PageContent] = []
        for page in pdf.pages[:max_pages]:
            try:
                text = page.extract_text() or ""
                tables = page.extract_tables() or []
            except Exception as exc:
                log.warning("Text extraction failed on page %s: %s", page.page_number, exc)
                text, tables = "", []
            used_ocr = False
            if ocr_enabled and len(text.strip()) < ocr_min_chars:
                ocr_text = _ocr_page(page, ocr_dpi, ocr_lang)
                if len(ocr_text.strip()) > len(text.strip()):
                    text, used_ocr = ocr_text, True
            pages.append(PageContent(page.page_number, text, tables, used_ocr))
            page.close()  # release cached layout objects on large documents

    return ParsedDocument(pages=pages, metadata=metadata, total_pages=total)
