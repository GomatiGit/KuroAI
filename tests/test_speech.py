from kuro.speech import (
    SpeechStream, detect_emotion, split_script, spoken_form, to_speech_segments,
)


def texts(segments):
    return [s.text for s in segments]


def test_action_is_not_spoken_but_sets_tone():
    segs = to_speech_segments(
        "*schmiegt sich an deine Seite, Schnurren setzt ein*\nIss ein Stück Kuchen für mich mit, Meister."
    )
    assert texts(segs) == ["Iss ein Stück Kuchen für mich mit, Meister."]
    assert segs[0].emotion == "zärtlich"


def test_emphasis_inside_sentence_is_spoken():
    segs = to_speech_segments("Vielleicht ein *kleeeines* bisschen zu viel, du Schlingel.")
    assert texts(segs) == ["Vielleicht ein kleeeines bisschen zu viel, du Schlingel."]


def test_sound_words_mid_sentence_stay():
    parts = split_script("Warte, ich zauber dir was her – *Prestidigitation* und *plopp* – fertig.")
    assert [kind for kind, _ in parts] == ["say"]


def test_cat_sounds_and_shouting_are_pronounceable():
    segs = to_speech_segments("NYAAAA, LACHS?! 🐟💛 Das ist ja toll, nya~")
    assert segs[0].text.startswith("Njaaa, Lachs?!")
    assert segs[0].emotion == "aufgeregt"
    assert "~" not in " ".join(texts(segs)) and "njaa" in " ".join(texts(segs)).lower()


def test_acronyms_survive_shouting_rules():
    assert "MRT" in spoken_form("Mein MRT war ok.")
    assert spoken_form("ESS DU SELBST DIE BUGS!") == "Ess Du Selbst Die Bugs!"


def test_markup_links_mentions_and_emoji_removed():
    segs = to_speech_segments(
        "Hallo <@668478977811415063>! **Wichtig:** schau auf https://example.org/x `code` :3 😼\n"
        "- erster Punkt\n- zweiter Punkt"
    )
    joined = " ".join(texts(segs))
    for junk in ("<@", "http", "**", "`", ":3", "😼", "- "):
        assert junk not in joined
    assert "erster Punkt" in joined and "zweiter Punkt" in joined


def test_code_blocks_are_dropped():
    segs = to_speech_segments("Hier:\n```yaml\nfoo: bar\n```\nFertig, miau.")
    assert "foo" not in " ".join(texts(segs))


def test_emoji_at_sentence_end_colours_that_sentence():
    segs = to_speech_segments("Na gut, komm her.\n\nKein. Wort. 😾")
    assert segs[-1].text == "Kein. Wort."
    assert segs[-1].emotion == "empört"


def test_ellipsis_does_not_break_a_sentence():
    segs = to_speech_segments("Sehr nah dran… vielleicht ein bisschen zu viel Fokus.")
    assert len(segs) == 1


def test_detect_emotion_priorities():
    assert detect_emotion("verschränkt die Pfoten und schnaubt") == "schmollend"
    assert detect_emotion("kichert und grinst") == "neckisch"
    assert detect_emotion("steht am Tresen") is None


def test_stream_matches_full_conversion_and_waits_for_open_action():
    text = (
        "*Ohren zucken, dann schmelze ich einfach weg.*\n\nH-hey… so intensiv gleich… NYAAA… 💛\n"
        "*Ich drücke mich enger an dich.*\n\nDu riechst nach „mein“ – das reicht mir."
    )
    stream = SpeechStream()
    streamed = []
    for i in range(0, len(text), 4):
        streamed += stream.feed(text[i:i + 4])
        # nie mitten aus einer Handlung heraus sprechen
        assert all("*" not in s.text and "drücke" not in s.text for s in streamed)
    streamed += stream.flush()
    assert " ".join(texts(streamed)) == " ".join(texts(to_speech_segments(text)))
    assert streamed, "es muss etwas zu sprechen geben"


def test_stream_emits_first_sentence_before_reply_is_complete():
    stream = SpeechStream()
    out = stream.feed("Na also, geht doch, du Schlingel. Und jetzt setz dich hin und ")
    assert texts(out) == ["Na also, geht doch, du Schlingel."]


def test_instruction_combines_base_voice_and_emotion():
    segs = to_speech_segments("Hmpf! Das war meine Brezel.", base_instruction="Junge Frauenstimme.")
    assert segs[0].emotion == "schmollend"
    assert segs[0].instruction.startswith("Junge Frauenstimme.")
    assert "Schmollend" in segs[0].instruction


def test_custom_pronunciation_is_applied():
    segs = to_speech_segments("Hmpf, na gut.", pronunciation={r"\bhmpf\b": "Hmmpf"})
    assert segs[0].text == "Hmmpf, na gut."
