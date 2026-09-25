import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import OllamaClient
from app.main import create_app
from conftest import PROMPTS, SAMPLES, FakeOllama

INVOICE = {"invoice_number": "INV-2026-0419", "total_amount": 3574.15, "currency": "USD", "line_items": []}


@pytest.fixture
def fake():
    return FakeOllama(responses=[INVOICE])


@pytest.fixture
def client(fake):
    settings = Settings(prompts_dir=PROMPTS, ocr_enabled=False, auto_pull=False)
    app = create_app(settings, OllamaClient("http://ollama", transport=fake.transport()))
    with TestClient(app) as c:
        yield c


def pdf(name="sample_invoice.pdf"):
    return {"file": (name, (SAMPLES / name).read_bytes(), "application/pdf")}


def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"]


def test_not_ready_without_model(fake, client):
    fake.models.clear()
    assert client.get("/readyz").status_code == 503


def test_ui_served(client):
    assert "Document Parser" in client.get("/").text


def test_list_prompts(client):
    body = client.get("/api/prompts").json()
    assert {p["name"] for p in body["prompts"]} >= {"invoice", "contract"}
    assert client.get("/api/prompts/invoice").json()["schema"]["type"] == "object"
    assert client.get("/api/prompts/nope").status_code == 404


def test_parse(client):
    body = client.post("/api/parse", files=pdf()).json()
    assert "INV-2026-0419" in body["text"]


def test_parse_rejects_garbage(client):
    r = client.post("/api/parse", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 422


def test_extract(client):
    r = client.post("/api/extract", files=pdf(), data={"prompt": "invoice"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["invoice_number"] == "INV-2026-0419"


def test_fill_returns_pdf(client):
    r = client.post("/api/fill", files=pdf(), data={"prompt": "invoice"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_fill_json(client):
    body = client.post("/api/fill?format=json", files=pdf(), data={"prompt": "invoice"}).json()
    assert body["filled_fields"]["InvoiceNumber"] == "INV-2026-0419"
    assert body["pdf_base64"]


def test_fill_without_template_is_400(client):
    r = client.post("/api/fill", files=pdf(), data={"prompt": "contract"})
    assert r.status_code == 400


def test_inspect_template(client):
    t = (PROMPTS / "invoice_summary.pdf").read_bytes()
    body = client.post("/api/templates/inspect", files={"template": ("t.pdf", t, "application/pdf")}).json()
    assert body["fields"]["Paid"]["type"] == "button"


def test_auto_pull_missing_model(fake):
    fake.models.clear()
    settings = Settings(prompts_dir=PROMPTS, auto_pull=True, model="tiny:1b")
    app = create_app(settings, OllamaClient("http://ollama", transport=fake.transport()))
    with TestClient(app) as c:
        for _ in range(50):
            if c.get("/readyz").status_code == 200:
                break
            time.sleep(0.05)
        assert fake.pulled == ["tiny:1b"]
        assert c.get("/readyz").status_code == 200
