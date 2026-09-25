"""Generate the invoice form template and sample PDFs (requires reportlab + Pillow: pip install -r requirements-dev.txt).

    python scripts/make_samples.py
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import black
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
W, H = letter

INVOICE_LINES = [
    "ACME Industrial Supply LLC",
    "1200 Foundry Road, Omaha, NE 68102",
    "billing@acme-industrial.example",
    "",
    "INVOICE",
    "Invoice #: INV-2026-0419",
    "Invoice Date: September 3, 2026",
    "Due Date: October 3, 2026",
    "",
    "Bill To:",
    "Wayne Logistics Inc.",
    "88 Harbor Blvd, Tacoma, WA 98402",
]
ITEMS = [
    ("Hydraulic pump HX-200", 2, 1450.00),
    ("Steel fitting kit (50 pc)", 10, 38.50),
    ("On-site installation (hours)", 6, 125.00),
]


def invoice_template(path: Path) -> None:
    """A fillable AcroForm summary sheet; field names line up with prompts/invoice.yaml."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Invoice Summary Form")
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, H - 72, "Invoice Summary")
    c.setFont("Helvetica", 10)
    c.drawString(72, H - 90, "Completed automatically from the source invoice.")
    form = c.acroForm
    rows = [
        ("InvoiceNumber", "Invoice number"),
        ("InvoiceDate", "Invoice date"),
        ("DueDate", "Due date"),
        ("VendorName", "Vendor"),
        ("VendorAddress", "Vendor address"),
        ("CustomerName", "Bill to"),
        ("Subtotal", "Subtotal"),
        ("Tax", "Tax"),
        ("Total", "Total due"),
    ]
    y = H - 130
    for name, label in rows:
        c.setFont("Helvetica", 11)
        c.drawString(72, y + 6, label)
        form.textfield(name=name, x=200, y=y, width=340, height=20, borderColor=black, fontSize=10, maxlen=500)
        y -= 32
    c.drawString(72, y + 6, "Paid")
    form.checkbox(name="Paid", x=200, y=y, size=16, buttonStyle="check", borderColor=black)
    y -= 40
    c.drawString(72, y + 6, "Line items")
    form.textfield(name="LineItems", x=200, y=y - 100, width=340, height=120, borderColor=black,
                   fontSize=9, fieldFlags="multiline", maxlen=4000)
    c.save()


def invoice_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Invoice INV-2026-0419")
    y = H - 72
    for line in INVOICE_LINES:
        c.setFont("Helvetica-Bold" if line in ("INVOICE", "Bill To:") else "Helvetica", 11)
        c.drawString(72, y, line)
        y -= 16
    y -= 16
    c.setFont("Helvetica-Bold", 10)
    for x, h in zip((72, 330, 400, 480), ("Description", "Qty", "Unit Price", "Amount")):
        c.drawString(x, y, h)
    c.line(72, y - 4, 540, y - 4)
    c.setFont("Helvetica", 10)
    subtotal = 0.0
    for desc, qty, price in ITEMS:
        y -= 18
        amount = qty * price
        subtotal += amount
        for x, v in zip((72, 330, 400, 480), (desc, str(qty), f"${price:,.2f}", f"${amount:,.2f}")):
            c.drawString(x, y, v)
    tax = round(subtotal * 0.07, 2)
    y -= 30
    for label, v in (("Subtotal", subtotal), ("Sales tax (7%)", tax), ("TOTAL DUE (USD)", subtotal + tax)):
        c.drawString(360, y, label)
        c.drawRightString(540, y, f"${v:,.2f}")
        y -= 16
    y -= 20
    c.drawString(72, y, "Payment terms: Net 30. Status: UNPAID.")
    c.save()


CONTRACT = """MASTER SERVICES AGREEMENT
This Master Services Agreement ("Agreement") is entered into as of March 1, 2026 (the "Effective Date")
between Northwind Analytics Corp., a Delaware corporation ("Client"), and Blue Harbor Consulting LLC ("Contractor").
1. Services. Contractor shall provide data engineering services as described in each Statement of Work.
2. Fees. Client shall pay all undisputed invoices within forty-five (45) days of receipt.
3. Term. This Agreement continues until February 28, 2028 and renews automatically for successive
one-year terms unless either party gives sixty (60) days written notice of non-renewal.
4. Confidentiality. Each party shall protect the other's Confidential Information for five (5) years.
5. Liability. Contractor's aggregate liability shall not exceed the fees paid in the prior twelve months.
6. Governing Law. This Agreement is governed by the laws of the State of Nebraska."""


def contract_pdf(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Master Services Agreement")
    y = H - 72
    for i, line in enumerate(CONTRACT.splitlines()):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 9)
        c.drawString(50, y, line)
        y -= 18
    c.save()


def scanned_pdf(path: Path) -> None:
    """An image-only PDF (no text layer) to exercise the OCR fallback."""
    img = Image.new("L", (1700, 2200), 255)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 44)
    except OSError:
        font = ImageFont.load_default(size=44)
    lines = [
        "MEMORANDUM",
        "Date: August 14, 2026",
        "From: Dana Whitfield, Operations",
        "To: All Warehouse Staff",
        "",
        "Subject: New forklift certification",
        "",
        "All operators must complete the updated",
        "forklift safety certification by",
        "September 30, 2026. Sessions run Tuesdays",
        "at the Tacoma training center.",
    ]
    for i, line in enumerate(lines):
        draw.text((150, 200 + i * 80), line, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 0, 0, W, H)
    c.save()


if __name__ == "__main__":
    (ROOT / "samples").mkdir(exist_ok=True)
    invoice_template(ROOT / "prompts" / "invoice_summary.pdf")
    invoice_pdf(ROOT / "samples" / "sample_invoice.pdf")
    contract_pdf(ROOT / "samples" / "sample_contract.pdf")
    scanned_pdf(ROOT / "samples" / "scanned_memo.pdf")
    print("wrote prompts/invoice_summary.pdf and samples/*.pdf")
