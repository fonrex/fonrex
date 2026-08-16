"""Transport-independent errors raised by technical-analysis services."""


class TechnicalAnalysisError(Exception):
    """Base error carrying a safe, user-facing explanation."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class InvalidIndicator(TechnicalAnalysisError):
    """The requested indicator is unknown or invalid."""


class UnsupportedIndicatorResolution(TechnicalAnalysisError):
    """The indicator cannot be calculated at the requested resolution."""


class TechnicalDataNotFound(TechnicalAnalysisError):
    """The requested asset or its historical prices do not exist."""


class InsufficientHistoricalData(TechnicalAnalysisError):
    """There are not enough observations to calculate the indicator."""


class IndicatorCalculationFailed(TechnicalAnalysisError):
    """The indicator library rejected otherwise valid input data."""
