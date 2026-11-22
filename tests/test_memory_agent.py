import asyncio
import json

import pytest

from conftest import run
from kuro.llm import ChatResult, LLMUnavailable
from kuro.memory import MemoryStore
from kuro.memory_agent import MemoryAgent, parse_ops

G = 111
MARBUS, GOMATI = 1001, 1


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        reply = self.reply(messages) if callable(self.reply) else self.reply
        return ChatResult(text=reply)


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "k.sqlite3")
    yield s
    s.close()


def talk(store):
    store.add_message(5, "user", "Ich hab mir einen Broil-King-Grill gekauft, heute wird eingeweiht!",
                      guild_id=G, author_id=MARBUS, author_name="Marbus")
    store.add_message(5, "assistant", "NYAAA, schicker Grill, du Glutmagier!", guild_id=G)
    store.add_message(5, "user", "Merk dir: Marbus ist ab jetzt offiziell der Grillkapitän.",
                      guild_id=G, author_id=GOMATI, author_name="Gomati", is_owner=True)


def test_parse_ops_accepts_fenced_json_and_drops_garbage():
    raw = """```json
    {"ops": [
      {"op": "remember", "tier": "long", "about": "U1", "kind": "nonsense", "text": "Marbus mag Met.", "importance": 9},
      {"op": "remember", "tier": "short", "about": "server", "text": "Heute wird gegrillt.", "ttl_hours": 9999},
      {"op": "remember", "tier": "forever", "about": "U1", "text": "ungültige Stufe"},
      {"op": "remember", "tier": "long", "about": "U1", "text": ""},
      {"op": "update", "id": "x", "text": "kaputt"},
      {"op": "forget", "id": 3},
      {"op": "rm -rf", "id": 1},
      "quatsch"
    ]}
    ```"""
    ops = parse_ops(raw)
    assert [o["op"] for o in ops] == ["remember", "remember", "forget"]
    assert ops[0]["kind"] == "fact" and ops[0]["importance"] == 5
    assert ops[1]["ttl_hours"] == 336
    assert parse_ops("Ich finde nichts Merkenswertes.") == []
    assert parse_ops('{"ops": "nein"}') == [] and parse_ops("") == []


def test_agent_stores_long_and_short_term_and_marks_messages(store):
    talk(store)
    llm = FakeLLM(json.dumps({"ops": [
        {"op": "remember", "tier": "long", "about": "U1", "kind": "fact",
         "text": "Marbus besitzt einen Broil-King-Grill.", "importance": 3},
        {"op": "remember", "tier": "short", "about": "server", "kind": "event",
         "text": "Heute wird Marbus' neuer Grill eingeweiht.", "importance": 2, "ttl_hours": 12},
        {"op": "remember", "tier": "long", "about": "U9", "text": "Unbekannte Kennung wird verworfen."},
    ]}))
    agent = MemoryAgent(store, llm)
    assert run(agent.run_channel(5, G)) == 2

    memories = store.list_memories(G)
    by_tier = {m.tier: m for m in memories}
    assert by_tier["long"].subject_id == MARBUS and by_tier["long"].subject_name == "Marbus"
    assert by_tier["short"].subject_id is None and by_tier["short"].expires_at is not None
    assert store.count_unprocessed(5) == 0

    prompt = llm.calls[0][0][1]["content"]
    assert "[U1] Marbus:" in prompt and "Kuros Ehemann/Betreiber" in prompt
    assert str(MARBUS) not in prompt, "keine Discord-IDs ans Modell"
    assert llm.calls[0][1]["json_mode"] is True


def test_agent_can_update_promote_and_forget_only_known_entries(store):
    keep = store.add_memory(G, "Marbus plant einen Grillabend.", tier="short", subject_id=MARBUS,
                            subject_name="Marbus")
    gone = store.add_memory(G, "Marbus trinkt nur Bier.", subject_id=MARBUS, subject_name="Marbus")
    foreign = store.add_memory(999, "Erinnerung eines anderen Servers.")
    talk(store)
    llm = FakeLLM(json.dumps({"ops": [
        {"op": "promote", "id": keep},
        {"op": "update", "id": keep, "text": "Marbus grillt regelmäßig für die Taverne."},
        {"op": "forget", "id": gone},
        {"op": "forget", "id": foreign},
    ]}))
    assert run(MemoryAgent(store, llm).run_channel(5, G)) == 3
    assert store.get_memory(keep).tier == "long" and "regelmäßig" in store.get_memory(keep).content
    assert store.get_memory(gone) is None
    assert store.get_memory(foreign) is not None, "fremde Einträge bleiben unberührt"
    assert f"#{keep} [short]" in llm.calls[0][0][1]["content"]


def test_agent_survives_nonsense_and_skips_assistant_only_batches(store):
    store.add_message(5, "assistant", "Nya~", guild_id=G)
    llm = FakeLLM("keine Ahnung")
    agent = MemoryAgent(store, llm)
    assert run(agent.run_channel(5, G)) == 0 and llm.calls == []
    talk(store)
    assert run(agent.run_channel(5, G)) == 0
    assert store.list_memories(G) == [] and store.count_unprocessed(5) == 0


def test_notify_runs_after_threshold_and_waits_while_voice_is_busy(store):
    async def scenario():
        busy = {"value": True}
        llm = FakeLLM('{"ops": []}')
        agent = MemoryAgent(store, llm, min_new_messages=3, idle_seconds=60,
                            is_busy=lambda: busy["value"], busy_poll_seconds=0.05)
        talk(store)                       # 3 Nachrichten -> Schwelle erreicht
        agent.notify(5, G)
        await asyncio.sleep(0.15)
        assert llm.calls == [], "wartet, solange Kuro gerade spricht"
        busy["value"] = False
        for _ in range(80):
            await asyncio.sleep(0.1)
            if llm.calls:
                break
        assert len(llm.calls) == 1
        agent.close()

    run(scenario())


def test_sleeping_model_leaves_messages_for_later(store):
    class Asleep:
        async def chat(self, messages, **kwargs):
            raise LLMUnavailable("Rechner aus")

    async def scenario():
        agent = MemoryAgent(store, Asleep(), min_new_messages=1)
        talk(store)
        agent.notify(5, G)
        await asyncio.sleep(0.2)
        agent.close()

    run(scenario())     # darf nicht abstürzen
    assert store.count_unprocessed(5) == 3 and store.list_memories(G) == []


def test_explicit_remember_request_is_evaluated_at_once(store):
    from kuro.memory_agent import SYSTEM_PROMPT, is_remember_request

    for text in ("Merk dir: ich mag Met", "kuro, merke dir das bitte", "Vergiss nicht, dass ich Sonntag Geburtstag habe",
                 "denk dran, morgen ist Grillen", "Behalte das für dich"):
        assert is_remember_request(text), text
    for text in ("Ich merke gerade, dass ich müde bin", "Hallo Kuro", "Vergissmeinnicht ist eine Blume"):
        assert not is_remember_request(text), text
    assert "IMMER" in SYSTEM_PROMPT

    async def scenario():
        llm = FakeLLM('{"ops": []}')
        agent = MemoryAgent(store, llm, min_new_messages=6, idle_seconds=60)
        store.add_message(5, "user", "Merk dir: ich trinke am liebsten Met.", guild_id=G,
                          author_id=MARBUS, author_name="Marbus")
        agent.notify(5, G, urgent=True)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if llm.calls:
                break
        assert len(llm.calls) == 1, "nicht 60 Sekunden gewartet"
        agent.close()

    run(scenario())



def test_opted_out_speakers_are_not_evaluated(store):
    store.set_opted_out(MARBUS, True)
    talk(store)
    llm = FakeLLM('{"ops": []}')
    run(MemoryAgent(store, llm).run_channel(5, G))
    prompt = llm.calls[0][0][1]["content"]
    assert "Broil-King" not in prompt and "] Marbus:" not in prompt, "Marbus' eigene Nachricht fehlt"
    assert "Gomati" in prompt
    assert store.count_unprocessed(5) == 0
