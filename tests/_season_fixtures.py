"""Synthetic ESPN scoreboard payload builders for offline season-level tests.

A real 272-game season is impractical to hand-maintain as static per-game
JSON fixtures (SUD-23's fixtures cover single-game edge cases instead), so
season-shaped tests build deterministic synthetic payloads that match
ESPN's observed schema instead of hitting the network or a giant checked-in
file.
"""

from __future__ import annotations

TEAM_IDS = [str(100 + i) for i in range(32)]
TEAM_ABBR = [f"T{i:02d}" for i in range(32)]


def _competitor(home_away: str, team_index: int, score: str | None) -> dict:
    return {
        "homeAway": home_away,
        "score": score,
        "team": {
            "id": TEAM_IDS[team_index],
            "abbreviation": TEAM_ABBR[team_index],
            "displayName": f"Team {team_index}",
        },
    }


def _event(event_id: str, season_year: int, week: int, home_idx: int, away_idx: int, *, completed: bool) -> dict:
    status_name = "STATUS_FINAL" if completed else "STATUS_SCHEDULED"
    home_score = "24" if completed else None
    away_score = "17" if completed else None
    return {
        "id": event_id,
        "date": f"{season_year}-09-08T18:00Z",
        "season": {"year": season_year, "type": 2, "slug": "regular-season"},
        "week": {"number": week},
        "competitions": [
            {
                "neutralSite": False,
                "status": {"type": {"name": status_name, "completed": completed}},
                "competitors": [
                    _competitor("home", home_idx, home_score),
                    _competitor("away", away_idx, away_score),
                ],
            }
        ],
    }


def full_season_weeks(
    season_year: int,
    *,
    completed: bool = True,
    full_weeks: int = 16,
    bye_weeks: int = 2,
) -> dict[int, dict]:
    """Return {week_number: scoreboard_payload} covering exactly 272 games / 32 teams.

    16 full weeks (16 games, all 32 teams) + 2 bye weeks (8 games, teams
    0-15) = 272 games, matching the live-verified 2023/2024/2025 totals.
    """
    payloads: dict[int, dict] = {}
    week = 1
    for _ in range(full_weeks):
        events = [
            _event(f"{season_year}{week:02d}{g:02d}", season_year, week, 2 * g, 2 * g + 1, completed=completed)
            for g in range(16)
        ]
        payloads[week] = {"events": events}
        week += 1
    for _ in range(bye_weeks):
        events = [
            _event(f"{season_year}{week:02d}{g:02d}", season_year, week, 2 * g, 2 * g + 1, completed=completed)
            for g in range(8)
        ]
        payloads[week] = {"events": events}
        week += 1
    while week <= 18:
        payloads[week] = {"events": []}
        week += 1
    return payloads


def install_week_payloads(espn_context: dict, payloads: dict[int, dict]) -> None:
    """Monkeypatch connector.get_json to serve per-week payloads by the 'week' param."""

    async def fake_get_json(path: str, params: dict | None = None):
        week = params["week"]
        espn_context.setdefault("requests", []).append({"path": path, "params": params})
        return payloads.get(week, {"events": []})

    espn_context["monkeypatch"].setattr(espn_context["connector"], "get_json", fake_get_json)
