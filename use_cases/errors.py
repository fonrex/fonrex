"""Transport-agnostic errors raised by application use cases."""


class UseCaseError(Exception):
    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


class InvalidInput(UseCaseError):
    pass


class ResourceNotFound(UseCaseError):
    pass


class DependencyUnavailable(UseCaseError):
    pass


class UpstreamFailure(UseCaseError):
    pass
