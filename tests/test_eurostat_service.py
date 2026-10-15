"""Indicators of Eurostat (``macro/eurostat_service.py``).

The answers are the reduced real ones of ``tests/fixtures/eurostat/``. Storing and
refreshing the series is tested on PostgreSQL (``tests/test_timescale_integration.py``).
No test reaches the network.
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from macro.eurostat_service import (
    EURO_AREA,
    INDICATORS,
    EurostatError,
    EurostatService,
    download_series,
    indicator,
    normalize_geo,
    parse_period,
    parse_sdmx_csv,
    series_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "eurostat"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _by_geo(text: str, name: str, geo: str, column: str = "geo", **only: str) -> str:
    """The rows of one series of a fixture holding several (header kept)."""
    lines = text.splitlines()
    header = lines[0].split(",")
    kept = [
        line
        for line in lines[1:]
        if (cells := dict(zip(header, line.split(","), strict=True)))[column] == geo
        and all(cells[key] == value for key, value in only.items())
    ]
    return "\r\n".join([lines[0], *kept]) + "\r\n"


class TestCatalogue:
    def test_every_indicator_names_a_flow_and_a_key_with_the_country(self):
        assert set(INDICATORS) == {
            "hicp",
            "hicp_core",
            "unemployment",
            "gdp_qoq",
            "gdp_yoy",
            "economic_sentiment",
            "consumer_confidence",
        }
        for item in INDICATORS.values():
            assert "{geo}" in item.key
            assert item.frequency in ("monthly", "quarterly")
            assert item.unit in ("ratio", "index", "balance")

    def test_inflation_comes_from_the_ecoicop_2_flow(self):
        # prc_hicp_manr stopped in December 2025, when Eurostat moved to ECOICOP 2.
        assert indicator("hicp").flow == "prc_hicp_minr"
        assert indicator("HICP_Core").series_key("fr") == "M.RCH_A.TOT_X_NRG_FOOD.FR"

    def test_an_unknown_indicator_is_refused(self):
        with pytest.raises(KeyError):
            indicator("gdp")


class TestGeo:
    def test_the_euro_area_is_its_composition_since_2026(self):
        assert normalize_geo("EA") == normalize_geo(" ea21 ") == EURO_AREA == "EA21"

    def test_a_country_keeps_its_code(self):
        assert normalize_geo("fr") == "FR"
        assert normalize_geo("EU27_2020") == "EU27_2020"

    @pytest.mark.parametrize("geo", ["", "F", "FR;DROP", "EA+FR", "ABCDEFGHIJK"])
    def test_a_code_that_is_not_one_is_refused(self, geo):
        with pytest.raises(ValueError):
            normalize_geo(geo)


class TestAddresses:
    def test_the_series_of_a_country(self, monkeypatch):
        monkeypatch.delenv("EUROSTAT_API_URL", raising=False)
        assert series_url(indicator("unemployment"), "EA") == (
            "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/"
            "une_rt_m/M.SA.TOTAL.PC_ACT.T.EA21"
        )

    def test_a_mirror_of_eurostat_is_used(self, monkeypatch):
        monkeypatch.setenv("EUROSTAT_API_URL", " https://mirror.example/data/ ")
        assert series_url(indicator("gdp_qoq"), "FR") == (
            "https://mirror.example/data/namq_10_gdp/Q.CLV_PCH_PRE.SCA.B1GQ.FR"
        )


class TestPeriods:
    @pytest.mark.parametrize(
        ("raw", "first_day"),
        [
            ("2026-09", date(2026, 9, 1)),
            ("2026-Q1", date(2026, 1, 1)),
            ("2026-Q2", date(2026, 4, 1)),
            ("2026-Q4", date(2026, 10, 1)),
            ("2025", date(2025, 1, 1)),
        ],
    )
    def test_a_period_is_dated_by_its_first_day(self, raw, first_day):
        assert parse_period(raw) == first_day

    @pytest.mark.parametrize("raw", ["2026-13", "2026-Q5", "2026-09-01", "Sep 2026", ""])
    def test_a_text_that_is_not_a_period_is_refused(self, raw):
        with pytest.raises(EurostatError):
            parse_period(raw)


class TestAnswers:
    @pytest.mark.parametrize(
        ("fixture", "name", "series", "filters"),
        [
            ("hicp_total.csv", "hicp", "EA", {}),
            ("hicp_total.csv", "hicp", "FR", {}),
            ("hicp_total.csv", "hicp", "DE", {}),
            ("hicp_core.csv", "hicp_core", "EA", {}),
            ("unemployment.csv", "unemployment", "EA21", {}),
            ("unemployment.csv", "unemployment", "FR", {}),
            ("gdp_growth.csv", "gdp_qoq", "EA CLV_PCH_PRE", {"unit": "CLV_PCH_PRE"}),
            ("gdp_growth.csv", "gdp_qoq", "FR CLV_PCH_PRE", {"unit": "CLV_PCH_PRE"}),
            ("gdp_growth.csv", "gdp_yoy", "EA CLV_PCH_SM", {"unit": "CLV_PCH_SM"}),
            ("sentiment.csv", "economic_sentiment", "EA21 BS-ESI-I", {"indic": "BS-ESI-I"}),
            (
                "sentiment.csv",
                "consumer_confidence",
                "FR BS-CSMCI-BAL",
                {"indic": "BS-CSMCI-BAL"},
            ),
        ],
    )
    def test_the_latest_value_of_a_real_answer(self, fixture, name, series, filters):
        expected = EXPECTED[fixture][series]
        geo = series.split()[0]
        answer = parse_sdmx_csv(_by_geo(_read(fixture), name, geo, **filters), indicator(name))

        latest = answer.observations[-1]
        assert latest.period == date.fromisoformat(expected["period"])
        assert latest.value == Decimal(expected["value"])
        if "status" in expected:
            assert latest.status == (expected["status"] or None)
        if "unit" in expected:
            assert indicator(name).unit == expected["unit"]

    def test_rates_are_ratios_and_the_estimate_is_flagged(self):
        answer = parse_sdmx_csv(_by_geo(_read("hicp_total.csv"), "hicp", "EA"), indicator("hicp"))

        assert [(o.period, o.value, o.status) for o in answer.observations[-2:]] == [
            (date(2026, 8, 1), Decimal("0.03200000"), None),
            (date(2026, 9, 1), Decimal("0.03800000"), "e"),
        ]

    def test_a_negative_growth_stays_negative(self):
        text = _by_geo(_read("gdp_growth.csv"), "gdp_qoq", "FR", unit="CLV_PCH_PRE")
        values = {
            o.period: o.value for o in parse_sdmx_csv(text, indicator("gdp_qoq")).observations
        }
        assert values[date(2026, 1, 1)] == Decimal("-0.00200000")

    def test_the_last_update_of_eurostat_is_kept_in_utc(self):
        answer = parse_sdmx_csv(_read("unemployment.csv"), indicator("unemployment"))
        # 08/10/26 11:00:00 in Luxembourg (summer time) is 09:00 UTC.
        assert answer.source_updated_at == datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)

    def test_a_period_without_value_is_left_out(self):
        text = (
            "DATAFLOW,LAST UPDATE,freq,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG,CONF_STATUS\r\n"
            "ESTAT:X(1.0),,M,FR,2026-07,1.5,,\r\n"
            "ESTAT:X(1.0),,M,FR,2026-08,,c,C\r\n"
            "ESTAT:X(1.0),,M,FR,2026-09,NaN,,\r\n"
        )
        answer = parse_sdmx_csv(text, indicator("unemployment"))
        assert [o.period for o in answer.observations] == [date(2026, 7, 1)]
        assert answer.source_updated_at is None

    def test_an_empty_answer_holds_nothing(self):
        assert parse_sdmx_csv("", indicator("hicp")).observations == []

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("<html>maintenance</html>\n", "TIME_PERIOD"),
            ("TIME_PERIOD,OBS_VALUE\r\n2026-09,n/a\r\n", "not a number"),
            ("TIME_PERIOD,OBS_VALUE\r\nSep 2026,1.0\r\n", "not a period"),
        ],
    )
    def test_an_answer_that_cannot_be_read_is_refused(self, text, message):
        with pytest.raises(EurostatError, match=message):
            parse_sdmx_csv(text, indicator("hicp"))


def _eurostat(text: str, status: int = 200):
    request = httpx.Request("GET", "https://eurostat")
    return AsyncMock(return_value=httpx.Response(status, text=text, request=request))


class TestDownload:
    async def test_the_whole_series_without_start(self):
        get = _eurostat(_by_geo(_read("hicp_total.csv"), "hicp", "FR"))
        with patch("httpx.AsyncClient.get", get):
            answer = await download_series(indicator("hicp"), "FR")

        assert len(answer.observations) == 6
        assert get.await_args.args[0].endswith("/prc_hicp_minr/M.RCH_A.TOTAL.FR")
        assert get.await_args.kwargs["params"] == {"format": "SDMX-CSV"}

    async def test_a_monthly_start(self):
        get = _eurostat("")
        with patch("httpx.AsyncClient.get", get):
            answer = await download_series(indicator("hicp"), "EA", date(2024, 9, 1))
        assert answer.observations == []
        assert get.await_args.kwargs["params"]["startPeriod"] == "2024-09"

    async def test_a_quarterly_start(self):
        get = _eurostat("")
        with patch("httpx.AsyncClient.get", get):
            await download_series(indicator("gdp_qoq"), "EA", date(2024, 4, 1))
        assert get.await_args.kwargs["params"]["startPeriod"] == "2024-Q2"

    async def test_a_code_eurostat_does_not_know_is_refused_with_its_reason(self):
        get = _eurostat(_read("error_invalid_geo.xml"), status=400)
        with patch("httpx.AsyncClient.get", get):
            with pytest.raises(EurostatError, match="not allowed: GEO=EA") as refused:
                await download_series(indicator("unemployment"), "XX")
        assert "&apos;" not in str(refused.value)

    async def test_an_unknown_flow_is_refused(self):
        with patch("httpx.AsyncClient.get", _eurostat(_read("error_unknown_dataset.xml"), 404)):
            with pytest.raises(EurostatError, match="not available for dissemination"):
                await download_series(indicator("hicp"), "FR")

    async def test_a_refusal_without_reason_gives_its_status(self):
        with patch("httpx.AsyncClient.get", _eurostat("", 404)):
            with pytest.raises(EurostatError, match="HTTP 404"):
                await download_series(indicator("hicp"), "FR")

    async def test_a_server_error_is_raised(self):
        with patch("httpx.AsyncClient.get", _eurostat("down", 503)):
            with pytest.raises(httpx.HTTPStatusError):
                await download_series(indicator("hicp"), "FR")


class TestService:
    def test_the_refresh_delay_follows_the_environment(self, monkeypatch):
        monkeypatch.setenv("EUROSTAT_REFRESH_HOURS", "48")
        assert EurostatService(session_factory=None).refresh_after.total_seconds() == 48 * 3600

    def test_the_refresh_delay_defaults_to_a_day(self, monkeypatch):
        monkeypatch.setenv("EUROSTAT_REFRESH_HOURS", "0")  # out of range: the default
        assert EurostatService(session_factory=None).refresh_after.total_seconds() == 24 * 3600

    def test_a_reading_without_time_zone_is_in_utc(self):
        service = EurostatService(session_factory=None)
        assert service._is_recent(datetime.now(timezone.utc).replace(tzinfo=None))
        assert not service._is_recent(None)
        assert not service._is_recent(datetime(2020, 1, 1, tzinfo=timezone.utc))
