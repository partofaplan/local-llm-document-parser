"""Guided form filling: a chat that walks the user through a fillable PDF one question at a time.

Stages:  contract -> fields -> review -> done

- contract: ask which contract the form is for. A known contract pre-fills every field whose fact
  (contract_number, renewal_date, ...) is already in contract memory.
- fields:   ask for each remaining field. Unambiguous replies (yes/no, a date, a short value) are taken
  as-is; anything else goes to the LLM, which maps the reply onto one or more fields.
- review:   show every value and let the user correct any of them before the PDF is written.
- done:     the filled PDF can be downloaded; further corrections go back through review.

Answers to fields marked `remember` are saved to contract memory as soon as they are given.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from .config import Settings
from .extractor import extract
from .filler import FillError, field_labels, form_fields, to_text, write_form
from .llm import LLMError, OllamaClient
from .pdf_parser import parse_pdf
from .prompts import PromptNotFound, PromptRegistry
from .store import Store, contract_key

KINDS = ["text", "date", "number", "yes_no", "choice"]
YES = {"yes", "y", "yeah", "yep", "true", "checked", "check", "tick", "x", "on"}
NO = {"no", "n", "nope", "false", "unchecked", "uncheck", "off", "none"}
NO_CONTRACT = {"none", "no", "n/a", "na", "no contract", "skip", "not applicable"}
COMMANDS = {
    "skip": {"skip", "pass", "leave blank", "leave it blank", "blank", "next", "dont know", "don't know", "idk"},
    "back": {"back", "go back", "previous", "undo"},
    "review": {"review", "summary", "show", "status", "show me"},
    "fill": {"fill", "done", "finish", "generate", "fill it", "fill the pdf", "fill it in", "create the pdf"},
    "help": {"help", "?", "commands"},
}
# Only a confirmation once the review is on screen; mid-questionnaire "yes" answers a yes/no field.
CONFIRM = {"yes", "y", "ok", "okay", "correct", "looks good", "all good", "lgtm", "go", "go ahead", "sure", "yep"}
# A short reply starting with one of these is a sentence about the value, not the value itself.
CHATTY = {"it", "it's", "its", "the", "same", "i", "i'm", "we", "use", "not", "that", "this", "they", "our", "my",
          "please", "just", "change", "actually", "set", "make", "put", "what", "why", "how", "can", "should"}
# Labels of per-submission fields, which are never saved to contract memory.
TRANSIENT = re.compile(r"^date$|^today|sign|comment|note|initial|remark|submitted|requested on", re.I)
# An LLM-proposed date or number must come from a reply that mentions one (small models fill in today's date).
DATEISH = re.compile(r"\d|today|tomorrow|yesterday|next|last|this|week|month|year|end of|start of|"
                     r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", re.I)
NUMBERISH = re.compile(r"\d|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|teen|twenty|"
                       r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|dozen|half", re.I)
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
                "%B %Y", "%b %Y"]
HELP = ("You can answer in your own words, answer several fields at once, or type:\n"
        "- **skip** to leave a field blank\n- **back** to redo the previous field\n"
        "- **review** to see everything so far\n- **fill** to create the PDF now\n\n"
        "Attach the contract itself (📎) and I'll pull its details out for you.")


# Parsing helpers -----------------------------------------------------------------------------------

def humanize(name: str) -> str:
    """'RenewalDate' / 'renewal_date' / 'form1.renewal-date' -> 'Renewal date'."""
    name = name.rsplit(".", 1)[-1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|[_\-]+", " ", name).split()
    return " ".join(words).capitalize() if words else name


def fact_key(text: str) -> str:
    """Canonical contract-memory key: 'Contract No.' / 'ContractNum' / 'contract #' -> 'contract_number'."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text.replace("#", " number "))
    key = "_".join(re.findall(r"[a-z0-9]+", text.lower()))
    return re.sub(r"_(no|num|nbr|nr)$", "_number", key)


def command(text: str, confirming: bool = False) -> str | None:
    t = re.sub(r"[.!]+$", "", text.strip().lower())
    if confirming and t in CONFIRM:
        return "fill"
    return next((c for c, words in COMMANDS.items() if t in words), None)


def parse_date(text: str) -> str | None:
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip().replace(",", " "))
    t = re.sub(r"\s+", " ", t)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_number(text: str) -> str | None:
    t = text.strip()
    if re.fullmatch(r"[$€£]?\s*-?[\d,]*\.?\d+\s*(%|[A-Za-z]{3})?", t):
        return re.sub(r"[$€£,\s]", "", t)
    return None


def days_until(iso: str) -> int | None:
    try:
        return (date.fromisoformat(iso) - date.today()).days
    except (TypeError, ValueError):
        return None


# State ---------------------------------------------------------------------------------------------

@dataclass
class Field:
    name: str                 # exact PDF form field name
    label: str
    type: str                 # PDF field type: text | button | choice
    kind: str = "text"        # how to ask and validate: one of KINDS
    options: list[str] | None = None
    question: str = ""
    fact: str | None = None   # contract-memory key, e.g. "renewal_date"
    remember: bool = True     # stable fact about the contract, as opposed to specific to this submission
    value: str | None = None
    status: str = "pending"   # pending | answered | skipped
    source: str | None = None  # you | memory | document

    def display(self) -> str:
        if self.status == "skipped" or self.value in (None, ""):
            return "_blank_"
        return self.value


@dataclass
class Session:
    id: str
    filename: str
    fields: list[Field]
    stage: str = "contract"
    contract: str | None = None
    messages: list[dict[str, str]] = field(default_factory=list)
    pending_facts: dict[str, str] = field(default_factory=dict)  # read from a document before a contract was chosen

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        return cls(**{**d, "fields": [Field(**f) for f in d["fields"]]})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def current(self) -> Field | None:
        return next((f for f in self.fields if f.status == "pending"), None) if self.stage == "fields" else None

    def get(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def say(self, text: str, kind: str | None = None) -> None:
        self.messages.append({"role": "assistant", "text": text, **({"kind": kind} if kind else {})})


# Engine --------------------------------------------------------------------------------------------

class FormChat:
    def __init__(self, settings: Settings, client: OllamaClient, store: Store, registry: PromptRegistry,
                 jobs: asyncio.Semaphore | None = None):
        self.s = settings
        self.client = client
        self.store = store
        self.registry = registry
        self.jobs = jobs or asyncio.Semaphore(1)

    async def _llm(self, system: str, user: str, schema: dict[str, Any], history: list[dict] | None = None,
                   max_tokens: int = 400) -> dict:
        # max_tokens bounds runaway generations (small models can loop inside constrained JSON).
        async with self.jobs:
            return await self.client.chat_json(
                model=self.s.model, system=system, user=user, schema=schema, history=history,
                options={"temperature": self.s.temperature, "num_ctx": self.s.num_ctx, "num_predict": max_tokens},
            )

    def load(self, session_id: str) -> tuple[Session, bytes] | None:
        found = self.store.load_session(session_id)
        return (Session.from_dict(found[0]), found[1]) if found else None

    def save(self, session: Session) -> None:
        self.store.save_session(session.id, session.to_dict())

    # Starting a session ---------------------------------------------------------------------------

    async def start(self, filename: str, template: bytes) -> Session:
        raw = form_fields(template)  # raises FillError for unreadable PDFs
        labels = field_labels(template)
        fields = []
        for name, info in raw.items():
            if info["type"] == "signature":
                continue
            states = [s.lstrip("/") for s in info.get("states") or [] if s != "/Off"]
            options = info.get("options") or (states if info["type"] == "button" and len(states) > 1 else None)
            label = info.get("label") or labels.get(name) or humanize(name)
            fields.append(Field(name=name, label=label, type=info["type"], options=options))
        if not fields:
            raise FillError("This PDF has no fillable form fields. Upload a fillable (AcroForm) PDF.")

        text = ""
        try:
            doc = await asyncio.to_thread(parse_pdf, template, max_pages=5, ocr_enabled=False)
            text = doc.full_text()
        except Exception:
            pass
        await self._plan(filename, fields, text)

        session = Session(id=uuid.uuid4().hex, filename=filename, fields=fields)
        session.say(f"I found **{len(fields)} fields** in _{filename}_. I'll ask about each one; "
                    "type **help** at any time to see what else I can do.")
        session.say(self._contract_prompt())
        self.store.save_session(session.id, session.to_dict(), template)
        return session

    def _known_facts(self) -> list[str]:
        keys = set(self.store.fact_keys()) | {"contract_number", "renewal_date"}
        try:
            keys |= {f.name for f in self.registry.get(self.s.contract_prompt).fields}
        except PromptNotFound:
            pass
        return sorted(keys)

    async def _plan(self, filename: str, fields: list[Field], form_text: str) -> None:
        """Ask the LLM for a question, kind and memory key per field; fall back to heuristics for anything missing."""
        known = self._known_facts()
        planned: dict[str, dict] = {}
        lines = "\n".join(f"- {f.name} | {f.label} | {f.type}" + (f" | options: {', '.join(f.options)}" if f.options else "")
                          for f in fields)
        user = (
            f'A user needs to fill in the PDF form "{filename}". For every form field below, provide:\n'
            "- question: one short, friendly question asking the user for this value.\n"
            "- kind: text, date, number, yes_no (checkboxes) or choice (a fixed list of options).\n"
            "- fact: a snake_case name for the underlying fact, e.g. contract_number, renewal_date, vendor_name. "
            f"Reuse one of these existing names whenever it means the same thing: {', '.join(known)}\n"
            "- remember: true if the value is a stable fact about the contract that will be the same on future "
            "forms (contract number, parties, contract dates, addresses, contacts); false if it is specific to "
            "this one submission (today's date, amounts for a period, signatures, comments).\n\n"
            f"Fields (name | label | type):\n{lines}\n\n"
            f"Text printed on the form, for context:\n<form>\n{form_text[:4000]}\n</form>"
        )
        # One object per field, keyed by field name: exactly one entry each and no open-ended list to loop in.
        # (No maxLength caps: llama.cpp expands them into huge grammars that slow sampling ~10x; num_predict bounds it.)
        entry = {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "kind": {"type": "string", "enum": KINDS},
                "fact": {"type": "string"},
                "remember": {"type": "boolean"},
            },
            "required": ["question", "kind", "fact", "remember"],
            "additionalProperties": False,
        }
        schema = {"type": "object", "properties": {f.name: entry for f in fields},
                  "required": [f.name for f in fields], "additionalProperties": False}
        try:
            result = await self._llm("You design short, friendly questionnaires for filling in PDF forms.", user, schema,
                                     max_tokens=80 * len(fields) + 100)
            planned = {name: p for name, p in result.items() if isinstance(p, dict)}
        except LLMError:
            pass  # the heuristics below still give a usable chat

        known_by_norm = {fact_key(k): k for k in known}
        for f in fields:
            p = planned.get(f.name, {})
            f.kind = self._kind(f, p.get("kind"))
            f.question = (p.get("question") or "").strip() or self._default_question(f)
            own = fact_key(f.label)
            # A field whose own label names a known fact always maps to it, whatever the model said.
            if TRANSIENT.search(f.label):
                # Per-submission: never remembered, and never mapped onto a contract fact such as effective_date.
                f.fact, f.remember = own or None, False
                continue
            f.fact = known_by_norm.get(own) or fact_key(p.get("fact") or "") or own or None
            # Small models are unreliable here, so facts the contract prompt or memory already tracks always count.
            f.remember = f.fact in known or (p["remember"] if isinstance(p.get("remember"), bool) else True)

    @staticmethod
    def _kind(f: Field, suggested: str | None) -> str:
        if f.type == "button":
            return "choice" if f.options else "yes_no"
        if f.type == "choice" or f.options:
            return "choice"
        if re.search(r"\bdate\b|\bdob\b", f.label, re.I):
            return "date"
        if re.search(r"amount|total|price|cost|fee|value|rate|qty|quantity", f.label, re.I):
            return "number"
        return suggested if suggested in ("text", "date", "number") else "text"

    @staticmethod
    def _default_question(f: Field) -> str:
        if f.kind == "yes_no":
            return f"Should **{f.label}** be checked?"
        if f.kind == "choice":
            return f"Which option for **{f.label}**?"
        return f"What should I put for **{f.label}**?"

    def _contract_prompt(self) -> str:
        known = self.store.list_contracts()[:8]
        msg = "Which **contract** is this form for? Type the contract number, or **none** if it isn't for one."
        if known:
            msg += "\n\nContracts I know about:\n" + "\n".join(
                f"- {c['number']}" + (f" — {c['facts']['title']}" if c["facts"].get("title") else "") for c in known)
        return msg

    # Conversation ---------------------------------------------------------------------------------

    async def reply(self, session: Session, text: str) -> Session:
        text = text.strip()
        if not text:
            return session
        session.messages.append({"role": "user", "text": text})
        if command(text) == "help":
            session.say(HELP)
        elif session.stage == "contract":
            await self._on_contract(session, text)
        elif session.stage == "fields":
            await self._on_field(session, text)
        else:
            await self._on_review(session, text)
        self.save(session)
        return session

    async def _on_contract(self, session: Session, text: str) -> None:
        if text.lower().strip(" .") in NO_CONTRACT:
            session.say("No problem. I won't link this form to a contract, so nothing will be remembered.")
            return self._advance(session)
        known = self.store.find_contract_in(text)
        number = known["number"] if known else await self._contract_number(text)
        if not number:
            session.say("I couldn't find a contract number in that. What is the contract number? "
                        "(or **none** if this form isn't for a contract)")
            return
        self._use_contract(session, number)
        self._advance(session)

    async def _contract_number(self, text: str) -> str | None:
        with_digits = [t.strip(".#") for t in re.findall(r"[#\w][\w\-/.#]*", text) if re.search(r"\d", t)]
        if len(with_digits) == 1:  # "NW-2026-014", "it's contract #4471"
            return with_digits[0]
        try:
            result = await self._llm(
                "You extract identifiers from short messages.",
                f"The user was asked which contract a form is for and replied:\n<reply>{text}</reply>\n"
                "Return the contract number exactly as written, or null if the reply does not contain one.",
                {"type": "object", "properties": {"contract_number": {"type": ["string", "null"]}},
                 "required": ["contract_number"]},
            )
        except LLMError:
            return None
        number = (result.get("contract_number") or "").strip()
        return number or None

    def _use_contract(self, session: Session, number: str) -> None:
        existing = self.store.get_contract(number)
        from_document = session.pending_facts
        contract = self.store.remember(number, from_document)
        session.pending_facts = {}
        session.contract = contract["number"]
        filled = self._prefill(session, from_document, "document")
        filled += self._prefill(session, contract["facts"], "memory")
        filled.sort(key=session.fields.index)
        if existing is None:
            msg = f"Contract **{contract['number']}** is new to me. I'll remember its details for next time."
            filled = [f for f in filled if f.fact != "contract_number"]  # not news: they just typed it
        else:
            msg = f"Found contract **{contract['number']}**."
        if filled:
            msg += " I filled in these fields from what I know:\n" + "\n".join(f"- {f.label}: {f.value}" for f in filled)
            msg += "\n\nTell me if any of them are wrong."
        if (days := days_until(contract["facts"].get("renewal_date"))) is not None:
            when = f"in {days} days" if days >= 0 else f"{-days} days ago"
            msg += f"\n\n📅 This contract renews on **{contract['facts']['renewal_date']}** ({when})."
        session.say(msg)

    def _prefill(self, session: Session, facts: dict[str, Any], source: str) -> list[Field]:
        filled = []
        for f in session.fields:
            if f.status == "pending" and f.fact and facts.get(f.fact) not in (None, ""):
                value = self._normalize(f, to_text(facts[f.fact]))
                if value is not None:
                    f.value, f.status, f.source = value, "answered", source
                    filled.append(f)
        return filled

    async def _on_field(self, session: Session, text: str) -> None:
        current = session.current()
        cmd = command(text)
        if cmd == "skip" and current:
            current.status, current.value, current.source = "skipped", None, "you"
        elif cmd == "back":
            if not self._reopen_previous(session, current):
                session.say("We're at the first question already.")
        elif cmd == "review":  # progress so far; the questionnaire carries on where it was
            session.say(self._review_text(session, final=False))
        elif cmd == "fill":
            for f in session.fields:
                if f.status == "pending":
                    f.status = "skipped"
            return self._finish(session)
        elif not await self._take_answer(session, text, current):
            return  # a clarifying reply or error was given; the question stays open
        self._advance(session)

    def _reopen_previous(self, session: Session, current: Field | None) -> bool:
        idx = session.fields.index(current) if current else len(session.fields)
        for f in reversed(session.fields[:idx]):
            if f.source not in ("memory", "document"):
                f.status, f.value = "pending", None
                return True
        return False

    async def _on_review(self, session: Session, text: str) -> None:
        cmd = command(text, confirming=True)
        if cmd == "fill":
            return self._finish(session)
        if cmd == "back":
            session.stage = "fields"
            self._reopen_previous(session, None)
            return self._advance(session)
        if cmd == "review":
            return session.say(self._review_text(session))
        if await self._take_answer(session, text, None):
            self._advance(session)

    async def _take_answer(self, session: Session, text: str, current: Field | None) -> bool:
        """Apply the reply to the form. Returns True when the conversation should move on."""
        updates, reply, ask_about = {}, None, None
        quick = self._quick_value(current, text) if current else None
        if quick is not None:
            updates = {current.name: quick}
        else:
            try:
                if text.rstrip().endswith("?"):
                    # A question: answer it, with no chance of the model recording a value it made up.
                    reply = await self._answer_question(session, text, current)
                else:
                    updates, reply, ask_about = await self._interpret(session, text, current)
                    updates = {n: v for n, v in updates.items() if self._grounded(session, session.get(n), v, text)}
            except LLMError:
                session.say("I couldn't reach the language model just now. Please try again, "
                            "or type just the value on its own.")
                return False
        applied = self._apply(session, updates)
        if ask_about and (f := session.get(ask_about)) and not applied:
            f.status, f.value = "pending", None
            session.stage = "fields"
            return True
        if reply and not applied:
            session.say(reply)
        elif not applied:
            session.say("Sorry, I didn't catch that." + (f" {self._ask_text(current)}" if current else ""))
        elif session.stage in ("review", "done") or applied != [current.name]:
            # Say what changed whenever it isn't simply the answer to the question just asked.
            session.say("Got it: " + ", ".join(f"{session.get(n).label} → {session.get(n).display()}" for n in applied))
        return bool(applied)

    def _quick_value(self, f: Field, text: str) -> str | None:
        """Take unambiguous replies without an LLM round trip (each call costs seconds on CPU)."""
        t = text.strip()
        if not t or "?" in t:
            return None
        low = t.lower().strip(" .!")
        if f.kind == "yes_no":
            return "Yes" if low in YES else "No" if low in NO else None
        if f.kind == "choice":
            return next((o for o in f.options or [] if o.lower() == low), None)
        if f.kind == "date":
            return parse_date(t)
        if f.kind == "number":
            return parse_number(t)
        words = t.split()
        if len(words) <= 5 and words[0].lower() not in CHATTY and low not in YES | NO:
            return t
        return None

    async def _interpret(self, session: Session, text: str, current: Field | None) -> tuple[dict, str | None, str | None]:
        lines = []
        for f in session.fields:
            desc = f"- {f.name} | {f.label} | {f.kind}"
            if f.options:
                desc += f" (options: {', '.join(f.options)})"
            lines.append(desc + f" | current value: {f.value if f.value not in (None, '') else 'empty'}")
        focus = (f'The question just asked was about {current.name}: "{current.question}"' if current
                 else "The user is reviewing all values before the PDF is created.")
        user = (
            f"Form fields (name | label | kind | current value):\n" + "\n".join(lines) + "\n\n"
            f"{focus}\nToday's date is {date.today().isoformat()}.\n\n"
            f"The user replied:\n<reply>{text}</reply>\n\n"
            "Return:\n"
            "- answers: one entry for each field the reply gives a value for (usually just the field asked about). "
            "Use the exact field names above. Write dates as YYYY-MM-DD, numbers without currency symbols or "
            "thousands separators, \"Yes\" or \"No\" for yes_no fields, and one of the listed options for choice fields. "
            "Never invent a value the user did not give.\n"
            "- ask_about: if the user wants to change a field but did not say the new value, that field's name; otherwise null.\n"
            "- reply: if the user asked a question or the reply is unclear, a short helpful response ending with "
            "the question to answer; otherwise null."
        )
        names = [f.name for f in session.fields]
        schema = {
            "type": "object",
            "properties": {
                "answers": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"field": {"type": "string", "enum": names},
                                   "value": {"type": "string"}},
                    "required": ["field", "value"],
                }},
                "ask_about": {"type": ["string", "null"], "enum": [*names, None]},
                "reply": {"type": ["string", "null"]},
            },
            "required": ["answers", "ask_about", "reply"],
        }
        history = [{"role": m["role"], "content": m["text"]} for m in session.messages[-7:-1]]
        result = await self._llm(
            "You help a user fill in a PDF form through conversation by turning their replies into form field "
            "values. You only record values the user actually gave.", user, schema, history)
        updates = {a["field"]: a["value"] for a in result.get("answers") or []
                   if isinstance(a, dict) and a.get("field") in names and a.get("value") not in (None, "")}
        return updates, (result.get("reply") or None), (result.get("ask_about") or None)

    async def _answer_question(self, session: Session, text: str, current: Field | None) -> str:
        about = (f'They are being asked for "{current.label}" ({current.kind}): "{current.question}"' if current
                 else "They are reviewing the form before it is filled.")
        labels = ", ".join(f.label for f in session.fields)
        contract = f" It is for contract {session.contract}." if session.contract else ""
        result = await self._llm(
            "You help a user fill in a PDF form. Answer their question briefly and plainly. You cannot see any "
            "document other than the form's field list, so never state facts about their contract or guess values.",
            f'The form is "{session.filename}" with fields: {labels}.{contract}\n{about}\n\n'
            f"The user asked:\n<question>{text}</question>\n\n"
            "Reply in one to three short sentences, then ask them for the value again.",
            {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]},
            history=[{"role": m["role"], "content": m["text"]} for m in session.messages[-5:-1]],
        )
        return (result.get("reply") or "").strip() or (self._ask_text(current) if current else self._review_text(session))

    @staticmethod
    def _grounded(session: Session, f: Field | None, value: str, reply: str) -> bool:
        """Is an LLM-proposed value supported by what the user wrote (or by values already on the form)?"""
        if f is None or f.kind in ("yes_no", "choice"):
            return f is not None  # validated against YES/NO or the options by _normalize
        if f.kind == "date":
            return bool(DATEISH.search(reply))
        if f.kind == "number":
            return bool(NUMBERISH.search(reply))
        words = re.findall(r"[a-z0-9]+", value.lower())
        context = reply + " " + " ".join(x.value or "" for x in session.fields)  # "same as the client"
        have = set(re.findall(r"[a-z0-9]+", context.lower()))
        return bool(words) and sum(w in have for w in words) * 2 >= len(words)

    def _normalize(self, f: Field, value: str) -> str | None:
        v = value.strip()
        if f.kind == "yes_no":
            low = v.lower()
            return "Yes" if low in YES else "No" if low in NO else None
        if f.kind == "choice":
            return next((o for o in f.options or [] if o.lower() == v.lower()), None)
        if f.kind == "date":
            return parse_date(v) or v
        return v or None

    def _apply(self, session: Session, updates: dict[str, str]) -> list[str]:
        applied, facts = [], {}
        for name, raw in updates.items():
            f = session.get(name)
            value = self._normalize(f, raw) if f else None
            if value is None:
                continue
            f.value, f.status, f.source = value, "answered", "you"
            applied.append(name)
            if f.remember and f.fact:
                facts[f.fact] = value
        if facts and session.contract:
            self.store.remember(session.contract, facts)
        return applied

    def _advance(self, session: Session) -> None:
        """Ask the next question, or move to review when every field has an answer."""
        if session.stage in ("contract", "fields"):
            session.stage = "fields"
            if (nxt := session.current()) is not None:
                return session.say(self._ask_text(nxt))
            session.stage = "review"
            return session.say(self._review_text(session))
        if session.stage == "done":
            session.stage = "review"

    def _ask_text(self, f: Field) -> str:
        q = f.question
        if f.kind == "choice" and f.options:
            q += "\n\nOptions: " + ", ".join(f.options)
        elif f.kind == "yes_no" and "yes" not in q.lower():
            q += " (yes or no)"
        return q

    def _review_text(self, session: Session, final: bool = True) -> str:
        rows = []
        for f in session.fields:
            if not final and f.status == "pending":
                continue
            note = {"memory": " _(remembered)_", "document": " _(from document)_"}.get(f.source or "", "")
            rows.append(f"- **{f.label}**: {f.display()}{note}")
        if not final:
            left = sum(1 for f in session.fields if f.status == "pending")
            return (("So far:\n" + "\n".join(rows) if rows else "Nothing filled in yet.") +
                    f"\n\n{left} to go. Say **fill** to create the PDF now and leave the rest blank.")
        return ("Here's everything I have:\n" + "\n".join(rows) +
                "\n\nReply **fill** to create the PDF, or tell me what to change.")

    def _finish(self, session: Session) -> None:
        session.stage = "done"
        blank = sum(1 for f in session.fields if f.status != "answered")
        msg = "Your PDF is ready."
        if blank:
            msg += f" {blank} field{'s were' if blank != 1 else ' was'} left blank."
        if session.contract:
            msg += f" I've saved what I learned about contract **{session.contract}** for next time."
        session.say(msg + " You can still tell me about changes and download it again.", kind="download")

    # Supporting documents -------------------------------------------------------------------------

    async def learn_document(self, session: Session, filename: str, data: bytes) -> Session:
        """Read a contract (or similar) PDF, remember its facts and pre-fill matching fields."""
        s = self.s
        session.messages.append({"role": "user", "text": f"📎 {filename}"})
        spec = self.registry.get(s.contract_prompt)
        doc = await asyncio.to_thread(parse_pdf, data, max_pages=s.max_pages, ocr_enabled=s.ocr_enabled,
                                      ocr_min_chars=s.ocr_min_chars, ocr_dpi=s.ocr_dpi, ocr_lang=s.ocr_lang)
        async with self.jobs:
            result = await extract(self.client, spec, doc.full_text(), default_model=s.model,
                                   chunk_chars=s.chunk_chars, chunk_overlap=s.chunk_overlap, max_chunks=s.max_chunks,
                                   base_options={"temperature": s.temperature, "num_ctx": s.num_ctx})
        facts = {}
        for key, value in result.data.items():
            if isinstance(value, list) and value and not all(isinstance(v, dict) for v in value):
                continue  # free-text lists such as obligations are summaries, not reusable facts
            if text := to_text(value):
                facts[key] = text
        # The current term's end is when the contract renews or expires. qwen2.5:3b tends to add a renewal
        # period to it ("renews for one-year terms"), so the stated end date wins.
        if facts.get("termination_date"):
            facts["renewal_date"] = facts["termination_date"]
        if not facts:
            session.say(f"I couldn't find any contract details in _{filename}_.")
            self.save(session)
            return session

        learned = f"I read _{filename}_ and learned:\n" + "\n".join(f"- {humanize(k)}: {v}" for k, v in facts.items())
        number = facts.get("contract_number")
        if session.contract and number and contract_key(number) != contract_key(session.contract):
            self.store.remember(number, facts)
            session.say(f"_{filename}_ looks like contract **{number}**, not **{session.contract}**, so I saved "
                        f"its details under {number} and didn't use them for this form.")
        elif session.contract:
            self.store.remember(session.contract, facts)
            filled = self._prefill(session, facts, "document")
            session.say(learned + f"\n\nSaved to contract **{session.contract}**."
                        + (f" That filled in {len(filled)} field(s)." if filled else ""))
        elif number and session.stage == "contract":
            session.say(learned)
            session.pending_facts = facts
            self._use_contract(session, number)
            session.stage = "fields"
        else:
            filled = self._prefill(session, facts, "document")
            msg = learned + (f"\n\nThat filled in {len(filled)} field(s)." if filled else "")
            if session.stage == "contract":
                session.pending_facts.update(facts)
                session.say(msg + "\n\nIt doesn't show a contract number. Which contract is this form for? "
                                  "(or **none**)")
                self.save(session)
                return session
            session.say(msg)

        if session.stage == "fields":
            self._advance(session)
        elif session.stage in ("review", "done"):
            session.stage = "review"
            session.say(self._review_text(session))
        self.save(session)
        return session

    # Output ---------------------------------------------------------------------------------------

    def fill(self, session: Session, template: bytes) -> bytes:
        values: dict[str, Any] = {}
        for f in session.fields:
            if f.status != "answered" or f.value in (None, ""):
                continue
            value = f.value
            if f.kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                value = date.fromisoformat(value).strftime(self.s.form_date_format)
            values[f.name] = value
        pdf, _ = write_form(template, values)
        return pdf

    def public(self, session: Session) -> dict[str, Any]:
        contract = self.store.get_contract(session.contract) if session.contract else None
        current = session.current()
        return {
            "id": session.id,
            "filename": session.filename,
            "stage": session.stage,
            "current": current.name if current else None,
            "contract": contract,
            "fields": [{k: getattr(f, k) for k in ("name", "label", "kind", "value", "status", "source", "remember")}
                       for f in session.fields],
            "messages": session.messages,
        }
