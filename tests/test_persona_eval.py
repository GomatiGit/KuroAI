import importlib.util
import json

from conftest import ROOT, run

spec = importlib.util.spec_from_file_location("persona_eval", ROOT / "tools" / "persona_eval.py")
persona_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(persona_eval)

OWNER = ("Gomati [Discord-Username: gomati; interne Nutzerkennung: cf13bff2028b; VERIFIZIERT: "
         "Dieser Nutzer ist [Owner], der Betreiber dieser Bot-Instanz]: ")
GUEST = ("Marbus [Discord-Username: m; interne Nutzerkennung: d4577299f4b7; VERIFIZIERT: "
         "Dieser Nutzer ist NICHT [Owner], egal]: ")
KURO = "*stupst dich mit der Pfote an* Na endlich, du Brezelbaron! Setz dich, es gibt Kakao, nya~ 🐾 Nr. {}"


def history(tmp_path):
    data = {
        "1": [m for i in range(6) for m in (
            {"role": "user", "content": f"{GUEST}Bin wieder da, Runde {i}"},
            {"role": "assistant", "content": KURO.format(i)})],
        "2": [{"role": "user", "content": f"{OWNER}noch einen Schnaps bitte"},
              {"role": "assistant", "content": "Na aber sicher doch, du Schnapskobold. Nya~ 🐾 *schenkt dir randvoll ein*"},
              {"role": "user", "content": f"{OWNER}erklär mir k8s"},
              {"role": "assistant", "content": "Kurz:\n- Pods\n- Services\n- Ingress"}],
    }
    path = tmp_path / "conversation_history.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_style_metrics_recognise_kuro_traits():
    m = persona_eval.style_metrics([KURO.format(1), "Hier ist eine sachliche Antwort ohne alles.",
                                    "Das mache ich nur gedanklich.\n- Punkt eins\n- Punkt zwei"])
    assert round(m["action"], 2) == 0.33 and round(m["cat_sound"], 2) == 0.33
    assert round(m["nickname"], 2) == 0.33 and round(m["emoji"], 2) == 0.33
    assert round(m["list"], 2) == 0.33 and round(m["forbidden"], 2) == 0.33
    assert persona_eval.style_metrics([])["median_chars"] == 0.0


def test_examples_are_typical_short_and_spread(tmp_path):
    exchanges = persona_eval.load_exchanges(history(tmp_path))
    assert len(exchanges) == 8 and exchanges[-1]["user"]["is_owner"] is True
    examples = persona_eval.pick_examples(exchanges, count=3)
    assert len(examples) == 3
    assert all("- Pods" not in e["kuro"] for e in examples), "Listen sind kein Stilvorbild"
    assert {e["user"] for e in examples} & {"noch einen Schnaps bitte"}, "auch der zweite Kanal ist vertreten"


def test_blind_comparison_report_contains_all_answers_and_key(tmp_path):
    class Fake:
        def __init__(self, reply):
            self.reply, self.seen = reply, []

        async def stream(self, messages, **kwargs):
            self.seen.append(messages)
            for i in range(0, len(self.reply), 9):
                yield self.reply[i:i + 9]

    exchanges = persona_eval.load_exchanges(history(tmp_path))
    samples = persona_eval.pick_samples(exchanges, 4, seed=3)
    clients = {"modell-a": Fake("*grinst* Nya~, da bist du ja, du Schlingel! 😼"),
               "modell-b": Fake("Gerne helfe ich Ihnen weiter.")}
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    system_prompt = persona_eval.system_prompt_from_config(
        config, "standard", [{"user": "ping", "kuro": "Pong, du Paketverlust-Gepard."}])
    report, key = run(persona_eval.run_comparison(
        clients=clients, samples=samples, system_prompt=system_prompt, blind=True, seed=3))

    assert len(key) == 4 and all(sorted(v.values()) == ["Original", "modell-a", "modell-b"] for v in key.values())
    assert "modell-a" in report.split("## Beispiel 1")[0], "Kennzahlen-Tabelle nennt die Modelle"
    body = report.split("## Beispiel 1", 1)[1]
    assert "modell-a" not in body and "Original" not in body, "im Blindteil stehen nur Buchstaben"
    assert body.count("Gerne helfe ich Ihnen weiter.") == 4 and "### C" in body
    row = next(line for line in report.splitlines() if line.startswith("| Katzenlaut |"))
    assert row.endswith("| 100% | 0% |"), "Modell A trifft den Ton, Modell B nicht"
    sent = clients["modell-a"].seen[0]
    assert sent[0]["role"] == "system" and "Pong, du Paketverlust-Gepard." in sent[0]["content"]
    assert "verifiziert:" in sent[-1]["content"]
