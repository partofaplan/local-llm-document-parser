import pytest

from app.extractor import chunk_text, extract, merge_results
from app.llm import OllamaClient
from app.prompts import PromptRegistry
from conftest import PROMPTS, FakeOllama


def test_chunking_respects_size_and_overlap():
    text = "\n\n".join(f"paragraph {i} " + "x" * 80 for i in range(50))
    chunks = chunk_text(text, 1000, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0][-50:] in chunks[1]  # overlap carried over
    assert chunk_text("short", 1000, 100) == ["short"]


def test_merge_first_scalar_and_dedup_arrays():
    spec = PromptRegistry(PROMPTS).get("invoice")
    item = {"description": "a", "quantity": 1, "unit_price": 2, "amount": 2}
    merged = merge_results(spec, [
        {"invoice_number": None, "vendor": {"name": "ACME", "address": None, "email": None}, "line_items": [item]},
        {"invoice_number": "X1", "vendor": {"name": "Other", "address": "Road", "email": None}, "line_items": [item]},
    ])
    assert merged["invoice_number"] == "X1"
    assert merged["vendor"] == {"name": "ACME", "address": "Road", "email": None}
    assert merged["line_items"] == [item]
    assert merged["total_amount"] is None


@pytest.mark.asyncio
async def test_extract_sends_schema_and_validates():
    fake = FakeOllama(responses=[{"invoice_number": "INV-1", "total_amount": 10.5, "line_items": []}])
    client = OllamaClient("http://ollama", transport=fake.transport())
    spec = PromptRegistry(PROMPTS).get("invoice")
    result = await extract(client, spec, "Invoice INV-1 total 10.50", default_model="m", chunk_chars=5000,
                           chunk_overlap=0, max_chunks=4, base_options={"temperature": 0})
    call = fake.chat_calls[0]
    assert call["model"] == "m" and call["format"] == spec.json_schema() and call["options"] == {"temperature": 0}
    assert "Invoice INV-1" in call["messages"][1]["content"]
    assert result.data["invoice_number"] == "INV-1"
    assert result.data["vendor"] is None  # missing keys are filled in
    assert result.validation_errors == []


@pytest.mark.asyncio
async def test_extract_reports_missing_required():
    fake = FakeOllama(responses=[{}])
    client = OllamaClient("http://ollama", transport=fake.transport())
    spec = PromptRegistry(PROMPTS).get("invoice")
    result = await extract(client, spec, "nothing", default_model="m", chunk_chars=5000, chunk_overlap=0,
                           max_chunks=4, base_options={})
    assert any("invoice_number" in e for e in result.validation_errors)
