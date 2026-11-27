from __future__ import annotations

import re
from enum import StrEnum

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.report import CanonicalBureauReport

SAFE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_issue_path(path: str, field_name: str | None = None) -> str:
    """Safely constructs an issue path without leaking unsanitized arbitrary text."""
    if not field_name:
        return path
    if SAFE_KEY_PATTERN.match(field_name):
        return f"{path}.{field_name}" if path else field_name
    return f"{path}.<unknown_key>" if path else "<unknown_key>"


class ParseIssueSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class ParseIssue(SafeBureauModel, frozen=True):
    path: str
    severity: ParseIssueSeverity
    issue_code: str
    message: str

    def __repr__(self) -> str:
        return (
            f"ParseIssue(severity={self.severity.value!r}, "
            f"code={self.issue_code!r}, path={self.path!r})"
        )


class ParseStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED_UNMAPPABLE = "FAILED_UNMAPPABLE"


class CrifParseResult(SafeBureauModel, frozen=True):
    status: ParseStatus
    report: CanonicalBureauReport | None = None
    issues: tuple[ParseIssue, ...] = ()
    adapter_version: str
    provider: str
    layout_version: str

    def __repr__(self) -> str:
        return (
            f"CrifParseResult(status={self.status.value!r}, "
            f"issues_count={len(self.issues)}, "
            f"has_report={self.report is not None})"
        )
