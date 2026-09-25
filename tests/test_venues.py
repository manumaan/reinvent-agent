from reinvent_agent.catalog.venues import VenueInferrer, room_tokens, venue_named_in
from reinvent_agent.events_api.models import Session


def s(room, venue=None, sid="x"):
    return Session(sessionId=sid, title="t", room=room, venue=venue)


def test_room_tokens():
    room = "Level 3 | Chairman's 363 | Content Hub | White Theater"
    assert room_tokens(room) == ["chairman's", "white theater"]
    assert room_tokens("Floor -4, Game Day, Room B4017") == ["game day"]


def test_venue_named_in_room():
    room = "Caesars Palace | Promenade South | Octavius 4 | Content Hub"
    assert venue_named_in(room) == "Caesars Palace"
    assert venue_named_in("Level 3 | Premier 318") is None


def test_learn_and_infer():
    labelled = [
        s("Level 3 | Chairman's 363 | Content Hub | Red Theater", "MGM Grand"),
        s("Level 3 | Chairman's 361", "MGM Grand"),
        s("Level 3 | Premier 318", "MGM Grand"),
        s("Level 3 | Premier 320", "MGM Grand"),
        s("Level 5 | Lido 3001", "Venetian"),
        s("Level 5 | Lido 3005", "Venetian"),
        # Theater names appear at several venues, so they must not become keys.
        s("Level 1 | Summit 1 | Content Hub | Red Theater", "Caesars Forum"),
        s("Level 1 | Summit 2", "Caesars Forum"),
    ]
    inf = VenueInferrer.learn(labelled)
    assert inf.token_venue["chairman's"] == "MGM Grand"
    assert "red theater" not in inf.token_venue
    assert inf.infer(s("Level 3 | Chairman's 363 | Content Hub | Blue Theater")) == (
        "MGM Grand",
        "room-learned",
    )
    assert inf.infer(s("Level 3 | Premier 312")) == ("MGM Grand", "room-learned")
    assert inf.infer(s("Level 2 | Unknown 1")) == (None, "none")
    assert inf.infer(s(None)) == (None, "none")
    assert inf.infer(s("x", "Venetian")) == ("Venetian", "api")
    assert inf.infer(s("Caesars Palace | Octavius 4")) == ("Caesars Palace", "room-name")


def test_level_number_handles_en_dash():
    assert Session(sessionId="a", title="t", level="300 – Advanced").level_number == 300
    assert Session(sessionId="a", title="t", level="500 - Distinguished").level_number == 500
    assert Session(sessionId="a", title="t").level_number is None
