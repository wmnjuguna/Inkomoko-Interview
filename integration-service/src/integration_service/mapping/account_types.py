"""ERP account type and usage to Fineract GL numbers, and the GL sync body for the plugin.

This is the only place these numbers live. Fineract's GL account types are
1 ASSET, 2 LIABILITY, 3 EQUITY, 4 INCOME, 5 EXPENSE, and its usages are 1 DETAIL and
2 HEADER (see API.md, "Fineract plugin").

manualEntriesAllowed is True for DETAIL accounts and False for HEADER accounts. A HEADER
only groups accounts and nothing may post to it. A DETAIL account can take a manual
correcting entry in Fineract, which is what an accountant expects there. Loan postings
work either way. The value is part of the synced data, so changing it later makes the
plugin answer 409 for every account already synced."""

from __future__ import annotations

from typing import Any

from integration_service.clients.erp import ErpAccount

FINERACT_GL_TYPES: dict[str, int] = {
    "ASSET": 1,
    "LIABILITY": 2,
    "EQUITY": 3,
    "INCOME": 4,
    "EXPENSE": 5,
}
FINERACT_GL_USAGES: dict[str, int] = {"DETAIL": 1, "HEADER": 2}


class UnknownAccountTypeError(ValueError):
    """The ERP sent an account type Fineract has no number for."""


class UnknownAccountUsageError(ValueError):
    """The ERP sent an account usage that is neither HEADER nor DETAIL."""


class MissingParentReferenceError(ValueError):
    """A child account was given without its parent's ERP reference."""


def fineract_gl_type(erp_type: str) -> int:
    try:
        return FINERACT_GL_TYPES[erp_type]
    except KeyError:
        raise UnknownAccountTypeError(
            f"Unknown ERP account type {erp_type!r}; expected one of {sorted(FINERACT_GL_TYPES)}"
        ) from None


def fineract_gl_usage(usage: str) -> int:
    try:
        return FINERACT_GL_USAGES[usage]
    except KeyError:
        raise UnknownAccountUsageError(
            f"Unknown ERP account usage {usage!r}; expected one of {sorted(FINERACT_GL_USAGES)}"
        ) from None


def build_fineract_gl_body(
    account: ErpAccount, parent_reference: str | None = None
) -> dict[str, Any]:
    """The plugin body for one ERP account. parent_reference is the parent's ERP reference."""
    body: dict[str, Any] = {
        "name": account.name,
        "glCode": account.code,
        "type": fineract_gl_type(account.type),
        "usage": fineract_gl_usage(account.usage),
        "manualEntriesAllowed": account.usage == "DETAIL",
        "description": f"Synced from ERP account {account.external_reference}",
    }
    if account.parent_code is not None:
        if parent_reference is None:
            raise MissingParentReferenceError(
                f"{account.external_reference} has parent code {account.parent_code}, "
                "but no parent reference was given"
            )
        body["parentAccount"] = parent_reference
    return body
