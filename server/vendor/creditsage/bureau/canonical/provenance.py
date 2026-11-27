from __future__ import annotations

from enum import StrEnum

from creditsage.bureau.canonical.base import SafeBureauModel


class SourceType(StrEnum):
    CRIF_NATIVE_JSON = "CRIF_NATIVE_JSON"
    CRIF_RENDERED_PDF = "CRIF_RENDERED_PDF"


class SourceValueStatus(StrEnum):
    PARSED = "PARSED"
    MISSING = "MISSING"
    EMPTY = "EMPTY"
    NULL_LITERAL = "NULL_LITERAL"
    SPECIAL_OR_UNKNOWN = "SPECIAL_OR_UNKNOWN"
    MALFORMED = "MALFORMED"


class ObservationProvenance(SafeBureauModel, frozen=True):
    source_provider: str
    source_product: str
    source_layout_version: str
    source_type: SourceType
    source_path: str
    record_ordinal: int | None = None

    # Optional multi-source PDF compatibility slots (None for JSON):
    page_number: int | None = None
    section_name: str | None = None
    field_label: str | None = None
    extraction_method: str | None = None
    extraction_probability: float | None = None

    def __repr__(self) -> str:
        return (
            f"ObservationProvenance(provider={self.source_provider!r}, "
            f"type={self.source_type.value!r}, path={self.source_path!r})"
        )
