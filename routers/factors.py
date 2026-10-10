"""HTTP routes of the factor returns (Kenneth French Data Library)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from factors.french_library import DATASETS
from factors.store import FactorLibrary, FactorLoad
from schemas.factors import (
    FactorDatasetInfo,
    FactorDatasetsResponse,
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
