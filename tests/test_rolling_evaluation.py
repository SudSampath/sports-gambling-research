from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sgr.models import NFLSeasonType
from sgr.research.rolling_evaluation import (
    available_completed_seasons,
    training_seasons_for_fold,
)
from sgr.research.storage import ResearchStore

from _game_factory import make_game


def test_available_completed_seasons_ignores_incomplete_and_non_regular_games(tmp_path):
    store = ResearchStore(root=tmp_path / "store")
    store.write(
        [
            make_game(
                event_id="reg-2020",
                season_year=2020,
                week=1,
                home_abbr="AAA",
                away_abbr="BBB",
                kickoff_at=datetime(2020, 9, 10, tzinfo=timezone.utc),
                home_score=20,
                away_score=10,
                completed=True,
            ),
            make_game(
                event_id="scheduled-2021",
                season_year=2021,
                week=1,
                home_abbr="AAA",
                away_abbr="BBB",
                kickoff_at=datetime(2021, 9, 10, tzinfo=timezone.utc),
                home_score=None,
                away_score=None,
                completed=False,
            ),
            make_game(
                event_id="preseason-2020",
                season_year=2020,
                week=1,
                home_abbr="AAA",
                away_abbr="BBB",
                kickoff_at=datetime(2020, 8, 10, tzinfo=timezone.utc),
                home_score=20,
                away_score=10,
                completed=True,
                season_type=NFLSeasonType.PRESEASON,
            ),
        ]
    )

    assert available_completed_seasons(store) == (2020,)


def test_training_seasons_for_fold_rejects_unknown_window():
    with pytest.raises(ValueError, match="Unknown training window"):
        training_seasons_for_fold(2020, (2018, 2019), window="bogus")  # type: ignore[arg-type]


def test_training_seasons_for_fold_rejects_non_positive_rolling_window():
    with pytest.raises(ValueError, match="must be positive"):
        training_seasons_for_fold(2020, (2018, 2019), window="rolling", rolling_window_seasons=0)


def test_training_seasons_for_fold_rolling_window_shorter_than_history():
    result = training_seasons_for_fold(
        2020, (2010, 2011, 2012, 2013, 2014), window="rolling", rolling_window_seasons=8
    )
    assert result == (2010, 2011, 2012, 2013, 2014)
