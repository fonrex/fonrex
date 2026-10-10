"""HTTP routes of the factor returns (Kenneth French Data Library)."""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from factors.exposure import PERIODS_PER_YEAR, ExposureError, FactorExposure
from factors.french_library import DATASETS
from factors.store import FactorLibrary, FactorLoad
from schemas.factors import (
    FactorDatasetInfo,
    FactorDatasetsResponse,
    FactorExposureResponse,
    FactorLoadInfo,
    FactorRefreshResponse,
    FactorSeriesResponse,
)

router = APIRouter(prefix="/factors", tags=["Factors"])

FrequencyParameter = Literal["monthly", "daily"]


def get_factor_library(request: Request) -> FactorLibrary:
    library = getattr(request.app.state, "factor_library", None)
    if library is None:
        raise HTTPException(status_code=503, detail="Factor returns unavailable (no database)")
    return library


def get_factor_exposure(request: Request) -> FactorExposure:
    exposure = getattr(request.app.state, "factor_exposure", None)
    if exposure is None:
        raise HTTPException(status_code=503, detail="Factor exposure unavailable (no database)")
    return exposure


def _known_dataset(dataset: str) -> str:
    name = dataset.strip().lower()
    if name not in DATASETS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown factor dataset {dataset!r}: {', '.join(DATASETS)}",
        )
    return name


def _info(load: FactorLoad) -> FactorLoadInfo:
    return FactorLoadInfo(**asdict(load))


def _missing(dataset: str, frequency: str) -> FactorLoadInfo:
    return FactorLoadInfo(dataset=dataset, frequency=frequency, status="missing")


@router.get("", response_model=FactorDatasetsResponse)
async def list_factor_datasets(library: FactorLibrary = Depends(get_factor_library)):
    """The datasets of the library, and what the database holds of each file."""
    datasets = []
    for name, dataset in DATASETS.items():
        loads = []
        for frequency in ("monthly", "daily"):
            load = await library.status(name, frequency)
            loads.append(_info(load) if load else _missing(name, frequency))
        datasets.append(
            FactorDatasetInfo(
                dataset=name,
                region=dataset.region,
                label=dataset.label,
                factors=list(dataset.factors),
                loads=loads,
            )
        )
    return FactorDatasetsResponse(datasets=datasets)


@router.post("/refresh", response_model=FactorRefreshResponse)
async def refresh_factor_datasets(
    dataset: Optional[List[str]] = Query(
        None, description="Datasets to download (every dataset when left out)"
    ),
    frequency: List[FrequencyParameter] = Query(["monthly"]),
    force: bool = Query(False, description="Download even a file read recently"),
    library: FactorLibrary = Depends(get_factor_library),
):
    """Download files of the library into the database.

    A file read less than ``FACTORS_REFRESH_DAYS`` days ago is skipped unless
    ``force`` is set; a failed download keeps what was stored and says why.
    """
    names = [_known_dataset(name) for name in dataset] if dataset else list(DATASETS)
    loads = [
        _info(await library.refresh(name, period, force=force))
        for name in dict.fromkeys(names)
        for period in dict.fromkeys(frequency)
    ]
    return FactorRefreshResponse(loads=loads)


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


@router.get("/exposure/{ticker}", response_model=FactorExposureResponse)
async def get_factor_exposure_of_listing(
    ticker: str,
    model: Literal["ff3", "ff5", "carhart"] = "ff3",
    frequency: FrequencyParameter = "monthly",
    window: Optional[int] = Query(
        None, ge=24, le=10000, description="Periods regressed (60 months or 252 days by default)"
    ),
    end: Optional[date] = Query(None, description="Last period of the regression"),
    region: Optional[Literal["us", "europe", "developed"]] = Query(
        None, description="Factors of this region (from the currency of the listing by default)"
    ),
    currency: Optional[str] = Query(None, description="Currency of the listing to read"),
    exchange: Optional[str] = Query(None, description="Exchange of the listing to read"),
    isin: Optional[str] = Query(None, description="Instrument of the listing to read"),
    exposure: FactorExposure = Depends(get_factor_exposure),
):
    """Exposure of a listing to the Fama/French factors (betas, alpha, R²).

    The excess returns of the listing, in US dollars, are regressed on the
    factors of its region: ``ff3`` (market, size, value), ``ff5`` (plus
    profitability and investment) or ``carhart`` (``ff3`` plus momentum).
    """
    try:
        result = await exposure.measure(
            ticker,
            model=model,
            frequency=frequency,
            window=window,
            end=end,
            region=region,
            currency=currency,
            exchange=exchange,
            isin=isin,
        )
    except ExposureError as error:
        raise HTTPException(status_code=error.status, detail=str(error)) from error

    regression = result.regression
    per_year = PERIODS_PER_YEAR[result.frequency]

    def coefficient(item):
        return {"value": item.value, "std_error": item.std_error, "t_stat": _finite(item.t_stat)}

    return FactorExposureResponse(
        ticker=result.ticker,
        listing=result.listing,
        model=result.model,
        region=result.region,
        frequency=result.frequency,
        datasets=result.datasets,
        converted_from=result.converted_from,
        start=result.start,
        end=result.end,
        periods=regression.periods,
        alpha={**coefficient(regression.alpha), "annualized": regression.alpha.value * per_year},
        betas={name: coefficient(beta) for name, beta in regression.betas.items()},
        r_squared=regression.r_squared,
        adj_r_squared=regression.adj_r_squared,
        residual_volatility=regression.residual_std * math.sqrt(per_year),
        warnings=result.warnings,
    )


@router.get("/{dataset}", response_model=FactorSeriesResponse)
async def get_factor_series(
    dataset: str,
    frequency: FrequencyParameter = "monthly",
    start: Optional[date] = Query(None, description="First period included"),
    end: Optional[date] = Query(None, description="Last period included"),
    library: FactorLibrary = Depends(get_factor_library),
):
    """Factor returns of one dataset, as ratios, in US dollars.

    A file never downloaded, or downloaded more than ``FACTORS_REFRESH_DAYS``
    days ago, is downloaded first; when that fails, the stored values are
    answered and ``load.status`` is ``failed``.
    """
    name = _known_dataset(dataset)
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="start is after end")

    load = await library.status(name, frequency)
    if load is None or load.status == "stale":
        load = await library.refresh(name, frequency)
    if not load.periods:
        raise HTTPException(
            status_code=503,
            detail=f"No factor returns stored for {name} ({frequency}): {load.reason}",
        )

    series = await library.series(name, frequency, start, end)
    definition = DATASETS[name]
    return FactorSeriesResponse(
        dataset=name,
        frequency=frequency,
        region=definition.region,
        label=definition.label,
        factors=list(definition.factors),
        load=_info(load),
        data=[
            {"date": period, **{factor: float(value) for factor, value in values.items()}}
            for period, values in series.items()
        ],
    )
