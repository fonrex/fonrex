"""Risk metrics of a price series: pure calculations, without input or output.

The closes are the daily ``adj_close`` of one listing (dividends included), read by
the caller through ``database/price_series.py``. Every figure is a ratio: a 12 %
volatility is ``0.12``. Conventions:

* **Returns** are simple returns between consecutive closes, ``p_t / p_t-1 - 1``.
* **Annualization** uses ``periods_per_year`` (252 for daily returns): the
  annualized return is geometric, ``prod(1 + r) ** (periods_per_year / n) - 1``,
  and the volatility is the sample standard deviation (``ddof=1``) times
  ``sqrt(periods_per_year)``.
* **Losses are negative.** The maximum drawdown, the value at risk (VaR) and the
  expected shortfall (CVaR) are returns: a 95 % daily VaR of ``-0.021`` means that
  5 % of the daily returns are at or below -2.1 %.
* **Historical VaR** is the ``1 - confidence`` quantile of the returns, with linear
  interpolation between observations (numpy's default); the historical CVaR is the
  mean of the returns at or below it.
* **Gaussian VaR and CVaR** assume normal returns with the sample mean and standard
  deviation: ``mu + sigma * z`` and ``mu - sigma * phi(z) / (1 - confidence)``, with
  ``z`` the ``1 - confidence`` quantile of the standard normal distribution.
* **Calmar** is the annualized return divided by the absolute maximum drawdown.
* **Sharpe and Sortino** use the risk-free rate of the day of each return, an
  annual ratio divided by ``periods_per_year``: the excess return of a day is
  ``r_t - rf_t / periods_per_year``. Sharpe is the mean excess return over its
  sample standard deviation, times ``sqrt(periods_per_year)``. Sortino divides the
  annualized mean excess return by the annualized downside deviation below the
  risk-free rate, ``sqrt(mean(min(excess, 0) ** 2))`` over every return.

A figure that cannot be measured (no variation, no drawdown, no risk-free rate) is
``None``, never ``NaN``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist

import numpy as np
import numpy.typing as npt

TRADING_DAYS_PER_YEAR = 252
DEFAULT_CONFIDENCE = 0.95
# Fewer returns than this do not describe the risk of a listing.
MIN_RETURNS = 60

FloatArray = npt.NDArray[np.float64]


class RiskMetricsError(Exception):
    """The series cannot be measured: too short, or a close that is not positive."""


@dataclass(frozen=True)
class Drawdown:
    """The deepest fall from a peak close, and the first close back at that peak."""

    value: float
    peak: date
    trough: date
    recovery: date | None


@dataclass(frozen=True)
class RiskMetrics:
    start: date
    end: date
    returns: int
    confidence: float
    annualized_return: float
    annualized_volatility: float | None
    max_drawdown: Drawdown | None
    calmar_ratio: float | None
    historical_var: float
    historical_cvar: float
    gaussian_var: float | None
    gaussian_cvar: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    mean_risk_free_rate: float | None


# ── Returns ──────────────────────────────────────────────────────────────────


def simple_returns(closes: Mapping[date, float]) -> list[tuple[date, float]]:
    """Returns between consecutive closes, dated by the later close."""
    days = sorted(closes)
    for day in days:
        price = closes[day]
        if not math.isfinite(price) or price <= 0:
            raise RiskMetricsError(f"the close of {day.isoformat()} is not a positive number")
    return [
        (later, closes[later] / closes[earlier] - 1)
        for earlier, later in zip(days, days[1:], strict=False)
    ]


def _array(values: Sequence[float]) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


def annualized_return(returns: Sequence[float], periods_per_year: int) -> float:
    """Geometric mean return, compounded over a year."""
    growth = float(np.prod(1 + _array(returns)))
    return float(growth ** (periods_per_year / len(returns)) - 1)


def annualized_volatility(returns: Sequence[float], periods_per_year: int) -> float | None:
    """Sample standard deviation of the returns, scaled to a year."""
    if len(returns) < 2:
        return None
    deviation = float(np.std(_array(returns), ddof=1))
    return deviation * math.sqrt(periods_per_year) if deviation > 0 else None


# ── Drawdown ─────────────────────────────────────────────────────────────────


def max_drawdown(closes: Mapping[date, float]) -> Drawdown | None:
    """The deepest fall of the closes below their running peak; ``None`` if they never fall."""
    days = sorted(closes)
    deepest: tuple[float, date, date] | None = None
    peak_day = days[0]
    for day in days[1:]:
        if closes[day] >= closes[peak_day]:
            peak_day = day
            continue
        fall = closes[day] / closes[peak_day] - 1
        if deepest is None or fall < deepest[0]:
            deepest = (fall, peak_day, day)
    if deepest is None:
        return None
    value, peak, trough = deepest
    recovery = next(
        (day for day in days if day > trough and closes[day] >= closes[peak]),
        None,
    )
    return Drawdown(value=value, peak=peak, trough=trough, recovery=recovery)


# ── Value at risk ────────────────────────────────────────────────────────────


def _check_confidence(confidence: float) -> None:
    if not 0 < confidence < 1:
        raise RiskMetricsError(f"the confidence must be between 0 and 1, not {confidence}")


def historical_var(returns: Sequence[float], confidence: float) -> float:
    """The ``1 - confidence`` quantile of the returns (linear interpolation)."""
    _check_confidence(confidence)
    return float(np.quantile(_array(returns), 1 - confidence))


def historical_cvar(returns: Sequence[float], confidence: float) -> float:
    """Mean of the returns at or below the historical VaR."""
    values = _array(returns)
    threshold = historical_var(returns, confidence)
    return float(values[values <= threshold].mean())


def gaussian_var(returns: Sequence[float], confidence: float) -> float | None:
    """VaR of a normal distribution with the sample mean and standard deviation."""
    _check_confidence(confidence)
    if len(returns) < 2:
        return None
    values = _array(returns)
    z = NormalDist().inv_cdf(1 - confidence)
    return float(values.mean() + float(np.std(values, ddof=1)) * z)


def gaussian_cvar(returns: Sequence[float], confidence: float) -> float | None:
    """Expected shortfall of a normal distribution with the sample mean and deviation."""
    _check_confidence(confidence)
    if len(returns) < 2:
        return None
    values = _array(returns)
    z = NormalDist().inv_cdf(1 - confidence)
    tail = NormalDist().pdf(z) / (1 - confidence)
    return float(values.mean() - float(np.std(values, ddof=1)) * tail)


# ── Risk-adjusted performance ────────────────────────────────────────────────


def excess_returns(
    returns: Sequence[float], risk_free_rates: Sequence[float], periods_per_year: int
) -> FloatArray:
    """Returns minus the risk-free rate of their day, an annual ratio spread over the year."""
    if len(returns) != len(risk_free_rates):
        raise RiskMetricsError("every return needs the risk-free rate of its day")
    return _array(returns) - _array(risk_free_rates) / periods_per_year


def sharpe_ratio(excess: Sequence[float], periods_per_year: int) -> float | None:
    """Mean excess return over its sample standard deviation, annualized."""
    if len(excess) < 2:
        return None
    values = _array(excess)
    deviation = float(np.std(values, ddof=1))
    if deviation == 0:
        return None
    return float(values.mean()) / deviation * math.sqrt(periods_per_year)


def sortino_ratio(excess: Sequence[float], periods_per_year: int) -> float | None:
    """Annualized mean excess return over the annualized downside deviation."""
    if not len(excess):
        return None
    values = _array(excess)
    downside = math.sqrt(float(np.mean(np.minimum(values, 0.0) ** 2)))
    if downside == 0:
        return None
    return float(values.mean()) * periods_per_year / (downside * math.sqrt(periods_per_year))


# ── All the metrics of a series ──────────────────────────────────────────────


def measure(
    closes: Mapping[date, float],
    risk_free_rates: Mapping[date, float] | None = None,
    confidence: float = DEFAULT_CONFIDENCE,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> RiskMetrics:
    """Every metric of the closes of a listing.

    ``risk_free_rates`` gives the annual risk-free rate, as a ratio, of the day of each
    return; without it, Sharpe and Sortino are ``None``. A rate missing for a day of
    the series is an error: the caller decides how a rate is carried over.
    """
    _check_confidence(confidence)
    dated = simple_returns(closes)
    if len(dated) < MIN_RETURNS:
        raise RiskMetricsError(
            f"{len(dated)} returns: at least {MIN_RETURNS} are needed to measure the risk"
        )
    returns = [value for _day, value in dated]

    sharpe = sortino = mean_rate = None
    if risk_free_rates is not None:
        missing = [day for day, _value in dated if day not in risk_free_rates]
        if missing:
            raise RiskMetricsError(f"no risk-free rate for {missing[0].isoformat()}")
        rates = [risk_free_rates[day] for day, _value in dated]
        excess = excess_returns(returns, rates, periods_per_year)
        sharpe = sharpe_ratio(excess.tolist(), periods_per_year)
        sortino = sortino_ratio(excess.tolist(), periods_per_year)
        mean_rate = float(np.mean(_array(rates)))

    yearly = annualized_return(returns, periods_per_year)
    drawdown = max_drawdown(closes)
    return RiskMetrics(
        start=min(closes),
        end=max(closes),
        returns=len(returns),
        confidence=confidence,
        annualized_return=yearly,
        annualized_volatility=annualized_volatility(returns, periods_per_year),
        max_drawdown=drawdown,
        calmar_ratio=yearly / abs(drawdown.value) if drawdown is not None else None,
        historical_var=historical_var(returns, confidence),
        historical_cvar=historical_cvar(returns, confidence),
        gaussian_var=gaussian_var(returns, confidence),
        gaussian_cvar=gaussian_cvar(returns, confidence),
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        mean_risk_free_rate=mean_rate,
    )
