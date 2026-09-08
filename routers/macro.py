"""HTTP routes for macro-economic data."""

from fastapi import APIRouter, Depends, HTTPException, Request

from schemas.macro import MacroRatesResponse

router = APIRouter(prefix="/macro", tags=["Macro"])


def get_fred_service(request: Request):
    service = getattr(request.app.state, "fred_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="FREDService indisponible")
    return service


@router.get("/rates", response_model=MacroRatesResponse)
async def get_macro_rates(
    service=Depends(get_fred_service)
):
    """Retrieve current macro-economic rates (like the risk-free rate)."""
    return await service.get_current_rates()
