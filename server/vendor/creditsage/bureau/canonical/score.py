from __future__ import annotations

from creditsage.bureau.canonical.base import SafeBureauModel
from creditsage.bureau.canonical.provenance import SourceValueStatus


class CanonicalScoreFactor(SafeBureauModel, frozen=True):
    provider_code: str
    raw_description: str
    factor_ordinal: int

    def __repr__(self) -> str:
        return (
            f"CanonicalScoreFactor(ordinal={self.factor_ordinal}, "
            f"code={self.provider_code!r})"
        )


class CanonicalScore(SafeBureauModel, frozen=True):
    raw_score_value: str | None
    parsed_numeric_score: int | None
    score_value_status: SourceValueStatus
    provider_model_name: str | None = None
    provider_description_code: str | None = None
    factors: tuple[CanonicalScoreFactor, ...] = ()

    def __repr__(self) -> str:
        return (
            f"CanonicalScore(status={self.score_value_status.value!r}, "
            f"factors_count={len(self.factors)})"
        )
