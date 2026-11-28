from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kuro import reminders as r
from kuro.memory import MemoryStore

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 28, 11, 52, tzinfo=BERLIN)


def test_parse_and_resolve_relative_and_absolute():
    rel = r.parse_request({"text": "  Spülmaschine   ausräumen ", "minutes_from_now": 30})
    assert rel.text == "Spülmaschine ausräumen"
    assert r.resolve_due(rel, NOW) == NOW + timedelta(minutes=30)
    absolute = r.parse_request({"text": "Pizza", "at": "2026-11-29 08:00"})
    assert r.resolve_due(absolute, NOW) == datetime(2026, 11, 29, 8, 0, tzinfo=BERLIN)
    assert r.parse_request({"text": "x", "at": "", "minutes_from_now": 5}).minutes_from_now == 5


@pytest.mark.parametrize("data, message", [
    ({"text": "", "minutes_from_now": 5}, "Woran"),
    ({"text": "x"}, "genau eine Zeitangabe"),
    ({"text": "x", "minutes_from_now": 5, "at": "2026-11-28 13:00"}, "genau eine Zeitangabe"),
    ({"text": "x", "minutes_from_now": 2.5}, "ganze Zahl"),
    ({"text": "x" * 201, "minutes_from_now": 5}, "zu lang"),
])
def test_parse_rejects_bad_input(data, message):
    with pytest.raises(ValueError, match=message):
        r.parse_request(data)


def test_resolve_rejects_past_far_future_and_garbage():
    with pytest.raises(ValueError, match="Vergangenheit"):
        r.resolve_due(r.ReminderRequest("x", at="2026-11-28 09:00"), NOW)
    with pytest.raises(ValueError, match="7 Tage"):
        r.resolve_due(r.ReminderRequest("x", minutes_from_now=8 * 24 * 60), NOW)
    with pytest.raises(ValueError, match="Uhrzeit"):
        r.resolve_due(r.ReminderRequest("x", at="morgen früh"), NOW)


def test_format_when_and_texts():
    assert r.format_when(NOW + timedelta(minutes=30), NOW) == "in 30 Minuten (um 12:22 Uhr)"
    assert r.format_when(NOW + timedelta(minutes=1), NOW) == "in 1 Minute (um 11:53 Uhr)"
    assert r.format_when(NOW.replace(hour=18, minute=0), NOW) == "heute um 18:00 Uhr"
    assert r.format_when(datetime(2026, 11, 29, 8, 0, tzinfo=BERLIN), NOW) == "morgen um 08:00 Uhr"
    assert r.format_when(datetime(2026, 12, 2, 9, 0, tzinfo=BERLIN), NOW) == "am 02.12. um 09:00 Uhr"
    confirm = r.confirmation_text("Spülmaschine", "in 30 Minuten (um 12:22 Uhr)")
    assert "Spülmaschine" in confirm and "DM" in confirm
    assert "–" not in confirm and "—" not in confirm
    late = r.delivery_text("<@1>", "Spülmaschine", late_seconds=600)
    assert late.startswith("<@1>") and "Spülmaschine" in late and "verspätet" in late
    assert "verspätet" not in r.delivery_text("<@1>", "x", late_seconds=10)


def test_store_limit_due_and_forget(tmp_path):
    store = MemoryStore(tmp_path / "k.sqlite3")
    ids = [store.add_reminder(7, 1, 10, f"r{i}", 100 + i, limit=3) for i in range(3)]
    assert all(ids) and store.add_reminder(7, 1, 10, "zu viel", 200, limit=3) is None
    assert store.add_reminder(8, 2, 20, "andere Person", 50, limit=3)
    assert [x["text"] for x in store.due_reminders(101)] == ["andere Person", "r0", "r1"]
    assert store.delete_reminder(ids[0], user_id=8) is False
    assert store.delete_reminder(ids[0], user_id=7) is True
    assert store.bump_reminder_attempts(ids[1]) == 1
    assert store.forget_reminders(7, guild_id=2) == 0
    assert store.forget_reminders(7, guild_id=1) == 2
    assert [x["user_id"] for x in store.list_reminders(8)] == [8]
    store.close()
