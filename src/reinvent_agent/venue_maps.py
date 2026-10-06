"""Official maps for the re:Invent campus and each venue's conference centre.

Links checked on 2026-10-06 (all answered 200). Venue names match the catalog after
venue inference ("Wynn" covers Wynn and Encore). The first link per venue is the
primary one shown next to sessions; the rest are listed on the Venues tab.
"""

from __future__ import annotations

CAMPUS = [
    ("re:Invent 2026 venues and hotels (official)",
     "https://registration.awsevents.com/flow/awsevents/reinvent2026/venueshotel/page/venuehotel"),
    ("re:Invent FAQ: shuttles, getting around (official)",
     "https://aws.amazon.com/events/reinvent/faqs/"),
]  # fmt: skip

VENUE_MAPS: dict[str, list[tuple[str, str]]] = {
    "Caesars Forum": [
        ("Caesars Forum meetings: floor plans",
         "https://www.caesars.com/meetings/properties/caesars-forum"),
        ("Caesars meeting planner resources (Maps & Floor Plans → CAESARS FORUM)",
         "https://www.caesars.com/meetings/resources"),
    ],
    "Caesars Palace": [
        ("Caesars Palace meetings: floor plans",
         "https://www.caesars.com/meetings/properties/caesars-palace"),
        ("Caesars meeting planner resources (Maps & Floor Plans → Caesars Palace)",
         "https://www.caesars.com/meetings/resources"),
    ],
    "MGM Grand": [
        ("MGM Grand property map incl. Conference Center (PDF)",
         "https://assets.contentstack.io/v3/assets/bltc6ce635bc4868eb2/blt67c0bdb5f16b8094/mgm-grand-property-map.pdf"),
        ("MGM Grand Conference Center interactive floor plan (ExpoFP, third party)",
         "https://expofp.com/mgm-grand-conference-center"),
    ],
    "Venetian": [
        ("Venetian Expo map & floor plans",
         "https://www.venetianlasvegas.com/meetings/planning/spaces/expo-halls/expo-map-floor-plan.html"),
        ("Venetian meeting floor plans (PDF)",
         "https://www.venetianlasvegas.com/content/dam/vlv/meetings/downloadable-pdfs/M_FloorPlans.pdf"),
        ("Venetian facilities guide (PDF)",
         "https://www.venetianlasvegas.com/content/dam/vlv/meetings/downloadable-pdfs/facilities-guide.pdf"),
    ],
    "Wynn": [
        ("Wynn & Encore resort maps (meetings)",
         "https://www.wynnlasvegas.com/meetings/resort-maps"),
        ("Wynn & Encore property map (PDF)",
         "https://www.visitwynn.com/documents/PropertyMap.pdf"),
    ],
}  # fmt: skip


def map_url(venue: str | None) -> str | None:
    """The primary map for a venue (None when unknown)."""
    links = VENUE_MAPS.get(venue or "")
    return links[0][1] if links else None
