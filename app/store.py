"""SQLite persistence for contract memory and chat sessions.

Contract memory holds facts about each contract (contract number, renewal date, parties...) keyed by a
canonical fact name, so any form that asks for the same fact can be pre-filled from it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    key TEXT PRIMARY KEY,          -- normalised contract number, used for lookups
    number TEXT NOT NULL,          -- contract number as the user first wrote it
    facts TEXT NOT NULL,           -- JSON {fact_key: value}
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,           -- JSON chat state
    template BLOB NOT NULL,        -- the uploaded fillable PDF
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contract_key(number: str) -> str:
    """'abc 123', 'ABC-123' and 'abc_123' refer to the same contract."""
    return re.sub(r"[^A-Z0-9]", "", number.upper())


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # Contracts ------------------------------------------------------------------------------------

    def _contract(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {"number": row["number"], "facts": json.loads(row["facts"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get_contract(self, number: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM contracts WHERE key = ?", (contract_key(number),)).fetchone()
        return self._contract(row)

    def list_contracts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM contracts ORDER BY updated_at DESC").fetchall()
        return [self._contract(r) for r in rows]

    def find_contract_in(self, text: str) -> dict[str, Any] | None:
        """A known contract whose number appears in free text, e.g. 'it's for NW-2026-014'."""
        haystack = contract_key(text)
        best = None
        for c in self.list_contracts():
            key = contract_key(c["number"])
            if key and key in haystack and (best is None or len(key) > len(contract_key(best["number"]))):
                best = c
        return best

    def remember(self, number: str, facts: dict[str, Any]) -> dict[str, Any]:
        """Create the contract if needed and merge facts into it. Empty values are ignored."""
        key = contract_key(number)
        if not key:
            raise ValueError("contract number is empty")
        facts = {k: v for k, v in facts.items() if v not in (None, "", [])}
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM contracts WHERE key = ?", (key,)).fetchone()
            now = _now()
            if row is None:
                merged = {"contract_number": number, **facts}
                self._db.execute("INSERT INTO contracts VALUES (?, ?, ?, ?, ?)",
                                 (key, number, json.dumps(merged), now, now))
            else:
                merged = {**json.loads(row["facts"]), **facts}
                self._db.execute("UPDATE contracts SET facts = ?, updated_at = ? WHERE key = ?",
                                 (json.dumps(merged), now, key))
        return self.get_contract(number)

    def forget_fact(self, number: str, fact: str) -> dict[str, Any] | None:
        with self._lock, self._db:
            row = self._db.execute("SELECT facts FROM contracts WHERE key = ?", (contract_key(number),)).fetchone()
            if row is None:
                return None
            facts = json.loads(row["facts"])
            facts.pop(fact, None)
            self._db.execute("UPDATE contracts SET facts = ?, updated_at = ? WHERE key = ?",
                             (json.dumps(facts), _now(), contract_key(number)))
        return self.get_contract(number)

    def delete_contract(self, number: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM contracts WHERE key = ?", (contract_key(number),))
        return cur.rowcount > 0

    def fact_keys(self) -> set[str]:
        return {k for c in self.list_contracts() for k in c["facts"]}

    # Sessions -------------------------------------------------------------------------------------

    def save_session(self, session_id: str, state: dict[str, Any], template: bytes | None = None) -> None:
        with self._lock, self._db:
            if template is None:
                self._db.execute("UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                                 (json.dumps(state), _now(), session_id))
            else:
                self._db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)",
                                 (session_id, json.dumps(state), template, _now()))

    def load_session(self, session_id: str) -> tuple[dict[str, Any], bytes] | None:
        with self._lock:
            row = self._db.execute("SELECT state, template FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return (json.loads(row["state"]), bytes(row["template"])) if row else None

    def purge_sessions(self, older_than: timedelta) -> int:
        cutoff = (datetime.now(timezone.utc) - older_than).isoformat(timespec="seconds")
        with self._lock, self._db:
            return self._db.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,)).rowcount
