from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from sgr.connectors.base import APIConnector, APIRequestError
from sgr.models import NFLAthlete, NFLGame, NFLInjuryReport, NFLPlayerStatline, NFLSeasonType, NFLTeam


class EspnError(RuntimeError):
    """Base error for the read-only ESPN adapter."""


class EspnRequestError(EspnError):
    """Raised when ESPN cannot return a usable scoreboard response."""


class EspnSchemaError(EspnError):
    """Raised when an ESPN payload cannot be normalized safely."""


class PointInTimeDataUnavailableError(EspnError):
    """Raised when no cached snapshot predates a requested prediction time."""


class EspnConnector(APIConnector):
    """Cache-first, read-only adapter for ESPN's unofficial NFL scoreboard feed."""

    NORMALIZATION_VERSION = "espn-scoreboard-v1"
    NORMALIZATION_VERSION_SUMMARY = "espn-summary-v1"
    # ESPN fields that must never be read into any canonical record, per the
    # PRD's rule against provider win-probability/odds leakage. Not parsed
    # anywhere below; listed here so the boundary is explicit and greppable.
    EXCLUDED_SUMMARY_FIELDS = ("predictor", "odds", "pickcenter", "winprobability")
    ESPN_SEASON_TYPES = {
        NFLSeasonType.PRESEASON: 1,
        NFLSeasonType.REGULAR: 2,
        NFLSeasonType.POSTSEASON: 3,
    }
    CANONICAL_SEASON_TYPES = {value: key for key, value in ESPN_SEASON_TYPES.items()}

    def __init__(self, cache_dir: Path | str = ".cache/espn", timeout: float = 15.0) -> None:
        super().__init__(
            base_url="https://site.api.espn.com/apis/site/v2/sports/football/nfl",
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        self.cache_dir = Path(cache_dir)

    async def games_for_date(
        self,
        game_date: date,
        *,
        prediction_at: datetime | None = None,
        refresh: bool = False,
    ) -> list[NFLGame]:
        """Return normalized events while preserving point-in-time semantics.

        The normal path reuses the newest local snapshot. Point-in-time reads are
        cache-only, preventing a present-day response from contaminating a past
        prediction.
        """

        return await self._games_for_query(
            game_date,
            {"dates": game_date.strftime("%Y%m%d")},
            prediction_at=prediction_at,
            refresh=refresh,
        )

    async def games_for_week(
        self,
        season_year: int,
        week: int,
        *,
        season_type: NFLSeasonType = NFLSeasonType.REGULAR,
        prediction_at: datetime | None = None,
        refresh: bool = False,
    ) -> list[NFLGame]:
        """Return one NFL week without mixing season phases."""

        self._validate_season_request(season_year, week)
        season_type = NFLSeasonType(season_type)
        return await self._games_for_query(
            f"season-{season_year}/type-{self.ESPN_SEASON_TYPES[season_type]}/week-{week:02d}",
            {
                "dates": str(season_year),
                "seasontype": self.ESPN_SEASON_TYPES[season_type],
                "week": week,
                "limit": 100,
            },
            prediction_at=prediction_at,
            refresh=refresh,
        )

    async def games_for_season(
        self,
        season_year: int,
        *,
        season_type: NFLSeasonType = NFLSeasonType.REGULAR,
        prediction_at: datetime | None = None,
        refresh: bool = False,
    ) -> list[NFLGame]:
        """Return a single NFL season phase for historical dataset construction."""

        self._validate_season_request(season_year)
        season_type = NFLSeasonType(season_type)
        return await self._games_for_query(
            f"season-{season_year}/type-{self.ESPN_SEASON_TYPES[season_type]}",
            {
                "dates": str(season_year),
                "seasontype": self.ESPN_SEASON_TYPES[season_type],
                "limit": 1000,
            },
            prediction_at=prediction_at,
            refresh=refresh,
        )

    async def game_summary(
        self, event_id: str, *, refresh: bool = False
    ) -> tuple[list[NFLPlayerStatline], list[NFLInjuryReport]]:
        """Return one game's player boxscore and its injuries field as currently reported.

        Cache-first like the scoreboard methods, but intentionally has no
        prediction_at parameter: ESPN's injuries field reflects the
        *current* team injury report regardless of which event_id is
        queried (verified live against a 2023 game -- the entries came
        back dated the current day). A cached snapshot's own retrieved_at
        is therefore the only honest timestamp for injuries; there is no
        historical injury state to select among by requesting an old
        event_id. Point-in-time injury reconstruction can only come from
        a caller's own accumulated snapshot history going forward.
        """
        if not event_id or not event_id.strip():
            raise ValueError("event_id must be a non-empty string.")
        query_key = f"event-{event_id}"
        snapshot = None if refresh else self._latest_snapshot(
            query_key, namespace="nfl-summary", normalization_version=self.NORMALIZATION_VERSION_SUMMARY
        )
        if snapshot is None:
            snapshot = await self._fetch_and_store(
                query_key,
                {"event": event_id},
                endpoint="summary",
                namespace="nfl-summary",
                normalization_version=self.NORMALIZATION_VERSION_SUMMARY,
            )
        return self._normalize_summary_snapshot(event_id, snapshot)

    def _normalize_summary_snapshot(
        self, event_id: str, snapshot: dict[str, Any]
    ) -> tuple[list[NFLPlayerStatline], list[NFLInjuryReport]]:
        self._validate_snapshot_envelope(snapshot, normalization_version=self.NORMALIZATION_VERSION_SUMMARY)
        payload = snapshot["payload"]
        retrieved_at = self._as_utc(datetime.fromisoformat(snapshot["retrieved_at"]))
        statlines = self._normalize_boxscore(event_id, payload.get("boxscore"), snapshot, retrieved_at)
        injuries = self._normalize_injuries(event_id, payload.get("injuries"), snapshot, retrieved_at)
        return statlines, injuries

    def _normalize_boxscore(
        self, event_id: str, boxscore: Any, snapshot: dict[str, Any], retrieved_at: datetime
    ) -> list[NFLPlayerStatline]:
        if boxscore is None:
            return []  # game has not been played yet
        if not isinstance(boxscore, dict):
            raise EspnSchemaError(f"Event {event_id} boxscore must be an object.")
        players = boxscore.get("players")
        if players is None:
            return []
        if not isinstance(players, list):
            raise EspnSchemaError(f"Event {event_id} boxscore players must be a list.")

        statlines: list[NFLPlayerStatline] = []
        for team_block in players:
            if not isinstance(team_block, dict):
                raise EspnSchemaError(f"Event {event_id} boxscore team entry must be an object.")
            team = self._team_from_dict(team_block.get("team"), f"event {event_id} boxscore team")
            statistics = team_block.get("statistics")
            if not isinstance(statistics, list):
                raise EspnSchemaError(f"Event {event_id} boxscore team is missing statistics.")
            for category in statistics:
                if not isinstance(category, dict):
                    raise EspnSchemaError(f"Event {event_id} boxscore category must be an object.")
                category_name = self._required_text(category, "name", f"event {event_id} boxscore category")
                labels = category.get("labels")
                if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
                    raise EspnSchemaError(f"Event {event_id} boxscore category {category_name} has invalid labels.")
                athletes = category.get("athletes")
                if not isinstance(athletes, list):
                    raise EspnSchemaError(f"Event {event_id} boxscore category {category_name} is missing athletes.")
                for entry in athletes:
                    if not isinstance(entry, dict):
                        raise EspnSchemaError(f"Event {event_id} boxscore athlete entry must be an object.")
                    athlete = self._athlete(entry.get("athlete"), event_id)
                    stats = entry.get("stats")
                    if not isinstance(stats, list) or not all(isinstance(value, str) for value in stats):
                        raise EspnSchemaError(
                            f"Event {event_id} boxscore stats for {athlete.display_name} are invalid."
                        )
                    statlines.append(
                        NFLPlayerStatline(
                            event_id=event_id,
                            team=team,
                            athlete=athlete,
                            stat_category=category_name,
                            stat_labels=tuple(labels),
                            stat_values=tuple(stats),
                            retrieved_at=retrieved_at,
                            source_url=snapshot["source_url"],
                            raw_snapshot_path=snapshot["snapshot_path"],
                            raw_snapshot_sha256=snapshot["payload_sha256"],
                            normalization_version=snapshot["normalization_version"],
                        )
                    )
        return statlines

    def _normalize_injuries(
        self, event_id: str, injuries: Any, snapshot: dict[str, Any], retrieved_at: datetime
    ) -> list[NFLInjuryReport]:
        if injuries is None:
            return []
        if not isinstance(injuries, list):
            raise EspnSchemaError(f"Event {event_id} injuries must be a list.")

        reports: list[NFLInjuryReport] = []
        for team_block in injuries:
            if not isinstance(team_block, dict):
                raise EspnSchemaError(f"Event {event_id} injuries team entry must be an object.")
            team = self._team_from_dict(team_block.get("team"), f"event {event_id} injuries team")
            entries = team_block.get("injuries")
            if not isinstance(entries, list):
                raise EspnSchemaError(f"Event {event_id} injuries team is missing its injuries list.")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise EspnSchemaError(f"Event {event_id} injury entry must be an object.")
                athlete = self._athlete(entry.get("athlete"), event_id)
                status_text = self._required_text(
                    entry, "status", f"event {event_id} injury for {athlete.display_name}"
                )
                reported_at = self._parse_datetime(
                    self._required_text(entry, "date", f"event {event_id} injury for {athlete.display_name}")
                )
                reports.append(
                    NFLInjuryReport(
                        event_id=event_id,
                        team=team,
                        athlete=athlete,
                        status_text=status_text,
                        reported_at=reported_at,
                        retrieved_at=retrieved_at,
                        source_url=snapshot["source_url"],
                        raw_snapshot_path=snapshot["snapshot_path"],
                        raw_snapshot_sha256=snapshot["payload_sha256"],
                        normalization_version=snapshot["normalization_version"],
                    )
                )
        return reports

    async def _games_for_query(
        self,
        query_key: date | str,
        params: dict[str, str | int],
        *,
        prediction_at: datetime | None,
        refresh: bool,
    ) -> list[NFLGame]:
        if prediction_at is not None:
            if refresh:
                raise ValueError("Point-in-time reads are cache-only and cannot refresh ESPN data.")
            snapshot = self._latest_snapshot_before(query_key, prediction_at)
            if snapshot is None:
                raise PointInTimeDataUnavailableError(
                    "No cached ESPN scoreboard snapshot exists on or before "
                    f"{self._as_utc(prediction_at).isoformat()} for {self._cache_key(query_key)}."
                )
        else:
            snapshot = None if refresh else self._latest_snapshot(query_key)
            if snapshot is None:
                snapshot = await self._fetch_and_store(query_key, params)

        return self._normalize_snapshot(snapshot)

    async def _fetch_and_store(
        self,
        query_key: date | str,
        params: dict[str, str | int],
        *,
        endpoint: str = "scoreboard",
        namespace: str = "nfl-scoreboard",
        normalization_version: str | None = None,
    ) -> dict[str, Any]:
        source_url = self._source_url(params, endpoint=endpoint)
        try:
            payload = await self.get_json(endpoint, params=params)
        except APIRequestError as error:
            raise EspnRequestError(f"ESPN {endpoint} request failed: {error}") from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code if error.response is not None else "unknown"
            raise EspnRequestError(f"ESPN {endpoint} request failed with HTTP status {status}.") from error
        except httpx.HTTPError as error:
            raise EspnRequestError(f"ESPN {endpoint} request failed; retry after checking connectivity.") from error
        except (TypeError, ValueError) as error:
            raise EspnRequestError(f"ESPN {endpoint} response was not valid JSON.") from error

        if not isinstance(payload, dict):
            raise EspnSchemaError(f"ESPN {endpoint} response must be a JSON object.")
        return self._write_snapshot(
            query_key, payload, source_url, datetime.now(timezone.utc),
            namespace=namespace, normalization_version=normalization_version,
        )

    def _write_snapshot(
        self,
        query_key: date | str,
        payload: dict[str, Any],
        source_url: str,
        retrieved_at: datetime,
        *,
        namespace: str = "nfl-scoreboard",
        normalization_version: str | None = None,
    ) -> dict[str, Any]:
        """Persist raw response data and the metadata required to reproduce it."""

        retrieved_at = self._as_utc(retrieved_at)
        raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        snapshot = {
            "normalization_version": normalization_version or self.NORMALIZATION_VERSION,
            "source_url": source_url,
            "retrieved_at": retrieved_at.isoformat(),
            "payload_sha256": hashlib.sha256(raw_payload.encode()).hexdigest(),
            "payload": payload,
        }
        directory = self.cache_dir / namespace / self._cache_key(query_key)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = retrieved_at.strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{timestamp}.json"
        encoded_snapshot = json.dumps(snapshot, sort_keys=True, indent=2)
        try:
            with path.open("x", encoding="utf-8") as snapshot_file:
                snapshot_file.write(encoded_snapshot)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != encoded_snapshot:
                raise EspnSchemaError(
                    f"Refusing to overwrite immutable ESPN snapshot: {path}."
                ) from None
        snapshot["snapshot_path"] = str(path)
        return snapshot

    def _latest_snapshot(
        self, query_key: date | str, *, namespace: str = "nfl-scoreboard", normalization_version: str | None = None
    ) -> dict[str, Any] | None:
        snapshots = self._load_snapshots(query_key, namespace=namespace, normalization_version=normalization_version)
        return snapshots[-1] if snapshots else None

    def _latest_snapshot_before(
        self,
        query_key: date | str,
        prediction_at: datetime,
        *,
        namespace: str = "nfl-scoreboard",
        normalization_version: str | None = None,
    ) -> dict[str, Any] | None:
        prediction_at = self._as_utc(prediction_at)
        candidates = [
            snapshot
            for snapshot in self._load_snapshots(query_key, namespace=namespace, normalization_version=normalization_version)
            if self._as_utc(datetime.fromisoformat(snapshot["retrieved_at"])) <= prediction_at
        ]
        return candidates[-1] if candidates else None

    def _load_snapshots(
        self, query_key: date | str, *, namespace: str = "nfl-scoreboard", normalization_version: str | None = None
    ) -> list[dict[str, Any]]:
        directory = self.cache_dir / namespace / self._cache_key(query_key)
        snapshots: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            try:
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(snapshot, dict):
                    raise ValueError("snapshot is not an object")
                self._validate_snapshot_envelope(snapshot, normalization_version=normalization_version)
            except (OSError, json.JSONDecodeError, ValueError, KeyError) as error:
                raise EspnSchemaError(f"Cached ESPN snapshot is invalid: {path}.") from error
            snapshot["snapshot_path"] = str(path)
            snapshots.append(snapshot)
        return snapshots

    def _normalize_snapshot(self, snapshot: dict[str, Any]) -> list[NFLGame]:
        self._validate_snapshot_envelope(snapshot)
        events = snapshot["payload"].get("events")
        if not isinstance(events, list):
            raise EspnSchemaError("ESPN scoreboard payload is missing its events list.")
        retrieved_at = self._as_utc(datetime.fromisoformat(snapshot["retrieved_at"]))
        return [self._normalize_event(event, snapshot, retrieved_at) for event in events]

    def _normalize_event(
        self, event: Any, snapshot: dict[str, Any], retrieved_at: datetime
    ) -> NFLGame:
        if not isinstance(event, dict):
            raise EspnSchemaError("An ESPN scoreboard event must be an object.")
        event_id = self._required_text(event, "id", "event")
        kickoff = self._parse_datetime(self._required_text(event, "date", f"event {event_id}"))
        competitions = event.get("competitions")
        if not isinstance(competitions, list) or len(competitions) != 1:
            raise EspnSchemaError(f"Event {event_id} must contain exactly one competition.")
        competition = competitions[0]
        if not isinstance(competition, dict):
            raise EspnSchemaError(f"Event {event_id} competition must be an object.")
        season = event.get("season")
        if not isinstance(season, dict):
            raise EspnSchemaError(f"Event {event_id} is missing season metadata.")
        season_year = self._required_int(season, "year", f"event {event_id} season")
        season_type_id = self._required_int(season, "type", f"event {event_id} season")
        try:
            season_type = self.CANONICAL_SEASON_TYPES[season_type_id]
        except KeyError as error:
            raise EspnSchemaError(
                f"Event {event_id} has unsupported ESPN season type {season_type_id}."
            ) from error
        week = event.get("week")
        if not isinstance(week, dict):
            raise EspnSchemaError(f"Event {event_id} is missing week metadata.")
        week_number = self._required_int(week, "number", f"event {event_id} week")
        neutral_site = competition.get("neutralSite")
        if neutral_site is not None and not isinstance(neutral_site, bool):
            raise EspnSchemaError(f"Event {event_id} neutral-site flag must be a boolean.")
        competitors = competition.get("competitors")
        if not isinstance(competitors, list):
            raise EspnSchemaError(f"Event {event_id} is missing competitors.")

        by_side: dict[str, dict[str, Any]] = {}
        for competitor in competitors:
            if isinstance(competitor, dict) and competitor.get("homeAway") in {"home", "away"}:
                side = competitor["homeAway"]
                if side in by_side:
                    raise EspnSchemaError(f"Event {event_id} contains multiple {side} competitors.")
                by_side[side] = competitor
        if set(by_side) != {"home", "away"}:
            raise EspnSchemaError(f"Event {event_id} must have one home and one away competitor.")

        status = competition.get("status")
        if not isinstance(status, dict) or not isinstance(status.get("type"), dict):
            raise EspnSchemaError(f"Event {event_id} is missing status metadata.")
        status_type = status["type"]
        status_name = self._required_text(status_type, "name", f"event {event_id} status")
        completed = status_type.get("completed")
        if not isinstance(completed, bool):
            raise EspnSchemaError(f"Event {event_id} completed status must be a boolean.")
        home_score = self._score(by_side["home"], event_id)
        away_score = self._score(by_side["away"], event_id)
        if completed and (home_score is None or away_score is None):
            raise EspnSchemaError(f"Completed event {event_id} must include both scores.")

        return NFLGame(
            event_id=event_id,
            season_year=season_year,
            season_type=season_type,
            week=week_number,
            kickoff=kickoff,
            status=status_name,
            completed=completed,
            neutral_site=neutral_site,
            home_team=self._team(by_side["home"], event_id),
            away_team=self._team(by_side["away"], event_id),
            home_score=home_score,
            away_score=away_score,
            retrieved_at=retrieved_at,
            source_url=snapshot["source_url"],
            raw_snapshot_path=snapshot["snapshot_path"],
            raw_snapshot_sha256=snapshot["payload_sha256"],
            normalization_version=snapshot["normalization_version"],
        )

    @staticmethod
    def _validate_snapshot_envelope(snapshot: dict[str, Any], *, normalization_version: str | None = None) -> None:
        for field in (
            "normalization_version",
            "source_url",
            "retrieved_at",
            "payload_sha256",
            "payload",
        ):
            if field not in snapshot:
                raise ValueError(f"missing {field}")
        if snapshot["normalization_version"] != (normalization_version or EspnConnector.NORMALIZATION_VERSION):
            raise ValueError("normalization version is unsupported")
        if not isinstance(snapshot["source_url"], str):
            raise ValueError("source URL is invalid")
        if not isinstance(snapshot["retrieved_at"], str):
            raise ValueError("retrieval timestamp is invalid")
        EspnConnector._as_utc(datetime.fromisoformat(snapshot["retrieved_at"]))
        if not isinstance(snapshot["payload_sha256"], str):
            raise ValueError("payload checksum is invalid")
        if not isinstance(snapshot["payload"], dict):
            raise ValueError("payload is not an object")
        raw_payload = json.dumps(snapshot["payload"], sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(raw_payload.encode()).hexdigest() != snapshot["payload_sha256"]:
            raise ValueError("payload checksum does not match")

    @staticmethod
    def _team(competitor: dict[str, Any], event_id: str) -> NFLTeam:
        return EspnConnector._team_from_dict(competitor.get("team"), f"event {event_id} team")

    @staticmethod
    def _team_from_dict(team_info: Any, context: str) -> NFLTeam:
        if not isinstance(team_info, dict):
            raise EspnSchemaError(f"{context} is missing team metadata.")
        return NFLTeam(
            espn_team_id=EspnConnector._required_text(team_info, "id", context),
            abbreviation=EspnConnector._required_text(team_info, "abbreviation", context),
            name=EspnConnector._required_text(team_info, "displayName", context),
        )

    @staticmethod
    def _athlete(athlete_info: Any, event_id: str) -> NFLAthlete:
        if not isinstance(athlete_info, dict):
            raise EspnSchemaError(f"Event {event_id} entry is missing athlete metadata.")
        position = athlete_info.get("position")
        position_name = None
        if isinstance(position, dict):
            raw_position = position.get("abbreviation") or position.get("displayName")
            if isinstance(raw_position, str) and raw_position.strip():
                position_name = raw_position
        return NFLAthlete(
            espn_athlete_id=EspnConnector._required_text(athlete_info, "id", f"event {event_id} athlete"),
            display_name=EspnConnector._required_text(athlete_info, "displayName", f"event {event_id} athlete"),
            position=position_name,
        )

    @staticmethod
    def _score(competitor: dict[str, Any], event_id: str) -> int | None:
        score = competitor.get("score")
        if score in (None, ""):
            return None
        if isinstance(score, bool):
            raise EspnSchemaError(f"Event {event_id} has a non-integer score.")
        try:
            numeric_score = int(score)
        except (TypeError, ValueError) as error:
            raise EspnSchemaError(f"Event {event_id} has a non-integer score.") from error
        if numeric_score < 0:
            raise EspnSchemaError(f"Event {event_id} has a negative score.")
        return numeric_score

    @staticmethod
    def _required_text(value: dict[str, Any], field: str, context: str) -> str:
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise EspnSchemaError(f"ESPN {context} is missing {field}.")
        return raw

    @staticmethod
    def _required_int(value: dict[str, Any], field: str, context: str) -> int:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise EspnSchemaError(f"ESPN {context} is missing a positive integer {field}.")
        return raw

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        try:
            return EspnConnector._as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as error:
            raise EspnSchemaError("ESPN event kickoff time is invalid.") from error

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise EspnSchemaError("ESPN snapshot timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _cache_key(query_key: date | str) -> str:
        return query_key.isoformat() if isinstance(query_key, date) else query_key

    @staticmethod
    def _validate_season_request(season_year: int, week: int | None = None) -> None:
        if isinstance(season_year, bool) or not isinstance(season_year, int) or season_year < 2000:
            raise ValueError("season_year must be an integer greater than or equal to 2000.")
        if week is not None and (
            isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 25
        ):
            raise ValueError("week must be an integer between 1 and 25.")

    def _source_url(self, params: dict[str, str | int], *, endpoint: str = "scoreboard") -> str:
        return f"{self.base_url}/{endpoint}?{httpx.QueryParams(params)}"
