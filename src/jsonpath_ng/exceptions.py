class JSONPathError(Exception):
    pass


class JsonPathLexerError(JSONPathError):
    pass


class JsonPathParserError(JSONPathError):
    pass


class UpdatePlanError(JSONPathError, ValueError):
    """An update plan cannot be created or applied."""

    def __init__(  # noqa: B042 - Exception.__init__() rejects keyword arguments.
        self,
        message: str,
        *,
        path: str | None = None,
        paths: tuple[str, ...] | None = None,
        expected: object = None,
        actual: object = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.paths = paths
        self.expected = expected
        self.actual = actual


class UpdatePlanConflictError(UpdatePlanError):
    """Two entries in an update plan target the same location or overlap."""


class UpdatePlanStaleError(UpdatePlanError):
    """A location in an update plan no longer exists or has a different value."""


# Provide alternate spellings that read naturally in ``except`` clauses.
ConflictUpdatePlanError = UpdatePlanConflictError
StaleUpdatePlanError = UpdatePlanStaleError
UpdatePlanConflict = UpdatePlanConflictError
UpdatePlanStale = UpdatePlanStaleError
