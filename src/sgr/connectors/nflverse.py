from __future__ import annotations

import csv
import hashlib
import io
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from sgr.research.schemas import RawSnapshotRef


class NflverseError(RuntimeError):
    """Base error for the read-only nflverse adapter."""


class NflverseRequestError(NflverseError):
    """Raised when an nflverse release asset cannot be downloaded."""


class NflverseSchemaError(NflverseError):
    """Raised when an nflverse CSV no longer matches the required contract."""


@dataclass(frozen=True)
class NflverseCsvSnapshot:
    rows: tuple[dict[str, str], ...]
    source: RawSnapshotRef


class NflverseConnector:
    """Cache-first adapter for public nflverse roster and snap-count assets."""

    BASE_URL = "https://github.com/nflverse/nflverse-data/releases/download"
    REQUIRED_COLUMNS = {
        "snap_counts": {
            "season",
            "game_type",
            "player",
            "pfr_player_id",
            "position",
            "team",
            "offense_snaps",
            "defense_snaps",
        },
        "weekly_rosters": {"season", "week", "game_type", "team", "status", "pfr_id"},
        "rosters": {"season", "team", "status", "pfr_id"},
        "games": {
            "game_id",
            "season",
            "game_type",
            "week",
            "home_team",
            "away_team",
            "home_score",
            "away_score",
            "spread_line",
            "total_line",
            "home_moneyline",
            "away_moneyline",
            "espn",
        },
    }

    def __init__(
        self,
        cache_dir: Path | str = ".cache/nflverse",
        timeout: float = 60.0,
        verify: ssl.SSLContext | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.verify = verify if verify is not None else ssl.create_default_context()

    async def snap_counts(self, season_year: int, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return await self._dataset("snap_counts", season_year, refresh=refresh)

    async def weekly_rosters(self, season_year: int, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return await self._dataset("weekly_rosters", season_year, refresh=refresh)

    async def current_rosters(self, season_year: int, *, refresh: bool = False) -> NflverseCsvSnapshot:
        return await self._dataset("rosters", season_year, refresh=refresh)

    async def games(self, *, refresh: bool = False) -> NflverseCsvSnapshot:
        """Fetch the single whole-history nflverse schedules/games release asset.

        Unlike snap_counts/weekly_rosters/rosters, games.csv is published as
        one file covering every season (1999-present), not one file per
        season, so it is cached under its own dataset directory with no
        season subdirectory.
        """
        return await self._fetch_csv(
            "games", self.cache_dir / "games", f"{self.BASE_URL}/schedules/games.csv", refresh=refresh
        )

    async def _dataset(self, dataset: str, season_year: int, *, refresh: bool) -> NflverseCsvSnapshot:
        if isinstance(season_year, bool) or not isinstance(season_year, int) or season_year < 2000:
            raise ValueError("season_year must be an integer greater than or equal to 2000.")
        source_url = self._source_url(dataset, season_year)
        directory = self.cache_dir / dataset / str(season_year)
        return await self._fetch_csv(dataset, directory, source_url, refresh=refresh)

    async def _fetch_csv(
        self, dataset: str, directory: Path, source_url: str, *, refresh: bool = False
    ) -> NflverseCsvSnapshot:
        cached = sorted(directory.glob("*.csv")) if directory.exists() else []
        if cached and not refresh:
            path = cached[-1]
            raw = path.read_bytes()
            try:
                timestamp, expected_checksum = path.stem.split("-", maxsplit=1)
                retrieved_at = datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError as error:
                raise NflverseSchemaError(
                    f"Cached nflverse snapshot name is invalid: {path}."
                ) from error
            if hashlib.sha256(raw).hexdigest() != expected_checksum:
                raise NflverseSchemaError(
                    f"Cached nflverse snapshot checksum does not match: {path}."
                )
        else:
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    verify=self.verify,
                ) as client:
                    response = await client.get(source_url, headers={"Accept": "text/csv"})
                    response.raise_for_status()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code if error.response is not None else "unknown"
                raise NflverseRequestError(
                    f"nflverse {dataset} download failed with HTTP status {status}."
                ) from error
            except httpx.HTTPError as error:
                raise NflverseRequestError(
                    f"nflverse {dataset} download failed; retry after checking connectivity."
                ) from error
            raw = response.content
            retrieved_at = datetime.now(timezone.utc)
            checksum = hashlib.sha256(raw).hexdigest()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (
                f"{retrieved_at.strftime('%Y%m%dT%H%M%S%fZ')}-{checksum}.csv"
            )
            try:
                with path.open("xb") as output:
                    output.write(raw)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise NflverseSchemaError(f"Refusing to overwrite immutable nflverse snapshot: {path}.")

        try:
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            fields = set(reader.fieldnames or ())
            missing = self.REQUIRED_COLUMNS[dataset] - fields
            if missing:
                raise NflverseSchemaError(
                    f"nflverse {dataset} CSV is missing required columns: {sorted(missing)}."
                )
            rows = tuple(dict(row) for row in reader)
        except UnicodeDecodeError as error:
            raise NflverseSchemaError(f"nflverse {dataset} response is not UTF-8 CSV.") from error
        if not rows:
            raise NflverseSchemaError(f"nflverse {dataset} CSV contains no rows.")

        checksum = hashlib.sha256(raw).hexdigest()
        return NflverseCsvSnapshot(
            rows=rows,
            source=RawSnapshotRef(
                provider="nflverse",
                path=path.as_posix(),
                source_url=source_url,
                retrieved_at=retrieved_at,
                sha256=checksum,
            ),
        )

    @classmethod
    def _source_url(cls, dataset: str, season_year: int) -> str:
        if dataset == "snap_counts":
            return f"{cls.BASE_URL}/snap_counts/snap_counts_{season_year}.csv"
        if dataset == "weekly_rosters":
            return f"{cls.BASE_URL}/weekly_rosters/roster_weekly_{season_year}.csv"
        if dataset == "rosters":
            return f"{cls.BASE_URL}/rosters/roster_{season_year}.csv"
        raise ValueError(f"Unsupported nflverse dataset: {dataset!r}.")
