class RailError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Refuse(RailError):
    """Fail closed before provider execution or promotion."""


class ProviderError(RailError):
    pass
