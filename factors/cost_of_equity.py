"""Cost of equity from the Fama/French factors, for the valuation.

    Ke = Rf + sum_k beta_k * premium_k

``beta_k`` is the exposure of the listing to factor ``k`` (``factors/exposure.py``,
monthly, 60 months by default). ``premium_k`` is the long-run premium of the
factor: the arithmetic mean of its monthly returns over the whole history stored
for the region (US since 1926 or 1963, Europe since 1990), times twelve.

``Rf`` stays the risk-free rate of the currency of the cash flows chosen by the
valuation. The premia of the library are excess returns in US dollars over the
US T-bill: for a valuation in another currency they are an approximation, said
in the warnings. Factor premia are measured with a large error and change with
the period: the CAPM stays the default, this cost of equity is asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from factors.exposure import MODELS, ExposureError, FactorExposure

_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class FactorCostOfEquity:
    """The factor part of the cost of equity: Σ beta × premium, and how it was measured."""

    model: str
    region: str
    betas: dict[str, Decimal]
    premia: dict[str, Decimal]  # annual, as ratios
    start: date
    end: date
    periods: int
    r_squared: float
    warnings: list[str] = field(default_factory=list)

    @property
    def premium(self) -> Decimal:
        return sum((self.betas[name] * self.premia[name] for name in self.betas), Decimal(0))

    @property
    def market_beta(self) -> Decimal:
        return self.betas["MKT_RF"]


async def factor_cost_of_equity(
    exposure: FactorExposure, ticker: str, model: str
) -> FactorCostOfEquity:
    """Betas of the listing and long-run premia of the factors of its region.

    Raises ``ValueError`` when the exposure cannot be measured, as the
    valuation does for a missing input.
    """
    try:
        result = await exposure.measure(ticker, model=model, frequency="monthly")
    except ExposureError as error:
        raise ValueError(f"Factor cost of equity unavailable for {ticker}: {error}") from error

    names = MODELS[model][1]
    history: dict[str, list[Decimal]] = {name: [] for name in names}
    for dataset in result.datasets:
        for values in (await exposure.library.series(dataset, "monthly")).values():
            for name in names:
                if name in values:
                    history[name].append(values[name])
    premia = {
        name: (sum(values, Decimal(0)) / len(values) * 12).quantize(_PRECISION)
        for name, values in history.items()
        if values
    }
    missing = [name for name in names if name not in premia]
    if missing:
        raise ValueError(f"No stored history of the factors {', '.join(missing)}")

    regression = result.regression
    warnings = list(result.warnings)
    if result.converted_from:
        warnings.append(
            f"Factor premia are US-dollar excess returns over the US T-bill; the cash "
            f"flows of {ticker} are discounted in their own currency: an approximation."
        )
    return FactorCostOfEquity(
        model=model,
        region=result.region,
        betas={
            name: Decimal(str(beta.value)).quantize(_PRECISION)
            for name, beta in regression.betas.items()
        },
        premia=premia,
        start=result.start,
        end=result.end,
        periods=regression.periods,
        r_squared=regression.r_squared,
        warnings=warnings,
    )
