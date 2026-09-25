from datetime import timedelta

from app.store import Store, contract_key


def test_contract_key_ignores_case_and_punctuation():
    assert contract_key("nw-2026 014") == contract_key("NW_2026_014") == "NW2026014"


def test_remember_merges_and_keeps_number(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.remember("NW-2026-014", {"renewal_date": "2028-02-28", "empty": ""})
    store.remember("nw 2026 014", {"vendor": "Blue Harbor"})
    c = store.get_contract("NW2026014")
    assert c["number"] == "NW-2026-014"
    assert c["facts"] == {"contract_number": "NW-2026-014", "renewal_date": "2028-02-28", "vendor": "Blue Harbor"}
    assert store.fact_keys() == {"contract_number", "renewal_date", "vendor"}


def test_find_contract_in_text_prefers_longest_match(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.remember("A1", {})
    store.remember("A1-22", {})
    assert store.find_contract_in("it's for contract a1-22 please")["number"] == "A1-22"
    assert store.find_contract_in("nothing here") is None


def test_forget_and_delete(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.remember("X9", {"a": "1"})
    assert store.forget_fact("X9", "a")["facts"] == {"contract_number": "X9"}
    assert store.delete_contract("x9") and not store.delete_contract("x9")
    assert store.forget_fact("X9", "a") is None


def test_sessions_round_trip_and_purge(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.save_session("s1", {"stage": "contract"}, b"%PDF-1")
    store.save_session("s1", {"stage": "fields"})
    assert store.load_session("s1") == ({"stage": "fields"}, b"%PDF-1")
    assert store.purge_sessions(timedelta(hours=1)) == 0
    assert store.purge_sessions(timedelta(hours=-1)) == 1
    assert store.load_session("s1") is None
