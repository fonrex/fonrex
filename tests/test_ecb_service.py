"""Euro area rates from the ECB Data Portal (``macro/ecb_service.py``).

The answers below are the ones the ECB gives (``format=csvdata``); no test
reaches the network.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from macro.ecb_service import (
    DEPOSIT_FACILITY_RATE,
    EURO_RISK_FREE_10Y,
    SYSTEMIC_STRESS,
    ECBService,
    parse_latest_observation,
)
from models import Base, MacroRateCache

HEADER = (
    "KEY,FREQ,REF_AREA,CURRENCY,PROVIDER_FM,INSTRUMENT_FM,PROVIDER_FM_ID,DATA_TYPE_FM,"
    "TIME_PERIOD,OBS_VALUE,OBS_STATUS,OBS_CONF,OBS_PRE_BREAK,OBS_COM,TIME_FORMAT,BREAKS,"
    "COLLECTION,COMPILING_ORG,DISS_ORG,DOM_SER_IDS,FM_CONTRACT_TIME,FM_COUPON_RATE,"
    "FM_IDENTIFIER,FM_LOT_SIZE,FM_MATURITY,FM_OUTS_AMOUNT,FM_PUT_CALL,FM_STRIKE_PRICE,"
    "PUBL_MU,PUBL_PUBLIC,UNIT_INDEX_BASE,COMPILATION,COVERAGE,DECIMALS,SOURCE_AGENCY,"
    "SOURCE_PUB,TITLE,TITLE_COMPL,UNIT,UNIT_MULT"
)


def _row(period: str, value: str) -> str:
    # As the ECB answered for the AAA 10-year spot rate on 2026-10-08.
    return (
        f"YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y,B,U2,EUR,4F,G_N_A,SV_C_YM,SR_10Y,{period},"
        f"{value},A,F,,,P1D,,E,,,,,,,,,,,,,,,Technical notes are available at the following "
        "link: https://www.ecb.europa.eu/stats/financial_markets_and_interest_rates/"
        "euro_area_yield_curves/shared/pdf/technical_notes.pdf,,6,,,AAA yield curve - "
        '10-year spot rate,"Euro area (changing composition) - Government bond, nominal, all '
        "issuers whose rating is triple A - Svensson model - continuous compounding - yield "
        'error minimisation - Yield curve spot rate, 10-year maturity",PCPA,0'
    )


def _answer(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


class TestParsing:
    def test_the_rate_published_in_percent_is_stored_as_a_ratio(self):
        rate = parse_latest_observation(
            _answer(_row("2026-10-08", "3.51918971")), EURO_RISK_FREE_10Y
        )

        assert rate.series_id == "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
        assert (rate.value, rate.unit, rate.observation_date) == (
            Decimal("0.035192"),
            "ratio",
            date(2026, 10, 8),
        )

    def test_the_latest_observation_with_a_value_is_taken(self):
        text = _answer(
            _row("2026-10-06", "3.40"), _row("2026-10-08", ""), _row("2026-10-07", "3.45")
        )
        rate = parse_latest_observation(text, EURO_RISK_FREE_10Y)
        assert (rate.value, rate.observation_date) == (Decimal("0.034500"), date(2026, 10, 7))

    def test_a_negative_rate_is_a_rate(self):
        # The AAA 10-year rate was negative in 2019-2021.
        rate = parse_latest_observation(_answer(_row("2020-08-03", "-0.5215")), EURO_RISK_FREE_10Y)
        assert rate.value == Decimal("-0.005215")

    def test_an_index_is_stored_as_published(self):
        text = "KEY,TIME_PERIOD,OBS_VALUE\nCISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX,2026-10-08,0.0712\n"
        rate = parse_latest_observation(text, SYSTEMIC_STRESS)
        assert (rate.value, rate.unit) == (Decimal("0.071200"), "index")

    def test_an_answer_without_observation_gives_nothing(self):
        assert parse_latest_observation(_answer(), EURO_RISK_FREE_10Y) is None
        assert parse_latest_observation("", EURO_RISK_FREE_10Y) is None
        assert (
            parse_latest_observation(_answer(_row("2026-10-08", "n/a")), EURO_RISK_FREE_10Y) is None
        )

    def test_series_names_fit_the_cache_table(self):
        for series in (EURO_RISK_FREE_10Y, DEPOSIT_FACILITY_RATE, SYSTEMIC_STRESS):
            assert len(series.series_id) <= MacroRateCache.__table__.c.series_id.type.length


@pytest.fixture
def stored():
    """A real table of stored rates, and an ECB service reading it without Redis."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    database = MagicMock()
    database.get_session.side_effect = lambda: Session()

    def store(value, observed, age):
        session = Session()
        session.add(
            MacroRateCache(
                series_id=EURO_RISK_FREE_10Y.series_id,
                source="ecb",
                label="10Y",
                value=Decimal(value),
                unit="ratio",
                observation_date=observed,
                fetched_at=datetime.now(timezone.utc) - age,
            )
        )
        session.commit()

    def rows():
        return Session().query(MacroRateCache).order_by(MacroRateCache.observation_date).all()

    yield MagicMock(service=ECBService(database, redis_client=None), store=store, rows=rows)
    Session.remove()
    engine.dispose()


def _ecb_answers(text: str, status: int = 200):
    response = httpx.Response(status, text=text, request=httpx.Request("GET", "https://ecb"))
    return AsyncMock(return_value=response)


class TestService:
    async def test_rate_read_now_is_live_stored_and_asked_with_the_series_key(self, stored):
        get = _ecb_answers(_answer(_row("2026-10-08", "3.51918971")))
        with patch("httpx.AsyncClient.get", get):
            rate = await stored.service.get_euro_risk_free_rate()

        assert (rate.value, rate.source, rate.observation_date) == (
            Decimal("0.035192"),
            "ecb_live",
            date(2026, 10, 8),
        )
        url = get.await_args.args[0]
        assert url == (
            "https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
        )
        assert get.await_args.kwargs["params"]["format"] == "csvdata"
        (row,) = stored.rows()
        assert (row.source, row.series_id, row.value) == (
            "ecb",
            "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",
            Decimal("0.035192"),
        )

    async def test_rate_read_recently_is_not_asked_again(self, stored):
        stored.store("0.0350", date(2026, 10, 8), timedelta(hours=1))
        get = _ecb_answers(_answer())
        with patch("httpx.AsyncClient.get", get):
            rate = await stored.service.get_euro_risk_free_rate()

        assert (rate.value, rate.source) == (Decimal("0.0350"), "ecb_cached")
        get.assert_not_awaited()

    @pytest.mark.parametrize(
        "failure",
        [
            AsyncMock(side_effect=httpx.ConnectTimeout("no answer")),
            _ecb_answers("Service unavailable", status=503),
            _ecb_answers(_answer()),
        ],
        ids=["timeout", "error", "no observation"],
    )
    async def test_older_stored_rate_is_what_is_left_when_the_ecb_fails(self, stored, failure):
        stored.store("0.0300", date(2026, 9, 1), timedelta(days=30))
        with patch("httpx.AsyncClient.get", failure):
            rate = await stored.service.get_euro_risk_free_rate()

        assert (rate.value, rate.source, rate.observation_date) == (
            Decimal("0.0300"),
            "ecb_stale",
            date(2026, 9, 1),
        )

    async def test_nothing_known_gives_no_rate_and_the_caller_chooses(self, stored):
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("down"))):
            assert await stored.service.get_euro_risk_free_rate() is None

    async def test_the_api_address_can_be_set(self, stored, monkeypatch):
        monkeypatch.setenv("ECB_API_URL", "https://mirror.example/service/data/")
        service = ECBService(stored.service.db_service)
        get = _ecb_answers(_answer(_row("2026-10-08", "3.5")))
        with patch("httpx.AsyncClient.get", get):
            await service.get_series(DEPOSIT_FACILITY_RATE)

        assert get.await_args.args[0] == (
            "https://mirror.example/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
        )

    async def test_redis_holds_the_rate_under_the_ecb_prefix(self, stored):
        redis = AsyncMock()
        redis.get.return_value = None
        service = ECBService(stored.service.db_service, redis_client=redis)
        with patch("httpx.AsyncClient.get", _ecb_answers(_answer(_row("2026-10-08", "3.5")))):
            await service.get_euro_risk_free_rate()

        key = redis.setex.await_args.args[0]
        assert key == "macro:ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"


class TestRatesOfTheEuroArea:
    async def test_every_series_known_is_given_with_its_source_and_currency(self, stored):
        stored.store("0.0350", date(2026, 10, 8), timedelta(hours=1))
        # The two other series are asked and the ECB does not answer.
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("down"))):
            answer = await stored.service.get_rates()

        assert answer.currency == "EUR"
        assert answer.risk_free_rate.value == Decimal("0.0350")
        assert [rate.series_id for rate in answer.rates] == [EURO_RISK_FREE_10Y.series_id]
        assert (answer.rates[0].source, answer.rates[0].currency) == ("ecb", "EUR")

    async def test_a_rate_read_from_redis_says_where_it_comes_from(self, stored):
        redis = AsyncMock()
        redis.get.return_value = (
            '{"series_id": "FM.D.U2.EUR.4F.KR.DFR.LEV", "label": "DFR", "value": "0.02",'
            ' "unit": "ratio", "observation_date": "2026-10-08"}'
        )
        service = ECBService(stored.service.db_service, redis_client=redis)

        rate = await service.get_series(DEPOSIT_FACILITY_RATE)

        assert (rate.freshness, rate.source, rate.currency) == ("cached", "ecb", "EUR")
