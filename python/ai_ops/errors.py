class RailError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Refuse(RailError):
    """Fail closed before provider execution or promotion."""


class DirtyWorktree(Refuse):
    """Worktree integrity changed during the job — exit 2, per the published table.

    A Refuse, because it is still fail-closed and nothing was promoted, but with
    the exit code the contract promises for this specific case. It used to raise
    a bare Refuse (exit 1, generic) while the WEAKER integrity violation a few
    lines away correctly produced `dirty`/exit 2.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=2)


class ProviderError(RailError):
    pass
