import pytest
from pypdf import PdfReader
import io

from app.filler import FillError, field_labels, fill_pdf, form_fields, resolve, write_form
from app.prompts import PromptRegistry
from conftest import PROMPTS

DATA = {
    "invoice_number": "INV-9",
    "invoice_date": "2026-09-03",
    "vendor": {"name": "ACME", "address": "1 Road"},
    "customer": {"name": "Wayne"},
    "total_amount": 123.0,
    "currency": "USD",
    "paid": True,
    "line_items": [{"description": "pump", "amount": 100}],
}


def test_resolve_paths():
    assert resolve(DATA, "vendor.name") == "ACME"
    assert resolve(DATA, "line_items.0.description") == "pump"
    assert resolve(DATA, "line_items.5.description") is None
    assert resolve(DATA, "missing.x") is None


def test_fill_invoice_template():
    spec = PromptRegistry(PROMPTS).get("invoice")
    template = spec.template_path()
    assert "InvoiceNumber" in form_fields(template)
    pdf, written = fill_pdf(template, DATA, spec.output_pdf.field_map)
    assert written["InvoiceNumber"] == "INV-9"  # matched automatically by normalised name
    assert written["VendorName"] == "ACME"
    assert written["Total"] == "123 USD"
    assert written["Paid"] != "/Off"
    assert "pump" in written["LineItems"]

    values = {k: v.get("/V") for k, v in PdfReader(io.BytesIO(pdf)).get_fields().items()}
    assert values["InvoiceNumber"] == "INV-9"
    assert values["Total"] == "123 USD"


def test_unknown_mapped_field_errors():
    spec = PromptRegistry(PROMPTS).get("invoice")
    with pytest.raises(FillError):
        fill_pdf(spec.template_path(), DATA, {"Nope": "invoice_number"})


def test_multiline_lists_one_item_per_line_and_wrap():
    spec = PromptRegistry(PROMPTS).get("invoice")
    items = [{"description": f"item {i} " + "long words " * 12, "amount": i} for i in range(3)]
    _, written = fill_pdf(spec.template_path(), {"line_items": items}, spec.output_pdf.field_map)
    lines = written["LineItems"].split("\n")
    assert len(lines) > 3  # three items, each wrapped
    assert all(len(line) <= 80 for line in lines)


FORM = PROMPTS.parent / "samples" / "contract_renewal_form.pdf"


def test_field_labels_read_printed_text():
    labels = field_labels(FORM)
    assert labels["ContractNo"] == "Contract number"
    assert labels["AutoRenew"] == "Renew automatically"
    assert form_fields(FORM)["Term"]["options"] == ["1 year", "2 years", "3 years"]


def test_write_form_by_exact_name():
    pdf, written = write_form(FORM, {"ContractNo": "NW-1", "AutoRenew": "Yes", "Term": "2 years"})
    assert written["AutoRenew"] == "/Yes"
    values = {k: v.get("/V") for k, v in PdfReader(io.BytesIO(pdf)).get_fields().items()}
    assert values["ContractNo"] == "NW-1" and values["Term"] == "2 years"
    with pytest.raises(FillError):
        write_form(FORM, {"Nope": "x"})
