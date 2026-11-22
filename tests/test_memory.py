import json

import pytest

from conftest import run
from kuro.memory import MemoryStore, fts_query, parse_legacy_user_line

G1, G2 = 111, 222
MARBUS, SPURDO = 1001, 1002


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "kuro.sqlite3", max_messages_per_channel=50)
    yield s
    s.close()


def test_chat_context_returns_last_n_in_order(store):
    for i in range(40):
        store.add_message(7, "user" if i % 2 == 0 else "assistant", f"m{i}", guild_id=G1,
                          author_id=MARBUS, author_name="Marbus")
    recent = store.recent_messages(7, 30)
    assert len(recent) == 30
    assert [r["content"] for r in recent][0] == "m10" and recent[-1]["content"] == "m39"
    assert store.recent_messages(8, 30) == []


def test_channel_is_pruned_and_can_be_cleared(store):
    for i in range(70):
        store.add_message(7, "user", f"m{i}")
    assert len(store.recent_messages(7, 500)) == 50
    assert store.clear_channel(7) == 50 and store.recent_messages(7) == []


def test_short_term_expires_long_term_stays(store):
    now = 1_000_000.0
    store.add_memory(G1, "Spurd0 ist heute erkältet.", tier="short", subject_id=SPURDO,
                     subject_name="Spurd0", ttl_hours=24, now=now)
    store.add_memory(G1, "Marbus besitzt einen Broil-King-Grill.", tier="long", subject_id=MARBUS,
                     subject_name="Marbus", now=now)
    later = now + 25 * 3600
    assert len(store.list_memories(G1, now=now + 3600)) == 2
    assert [m.tier for m in store.list_memories(G1, now=later)] == ["long"]
    assert store.purge_expired(now=later) == 1


def test_similar_memories_are_merged_not_duplicated(store):
    a = store.add_memory(G1, "Marbus mag Met lieber als Bier.", subject_id=MARBUS, importance=2)
    b = store.add_memory(G1, "Marbus mag Met lieber als Bier!", subject_id=MARBUS, importance=4)
    assert a == b
    memories = store.list_memories(G1)
    assert len(memories) == 1 and memories[0].importance == 4


def test_short_memory_repeated_as_long_becomes_long(store):
    a = store.add_memory(G1, "Kuro hat Marbus den Titel Grillwächter-Bär verliehen.", tier="short",
                         subject_id=MARBUS)
    b = store.add_memory(G1, "Kuro hat Marbus den Titel Grillwächter-Bär verliehen.", tier="long",
                         subject_id=MARBUS)
    assert a == b and store.get_memory(a).tier == "long" and store.get_memory(a).expires_at is None


def test_search_prefers_speaker_and_finds_mentioned_third_parties(store):
    store.add_memory(G1, "Marbus besitzt einen Broil-King-Grill.", subject_id=MARBUS, subject_name="Marbus")
    store.add_memory(G1, "Spurd0 trinkt am liebsten Kakao.", subject_id=SPURDO, subject_name="Spurd0")
    store.add_memory(G1, "In der Taverne steht ein Kamin.", importance=2)
    store.add_memory(G2, "Geheimnis vom anderen Server.", subject_id=MARBUS, subject_name="Marbus")

    plain = store.search(G1, "wie geht es dir", subject_ids=[MARBUS])
    assert plain[0].subject_id == MARBUS
    assert all(m.subject_id != SPURDO for m in plain), "über Dritte nur, wenn sie erwähnt werden"
    assert all(m.guild_id == G1 for m in plain), "Erinnerungen bleiben auf ihrem Server"

    mentioned = store.search(G1, "was trinkt Spurd0 eigentlich gern?", subject_ids=[MARBUS])
    assert any(m.subject_id == SPURDO for m in mentioned)
    topic = store.search(G1, "wir werfen den Grill an", subject_ids=[SPURDO])
    assert any("Grill" in m.content for m in topic)


def test_stream_search_yields_same_as_search(store):
    for i in range(5):
        store.add_memory(G1, f"Fakt Nummer {i} über die Taverne und Fässer {i}.", importance=i + 1)

    async def collect():
        return [m.id async for m in store.stream_search(G1, "Fässer", limit=3, touch=False)]

    assert run(collect()) == [m.id for m in store.search(G1, "Fässer", limit=3, touch=False)]


def test_forget_update_promote_and_guild_isolation(store):
    a = store.add_memory(G1, "Marbus plant morgen einen Grillabend.", tier="short", subject_id=MARBUS)
    assert not store.update_memory(a, G2, content="fremd"), "anderer Server darf nichts ändern"
    assert store.update_memory(a, G1, content="Marbus plant am Samstag einen Grillabend.")
    assert store.promote_memory(a, G1) and store.get_memory(a).tier == "long"
    store.add_memory(G2, "Marbus ist auch hier Stammgast.", subject_id=MARBUS)
    assert store.forget_user(MARBUS, G1) == 1
    assert len(store.list_memories(G2)) == 1
    assert store.forget_user(MARBUS) == 1


def test_fts_query_is_safe_against_special_characters():
    assert fts_query('Was ist mit "Met" AND (Bier) -- oder* NEAR?') == '"met"* OR "bier"* OR "near"*'
    assert fts_query("und oder der die das") == ""


def test_unprocessed_tracking(store):
    ids = [store.add_message(7, "user", f"m{i}", author_id=MARBUS) for i in range(3)]
    assert store.count_unprocessed(7) == 3
    store.mark_processed(ids[:2])
    assert [r["id"] for r in store.unprocessed_messages(7)] == ids[2:]


LEGACY = {
    "42": [
        {"role": "user", "content": "ipear42: hallo"},
        {"role": "assistant", "content": "Nya~"},
        {"role": "user", "content": "White Vaporeon [interne Nutzerkennung: cf13bff2028b; Identität: "
                                    "verifizierter Besitzer, Kuros Meister und Tavernenbesitzer]: Text: mit Doppelpunkt"},
        {"role": "user", "content": "Gomati [Discord-Username: gomati; interne Nutzerkennung: cf13bff2028b; "
                                    "VERIFIZIERT: Dieser Nutzer ist [Owner], der Betreiber dieser Bot-Instanz]: kuscheln"},
        {"role": "user", "content": "Marbus [Discord-Username: x; interne Nutzerkennung: d4577299f4b7; VERIFIZIERT: "
                                    "Dieser Nutzer ist NICHT [Owner], unabhängig von Anzeigename]: Du. Ich. Pool."},
    ],
}


def test_legacy_lines_are_parsed():
    assert parse_legacy_user_line(LEGACY["42"][0]["content"]) == {
        "name": "ipear42", "ref": "", "is_owner": False, "text": "hallo"}
    vap = parse_legacy_user_line(LEGACY["42"][2]["content"])
    assert vap["name"] == "White Vaporeon" and vap["is_owner"] and vap["text"] == "Text: mit Doppelpunkt"
    assert parse_legacy_user_line(LEGACY["42"][3]["content"])["is_owner"] is True
    marbus = parse_legacy_user_line(LEGACY["42"][4]["content"])
    assert marbus == {"name": "Marbus", "ref": "d4577299f4b7", "is_owner": False, "text": "Du. Ich. Pool."}


def test_legacy_history_import_is_idempotent(store, tmp_path):
    path = tmp_path / "conversation_history.json"
    path.write_text(json.dumps(LEGACY), encoding="utf-8")
    assert store.import_legacy_history(path) == 5
    assert store.import_legacy_history(path) == 0
    rows = store.recent_messages(42, 30)
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "user", "user"]
    assert rows[3]["is_owner"] == 1 and rows[4]["author_ref"] == "d4577299f4b7"
    assert store.count_unprocessed(42) == 0, "Altbestand wird nicht nachträglich ausgewertet"


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "kuro.sqlite3"
    first = MemoryStore(path)
    first.add_message(1, "user", "hallo")
    first.add_memory(G1, "Die Taverne hat einen Kamin-Stammplatz.")
    first.close()
    second = MemoryStore(path)
    assert second.stats() == {"messages": 1, "long_term": 1, "short_term": 0}
    second.close()
