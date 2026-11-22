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


def test_repeated_actions_in_one_reply_are_dropped():
    from kuro.formatting import drop_repeated_actions

    reply = ("*Ohren spitze sich auf und schnurre* Nya~ mein Goblin-Charmeur! *Strecke mich und reibe mich an dir*\n"
             "Zarya die Heldin? *Ohren spitze sich auf und schnurre* Das ist ja aufregend! "
             "*Schwinge meinen Schwanz aufgeregt*\n"
             "Erzähl mir mehr! *Ohren spitze sich auf* Bin schon ganz gespannt, du magische Goblin-Dame! 😼💖")
    out = drop_repeated_actions(reply)
    assert out.count("Ohren spitze") == 1
    assert "*Strecke mich und reibe mich an dir*" in out and "*Schwinge meinen Schwanz aufgeregt*" in out
    assert "Zarya die Heldin? Das ist ja aufregend!" in out
    assert "Erzähl mir mehr! Bin schon ganz gespannt" in out
    assert drop_repeated_actions("*grinst* Hallo. *grinst breit und kichert* Na?") == \
        "*grinst* Hallo. *grinst breit und kichert* Na?", "neue Details bleiben erhalten"


def test_actions_from_previous_replies_are_dropped_too():
    from kuro.formatting import drop_repeated_actions

    reply = ("*Ohren spitze sich auf und mein Schwanz zucke vor Aufregung* Na klar, mein Koch! "
             "*Strecke mich und reibe mich an dir* Was steht heute auf dem Speiseplan? "
             "*holt eine Pfanne vom Haken* Soll ich dir etwas zaubern?")
    out = drop_repeated_actions(reply, previous=["Ohren spitze sich auf und schnurre", "Strecke mich und reibe mich an dir"])
    assert "Ohren" not in out and "reibe" not in out
    assert "*holt eine Pfanne vom Haken*" in out and out.startswith("Na klar, mein Koch!")
