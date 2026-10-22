"""The LEI of an instrument (``financials/esef_lei.py``): asked by ISIN, verified by name.

The answers of GLEIF are the real ones of ``tests/fixtures/esef/``. The stored mapping
is tested on PostgreSQL (``tests/test_timescale_integration.py``). No test reaches GLEIF.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from financials.esef_lei import (
    FOUND_BY_ISIN,
    NOT_FOUND,
    RETRY_AFTER,
    SET_BY_HAND,
    LeiInstrument,
    LeiResolution,
    LeiResolver,
    same_entity,
)
from financials.providers.esef import EsefError, LeiRecord, parse_lei_records

FIXTURES = Path(__file__).parent / "fixtures" / "esef"
NOW = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)

AIRBUS = "MINO79WLOO247M1IL051"
NOVO = "549300DAQ1CVT6CXN342"


def records(key: str) -> list[LeiRecord]:
    return parse_lei_records(json.loads((FIXTURES / f"{key}_gleif.json").read_text()))


def record(name: str, *other_names: str, lei: str = "X" * 18 + "00") -> LeiRecord:
    return LeiRecord(lei, name, other_names, None, None, None)


class FakeStore:
    def __init__(self, instrument):
        self.instrument = instrument
        self.remembered: list[LeiResolution] = []

    async def load(self, asset_id):
        return self.instrument if self.instrument and self.instrument.asset_id == asset_id else None

    async def remember(self, asset_id, resolution, now):
        self.remembered.append(resolution)


def instrument(name="Airbus SE", isin="NL0000235190", quote_type="EQUITY", **stored):
    return LeiInstrument(1, name, quote_type, isin, **stored)


def resolver(lei_instrument, answer=None, **options):
    source = AsyncMock(return_value=records("airbus") if answer is None else answer)
    store = FakeStore(lei_instrument)
    return LeiResolver(store, source, now=lambda: NOW, **options), store, source


class TestNames:
    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("Airbus SE", "AIRBUS SE"),
            ("Novo Nordisk B", "NOVO NORDISK A/S"),
            ("Pernod Ricard SA", "PERNOD RICARD"),
            ("Société Générale S.A.", "SOCIETE GENERALE"),
            ("L'Oréal S.A.", "L'OREAL"),
            ("Enel S.p.A.", "ENEL - SPA"),
            ("Volvo AB (publ)", "AB Volvo"),
            ("LVMH Moët Hennessy - Louis Vuitton SE", "LVMH MOET HENNESSY-LOUIS VUITTON"),
            # Names of the first reading of a real catalogue.
            ("Octava SA", "OCTAVA SPÓŁKA AKCYJNA"),
            ("Broedrene A & O Johansen A/S Class B", "BRØDRENE A. & O. JOHANSEN A/S"),
            ("Broendbyernes IF Fodbold A/S", "BRØNDBYERNES I.F. FODBOLD A/S"),
            ("HF Company SA", "H.F.COMPANY"),
            ("Klea Holding SA", "KLEAHOLDING"),
            ("Groupe Entreprendre SA", "ENTREPRENDRE"),
            ("A G Barr PLC", "A.G. BARR P.L.C."),
            ("Dont Nod Entertainment SA", "DON'T NOD ENTERTAINMENT"),
            ("Mexedia SpA SB", "Mexedia Societa' Per Azioni - Societa' Benefit"),
            ("B.P. Marsh & Partners PLC", "B.P.MARSH & PARTNERS PLC"),
            ("Tallink Grupp", "AS TALLINK GRUPP"),
            (
                "Air Liquide",
                "L'AIR LIQUIDE SOCIETE ANONYME POUR L'ETUDE ET L'EXPLOITATION DES PROCEDES "
                "GEORGES CLAUDE",
            ),
        ],
    )
    def test_the_same_company(self, ours, theirs):
        assert same_entity(ours, record(theirs))

    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("AAR Corp", "AIRBUS SE"),
            ("Total", "TotalEnergies SE"),
            ("Air France-KLM", "L'AIR LIQUIDE SOCIETE ANONYME"),
            ("SA", "AIRBUS SE"),
            (None, "AIRBUS SE"),
            ("Buccaneer Energy PLC", "NOSTRA TERRA OIL AND GAS COMPANY PLC"),
            ("Bank AG", "AG BARR"),
            ("Klea", "KLEAHOLDING"),
        ],
    )
    def test_another_company(self, ours, theirs):
        assert not same_entity(ours, record(theirs))

    def test_a_name_made_of_legal_forms_only_is_no_name(self):
        assert not same_entity("Airbus SE", record("S.A.", "N.V."))

    def test_one_of_the_other_names(self):
        assert same_entity("Total", record("TotalEnergies SE", "TOTAL S.A."))


class TestResolution:
    @pytest.mark.parametrize(
        ("key", "name", "isin", "lei", "country"),
        [
            ("airbus", "Airbus SE", "NL0000235190", AIRBUS, "NL"),
            ("novo", "Novo Nordisk A/S", "DK0062498333", NOVO, "DK"),
            ("total", "TotalEnergies SE", "FR0000120271", "529900S21EQ1BO4ESM68", "FR"),
            ("bnp", "BNP Paribas SA", "FR0000131104", "R0MUWSFPU8MPRO8K5P83", "FR"),
            ("pernod", "Pernod Ricard SA", "FR0000120693", "52990097YFPX9J0H5D87", "FR"),
            ("sap", "SAP SE", "DE0007164600", "529900D6BF99LW9R2E68", "DE"),
        ],
    )
    async def test_the_lei_of_the_isin_is_found_and_remembered(self, key, name, isin, lei, country):
        lei_resolver, store, source = resolver(instrument(name, isin), records(key))
        resolution = await lei_resolver.resolve(1)
        assert (resolution.lei, resolution.origin, resolution.country) == (
            lei,
            FOUND_BY_ISIN,
            country,
        )
        source.assert_awaited_once_with(isin)
        assert store.remembered == [resolution]

    async def test_the_isin_is_asked_in_capitals(self):
        lei_resolver, _, source = resolver(instrument(isin=" nl0000235190 "))
        assert (await lei_resolver.resolve(1)).lei == AIRBUS
        source.assert_awaited_once_with("NL0000235190")

    async def test_an_entity_of_another_name_is_refused(self):
        lei_resolver, store, _ = resolver(instrument("AAR Corp"))
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and resolution.origin == NOT_FOUND
        assert "AIRBUS SE" in resolution.reason
        assert store.remembered == [resolution]

    async def test_an_isin_unknown_to_gleif(self):
        lei_resolver, store, _ = resolver(instrument(), [])
        resolution = await lei_resolver.resolve(1)
        assert resolution.reason == "GLEIF knows no entity for NL0000235190"
        assert store.remembered[0].origin == NOT_FOUND

    async def test_several_entities_named_alike(self):
        twins = [record("AIRBUS SE", lei="A" * 18 + "00"), record("Airbus SE", lei="B" * 18 + "00")]
        lei_resolver, _, _ = resolver(instrument(), twins)
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and "Several entities" in resolution.reason

    async def test_one_entity_given_twice(self):
        lei_resolver, _, _ = resolver(instrument(), records("airbus") * 2)
        assert (await lei_resolver.resolve(1)).lei == AIRBUS

    async def test_an_instrument_without_name(self):
        lei_resolver, store, _ = resolver(instrument(name=None))
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and "no name" in resolution.reason
        assert store.remembered[0].origin == NOT_FOUND

    @pytest.mark.parametrize("isin", [None, "", "AIR.PA", "NL000023519"])
    async def test_without_isin_gleif_is_not_asked(self, isin):
        lei_resolver, store, source = resolver(instrument(isin=isin))
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and "no ISIN" in resolution.reason
        source.assert_not_awaited()
        assert store.remembered == []

    @pytest.mark.parametrize(
        "isin", ["JP3633400001", "US0378331005", "CA92707Y1088", "KYG226921008", "CH0012005267"]
    )
    async def test_an_issuer_outside_esef_is_not_asked(self, isin):
        lei_resolver, store, source = resolver(instrument(isin=isin))
        resolution = await lei_resolver.resolve(1)
        assert resolution.reason == (
            f"An issuer of {isin[:2]} files no ESEF report: GLEIF is not asked about it"
        )
        source.assert_not_awaited()
        assert store.remembered == []

    @pytest.mark.parametrize("isin", ["GB00B6XZKY75", "NO0010096985", "PLNFI0800016"])
    async def test_the_united_kingdom_and_the_european_economic_area_are_asked(self, isin):
        lei_resolver, _, source = resolver(instrument(isin=isin), [])
        await lei_resolver.resolve(1)
        source.assert_awaited_once_with(isin)

    async def test_a_lei_set_by_hand_outside_esef_is_kept(self):
        by_hand = instrument(
            "Novartis AG",
            isin="CH0012005267",
            stored_lei="5493007HSCDAFOOUWG35",
            stored_source=SET_BY_HAND,
            stored_active=True,
        )
        lei_resolver, _, source = resolver(by_hand)
        assert (await lei_resolver.resolve(1)).lei == "5493007HSCDAFOOUWG35"
        source.assert_not_awaited()

    @pytest.mark.parametrize("quote_type", ["ETF", "MUTUALFUND", "INDEX", " etf "])
    async def test_a_fund_files_no_annual_report(self, quote_type):
        lei_resolver, _, source = resolver(instrument(quote_type=quote_type))
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and "files no annual report" in resolution.reason
        source.assert_not_awaited()

    async def test_an_unknown_instrument(self):
        lei_resolver, _, source = resolver(None)
        assert (await lei_resolver.resolve(1)).reason == "Instrument 1 does not exist"
        source.assert_not_awaited()

    async def test_gleif_not_answering_is_not_remembered(self):
        lei_resolver, store, source = resolver(instrument())
        source.return_value = None
        resolution = await lei_resolver.resolve(1)
        assert (resolution.lei, resolution.reason) == (None, "GLEIF did not answer")
        assert store.remembered == []

    async def test_an_answer_of_gleif_not_read_is_not_remembered(self):
        lei_resolver, store, source = resolver(instrument())
        source.side_effect = EsefError("GLEIF: not a JSON:API answer")
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and "could not be read" in resolution.reason
        assert store.remembered == []


class TestWhatIsStored:
    VERIFIED = {
        "stored_lei": AIRBUS,
        "stored_source": FOUND_BY_ISIN,
        "stored_active": True,
        "stored_verified": True,
        "stored_at": NOW - timedelta(days=400),
    }

    async def test_a_verified_lei_is_reused_without_asking(self):
        lei_resolver, store, source = resolver(instrument(**self.VERIFIED))
        resolution = await lei_resolver.resolve(1)
        assert (resolution.lei, resolution.origin) == (AIRBUS, FOUND_BY_ISIN)
        source.assert_not_awaited()
        assert store.remembered == []

    async def test_refresh_asks_gleif_again(self):
        lei_resolver, store, source = resolver(instrument(**self.VERIFIED))
        assert (await lei_resolver.resolve(1, refresh=True)).lei == AIRBUS
        source.assert_awaited_once()
        assert len(store.remembered) == 1

    async def test_a_lei_no_longer_confirmed_is_switched_off(self):
        lei_resolver, store, _ = resolver(instrument(**self.VERIFIED), [])
        resolution = await lei_resolver.resolve(1, refresh=True)
        assert resolution.lei is None
        assert store.remembered == [resolution]

    async def test_a_lei_set_by_hand_is_trusted_even_on_refresh(self):
        by_hand = instrument(
            "Total",
            stored_lei="529900S21EQ1BO4ESM68",
            stored_source=SET_BY_HAND,
            stored_active=True,
        )
        lei_resolver, _, source = resolver(by_hand)
        for refresh in (False, True):
            resolution = await lei_resolver.resolve(1, refresh=refresh)
            assert (resolution.lei, resolution.origin) == ("529900S21EQ1BO4ESM68", SET_BY_HAND)
        source.assert_not_awaited()

    async def test_a_lei_switched_off_is_not_used(self):
        off = replace(instrument(**self.VERIFIED), stored_active=False)
        lei_resolver, _, source = resolver(off)
        assert (await lei_resolver.resolve(1)).lei == AIRBUS
        source.assert_awaited_once()

    async def test_a_lei_not_verified_is_checked_again(self):
        unverified = replace(instrument(**self.VERIFIED), stored_verified=False)
        lei_resolver, _, source = resolver(unverified)
        assert (await lei_resolver.resolve(1)).lei == AIRBUS
        source.assert_awaited_once()

    async def test_a_recent_failure_is_not_asked_again(self):
        failed = instrument(stored_source=NOT_FOUND, stored_at=NOW - timedelta(days=3))
        lei_resolver, store, source = resolver(failed)
        resolution = await lei_resolver.resolve(1)
        assert resolution.lei is None and resolution.origin == NOT_FOUND
        assert "checked on 2026-10-16 08:00 UTC" in resolution.reason
        source.assert_not_awaited()
        assert store.remembered == []

    async def test_an_old_failure_is_asked_again(self):
        failed = instrument(stored_source=NOT_FOUND, stored_at=NOW - RETRY_AFTER - timedelta(1))
        lei_resolver, _, source = resolver(failed)
        assert (await lei_resolver.resolve(1)).lei == AIRBUS
        source.assert_awaited_once()

    async def test_a_failure_stored_without_time_zone(self):
        naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
        lei_resolver, _, source = resolver(instrument(stored_source=NOT_FOUND, stored_at=naive))
        assert (await lei_resolver.resolve(1)).lei is None
        source.assert_not_awaited()

    async def test_refresh_asks_again_after_a_recent_failure(self):
        failed = instrument(stored_source=NOT_FOUND, stored_at=NOW - timedelta(days=3))
        lei_resolver, _, source = resolver(failed)
        assert (await lei_resolver.resolve(1, refresh=True)).lei == AIRBUS
        source.assert_awaited_once()
