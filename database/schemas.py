from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PriceEOD(BaseModel):
    timestamp: datetime
    asset_id: int
    asset_listing_id: Optional[int] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    adj_close: Optional[float] = None
    volume: Optional[int] = None

    class Config:
        from_attributes = True
