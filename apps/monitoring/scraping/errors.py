"""Public, safe diagnostics shared by the fetching and extraction modules."""

from typing import Any


class ScrapingError(Exception):
    """An expected failure, with a stable code and a safe user-facing message.

    Never put page HTML, credentials, raw URLs or exception messages in details.
    Technical exceptions can be chained by callers for controlled debugging.
    """

    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)
