"""Tests for the transport-independent application layer."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from use_cases.errors import InvalidInput, ResourceNotFound
from use_cases.fundamentals import GetDeepFundamentals, GetFundamentals
from use_cases.realtime import GetQuote, UnsubscribeTicker
from use_cases.specialized import GetEtfDetails, GetIndexConstituents


@pytest.mark.asyncio
async def test_fundamentals_rejects_missing_identity_without_http_dependency():
    with pytest.raises(InvalidInput):
        await GetFundamentals().execute()


@pytest.mark.asyncio
async def test_fundamentals_returns_cache_metadata():
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({"General": {"Code": "AAPL"}})))
    result = await GetFundamentals(redis=redis).execute(ticker="AAPL")
    assert result.data == {"General": {"Code": "AAPL"}}
    assert result.cache_hit is True


@pytest.mark.asyncio
async def test_fundamentals_accepts_isin_without_database():
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({"General": {"ISIN": "FR0001"}})))
    result = await GetFundamentals(redis=redis).execute(isin="FR0001")
    assert result.data["General"]["ISIN"] == "FR0001"


@pytest.mark.asyncio
async def test_fundamentals_does_not_hide_unexpected_repository_errors():
    database = SimpleNamespace(
        get_asset_details=MagicMock(side_effect=RuntimeError("repository bug"))
    )
    with pytest.raises(RuntimeError, match="repository bug"):
        await GetFundamentals(database=database).execute(ticker="AAPL")


@pytest.mark.asyncio
async def test_fundamentals_orchestrates_injected_ports():
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=(
                {"YahooFinance": {"ticker": "AAPL"}},
                {"YahooFinance": "https://example.test/AAPL"},
            )
        )
    )
    formatter = SimpleNamespace(to_eodhd=MagicMock(return_value={"General": {"Code": "AAPL"}}))

    result = await GetFundamentals(
        provider_runner=runner,
        formatter=formatter,
    ).execute(ticker="AAPL", nocache=True)

    assert result.data == {"General": {"Code": "AAPL"}}
    assert result.provider_used == "YahooFinance"
    runner.run.assert_awaited_once()
    formatter.to_eodhd.assert_called_once()


@pytest.mark.asyncio
async def test_fundamentals_coordinates_identity_enrichment_and_cache_ports():
    database = SimpleNamespace(
        get_asset_details=MagicMock(return_value={"isin": "US0378331005"}),
        get_asset_context=MagicMock(
            return_value={
                "details": {
                    "asset_id": 42,
                    "listing_id": 7,
                    "ticker": "AAPL",
                    "isin": "US0378331005",
                },
                "mappings": {},
            }
        ),
        get_deep_fundamentals=MagicMock(
            return_value={
                "statements": {"income": []},
                "analyst_ratings": {"consensus": "buy"},
                "earnings_history": [],
            }
        ),
    )
    redis = SimpleNamespace(
        get=AsyncMock(return_value=None),
        setex=AsyncMock(),
    )
    runner = SimpleNamespace(run=AsyncMock(return_value=({"YahooFinance": {"ticker": "AAPL"}}, {})))
    profile_enricher = SimpleNamespace(enrich=AsyncMock())
    sec_provider = SimpleNamespace(fetch=AsyncMock(return_value={"transactions": []}))

    result = await GetFundamentals(
        database=database,
        redis=redis,
        provider_runner=runner,
        profile_enricher=profile_enricher,
        ticker_normalizer=lambda _ticker: "AAPL",
        sec_edgar_provider=sec_provider,
    ).execute(ticker="AAPL:NASDAQ", fmt="raw")

    assert result.data["asset_profile"]["asset_id"] == 42
    assert result.data["Financials"] == {"income": []}
    assert result.data["SECEdgar"] == {"transactions": []}
    profile_enricher.enrich.assert_awaited_once()
    sec_provider.fetch.assert_awaited_once_with(ticker="AAPL", limit=10)
    redis.setex.assert_awaited_once()


# ── The Yahoo symbol of a listing of the catalogue ─────────────────────────────


def _catalogue(profile: dict, deep: dict | None = None):
    """A repository knowing one listing, as ``get_asset_context`` returns it."""
    return SimpleNamespace(
        get_asset_details=MagicMock(return_value=profile),
        get_asset_context=MagicMock(return_value={"details": dict(profile), "mappings": {}}),
        get_deep_fundamentals=MagicMock(return_value=deep),
        get_deep_sections=MagicMock(return_value={}),
    )


def _symbols(symbol: str | None, reason: str | None = None):
    return SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(symbol=symbol, reason=reason))
    )


SPFF = {"asset_id": 3, "listing_id": 5, "ticker": "SPFF", "isin": "IE000AQ7A2X6", "currency": "EUR"}


@pytest.mark.asyncio
async def test_fundamentals_ask_yahoo_with_the_symbol_verified_for_the_listing():
    """``SPFF`` alone is a US fund on Yahoo: the listing is asked as ``SPFF.DE``."""
    runner = SimpleNamespace(run=AsyncMock(return_value=({}, {})))
    profile_enricher = SimpleNamespace(enrich=AsyncMock())
    symbols = _symbols("SPFF.DE")

    await GetFundamentals(
        database=_catalogue(SPFF),
        provider_runner=runner,
        profile_enricher=profile_enricher,
        symbols=symbols,
    ).execute(ticker="SPFF", fmt="raw", nocache=True)

    symbols.resolve.assert_awaited_once_with(5, refresh=False)
    assert runner.run.await_args.kwargs["verified_symbols"] == {"yahoofinance": "SPFF.DE"}
    assert runner.run.await_args.kwargs["refused_providers"] == {}
    assert profile_enricher.enrich.await_args.kwargs == {"symbol": "SPFF.DE"}


@pytest.mark.asyncio
async def test_fundamentals_do_not_ask_yahoo_about_a_listing_without_verified_symbol():
    reason = "No Yahoo symbol quoted in CHF for ISIN IE00B3S5XW04; Yahoo offers SYBB.DE (EUR)"
    runner = SimpleNamespace(run=AsyncMock(return_value=({}, {})))
    profile_enricher = SimpleNamespace(enrich=AsyncMock())

    await GetFundamentals(
        database=_catalogue({**SPFF, "ticker": "GOVY", "currency": "CHF"}),
        provider_runner=runner,
        profile_enricher=profile_enricher,
        symbols=_symbols(None, reason),
    ).execute(ticker="GOVY", currency="CHF", fmt="raw", nocache=True)

    assert runner.run.await_args.kwargs["refused_providers"] == {"yahoofinance": reason}
    assert runner.run.await_args.kwargs["verified_symbols"] == {}
    # The profile is not completed with the figures of another instrument either.
    profile_enricher.enrich.assert_not_awaited()


@pytest.mark.asyncio
async def test_ticker_outside_the_catalogue_is_asked_as_typed():
    runner = SimpleNamespace(run=AsyncMock(return_value=({}, {})))
    symbols = _symbols("never used")
    database = SimpleNamespace(
        get_asset_details=MagicMock(return_value=None),
        get_asset_context=MagicMock(return_value=None),
    )

    await GetFundamentals(database=database, provider_runner=runner, symbols=symbols).execute(
        ticker="AAPL", fmt="raw", nocache=True
    )

    symbols.resolve.assert_not_awaited()
    assert runner.run.await_args.kwargs["verified_symbols"] == {}
    assert runner.run.await_args.kwargs["refused_providers"] == {}


@pytest.mark.asyncio
async def test_stored_figures_reach_the_rendered_document():
    """With the real formatter: what the database holds is in the answer.

    The stored sections were handed over under names the formatter does not
    read; a formatter replaced by a mock could not show it.
    """
    from financials.formatter import FinancialsFormatter

    deep = {
        "highlights": {"pe_ratio": 38.0, "market_cap": 4800000000000},
        "statements": [
            {
                "statement_type": "income",
                "period_type": "annual",
                "period_end": "2025-09-27",
                "revenue": 416161000000.0,
            }
        ],
        "analyst_ratings": {"consensus": "buy", "strong_buy": 12},
        "earnings_history": [{"period": "Q3 2026", "period_end": "2026-06-27", "eps_actual": 1.9}],
        "earnings_trend": [{"period": "+1y", "eps_avg": 9.4}],
        "esg_scores": {"total_esg": 16.6},
        "etf_details": None,
        "etf_holdings": [],
    }
    runner = SimpleNamespace(
        run=AsyncMock(return_value=({"YahooFinance": {"trailingPE": 38.3, "beta": 1.085}}, {}))
    )

    result = await GetFundamentals(
        database=_catalogue({"asset_id": 42, "ticker": "AAPL", "isin": "US0378331005"}, deep),
        provider_runner=runner,
        formatter=FinancialsFormatter,
    ).execute(ticker="AAPL", nocache=True)

    document = result.data
    # The answer of this request first, the stored figures for what it lacks.
    assert document["Highlights"]["PERatio"] == 38.3
    assert document["Highlights"]["MarketCapitalization"] == 4800000000000
    assert document["Sources"]["Highlights"] == {
        "MarketCapitalization": "database",
        "PERatio": "YahooFinance",
    }
    assert document["Technicals"]["Beta"] == 1.085
    assert document["AnalystRatings"]["StrongBuy"] == 12
    assert document["Earnings"]["History"]["0"]["epsActual"] == 1.9
    assert document["Earnings"]["Trend"]["0"]["earningsEstimateAvg"] == 9.4
    assert document["ESGScores"]["TotalEsg"] == 16.6
    assert document["Financials"]["Income_Statement"]["yearly"]["2025-09-27"]["revenue"] == (
        "416161000000"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticker", "isin", "profile", "asked"),
    [
        ("AAPL", "US0378331005", None, True),
        ("AAPL", None, None, True),
        ("AIR.PA", None, None, False),
        # A ticker without suffix is not enough: SPFF, quoted in EUR, is not the US fund.
        ("SPFF", "IE000AQ7A2X6", None, False),
        ("SPFF", None, {"isin": "IE000AQ7A2X6", "currency": "EUR", "quote_type": "ETF"}, False),
        # Accenture has an Irish ISIN and files with the SEC: its listing is in USD.
        ("ACN", None, {"isin": "IE00B4BNMY34", "currency": "USD", "quote_type": "EQUITY"}, True),
        # A fund quoted in USD has no insiders.
        ("CSPX", None, {"isin": "IE00B5BMR087", "currency": "USD", "quote_type": "ETF"}, False),
    ],
)
async def test_insider_transactions_are_asked_for_shares_listed_in_the_us(
    ticker, isin, profile, asked
):
    runner = SimpleNamespace(run=AsyncMock(return_value=({}, {})))
    sec = SimpleNamespace(fetch=AsyncMock(return_value=None))
    database = _catalogue({"asset_id": 1, "ticker": ticker, **profile}) if profile else None

    await GetFundamentals(database=database, provider_runner=runner, sec_edgar_provider=sec).execute(
        ticker=ticker, isin=isin, fmt="raw", nocache=True
    )

    assert sec.fetch.await_count == (1 if asked else 0)


@pytest.mark.asyncio
async def test_slow_symbol_lookup_does_not_hold_the_answer(monkeypatch):
    import use_cases.fundamentals as fundamentals

    async def never_answers(_listing_id, *, refresh=False):
        await asyncio.sleep(60)

    monkeypatch.setattr(fundamentals, "SYMBOL_LOOKUP_TIMEOUT_SECONDS", 0.01)
    runner = SimpleNamespace(run=AsyncMock(return_value=({}, {})))

    await GetFundamentals(
        database=_catalogue(SPFF),
        provider_runner=runner,
        symbols=SimpleNamespace(resolve=never_answers),
    ).execute(ticker="SPFF", fmt="raw", nocache=True)

    assert runner.run.await_args.kwargs["refused_providers"] == {
        "yahoofinance": "Yahoo symbol lookup too slow for SPFF"
    }


@pytest.mark.asyncio
async def test_insider_transactions_are_stored_as_plain_data_in_the_cache():
    """The provider answers with a model; the cache holds JSON, read back as is."""
    from financials.providers.sec_edgar import InsiderTransaction, InsiderTransactionsResult

    answer = InsiderTransactionsResult(
        ticker="AAPL",
        transactions=[
            InsiderTransaction(
                filing_date="2026-10-01", insider_name="Newstead Jennifer", transaction_type="Sell"
            )
        ],
        total_count=1,
    )
    redis = SimpleNamespace(get=AsyncMock(return_value=None), setex=AsyncMock())
    runner = SimpleNamespace(run=AsyncMock(return_value=({}, {})))
    sec = SimpleNamespace(fetch=AsyncMock(return_value=answer))

    result = await GetFundamentals(redis=redis, provider_runner=runner, sec_edgar_provider=sec).execute(
        ticker="AAPL", fmt="raw"
    )

    cached = json.loads(redis.setex.await_args.args[2])
    assert cached["SECEdgar"] == result.data["SECEdgar"]
    assert cached["SECEdgar"]["transactions"][0]["filing_date"] == "2026-10-01"


@pytest.mark.asyncio
async def test_slow_insider_lookup_does_not_hold_the_answer(monkeypatch):
    import use_cases.fundamentals as fundamentals

    async def never_answers(**_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(fundamentals, "INSIDER_TIMEOUT_SECONDS", 0.01)
    runner = SimpleNamespace(run=AsyncMock(return_value=({"Msn": {"ticker": "AAPL"}}, {})))

    result = await GetFundamentals(
        provider_runner=runner, sec_edgar_provider=SimpleNamespace(fetch=never_answers)
    ).execute(ticker="AAPL", fmt="raw", nocache=True)

    assert "SECEdgar" not in result.data
    assert result.data["Msn"] == {"ticker": "AAPL"}


@pytest.mark.asyncio
async def test_deep_fundamentals_are_fetched_with_the_verified_symbol():
    enricher = SimpleNamespace(enrich=AsyncMock(return_value={}))
    symbols = _symbols("SPFF.DE")

    result = await GetDeepFundamentals(
        database=_catalogue(SPFF), enricher=enricher, symbols=symbols
    ).execute(ticker="SPFF", refresh=True)

    symbols.resolve.assert_awaited_once_with(5, refresh=True)
    enricher.enrich.assert_awaited_once_with(3, "SPFF.DE")
    assert result["meta"]["symbol"] == "SPFF.DE"
    assert "note" not in result["meta"]


@pytest.mark.asyncio
async def test_deep_fundamentals_of_a_listing_without_symbol_are_not_fetched():
    """Fetching ``SPFF`` would store the figures of a US fund under this instrument."""
    reason = "No Yahoo symbol quoted in EUR for ISIN IE000AQ7A2X6"
    enricher = SimpleNamespace(enrich=AsyncMock())

    result = await GetDeepFundamentals(
        database=_catalogue(SPFF), enricher=enricher, symbols=_symbols(None, reason)
    ).execute(ticker="SPFF")

    enricher.enrich.assert_not_awaited()
    assert result["meta"]["note"] == reason
    assert result["meta"]["source"] == "database"
    assert "symbol" not in result["meta"]


@pytest.mark.asyncio
async def test_refused_refresh_is_not_cached_and_a_cached_answer_keeps_its_symbol():
    cache = SimpleNamespace(enabled=True, get=MagicMock(return_value=None), set=MagicMock())
    enricher = SimpleNamespace(enrich=AsyncMock(return_value={}))

    await GetDeepFundamentals(
        database=_catalogue(SPFF), cache=cache, enricher=enricher, symbols=_symbols(None, "none")
    ).execute(ticker="SPFF")
    cache.set.assert_not_called()

    await GetDeepFundamentals(
        database=_catalogue(SPFF), cache=cache, enricher=enricher, symbols=_symbols("SPFF.DE")
    ).execute(ticker="SPFF")
    stored = cache.set.call_args.args[1]
    assert stored["meta"]["symbol"] == "SPFF.DE"

    cache.get.return_value = stored
    again = await GetDeepFundamentals(database=_catalogue(SPFF), cache=cache).execute(ticker="SPFF")
    assert again["meta"]["cache_hit"] is True
    assert again["meta"]["symbol"] == "SPFF.DE"


@pytest.mark.asyncio
async def test_deep_fundamentals_uses_application_errors():
    with pytest.raises(InvalidInput):
        await GetDeepFundamentals(database=MagicMock()).execute()


@pytest.mark.asyncio
async def test_deep_fundamentals_uses_repository_and_enricher_ports():
    database = SimpleNamespace(
        get_asset_context=MagicMock(
            return_value={
                "details": {
                    "asset_id": 42,
                    "ticker": "AAPL",
                    "isin": "US0378331005",
                    "name": "Apple",
                    "exchange": "NASDAQ",
                    "currency": "USD",
                }
            }
        ),
        get_deep_sections=MagicMock(return_value={"highlights": {"market_cap": 123}}),
    )
    enricher = SimpleNamespace(enrich=AsyncMock(return_value={"highlights": True}))

    result = await GetDeepFundamentals(
        database=database,
        enricher=enricher,
    ).execute(ticker="AAPL", sections="highlights")

    assert result["highlights"] == {"market_cap": 123}
    assert result["asset_profile"]["ticker"] == "AAPL"
    enricher.enrich.assert_awaited_once_with(42, "AAPL")
    # Every section is read (and cached); the request receives the ones it asked for.
    database.get_deep_sections.assert_called_once_with(42, set(), True)


@pytest.mark.asyncio
async def test_etf_use_case_rejects_non_etf_before_provider_call():
    provider = SimpleNamespace(fetch=AsyncMock())
    database = SimpleNamespace(
        get_asset_context=MagicMock(return_value={"details": {"quote_type": "EQUITY"}})
    )
    with pytest.raises(ResourceNotFound):
        await GetEtfDetails(provider, database).execute("FR0000000001")
    provider.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_use_case_validates_supported_names():
    with pytest.raises(InvalidInput):
        await GetIndexConstituents(provider=object(), index_name_enum=object()).execute("UNKNOWN")


@pytest.mark.asyncio
async def test_quote_use_case_builds_snapshot_from_worker_cache():
    worker = SimpleNamespace(
        get_quote_from_cache=AsyncMock(return_value={"close": 123.4, "previous_close": 120.0})
    )
    quote = await GetQuote(worker).execute("aapl")
    assert quote.ticker == "AAPL"
    assert float(quote.price) == 123.4
    assert quote.is_realtime is True


@pytest.mark.asyncio
async def test_unsubscribe_use_case_reports_missing_subscription():
    worker = SimpleNamespace(unsubscribe=AsyncMock(return_value=False))
    with pytest.raises(ResourceNotFound):
        await UnsubscribeTicker(worker).execute("aapl")


def test_use_case_modules_do_not_import_fastapi():
    root = Path(__file__).parents[1] / "use_cases"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        imported_roots = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "fastapi" not in imported_roots, path


def test_fundamentals_use_case_depends_only_on_application_ports():
    path = Path(__file__).parents[1] / "use_cases" / "fundamentals.py"
    tree = ast.parse(path.read_text())
    imported_roots = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_roots.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "database",
        "fastapi",
        "financials",
        "fundamental",
        "import_assets",
        "models",
        "redis",
        "sqlalchemy",
        "yfinance",
    }
    assert imported_roots.isdisjoint(forbidden)
