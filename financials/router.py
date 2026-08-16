from typing import List

from fastapi import APIRouter, HTTPException, Request

from financials.models import StandardFinancials, StockSummary
from financials.service import FinancialsAggregator

router = APIRouter(prefix="/stocks", tags=["Financials"])


@router.get("", response_model=List[StockSummary])
async def read_market_overview(request: Request):
    service: FinancialsAggregator = request.app.state.financials_service
    return await service.get_market_overview()


@router.get("/{ticker}/financials", response_model=StandardFinancials)
async def read_financials(ticker: str, request: Request):
    service: FinancialsAggregator = request.app.state.financials_service
    data = await service.get_financials(ticker)
    if not data:
        raise HTTPException(status_code=404, detail="Financials not found")
    return data
