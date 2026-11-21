"""Application-level validation errors.

Every error carries a stable ``code`` (useful for tests and for the UI) and a
human readable Chinese message.  Import failures are *all* raised before a
single row of the current daily report is touched, which is what guarantees
"失败保留原日报和结果".
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected, user-facing failures."""

    code = "app_error"
    http_status = 400

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = list(self.details)
        return payload


class NotFound(AppError):
    code = "not_found"
    http_status = 404


class ImportRejected(AppError):
    """The submitted report is malformed; nothing was persisted."""

    code = "import_rejected"
    http_status = 422


class RevisionConflict(AppError):
    """A non-increasing revision tried to replace an already accepted report.

    This is how "只接受更高修订整日替换" is enforced: a day is only ever
    replaced by a strictly higher revision, never edited in place.
    """

    code = "revision_conflict"
    http_status = 409


class ReportNotFound(AppError):
    """No current report exists for the requested store-day."""

    code = "report_missing"
    http_status = 404
