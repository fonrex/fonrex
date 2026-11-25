"""Risk metrics of a price series (``risk/metrics.py``).

Each metric is checked on a few closes whose answer is worked out by hand, then
``measure`` is checked to put them together on a series long enough to be measured.
"""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from risk.metrics import (
    MIN_RETURNS,
    Drawdown,
    RiskMetricsError,
    annualized_return,
    annualized_volatility,
    excess_returns,
    gaussian_cvar,
    gaussian_var,
    historical_cvar,
    historical_var,
    max_drawdown,
    measure,
    sharpe_ratio,
    simple_returns,
    sortino_ratio,
)

D = [date(2024, 1, 1) + timedelta(days=i) for i in range(5)]


def _closes(*prices: float) -> dict[date, float]:
    return dict(zip(D, prices, strict=False))


class TestReturns:
    def test_simple_returns_between_consecutive_closes(self):
        returns = simple_returns(_closes(100.0, 110.0, 99.0, 108.9))
        assert [day for day, _ in returns] == D[1:4]
        assert [value for _, value in returns] == pytest.approx([0.1, -0.1, 0.1])

    def test_closes_are_read_in_date_order(self):
        closes = {D[2]: 99.0, D[0]: 100.0, D[1]: 110.0}
        assert [value for _, value in simple_returns(closes)] == pytest.approx([0.1, -0.1])

    @pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf])
    def test_a_close_that_is_not_a_positive_number_is_refused(self, price):
        with pytest.raises(RiskMetricsError, match="not a positive number"):
            simple_returns(_closes(100.0, price))

    def test_annualized_return_compounds_the_returns(self):
        # 1.1 * 0.9 * 1.1 = 1.089 over 3 periods; 3 periods a year: one year.
        assert annualized_return([0.1, -0.1, 0.1], 3) == pytest.approx(0.089)
        # The same growth over half a year compounds twice.
        assert annualized_return([0.1, -0.1, 0.1], 6) == pytest.approx(1.089**2 - 1)

    def test_volatility_is_the_sample_deviation_scaled_to_a_year(self):
        # Mean 1/30; squared deviations sum to 0.08/3, over n - 1 = 2.
        expected = math.sqrt(0.08 / 3 / 2) * math.sqrt(252)
        assert annualized_volatility([0.1, -0.1, 0.1], 252) == pytest.approx(expected)

    def test_volatility_without_variation_is_none(self):
        assert annualized_volatility([0.01, 0.01, 0.01], 252) is None
        assert annualized_volatility([0.01], 252) is None


class TestDrawdown:
    def test_deepest_fall_from_the_running_peak(self):
        drawdown = max_drawdown(_closes(100.0, 120.0, 90.0, 110.0, 125.0))
        assert drawdown == Drawdown(value=pytest.approx(-0.25), peak=D[1], trough=D[2], recovery=D[4])

    def test_a_fall_not_recovered_has_no_recovery_date(self):
        drawdown = max_drawdown(_closes(100.0, 110.0, 99.0, 108.9))
        assert drawdown is not None
        assert drawdown.value == pytest.approx(-0.1)
        assert (drawdown.peak, drawdown.trough, drawdown.recovery) == (D[1], D[2], None)

    def test_the_deepest_of_two_falls_is_kept(self):
        drawdown = max_drawdown(_closes(100.0, 95.0, 100.0, 70.0, 80.0))
        assert drawdown is not None
        assert drawdown.value == pytest.approx(-0.3)
        assert (drawdown.peak, drawdown.trough) == (D[2], D[3])

    def test_closes_that_never_fall_have_no_drawdown(self):
        assert max_drawdown(_closes(100.0, 100.0, 101.0)) is None


class TestValueAtRisk:
    RETURNS = [0.04, -0.02, 0.0, -0.04, 0.02]

    def test_historical_var_interpolates_between_returns(self):
        # Sorted: -0.04, -0.02, 0, 0.02, 0.04; the 5 % quantile sits at position 0.2.
        assert historical_var(self.RETURNS, 0.95) == pytest.approx(-0.036)

    def test_historical_cvar_is_the_mean_of_the_returns_beyond_the_var(self):
        assert historical_cvar(self.RETURNS, 0.95) == pytest.approx(-0.04)
        # The 30 % quantile is -0.016: two returns lie at or below it.
        assert historical_cvar(self.RETURNS, 0.70) == pytest.approx(-0.03)

    def test_gaussian_var_and_cvar_of_a_centred_series(self):
        # Mean 0, sample deviation sqrt(0.004 / 4); z(5 %) = -1.6448536, phi(z) = 0.1031356.
        sigma = math.sqrt(0.001)
        assert gaussian_var(self.RETURNS, 0.95) == pytest.approx(-1.6448536 * sigma)
        assert gaussian_cvar(self.RETURNS, 0.95) == pytest.approx(-0.1031356 / 0.05 * sigma)

    def test_gaussian_figures_need_two_returns(self):
        assert gaussian_var([0.01], 0.95) is None
        assert gaussian_cvar([0.01], 0.95) is None

    @pytest.mark.parametrize("confidence", [0.0, 1.0, 95.0, -0.5])
    def test_a_confidence_outside_zero_and_one_is_refused(self, confidence):
        with pytest.raises(RiskMetricsError, match="confidence"):
            historical_var(self.RETURNS, confidence)


class TestRiskAdjustedPerformance:
    def test_excess_returns_spread_the_annual_rate_over_the_periods(self):
        excess = excess_returns([0.02, 0.0], [0.252, 0.0], 252)
        assert excess.tolist() == pytest.approx([0.019, 0.0])

    def test_excess_returns_need_a_rate_for_every_return(self):
        with pytest.raises(RiskMetricsError, match="risk-free rate"):
            excess_returns([0.02, 0.0], [0.0], 252)

    def test_sharpe_ratio(self):
        # Mean 0.01, sample deviation 0.01 * sqrt(2), one period a year.
        assert sharpe_ratio([0.02, 0.0], 1) == pytest.approx(1 / math.sqrt(2))
        assert sharpe_ratio([0.02, 0.0], 4) == pytest.approx(2 / math.sqrt(2))

    def test_sharpe_ratio_without_variation_is_none(self):
        assert sharpe_ratio([0.01, 0.01], 252) is None
        assert sharpe_ratio([0.01], 252) is None

    def test_sortino_ratio_counts_only_the_returns_below_the_rate(self):
        # Mean 0.01; downside deviation sqrt((0 + 0.01 ** 2) / 2).
        assert sortino_ratio([0.03, -0.01], 1) == pytest.approx(0.01 / math.sqrt(0.00005))

    def test_sortino_ratio_without_a_return_below_the_rate_is_none(self):
        assert sortino_ratio([0.01, 0.02], 252) is None
        assert sortino_ratio([], 252) is None


def _series(count: int, seed: int = 3) -> dict[date, float]:
    rng = np.random.default_rng(seed)
    price, closes = 100.0, {}
    for i in range(count):
        closes[date(2020, 1, 1) + timedelta(days=i)] = price
        price *= 1 + rng.normal(0.0004, 0.012)
    return closes


class TestReferenceValues:
    """The figures of FinanceToolkit 2.3.4 on the same series, written down once.

    They were produced outside the project (FinanceToolkit is not a dependency of
    Fonrex) with ``var_model.get_var_historic`` / ``get_var_gaussian``,
    ``cvar_model.get_cvar_historic`` / ``get_cvar_gaussian`` (alpha 0.05),
    ``risk_model.get_max_drawdown``, and ``performance_model.get_sharpe_ratio`` /
    ``get_sortino_ratio`` on the daily excess returns, times sqrt(252). They confirm
    the conventions of the module, not only its arithmetic.
    """

    FINANCETOOLKIT = {
        "historical_var": -0.014849999230896227,
        "historical_cvar": -0.016195733544935895,
        "gaussian_var": -0.01509782390027687,
        "gaussian_cvar": -0.01904771415978616,
        "max_drawdown": -0.037783982046779796,
        "sharpe_ratio": 0.5565550340924803,
        "sortino_ratio": 0.8150747000058538,
    }

    @staticmethod
    def _closes() -> dict[date, float]:
        price, closes = 100.0, {}
        for i in range(300):
            closes[date(2020, 1, 1) + timedelta(days=i)] = price
            price *= 1 + 0.012 * math.sin(i * 0.7) + 0.006 * math.cos(i * 1.3) + 0.0004
        return closes

    def test_the_metrics_match_financetoolkit(self):
        closes = self._closes()
        rates = {day: 0.03 for day, _ in simple_returns(closes)}
        metrics = measure(closes, risk_free_rates=rates)
        assert metrics.max_drawdown is not None
        measured = {
            "historical_var": metrics.historical_var,
            "historical_cvar": metrics.historical_cvar,
            "gaussian_var": metrics.gaussian_var,
            "gaussian_cvar": metrics.gaussian_cvar,
            "max_drawdown": metrics.max_drawdown.value,
            "sharpe_ratio": metrics.sharpe_ratio,
            "sortino_ratio": metrics.sortino_ratio,
        }
        # Within 1e-15 in absolute terms, as the issue asks: the figures differ by a few
        # units of the last place at most (the order of the sums), never by a convention.
        # Not a relative bound: on a figure near zero (a drawdown of -0.038) one unit of
        # the last place is already a relative gap above 1e-15.
        assert measured == pytest.approx(self.FINANCETOOLKIT, rel=0, abs=1e-15)


class TestMeasure:
    def test_puts_the_metrics_together(self):
        closes = _series(MIN_RETURNS + 41)
        returns = [value for _, value in simple_returns(closes)]
        metrics = measure(closes)

        assert (metrics.start, metrics.end) == (min(closes), max(closes))
        assert metrics.returns == MIN_RETURNS + 40
        assert metrics.confidence == 0.95
        assert metrics.annualized_return == pytest.approx(annualized_return(returns, 252))
        assert metrics.annualized_volatility == pytest.approx(annualized_volatility(returns, 252))
        assert metrics.max_drawdown == max_drawdown(closes)
        assert metrics.max_drawdown is not None
        assert metrics.calmar_ratio == pytest.approx(
            metrics.annualized_return / abs(metrics.max_drawdown.value)
        )
        assert metrics.historical_var == pytest.approx(historical_var(returns, 0.95))
        assert metrics.historical_cvar == pytest.approx(historical_cvar(returns, 0.95))
        assert metrics.gaussian_var == pytest.approx(gaussian_var(returns, 0.95))
        assert metrics.gaussian_cvar == pytest.approx(gaussian_cvar(returns, 0.95))

    def test_without_risk_free_rate_sharpe_and_sortino_are_none(self):
        metrics = measure(_series(MIN_RETURNS + 1))
        assert (metrics.sharpe_ratio, metrics.sortino_ratio, metrics.mean_risk_free_rate) == (
            None,
            None,
            None,
        )

    def test_the_rate_of_each_day_is_taken_from_its_return(self):
        closes = _series(MIN_RETURNS + 21)
        dated = simple_returns(closes)
        # The rate goes from 0 % to 5 % halfway through the series.
        rates = {day: (0.0 if i < len(dated) // 2 else 0.05) for i, (day, _) in enumerate(dated)}
        excess = excess_returns([value for _, value in dated], list(rates.values()), 252)

        metrics = measure(closes, risk_free_rates=rates)

        assert metrics.sharpe_ratio == pytest.approx(sharpe_ratio(excess.tolist(), 252))
        assert metrics.sortino_ratio == pytest.approx(sortino_ratio(excess.tolist(), 252))
        assert metrics.mean_risk_free_rate == pytest.approx(np.mean(list(rates.values())))

    def test_a_rate_missing_for_a_day_is_an_error(self):
        closes = _series(MIN_RETURNS + 1)
        rates = {day: 0.03 for day, _ in simple_returns(closes)[1:]}
        with pytest.raises(RiskMetricsError, match="no risk-free rate"):
            measure(closes, risk_free_rates=rates)

    def test_a_series_too_short_is_refused(self):
        with pytest.raises(RiskMetricsError, match=f"at least {MIN_RETURNS}"):
            measure(_series(MIN_RETURNS))

    def test_a_confidence_outside_zero_and_one_is_refused(self):
        with pytest.raises(RiskMetricsError, match="confidence"):
            measure(_series(MIN_RETURNS + 1), confidence=95)

    def test_closes_that_never_fall_have_no_calmar_ratio(self):
        closes = {date(2020, 1, 1) + timedelta(days=i): 100.0 + i for i in range(MIN_RETURNS + 1)}
        metrics = measure(closes)
        assert metrics.max_drawdown is None
        assert metrics.calmar_ratio is None
