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
def client(fake, tmp_path):
    settings = Settings(prompts_dir=PROMPTS, data_dir=tmp_path, ocr_enabled=False, auto_pull=False)
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
    assert "Form Assistant" in client.get("/").text
    assert "Document Parser Tools" in client.get("/tools").text


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


def test_auto_pull_missing_model(fake, tmp_path):
    fake.models.clear()
    settings = Settings(prompts_dir=PROMPTS, data_dir=tmp_path, auto_pull=True, model="tiny:1b")
    app = create_app(settings, OllamaClient("http://ollama", transport=fake.transport()))
    with TestClient(app) as c:
        for _ in range(50):
            if c.get("/readyz").status_code == 200:
                break
            time.sleep(0.05)
        assert fake.pulled == ["tiny:1b"]
        assert c.get("/readyz").status_code == 200


FORM = SAMPLES / "contract_renewal_form.pdf"


@pytest.fixture
def chat_client(tmp_path):
    fake = FakeOllama()  # every LLM call returns {}: the chat falls back to its heuristics
    settings = Settings(prompts_dir=PROMPTS, data_dir=tmp_path, ocr_enabled=False, auto_pull=False)
    app = create_app(settings, OllamaClient("http://ollama", transport=fake.transport()))
    with TestClient(app) as c:
        yield c


def say(c, sid, text):
    r = c.post(f"/api/chat/{sid}/message", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_chat_flow_fills_pdf_and_remembers_contract(chat_client):
    c = chat_client
    r = c.post("/api/chat", files={"template": ("renewal.pdf", FORM.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["stage"] == "contract" and len(state["fields"]) == 11
    sid = state["id"]

    state = say(c, sid, "NW-2026-014")
    assert state["stage"] == "fields" and state["current"] == "Vendor"
    assert state["contract"]["number"] == "NW-2026-014"
    state = say(c, sid, "Blue Harbor Consulting")
    state = say(c, sid, "fill")
    assert state["stage"] == "done" and state["messages"][-1]["kind"] == "download"

    assert c.get(f"/api/chat/{sid}").json()["stage"] == "done"
    pdf = c.get(f"/api/chat/{sid}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    contract = c.get("/api/contracts/nw-2026-014").json()
    assert contract["facts"]["contractor_name"] == "Blue Harbor Consulting"


def test_chat_rejects_non_fillable_pdf(chat_client):
    r = chat_client.post("/api/chat", files=pdf())
    assert r.status_code == 422


def test_chat_unknown_session(chat_client):
    assert chat_client.get("/api/chat/nope").status_code == 404
    assert chat_client.post("/api/chat/nope/message", json={"text": "hi"}).status_code == 404


def test_contract_crud(chat_client):
    c = chat_client
    assert c.get("/api/contracts").json() == {"contracts": []}
    body = c.put("/api/contracts/ABC-1", json={"facts": {"renewal_date": "2027-01-01"}}).json()
    assert body["facts"] == {"contract_number": "ABC-1", "renewal_date": "2027-01-01"}
    assert c.delete("/api/contracts/abc1/facts/renewal_date").json()["facts"] == {"contract_number": "ABC-1"}
    assert c.delete("/api/contracts/ABC-1").status_code == 204
    assert c.get("/api/contracts/ABC-1").status_code == 404
