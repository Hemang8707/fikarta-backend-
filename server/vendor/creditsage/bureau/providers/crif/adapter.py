from __future__ import annotations

import json
from typing import Any

from creditsage.bureau.canonical.account import (
    AccountStatus,
    AccountType,
    CanonicalAccount,
    CanonicalSecurityDetail,
    HistoricalMetricRecord,
    OwnershipType,
)
from creditsage.bureau.canonical.employment import (
    CanonicalBureauIncome,
    CanonicalEmploymentRecord,
)
from creditsage.bureau.canonical.inquiry import CanonicalInquiry
from creditsage.bureau.canonical.installment import CanonicalInstallment
from creditsage.bureau.canonical.payment_history import (
    AlignmentStatus,
    CanonicalPaymentHistory,
    MonthlyPaymentObservation,
)
from creditsage.bureau.canonical.provenance import (
    ObservationProvenance,
    SourceType,
    SourceValueStatus,
)
from creditsage.bureau.canonical.report import (
    BureauMetadata,
    CanonicalBureauReport,
)
from creditsage.bureau.canonical.score import (
    CanonicalScore,
    CanonicalScoreFactor,
)
from creditsage.bureau.canonical.summary import (
    AccountSummaryMetrics,
    BureauReportSummary,
)
from creditsage.bureau.providers.crif.issues import (
    CrifParseResult,
    ParseIssue,
    ParseIssueSeverity,
    ParseStatus,
)
from creditsage.bureau.providers.crif.normalizers import (
    parse_compound_payment_token,
    parse_crif_date,
    parse_indian_currency,
    parse_installment_string,
    parse_month_year_token,
    split_history_tokens,
)
from creditsage.bureau.providers.crif.vocabulary import (
    CRIF_ADAPTER_VERSION,
    EXPECTED_PRODUCT_TYPE,
    EXPECTED_PRODUCT_VER,
    OBSERVED_ACCOUNT_STATUS_MAP,
    OBSERVED_ACCOUNT_TYPE_MAP,
    OBSERVED_OWNERSHIP_MAP,
    PROVIDER_NAME,
)


class CrifJsonAdapter:
    """Ingests native CRIF BBC Consumer Score v2 JSON payloads and produces a typed,

    immutable, provenanced CanonicalBureauReport wrapped in CrifParseResult.
    """

    def parse(self, raw_input: dict[str, Any] | str) -> CrifParseResult:
        issues: list[ParseIssue] = []

        if isinstance(raw_input, str):
            try:
                data: dict[str, Any] = json.loads(raw_input)
            except json.JSONDecodeError:
                issues.append(
                    ParseIssue(
                        path="<root>",
                        severity=ParseIssueSeverity.FATAL,
                        issue_code="INVALID_JSON",
                        message="Payload is not valid JSON",
                    )
                )
                return CrifParseResult(
                    status=ParseStatus.FAILED_UNMAPPABLE,
                    report=None,
                    issues=tuple(issues),
                    adapter_version=CRIF_ADAPTER_VERSION,
                    provider=PROVIDER_NAME,
                    layout_version="UNKNOWN",
                )
        else:
            data = raw_input

        if not isinstance(data, dict):
            issues.append(
                ParseIssue(
                    path="<root>",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="INVALID_PAYLOAD_TYPE",
                    message="Payload root must be a JSON dictionary",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version="UNKNOWN",
            )

        b2c_report = data.get("B2C-REPORT")
        if not isinstance(b2c_report, dict):
            issues.append(
                ParseIssue(
                    path="B2C-REPORT",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="MISSING_B2C_REPORT_ROOT",
                    message="Root payload is missing mandatory B2C-REPORT segment",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version="UNKNOWN",
            )

        header = b2c_report.get("HEADER-SEGMENT")
        if not isinstance(header, dict):
            issues.append(
                ParseIssue(
                    path="B2C-REPORT.HEADER-SEGMENT",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="MISSING_HEADER_SEGMENT",
                    message="Mandatory HEADER-SEGMENT is missing",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version="UNKNOWN",
            )

        product_type = str(header.get("PRODUCT-TYPE") or "")
        product_ver = str(header.get("PRODUCT-VER") or "")

        if product_type != EXPECTED_PRODUCT_TYPE:
            issues.append(
                ParseIssue(
                    path="B2C-REPORT.HEADER-SEGMENT.PRODUCT-TYPE",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="UNSUPPORTED_PRODUCT_TYPE",
                    message="Product type is unsupported by this adapter",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version=product_ver or "UNKNOWN",
            )

        if product_ver != EXPECTED_PRODUCT_VER:
            issues.append(
                ParseIssue(
                    path="B2C-REPORT.HEADER-SEGMENT.PRODUCT-VER",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="UNSUPPORTED_PRODUCT_VERSION",
                    message="Product version is unsupported by this adapter",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version=product_ver or "UNKNOWN",
            )

        report_data = b2c_report.get("REPORT-DATA")
        if not isinstance(report_data, dict):
            issues.append(
                ParseIssue(
                    path="B2C-REPORT.REPORT-DATA",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="MISSING_REPORT_DATA",
                    message="Mandatory REPORT-DATA segment is missing",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version=product_ver,
            )

        standard_data = report_data.get("STANDARD-DATA")
        if not isinstance(standard_data, dict):
            issues.append(
                ParseIssue(
                    path="B2C-REPORT.REPORT-DATA.STANDARD-DATA",
                    severity=ParseIssueSeverity.FATAL,
                    issue_code="MISSING_STANDARD_DATA",
                    message="Mandatory STANDARD-DATA segment is missing",
                )
            )
            return CrifParseResult(
                status=ParseStatus.FAILED_UNMAPPABLE,
                report=None,
                issues=tuple(issues),
                adapter_version=CRIF_ADAPTER_VERSION,
                provider=PROVIDER_NAME,
                layout_version=product_ver,
            )

        # 1. Parse Metadata (omitting batch_id, report_id per privacy review §11)
        issue_date, _, _ = parse_crif_date(header.get("DATE-OF-ISSUE"))
        request_date, _, _ = parse_crif_date(header.get("DATE-OF-REQUEST"))
        raw_status = header.get("STATUS")

        metadata = BureauMetadata(
            provider=PROVIDER_NAME,
            product_type=product_type,
            product_version=product_ver,
            adapter_version=CRIF_ADAPTER_VERSION,
            source_type=SourceType.CRIF_NATIVE_JSON,
            issue_date=issue_date,
            request_date=request_date,
            raw_status=str(raw_status) if raw_status is not None else None,
        )

        # 2. Parse Score
        score = self._parse_score(standard_data.get("SCORE"), issues)

        # 3. Parse Tradelines
        accounts = self._parse_tradelines(standard_data.get("TRADELINES"), issues)

        # 4. Parse Inquiries
        inquiries = self._parse_inquiries(standard_data.get("INQUIRY-HISTORY"), issues)

        # 5. Parse Summary
        summary = self._parse_summary(report_data.get("ACCOUNTS-SUMMARY"), issues)

        # 6. Parse Employment Details
        employment_records = self._parse_employment(
            standard_data.get("EMPLOYMENT-DETAILS"), issues
        )

        report_provenance = ObservationProvenance(
            source_provider=PROVIDER_NAME,
            source_product=product_type,
            source_layout_version=product_ver,
            source_type=SourceType.CRIF_NATIVE_JSON,
            source_path="B2C-REPORT",
        )

        report: CanonicalBureauReport | None = CanonicalBureauReport(
            metadata=metadata,
            score=score,
            accounts=accounts,
            inquiries=inquiries,
            summary=summary,
            employment_records=employment_records,
            provenance=report_provenance,
        )

        final_status = ParseStatus.SUCCESS
        if any(issue.severity == ParseIssueSeverity.FATAL for issue in issues):
            final_status = ParseStatus.FAILED_UNMAPPABLE
            report = None
        elif any(
            issue.severity in (ParseIssueSeverity.WARNING, ParseIssueSeverity.ERROR)
            for issue in issues
        ):
            final_status = ParseStatus.PARTIAL_SUCCESS

        return CrifParseResult(
            status=final_status,
            report=report,
            issues=tuple(issues),
            adapter_version=CRIF_ADAPTER_VERSION,
            provider=PROVIDER_NAME,
            layout_version=product_ver,
        )

    def _parse_score(
        self, score_data: Any, issues: list[ParseIssue]
    ) -> CanonicalScore | None:
        if not isinstance(score_data, list) or not score_data:
            return None

        first_score = score_data[0]
        if not isinstance(first_score, dict):
            issues.append(
                ParseIssue(
                    path="B2C-REPORT.REPORT-DATA.STANDARD-DATA.SCORE[0]",
                    severity=ParseIssueSeverity.WARNING,
                    issue_code="INVALID_SCORE_STRUCTURE",
                    message="Score element is not a dictionary",
                )
            )
            return None

        raw_val = first_score.get("VALUE")
        parsed_score: int | None = None
        score_status = SourceValueStatus.MISSING

        if raw_val is None:
            score_status = SourceValueStatus.MISSING
        else:
            raw_val_str = str(raw_val).strip()
            if raw_val_str == "":
                score_status = SourceValueStatus.EMPTY
            elif raw_val_str.lower() in {"null", "none"}:
                score_status = SourceValueStatus.NULL_LITERAL
            elif raw_val_str.isdigit():
                parsed_score = int(raw_val_str)
                score_status = SourceValueStatus.PARSED
            else:
                score_status = SourceValueStatus.SPECIAL_OR_UNKNOWN

        factors: list[CanonicalScoreFactor] = []
        raw_factors = first_score.get("FACTORS")
        if isinstance(raw_factors, list):
            for idx, f in enumerate(raw_factors):
                if isinstance(f, dict):
                    factors.append(
                        CanonicalScoreFactor(
                            provider_code=str(f.get("TYPE") or ""),
                            raw_description=str(f.get("DESC") or ""),
                            factor_ordinal=idx,
                        )
                    )

        return CanonicalScore(
            raw_score_value=str(raw_val) if raw_val is not None else None,
            parsed_numeric_score=parsed_score,
            score_value_status=score_status,
            provider_model_name=str(first_score.get("NAME"))
            if first_score.get("NAME") is not None
            else None,
            provider_description_code=str(first_score.get("DESCRIPTION"))
            if first_score.get("DESCRIPTION") is not None
            else None,
            factors=tuple(factors),
        )

    def _parse_tradelines(
        self, tradelines_data: Any, issues: list[ParseIssue]
    ) -> tuple[CanonicalAccount, ...]:
        if not isinstance(tradelines_data, list):
            return ()

        accounts: list[CanonicalAccount] = []
        for ordinal, tl in enumerate(tradelines_data):
            if not isinstance(tl, dict):
                issues.append(
                    ParseIssue(
                        path=f"TRADELINES[{ordinal}]",
                        severity=ParseIssueSeverity.WARNING,
                        issue_code="INVALID_TRADELINE_STRUCTURE",
                        message="Tradeline element is not a dictionary",
                    )
                )
                continue

            # Account identity is source_record_ordinal; account numbers dropped.
            raw_type = str(tl.get("ACCT-TYPE") or "")
            account_type = OBSERVED_ACCOUNT_TYPE_MAP.get(raw_type, AccountType.UNKNOWN)
            if account_type == AccountType.UNKNOWN and raw_type:
                issues.append(
                    ParseIssue(
                        path=f"TRADELINES[{ordinal}].ACCT-TYPE",
                        severity=ParseIssueSeverity.WARNING,
                        issue_code="UNKNOWN_ACCOUNT_TYPE",
                        message="Tradeline has unmapped account type",
                    )
                )

            raw_status = str(tl.get("ACCOUNT-STATUS") or "")
            account_status = OBSERVED_ACCOUNT_STATUS_MAP.get(
                raw_status.upper(), AccountStatus.UNKNOWN
            )
            if account_status == AccountStatus.UNKNOWN and raw_status:
                issues.append(
                    ParseIssue(
                        path=f"TRADELINES[{ordinal}].ACCOUNT-STATUS",
                        severity=ParseIssueSeverity.WARNING,
                        issue_code="UNKNOWN_ACCOUNT_STATUS",
                        message="Tradeline has unmapped account status",
                    )
                )

            raw_ownership = str(tl.get("OWNERSHIP-TYPE") or "")
            ownership_type = OBSERVED_OWNERSHIP_MAP.get(
                raw_ownership.upper(), OwnershipType.UNKNOWN
            )

            disbursed, disbursed_status, raw_disbursed = parse_indian_currency(
                tl.get("DISBURSED-AMT")
            )
            credit_limit, credit_limit_status, raw_credit_limit = parse_indian_currency(
                tl.get("CREDIT-LIMIT")
            )
            cash_limit, cash_limit_status, raw_cash_limit = parse_indian_currency(
                tl.get("CASH-LIMIT")
            )
            current_bal, current_bal_status, raw_current_bal = parse_indian_currency(
                tl.get("CURRENT-BAL")
            )
            overdue, overdue_status, raw_overdue = parse_indian_currency(
                tl.get("OVERDUE-AMT")
            )
            actual_pmt, _, _ = parse_indian_currency(tl.get("ACTUAL-PAYMENT"))
            last_paid, _, _ = parse_indian_currency(tl.get("LAST-PAID-AMOUNT"))
            write_off, _, _ = parse_indian_currency(tl.get("WRITE-OFF-AMT"))
            principal_write_off, _, _ = parse_indian_currency(
                tl.get("PRINCIPAL-WRITE-OFF-AMT")
            )

            open_dt, open_dt_status, _ = parse_crif_date(tl.get("DISBURSED-DT"))
            closed_dt, closed_dt_status, _ = parse_crif_date(tl.get("CLOSED-DT"))
            reported_dt, reported_dt_status, _ = parse_crif_date(tl.get("REPORTED-DT"))
            last_pmt_dt, last_pmt_dt_status, _ = parse_crif_date(
                tl.get("LAST-PAYMENT-DT")
            )

            # Installment parsing
            parsed_emi, emi_status, raw_freq, norm_freq, freq_status, raw_emi = (
                parse_installment_string(tl.get("INSTALLMENT-AMT"))
            )
            parsed_ob, ob_status, raw_ob = parse_indian_currency(tl.get("OBLIGATION"))
            if freq_status == SourceValueStatus.NULL_LITERAL:
                issues.append(
                    ParseIssue(
                        path=f"TRADELINES[{ordinal}].INSTALLMENT-AMT",
                        severity=ParseIssueSeverity.WARNING,
                        issue_code="LITERAL_NULL_FREQUENCY",
                        message="Installment frequency component is literal null",
                    )
                )

            installment = CanonicalInstallment(
                raw_installment=raw_emi,
                parsed_amount=parsed_emi,
                amount_status=emi_status,
                raw_frequency=raw_freq,
                normalized_frequency=norm_freq,
                frequency_status=freq_status,
                raw_obligation=raw_ob,
                parsed_obligation=parsed_ob,
                obligation_status=ob_status,
            )

            # Tenure
            tenure_months: int | None = None
            term_val = tl.get("ORIGINAL-TERM")
            if isinstance(term_val, int):
                tenure_months = term_val
            elif tl.get("REPAYMENT-TENURE"):
                try:
                    tenure_months = int(str(tl.get("REPAYMENT-TENURE")).strip())
                except ValueError:
                    tenure_months = None

            term_to_maturity: int | None = None
            if isinstance(tl.get("TERM-TO-MATURITY"), int):
                term_to_maturity = tl.get("TERM-TO-MATURITY")

            interest_rate_val, _, _ = parse_indian_currency(tl.get("INTEREST-RATE"))

            # Payment history
            payment_history, balance_hist, high_credit_hist, amt_paid_hist = (
                self._parse_histories(tl.get("HISTORY"), ordinal, issues)
            )

            # Security details
            security_details: list[CanonicalSecurityDetail] = []
            for sec in tl.get("SECURITY-DETAILS") or []:
                if isinstance(sec, dict):
                    sec_val, _, _ = parse_indian_currency(sec.get("SECURITY-VALUATION"))
                    security_details.append(
                        CanonicalSecurityDetail(
                            automobile_type=sec.get("AUTOMOBILE-TYPE"),
                            security_type=sec.get("SECURITY-TYPE"),
                            security_valuation=sec_val,
                        )
                    )

            # Bureau reported income
            inc_amount, inc_status, raw_inc = parse_indian_currency(
                tl.get("INCOME-AMOUNT")
            )
            bureau_income = CanonicalBureauIncome(
                raw_amount=raw_inc,
                parsed_amount=inc_amount,
                amount_status=inc_status,
                raw_frequency=tl.get("INCOME-FREQUENCY"),
                stated_occupation=tl.get("OCCUPATION"),
            )

            account_provenance = ObservationProvenance(
                source_provider=PROVIDER_NAME,
                source_product=EXPECTED_PRODUCT_TYPE,
                source_layout_version=EXPECTED_PRODUCT_VER,
                source_type=SourceType.CRIF_NATIVE_JSON,
                source_path=f"B2C-REPORT.REPORT-DATA.STANDARD-DATA.TRADELINES[{ordinal}]",
                record_ordinal=ordinal,
            )

            accounts.append(
                CanonicalAccount(
                    source_record_ordinal=ordinal,
                    raw_account_type=raw_type,
                    account_type=account_type,
                    raw_account_status=raw_status,
                    account_status=account_status,
                    raw_ownership_type=raw_ownership,
                    ownership_type=ownership_type,
                    raw_security_status=tl.get("SECURITY-STATUS"),
                    disbursed_amount=disbursed,
                    raw_disbursed_amount=raw_disbursed,
                    disbursed_amount_status=disbursed_status,
                    credit_limit=credit_limit,
                    raw_credit_limit=raw_credit_limit,
                    credit_limit_status=credit_limit_status,
                    cash_limit=cash_limit,
                    raw_cash_limit=raw_cash_limit,
                    cash_limit_status=cash_limit_status,
                    current_balance=current_bal,
                    raw_current_balance=raw_current_bal,
                    current_balance_status=current_bal_status,
                    overdue_amount=overdue,
                    raw_overdue_amount=raw_overdue,
                    overdue_amount_status=overdue_status,
                    installment=installment,
                    repayment_tenure_months=tenure_months,
                    term_to_maturity_months=term_to_maturity,
                    interest_rate=interest_rate_val,
                    open_date=open_dt,
                    open_date_status=open_dt_status,
                    closed_date=closed_dt,
                    closed_date_status=closed_dt_status,
                    reported_date=reported_dt,
                    reported_date_status=reported_dt_status,
                    last_payment_date=last_pmt_dt,
                    last_payment_date_status=last_pmt_dt_status,
                    actual_payment_amount=actual_pmt,
                    last_paid_amount=last_paid,
                    credit_grantor_name=tl.get("CREDIT-GRANTOR"),
                    credit_grantor_type=tl.get("CREDIT-GRANTOR-TYPE"),
                    credit_grantor_group=tl.get("CREDIT-GRANTOR-GROUP"),
                    wilful_default_status=tl.get("SUIT-FILED-WILFUL-DEFAULT-STATUS"),
                    written_off_settled_status=tl.get("WRITTEN-OFF-SETTLED-STATUS"),
                    write_off_amount=write_off,
                    principal_write_off_amount=principal_write_off,
                    bureau_reported_income=bureau_income,
                    security_details=tuple(security_details),
                    payment_history=payment_history,
                    balance_history=balance_hist,
                    high_credit_history=high_credit_hist,
                    amount_paid_history=amt_paid_hist,
                    provenance=account_provenance,
                )
            )

        return tuple(accounts)

    def _parse_histories(
        self, histories_data: Any, ordinal: int, issues: list[ParseIssue]
    ) -> tuple[
        CanonicalPaymentHistory | None,
        tuple[HistoricalMetricRecord, ...],
        tuple[HistoricalMetricRecord, ...],
        tuple[HistoricalMetricRecord, ...],
    ]:
        if not isinstance(histories_data, list):
            return None, (), (), ()

        payment_history: CanonicalPaymentHistory | None = None
        balance_records: list[HistoricalMetricRecord] = []
        high_credit_records: list[HistoricalMetricRecord] = []
        amt_paid_records: list[HistoricalMetricRecord] = []

        for h in histories_data:
            if not isinstance(h, dict):
                continue
            name = h.get("NAME")
            raw_dates = str(h.get("DATES") or "")
            raw_values = str(h.get("VALUES") or "")

            if name == "COMBINED-PAYMENT-HISTORY":
                date_tokens = split_history_tokens(raw_dates, "|")
                value_tokens = split_history_tokens(raw_values, "|")

                alignment_status = AlignmentStatus.PERFECT_MATCH
                if len(date_tokens) != len(value_tokens):
                    alignment_status = AlignmentStatus.LENGTH_MISMATCH
                    issues.append(
                        ParseIssue(
                            path=f"TRADELINES[{ordinal}].HISTORY[COMBINED-PAYMENT-HISTORY]",
                            severity=ParseIssueSeverity.WARNING,
                            issue_code="PAYMENT_HISTORY_LENGTH_MISMATCH",
                            message="Payment history dates and values counts mismatch",
                        )
                    )

                max_len = max(len(date_tokens), len(value_tokens))
                obs_list: list[MonthlyPaymentObservation] = []
                for idx in range(max_len):
                    dt_tok = date_tokens[idx] if idx < len(date_tokens) else ""
                    val_tok = value_tokens[idx] if idx < len(value_tokens) else ""

                    year: int | None = None
                    month: int | None = None
                    if dt_tok:
                        parsed_my = parse_month_year_token(dt_tok)
                        if parsed_my:
                            year, month = parsed_my

                    parsed_dpd, raw_dpd, dpd_status, raw_asset, asset_status = (
                        parse_compound_payment_token(val_tok)
                    )
                    if dpd_status == SourceValueStatus.MALFORMED:
                        alignment_status = AlignmentStatus.MALFORMED_TOKENS
                        issues.append(
                            ParseIssue(
                                path=f"TRADELINES[{ordinal}].HISTORY[COMBINED-PAYMENT-HISTORY]",
                                severity=ParseIssueSeverity.WARNING,
                                issue_code="MALFORMED_PAYMENT_TOKEN",
                                message="Malformed payment history status token",
                            )
                        )

                    obs_list.append(
                        MonthlyPaymentObservation(
                            year=year,
                            month=month,
                            month_label=dt_tok,
                            raw_compound_token=val_tok,
                            raw_dpd_token=raw_dpd,
                            parsed_dpd_days=parsed_dpd,
                            dpd_status=dpd_status,
                            raw_asset_classification=raw_asset,
                            asset_classification_status=asset_status,
                        )
                    )

                payment_history = CanonicalPaymentHistory(
                    observations=tuple(obs_list),
                    raw_dates_string=raw_dates,
                    raw_values_string=raw_values,
                    alignment_status=alignment_status,
                )

            elif name in (
                "CURRENT-BALANCE-HISTORY",
                "HIGH-CREDIT-HISTORY",
                "AMT-PAID-HISTORY",
            ):
                date_tokens = split_history_tokens(raw_dates, "|")
                value_tokens = split_history_tokens(raw_values, ",")

                max_len = max(len(date_tokens), len(value_tokens))
                records: list[HistoricalMetricRecord] = []
                for idx in range(max_len):
                    dt_tok = date_tokens[idx] if idx < len(date_tokens) else ""
                    val_tok = value_tokens[idx] if idx < len(value_tokens) else ""
                    amt, status, _ = parse_indian_currency(val_tok)
                    records.append(
                        HistoricalMetricRecord(
                            month_label=dt_tok,
                            raw_value=val_tok,
                            parsed_amount=amt,
                            status=status,
                        )
                    )

                if name == "CURRENT-BALANCE-HISTORY":
                    balance_records = records
                elif name == "HIGH-CREDIT-HISTORY":
                    high_credit_records = records
                elif name == "AMT-PAID-HISTORY":
                    amt_paid_records = records

        return (
            payment_history,
            tuple(balance_records),
            tuple(high_credit_records),
            tuple(amt_paid_records),
        )

    def _parse_inquiries(
        self, inquiries_data: Any, issues: list[ParseIssue]
    ) -> tuple[CanonicalInquiry, ...]:
        if not isinstance(inquiries_data, list):
            return ()

        inquiries: list[CanonicalInquiry] = []
        for ordinal, inq in enumerate(inquiries_data):
            if not isinstance(inq, dict):
                issues.append(
                    ParseIssue(
                        path=f"INQUIRY-HISTORY[{ordinal}]",
                        severity=ParseIssueSeverity.WARNING,
                        issue_code="INVALID_INQUIRY_STRUCTURE",
                        message="Inquiry element is not a dictionary",
                    )
                )
                continue

            inq_dt, inq_dt_status, raw_inq_dt = parse_crif_date(inq.get("INQUIRY-DT"))
            amt, amt_status, raw_amt = parse_indian_currency(inq.get("AMOUNT"))

            inq_provenance = ObservationProvenance(
                source_provider=PROVIDER_NAME,
                source_product=EXPECTED_PRODUCT_TYPE,
                source_layout_version=EXPECTED_PRODUCT_VER,
                source_type=SourceType.CRIF_NATIVE_JSON,
                source_path=f"B2C-REPORT.REPORT-DATA.STANDARD-DATA.INQUIRY-HISTORY[{ordinal}]",
                record_ordinal=ordinal,
            )

            inquiries.append(
                CanonicalInquiry(
                    source_record_ordinal=ordinal,
                    inquiry_date=inq_dt,
                    raw_inquiry_date=raw_inq_dt or "",
                    inquiry_date_status=inq_dt_status,
                    amount=amt,
                    raw_amount=raw_amt,
                    amount_status=amt_status,
                    purpose_type=inq.get("CREDIT-INQ-PURPS-TYPE"),
                    lender_name=inq.get("LENDER-NAME"),
                    lender_type=inq.get("LENDER-TYPE"),
                    ownership_type=inq.get("OWNERSHIP-TYPE"),
                    inquiry_stage=inq.get("CREDIT-INQUIRY-STAGE"),
                    provenance=inq_provenance,
                )
            )

        return tuple(inquiries)

    def _parse_summary(
        self, summary_data: Any, issues: list[ParseIssue]
    ) -> BureauReportSummary | None:
        if not isinstance(summary_data, dict):
            return None

        primary = self._parse_summary_metrics(
            summary_data.get("PRIMARY-ACCOUNTS-SUMMARY")
        )
        secondary = self._parse_summary_metrics(
            summary_data.get("SECONDARY-ACCOUNTS-SUMMARY")
        )

        return BureauReportSummary(
            primary_accounts=primary, secondary_accounts=secondary
        )

    def _parse_summary_metrics(self, metrics_dict: Any) -> AccountSummaryMetrics | None:
        if not isinstance(metrics_dict, dict) or not metrics_dict:
            return None

        def _to_int(val: Any) -> int | None:
            if val is None:
                return None
            try:
                return int(str(val).strip().replace(",", ""))
            except ValueError:
                return None

        def _to_dec(val: Any) -> Any:
            amt, _, _ = parse_indian_currency(val)
            return amt

        return AccountSummaryMetrics(
            active_accounts_count=_to_int(metrics_dict.get("ACTIVE-ACCOUNTS")),
            number_of_accounts=_to_int(metrics_dict.get("NUMBER-OF-ACCOUNTS")),
            overdue_accounts_count=_to_int(metrics_dict.get("OVERDUE-ACCOUNTS")),
            secured_accounts_count=_to_int(metrics_dict.get("SECURED-ACCOUNTS")),
            unsecured_accounts_count=_to_int(metrics_dict.get("UNSECURED-ACCOUNTS")),
            total_current_balance=_to_dec(metrics_dict.get("TOTAL-CURRENT-BALANCE")),
            total_disbursed_amount=_to_dec(metrics_dict.get("TOTAL-DISBURSED-AMT")),
            total_overdue_amount=_to_dec(metrics_dict.get("TOTAL-AMT-OVERDUE")),
            total_sanctioned_amount=_to_dec(metrics_dict.get("TOTAL-SANCTIONED-AMT")),
        )

    def _parse_employment(
        self, emp_data: Any, issues: list[ParseIssue]
    ) -> tuple[CanonicalEmploymentRecord, ...]:
        if not isinstance(emp_data, list):
            return ()

        records: list[CanonicalEmploymentRecord] = []
        for item in emp_data:
            if not isinstance(item, dict):
                continue
            detail = item.get("EMPLOYMENT-DETAIL")
            if isinstance(detail, dict):
                f_dt, _, _ = parse_crif_date(detail.get("FIRST-REPORTED-DT"))
                l_dt, _, _ = parse_crif_date(detail.get("LAST-REPORTED-DT"))
                records.append(
                    CanonicalEmploymentRecord(
                        occupation=detail.get("OCCUPATION"),
                        acct_type=detail.get("ACCT-TYPE"),
                        source_indicator=detail.get("SOURCE-INDICATOR"),
                        first_reported_date=f_dt,
                        last_reported_date=l_dt,
                    )
                )

        return tuple(records)
