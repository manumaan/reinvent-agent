from reinvent_agent.catalog.venues import VENUE_ALIASES
from reinvent_agent.venue_maps import CAMPUS, VENUE_MAPS, map_url


def test_every_campus_venue_has_a_map_and_links_are_https():
    campus = {"Caesars Forum", "Caesars Palace", "MGM Grand", "Venetian", "Wynn"}
    assert campus <= set(VENUE_MAPS)
    assert campus <= set(VENUE_ALIASES.values())  # names match venue inference
    for _label, url in CAMPUS + [x for links in VENUE_MAPS.values() for x in links]:
        assert url.startswith("https://")
    assert map_url("Wynn").startswith("https://www.wynnlasvegas.com/")
    assert map_url(None) is None and map_url("Elsewhere") is None
