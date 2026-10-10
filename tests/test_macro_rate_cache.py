"""The shared cache of the macro sources (``macro/rate_cache.py``) when a layer fails.

Redis down, a database that refuses a read or a write: a rate still comes back
when one can be read, and nothing raises into the valuation or the route.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from macro.ecb_service import DEPOSIT_FACILITY_RATE, ECBService, parse_latest_observation
from macro.rate_cache import CachedRateService
from models import Base, MacroRateCache

CSV = "KEY,TIME_PERIOD,OBS_VALUE\n{key},{period},{value}\n"


def _ecb(text: str):
    response = httpx.Response(200, text=text, request=httpx.Request("GET", "https://ecb"))
    return AsyncMock(return_value=response)


def _answer(value: str = "2.0", period: str = "2026-10-08") -> str:
    return CSV.format(key=DEPOSIT_FACILITY_RATE.series_id, period=period, value=value)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine))
    db = MagicMock()
    db.get_session.side_effect = lambda: Session()
    yield MagicMock(db=db, Session=Session)
    Session.remove()
    engine.dispose()


def _broken_database():
    session = MagicMock()
    session.query.side_effect = OperationalError("SELECT", {}, Exception("database down"))
    db = MagicMock()
    db.get_session.return_value = session
    return db, session


class TestRedisDown:
    async def test_a_failing_read_falls_back_on_the_source(self, database):
        redis = AsyncMock()
        redis.get.side_effect = ConnectionError("redis down")
        service = ECBService(database.db, redis_client=redis)

        with patch("httpx.AsyncClient.get", _ecb(_answer())):
            rate = await service.get_series(DEPOSIT_FACILITY_RATE)

        assert (rate.value, rate.freshness) == (Decimal("0.020000"), "live")

    async def test_a_failing_write_still_gives_the_rate(self, database):
        redis = AsyncMock()
        redis.get.return_value = None
        redis.setex.side_effect = ConnectionError("redis down")
        service = ECBService(database.db, redis_client=redis)

        with patch("httpx.AsyncClient.get", _ecb(_answer())):
            rate = await service.get_series(DEPOSIT_FACILITY_RATE)

        assert rate.value == Decimal("0.020000")
        assert database.Session().query(MacroRateCache).count() == 1


class TestDatabaseDown:
    async def test_a_failing_read_and_write_still_give_the_rate_of_the_source(self):
        db, session = _broken_database()
        service = ECBService(db, redis_client=None)

        with patch("httpx.AsyncClient.get", _ecb(_answer())):
            rate = await service.get_series(DEPOSIT_FACILITY_RATE)

        assert (rate.value, rate.freshness, rate.source) == (Decimal("0.020000"), "live", "ecb")
        session.rollback.assert_called_once()
        assert session.close.call_count == 2  # the read and the write

    async def test_nothing_readable_anywhere_gives_no_rate(self):
        db, _ = _broken_database()
        service = ECBService(db, redis_client=None)

        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("down"))):
            assert await service.get_series(DEPOSIT_FACILITY_RATE) is None


class TestStoredValues:
    def test_a_date_of_reading_without_time_zone_is_utc(self, database):
        service = ECBService(database.db)
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)

        assert service._is_recent(recent) is True
        assert service._is_recent(recent - timedelta(days=2)) is False
        assert service._is_recent(None) is False

    async def test_a_source_that_cannot_be_asked_says_why(self, database, caplog):
        class Closed(CachedRateService):
            source = "closed"

            def _can_fetch(self):
                return False

        rate = await Closed(database.db)._get_series("X", "x")

        assert rate is None
        assert "closed: X cannot be read from the source" in caplog.text

    async def test_a_source_must_say_how_it_reads_a_series(self, database):
        with pytest.raises(NotImplementedError):
            await CachedRateService(database.db)._fetch_series("X", "x")


class TestEcbAnswers:
    def test_a_value_that_is_not_a_number_is_skipped(self):
        text = _answer("NaN", "2026-10-09") + _answer("2.0", "2026-10-08").split("\n", 1)[1]

        rate = parse_latest_observation(text, DEPOSIT_FACILITY_RATE)

        assert rate.observation_date == date(2026, 10, 8)

    def test_an_older_row_after_a_newer_one_does_not_replace_it(self):
        text = _answer("2.25", "2026-10-08") + _answer("2.0", "2026-10-01").split("\n", 1)[1]

        rate = parse_latest_observation(text, DEPOSIT_FACILITY_RATE)

        assert (rate.value, rate.observation_date) == (Decimal("0.022500"), date(2026, 10, 8))

    async def test_a_series_the_service_does_not_know_is_not_asked(self, database):
        get = AsyncMock()
        with patch("httpx.AsyncClient.get", get):
            assert await ECBService(database.db)._fetch_series("XX.UNKNOWN", "x") is None
        get.assert_not_awaited()
