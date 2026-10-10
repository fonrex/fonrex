"""Factor returns of the Kenneth French Data Library (``factors/``).

The fixtures of ``tests/fixtures/factors/`` are the real files of the library
(August 2026), reduced to their first and last three months and the start of
their annual table. No test reaches the network.
"""

import io
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from factors import store
from factors.french_library import (
    DATASETS,
    FREQUENCIES,
    FactorFileError,
    download_factor_table,
    file_url,
    parse_factor_csv,
    read_zip,
)
from factors.store import FactorLibrary
from scripts import load_factors

FIXTURES = Path(__file__).parent / "fixtures" / "factors"


def _fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}_CSV.zip").read_bytes()


def _table(name: str):
    return parse_factor_csv(read_zip(_fixture(name)), "monthly")


def _zip(**files: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


# ── Reading a file ────────────────────────────────────────────────────────────


class TestUsFiles:
    def test_three_factors_are_read_as_ratios_and_dated_by_month_end(self):
        table = _table("F-F_Research_Data_Factors")

        assert table.factors == ("MKT_RF", "SMB", "HML", "RF")
        assert (table.first_period, table.last_period) == (date(1926, 7, 31), date(2026, 8, 31))
        # 192607,   2.89,  -2.42,  -2.75,   0.22
        assert table.rows[date(1926, 7, 31)] == {
            "MKT_RF": Decimal("0.0289"),
            "SMB": Decimal("-0.0242"),
            "HML": Decimal("-0.0275"),
            "RF": Decimal("0.0022"),
        }
        assert table.note == "CRSP 202608"

    def test_the_annual_table_that_follows_is_not_read(self):
        table = _table("F-F_Research_Data_Factors")

        assert len(table.rows) == 6  # three first and three last months of the fixture
        assert all(period.year != 1927 or period.month != 12 for period in table.rows)

    def test_five_factors(self):
        table = _table("F-F_Research_Data_5_Factors_2x3")

        assert table.factors == ("MKT_RF", "SMB", "HML", "RMW", "CMA", "RF")
        assert table.first_period == date(1963, 7, 31)
        assert table.rows[date(2026, 8, 31)]["CMA"] == Decimal("-0.0201")

    def test_momentum_is_named_mom(self):
        table = _table("F-F_Momentum_Factor")

        assert table.factors == ("MOM",)
        assert table.rows[date(1927, 1, 31)] == {"MOM": Decimal("0.0057")}


class TestEuropeFiles:
    def test_three_factors_padded_with_spaces(self):
        table = _table("Europe_3_Factors")

        assert table.first_period == date(1990, 7, 31)
        # 199007    ,4.46    ,0.29   ,-1.52    ,0.68
        assert table.rows[date(1990, 7, 31)]["MKT_RF"] == Decimal("0.0446")
        assert table.note == "Bloomberg 202608"

    def test_five_factors(self):
        assert _table("Europe_5_Factors").factors == ("MKT_RF", "SMB", "HML", "RMW", "CMA", "RF")

    def test_winners_minus_losers_is_momentum(self):
        table = _table("Europe_Mom_Factor")

        assert table.factors == ("MOM",)
        assert table.first_period == date(1990, 11, 30)


class TestOddContent:
    HEADER = "Notes\r\n\r\n,Mkt-RF,SMB,HML,RF\r\n"

    def test_missing_values_are_left_out(self):
        text = (
            self.HEADER
            + "202001, -99.99, 1.00, -999, 0.10\r\n202002, -99.99, -999, -99.99, -999\r\n"
        )

        table = parse_factor_csv(text, "monthly")

        assert table.rows == {date(2020, 1, 31): {"SMB": Decimal("0.01"), "RF": Decimal("0.001")}}

    def test_daily_rows_are_dated_by_their_day(self):
        text = (
            self.HEADER
            + "20260803,  0.52, -0.10,  0.07,  0.015\r\n20260804, -0.31,  0.20, -0.11,  0.015\r\n"
        )

        table = parse_factor_csv(text, "daily")

        assert table.first_period == date(2026, 8, 3)
        assert table.rows[date(2026, 8, 4)]["MKT_RF"] == Decimal("-0.0031")
        assert table.rows[date(2026, 8, 4)]["RF"] == Decimal("0.00015")

    def test_the_database_is_read_when_the_note_names_the_program_first(self):
        text = (
            "This file was created by CMPT_ME_BEME_RETS_DAILY using the 202608 CRSP database.\r\n"
            + self.HEADER
            + "20260803,  0.52, -0.10,  0.07,  0.015\r\n"
        )

        assert parse_factor_csv(text, "daily").note == "CRSP 202608"

    def test_a_monthly_reading_of_a_daily_file_finds_nothing(self):
        text = self.HEADER + "20260803,  0.52, -0.10,  0.07,  0.015\r\n"

        with pytest.raises(FactorFileError, match="empty"):
            parse_factor_csv(text, "monthly")

    @pytest.mark.parametrize(
        "text, message",
        [
            ("no table here\r\n", "no header"),
            ("x\r\n,Mkt-RF,Foo\r\n202001,1,2\r\n", "no header"),
            (HEADER + "202001, 1.0, 2.0\r\n", "has 2 values"),
            (HEADER + "202001, 1.0, n/a, 2.0, 0.1\r\n", "is not a number"),
            (HEADER + "\r\n", "empty"),
        ],
        ids=["no header", "unknown factor", "short row", "not a number", "no row"],
    )
    def test_a_file_that_cannot_be_read_is_refused(self, text, message):
        with pytest.raises(FactorFileError, match=message):
            parse_factor_csv(text, "monthly")

    def test_an_impossible_period_ends_the_table(self):
        text = self.HEADER + "202001, 1, 1, 1, 0.1\r\n202013, 1, 1, 1, 0.1\r\n"

        assert list(parse_factor_csv(text, "monthly").rows) == [date(2020, 1, 31)]

    def test_an_answer_that_is_not_a_zip(self):
        with pytest.raises(FactorFileError, match="not a zip"):
            read_zip(b"<html>Not found</html>")

    def test_a_zip_with_several_csv(self):
        with pytest.raises(FactorFileError, match="one CSV"):
            read_zip(_zip(**{"a.csv": "", "b.CSV": ""}))


# ── The catalogue and the download ────────────────────────────────────────────


class TestCatalogue:
    def test_nine_datasets_monthly_and_daily_each_with_its_own_file(self):
        files = {
            DATASETS[name].file_name(frequency) for name in DATASETS for frequency in FREQUENCIES
        }

        assert len(DATASETS) == 9 and len(files) == 18
        assert {dataset.region for dataset in DATASETS.values()} == {"us", "europe", "developed"}

    def test_the_address_of_a_file(self, monkeypatch):
        monkeypatch.delenv("FRENCH_LIBRARY_URL", raising=False)
        assert file_url("europe_3", "daily") == (
            "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_3_Factors_Daily_CSV.zip"
        )

    def test_a_mirror_can_be_set(self, monkeypatch):
        monkeypatch.setenv("FRENCH_LIBRARY_URL", "https://mirror.example/ff/")
        assert file_url("us_3", "monthly") == (
            "https://mirror.example/ff/F-F_Research_Data_Factors_CSV.zip"
        )


def _answer(content: bytes, status: int = 200):
    request = httpx.Request("GET", "https://library")
    return AsyncMock(return_value=httpx.Response(status, content=content, request=request))


class TestDownload:
    async def test_a_file_is_downloaded_and_read(self):
        get = _answer(_fixture("Europe_3_Factors"))
        with patch("httpx.AsyncClient.get", get):
            table = await download_factor_table("europe_3", "monthly")

        assert table.factors == ("MKT_RF", "SMB", "HML", "RF")
        assert get.await_args.args[0].endswith("/Europe_3_Factors_CSV.zip")

    async def test_a_file_with_other_factors_is_refused(self):
        with patch("httpx.AsyncClient.get", _answer(_fixture("F-F_Research_Data_Factors"))):
            with pytest.raises(FactorFileError, match="not CMA"):
                await download_factor_table("us_5", "monthly")

    async def test_an_http_error_is_raised(self):
        with patch("httpx.AsyncClient.get", _answer(b"", status=404)):
            with pytest.raises(httpx.HTTPStatusError):
                await download_factor_table("us_3", "monthly")


# ── Refreshing the stored files ───────────────────────────────────────────────


class FakeStore:
    """What ``factors.store`` reads and writes, kept in memory."""

    def __init__(self):
        self.loads = {}
        self.tables = {}

    async def read_load(self, session, dataset, frequency):
        return self.loads.get((dataset, frequency))

    async def replace_table(self, session, dataset, frequency, table, fetched_at):
        self.tables[(dataset, frequency)] = table
        self.loads[(dataset, frequency)] = SimpleNamespace(
            dataset=dataset,
            frequency=frequency,
            fetched_at=fetched_at,
            first_period=table.first_period,
            last_period=table.last_period,
            periods=len(table.rows),
            source_note=table.note,
        )

    async def read_series(self, session, dataset, frequency, start, end):
        rows = self.tables[(dataset, frequency)].rows
        return {
            p: v
            for p, v in rows.items()
            if (start is None or p >= start) and (end is None or p <= end)
        }

    def stored(self, age: timedelta, dataset="us_3", frequency="monthly"):
        table = _table("F-F_Research_Data_Factors")
        self.tables[(dataset, frequency)] = table
        self.loads[(dataset, frequency)] = SimpleNamespace(
            dataset=dataset,
            frequency=frequency,
            fetched_at=(datetime.now(timezone.utc) - age).replace(tzinfo=None),
            first_period=table.first_period,
            last_period=table.last_period,
            periods=len(table.rows),
            source_note=table.note,
        )


class FakeSession:
    @asynccontextmanager
    async def begin(self):
        yield


@pytest.fixture
def library(monkeypatch):
    fake = FakeStore()
    for name in ("read_load", "replace_table", "read_series"):
        monkeypatch.setattr(store, name, getattr(fake, name))

    @asynccontextmanager
    async def session_factory():
        yield FakeSession()

    downloader = AsyncMock(return_value=_table("F-F_Research_Data_Factors"))
    return SimpleNamespace(
        fake=fake,
        downloader=downloader,
        library=FactorLibrary(session_factory, downloader=downloader),
    )


class TestRefresh:
    async def test_a_file_never_read_is_downloaded_and_stored(self, library):
        load = await library.library.refresh("us_3", "monthly")

        assert (load.status, load.periods, load.last_period) == ("fetched", 6, date(2026, 8, 31))
        assert load.source_note == "CRSP 202608"
        library.downloader.assert_awaited_once_with("us_3", "monthly")
        assert ("us_3", "monthly") in library.fake.tables

    async def test_a_file_read_recently_is_not_asked_again(self, library):
        library.fake.stored(timedelta(days=2))

        load = await library.library.refresh("us_3", "monthly")

        assert load.status == "fresh"
        library.downloader.assert_not_awaited()

    async def test_force_downloads_a_recent_file(self, library):
        library.fake.stored(timedelta(hours=1))

        assert (await library.library.refresh("us_3", "monthly", force=True)).status == "fetched"

    async def test_an_old_file_is_downloaded_again(self, library):
        library.fake.stored(timedelta(days=8))

        assert (await library.library.refresh("us_3", "monthly")).status == "fetched"

    async def test_a_failed_download_keeps_what_was_stored(self, library):
        library.fake.stored(timedelta(days=30))
        library.downloader.side_effect = httpx.ConnectError("no answer")

        load = await library.library.refresh("us_3", "monthly")

        assert (load.status, load.periods) == ("failed", 6)
        assert load.reason == "ConnectError: no answer"

    async def test_a_failed_download_with_nothing_stored(self, library):
        library.downloader.side_effect = FactorFileError("the answer is not a zip file")

        load = await library.library.refresh("europe_5", "daily")

        assert (load.status, load.periods, load.reason) == (
            "failed",
            0,
            "FactorFileError: the answer is not a zip file",
        )

    async def test_the_refresh_delay_follows_the_environment(self, library, monkeypatch):
        monkeypatch.setenv("FACTORS_REFRESH_DAYS", "30")
        library.fake.stored(timedelta(days=8))
        patient = FactorLibrary(library.library.session_factory, downloader=library.downloader)

        assert (await patient.refresh("us_3", "monthly")).status == "fresh"

    @pytest.mark.parametrize("dataset, frequency", [("us_4", "monthly"), ("us_3", "weekly")])
    async def test_an_unknown_file_is_refused(self, library, dataset, frequency):
        with pytest.raises(ValueError, match="Unknown"):
            await library.library.refresh(dataset, frequency)


class TestStoredFiles:
    async def test_status(self, library):
        assert await library.library.status("us_3", "monthly") is None
        library.fake.stored(timedelta(days=1))
        assert (await library.library.status("us_3", "monthly")).status == "fresh"
        library.fake.stored(timedelta(days=10))
        assert (await library.library.status("us_3", "monthly")).status == "stale"

    async def test_series_between_two_dates(self, library):
        library.fake.stored(timedelta(days=1))

        series = await library.library.series(
            "us_3", "monthly", date(2026, 6, 1), date(2026, 7, 31)
        )

        assert list(series) == [date(2026, 6, 30), date(2026, 7, 31)]


# ── The script ────────────────────────────────────────────────────────────────


class TestScript:
    def test_every_dataset_monthly_by_default(self):
        arguments = load_factors.parse_arguments([])

        assert arguments.dataset == sorted(DATASETS)
        assert (arguments.frequency, arguments.force) == (["monthly"], False)

    async def test_each_file_asked_is_refreshed(self, library):
        arguments = load_factors.parse_arguments(
            ["--dataset", "us_3", "europe_3", "--frequency", "monthly", "daily", "--force"]
        )

        loads = await load_factors.load(library.library, arguments)

        assert [(load.dataset, load.frequency) for load in loads] == [
            ("us_3", "monthly"),
            ("us_3", "daily"),
            ("europe_3", "monthly"),
            ("europe_3", "daily"),
        ]

    def test_a_line_per_file(self):
        fetched = store.FactorLoad(
            "us_3",
            "monthly",
            "fetched",
            periods=1202,
            first_period=date(1926, 7, 31),
            last_period=date(2026, 8, 31),
        )
        failed = store.FactorLoad("us_5", "daily", "failed", reason="ConnectError: down")

        assert load_factors.describe(fetched).split() == [
            "us_3",
            "monthly",
            "fetched",
            "1202",
            "periods",
            "1926-07-31",
            "→",
            "2026-08-31",
        ]
        assert load_factors.describe(failed).endswith("nothing stored  (ConnectError: down)")

    async def test_without_database_the_script_stops(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("ASYNC_DATABASE_URL", raising=False)

        assert await load_factors.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err
