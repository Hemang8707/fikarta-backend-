from __future__ import annotations

import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from creditsage.bureau.canonical.installment import InstallmentFrequency
from creditsage.bureau.canonical.provenance import SourceValueStatus
from creditsage.bureau.providers.crif.vocabulary import (
    MONTH_TOKEN_MAP,
    OBSERVED_FREQUENCY_MAP,
)

INDIAN_COMMA_REGEX = re.compile(r"^-?(?:\d{1,2}(?:,\d{2})*,\d{3}|\d+)(?:\.\d+)?$")
PLAIN_NUMERIC_REGEX = re.compile(r"^-?\d+(?:\.\d+)?$")


def parse_indian_currency(
    raw: Any,
) -> tuple[Decimal | None, SourceValueStatus, str | None]:
    """Deterministically parses Indian comma currency strings into exact Decimals.

    Validates Indian comma grouping before removing commas. Rejects malformed
    groupings (e.g. '1,2,3', '15,,000').
    Never converts missing or malformed values to 0. Preserves signed negative values.
    """
    if raw is None:
        return None, SourceValueStatus.MISSING, None

    raw_str = str(raw).strip()
    if raw_str == "":
        return None, SourceValueStatus.EMPTY, raw_str
    if raw_str.lower() in {"null", "none"}:
        return None, SourceValueStatus.NULL_LITERAL, raw_str

    if not (PLAIN_NUMERIC_REGEX.match(raw_str) or INDIAN_COMMA_REGEX.match(raw_str)):
        return None, SourceValueStatus.MALFORMED, raw_str

    cleaned = raw_str.replace(",", "")
    try:
        val = Decimal(cleaned)
        return val, SourceValueStatus.PARSED, raw_str
    except InvalidOperation:
        return None, SourceValueStatus.MALFORMED, raw_str


def parse_crif_date(
    raw: Any,
) -> tuple[datetime.date | None, SourceValueStatus, str | None]:
    """Deterministically parses CRIF DD-MM-YYYY dates.

    Handles explicit '0', empty, or null without interpreting '0' as epoch.
    """
    if raw is None:
        return None, SourceValueStatus.MISSING, None

    raw_str = str(raw).strip()
    if raw_str == "":
        return None, SourceValueStatus.EMPTY, raw_str
    if raw_str.lower() in {"null", "none"}:
        return None, SourceValueStatus.NULL_LITERAL, raw_str
    if raw_str == "0" or raw == 0:
        return None, SourceValueStatus.SPECIAL_OR_UNKNOWN, raw_str

    try:
        parsed = datetime.datetime.strptime(raw_str, "%d-%m-%Y").date()
        return parsed, SourceValueStatus.PARSED, raw_str
    except ValueError:
        return None, SourceValueStatus.MALFORMED, raw_str


def parse_month_year_token(token: str) -> tuple[int, int] | None:
    """Parses a month-year token such as 'Jul:2026' into (year, month).

    Returns None if malformed or empty.
    """
    cleaned = token.strip()
    if ":" not in cleaned:
        return None

    month_str, year_str = cleaned.split(":", 1)
    month_num = MONTH_TOKEN_MAP.get(month_str.strip().upper())
    if month_num is None:
        return None

    try:
        year_num = int(year_str.strip())
        if year_num < 100:
            year_num += 2000
        return year_num, month_num
    except ValueError:
        return None


def split_history_tokens(token_string: str, delimiter: str = "|") -> list[str]:
    """Splits delimited sequence tokens while preserving internal empty positions.

    Strips exactly one trailing terminal delimiter if present, but never shifts
    internal positions (e.g. 'a||b|' -> ['a', '', 'b']).
    """
    if not token_string:
        return []

    target = token_string
    if target.endswith(delimiter):
        target = target[: -len(delimiter)]

    return target.split(delimiter)


def parse_compound_payment_token(
    token: str,
) -> tuple[int | None, str | None, SourceValueStatus, str | None, SourceValueStatus]:
    """Parses compound payment token (e.g. '030/STD', 'XXX/STD', '819/DBT').

    Returns (parsed_dpd_days, raw_dpd_token, dpd_status, raw_asset, asset_status).
    Numeric DPD and asset classification remain independent without mutual precedence.
    Arbitrary integer DPD is uncapped (int | None).
    """
    cleaned = token.strip()
    if not cleaned:
        return None, None, SourceValueStatus.EMPTY, None, SourceValueStatus.EMPTY

    if "/" not in cleaned:
        return (
            None,
            cleaned,
            SourceValueStatus.MALFORMED,
            None,
            SourceValueStatus.MISSING,
        )

    dpd_part, asset_part = cleaned.split("/", 1)
    raw_dpd = dpd_part.strip()
    raw_asset = asset_part.strip() or None

    if not raw_dpd:
        parsed_dpd = None
        dpd_status = SourceValueStatus.EMPTY
    elif raw_dpd.upper() == "XXX":
        parsed_dpd = None
        dpd_status = SourceValueStatus.SPECIAL_OR_UNKNOWN
    else:
        try:
            parsed_dpd = int(raw_dpd)
            dpd_status = SourceValueStatus.PARSED
        except ValueError:
            parsed_dpd = None
            dpd_status = SourceValueStatus.MALFORMED

    if raw_asset is None:
        asset_status = SourceValueStatus.MISSING
    elif raw_asset == "":
        asset_status = SourceValueStatus.EMPTY
    else:
        asset_status = SourceValueStatus.PARSED

    return parsed_dpd, raw_dpd, dpd_status, raw_asset, asset_status


def parse_installment_string(
    raw: Any,
) -> tuple[
    Decimal | None,
    SourceValueStatus,
    str | None,
    InstallmentFrequency | None,
    SourceValueStatus,
    str | None,
]:
    """Parses installment strings like '15,000/Monthly' or '4,500/null'.

    Returns (parsed_amount, amount_status, raw_frequency, normalized_frequency,
    freq_status, raw_str). Missing EMI returns parsed_amount=None, NEVER Decimal('0').
    """
    if raw is None:
        return (
            None,
            SourceValueStatus.MISSING,
            None,
            None,
            SourceValueStatus.MISSING,
            None,
        )

    raw_str = str(raw).strip()
    if raw_str == "":
        return (
            None,
            SourceValueStatus.EMPTY,
            None,
            None,
            SourceValueStatus.EMPTY,
            raw_str,
        )
    if raw_str.lower() in {"null", "none"}:
        return (
            None,
            SourceValueStatus.NULL_LITERAL,
            None,
            None,
            SourceValueStatus.NULL_LITERAL,
            raw_str,
        )

    if "/" in raw_str:
        amount_part, freq_part = raw_str.split("/", 1)
        amount_val, amount_status, _ = parse_indian_currency(amount_part)
        raw_freq = freq_part.strip()
        if raw_freq.lower() in {"null", "none"}:
            norm_freq = None
            freq_status = SourceValueStatus.NULL_LITERAL
        else:
            norm_freq = OBSERVED_FREQUENCY_MAP.get(
                raw_freq.upper(), InstallmentFrequency.UNKNOWN
            )
            freq_status = (
                SourceValueStatus.PARSED
                if norm_freq != InstallmentFrequency.UNKNOWN
                else SourceValueStatus.SPECIAL_OR_UNKNOWN
            )
        return amount_val, amount_status, raw_freq, norm_freq, freq_status, raw_str

    amount_val, amount_status, _ = parse_indian_currency(raw_str)
    return (
        amount_val,
        amount_status,
        None,
        None,
        SourceValueStatus.MISSING,
        raw_str,
    )
