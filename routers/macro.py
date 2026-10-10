"""HTTP routes for macro-economic data: rates of FRED (USD) and of the ECB (EUR)."""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from schemas.macro import MacroRatesResponse

router = APIRouter(prefix="/macro", tags=["Macro"])

# Currency -> service of app.state that publishes its rates.
MACRO_SOURCES = {"USD": "fred_service", "EUR": "ecb_service"}


def _service(request: Request, currency: str):
    return getattr(request.app.state, MACRO_SOURCES[currency], None)


@router.get("/rates", response_model=MacroRatesResponse)
async def get_macro_rates(
    request: Request,
    currency: Optional[str] = Query(
        None,
        pattern="^([A-Za-z]{3})?$",
        description="USD (FRED) or EUR (ECB); every currency when left out.",
    ),
) -> MacroRatesResponse:
    """Current macro-economic rates: the 10-year risk-free rate of each currency, and more.

    Without ``currency`` the answer holds the series of every source, and
    ``risk_free_rate`` is the US one, as before the ECB was a source.
    """
    if not currency:  # left out, or empty (the "both" choice of the OpenBB widget)
        currency = None
        currencies = list(MACRO_SOURCES)
    else:
        currency = currency.upper()
        if currency not in MACRO_SOURCES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No source of macro rates for {currency}: "
                    f"{', '.join(sorted(MACRO_SOURCES))} are known."
                ),
            )
        currencies = [currency]

    services = {code: _service(request, code) for code in currencies}
    available = {code: service for code, service in services.items() if service is not None}
    if not available:
        names = ", ".join(MACRO_SOURCES[code] for code in currencies)
        raise HTTPException(
            status_code=503, detail=f"Macro rates unavailable ({names} not started)"
        )

    results = await asyncio.gather(*(service.get_rates() for service in available.values()))
    answers = {
        code: MacroRatesResponse.model_validate(result)
        for code, result in zip(available, results, strict=True)
    }
    first = currencies[0]
    return MacroRatesResponse(
        currency=currency,
        risk_free_rate=answers[first].risk_free_rate if first in answers else None,
        rates=[rate for code in currencies if code in answers for rate in answers[code].rates],
    )
