from __future__ import annotations

# Real NFL division alignment (stable since the 2002 realignment) -- fixed
# real-world structure, not something this project models or fits.
# {abbreviation: (conference, division)}.
TEAM_DIVISIONS: dict[str, tuple[str, str]] = {
    "BUF": ("AFC", "East"), "MIA": ("AFC", "East"), "NE": ("AFC", "East"), "NYJ": ("AFC", "East"),
    "BAL": ("AFC", "North"), "CIN": ("AFC", "North"), "CLE": ("AFC", "North"), "PIT": ("AFC", "North"),
    "HOU": ("AFC", "South"), "IND": ("AFC", "South"), "JAX": ("AFC", "South"), "TEN": ("AFC", "South"),
    "DEN": ("AFC", "West"), "KC": ("AFC", "West"), "LAC": ("AFC", "West"), "LV": ("AFC", "West"),
    "DAL": ("NFC", "East"), "NYG": ("NFC", "East"), "PHI": ("NFC", "East"), "WSH": ("NFC", "East"),
    "CHI": ("NFC", "North"), "DET": ("NFC", "North"), "GB": ("NFC", "North"), "MIN": ("NFC", "North"),
    "ATL": ("NFC", "South"), "CAR": ("NFC", "South"), "NO": ("NFC", "South"), "TB": ("NFC", "South"),
    "ARI": ("NFC", "West"), "LAR": ("NFC", "West"), "SF": ("NFC", "West"), "SEA": ("NFC", "West"),
}

CONFERENCES = ("AFC", "NFC")
DIVISIONS_PER_CONFERENCE = ("East", "North", "South", "West")
DIVISION_WINNERS_PER_CONFERENCE = 4
WILDCARDS_PER_CONFERENCE = 3


class UnknownTeamAbbreviationError(ValueError):
    """Raised when a team abbreviation has no entry in TEAM_DIVISIONS."""


def division_of(abbreviation: str) -> tuple[str, str]:
    entry = TEAM_DIVISIONS.get(abbreviation)
    if entry is None:
        raise UnknownTeamAbbreviationError(f"No known NFL division for team abbreviation {abbreviation!r}.")
    return entry
