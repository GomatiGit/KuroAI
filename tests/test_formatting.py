from kuro.formatting import add_paragraphs

LONG = ("*Ohren spitzen sich, Schwanz wedelt neugierig* Na sieh mal einer an, der Meister ist zurück! "
        "Ich hab schon gedacht, du hast dich im Keller zwischen den Fässern verlaufen, du Tavernen-Trottel. "
        "*stellt dir einen dampfenden Becher Kakao hin* Und jetzt erzähl, was hast du mir mitgebracht? Hmpf. 🐾")


def test_actions_get_their_own_paragraphs():
    out = add_paragraphs(LONG).split("\n\n")
    assert out[0] == "*Ohren spitzen sich, Schwanz wedelt neugierig*"
    assert out[2] == "*stellt dir einen dampfenden Becher Kakao hin*"
    assert out[-1].endswith("Hmpf. 🐾") and len(out) == 4


def test_emphasis_inside_a_sentence_stays_put():
    text = ("Vielleicht ein *kleeeines* bisschen zu viel Fokus auf gewisse Kurven, du Schlingel, und das sage ich "
            "nur einmal, weil ich heute gute Laune habe und du mir einen Kakao versprochen hast, nya~")
    assert add_paragraphs(text) == text


def test_long_speech_is_split_by_sentences():
    speech = " ".join(f"Das ist Satz Nummer {i} mit ein bisschen Inhalt, damit es lang wird." for i in range(12))
    paragraphs = add_paragraphs(speech).split("\n\n")
    assert len(paragraphs) >= 3 and all(len(p) <= 340 for p in paragraphs)
    assert " ".join(paragraphs) == speech


def test_short_or_already_formatted_text_is_untouched():
    assert add_paragraphs("*grinst* Nya~") == "*grinst* Nya~"
    formatted = LONG.replace("! ", "!\n\n", 1)
    assert add_paragraphs(formatted) == formatted
    code = LONG + " ```yaml\nfoo: bar\n```"
    assert add_paragraphs(code) == code
