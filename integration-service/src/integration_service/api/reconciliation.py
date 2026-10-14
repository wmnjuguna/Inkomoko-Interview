"""POST /api/reconciliation (Part K): compares Fineract loan transactions with ERP journal
entries over a date range or a set of references. Read only, and the report is not stored:
it is rebuilt from the sources on every call. The work is in services/reconciliation.py."""

from __future__ import annotations

from typing import Any

from flask_openapi3.blueprint import APIBlueprint
from flask_openapi3.models.tag import Tag

from integration_service.api.loans import correlation_id, fineract, gl_accounts
from integration_service.clients.erp import ErpClient
from integration_service.config import get_settings
from integration_service.db import get_engine
from integration_service.logs import log_field
from integration_service.schemas.common import API_ERRORS, UPSTREAM_ERRORS
from integration_service.schemas.reconciliation import (
    ErpSide,
    FineractSide,
    Item,
    Line,
    NotCompared,
    OurSide,
    ReconciliationRequest,
    ReconciliationResponse,
    Scope,
    Summary,
)
from integration_service.services import reconciliation as recon

bp = APIBlueprint(
    "reconciliation",
    __name__,
    abp_tags=[Tag(name="reconciliation")],
    abp_security=[{"api_key": []}],
    abp_responses=API_ERRORS,
)


@bp.post(
    "/api/reconciliation",
    summary="Compare Fineract and the ERP",
    description="Send either from and to (at most 31 days) or references (at most 100). "
    "Every disbursement and repayment of every loan we know is matched to the ERP journal "
    "entry with the same key (the op key, e.g. RPY:FIN-1). Each item is MATCHED, "
    "MISSING_IN_ERP, MISSING_IN_FINERACT, AMOUNT_MISMATCH (with a reason per difference) or "
    "IN_FLIGHT (our outbox has not sent it yet). The ERP lines are compared with the lines "
    "our mapping builds from Fineract's own transaction. Read only; nothing is stored.",
    responses={200: ReconciliationResponse, **UPSTREAM_ERRORS},
)
def post_reconciliation(body: ReconciliationRequest) -> tuple[dict[str, Any], int]:
    scope = recon.scope_of(body)
    settings = get_settings()
    erp = ErpClient(settings.erp_base_url, settings.erp_api_key, settings.erp_timeout_seconds)
    try:
        with fineract() as f:
            report = recon.run_reconciliation(
                get_engine(), f, erp, scope, gl_accounts(), correlation_id()
            )
    finally:
        erp.close()
    summary = report.summary()
    log_field("status", "RECONCILED")
    log_field("summary", summary)
    response = ReconciliationResponse(
        scope=Scope(
            date_from=body.date_from,
            date_to=body.date_to,
            references=body.references,
        ),
        generated_at=recon.generated_at(),
        compared_types=recon.COMPARED_TYPES,
        summary=Summary.model_validate(summary),
        not_compared=NotCompared(
            fineract_types=report.not_compared_fineract, erp_entries=report.not_compared_erp
        ),
        warnings=report.warnings,
        items=[_item(i) for i in report.items],
    )
    return response.model_dump(mode="json", by_alias=True), 200


def _lines(lines: tuple[recon.Line, ...] | None) -> list[Line] | None:
    if lines is None:
        return None
    return [Line(account_code=x.account_code, debit=x.debit, credit=x.credit) for x in lines]


def _item(item: recon.Item) -> Item:
    f, e, o = item.fineract, item.erp, item.ours
    return Item(
        key=item.key,
        outcome=item.outcome.value,
        loan_reference=item.loan_reference,
        type=item.kind,
        fineract=FineractSide(**vars(f)) if f else None,
        erp=ErpSide(
            entry_date=e.entry_date,
            source=e.source,
            reverses=e.reverses,
            total=e.total,
            lines=_lines(e.lines) or [],
        )
        if e
        else None,
        expected_lines=_lines(item.expected_lines),
        ours=OurSide(status=o.status, erp_status=o.erp_status) if o else None,
        reasons=list(item.reasons),
    )
