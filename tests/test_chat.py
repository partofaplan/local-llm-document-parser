import io
from datetime import date, timedelta

import pytest
from pypdf import PdfReader

from app.chat import FormChat, fact_key, humanize, parse_date, parse_number
from app.config import Settings
from app.filler import FillError
from app.llm import OllamaClient
from app.prompts import PromptRegistry
from app.store import Store
from conftest import PROMPTS, SAMPLES, FakeOllama

FORM = SAMPLES / "contract_renewal_form.pdf"


@pytest.fixture
def make_chat(tmp_path):
    """Build a FormChat on a shared database; LLM replies are queued per chat (an empty queue returns {})."""
    def make(*responses):
        fake = FakeOllama(responses=list(responses))
        settings = Settings(prompts_dir=PROMPTS, data_dir=tmp_path, ocr_enabled=False)
        client = OllamaClient("http://ollama", transport=fake.transport())
        return FormChat(settings, client, Store(tmp_path / "db.sqlite"), PromptRegistry(PROMPTS)), fake
    return make


async def started(chat, *replies):
    session = await chat.start("renewal.pdf", FORM.read_bytes())
    for text in replies:
        await chat.reply(session, text)
    return session


def last(session):
    return session.messages[-1]["text"]


def test_helpers():
    assert humanize("RenewalDate") == "Renewal date" and humanize("form1.notice_days") == "Notice days"
    assert fact_key("Contract No.") == fact_key("ContractNum") == fact_key("Contract #") == "contract_number"
    assert parse_date("Feb 28th, 2028") == parse_date("02/28/2028") == "2028-02-28"
    assert parse_date("next tuesday") is None
    assert parse_number("$250,000.50") == "250000.50" and parse_number("a lot") is None


@pytest.mark.asyncio
async def test_start_plans_questions_with_heuristic_fallback(make_chat):
    chat, _ = make_chat()  # model returns {}: every field falls back to heuristics
    session = await started(chat)
    assert session.stage == "contract" and "contract" in last(session).lower()
    by = {f.name: f for f in session.fields}
    assert by["ContractNo"].label == "Contract number" and by["ContractNo"].fact == "contract_number"
    assert by["RenewalDate"].kind == "date" and by["RenewalDate"].fact == "renewal_date"
    assert by["AutoRenew"].kind == "yes_no" and by["Term"].kind == "choice"
    assert not by["Date"].remember and not by["Comments"].remember and by["Vendor"].remember


@pytest.mark.asyncio
async def test_llm_plan_is_used_and_known_facts_win(make_chat):
    plan = {
        "Value": {"question": "What's the total contract value?", "kind": "number", "fact": "contract_value",
                  "remember": True},
        "RenewalDate": {"question": "When does it renew?", "kind": "text", "fact": "renew_on", "remember": False},
        "NoticeDays": {"question": "Notice days?", "kind": "number", "fact": "renewal_notice_days", "remember": False},
        "Date": {"question": "Today's date?", "kind": "text", "fact": "effective_date", "remember": True},
        "RequestedBy": {"question": "Who asked?", "kind": "text", "fact": "requested_by", "remember": False},
    }
    chat, fake = make_chat(plan)
    session = await started(chat)
    by = {f.name: f for f in session.fields}
    assert by["Value"].question == "What's the total contract value?" and by["Value"].fact == "contract_value"
    assert by["RenewalDate"].fact == "renewal_date"  # the printed label names a known fact
    # Seen with qwen2.5:3b: known contract facts marked not-to-remember, and "Date" mapped to effective_date.
    assert by["RenewalDate"].remember and by["NoticeDays"].remember and by["RenewalDate"].kind == "date"
    assert by["Date"].fact == "date" and not by["Date"].remember and by["Date"].kind == "date"
    assert not by["RequestedBy"].remember  # unknown facts follow the model
    call = fake.chat_calls[0]
    assert "renewal_date" in call["messages"][1]["content"]
    assert call["format"]["required"] == [f.name for f in session.fields]  # one entry per field, nothing open-ended
    assert call["options"]["num_predict"] == 80 * len(session.fields) + 100


@pytest.mark.asyncio
async def test_full_walkthrough_fills_pdf_and_remembers(make_chat):
    chat, fake = make_chat()
    session = await started(chat, "NW-2026-014")
    assert session.contract == "NW-2026-014"
    assert session.get("ContractNo").value == "NW-2026-014" and session.get("ContractNo").source == "memory"
    assert session.current().name == "Vendor"

    for text in ["Blue Harbor Consulting", "Northwind Analytics", "Feb 28, 2028", "60", "$250,000", "skip",
                 "2026-09-25", "yes", "2 years"]:
        await chat.reply(session, text)
    assert session.current().name == "Comments"
    await chat.reply(session, "fill")
    assert session.stage == "done" and session.messages[-1]["kind"] == "download"
    assert len(fake.chat_calls) == 1  # only the plan; every answer was unambiguous

    values = {k: v.get("/V") for k, v in PdfReader(io.BytesIO(chat.fill(session, FORM.read_bytes()))).get_fields().items()}
    assert values["ContractNo"] == "NW-2026-014" and values["RenewalDate"] == "2028-02-28"
    assert values["Value"] == "250000" and values["AutoRenew"] == "/Yes" and values["Term"] == "2 years"

    facts = chat.store.get_contract("nw 2026 014")["facts"]
    assert facts["renewal_date"] == "2028-02-28" and facts["contractor_name"] == "Blue Harbor Consulting"
    assert "date" not in facts and "requested_by" not in facts  # per-submission / skipped fields are not kept


@pytest.mark.asyncio
async def test_known_contract_prefills_next_form(make_chat):
    chat, _ = make_chat()
    renewal = (date.today() + timedelta(days=30)).isoformat()
    chat.store.remember("NW-2026-014", {"renewal_date": renewal, "contractor_name": "Blue Harbor"})

    chat2, _ = make_chat()
    session = await started(chat2, "it's for nw-2026-014")
    assert session.contract == "NW-2026-014"
    assert session.get("RenewalDate").value == renewal and session.get("Vendor").value == "Blue Harbor"
    intro = session.messages[-2]["text"]
    assert "Found contract" in intro and "in 30 days" in intro
    assert session.current().name == "Client"


@pytest.mark.asyncio
async def test_free_text_goes_to_llm_and_can_answer_several_fields(make_chat):
    answers = {"answers": [{"field": "Vendor", "value": "Blue Harbor"}, {"field": "Client", "value": "Northwind"}],
               "ask_about": None, "reply": None}
    chat, fake = make_chat({}, answers)
    session = await started(chat, "NW-1", "The contractor is Blue Harbor and the client is Northwind")
    assert session.get("Vendor").value == "Blue Harbor" and session.get("Client").value == "Northwind"
    assert session.current().name == "RenewalDate"
    assert "Got it" in session.messages[-2]["text"]
    call = fake.chat_calls[1]
    assert call["format"]["properties"]["answers"]["items"]["properties"]["field"]["enum"][0] == "ContractNo"
    assert any("Blue Harbor" in m["content"] for m in call["messages"] if m["role"] == "user")


@pytest.mark.asyncio
async def test_question_gets_reply_and_field_stays_open(make_chat):
    chat, _ = make_chat({}, {"answers": [], "ask_about": None, "reply": "The company doing the work. Who is it?"})
    session = await started(chat, "NW-1", "what do you mean by contractor?")
    assert last(session) == "The company doing the work. Who is it?"
    assert session.current().name == "Vendor"


@pytest.mark.asyncio
async def test_questions_never_record_values(make_chat):
    chat, fake = make_chat({}, {"reply": "Use the date the current term ends. When is that?"})
    session = await started(chat, "NW-1")
    session.get("Vendor").status = session.get("Client").status = "answered"
    await chat.reply(session, "what date should I use here?")
    assert session.get("RenewalDate").status == "pending" and "term ends" in last(session)
    assert list(fake.chat_calls[1]["format"]["properties"]) == ["reply"]


@pytest.mark.asyncio
async def test_ungrounded_llm_values_are_dropped(make_chat):
    # Seen with qwen2.5:3b: a name given for a date field came back as today's date.
    invented = {"answers": [{"field": "RenewalDate", "value": date.today().isoformat()},
                            {"field": "Client", "value": "Acme Corporation"}], "ask_about": None, "reply": None}
    chat, _ = make_chat({}, invented)
    session = await started(chat, "NW-1")
    session.get("Vendor").status = session.get("Client").status = "answered"
    await chat.reply(session, "Zach Perkins will handle it all")
    assert session.get("RenewalDate").status == "pending" and session.get("Client").value is None


@pytest.mark.asyncio
async def test_invalid_llm_values_are_dropped(make_chat):
    bad = {"answers": [{"field": "AutoRenew", "value": "maybe"}], "ask_about": None, "reply": None}
    chat, _ = make_chat({}, bad)
    session = await started(chat, "NW-1")
    for f in session.fields[:8]:
        f.status, f.value = "answered", "x"
    await chat.reply(session, "I'm honestly not sure about this one")
    assert session.get("AutoRenew").status == "pending" and "didn't catch" in last(session)


@pytest.mark.asyncio
async def test_back_skip_and_yes_is_an_answer_not_a_command(make_chat):
    chat, _ = make_chat()
    session = await started(chat, "NW-1", "Blue Harbor")
    await chat.reply(session, "back")
    assert session.current().name == "Vendor" and session.get("Vendor").value is None
    await chat.reply(session, "back")
    assert "first question" in session.messages[-2]["text"]  # ContractNo came from memory
    for f in session.fields[:8]:
        f.status, f.value = "answered", "x"
    await chat.reply(session, "yes")
    assert session.get("AutoRenew").value == "Yes" and session.stage == "fields"


@pytest.mark.asyncio
async def test_review_corrections_update_memory_then_confirm(make_chat):
    fix = {"answers": [{"field": "RenewalDate", "value": "March 1, 2028"}], "ask_about": None, "reply": None}
    chat, _ = make_chat({}, fix)
    session = await started(chat, "NW-1", "fill")
    assert session.stage == "done"
    await chat.reply(session, "the renewal date should be March 1 2028")
    assert session.stage == "review" and session.get("RenewalDate").value == "2028-03-01"
    assert chat.store.get_contract("NW-1")["facts"]["renewal_date"] == "2028-03-01"
    await chat.reply(session, "looks good")
    assert session.stage == "done"


@pytest.mark.asyncio
async def test_review_mid_way_shows_progress_and_continues(make_chat):
    chat, _ = make_chat()
    session = await started(chat, "NW-1", "Blue Harbor", "review")
    assert session.stage == "fields" and session.current().name == "Client"
    summary = session.messages[-2]["text"]
    assert "Blue Harbor" in summary and "9 to go" in summary and "Client name" in last(session)


@pytest.mark.asyncio
async def test_ask_about_reopens_field(make_chat):
    chat, _ = make_chat({}, {"answers": [], "ask_about": "Vendor", "reply": None})
    session = await started(chat, "NW-1", "Blue Harbor", "fill", "I need to change the contractor")
    assert session.stage == "fields" and session.current().name == "Vendor"


@pytest.mark.asyncio
async def test_no_contract_remembers_nothing(make_chat):
    chat, _ = make_chat()
    session = await started(chat, "none", "C-77", "Blue Harbor")
    assert session.contract is None and session.current().name == "Client"
    assert session.get("ContractNo").value == "C-77" and session.get("Vendor").value == "Blue Harbor"
    assert chat.store.list_contracts() == []


@pytest.mark.asyncio
async def test_learn_document_selects_contract_and_prefills(make_chat):
    extracted = {"contract_number": "NW-2026-014", "title": "Master Services Agreement",
                 "parties": [{"name": "Northwind", "role": "Client"}, {"name": "Blue Harbor", "role": "Contractor"}],
                 "termination_date": "2028-02-28", "renewal_date": "2029-02-28", "auto_renews": True, "key_obligations": ["Pay within 45 days"]}
    chat, _ = make_chat({}, extracted)
    chat.store.remember("NW-2026-014", {"contract_value_usd": "250000"})
    session = await started(chat)
    await chat.learn_document(session, "msa.pdf", (SAMPLES / "sample_contract.pdf").read_bytes())
    assert session.contract == "NW-2026-014" and session.stage == "fields"
    assert session.get("RenewalDate").value == "2028-02-28" and session.get("RenewalDate").source == "document"
    assert session.get("Value").value == "250000" and session.get("Value").source == "memory"
    facts = chat.store.get_contract("NW-2026-014")["facts"]
    assert facts["parties"] == "name: Northwind, role: Client; name: Blue Harbor, role: Contractor"
    assert facts["auto_renews"] == "Yes" and "key_obligations" not in facts


@pytest.mark.asyncio
async def test_session_persists_across_engines(make_chat):
    chat, _ = make_chat()
    session = await started(chat, "NW-1")
    chat2, _ = make_chat()
    loaded, template = chat2.load(session.id)
    assert loaded.contract == "NW-1" and loaded.current().name == "Vendor" and template.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_non_fillable_pdf_rejected(make_chat):
    chat, _ = make_chat()
    with pytest.raises(FillError):
        await chat.start("invoice.pdf", (SAMPLES / "sample_invoice.pdf").read_bytes())
